from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import time
from typing import Any, Callable

from aiohttp import web

from .controller import (
    MigrationState,
    PreflightError,
    require_flash_preconditions,
    require_staged_preconditions,
)
from .firmware import MAX_APP_SIZE, FirmwareValidationError, validate_meshcore_firmware

WORKER_SOCKET = Path("/data/meshcore-ota-worker.sock")
RESULT_PATH_NAME = "migration-result.json"
USED_MARKER_NAME = "destructive-write-attempted"
MAX_BACKUP_SIZE = 10 * 1024 * 1024
MESHTASTIC_CLI = "/opt/venv/bin/meshtastic"
ALLOWED_PEERS = {"127.0.0.1", "::1", "172.30.32.2"}


@dataclass(frozen=True)
class AddonOptions:
    meshtastic_host: str
    meshcore_host: str
    meshcore_port: int
    expected_firmware_sha256: str = ""
    data_dir: Path = Path("/data")
    backup_dir: Path = Path("/data/backups")
    worker_socket: Path = WORKER_SOCKET

    @classmethod
    def load(cls, path: Path = Path("/data/options.json")) -> "AddonOptions":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            meshtastic_host=str(raw.get("meshtastic_host", "172.30.32.1")),
            meshcore_host=str(raw.get("meshcore_host", "192.168.0.181")),
            meshcore_port=int(raw.get("meshcore_port", 5000)),
            expected_firmware_sha256=str(raw.get("expected_firmware_sha256", ""))
            .strip()
            .casefold(),
        )


@dataclass
class OperationState:
    pending: bool = False


@dataclass
class SecurityState:
    csrf_token: str
    arming_code: str
    arming_expires_at: float
    owner_user_id: str | None = None
    disarmed: bool = False


async def _run_command(*args: str, timeout: float = 60) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Command timed out: {args[0]}") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        summary = detail[-1][:300] if detail else f"exit code {process.returncode}"
        raise RuntimeError(f"{args[0]} failed: {summary}")
    return stdout, stderr


async def _port_open(host: str, port: int, timeout: float = 3) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (TimeoutError, OSError, socket.gaierror):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _worker_request(
    app: web.Application,
    payload: dict[str, str],
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    timeout: float = 240,
) -> dict[str, Any]:
    options: AddonOptions = app["options"]
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(options.worker_socket), timeout=5
        )
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(
            "The restricted Bluetooth worker is unavailable; check the add-on log and AppArmor status"
        ) from exc
    try:
        writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                raise RuntimeError("The Bluetooth worker closed without a result")
            event = json.loads(line)
            if on_event:
                on_event(event)
            if event.get("event") == "error":
                raise RuntimeError(str(event.get("error", "Bluetooth worker failed")))
            if event.get("event") in {"scan", "complete"}:
                return event
    finally:
        writer.close()
        await writer.wait_closed()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_private_regular_file(path: Path, maximum_size: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"Private artifact is not a regular file: {path.name}")
        if metadata.st_size < 32 or metadata.st_size > maximum_size:
            raise OSError(f"Private artifact has an invalid size: {path.name}")
        os.fchmod(descriptor, 0o600)
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError(f"Private artifact was truncated: {path.name}")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _restore_staged_artifacts(options: AddonOptions, state: MigrationState) -> None:
    firmware_path = options.data_dir / "firmware.bin"
    try:
        firmware_data = _read_private_regular_file(firmware_path, MAX_APP_SIZE)
        firmware_info = validate_meshcore_firmware(firmware_data)
        if len(options.expected_firmware_sha256) != 64 or not hmac.compare_digest(
            firmware_info.sha256, options.expected_firmware_sha256
        ):
            raise FirmwareValidationError(
                "Persisted firmware does not match the pinned SHA-256"
            )
        state.firmware_path = firmware_path
        state.firmware_info = firmware_info
    except (OSError, FirmwareValidationError):
        pass

    if options.backup_dir.is_dir():
        for candidate in sorted(
            options.backup_dir.glob("meshtastic-backup-*.yaml"), reverse=True
        ):
            try:
                _read_private_regular_file(candidate, MAX_BACKUP_SIZE)
            except OSError:
                continue
            state.backup_path = candidate
            break

    if state.firmware_path and state.backup_path:
        state.phase = "firmware_ready"
        state.record(
            "Restored validated firmware and private backup from add-on storage"
        )
    elif state.backup_path:
        state.phase = "backup_ready"
        state.record("Restored private Meshtastic backup from add-on storage")


