from __future__ import annotations

from pathlib import Path
from typing import cast
import asyncio
import hashlib

import pytest
from aiohttp import FormData

from app import server as server_module
from app.firmware import validate_meshcore_firmware
from app.server import AddonOptions, create_app
from tests.test_firmware import make_app_image

pytestmark = pytest.mark.asyncio
INGRESS_HEADERS = {"X-Remote-User-Id": "test-admin"}


async def csrf_headers(client) -> dict[str, str]:
    response = await client.get("/api/status", headers=INGRESS_HEADERS)
    token = (await response.json())["csrf_token"]
    return {**INGRESS_HEADERS, "X-CSRF-Token": token}


@pytest.fixture
def options(tmp_path: Path) -> AddonOptions:
    return AddonOptions(
        meshtastic_host="192.168.0.181",
        meshcore_host="192.168.0.181",
        meshcore_port=5000,
        expected_firmware_sha256=hashlib.sha256(
            make_app_image(project_name="MeshCore")
        ).hexdigest(),
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
    )


async def test_health_and_initial_status(aiohttp_client, options: AddonOptions) -> None:
    client = await aiohttp_client(create_app(options))

    health = await client.get("/health")
    status = await client.get("/api/status", headers=INGRESS_HEADERS)

    assert health.status == 200
    assert await health.json() == {"status": "ok"}
    payload = await status.json()
    assert payload["phase"] == "idle"
    assert payload["firmware"] is None
    assert payload["backup_ready"] is False


async def test_upload_validates_and_persists_application(
    aiohttp_client, options: AddonOptions
) -> None:
    client = await aiohttp_client(create_app(options))
    headers = await csrf_headers(client)
    form = FormData()
    form.add_field(
        "firmware",
        make_app_image(project_name="MeshCore"),
        filename="firmware-heltec-v3.bin",
        content_type="application/octet-stream",
    )

    response = await client.post("/api/firmware", data=form, headers=headers)

    assert response.status == 200
    payload = await response.json()
    assert payload["firmware"]["project_name"] == "MeshCore"
    firmware_path = options.data_dir / "firmware.bin"
    assert firmware_path.exists()
    assert firmware_path.stat().st_mode & 0o777 == 0o600


async def test_upload_rejects_non_meshcore_image(
    aiohttp_client, options: AddonOptions
) -> None:
    client = await aiohttp_client(create_app(options))
    headers = await csrf_headers(client)
    form = FormData()
    form.add_field(
        "firmware",
        make_app_image(project_name="Meshtastic"),
        filename="wrong.bin",
        content_type="application/octet-stream",
    )

    response = await client.post("/api/firmware", data=form, headers=headers)

    assert response.status == 400
    assert "MeshCore" in (await response.json())["error"]
    assert not (options.data_dir / "firmware.bin").exists()


async def test_private_backup_download_requires_authenticated_post(
    aiohttp_client, options: AddonOptions
) -> None:
    app = create_app(options)
    backup = options.backup_dir / "meshtastic-backup-test.yaml"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"private-backup-data")
    app["state"].backup_path = backup
    client = await aiohttp_client(app)
    headers = await csrf_headers(client)

    response = await client.post(
        "/api/backup/download",
        data="{}",
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status == 200
    assert await response.read() == b"private-backup-data"
    assert response.headers["Cache-Control"] == "no-store"
    assert "attachment" in response.headers["Content-Disposition"]


async def test_prepare_rejects_duplicate_request_before_task_takes_lock(
    aiohttp_client, options: AddonOptions, monkeypatch
) -> None:
    image = make_app_image()
    firmware = options.data_dir / "firmware.bin"
    backup = options.backup_dir / "backup.yaml"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(image)
    backup.parent.mkdir(parents=True)
    backup.write_text("valid private backup\n")

    app = create_app(options, arming_code="TESTCODE")
    state = app["state"]
    state.firmware_path = firmware
    state.firmware_info = validate_meshcore_firmware(image)
    state.backup_path = backup
    state.phase = "firmware_ready"
    gate = asyncio.Event()

    async def blocked_prepare(_app) -> None:
        await gate.wait()

    monkeypatch.setattr(server_module, "_perform_prepare", blocked_prepare)
    client = await aiohttp_client(app)
    headers = await csrf_headers(client)

    first = await client.post("/api/prepare", json={}, headers=headers)
    second = await client.post("/api/prepare", json={}, headers=headers)
    gate.set()
    await asyncio.sleep(0)

    assert first.status == 202
    assert second.status == 409


async def test_atomic_private_write_ignores_stale_permissive_temporary_file(
    tmp_path,
) -> None:
    destination = tmp_path / "private.bin"
    stale = destination.with_name("private.bin.tmp")
    stale.write_bytes(b"stale-do-not-reuse")
    stale.chmod(0o644)

    server_module._atomic_private_write(destination, b"secret")

    assert destination.read_bytes() == b"secret"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert stale.read_bytes() == b"stale-do-not-reuse"
    assert stale.stat().st_mode & 0o777 == 0o644


async def test_atomic_private_write_exclusively_creates_unique_temporary_file(
    tmp_path, monkeypatch
) -> None:
    destination = tmp_path / "private.bin"
    temporary = destination.with_name("private.bin.tmp.FIXED")
    temporary.write_bytes(b"pre-existing-permissive-file")
    temporary.chmod(0o644)
    monkeypatch.setattr(server_module.secrets, "token_hex", lambda _size: "FIXED")

    with pytest.raises(FileExistsError):
        server_module._atomic_private_write(destination, b"secret")

    assert temporary.read_bytes() == b"pre-existing-permissive-file"
    assert temporary.stat().st_mode & 0o777 == 0o644
    assert not destination.exists()


async def test_flash_reserves_operation_before_awaiting_request_body(
    options: AddonOptions,
) -> None:
    app = create_app(options, arming_code="TESTCODE")
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    class SlowRequest:
        def __init__(self) -> None:
            self.app = app

        async def json(self):
            body_started.set()
            await release_body.wait()
            return {}

    request = cast(server_module.web.Request, SlowRequest())
    task = asyncio.create_task(server_module._flash(request))
    await body_started.wait()

    assert app["operation"].pending is True
    with pytest.raises(server_module.web.HTTPConflict):
        server_module._reserve_operation(app)

    release_body.set()
    with pytest.raises(server_module.PreflightError):
        await task
    assert app["operation"].pending is False


@pytest.mark.parametrize(
    "ingress_path",
    ["//attacker.example/path", "/bad\\path", "/bad?query", "/bad#fragment"],
)
async def test_index_rejects_unsafe_ingress_base(
    aiohttp_client, options: AddonOptions, ingress_path: str
) -> None:
    client = await aiohttp_client(create_app(options))
    response = await client.get(
        "/", headers={**INGRESS_HEADERS, "X-Ingress-Path": ingress_path}
    )

    assert response.status == 400
