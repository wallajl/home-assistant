from __future__ import annotations

import pytest

from app.ota import (
    KNOWN_SAFE_CHUNK_CAP,
    OTA_CHARACTERISTIC_UUID,
    OTA_TX_CHARACTERISTIC_UUID,
    OtaProtocolError,
    upload_firmware,
)


class FakeCharacteristic:
    def __init__(
        self, uuid: str, max_write_without_response_size: int, properties: list[str]
    ) -> None:
        self.uuid = uuid
        self.max_write_without_response_size = max_write_without_response_size
        self.properties = properties


class FakeServices:
    def __init__(self, max_write_without_response_size: int) -> None:
        self.ota = FakeCharacteristic(
            OTA_CHARACTERISTIC_UUID,
            max_write_without_response_size,
            ["write-without-response"],
        )
        self.tx = FakeCharacteristic(
            OTA_TX_CHARACTERISTIC_UUID,
            max_write_without_response_size,
            ["notify"],
        )

    def get_characteristic(self, uuid: str):
        if uuid == OTA_CHARACTERISTIC_UUID:
            return self.ota
        if uuid == OTA_TX_CHARACTERISTIC_UUID:
            return self.tx
        return None


class FakeClient:
    def __init__(
        self,
        *,
        max_write: int = 20,
        acknowledge: bool = True,
        ack_payload: bytes = b"\x00",
        duplicate_ack: bool = False,
        auto_disconnect: bool = True,
    ) -> None:
        self.services = FakeServices(max_write)
        self.is_connected = True
        self.acknowledge = acknowledge
        self.ack_payload = ack_payload
        self.duplicate_ack = duplicate_ack
        self.auto_disconnect = auto_disconnect
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_callback = None
        self.stopped_notify = False
        self.expected_size = 0
        self.received_size = 0

    async def start_notify(self, characteristic, callback) -> None:
        assert characteristic is self.services.tx
        self.notify_callback = callback

    async def write_gatt_char(
        self, characteristic, data: bytes, *, response: bool
    ) -> None:
        self.writes.append((characteristic.uuid, bytes(data), response))
        if data.startswith(b"OTA_SIZE:"):
            self.expected_size = int(data.removeprefix(b"OTA_SIZE:"))
            return
        self.received_size += len(data)
        if self.acknowledge and self.notify_callback:
            self.notify_callback(1, bytearray(self.ack_payload))
            if self.duplicate_ack:
                self.notify_callback(1, bytearray(self.ack_payload))
        if self.auto_disconnect and self.received_size == self.expected_size:
            self.is_connected = False

    async def stop_notify(self, characteristic) -> None:
        assert characteristic is self.services.tx
        self.stopped_notify = True


@pytest.mark.asyncio
async def test_upload_sends_size_then_exact_acknowledged_chunks() -> None:
    client = FakeClient(max_write=20)
    progress: list[tuple[int, int]] = []
    image = b"x" * 45

    result = await upload_firmware(
        client,
        image,
        progress=lambda sent, total: progress.append((sent, total)),
        size_settle_seconds=0,
        write_limit_wait_seconds=0,
    )

    assert [write[1] for write in client.writes] == [
        b"OTA_SIZE:45",
        b"x" * 20,
        b"x" * 20,
        b"x" * 5,
    ]
    assert all(write[0] == OTA_CHARACTERISTIC_UUID for write in client.writes)
    assert all(write[2] is False for write in client.writes)
    assert progress == [(20, 45), (40, 45), (45, 45)]
    assert result.bytes_sent == 45
    assert result.chunk_size == 20
    assert result.reboot_observed is True
    assert client.stopped_notify is False


@pytest.mark.asyncio
async def test_upload_caps_chunks_at_known_safe_limit() -> None:
    client = FakeClient(max_write=514)
    image = b"x" * 600

    result = await upload_firmware(
        client,
        image,
        size_settle_seconds=0,
        write_limit_wait_seconds=0,
    )

    assert result.chunk_size == KNOWN_SAFE_CHUNK_CAP
    assert [len(write[1]) for write in client.writes[1:]] == [509, 91]


@pytest.mark.asyncio
async def test_upload_fails_closed_when_ack_is_missing_without_retry() -> None:
    client = FakeClient(acknowledge=False)

    with pytest.raises(OtaProtocolError, match="must not be retried"):
        await upload_firmware(
            client,
            b"firmware",
            first_ack_timeout_seconds=0.01,
            ack_timeout_seconds=0.01,
            size_settle_seconds=0,
            write_limit_wait_seconds=0,
        )

    assert [write[1] for write in client.writes] == [b"OTA_SIZE:8", b"firmware"]


@pytest.mark.asyncio
async def test_upload_rejects_invalid_ack_payload() -> None:
    client = FakeClient(ack_payload=b"\x01")

    with pytest.raises(OtaProtocolError, match="Unexpected OTA notification"):
        await upload_firmware(
            client,
            b"firmware",
            size_settle_seconds=0,
            write_limit_wait_seconds=0,
        )


@pytest.mark.asyncio
async def test_upload_rejects_duplicate_ack() -> None:
    client = FakeClient(duplicate_ack=True)

    with pytest.raises(OtaProtocolError, match="Duplicate|out-of-sequence"):
        await upload_firmware(
            client,
            b"firmware",
            size_settle_seconds=0,
            write_limit_wait_seconds=0,
        )


@pytest.mark.asyncio
async def test_final_ack_without_disconnect_is_not_success() -> None:
    client = FakeClient(auto_disconnect=False)

    with pytest.raises(OtaProtocolError, match="reboot/disconnect was not observed"):
        await upload_firmware(
            client,
            b"firmware",
            size_settle_seconds=0,
            write_limit_wait_seconds=0,
            final_disconnect_timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_upload_rejects_disconnected_client() -> None:
    client = FakeClient()
    client.is_connected = False

    with pytest.raises(OtaProtocolError, match="not connected"):
        await upload_firmware(client, b"firmware", size_settle_seconds=0)
