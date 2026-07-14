from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner

from .firmware import validate_meshcore_firmware
from .ota import OTA_SERVICE_UUID, upload_firmware

SOCKET_PATH = Path("/data/meshcore-ota-worker.sock")
FIRMWARE_PATH = Path("/data/firmware.bin")
USED_MARKER = Path("/data/destructive-write-attempted")
BLUEZ_ADAPTER = "hci0"
MINIMUM_FLASH_RSSI = -85


def _write_durable_marker(value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(USED_MARKER, flags, 0o600)
    try:
        payload = (value + "\n").encode("ascii")
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("Short write while persisting destructive-attempt marker")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(USED_MARKER.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


async def _scan() -> list[dict[str, Any]]:
    discovered = await BleakScanner.discover(
        timeout=18,
        return_adv=True,
        bluez={"adapter": BLUEZ_ADAPTER},
    )
    wanted = OTA_SERVICE_UUID.casefold()
    devices: list[dict[str, Any]] = []
    for device, advertisement in discovered.values():
        uuids = {str(value).casefold() for value in advertisement.service_uuids}
        if wanted not in uuids:
            continue
        devices.append(
            {
                "address": device.address,
                "name": device.name or advertisement.local_name or "Meshtastic OTA",
                "rssi": advertisement.rssi,
                "service_uuid": OTA_SERVICE_UUID,
                "manufacturer_data": {
                    str(company): bytes(value).hex()
                    for company, value in advertisement.manufacturer_data.items()
                },
            }
        )
    return sorted(devices, key=lambda item: item["address"].casefold())


async def _find_exact(address: str, timeout: float = 35):
    wanted_address = address.casefold()
    wanted_service = OTA_SERVICE_UUID.casefold()
    matched_advertisement: dict[str, Any] = {}

    def matches(device, advertisement) -> bool:
        uuids = {str(value).casefold() for value in advertisement.service_uuids}
        matched = (
            device.address.casefold() == wanted_address and wanted_service in uuids
        )
        if matched:
            matched_advertisement["value"] = advertisement
        return matched

    device = await BleakScanner.find_device_by_filter(
        matches,
        timeout=timeout,
        bluez={"adapter": BLUEZ_ADAPTER},
    )
    return device, matched_advertisement.get("value")


async def _flash(
    writer: asyncio.StreamWriter, address: str, expected_sha256: str
) -> dict[str, Any]:
    if USED_MARKER.exists():
        raise RuntimeError(
            "This add-on instance has already attempted a destructive write; reinstall it before any further attempt"
        )
    image = FIRMWARE_PATH.read_bytes()
    info = validate_meshcore_firmware(image)
    actual_sha256 = hashlib.sha256(image).hexdigest()
    if actual_sha256 != expected_sha256 or actual_sha256 != info.sha256:
        raise RuntimeError("Staged firmware changed after validation")

    device, advertisement = await _find_exact(address)
    if device is None:
        raise RuntimeError(
            "The exact confirmed BLE address was not advertising the Meshtastic OTA service"
        )
    local_name = (
        (advertisement.local_name if advertisement else None) or device.name or ""
    )
    if "meshtastic" not in local_name.casefold():
        raise RuntimeError(
            f"Exact address advertised an unexpected local name: {local_name or 'missing'}"
        )
    rssi = int(advertisement.rssi) if advertisement is not None else -999
    if rssi < MINIMUM_FLASH_RSSI:
        raise RuntimeError(
            f"BLE signal is too weak for a non-resumable flash ({rssi} dBm; require at least {MINIMUM_FLASH_RSSI} dBm)"
        )

    disconnected = asyncio.Event()

    def on_disconnect(_client) -> None:
        disconnected.set()

    async with BleakClient(
        device,
        disconnected_callback=on_disconnect,
        services=[OTA_SERVICE_UUID],
        timeout=30,
        bluez={"adapter": BLUEZ_ADAPTER},
    ) as client:

        def mark_destructive_start() -> None:
            # Persist and flush before OTA_SIZE can start erase/write.
            _write_durable_marker(actual_sha256)

        def progress(sent: int, total: int) -> None:
            writer.write(
                json.dumps(
                    {"event": "progress", "sent": sent, "total": total},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )

        result = await upload_firmware(
            client,
            image,
            progress=progress,
            before_initialize=mark_destructive_start,
            disconnected=disconnected,
        )
        await writer.drain()
    return {
        "event": "complete",
        "bytes_sent": result.bytes_sent,
        "chunk_size": result.chunk_size,
        "sha256": actual_sha256,
        "address": address,
        "rssi": rssi,
        "reboot_observed": result.reboot_observed,
    }


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, lock: asyncio.Lock
) -> None:
    async with lock:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(line) > 4096:
                raise RuntimeError("Worker request is too large")
            request = json.loads(line)
            action = request.get("action")
            if action == "scan":
                await _send(writer, {"event": "scan", "devices": await _scan()})
            elif action == "flash":
                address = str(request.get("address", "")).strip()
                expected_sha256 = str(request.get("sha256", "")).strip().casefold()
                if not address or len(expected_sha256) != 64:
                    raise RuntimeError(
                        "Exact BLE address and firmware SHA-256 are required"
                    )
                await _send(writer, await _flash(writer, address, expected_sha256))
            else:
                raise RuntimeError("Unsupported worker action")
        except Exception as exc:
            await _send(
                writer,
                {"event": "error", "error": str(exc), "type": type(exc).__name__},
            )
        finally:
            writer.close()
            await writer.wait_closed()


async def run() -> None:
    SOCKET_PATH.unlink(missing_ok=True)
    lock = asyncio.Lock()
    server = await asyncio.start_unix_server(
        lambda reader, writer: _handle(reader, writer, lock), path=SOCKET_PATH
    )
    os.chmod(SOCKET_PATH, 0o600)
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
