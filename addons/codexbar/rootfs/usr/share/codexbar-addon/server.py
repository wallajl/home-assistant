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
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path("/usr/share/codexbar-addon")
CONFIG_PATH = pathlib.Path(os.environ.get("CODEXBAR_CONFIG", "/config/codexbar/config.json"))
CODEXBAR_URL = "http://127.0.0.1:8080"
MAX_BODY = 512 * 1024
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
        "auth": "Local Codex CLI / OAuth files",
        "help": "Best if /config/.codex contains your Codex CLI auth. API-key setup is not used for Codex quota data.",
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
        "help": "Shows OpenRouter credit balance and usage where supported.",
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
        print(f"{self.address_string()} - {fmt % args}", flush=True)

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
        if path == "/api/validate":
            ok, msg = validate_config(read_config())
            payload = {"ok": ok, "message": msg, "codexbar": run_codexbar_validate()}
            self.send_json(payload, 200 if ok else 400)
            return
        if path in ("/health", "/usage", "/cost"):
            status, body, content_type = proxy_get(path + query)
            self.send_bytes(body, status, content_type)
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
        self.send_json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    host = os.environ.get("CODEXBAR_SETUP_HOST", "0.0.0.0")
    port = int(os.environ.get("CODEXBAR_SETUP_PORT", "8099"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"CodexBar setup UI listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
