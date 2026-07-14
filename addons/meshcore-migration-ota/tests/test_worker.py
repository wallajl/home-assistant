from __future__ import annotations

from dataclasses import dataclass
import hashlib

import pytest

from app import worker
from app.ota import OTA_SERVICE_UUID
from tests.test_firmware import make_app_image


@dataclass
class FakeDevice:
    address: str
    name: str | None


@dataclass
class FakeAdvertisement:
    service_uuids: list[str]
    local_name: str | None
    rssi: int
    manufacturer_data: dict[int, bytes]


@pytest.mark.asyncio
async def test_scan_selects_hci0_and_serializes_identity(monkeypatch) -> None:
    captured: dict = {}
    device = FakeDevice("AA:BB:CC:DD:EE:FF", "Meshtastic_OTA")
    advertisement = FakeAdvertisement(
        [OTA_SERVICE_UUID], "Meshtastic_OTA", -52, {4660: b"\x01\x02"}
    )

    async def fake_discover(**kwargs):
        captured.update(kwargs)
        return {device.address: (device, advertisement)}

    monkeypatch.setattr(worker.BleakScanner, "discover", fake_discover)

    result = await worker._scan()

    assert captured["bluez"] == {"adapter": "hci0"}
    assert result == [
        {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Meshtastic_OTA",
            "rssi": -52,
            "service_uuid": OTA_SERVICE_UUID,
            "manufacturer_data": {"4660": "0102"},
        }
    ]


@pytest.mark.asyncio
async def test_flash_revalidates_staged_hash_before_bluetooth(
    monkeypatch, tmp_path
) -> None:
    image = make_app_image()
    firmware = tmp_path / "firmware.bin"
    marker = tmp_path / "used"
    firmware.write_bytes(image)
    monkeypatch.setattr(worker, "FIRMWARE_PATH", firmware)
    monkeypatch.setattr(worker, "USED_MARKER", marker)

    with pytest.raises(RuntimeError, match="changed after validation"):
        await worker._flash(None, "AA:BB:CC:DD:EE:FF", "0" * 64)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_flash_rejects_weak_signal_before_destructive_marker(
    monkeypatch, tmp_path
) -> None:
    image = make_app_image()
    firmware = tmp_path / "firmware.bin"
    marker = tmp_path / "used"
    firmware.write_bytes(image)
    monkeypatch.setattr(worker, "FIRMWARE_PATH", firmware)
    monkeypatch.setattr(worker, "USED_MARKER", marker)
    device = FakeDevice("AA:BB:CC:DD:EE:FF", "Meshtastic_OTA")
    advertisement = FakeAdvertisement([OTA_SERVICE_UUID], "Meshtastic_OTA", -95, {})

    async def fake_find_exact(_address):
        return device, advertisement

    monkeypatch.setattr(worker, "_find_exact", fake_find_exact)

    with pytest.raises(RuntimeError, match="signal is too weak"):
        await worker._flash(
            None,
            "AA:BB:CC:DD:EE:FF",
            hashlib.sha256(image).hexdigest(),
        )

    assert not marker.exists()


def test_destructive_marker_is_private_durable_and_one_use(
    monkeypatch, tmp_path
) -> None:
    marker = tmp_path / "destructive-write-attempted"
    fsync_calls: list[int] = []
    monkeypatch.setattr(worker, "USED_MARKER", marker)
    monkeypatch.setattr(worker.os, "fsync", fsync_calls.append)

    worker._write_durable_marker("a" * 64)

    assert marker.read_text(encoding="ascii") == "a" * 64 + "\n"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert len(fsync_calls) == 2
    with pytest.raises(FileExistsError):
        worker._write_durable_marker("b" * 64)
