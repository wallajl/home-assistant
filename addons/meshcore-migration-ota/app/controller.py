from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIRMATION_PHRASE = "FLASH MESHCORE TO HELTEC V3"


class PreflightError(RuntimeError):
    """Raised when destructive migration prerequisites are incomplete."""


@dataclass
class MigrationState:
    firmware_path: Path | None = None
    firmware_info: Any | None = None
    backup_path: Path | None = None
    phase: str = "idle"
    message: str = "Waiting for firmware and backup"
    bytes_sent: int = 0
    total_bytes: int = 0
    meshcore_reachable: bool = False
    ble_devices: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    history: list[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        self.message = message
        self.history.append(message)
        if len(self.history) > 50:
            del self.history[:-50]

    def public_dict(self) -> dict[str, Any]:
        firmware = self.firmware_info
        return {
            "phase": self.phase,
            "message": self.message,
            "bytes_sent": self.bytes_sent,
            "total_bytes": self.total_bytes,
            "meshcore_reachable": self.meshcore_reachable,
            "ble_devices": self.ble_devices,
            "error": self.error,
            "firmware": (
                {
                    "size": firmware.size,
                    "chip_id": firmware.chip_id,
                    "project_name": firmware.project_name,
                    "sha256": firmware.sha256,
                }
                if firmware
                else None
            ),
            "backup_ready": bool(self.backup_path and self.backup_path.exists()),
            "history": self.history[-20:],
            "confirmation_phrase": CONFIRMATION_PHRASE,
        }


def require_staged_preconditions(state: MigrationState) -> None:
    if state.firmware_path is None:
        raise PreflightError("No validated firmware has been uploaded")
    if not state.firmware_path.exists():
        raise PreflightError("The validated firmware file no longer exists")
    if state.backup_path is None or not state.backup_path.exists():
        raise PreflightError("A successful Meshtastic backup is required")


def require_flash_preconditions(
    state: MigrationState, confirmation: str, ble_address: str
) -> None:
    require_staged_preconditions(state)
    if state.phase != "armed" or not state.ble_devices:
        raise PreflightError("Prepare and scan for the failsafe BLE OTA device first")
    known_addresses = {
        str(device.get("address", "")).casefold() for device in state.ble_devices
    }
    if ble_address.casefold() not in known_addresses:
        raise PreflightError("The exact scanned BLE address is required")
    if confirmation != CONFIRMATION_PHRASE:
        raise PreflightError("The exact confirmation phrase is required")
