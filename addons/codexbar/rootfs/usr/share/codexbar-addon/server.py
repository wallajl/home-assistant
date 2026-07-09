#!/usr/bin/env python3
"""Small Home Assistant Ingress UI/API for CodexBar setup.

The CodexBar CLI already serves read-only usage/cost JSON on 127.0.0.1:8080.
This wrapper adds an admin GUI and a tiny config API so users do not need to hand-edit
provider_config_json or files in /addon_configs.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path("/usr/share/codexbar-addon")
CONFIG_PATH = pathlib.Path(os.environ.get("CODEXBAR_CONFIG", "/config/codexbar/config.json"))
CODEXBAR_URL = "http://127.0.0.1:8080"
MAX_BODY = 512 * 1024
LOGIN_TIMEOUT = 10 * 60
LOGIN_SESSIONS: dict[str, "LoginSession"] = {}
LOGIN_LOCK = threading.Lock()
AUTH_FILES = {
    "codex": pathlib.Path("/config/.codex/auth.json"),
    "claude": pathlib.Path("/config/.claude/.credentials.json"),
}
ALLOWED_CLIENTS = {
    item.strip()
    for item in os.environ.get("CODEXBAR_SETUP_ALLOWED_CLIENTS", "172.30.32.2,127.0.0.1").split(",")
    if item.strip()
}

PROVIDER_PRESETS = [
    {
        "id": "codex",
        "name": "Codex",
        "defaultSource": "auto",
        "auth": "OAuth file from Codex CLI",
        "help": "Codex subscription quota uses OAuth from /config/.codex/auth.json. There is no normal API-key box for Codex quota.",
        "fields": [],
    },
    {
        "id": "claude",
        "name": "Claude",
        "defaultSource": "auto",
        "auth": "Claude CLI files, Admin API key, OAuth token, or cookie",
        "help": "For simple usage, copy Claude CLI auth into /config/.claude. For org spend, paste an Anthropic Admin API key and choose API.",
        "fields": ["apiKey", "cookieHeader"],
    },
    {
        "id": "openai",
        "name": "OpenAI API",
        "defaultSource": "api",
        "auth": "OpenAI Admin API key",
        "help": "Paste an OpenAI Admin key for organization/project spend and usage. Optional project ID goes in Workspace / Project ID.",
        "fields": ["apiKey", "workspaceID"],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "defaultSource": "api",
        "auth": "OpenRouter API key",
        "help": "Paste a sk-or-v1... key from https://openrouter.ai/settings/keys. OpenRouter does not use OAuth here.",
        "fields": ["apiKey"],
    },
    {
        "id": "litellm",
        "name": "LiteLLM",
        "defaultSource": "api",
        "auth": "Virtual key + proxy URL",
        "help": "Requires a LiteLLM virtual key and base URL in Server / Base URL.",
        "fields": ["apiKey", "enterpriseHost"],
    },
    {
        "id": "llmproxy",
        "name": "LLM Proxy",
        "defaultSource": "api",
        "auth": "API key + proxy URL",
        "help": "Requires an API key and base URL in Server / Base URL.",
        "fields": ["apiKey", "enterpriseHost"],
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "defaultSource": "api",
        "auth": "Google/Gemini credentials",
        "help": "Usually uses local Google/Gemini CLI credentials copied into the add-on config home.",
        "fields": ["apiKey"],
    },
    {
        "id": "copilot",
        "name": "Copilot",
        "defaultSource": "api",
        "auth": "GitHub/Copilot token",
        "help": "Use a Copilot-capable token if you have one, or leave blank for local auth methods.",
        "fields": ["apiKey"],
    },
]
PROVIDER_IDS = {provider["id"] for provider in PROVIDER_PRESETS}


def read_config() -> dict:
    if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
        return {"version": 1, "providers": []}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("providers", [])
            return data
    except json.JSONDecodeError:
        pass
    return {"version": 1, "providers": [], "_error": "Existing config is not valid JSON"}


def validate_config(data: object) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Config must be a JSON object."
    providers = data.get("providers", [])
    if not isinstance(providers, list):
        return False, "providers must be a list."
    seen: set[str] = set()
    for idx, provider in enumerate(providers):
        if not isinstance(provider, dict):
            return False, f"providers[{idx}] must be an object."
        pid = provider.get("id")
        if not isinstance(pid, str) or not pid.strip():
            return False, f"providers[{idx}].id is required."
        if pid in seen:
            return False, f"Duplicate provider id: {pid}."
        seen.add(pid)
        if "enabled" in provider and not isinstance(provider["enabled"], bool):
            return False, f"{pid}.enabled must be true or false."
        for key in ["source", "cookieSource", "apiKey", "cookieHeader", "enterpriseHost", "workspaceID", "region"]:
            if key in provider and provider[key] is not None and not isinstance(provider[key], str):
                return False, f"{pid}.{key} must be a string."
    return True, "ok"


def write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="config.", suffix=".json", dir=str(CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONFIG_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def run_codexbar_validate() -> dict:
    binary = shutil.which("codexbar") or "/usr/local/bin/codexbar"
    if not pathlib.Path(binary).exists():
        return {"available": False, "ok": None, "message": "codexbar binary is not installed yet"}
    env = os.environ.copy()
    env["CODEXBAR_CONFIG"] = str(CONFIG_PATH)
    try:
        proc = subprocess.run(
            [binary, "config", "validate", "--format", "json", "--pretty"],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 - report to UI
        return {"available": True, "ok": False, "message": str(exc)}
    return {
        "available": True,
        "ok": proc.returncode == 0,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def auth_status() -> dict:
    status: dict[str, dict[str, object]] = {}
    for provider, path in AUTH_FILES.items():
        item: dict[str, object] = {"path": str(path), "exists": path.exists()}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if provider == "codex":
                    tokens = data.get("tokens") if isinstance(data, dict) else None
                    item["ok"] = isinstance(tokens, dict) and bool(tokens.get("access_token") or tokens.get("refresh_token"))
                    item["account_id"] = tokens.get("account_id") if isinstance(tokens, dict) else None
                elif provider == "claude":
                    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
                    item["ok"] = isinstance(oauth, dict) and bool(oauth.get("accessToken") or oauth.get("refreshToken"))
                item["message"] = "OAuth file looks usable" if item.get("ok") else "File exists but does not look like the expected OAuth JSON"
            except Exception as exc:  # noqa: BLE001
                item["ok"] = False
                item["message"] = f"Could not read JSON: {exc}"
        else:
            item["ok"] = False
            item["message"] = "Not uploaded yet"
        status[provider] = item
    return status


def write_auth_file(provider: str, content: str) -> dict:
    if provider not in AUTH_FILES:
        raise ValueError("provider must be codex or claude")
    data = json.loads(content)
    if provider == "codex":
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict) or not (tokens.get("access_token") or tokens.get("refresh_token")):
            raise ValueError("Codex auth.json must contain tokens.access_token or tokens.refresh_token")
    if provider == "claude":
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict) or not (oauth.get("accessToken") or oauth.get("refreshToken")):
            raise ValueError("Claude .credentials.json must contain claudeAiOauth accessToken or refreshToken")
    path = AUTH_FILES[provider]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return auth_status()[provider]


class LoginSession:
    def __init__(self, provider: str, owner: str):
        self.owner = owner
        if provider == "codex":
            command = ["codex", "login", "--device-auth"]
        elif provider == "claude":
            command = ["claude", "auth", "login", "--claudeai"]
        else:
            raise ValueError("provider must be codex or claude")
        self.id = uuid.uuid4().hex
        self.provider = provider
        self.command = command
        self.output = ""
        self.url: str | None = None
        self.done = False
        self.ok = False
        self.error: str | None = None
        self.started = time.time()
        self.process: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = "/config"
        env["XDG_CONFIG_HOME"] = "/config/xdg"
        env["CODEXBAR_CONFIG"] = str(CONFIG_PATH)
        env["CODEXBAR_HOME"] = "/config"
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        return env

    def _append(self, text: str) -> None:
        self.output = (self.output + text)[-12000:]
        match = re.search(r"https?://[^\s)>'\"]+", self.output)
        if match:
            self.url = match.group(0).rstrip(".,;:)]}>")

    def _run(self) -> None:
        master_fd = None
        try:
            master_fd, slave_fd = os.openpty()
            self.process = subprocess.Popen(
                self.command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self._env(),
                preexec_fn=os.setsid,
                close_fds=True,
            )
            os.close(slave_fd)
            deadline = time.time() + LOGIN_TIMEOUT
            while time.time() < deadline:
                if self.process.poll() is not None:
                    break
                readable, _, _ = select.select([master_fd], [], [], 0.25)
                if readable:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    self._append(text)
            # drain after exit/timeout
            end_drain = time.time() + 1.5
            while time.time() < end_drain:
                readable, _, _ = select.select([master_fd], [], [], 0.1)
                if not readable:
                    continue
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._append(chunk.decode("utf-8", errors="replace"))
            if self.process.poll() is None:
                self.error = "Login timed out waiting for the provider CLI to finish. You can retry."
                self.cancel()
            else:
                self.ok = self.process.returncode == 0 or "successfully logged in" in self.output.lower() or "login successful" in self.output.lower()
                if not self.ok:
                    self.error = f"Login command exited with status {self.process.returncode}."
        except FileNotFoundError as exc:
            self.error = f"Missing login CLI: {exc}"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            self.done = True

    def cancel(self) -> None:
        proc = self.process
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()

    def status(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "command": " ".join(self.command),
            "done": self.done,
            "ok": self.ok,
            "error": self.error,
            "url": self.url,
            "output": self.output,
            "auth": auth_status().get(self.provider),
        }


def start_login(provider: str, owner: str) -> LoginSession:
    with LOGIN_LOCK:
        active_count = 0
        for sid, existing in list(LOGIN_SESSIONS.items()):
            if existing.done and time.time() - existing.started > 900:
                LOGIN_SESSIONS.pop(sid, None)
                continue
            if not existing.done:
                active_count += 1
                if existing.provider == provider and existing.owner == owner:
                    existing.cancel()
        if active_count >= 2:
            raise RuntimeError("Too many login sessions are already running. Cancel one and retry.")
        session = LoginSession(provider, owner)
        LOGIN_SESSIONS[session.id] = session
    return session


def get_login_session(session_id: str, owner: str | None = None) -> LoginSession | None:
    with LOGIN_LOCK:
        session = LOGIN_SESSIONS.get(session_id)
        if session and owner is not None and session.owner != owner:
            return None
        return session


def proxy_get(path: str, timeout: int = 45) -> tuple[int, bytes, str]:
    url = CODEXBAR_URL + path
    req = urllib.request.Request(url, headers={"Host": "127.0.0.1:8080"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "application/json; charset=utf-8")
            return resp.status, resp.read(), content_type
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "application/json; charset=utf-8")
    except Exception as exc:  # noqa: BLE001
        body = json.dumps({"error": f"CodexBar API unavailable: {exc}"}).encode()
        return 502, body, "application/json; charset=utf-8"


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexBarSetup/0.2"

    def log_message(self, fmt: str, *args: object) -> None:
        message = fmt % args
        message = re.sub(r"([?&]id=)[A-Za-z0-9_-]+", r"\1[REDACTED]", message)
        print(f"{self.address_string()} - {message}", flush=True)

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def client_allowed(self) -> bool:
        return not ALLOWED_CLIENTS or self.client_address[0] in ALLOWED_CLIENTS

    def reject_forbidden_client(self) -> bool:
        if self.client_allowed():
            return False
        self.send_json({"error": "forbidden"}, 403)
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.reject_forbidden_client():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = f"?{parsed.query}" if parsed.query else ""
        if path in ("/", "/index.html"):
            self.send_bytes((ROOT / "index.html").read_bytes(), 200, "text/html; charset=utf-8")
            return
        if path == "/api/config":
            cfg = read_config()
            self.send_json({"path": str(CONFIG_PATH), "config": cfg, "presets": PROVIDER_PRESETS})
            return
        if path == "/api/auth-status":
            self.send_json(auth_status())
            return
        if path == "/api/login/status":
            session_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            session = get_login_session(session_id, self.client_address[0])
            if not session:
                self.send_json({"ok": False, "error": "login session not found"}, 404)
                return
            self.send_json(session.status())
            return
        if path == "/api/validate":
            ok, msg = validate_config(read_config())
            payload = {"ok": ok, "message": msg, "codexbar": run_codexbar_validate()}
            self.send_json(payload, 200 if ok else 400)
            return
        if path in ("/health", "/usage", "/cost"):
            status, body, content_type = proxy_get(path + query)
            self.send_bytes(body, status, content_type)
            return
        if path == "/api/test":
            provider = urllib.parse.parse_qs(parsed.query).get("provider", [""])[0]
            if not provider:
                self.send_json({"ok": False, "error": "provider is required"}, 400)
                return
            if provider not in PROVIDER_IDS:
                self.send_json({"ok": False, "error": f"unknown provider: {provider}"}, 400)
                return
            status, body, _content_type = proxy_get("/usage?provider=" + urllib.parse.quote(provider))
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = body.decode("utf-8", errors="replace")
            self.send_json({"ok": 200 <= status < 300, "status": status, "provider": provider, "payload": payload}, 200 if status < 500 else 502)
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.reject_forbidden_client():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json_body()
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"Invalid JSON: {exc}"}, 400)
            return
        if parsed.path == "/api/config":
            data = payload.get("config") if isinstance(payload, dict) and "config" in payload else payload
            ok, msg = validate_config(data)
            if not ok:
                self.send_json({"ok": False, "error": msg}, 400)
                return
            backup = None
            if CONFIG_PATH.exists():
                backup = CONFIG_PATH.with_suffix(f".json.bak-{int(time.time())}")
                shutil.copy2(CONFIG_PATH, backup)
            write_config(data)  # type: ignore[arg-type]
            self.send_json({"ok": True, "path": str(CONFIG_PATH), "backup": str(backup) if backup else None, "validation": run_codexbar_validate()})
            return
        if parsed.path == "/api/login/start":
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "error": "Expected JSON object"}, 400)
                return
            try:
                session = start_login(str(payload.get("provider", "")), self.client_address[0])
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json({"ok": True, "session": session.status()})
            return
        if parsed.path == "/api/login/cancel":
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "error": "Expected JSON object"}, 400)
                return
            session = get_login_session(str(payload.get("id", "")), self.client_address[0])
            if not session:
                self.send_json({"ok": False, "error": "login session not found"}, 404)
                return
            session.cancel()
            self.send_json({"ok": True, "session": session.status()})
            return
        if parsed.path == "/api/auth-upload":
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "error": "Expected JSON object"}, 400)
                return
            try:
                result = write_auth_file(str(payload.get("provider", "")), str(payload.get("content", "")))
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json({"ok": True, "auth": result})
            return
        self.send_json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    host = os.environ.get("CODEXBAR_SETUP_HOST", "0.0.0.0")
    port = int(os.environ.get("CODEXBAR_SETUP_PORT", "8099"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"CodexBar setup UI listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
