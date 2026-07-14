from __future__ import annotations

from pathlib import Path

import pytest

from app.controller import (
    CONFIRMATION_PHRASE,
    MigrationState,
    PreflightError,
    require_flash_preconditions,
)


def ready_state(tmp_path: Path) -> MigrationState:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    backup = tmp_path / "backup.yaml"
    backup.write_text("backup")
    return MigrationState(
        firmware_path=firmware,
        backup_path=backup,
        phase="armed",
        ble_devices=[{"address": "AA:BB:CC:DD:EE:FF"}],
    )


def test_preconditions_accept_validated_firmware_backup_and_exact_phrase(
    tmp_path: Path,
) -> None:
    state = ready_state(tmp_path)

    require_flash_preconditions(state, CONFIRMATION_PHRASE, "AA:BB:CC:DD:EE:FF")


@pytest.mark.parametrize(
    ("mutation", "phrase", "message"),
    [
        ("firmware", CONFIRMATION_PHRASE, "validated firmware"),
        ("backup", CONFIRMATION_PHRASE, "backup"),
        (None, "yes", "confirmation phrase"),
    ],
)
def test_preconditions_fail_closed(
    tmp_path: Path, mutation: str | None, phrase: str, message: str
) -> None:
    state = ready_state(tmp_path)
    if mutation == "firmware":
        state.firmware_path = None
    elif mutation == "backup":
        state.backup_path = None

    with pytest.raises(PreflightError, match=message):
        require_flash_preconditions(state, phrase, "AA:BB:CC:DD:EE:FF")


def test_preconditions_recheck_files_still_exist(tmp_path: Path) -> None:
    state = ready_state(tmp_path)
    assert state.firmware_path is not None
    state.firmware_path.unlink()

    with pytest.raises(PreflightError, match="no longer exists"):
        require_flash_preconditions(state, CONFIRMATION_PHRASE, "AA:BB:CC:DD:EE:FF")
