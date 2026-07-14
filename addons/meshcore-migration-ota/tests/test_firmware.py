from __future__ import annotations

import hashlib

import pytest

from app.firmware import (
    ESP32_IMAGE_MAGIC,
    ESP32_S3_CHIP_ID,
    MAX_APP_SIZE,
    FirmwareValidationError,
    validate_meshcore_firmware,
)


def make_app_image(
    *,
    chip_id: int = ESP32_S3_CHIP_ID,
    project_name: str = "MeshCore",
    app_descriptor: bool = True,
    meshcore_marker: bool = True,
) -> bytes:
    header = bytearray(24)
    header[0] = ESP32_IMAGE_MAGIC
    header[1] = 1
    header[12:14] = chip_id.to_bytes(2, "little")
    header[23] = 1

    payload = bytearray(256)
    if app_descriptor:
        payload[0:4] = (0xABCD5432).to_bytes(4, "little")
        encoded = project_name.encode("ascii")[:31]
        payload[48 : 48 + len(encoded)] = encoded
    if meshcore_marker:
        marker = b"https://meshcore.io\x00"
        payload[96 : 96 + len(marker)] = marker

    segment_header = (0x3C000020).to_bytes(4, "little") + len(payload).to_bytes(
        4, "little"
    )
    image = header + segment_header + payload
    checksum = 0xEF
    for value in payload:
        checksum ^= value
    while len(image) % 16 != 15:
        image.append(0)
    image.append(checksum)
    image.extend(hashlib.sha256(image).digest())
    return bytes(image)


def test_accepts_non_merged_meshcore_esp32_s3_application() -> None:
    image = make_app_image(project_name="MeshCore")

    result = validate_meshcore_firmware(image)

    assert result.size == len(image)
    assert result.chip_id == ESP32_S3_CHIP_ID
    assert result.project_name == "MeshCore"
    assert result.sha256 == hashlib.sha256(image).hexdigest()


def test_accepts_upstream_arduino_meshcore_identity_with_canonical_marker() -> None:
    image = make_app_image(project_name="arduino-lib-builder")

    result = validate_meshcore_firmware(image)

    assert result.project_name == "arduino-lib-builder"


def test_rejects_arduino_identity_without_meshcore_marker() -> None:
    image = make_app_image(project_name="arduino-lib-builder", meshcore_marker=False)

    with pytest.raises(FirmwareValidationError, match="canonical MeshCore"):
        validate_meshcore_firmware(image)


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (b"", "too small"),
        (b"not-an-esp-image" * 10, "magic"),
        (make_app_image(chip_id=0), "ESP32-S3"),
        (make_app_image(app_descriptor=False), "application descriptor"),
        (make_app_image(project_name="Meshtastic"), "MeshCore"),
        (make_app_image() + b"\x00" * MAX_APP_SIZE, "slot"),
    ],
)
def test_rejects_unsafe_or_wrong_firmware(image: bytes, message: str) -> None:
    with pytest.raises(FirmwareValidationError, match=message):
        validate_meshcore_firmware(image)


def test_rejects_invalid_segment_checksum() -> None:
    image = bytearray(make_app_image())
    image[-33] ^= 0x01

    with pytest.raises(FirmwareValidationError, match="checksum"):
        validate_meshcore_firmware(bytes(image))


def test_rejects_invalid_appended_validation_hash() -> None:
    image = bytearray(make_app_image())
    image[-1] ^= 0x01

    with pytest.raises(FirmwareValidationError, match="validation hash"):
        validate_meshcore_firmware(bytes(image))


def test_rejects_segment_table_that_does_not_match_file() -> None:
    image = bytearray(make_app_image())
    image[28:32] = (100000).to_bytes(4, "little")

    with pytest.raises(FirmwareValidationError, match="segment"):
        validate_meshcore_firmware(bytes(image))
