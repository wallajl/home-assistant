from __future__ import annotations

from dataclasses import dataclass
import hashlib

ESP32_IMAGE_MAGIC = 0xE9
ESP32_S3_CHIP_ID = 9
ESP_APP_DESC_MAGIC = 0xABCD5432
APP_DESCRIPTOR_OFFSET = 32
PROJECT_NAME_OFFSET = APP_DESCRIPTOR_OFFSET + 48
PROJECT_NAME_LENGTH = 32
MAX_APP_SIZE = 0x330000
MIN_APP_SIZE = PROJECT_NAME_OFFSET + PROJECT_NAME_LENGTH
MESHCORE_MARKER = b"https://meshcore.io\x00"
ARDUINO_PROJECT_NAME = "arduino-lib-builder"


class FirmwareValidationError(ValueError):
    """Raised when a firmware image is not safe for this migration path."""


@dataclass(frozen=True)
class FirmwareInfo:
    size: int
    chip_id: int
    project_name: str
    sha256: str


def _read_c_string(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def validate_meshcore_firmware(image: bytes) -> FirmwareInfo:
    """Validate a non-merged MeshCore ESP32-S3 application image."""
    size = len(image)
    if size < MIN_APP_SIZE:
        raise FirmwareValidationError(
            "Firmware image is too small to be an ESP32 application"
        )
    if size > MAX_APP_SIZE:
        raise FirmwareValidationError(
            f"Firmware image exceeds the Heltec V3 application slot ({MAX_APP_SIZE} bytes)"
        )
    if image[0] != ESP32_IMAGE_MAGIC:
        raise FirmwareValidationError("Firmware has an invalid ESP32 image magic byte")

    segment_count = image[1]
    if not 1 <= segment_count <= 16:
        raise FirmwareValidationError("Firmware has an invalid ESP32 segment count")

    chip_id = int.from_bytes(image[12:14], "little")
    if chip_id != ESP32_S3_CHIP_ID:
        raise FirmwareValidationError(
            f"Firmware is not for ESP32-S3 (chip ID {chip_id}, expected {ESP32_S3_CHIP_ID})"
        )

    checksum = 0xEF
    cursor = 24
    first_segment_data_offset = None
    for segment_index in range(segment_count):
        if cursor + 8 > size:
            raise FirmwareValidationError("ESP image has a truncated segment header")
        segment_size = int.from_bytes(image[cursor + 4 : cursor + 8], "little")
        cursor += 8
        if segment_size <= 0 or cursor + segment_size > size:
            raise FirmwareValidationError(
                f"ESP image segment {segment_index} has an invalid size"
            )
        if first_segment_data_offset is None:
            first_segment_data_offset = cursor
        for value in image[cursor : cursor + segment_size]:
            checksum ^= value
        cursor += segment_size

    hash_offset = (cursor + 1 + 15) & ~15
    checksum_offset = hash_offset - 1
    if hash_offset + 32 != size:
        raise FirmwareValidationError(
            "ESP image length does not match its segment table and appended validation hash"
        )
    if any(image[cursor:checksum_offset]):
        raise FirmwareValidationError("ESP image has invalid checksum padding")
    if image[checksum_offset] != checksum:
        raise FirmwareValidationError("ESP image segment checksum is invalid")
    if image[23] != 1:
        raise FirmwareValidationError("ESP image has no appended validation hash")
    expected_validation_hash = image[hash_offset : hash_offset + 32]
    actual_validation_hash = hashlib.sha256(image[:hash_offset]).digest()
    if expected_validation_hash != actual_validation_hash:
        raise FirmwareValidationError("ESP image appended validation hash is invalid")

    descriptor_offset = first_segment_data_offset or APP_DESCRIPTOR_OFFSET
    descriptor_magic = int.from_bytes(
        image[descriptor_offset : descriptor_offset + 4], "little"
    )
    if descriptor_magic != ESP_APP_DESC_MAGIC:
        raise FirmwareValidationError(
            "Firmware has no ESP application descriptor; merged and bootloader images are rejected"
        )

    project_name = _read_c_string(
        image[descriptor_offset + 48 : descriptor_offset + 48 + PROJECT_NAME_LENGTH]
    )
    if (
        "meshcore" not in project_name.casefold()
        and project_name != ARDUINO_PROJECT_NAME
    ):
        raise FirmwareValidationError(
            f"Firmware project is not MeshCore or the expected Arduino build identity "
            f"(reported project: {project_name or 'unknown'})"
        )
    if MESHCORE_MARKER not in image:
        raise FirmwareValidationError(
            "Firmware does not contain the canonical MeshCore application marker"
        )

    return FirmwareInfo(
        size=size,
        chip_id=chip_id,
        project_name=project_name,
        sha256=hashlib.sha256(image).hexdigest(),
    )
