#!/usr/bin/env python3
"""Small Home Assistant Ingress UI/API for CodexBar setup.

The CodexBar CLI already serves read-only usage/cost JSON on 127.0.0.1:8080.
This wrapper adds an admin GUI and a tiny config API so users do not need to hand-edit
provider_config_json or files in /addon_configs.

Ingress notes (things that break silently if changed):
- HA core proxies request bodies with Transfer-Encoding: chunked, so POST
  handling must decode chunked bodies, not just trust Content-Length.
- The browser URL under Ingress is /api/hassio_ingress/<token>[/...] and may
  lack a trailing slash; index.html gets a <base> tag injected from the
  X-Ingress-Path request header so relative API URLs always resolve.
- Ingress requests originate from 172.30.32.2 only.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import pathlib
import re
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return max(minimum, default)


ROOT = pathlib.Path("/usr/share/codexbar-addon")
CONFIG_PATH = pathlib.Path(os.environ.get("CODEXBAR_CONFIG", "/config/codexbar/config.json"))
CONFIG_HOME = pathlib.Path(os.environ.get("CODEXBAR_HOME", "/config"))
CODEXBAR_URL = "http://127.0.0.1:8080"
MAX_BODY = 512 * 1024
LOGIN_TIMEOUT = 10 * 60
LOGIN_SESSIONS: dict[str, "LoginSession"] = {}
LOGIN_LOCK = threading.Lock()
HISTORY_PATH = CONFIG_PATH.parent / "history.json"
ACTIVITY_LOG_PATH = CONFIG_PATH.parent / "activity.log"
HISTORY_DAYS = 7
HISTORY_INTERVAL = env_int("CODEXBAR_HISTORY_INTERVAL", 300, 60)
HISTORY_FUTURE_TOLERANCE = 5 * 60
PROVIDER_TIMEOUT = env_int("CODEXBAR_REQUEST_TIMEOUT", 90, 5)
PROXY_TIMEOUT = PROVIDER_TIMEOUT + 20
HISTORY_LOCK = threading.Lock()
USAGE_FETCH_LOCK = threading.Lock()
CLAUDE_CLI_LOCK = threading.Lock()
BACKGROUND_STATUS: dict[str, object] = {
    "running": False,
    "intervalSeconds": HISTORY_INTERVAL,
    "lastAttempt": None,
    "lastSuccess": None,
    "lastError": None,
    "claudeAuthOk": None,
    "sampleCount": 0,
    "providerStatus": {},
}
BACKGROUND_LOCK = threading.Lock()
# OSC sequences (terminal hyperlinks/titles): ESC ] ... BEL or ESC ] ... ESC \
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# CSI and two-byte escape sequences (colors, cursor movement, ...)
CSI_RE = re.compile(r"\x1b(?:[@-Z\\^_]|\[[0-?]*[ -/]*[@-~])")
# Any remaining C0 control characters except newline and tab
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}\x00-\x20]+")
CODE_RE = re.compile(r"(?:one-time code|device code)[^\n]*\n+\s*([A-Z0-9]{3,12}-[A-Z0-9]{3,12})", re.IGNORECASE)
PROMPT_RE = re.compile(r"(?:paste|enter)[^\n]*(?:code|token)[^\n]*[>:]\s*$", re.IGNORECASE)
SENSITIVE_ACTIVITY_KEY_RE = re.compile(
    r"(?i)(?:authorization|password|secret|token|callback|(?:one[_ -]?time|device|oauth)[_ -]?code|url)"
)
AUTH_FILES = {
    "codex": CONFIG_HOME / ".codex/auth.json",
    "claude": CONFIG_HOME / ".claude/.credentials.json",
}
ALLOWED_CLIENTS = {
    item.strip()
    for item in os.environ.get("CODEXBAR_SETUP_ALLOWED_CLIENTS", "172.30.32.2,127.0.0.1").split(",")
    if item.strip()
}

PROVIDER_PRESETS = [
    {
        "id": "codex",
        "name": "OpenAI Codex",
        "defaultSource": "auto",
        "auth": "ChatGPT subscription login",
        "help": "Uses the official Codex device-code flow and stores OAuth under /config/.codex.",
        "fields": [],
    },
    {
        "id": "claude",
        "name": "Claude",
        "defaultSource": "oauth",
        "auth": "Claude subscription login",
        "help": "Uses the official Claude login URL and stores OAuth under /config/.claude.",
        "fields": [],
    },
]
PROVIDER_IDS = {provider["id"] for provider in PROVIDER_PRESETS}


def default_config() -> dict:
    return {
        "version": 1,
        "providers": [
            {"id": "codex", "enabled": True, "source": "auto"},
            {"id": "claude", "enabled": True, "source": "oauth"},
        ],
    }


def clean_terminal_text(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = CSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return CTRL_RE.sub("", text)


def ensure_auth_dirs() -> None:
    for path in [CONFIG_PATH.parent, *(auth.parent for auth in AUTH_FILES.values())]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError as exc:
            print(f"WARNING: could not prepare {path}: {exc}", flush=True)


def read_config() -> dict:
    if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
        return default_config()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("providers", [])
            return data
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {**default_config(), "_error": "Existing config is not valid JSON"}


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
        if pid not in PROVIDER_IDS:
            return False, f"Unsupported provider in this add-on version: {pid}."
        seen.add(pid)
        if "enabled" in provider and not isinstance(provider["enabled"], bool):
            return False, f"{pid}.enabled must be true or false."
        for key in ["source", "cookieSource", "apiKey", "cookieHeader", "enterpriseHost", "workspaceID", "region"]:
            if key in provider and provider[key] is not None and not isinstance(provider[key], str):
                return False, f"{pid}.{key} must be a string."
    return True, "ok"


def fsync_directory(path: pathlib.Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="config.", suffix=".json", dir=str(CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONFIG_PATH)
        fsync_directory(CONFIG_PATH.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def ensure_basic_config() -> None:
    """Initialize a missing config without replacing valid user settings."""
    if not CONFIG_PATH.exists():
        write_config(default_config())
        return
    if CONFIG_PATH.stat().st_size == 0:
        current = {**default_config(), "_error": "Existing config is empty"}
    else:
        current = read_config()
    valid, _message = validate_config(current)
    if valid and "_error" not in current:
        return
    backup = CONFIG_PATH.parent / f"config.invalid-{time.time_ns()}.json"
    try:
        shutil.copy2(CONFIG_PATH, backup)
        os.chmod(backup, 0o600)
        with backup.open("rb") as fh:
            os.fsync(fh.fileno())
        fsync_directory(CONFIG_PATH.parent)
    except OSError as exc:
        print(f"WARNING: invalid config preserved because backup failed: {sanitize_activity_message(exc)}", flush=True)
        return
    write_config(default_config())


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
            item["message"] = "Not logged in yet"
        status[provider] = item
    return status



def provider_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(CONFIG_HOME)
    env["XDG_CONFIG_HOME"] = str(CONFIG_HOME / "xdg")
    env["CODEXBAR_CONFIG"] = str(CONFIG_PATH)
    env["CODEXBAR_HOME"] = str(CONFIG_HOME)
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    return env


def sanitize_activity_message(value: object) -> str:
    """Remove URLs and token-like values before diagnostics are persisted."""
    message = str(value).replace("\r", " ").replace("\n", " ")[:300]
    message = re.sub(r'''\\+(?=["'])''', "", message)
    message = re.sub(r"https?://\S+", "[URL REDACTED]", message, flags=re.IGNORECASE)
    message = re.sub(
        r'''(?ix)\bauthorization\b["']?(?:\s*[:=]\s*|\s+).*$''',
        "authorization=[REDACTED]",
        message,
    )
    message = re.sub(
        r'''(?ix)\b(access[_ -]?token|refresh[_ -]?token|id[_ -]?token|client[_ -]?secret|password|(?:one[_ -]?time|device|callback|oauth)[_ -]?code|callback)\b["']?\s*[:=]\s*(?:bearer\s+[A-Za-z0-9._~+/=-]+|"[^"]*"|'[^']*'|[^,;}\s]+)''',
        r"\1=[REDACTED]",
        message,
    )
    message = re.sub(
        r'''(?ix)\b((?:one[_ -]?time|device|callback|oauth)[_ -]?code)\b\s+(?:(?:is|was)\s*[:=]?\s*)?["']?[A-Za-z0-9][A-Za-z0-9._~+/=-]{3,}["']?''',
        r"\1 [REDACTED]",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}\b", "[JWT REDACTED]", message)
    return message


def sanitize_activity_value(value: object, key: str = "") -> object:
    """Recursively sanitize structured diagnostics, including legacy log fields."""
    if key and SENSITIVE_ACTIVITY_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_activity_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_activity_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_activity_message(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return sanitize_activity_message(value)


def activity_log(event: str, message: str, **fields: object) -> None:
    """Write a bounded, secret-free activity log for background collection."""
    safe_message = sanitize_activity_message(message)
    safe_fields = sanitize_activity_value(fields)
    entry = {
        **(safe_fields if isinstance(safe_fields, dict) else {}),
        "timestamp": int(time.time()),
        "event": sanitize_activity_message(event),
        "message": safe_message,
    }
    try:
        ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if ACTIVITY_LOG_PATH.exists() and ACTIVITY_LOG_PATH.stat().st_size > 512 * 1024:
            rotated = ACTIVITY_LOG_PATH.with_suffix(".log.1")
            os.replace(ACTIVITY_LOG_PATH, rotated)
        with ACTIVITY_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.chmod(ACTIVITY_LOG_PATH, 0o600)
    except OSError as exc:
        print(f"WARNING: could not write activity log: {sanitize_activity_message(exc)}", flush=True)
    print(f"background[{event}] {safe_message}", flush=True)


def read_activity_log(limit: int = 30) -> list[dict[str, object]]:
    if not ACTIVITY_LOG_PATH.exists():
        return []
    entries: list[dict[str, object]] = []
    try:
        for line in ACTIVITY_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                sanitized = sanitize_activity_value(item)
                if isinstance(sanitized, dict):
                    entries.append(sanitized)
    except OSError:
        return []
    return entries


def normalize_history_item(
    item: object,
    maximum_timestamp: int | None = None,
) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    provider = str(item.get("provider", "")).lower()
    if provider not in PROVIDER_IDS:
        return None
    try:
        timestamp = int(item["timestamp"])
        used = float(item["weeklyUsedPercent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0 or (maximum_timestamp is not None and timestamp > maximum_timestamp):
        return None
    if not math.isfinite(used) or not 0 <= used <= 100:
        return None
    return {"timestamp": timestamp, "provider": provider, "weeklyUsedPercent": round(used, 2)}


def quarantine_history() -> pathlib.Path | None:
    """Preserve an unreadable history file before starting a clean one."""
    if not HISTORY_PATH.exists():
        return None
    destination = HISTORY_PATH.with_name(
        f"{HISTORY_PATH.stem}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}{HISTORY_PATH.suffix}"
    )
    try:
        os.replace(HISTORY_PATH, destination)
        os.chmod(destination, 0o600)
        fsync_directory(HISTORY_PATH.parent)
        print(f"WARNING: quarantined malformed history as {destination.name}", flush=True)
        return destination
    except OSError as exc:
        print(f"WARNING: could not quarantine malformed history: {sanitize_activity_message(exc)}", flush=True)
        return None


def load_history(reference_time: int | None = None) -> list[dict[str, object]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except OSError:
        return []
    except json.JSONDecodeError:
        quarantine_history()
        return []
    if not isinstance(data, list):
        quarantine_history()
        return []
    maximum_timestamp = int(reference_time or time.time()) + HISTORY_FUTURE_TOLERANCE
    return [
        normalized
        for item in data
        if (normalized := normalize_history_item(item, maximum_timestamp)) is not None
    ]


def write_history(samples: list[dict[str, object]]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="history.", suffix=".json", dir=str(HISTORY_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(samples, fh, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, HISTORY_PATH)
        fsync_directory(HISTORY_PATH.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def usage_samples(payload: object, timestamp: int | None = None) -> list[dict[str, object]]:
    now = int(timestamp or time.time())
    providers = payload if isinstance(payload, list) else [payload]
    samples: list[dict[str, object]] = []
    for item in providers:
        if not isinstance(item, dict) or item.get("error"):
            continue
        provider = str(item.get("provider") or item.get("providerID") or "").lower()
        if provider not in PROVIDER_IDS:
            continue
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else item
        weekly = usage.get("secondary") if isinstance(usage, dict) else None
        if not isinstance(weekly, dict):
            continue
        raw_used = weekly.get("usedPercent")
        if raw_used is None and weekly.get("remainingPercent") is not None:
            try:
                raw_used = 100 - float(weekly["remainingPercent"])
            except (TypeError, ValueError):
                raw_used = None
        if raw_used is None:
            continue
        try:
            used = float(raw_used)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(used):
            continue
        used = max(0.0, min(100.0, used))
        samples.append({"timestamp": now, "provider": provider, "weeklyUsedPercent": round(used, 2)})
    return samples


def provider_sample_status(payload: object, timestamp: int | None = None) -> dict[str, dict[str, object]]:
    providers = payload if isinstance(payload, list) else [payload]
    status = {
        provider: {"ok": False, "error": "provider missing from usage response"}
        for provider in sorted(PROVIDER_IDS)
    }
    for item in providers:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or item.get("providerID") or "").lower()
        if provider not in PROVIDER_IDS:
            continue
        error = item.get("error")
        if error:
            if isinstance(error, dict):
                detail = error.get("message") or error.get("kind") or "provider error"
            else:
                detail = error
            status[provider] = {"ok": False, "error": sanitize_activity_message(detail)}
        elif usage_samples(item, timestamp):
            status[provider] = {"ok": True, "error": None}
        else:
            status[provider] = {"ok": False, "error": "weekly quota unavailable"}
    return status


def record_history(payload: object, timestamp: int | None = None) -> int:
    """Prune retained history and return the number of newly appended points."""
    now = int(timestamp or time.time())
    cutoff = now - HISTORY_DAYS * 24 * 60 * 60
    new_samples = usage_samples(payload, now)
    appended = 0
    with HISTORY_LOCK:
        existing = [item for item in load_history(now) if cutoff <= int(str(item["timestamp"])) <= now]
        for sample in new_samples:
            provider_items = [item for item in existing if item["provider"] == sample["provider"]]
            last = max(provider_items, key=lambda item: int(str(item["timestamp"])), default=None)
            if last and now - int(str(last["timestamp"])) < max(60, HISTORY_INTERVAL - 5):
                continue
            existing.append(sample)
            appended += 1
        existing.sort(key=lambda item: int(str(item["timestamp"])))
        write_history(existing)
        with BACKGROUND_LOCK:
            BACKGROUND_STATUS["sampleCount"] = len(existing)
    return appended


def claude_auth_keepalive() -> bool | None:
    """Let the official Claude CLI refresh its own OAuth file without using quota."""
    if not auth_status().get("claude", {}).get("ok"):
        return None
    if not CLAUDE_CLI_LOCK.acquire(blocking=False):
        return None
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"],
            env=provider_environment(),
            text=True,
            capture_output=True,
            timeout=45,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        CLAUDE_CLI_LOCK.release()


def collect_background_sample(attempt: int | None = None) -> int:
    attempt = int(attempt or time.time())
    with BACKGROUND_LOCK:
        BACKGROUND_STATUS["lastAttempt"] = attempt
    try:
        claude_ok = claude_auth_keepalive()
        status, body, _ = proxy_get("/usage?provider=both", timeout=PROXY_TIMEOUT)
        if not 200 <= status < 300:
            raise RuntimeError(f"CodexBar usage returned HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        # Retention must continue even when the backend is unavailable or malformed.
        record_history([], attempt)
        raise
    provider_status = provider_sample_status(payload, attempt)
    valid_count = sum(1 for item in provider_status.values() if item["ok"])
    appended = record_history(payload, attempt)
    failures = [
        f"{provider}: {item['error']}"
        for provider, item in provider_status.items()
        if not item["ok"]
    ]
    partial_error = "; ".join(failures) if failures else None
    with BACKGROUND_LOCK:
        BACKGROUND_STATUS["providerStatus"] = provider_status
        BACKGROUND_STATUS["claudeAuthOk"] = claude_ok
    if valid_count == 0:
        raise RuntimeError(f"usage response contained no weekly quota samples ({partial_error})")
    retained_count = len(load_history())
    with BACKGROUND_LOCK:
        BACKGROUND_STATUS.update({
            "lastSuccess": attempt,
            "lastError": f"Partial sample: {partial_error}" if partial_error else None,
            "sampleCount": retained_count,
        })
    activity_log(
        "partial" if partial_error else "sample",
        f"sampled {valid_count} providers; appended {appended} history points"
        + (f"; {partial_error}" if partial_error else ""),
        claudeAuthOk=claude_ok,
    )
    return appended


def background_collector() -> None:
    try:
        record_history([], int(time.time()))
    except OSError as exc:
        activity_log("error", f"startup history maintenance failed: {sanitize_activity_message(exc)}")
    retained = load_history()
    newest = max((int(str(item["timestamp"])) for item in retained), default=None)
    with BACKGROUND_LOCK:
        BACKGROUND_STATUS["running"] = True
        BACKGROUND_STATUS["sampleCount"] = len(retained)
        BACKGROUND_STATUS["lastSuccess"] = newest
        BACKGROUND_STATUS["providerStatus"] = {}
    activity_log("collector", f"started; sampling every {HISTORY_INTERVAL} seconds")
    startup_failures = 0
    next_run = time.monotonic()
    while True:
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            collect_background_sample()
        except Exception as exc:  # noqa: BLE001 - sanitized before persistence
            error = sanitize_activity_message(exc)
            with BACKGROUND_LOCK:
                BACKGROUND_STATUS["lastError"] = error
            activity_log("error", error)
            startup_failures += 1
            retry_delay = 15 if startup_failures <= 4 else HISTORY_INTERVAL
            next_run = time.monotonic() + retry_delay
        else:
            startup_failures = 5
            next_run = time.monotonic() + HISTORY_INTERVAL


class LoginSession:
    """Runs a provider login CLI on a PTY and exposes its state to the UI.

    codex login --device-auth prints a URL + one-time code and polls; no input
    is needed. claude auth login prints an OAuth URL and then blocks on
    "Paste code here if prompted >", so the UI must be able to send text back
    into the PTY via send_input().
    """

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
        self.code: str | None = None
        self.awaiting_input = False
        self.done = False
        self.ok = False
        self.cancelled = False
        self.error: str | None = None
        self.started = time.time()
        self.process: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self.io_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _env(self) -> dict[str, str]:
        env = provider_environment()
        env["TERM"] = "xterm-256color"
        return env

    def _append(self, text: str) -> None:
        self.output = clean_terminal_text(self.output + text)[-12000:]
        match = URL_RE.search(self.output)
        if match:
            self.url = match.group(0).rstrip(".,;:!?'\"")
        code_match = CODE_RE.search(self.output)
        if code_match:
            self.code = code_match.group(1)
        tail = self.output.rstrip("\n").rsplit("\n", 1)[-1]
        self.awaiting_input = bool(PROMPT_RE.search(tail))

    def _run(self) -> None:
        claude_lock_acquired = False
        try:
            if self.provider == "claude":
                while not self.cancelled:
                    if CLAUDE_CLI_LOCK.acquire(timeout=0.25):
                        claude_lock_acquired = True
                        break
                if not claude_lock_acquired:
                    self.error = "Login cancelled."
                    return
            if self.cancelled:
                self.error = "Login cancelled."
                return
            ensure_auth_dirs()
            master_fd, slave_fd = os.openpty()
            with self.io_lock:
                if self.cancelled:
                    os.close(master_fd)
                    os.close(slave_fd)
                    self.error = "Login cancelled."
                    return
                self.master_fd = master_fd
                try:
                    self.process = subprocess.Popen(
                        self.command,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        env=self._env(),
                        preexec_fn=os.setsid,
                        close_fds=True,
                    )
                finally:
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
                    self._append(chunk.decode("utf-8", errors="replace"))
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
            elif self.cancelled:
                self.error = "Login cancelled."
            else:
                auth = auth_status().get(self.provider, {})
                auth_fresh = bool(auth.get("ok")) and AUTH_FILES[self.provider].stat().st_mtime >= self.started
                self.ok = (
                    self.process.returncode == 0
                    or auth_fresh
                    or "successfully logged in" in self.output.lower()
                    or "login successful" in self.output.lower()
                )
                if not self.ok:
                    self.error = f"Login command exited with status {self.process.returncode}."
        except FileNotFoundError as exc:
            self.error = f"Missing login CLI: {exc}"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            with self.io_lock:
                if self.master_fd is not None:
                    try:
                        os.close(self.master_fd)
                    except OSError:
                        pass
                    self.master_fd = None
            if claude_lock_acquired:
                CLAUDE_CLI_LOCK.release()
            self.awaiting_input = False
            self.done = True

    def send_input(self, text: str) -> None:
        if self.done:
            raise RuntimeError("Login session already finished.")
        text = text.strip()
        if not text:
            raise ValueError("Nothing to send.")
        if len(text) > 4096:
            raise ValueError("Input is too long.")
        with self.io_lock:
            if self.master_fd is None:
                raise RuntimeError("Login session is not accepting input.")
            os.write(self.master_fd, (text + "\r").encode("utf-8"))
        self.awaiting_input = False

    def cancel(self) -> None:
        with self.io_lock:
            self.cancelled = True
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
            "cancelled": self.cancelled,
            "error": self.error,
            "url": self.url,
            "code": self.code,
            "awaitingInput": self.awaiting_input,
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
                if existing.provider == provider and existing.owner == owner:
                    existing.cancel()
                else:
                    active_count += 1
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


def proxy_get(path: str, timeout: int | float | None = None) -> tuple[int, bytes, str]:
    request_budget = max(0.05, float(PROXY_TIMEOUT if timeout is None else timeout))
    deadline = time.monotonic() + request_budget

    def update_response_timeout(response: object, remaining: float) -> None:
        pending = [response]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            set_timeout = getattr(current, "settimeout", None)
            if callable(set_timeout):
                set_timeout(max(0.001, remaining))
                return
            for attribute in ("fp", "raw", "_sock"):
                child = getattr(current, attribute, None)
                if child is not None:
                    pending.append(child)

    def read_with_deadline(response: object) -> bytes:
        chunks: list[bytes] = []
        read_chunk = getattr(response, "read1", None) or getattr(response, "read")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CodexBar response exceeded the request deadline")
            update_response_timeout(response, remaining)
            chunk = read_chunk(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def perform_request(request_timeout: float) -> tuple[int, bytes, str]:
        endpoint = urllib.parse.urlsplit(CODEXBAR_URL)
        if endpoint.scheme != "http" or not endpoint.hostname:
            raise ValueError("CODEXBAR_URL must be a local HTTP endpoint")
        target = f"{endpoint.path.rstrip('/')}{path}" or "/"
        connection = http.client.HTTPConnection(
            endpoint.hostname,
            endpoint.port or 80,
            timeout=max(0.001, request_timeout),
        )
        deadline_reached = threading.Event()

        def abort_at_deadline() -> None:
            deadline_reached.set()
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        watchdog = threading.Timer(max(0.001, deadline - time.monotonic()), abort_at_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.request("GET", target, headers={"Host": "127.0.0.1:8080"})
            with connection.getresponse() as resp:
                content_type = resp.headers.get("Content-Type", "application/json; charset=utf-8")
                return resp.status, read_with_deadline(resp), content_type
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                body = json.dumps({"error": "CodexBar response exceeded the request deadline"}).encode()
                return 504, body, "application/json; charset=utf-8"
            body = json.dumps({"error": f"CodexBar API unavailable: {sanitize_activity_message(exc)}"}).encode()
            return 502, body, "application/json; charset=utf-8"
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": f"CodexBar API unavailable: {sanitize_activity_message(exc)}"}).encode()
            return 502, body, "application/json; charset=utf-8"
        finally:
            watchdog.cancel()
            connection.close()

    lock_acquired = False
    if path.startswith("/usage"):
        lock_acquired = USAGE_FETCH_LOCK.acquire(timeout=request_budget)
        if not lock_acquired:
            body = json.dumps({"error": "CodexBar usage request busy; retry shortly"}).encode()
            return 503, body, "application/json; charset=utf-8"
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            body = json.dumps({"error": "CodexBar request timed out while waiting for another usage fetch"}).encode()
            return 503, body, "application/json; charset=utf-8"
        return perform_request(max(0.01, remaining))
    finally:
        if lock_acquired:
            USAGE_FETCH_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexBarSetup/0.3"
    # HTTP/1.1 keeps ingress connections alive; responses always carry Content-Length.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        message = fmt % args
        message = re.sub(r"([?&]id=)[A-Za-z0-9_-]+", r"\1[REDACTED]", message)
        print(f"{self.address_string()} - {message}", flush=True)

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_bytes(body, status, "application/json; charset=utf-8")

    def send_bytes(self, body: bytes, status: int, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self) -> bytes:
        # HA core's ingress proxy streams POST bodies as Transfer-Encoding:
        # chunked with no Content-Length; BaseHTTPRequestHandler does not
        # decode that, so handle it here or every POST arrives empty.
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = self.rfile.readline(128).split(b";", 1)[0].strip()
                try:
                    size = int(size_line or b"0", 16)
                except ValueError as exc:
                    raise ValueError(f"Bad chunked encoding: {size_line!r}") from exc
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_BODY:
                    raise ValueError("Request body is too large")
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF after each chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Request body is too large")
        return self.rfile.read(length)

    def read_json_body(self) -> object:
        raw = self.read_body()
        if not raw:
            raise ValueError("Empty request body")
        return json.loads(raw.decode("utf-8"))

    def client_allowed(self) -> bool:
        return not ALLOWED_CLIENTS or self.client_address[0] in ALLOWED_CLIENTS

    def reject_forbidden_client(self) -> bool:
        if self.client_allowed():
            return False
        print(f"Rejected request from {self.client_address[0]} (allowed: {sorted(ALLOWED_CLIENTS)})", flush=True)
        self.send_json(
            {"error": f"forbidden: client {self.client_address[0]} is not the Home Assistant ingress proxy"},
            403,
        )
        return True

    def serve_index(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        ingress_path = (self.headers.get("X-Ingress-Path") or "").strip()
        if ingress_path.startswith("/"):
            base = urllib.parse.quote(ingress_path.rstrip("/"), safe="/%") + "/"
            html = html.replace('<base href="./">', f'<base href="{base}">', 1)
        self.send_bytes(html.encode("utf-8"), 200, "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if self.reject_forbidden_client():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = f"?{parsed.query}" if parsed.query else ""
        if path in ("", "/", "/index.html"):
            self.serve_index()
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
        if path == "/api/history":
            cutoff = int(time.time()) - HISTORY_DAYS * 24 * 60 * 60
            with HISTORY_LOCK:
                samples = [item for item in load_history() if int(item.get("timestamp", 0)) >= cutoff]
            self.send_json({"days": HISTORY_DAYS, "intervalSeconds": HISTORY_INTERVAL, "samples": samples})
            return
        if path == "/api/background-status":
            with BACKGROUND_LOCK:
                status_payload = dict(BACKGROUND_STATUS)
            status_payload["recentActivity"] = read_activity_log()
            self.send_json(status_payload)
            return
        if path == "/api/validate":
            ok, msg = validate_config(read_config())
            payload = {"ok": ok, "message": msg, "codexbar": run_codexbar_validate()}
            self.send_json(payload, 200 if ok else 400)
            return
        if path == "/health":
            # Watchdog endpoint: the setup UI (which serves Ingress) is healthy
            # even while the CodexBar backend is still starting, so always 200
            # and report backend state in the payload.
            status, body, _content_type = proxy_get("/health" + query, timeout=2)
            payload: dict[str, object] = {"ok": True, "backendStatus": status}
            try:
                backend = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                backend = body.decode("utf-8", errors="replace")[:500]
            payload["backend"] = backend
            if isinstance(backend, dict) and backend.get("version"):
                payload["version"] = backend["version"]
            self.send_json(payload, 200)
            return
        if path in ("/usage", "/cost"):
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
        self.send_json({"error": f"not found: GET {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.reject_forbidden_client():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json_body()
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"Invalid JSON body for {parsed.path}: {exc}"}, 400)
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
        if parsed.path == "/api/login/input":
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "error": "Expected JSON object"}, 400)
                return
            session = get_login_session(str(payload.get("id", "")), self.client_address[0])
            if not session:
                self.send_json({"ok": False, "error": "login session not found"}, 404)
                return
            try:
                session.send_input(str(payload.get("text", "")))
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
        self.send_json({"ok": False, "error": f"not found: POST {parsed.path}"}, 404)


def main() -> None:
    host = os.environ.get("CODEXBAR_SETUP_HOST", "0.0.0.0")
    port = int(os.environ.get("CODEXBAR_SETUP_PORT", "8099"))
    ensure_auth_dirs()
    ensure_basic_config()
    collector = threading.Thread(target=background_collector, name="usage-history", daemon=True)
    collector.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"CodexBar setup UI listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
