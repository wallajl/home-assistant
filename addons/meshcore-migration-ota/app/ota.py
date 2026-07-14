from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Protocol

OTA_SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
OTA_TX_CHARACTERISTIC_UUID = "62ec0272-3ec5-11eb-b378-0242ac130003"
OTA_CHARACTERISTIC_UUID = "62ec0272-3ec5-11eb-b378-0242ac130005"
KNOWN_SAFE_CHUNK_CAP = 509


class OtaProtocolError(RuntimeError):
    """Raised when the Meshtastic failsafe OTA protocol cannot proceed safely."""


class BleClient(Protocol):
    @property
    def is_connected(self) -> bool: ...

    @property
    def services(self) -> object: ...

    async def start_notify(self, characteristic, callback) -> None: ...

    async def write_gatt_char(
        self, characteristic, data: bytes, *, response: bool
    ) -> None: ...

    async def stop_notify(self, characteristic) -> None: ...


@dataclass(frozen=True)
class UploadResult:
    bytes_sent: int
    chunk_size: int
    reboot_observed: bool


async def _wait_until_disconnected(client: BleClient) -> None:
    while client.is_connected:
        await asyncio.sleep(0.05)


async def upload_firmware(
    client: BleClient,
    image: bytes,
    *,
    progress: Callable[[int, int], None] | None = None,
    before_initialize: Callable[[], None] | None = None,
    disconnected: asyncio.Event | None = None,
    first_ack_timeout_seconds: float = 30.0,
    ack_timeout_seconds: float = 10.0,
    write_timeout_seconds: float = 10.0,
    final_disconnect_timeout_seconds: float = 15.0,
    size_settle_seconds: float = 0.1,
    write_limit_wait_seconds: float = 5.0,
) -> UploadResult:
    """Upload one validated application image using Meshtastic 2.6.11 BLE OTA."""
    if not client.is_connected:
        raise OtaProtocolError("BLE client is not connected")
    if not image:
        raise OtaProtocolError("Firmware image is empty")

    get_characteristic = getattr(client.services, "get_characteristic", None)
    if get_characteristic is None:
        raise OtaProtocolError("BLE service discovery is unavailable")
    tx_characteristic = get_characteristic(OTA_TX_CHARACTERISTIC_UUID)
    ota_characteristic = get_characteristic(OTA_CHARACTERISTIC_UUID)
    if tx_characteristic is None or ota_characteristic is None:
        raise OtaProtocolError("Required Meshtastic OTA characteristics are missing")

    tx_properties = set(getattr(tx_characteristic, "properties", ["notify"]))
    ota_properties = set(
        getattr(ota_characteristic, "properties", ["write-without-response"])
    )
    if "notify" not in tx_properties:
        raise OtaProtocolError("OTA acknowledgement characteristic does not notify")
    if "write-without-response" not in ota_properties:
        raise OtaProtocolError("OTA data characteristic is not write-without-response")

    acknowledgements: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
    protocol_error = asyncio.Event()
    protocol_errors: list[str] = []
    disconnected_event = disconnected or asyncio.Event()

    def on_ack(_sender, data: bytearray) -> None:
        payload = bytes(data)
        if payload != b"\x00":
            protocol_errors.append(
                f"Unexpected OTA notification payload: {payload.hex() or 'empty'}"
            )
            protocol_error.set()
            return
        try:
            acknowledgements.put_nowait(payload)
        except asyncio.QueueFull:
            protocol_errors.append("Duplicate or out-of-sequence OTA acknowledgement")
            protocol_error.set()

    async def wait_for_ack(timeout: float) -> None:
        ack_task = asyncio.create_task(acknowledgements.get())
        disconnect_task = asyncio.create_task(disconnected_event.wait())
        error_task = asyncio.create_task(protocol_error.wait())
        done, pending = await asyncio.wait(
            {ack_task, disconnect_task, error_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not done:
            raise OtaProtocolError(
                "OTA acknowledgement timed out; the chunk must not be retried"
            )
        if error_task in done and protocol_error.is_set():
            raise OtaProtocolError(protocol_errors[-1])
        if ack_task in done:
            return
        raise OtaProtocolError(
            "Device disconnected before acknowledging the chunk; do not resume"
        )

    notify_started = False
    try:
        await client.start_notify(tx_characteristic, on_ack)
        notify_started = True

        deadline = asyncio.get_running_loop().time() + write_limit_wait_seconds
        while (
            int(getattr(ota_characteristic, "max_write_without_response_size", 20))
            == 20
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.25)
        reported_limit = int(
            getattr(ota_characteristic, "max_write_without_response_size", 20)
        )
        if reported_limit < 1:
            raise OtaProtocolError("BLE adapter reported an invalid write size")
        chunk_size = min(reported_limit, KNOWN_SAFE_CHUNK_CAP)

        size_record = f"OTA_SIZE:{len(image)}".encode("ascii")
        if len(size_record) > reported_limit:
            raise OtaProtocolError(
                "Negotiated BLE limit cannot carry the OTA size record"
            )
        if before_initialize:
            before_initialize()
        await asyncio.wait_for(
            client.write_gatt_char(
                ota_characteristic,
                size_record,
                response=False,
            ),
            timeout=write_timeout_seconds,
        )
        if size_settle_seconds > 0:
            await asyncio.sleep(size_settle_seconds)
        if protocol_error.is_set():
            raise OtaProtocolError(protocol_errors[-1])
        if not acknowledgements.empty():
            raise OtaProtocolError(
                "Unexpected acknowledgement after OTA_SIZE; the helper state is stale"
            )

        sent = 0
        while sent < len(image):
            if disconnected_event.is_set() or not client.is_connected:
                raise OtaProtocolError(
                    "Device disconnected during upload; do not reconnect or resume"
                )
            if protocol_error.is_set():
                raise OtaProtocolError(protocol_errors[-1])
            if not acknowledgements.empty():
                raise OtaProtocolError("Stale acknowledgement before a data write")

            chunk = image[sent : sent + chunk_size]
            if not chunk or sent + len(chunk) > len(image):
                raise OtaProtocolError("Invalid final OTA chunk boundary")
            await asyncio.wait_for(
                client.write_gatt_char(
                    ota_characteristic,
                    chunk,
                    response=False,
                ),
                timeout=write_timeout_seconds,
            )
            await wait_for_ack(
                first_ack_timeout_seconds if sent == 0 else ack_timeout_seconds
            )
            sent += len(chunk)
            if progress:
                progress(sent, len(image))

        disconnect_waiter = (
            disconnected_event.wait()
            if disconnected is not None
            else _wait_until_disconnected(client)
        )
        try:
            await asyncio.wait_for(
                disconnect_waiter, timeout=final_disconnect_timeout_seconds
            )
        except TimeoutError as exc:
            raise OtaProtocolError(
                "Final chunk was acknowledged but reboot/disconnect was not observed"
            ) from exc

        return UploadResult(
            bytes_sent=sent,
            chunk_size=chunk_size,
            reboot_observed=True,
        )
    finally:
        if notify_started and client.is_connected:
            try:
                await client.stop_notify(tx_characteristic)
            except Exception:
                pass