def _atomic_private_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp.{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _save_result(app: web.Application) -> None:
    options: AddonOptions = app["options"]
    state: MigrationState = app["state"]
    firmware = state.firmware_info
    result = {
        "phase": state.phase,
        "bytes_sent": state.bytes_sent,
        "total_bytes": state.total_bytes,
        "firmware_sha256": firmware.sha256 if firmware else None,
        "error": state.error,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    options.data_dir.mkdir(parents=True, exist_ok=True)
    destination = options.data_dir / RESULT_PATH_NAME
    _atomic_private_write(
        destination, (json.dumps(result, indent=2) + "\n").encode("utf-8")
    )


def _reserve_operation(app: web.Application) -> None:
    operation: OperationState = app["operation"]
    if operation.pending or app["migration_lock"].locked() or app["security"].disarmed:
        raise web.HTTPConflict(text="Migration is running or permanently disarmed")
    # No await occurs before this assignment, closing the request/task race.
    operation.pending = True


def _release_operation(app: web.Application) -> None:
    operation: OperationState = app["operation"]
    operation.pending = False


async def _scan_devices(app: web.Application) -> None:
    state: MigrationState = app["state"]
    state.phase = "waiting_for_ble"
    state.record("Scanning for the Meshtastic failsafe OTA service")
    response = await _worker_request(app, {"action": "scan"}, timeout=40)
    devices = response.get("devices", [])
    if not isinstance(devices, list) or not devices:
        state.ble_devices = []
        state.error = "No Meshtastic failsafe OTA service was found. Move the Bluetooth adapter closer and use Rescan; do not power-cycle the node."
        state.record(state.error)
        return
    state.ble_devices = devices
    state.phase = "armed"
    state.error = None
    state.record(
        f"Found {len(devices)} OTA device(s); confirm one exact BLE address before flashing"
    )


async def _perform_prepare(app: web.Application) -> None:
    state: MigrationState = app["state"]
    options: AddonOptions = app["options"]
    lock: asyncio.Lock = app["migration_lock"]
    async with lock:
        try:
            require_staged_preconditions(state)
            state.error = None
            state.ble_devices = []
            state.phase = "preflight"
            state.record("Checking the Meshtastic TCP connection")
            if not await _port_open(options.meshtastic_host, 4403):
                raise PreflightError("Meshtastic TCP port 4403 is not reachable")
            if await _port_open(options.meshcore_host, options.meshcore_port):
                raise PreflightError(
                    "MeshCore TCP port is already open before migration, so post-flash verification would be ambiguous"
                )

            state.phase = "rebooting_to_ota"
            state.record("Requesting Meshtastic failsafe Bluetooth OTA mode")
            await _run_command(
                MESHTASTIC_CLI,
                "--host",
                options.meshtastic_host,
                "--no-time",
                "--no-nodes",
                "--timeout",
                "20",
                "--wait-to-disconnect",
                "1",
                "--reboot-ota",
                timeout=35,
            )
            await _scan_devices(app)
        except Exception as exc:
            state.error = str(exc)
            state.phase = "failed"
            state.record(str(exc))


async def _perform_rescan(app: web.Application) -> None:
    state: MigrationState = app["state"]
    lock: asyncio.Lock = app["migration_lock"]
    async with lock:
        try:
            state.error = None
            await _scan_devices(app)
        except Exception as exc:
            state.error = str(exc)
            state.phase = "waiting_for_ble"
            state.record(str(exc))


async def _perform_flash(app: web.Application, ble_address: str) -> None:
    state: MigrationState = app["state"]
    options: AddonOptions = app["options"]
    lock: asyncio.Lock = app["migration_lock"]
    async with lock:
        try:
            assert state.firmware_info is not None
            state.error = None
            state.meshcore_reachable = False
            state.bytes_sent = 0
            state.total_bytes = state.firmware_info.size
            state.phase = "connected"
            state.record(
                f"Connecting to confirmed BLE address {ble_address}; no automatic retry is permitted"
            )

            def worker_event(event: dict[str, Any]) -> None:
                if event.get("event") != "progress":
                    return
                state.phase = "flashing"
                state.bytes_sent = int(event.get("sent", 0))
                state.total_bytes = int(event.get("total", state.total_bytes))
                percent = int((state.bytes_sent / state.total_bytes) * 100)
                state.record(f"Flashing MeshCore: {percent}%")

            result = await _worker_request(
                app,
                {
                    "action": "flash",
                    "address": ble_address,
                    "sha256": state.firmware_info.sha256,
                },
                on_event=worker_event,
                timeout=900,
            )
            state.bytes_sent = int(result["bytes_sent"])
            state.record(
                f"Transfer accepted in {int(result['chunk_size'])}-byte BLE chunks; boot is not yet verified"
            )

            state.phase = "verifying"
            state.record(f"Waiting for MeshCore TCP port {options.meshcore_port}")
            for _attempt in range(90):
                if await _port_open(
                    options.meshcore_host, options.meshcore_port, timeout=2
                ):
                    state.meshcore_reachable = True
                    state.phase = "complete"
                    state.record("MeshCore is online and positively verified over TCP")
                    return
                await asyncio.sleep(2)

            state.phase = "verification_failed"
            raise RuntimeError(
                "Transfer completed but MeshCore TCP did not appear. Check the Omada lease and use USB recovery if the node did not join Wi-Fi."
            )
        except Exception as exc:
            state.error = str(exc)
            if state.phase != "verification_failed":
                state.phase = "failed_after_attempt"
            state.record(str(exc))
        finally:
            _save_result(app)


@web.middleware
async def _error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except (FirmwareValidationError, PreflightError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException:
        raise
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


@web.middleware
async def _security_middleware(request: web.Request, handler):
    if request.remote not in ALLOWED_PEERS:
        raise web.HTTPForbidden(text="Ingress peer is not allowed")
    if request.path == "/health":
        return await handler(request)

    user_id = request.headers.get("X-Remote-User-Id", "").strip()
    if not user_id:
        raise web.HTTPForbidden(
            text="Authenticated Home Assistant ingress user required"
        )
    security: SecurityState = request.app["security"]
    owner_user_id = security.owner_user_id
    if owner_user_id is None:
        security.owner_user_id = user_id
    elif not hmac.compare_digest(owner_user_id, user_id):
        raise web.HTTPForbidden(
            text="This one-use session is already owned by another user"
        )

    if request.method == "POST":
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, security.csrf_token):
            raise web.HTTPForbidden(text="Invalid CSRF token")
        if request.path == "/api/firmware":
            if not request.content_type.startswith("multipart/"):
                raise web.HTTPUnsupportedMediaType(
                    text="Multipart firmware upload required"
                )
        elif request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON request required")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self'; frame-ancestors 'self'"
    )
    return response


async def _index(request: web.Request) -> web.Response:
    base = request.headers.get("X-Ingress-Path", "/")
    if (
        not base.startswith("/")
        or base.startswith("//")
        or "\\" in base
        or any(char in base for char in "<>\"'?#")
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in base)
    ):
        raise web.HTTPBadRequest(text="Invalid ingress path")
    if not base.endswith("/"):
        base += "/"
    page = INDEX_HTML.replace("__BASE_HREF__", html.escape(base, quote=True))
    return web.Response(text=page, content_type="text/html")


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _status(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    security: SecurityState = request.app["security"]
    payload = state.public_dict()
    payload["csrf_token"] = security.csrf_token
    payload["disarmed"] = security.disarmed
    return web.json_response(payload)


async def _upload_firmware_impl(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    options: AddonOptions = request.app["options"]

    reader = await request.multipart()
    field = await reader.next()
    if field is None or getattr(field, "name", None) != "firmware":
        raise ValueError("A firmware file is required")
    filename = (getattr(field, "filename", None) or "").casefold()
    if any(marker in filename for marker in ("merged", "factory", "bootloader")):
        raise FirmwareValidationError(
            "Merged, factory and bootloader filenames are rejected; upload an application-only image"
        )

    data = bytearray()
    while True:
        chunk = await field.read_chunk(size=64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_APP_SIZE:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_APP_SIZE, actual_size=len(data)
            )

    image = bytes(data)
    info = validate_meshcore_firmware(image)
    if len(options.expected_firmware_sha256) != 64:
        raise PreflightError(
            "Set expected_firmware_sha256 in the add-on configuration from the privately reviewed build before uploading"
        )
    if not hmac.compare_digest(info.sha256, options.expected_firmware_sha256):
        raise FirmwareValidationError(
            f"Firmware SHA-256 does not match the privately pinned artifact (uploaded {info.sha256})"
        )
    options.data_dir.mkdir(parents=True, exist_ok=True)
    destination = options.data_dir / "firmware.bin"
    _atomic_private_write(destination, image)

    state.firmware_path = destination
    state.firmware_info = info
    state.total_bytes = info.size
    state.ble_devices = []
    state.error = None
    state.phase = "firmware_ready"
    state.record(
        f"Validated {info.project_name} ESP32-S3 application ({info.size} bytes, SHA-256 {info.sha256[:12]}…)"
    )
    return web.json_response(_status_payload(request))


async def _upload_firmware(request: web.Request) -> web.Response:
    _reserve_operation(request.app)
    try:
        return await _upload_firmware_impl(request)
    finally:
        _release_operation(request.app)


def _status_payload(request: web.Request) -> dict[str, Any]:
    state: MigrationState = request.app["state"]
    security: SecurityState = request.app["security"]
    payload = state.public_dict()
    payload["csrf_token"] = security.csrf_token
    payload["disarmed"] = security.disarmed
    return payload


async def _backup_impl(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    options: AddonOptions = request.app["options"]

    state.phase = "backing_up"
    state.record("Exporting Meshtastic configuration and security keys")
    options.backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(options.backup_dir, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = options.backup_dir / f"meshtastic-backup-{stamp}.yaml"
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.close(descriptor)
    try:
        await _run_command(
            MESHTASTIC_CLI,
            "--host",
            options.meshtastic_host,
            "--no-time",
            "--no-nodes",
            "--timeout",
            "30",
            "--wait-to-disconnect",
            "1",
            "--export-config",
            str(path),
            timeout=50,
        )
        if not path.exists() or path.stat().st_size < 32:
            raise RuntimeError("Meshtastic returned an empty configuration backup")
        os.chmod(path, 0o600)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        _fsync_directory(options.backup_dir)
    except Exception:
        path.unlink(missing_ok=True)
        state.phase = "failed"
        state.error = "Meshtastic backup failed. Verify the configured TCP endpoint is reachable and currently serving the target radio, then retry."
        state.record(state.error)
        raise RuntimeError(state.error) from None
    state.backup_path = path
    state.error = None
    state.phase = "backup_ready"
    state.record(f"Backup retained in private add-on storage: {path.name}")
    return web.json_response(_status_payload(request))


async def _backup(request: web.Request) -> web.Response:
    _reserve_operation(request.app)
    try:
        return await _backup_impl(request)
    finally:
        _release_operation(request.app)


async def _download_backup(request: web.Request) -> web.StreamResponse:
    state: MigrationState = request.app["state"]
    path = state.backup_path
    if path is None or not path.is_file():
        raise web.HTTPConflict(text="Create a valid backup before downloading it")
    response = web.FileResponse(path)
    response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _spawn(app: web.Application, coroutine) -> None:
    tasks: set[asyncio.Task] = app["background_tasks"]
    task = asyncio.create_task(coroutine)
    tasks.add(task)

    def finished(completed: asyncio.Task) -> None:
        tasks.discard(completed)
        _release_operation(app)

    task.add_done_callback(finished)


async def _prepare(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    require_staged_preconditions(state)
    _reserve_operation(request.app)
    try:
        _spawn(request.app, _perform_prepare(request.app))
    except Exception:
        _release_operation(request.app)
        raise
    return web.json_response({"accepted": True}, status=202)


async def _rescan(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    require_staged_preconditions(state)
    if state.phase not in {"firmware_ready", "waiting_for_ble", "armed", "failed"}:
        raise PreflightError(
            "Stage firmware and backup before scanning failsafe OTA mode"
        )
    _reserve_operation(request.app)
    try:
        _spawn(request.app, _perform_rescan(request.app))
    except Exception:
        _release_operation(request.app)
        raise
    return web.json_response({"accepted": True}, status=202)


async def _flash(request: web.Request) -> web.Response:
    state: MigrationState = request.app["state"]
    _reserve_operation(request.app)
    try:
        payload = await request.json()
        confirmation = str(payload.get("confirmation", ""))
        ble_address = str(payload.get("ble_address", "")).strip()
        arming_code = str(payload.get("arming_code", "")).strip().upper()
        require_flash_preconditions(state, confirmation, ble_address)
        security: SecurityState = request.app["security"]
        if not hmac.compare_digest(arming_code, security.arming_code):
            raise PreflightError(
                "The one-use arming code from the add-on log is required"
            )
        if time.monotonic() > security.arming_expires_at:
            raise PreflightError(
                "The one-use arming code expired; restart the add-on to generate a fresh code"
            )

        security.disarmed = True
        security.arming_code = secrets.token_hex(16).upper()
        state.phase = "attempt_accepted"
        state.record("One-use flash attempt accepted and permanently disarmed")
        _save_result(request.app)
        _spawn(request.app, _perform_flash(request.app, ble_address))
    except BaseException:
        _release_operation(request.app)
        raise
    return web.json_response({"accepted": True}, status=202)


def create_app(
    options: AddonOptions, *, arming_code: str | None = None
) -> web.Application:
    app = web.Application(
        client_max_size=MAX_APP_SIZE + 1024 * 1024,
        middlewares=[_error_middleware, _security_middleware],
    )
    app["options"] = options
    app["state"] = MigrationState()
    app["migration_lock"] = asyncio.Lock()
    app["operation"] = OperationState()
    app["background_tasks"] = set()
    app["security"] = SecurityState(
        csrf_token=secrets.token_urlsafe(32),
        arming_code=arming_code or secrets.token_hex(4).upper(),
        arming_expires_at=time.monotonic() + 30 * 60,
        disarmed=(
            (options.data_dir / RESULT_PATH_NAME).exists()
            or (options.data_dir / USED_MARKER_NAME).exists()
        ),
    )
    if app["security"].disarmed:
        app["state"].phase = "previous_attempt_detected"
        app[
            "state"
        ].error = "A previous migration attempt is recorded. This instance is permanently disarmed; inspect the result and reinstall only after physical recovery assessment."
        app["state"].record(app["state"].error)
    else:
        _restore_staged_artifacts(options, app["state"])

    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)
    app.router.add_get("/api/status", _status)
    app.router.add_post("/api/firmware", _upload_firmware)
    app.router.add_post("/api/backup", _backup)
    app.router.add_post("/api/backup/download", _download_backup)
    app.router.add_post("/api/prepare", _prepare)
    app.router.add_post("/api/rescan", _rescan)
    app.router.add_post("/api/flash", _flash)
    return app


def main() -> None:
    options = AddonOptions.load()
    arming_code = secrets.token_hex(4).upper()
    print(f"ONE-USE MESHCORE OTA ARMING CODE: {arming_code}", flush=True)
    print(
        "The code expires after 30 minutes or when a flash attempt is accepted.",
        flush=True,
    )
    web.run_app(
        create_app(options, arming_code=arming_code),
        host="0.0.0.0",
        port=8099,
        access_log=None,
    )


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><base href="__BASE_HREF__"><title>MeshCore Migration OTA</title>
<style>:root{color-scheme:dark;--bg:#080c12;--card:#111824;--line:#26364a;--text:#e8f0fa;--muted:#91a4b9;--cyan:#49d8ff;--green:#4de2a8;--red:#ff647c;--amber:#ffc857}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#102439 0,transparent 34%),var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}.wrap{max-width:960px;margin:auto;padding:28px 18px 60px;overflow:hidden}h1{font-size:clamp(25px,4vw,40px);margin:0}.tag{color:var(--cyan);font:700 12px ui-monospace,monospace;letter-spacing:.13em;text-transform:uppercase}.warn{border-left:4px solid var(--amber);background:#2b2413;padding:14px 16px;margin:20px 0}.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr))}.card{min-width:0;background:color-mix(in srgb,var(--card) 94%,transparent);border:1px solid var(--line);border-radius:14px;padding:20px}.card h2{margin:0 0 10px;font-size:18px}.step{color:var(--cyan);font:700 12px ui-monospace,monospace}.muted{color:var(--muted)}button,input{font:inherit}button{width:100%;border:0;border-radius:9px;padding:12px 14px;background:var(--cyan);color:#001018;font-weight:800;cursor:pointer;margin-top:8px}button.secondary{background:#23364c;color:var(--text)}button.danger{background:var(--red);color:#220008}button:disabled{opacity:.45;cursor:not-allowed}input[type=file],input[type=text]{width:100%;min-width:0;padding:10px;border:1px solid var(--line);border-radius:8px;background:#09111b;color:var(--text);margin:8px 0}.status{margin-top:16px}.bar{height:10px;background:#07101a;border:1px solid var(--line);border-radius:10px;overflow:hidden}.bar span{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));width:0;transition:width .25s}.error{color:var(--red);white-space:pre-wrap}.ok{color:var(--green)}code{font-family:ui-monospace,monospace;color:#bfeeff;word-break:break-all}ol.log{max-height:210px;overflow:auto;color:var(--muted);padding-left:22px}.device{padding:10px;border:1px solid var(--line);border-radius:8px;margin:8px 0;overflow-wrap:anywhere}@media(max-width:380px){.wrap{padding-inline:10px}.card{padding:14px}}</style></head><body><main class="wrap">
<div class="tag">Experimental • one-use recovery tool</div><h1>Meshtastic → MeshCore</h1><p class="muted">Cableless Heltec V3 application migration using the Home Assistant machine's local Bluetooth adapter.</p>
<div class="warn"><strong>Unsupported destructive operation.</strong> A BLE disconnect after writing starts can leave the application slot incomplete. Keep USB recovery hardware available. Never power-cycle or retry after a partial transfer.</div>
<section class="grid">
<div class="card"><div class="step">STEP 1</div><h2>Private backup</h2><p class="muted">Keep the Meshtastic integration loaded while backing up through this installation's local TCP proxy. Download the backup and keep it private. Leave Meshtastic loaded through STEP 3 so Prepare can send reboot-to-OTA.</p><button id="backup" class="secondary">Create backup</button><button id="downloadBackup" class="secondary" disabled>Download private backup</button></div>
<div class="card"><div class="step">STEP 2</div><h2>Validated MeshCore app</h2><p class="muted">Choose the non-merged Heltec V3 application binary. Full/merged images are rejected.</p><label>MeshCore application binary<input id="firmware" type="file" accept=".bin,application/octet-stream"></label><button id="upload">Validate and stage</button></div>
<div class="card"><div class="step">STEP 3</div><h2>Prepare and identify</h2><p class="muted">Reboot to failsafe OTA, scan, and display exact BLE identities. This stage does not write firmware.</p><button id="prepare" class="secondary">Reboot to OTA and scan</button><button id="rescan" class="secondary">Rescan without reboot</button><div id="devices"></div></div>
<div class="card"><div class="step">STEP 4</div><h2>One destructive write</h2><p class="muted">Re-enter the exact scanned BLE address, phrase, and one-use code printed in the add-on log.</p><label>BLE address<input id="bleAddress" type="text" autocomplete="off"></label><label>Confirmation phrase <code id="phrase"></code><input id="confirmation" type="text" autocomplete="off" placeholder="Exact phrase"></label><label>One-use arming code<input id="armingCode" type="text" autocomplete="off" placeholder="Arming code from add-on log"></label><button id="flash" class="danger">Flash exact device once</button></div>
</section><section class="card status"><div class="step">LIVE STATUS</div><h2 id="phase">Loading…</h2><p id="message" class="muted"></p><div class="bar"><span id="progress"></span></div><p id="firmwareInfo" class="muted"></p><p id="error" class="error"></p><ol id="history" class="log"></ol></section></main>
<script>const $=id=>document.getElementById(id);let status={},csrf='';const url=p=>new URL(p,document.baseURI);async function api(path,options={}){options.headers=new Headers(options.headers||{});if(options.method==='POST')options.headers.set('X-CSRF-Token',csrf);const r=await fetch(url(path),options);let p={};try{p=await r.json()}catch{}if(!r.ok)throw new Error(p.error||`Request failed: ${r.status}`);return p}function render(s){status=s;csrf=s.csrf_token||csrf;$('phase').textContent=s.phase.replaceAll('_',' ');$('message').textContent=s.message||'';$('phrase').textContent=s.confirmation_phrase||'';$('error').textContent=s.error||'';const pct=s.total_bytes?Math.round(s.bytes_sent/s.total_bytes*100):0;$('progress').style.width=`${pct}%`;$('firmwareInfo').textContent=s.firmware?`${s.firmware.project_name} • ${s.firmware.size} bytes • SHA-256 ${s.firmware.sha256}`:'No validated firmware staged';const d=$('devices');d.replaceChildren(...(s.ble_devices||[]).map(x=>{const e=document.createElement('div');e.className='device';e.textContent=`${x.name} • ${x.address} • RSSI ${x.rssi} • MFG ${JSON.stringify(x.manufacturer_data||{})}`;return e}));const h=$('history');h.replaceChildren(...(s.history||[]).map(x=>{const li=document.createElement('li');li.textContent=x;return li}));const busy=['preflight','rebooting_to_ota','waiting_for_ble','connected','flashing','verifying','backing_up'].includes(s.phase);for(const id of ['backup','upload','prepare','rescan'])$(id).disabled=busy||s.disarmed;$('downloadBackup').disabled=!s.backup_ready;$('rescan').disabled=busy||s.disarmed||!['waiting_for_ble','armed'].includes(s.phase);$('prepare').disabled=busy||s.disarmed||!s.firmware||!s.backup_ready;$('flash').disabled=busy||s.disarmed||s.phase!=='armed'||!(s.ble_devices||[]).length}async function refresh(){try{render(await api('api/status'))}catch(e){$('error').textContent=e.message}}async function post(path,body={}){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}$('backup').onclick=async()=>{try{render(await post('api/backup'))}catch(e){$('error').textContent=e.message;refresh()}};$('downloadBackup').onclick=async()=>{try{const r=await fetch(url('api/backup/download'),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});if(!r.ok)throw new Error(`Download failed: ${r.status}`);const blob=await r.blob(),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='meshtastic-private-backup.yaml';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}catch(e){$('error').textContent=e.message}};$('upload').onclick=async()=>{const f=$('firmware').files[0];if(!f)return alert('Choose a non-merged MeshCore .bin file');const form=new FormData();form.append('firmware',f);try{render(await api('api/firmware',{method:'POST',body:form}))}catch(e){$('error').textContent=e.message;refresh()}};$('prepare').onclick=async()=>{if(!confirm('Reboot the Meshtastic node into failsafe BLE OTA mode?'))return;try{await post('api/prepare');refresh()}catch(e){$('error').textContent=e.message;refresh()}};$('rescan').onclick=async()=>{try{await post('api/rescan');refresh()}catch(e){$('error').textContent=e.message;refresh()}};$('flash').onclick=async()=>{if(!confirm('FINAL WARNING: this can require USB recovery. Flash once?'))return;try{await post('api/flash',{ble_address:$('bleAddress').value,confirmation:$('confirmation').value,arming_code:$('armingCode').value});refresh()}catch(e){$('error').textContent=e.message;refresh()}};refresh();setInterval(refresh,1000);</script></body></html>"""


if __name__ == "__main__":
    main()
