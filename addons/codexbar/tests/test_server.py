#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest

ADDON_DIR = pathlib.Path(__file__).resolve().parents[1]
SERVER_PATH = ADDON_DIR / "rootfs/usr/share/codexbar-addon/server.py"

os.environ.setdefault("CODEXBAR_CONFIG", "/tmp/codexbar-tests/config.json")
os.environ.setdefault("CODEXBAR_HOME", "/tmp/codexbar-tests")
os.environ["CODEXBAR_HISTORY_INTERVAL"] = "300"
os.environ["CODEXBAR_REQUEST_TIMEOUT"] = "120"

spec = importlib.util.spec_from_file_location("codexbar_addon_server", SERVER_PATH)
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codexbar-tests-")
        root = pathlib.Path(self.temp.name)
        setattr(server, "CONFIG_HOME", root)
        setattr(server, "CONFIG_PATH", root / "config.json")
        setattr(server, "AUTH_FILES", {
            "codex": root / ".codex/auth.json",
            "claude": root / ".claude/.credentials.json",
        })
        setattr(server, "HISTORY_PATH", root / "history.json")
        setattr(server, "ACTIVITY_LOG_PATH", root / "activity.log")
        with server.BACKGROUND_LOCK:
            server.BACKGROUND_STATUS.update({
                "running": False,
                "intervalSeconds": server.HISTORY_INTERVAL,
                "lastAttempt": None,
                "lastSuccess": None,
                "lastError": None,
                "claudeAuthOk": None,
                "sampleCount": 0,
                "providerStatus": {},
            })

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def payload() -> list[dict[str, object]]:
        return [
            {"provider": "codex", "usage": {"secondary": {"usedPercent": 35}}},
            {"provider": "claude", "usage": {"secondary": {"remainingPercent": 70}}},
        ]

    def test_history_prunes_invalid_and_expired_records_without_new_samples(self) -> None:
        now = int(time.time())
        server.HISTORY_PATH.write_text(json.dumps([
            {"timestamp": now - 8 * 86400, "provider": "codex", "weeklyUsedPercent": 10},
            {"timestamp": "bad", "provider": "claude", "weeklyUsedPercent": "bad"},
            {"timestamp": now, "provider": "claude", "weeklyUsedPercent": float("nan")},
            {"timestamp": now - 100, "provider": "codex", "weeklyUsedPercent": 25},
        ]))

        self.assertEqual(server.record_history([], now), 0)
        self.assertEqual(server.load_history(), [
            {"timestamp": now - 100, "provider": "codex", "weeklyUsedPercent": 25.0}
        ])
        self.assertEqual(stat.S_IMODE(server.HISTORY_PATH.stat().st_mode), 0o600)

    def test_duplicate_samples_report_zero_appends_but_remain_successful(self) -> None:
        now = int(time.time())
        self.assertEqual(server.record_history(self.payload(), now), 2)
        before = server.load_history()
        self.assertEqual(server.record_history(self.payload(), now + 60), 0)
        self.assertEqual(server.load_history(), before)

        original_proxy = server.proxy_get
        original_keepalive = server.claude_auth_keepalive
        captured: dict[str, object] = {}
        try:
            setattr(server, "claude_auth_keepalive", lambda: True)

            def fake_proxy(path: str, timeout: int | None = None) -> tuple[int, bytes, str]:
                captured.update(path=path, timeout=timeout)
                return 200, json.dumps(self.payload()).encode(), "application/json"

            setattr(server, "proxy_get", fake_proxy)
            self.assertEqual(server.collect_background_sample(now + 60), 0)
        finally:
            setattr(server, "proxy_get", original_proxy)
            setattr(server, "claude_auth_keepalive", original_keepalive)

        self.assertEqual(captured["timeout"], server.PROXY_TIMEOUT)
        self.assertEqual(server.BACKGROUND_STATUS["lastSuccess"], now + 60)

    def test_activity_messages_redact_urls_and_token_values(self) -> None:
        cases = [
            ("authorization=secret https://example.test/callback?code=secret", ("authorization=secret", "code=secret")),
            ("Authorization: Bearer BEARER_SENTINEL_ALPHA_123", ("BEARER_SENTINEL_ALPHA_123",)),
            ("Authorization: Basic BASIC_SENTINEL_BETA_456", ("BASIC_SENTINEL_BETA_456",)),
            ("Authorization: Token TOKEN_SENTINEL_GAMMA_789", ("TOKEN_SENTINEL_GAMMA_789",)),
            ("authorization='Bearer quotedsecret'", ("quotedsecret",)),
            ("request failed with Bearer standalone-secret", ("standalone-secret",)),
            ("id_token=eyJheader.payload.signature; retrying", ("eyJheader",)),
            ("client_secret: topsecret, provider failed", ("topsecret",)),
        ]
        for message, secrets in cases:
            with self.subTest(message=message):
                safe = server.sanitize_activity_message(message)
                server.activity_log("error", message)
                raw = server.ACTIVITY_LOG_PATH.read_text()
                exposed = json.dumps(server.read_activity_log())
                for secret in secrets:
                    self.assertNotIn(secret, safe)
                    self.assertNotIn(secret, raw)
                    self.assertNotIn(secret, exposed)
                self.assertNotIn("http", safe)

    def test_activity_messages_redact_quoted_json_and_whitespace_codes(self) -> None:
        cases = [
            ('provider error {"access_token":"LEAK_SENTINEL_ALPHA_77"}', "LEAK_SENTINEL_ALPHA_77"),
            (r'provider error {\"access_token\":\"ESCAPED_SENTINEL_ALPHA_19\"}', "ESCAPED_SENTINEL_ALPHA_19"),
            ("device code DEVICE-SENTINEL-42 was rejected", "DEVICE-SENTINEL-42"),
            ("oauth code OAUTH-SENTINEL-84 expired", "OAUTH-SENTINEL-84"),
            ("device code is: DEVICE-COLON-SENTINEL-21", "DEVICE-COLON-SENTINEL-21"),
            ("oauth code was: OAUTH-COLON-SENTINEL-43", "OAUTH-COLON-SENTINEL-43"),
            ("callback code is: CALLBACK-COLON-SENTINEL-65", "CALLBACK-COLON-SENTINEL-65"),
        ]
        for message, secret in cases:
            with self.subTest(message=message):
                self.assertNotIn(secret, server.sanitize_activity_message(message))
                server.activity_log("error", message)
                self.assertNotIn(secret, server.ACTIVITY_LOG_PATH.read_text())
                self.assertNotIn(secret, json.dumps(server.read_activity_log()))

    def test_activity_log_sanitizes_structured_fields_and_legacy_reads(self) -> None:
        server.activity_log(
            "sample",
            "safe message",
            authorization="Bearer TOKEN_VALUE_BETA_456",
            nested={"refresh_token": "TOKEN_VALUE_GAMMA_789", "note": "Bearer TOKEN_VALUE_DELTA_012"},
        )
        raw = server.ACTIVITY_LOG_PATH.read_text()
        for secret in ("TOKEN_VALUE_BETA_456", "TOKEN_VALUE_GAMMA_789", "TOKEN_VALUE_DELTA_012"):
            self.assertNotIn(secret, raw)

        server.ACTIVITY_LOG_PATH.write_text(json.dumps({
            "timestamp": 1,
            "event": "legacy",
            "message": "Authorization: Bearer TOKEN_VALUE_EPSILON_345",
            "access_token": "TOKEN_VALUE_ZETA_678",
        }) + "\n")
        exposed = json.dumps(server.read_activity_log())
        self.assertNotIn("TOKEN_..._345", exposed)
        self.assertNotIn("TOKEN_VALUE_ZETA_678", exposed)

    def test_malformed_history_is_quarantined_before_replacement(self) -> None:
        server.HISTORY_PATH.write_text("{broken")
        self.assertEqual(server.record_history([], int(time.time())), 0)
        quarantined = list(server.HISTORY_PATH.parent.glob("history.corrupt-*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(), "{broken")
        self.assertEqual(server.load_history(), [])

    def test_future_history_cannot_suppress_current_sample(self) -> None:
        now = int(time.time())
        server.HISTORY_PATH.write_text(json.dumps([
            {"timestamp": now - 400, "provider": "codex", "weeklyUsedPercent": 20},
            {"timestamp": now + 120, "provider": "claude", "weeklyUsedPercent": 88},
            {"timestamp": now + 86400, "provider": "codex", "weeklyUsedPercent": 99},
        ]))
        self.assertEqual(server.record_history(self.payload()[:1], now), 1)
        retained = server.load_history(now)
        self.assertEqual([item["timestamp"] for item in retained], [now - 400, now])
        self.assertEqual(retained[-1]["weeklyUsedPercent"], 35.0)

    def test_http_failure_still_prunes_expired_history(self) -> None:
        now = int(time.time())
        server.HISTORY_PATH.write_text(json.dumps([
            {"timestamp": now - 8 * 86400, "provider": "codex", "weeklyUsedPercent": 10},
            {"timestamp": now - 100, "provider": "claude", "weeklyUsedPercent": 25},
        ]))
        original_proxy = server.proxy_get
        original_keepalive = server.claude_auth_keepalive
        try:
            setattr(server, "claude_auth_keepalive", lambda: True)
            setattr(server, "proxy_get", lambda _path, timeout=None: (503, b"", "text/plain"))
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                server.collect_background_sample(now)
        finally:
            setattr(server, "proxy_get", original_proxy)
            setattr(server, "claude_auth_keepalive", original_keepalive)

        self.assertEqual(server.load_history(), [
            {"timestamp": now - 100, "provider": "claude", "weeklyUsedPercent": 25.0}
        ])

    def test_malformed_usage_response_still_prunes_expired_history(self) -> None:
        now = int(time.time())
        server.HISTORY_PATH.write_text(json.dumps([
            {"timestamp": now - 8 * 86400, "provider": "codex", "weeklyUsedPercent": 10},
            {"timestamp": now - 100, "provider": "claude", "weeklyUsedPercent": 25},
        ]))
        original_proxy = server.proxy_get
        original_keepalive = server.claude_auth_keepalive
        try:
            setattr(server, "claude_auth_keepalive", lambda: True)
            setattr(server, "proxy_get", lambda _path, timeout=None: (200, b"{", "application/json"))
            with self.assertRaises(json.JSONDecodeError):
                server.collect_background_sample(now)
        finally:
            setattr(server, "proxy_get", original_proxy)
            setattr(server, "claude_auth_keepalive", original_keepalive)

        self.assertEqual(server.load_history(), [
            {"timestamp": now - 100, "provider": "claude", "weeklyUsedPercent": 25.0}
        ])

    def test_partial_provider_failure_is_visible_and_redacted(self) -> None:
        now = int(time.time())
        payload = [
            {"provider": "codex", "usage": {"secondary": {"usedPercent": 35}}},
            {
                "provider": "claude",
                "error": {"message": "authorization=secret https://example.test/callback?code=secret"},
            },
        ]
        original_proxy = server.proxy_get
        original_keepalive = server.claude_auth_keepalive
        try:
            setattr(server, "claude_auth_keepalive", lambda: False)
            setattr(
                server,
                "proxy_get",
                lambda _path, timeout=None: (200, json.dumps(payload).encode(), "application/json"),
            )
            self.assertEqual(server.collect_background_sample(now), 1)
        finally:
            setattr(server, "proxy_get", original_proxy)
            setattr(server, "claude_auth_keepalive", original_keepalive)

        status = server.BACKGROUND_STATUS
        self.assertTrue(status["providerStatus"]["codex"]["ok"])
        self.assertFalse(status["providerStatus"]["claude"]["ok"])
        self.assertIn("Partial sample", status["lastError"])
        diagnostics = server.ACTIVITY_LOG_PATH.read_text()
        self.assertNotIn("secret", diagnostics)
        self.assertNotIn("http", diagnostics)

    def test_usage_proxy_calls_are_serialized(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()

        class UsageHandler(server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.1)
                    body = b"[]"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with guard:
                        active -= 1

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), UsageHandler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        original_url = server.CODEXBAR_URL
        statuses: list[int] = []
        start = threading.Barrier(3)

        def request_usage() -> None:
            start.wait()
            statuses.append(server.proxy_get("/usage?provider=both")[0])

        try:
            server_thread.start()
            setattr(server, "CODEXBAR_URL", f"http://127.0.0.1:{httpd.server_port}")
            threads = [threading.Thread(target=request_usage) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()
        finally:
            setattr(server, "CODEXBAR_URL", original_url)
            httpd.shutdown()
            httpd.server_close()
            server_thread.join(timeout=1)

        self.assertEqual(statuses, [200, 200])
        self.assertEqual(peak, 1)

    def test_usage_lock_wait_consumes_request_timeout(self) -> None:
        server.USAGE_FETCH_LOCK.acquire()
        try:
            started = time.monotonic()
            status, body, _ = server.proxy_get("/usage?provider=both", timeout=0.05)
            elapsed = time.monotonic() - started
        finally:
            server.USAGE_FETCH_LOCK.release()
        self.assertEqual(status, 503)
        self.assertIn(b"busy", body)
        self.assertLess(elapsed, 0.15)

    def test_proxy_deadline_stops_drip_response(self) -> None:
        class DripHandler(server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b"0123456789"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        original_url = server.CODEXBAR_URL
        try:
            thread.start()
            setattr(server, "CODEXBAR_URL", f"http://127.0.0.1:{httpd.server_port}")
            started = time.monotonic()
            status, body, _ = server.proxy_get("/drip", timeout=0.1)
            elapsed = time.monotonic() - started
        finally:
            setattr(server, "CODEXBAR_URL", original_url)
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1)

        self.assertEqual(status, 504)
        self.assertIn(b"deadline", body)
        self.assertLess(elapsed, 0.2)

    def test_proxy_deadline_stops_drip_error_response(self) -> None:
        class DripErrorHandler(server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b"rate-limited"
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.15)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), DripErrorHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        original_url = server.CODEXBAR_URL
        try:
            thread.start()
            setattr(server, "CODEXBAR_URL", f"http://127.0.0.1:{httpd.server_port}")
            started = time.monotonic()
            status, body, _ = server.proxy_get("/drip-error", timeout=0.2)
            elapsed = time.monotonic() - started
        finally:
            setattr(server, "CODEXBAR_URL", original_url)
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1)

        self.assertEqual(status, 504)
        self.assertIn(b"deadline", body)
        self.assertLess(elapsed, 0.28)

    def test_proxy_deadline_stops_drip_response_headers(self) -> None:
        class DripHeaderHandler(server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
                try:
                    for byte in response:
                        self.connection.sendall(bytes([byte]))
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), DripHeaderHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        original_url = server.CODEXBAR_URL
        try:
            thread.start()
            setattr(server, "CODEXBAR_URL", f"http://127.0.0.1:{httpd.server_port}")
            started = time.monotonic()
            status, body, _ = server.proxy_get("/drip-headers", timeout=0.1)
            elapsed = time.monotonic() - started
        finally:
            setattr(server, "CODEXBAR_URL", original_url)
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1)

        self.assertEqual(status, 504)
        self.assertIn(b"deadline", body)
        self.assertLess(elapsed, 0.25)

    def test_cancel_during_login_setup_prevents_process_start(self) -> None:
        original_ensure_auth_dirs = server.ensure_auth_dirs
        original_popen = server.subprocess.Popen
        entered_setup = threading.Event()
        release_setup = threading.Event()
        process_started = threading.Event()

        class FakeProcess:
            returncode = 0

            @staticmethod
            def poll():
                return 0

        def blocked_setup() -> None:
            entered_setup.set()
            self.assertTrue(release_setup.wait(timeout=2))

        def fake_popen(*_args, **_kwargs):
            process_started.set()
            return FakeProcess()

        try:
            setattr(server, "ensure_auth_dirs", blocked_setup)
            server.subprocess.Popen = fake_popen
            session = server.LoginSession("codex", "127.0.0.1")
            self.assertTrue(entered_setup.wait(timeout=1))
            session.cancel()
            release_setup.set()
            session.thread.join(timeout=2)
        finally:
            release_setup.set()
            setattr(server, "ensure_auth_dirs", original_ensure_auth_dirs)
            server.subprocess.Popen = original_popen

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertFalse(process_started.is_set())
        self.assertIsNone(session.process)

    def test_cancelled_claude_login_does_not_start_after_lock_wait(self) -> None:
        server.CLAUDE_CLI_LOCK.acquire()
        try:
            session = server.LoginSession("claude", "127.0.0.1")
            time.sleep(0.05)
            session.cancel()
        finally:
            server.CLAUDE_CLI_LOCK.release()
        session.thread.join(timeout=2)
        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertIsNone(session.process)

    def test_startup_preserves_valid_user_provider_settings(self) -> None:
        configured = server.default_config()
        configured["providers"][0]["enabled"] = False
        server.write_config(configured)
        before = server.CONFIG_PATH.read_bytes()

        server.ensure_basic_config()

        self.assertEqual(server.CONFIG_PATH.read_bytes(), before)
        self.assertEqual(server.read_config(), configured)

    def test_startup_backs_up_empty_config_before_recovery(self) -> None:
        server.CONFIG_PATH.write_bytes(b"")

        server.ensure_basic_config()

        backups = list(server.CONFIG_PATH.parent.glob("config.invalid-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"")
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.read_config(), server.default_config())

    def test_startup_backs_up_invalid_config_before_recovery(self) -> None:
        malformed = b'{"providers": [broken'
        server.CONFIG_PATH.write_bytes(malformed)

        server.ensure_basic_config()

        backups = list(server.CONFIG_PATH.parent.glob("config.invalid-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.read_config(), server.default_config())

    def test_startup_backs_up_non_utf8_config_before_recovery(self) -> None:
        malformed = b'{"providers": ["\xff"]}'
        server.CONFIG_PATH.write_bytes(malformed)

        server.ensure_basic_config()

        backups = list(server.CONFIG_PATH.parent.glob("config.invalid-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), malformed)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.read_config(), server.default_config())

    def test_credential_home_migration_preserves_unexpected_symlinks(self) -> None:
        root = pathlib.Path(self.temp.name) / "root"
        persistent = pathlib.Path(self.temp.name) / "config"
        root.mkdir()
        (persistent / ".codex").mkdir(parents=True)
        (persistent / ".claude").mkdir(parents=True)
        (root / ".codex").symlink_to(persistent / ".codex")
        unexpected_target = pathlib.Path(self.temp.name) / "missing-claude-home"
        (root / ".claude").symlink_to(unexpected_target)

        run_script = (ADDON_DIR / "rootfs/etc/services.d/codexbar/run").read_text()
        start = run_script.index('root_base="')
        end = run_script.index("\ndone", start) + len("\ndone")
        snippet = run_script[start:end]
        command = "function bashio::log.warning(){ :; }; function bashio::log.fatal(){ :; }; " + snippet
        environment = {
            **os.environ,
            "CODEXBAR_ROOT_HOME": str(root),
            "CODEXBAR_PERSISTENT_HOME": str(persistent),
        }
        subprocess.run(["bash", "-c", command], check=True, env=environment)
        self.assertEqual(os.readlink(root / ".codex"), str(persistent / ".codex"))
        self.assertFalse((root / ".codex.pre-codexbar").exists())
        self.assertEqual(os.readlink(root / ".claude"), str(persistent / ".claude"))
        self.assertTrue((root / ".claude.pre-codexbar").is_symlink())
        self.assertEqual(os.readlink(root / ".claude.pre-codexbar"), str(unexpected_target))
        subprocess.run(["bash", "-c", command], check=True, env=environment)

    def test_healthcheck_budget_exceeds_backend_probe(self) -> None:
        server_source = SERVER_PATH.read_text()
        dockerfile = (ADDON_DIR / "Dockerfile").read_text()
        self.assertIn('proxy_get("/health" + query, timeout=2)', server_source)
        self.assertIn("--timeout=5s", dockerfile)

    def test_claude_default_is_explicit_oauth(self) -> None:
        config = server.default_config()
        claude = next(item for item in config["providers"] if item["id"] == "claude")
        preset = next(item for item in server.PROVIDER_PRESETS if item["id"] == "claude")
        self.assertEqual(claude["source"], "oauth")
        self.assertEqual(preset["defaultSource"], "oauth")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend behavior tests")
    def test_frontend_toggle_and_startup_scheduling(self) -> None:
        frontend = (ADDON_DIR / "rootfs/usr/share/codexbar-addon/index.html").read_text()
        script = frontend.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = f"""
const vm=require('vm');
const realSetTimeout=setTimeout;
const source={json.dumps(script)};
class ClassList{{constructor(){{this.values=new Set()}} add(v){{this.values.add(v)}} remove(v){{this.values.delete(v)}} toggle(v,force){{if(force===undefined)force=!this.values.has(v);force?this.values.add(v):this.values.delete(v);return force}} contains(v){{return this.values.has(v)}}}}
class Element{{constructor(id){{this.id=id;this.classList=new ClassList();this.attributes={{}};this.textContent='';this.innerHTML='';this.className='';this.disabled=false;this.value='';this.clientWidth=900;this.parentElement={{clientWidth:900}}}}addEventListener(){{}}setAttribute(k,v){{this.attributes[k]=String(v)}}getAttribute(k){{return this.attributes[k]}}}}
let resizeCallback=null;
class ResizeObserver{{constructor(callback){{resizeCallback=callback}}observe(){{}}}}
const elements={{}};
const calls=[];
const timerDelays=[];
const now=Math.floor(Date.now()/1000);
const responses={{
  'api/auth-status':{{codex:{{ok:true}},claude:{{ok:true}}}},
  'api/background-status':{{running:true,intervalSeconds:90,lastSuccess:now,sampleCount:3,providerStatus:{{}}}},
  'health':{{ok:true,backendStatus:200,version:'test'}},
  'usage?provider=both':[],
  'cost?provider=both':[],
  'api/history':{{intervalSeconds:90,samples:[
    {{timestamp:now-2*86400,provider:'codex',weeklyUsedPercent:10}},
    {{timestamp:now-12*3600,provider:'codex',weeklyUsedPercent:20}},
    {{timestamp:now-6*3600,provider:'claude',weeklyUsedPercent:30}}
  ]}}
}};
const context={{
  console,URL,Date,Promise,JSON,Math,Object,Array,Number,String,Error,ResizeObserver,
  document:{{baseURI:'http://example.test/ingress/',getElementById:id=>elements[id]||(elements[id]=new Element(id))}},
  setTimeout:(fn,delay)=>{{timerDelays.push(delay);return timerDelays.length}},clearTimeout:()=>{{}},
  setInterval:()=>1,clearInterval:()=>{{}},
  fetch:async url=>{{
    const key=Object.keys(responses).find(item=>String(url).endsWith(item));
    if(!key)throw new Error('unexpected URL '+url);
    calls.push(key);
    if(key==='api/background-status')await new Promise(resolve=>realSetTimeout(resolve,20));
    return {{ok:true,status:200,statusText:'OK',text:async()=>JSON.stringify(responses[key])}};
  }}
}};
vm.createContext(context);
vm.runInContext(source,context);
realSetTimeout(()=>{{
  try{{
    const historyCalls=calls.filter(item=>item==='api/history').length;
    if(historyCalls!==1)throw new Error('expected one initial history request, got '+historyCalls);
    if(calls.some(item=>item==='cost?provider=both'))throw new Error('cost endpoint should not be requested');
    if(!timerDelays.includes(90000)||timerDelays.includes(300000))throw new Error('wrong initial timer '+JSON.stringify(timerDelays));
    elements.trendChart.parentElement.clientWidth=320;
    if(!resizeCallback)throw new Error('ResizeObserver callback was not registered');
    resizeCallback([{{contentRect:{{width:320}}}}]);
    if(elements.trendChart.getAttribute('viewBox')!=='0 0 320 240')throw new Error('mobile chart did not resize: '+elements.trendChart.getAttribute('viewBox'));
    vm.runInContext('setTrendRange(1)',context);
    if(elements.trendHeading.textContent!=='1-day usage trend')throw new Error('1-day heading not updated');
    if(elements.showTrend1d.getAttribute('aria-pressed')!=='true')throw new Error('1-day aria state not selected');
    if(!elements.trendNote.textContent.includes('2 visible points'))throw new Error('1-day filtering failed: '+elements.trendNote.textContent);
    if(!elements.trendNote.textContent.includes('90 seconds'))throw new Error('second interval wording failed');
    vm.runInContext('setTrendRange(7)',context);
    if(elements.trendHeading.textContent!=='7-day usage trend')throw new Error('7-day heading not updated');
    if(!elements.trendNote.textContent.includes('3 visible points'))throw new Error('7-day filtering failed: '+elements.trendNote.textContent);
    if(!elements.backgroundStatus.textContent.includes('90-second interval'))throw new Error('compound interval wording failed');
    console.log('frontend toggle and startup scheduling passed');
  }}catch(error){{console.error(error.stack||error);process.exitCode=1}}
}},80);
"""
        result = subprocess.run(["node", "-e", harness], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frontend toggle and startup scheduling passed", result.stdout)

    def test_frontend_uses_completion_scheduled_refresh(self) -> None:
        frontend = (ADDON_DIR / "rootfs/usr/share/codexbar-addon/index.html").read_text()
        self.assertNotIn("setInterval(refreshUsage", frontend)
        self.assertIn("if(refreshPromise)return refreshPromise", frontend)
        self.assertIn("backgroundDetail", frontend)
        self.assertNotIn("=>label(provider)", frontend)
        self.assertIn("setTrendRange(1)", frontend)
        self.assertIn("setTrendRange(7)", frontend)
        self.assertIn("rangeDays===1?6:7", frontend)
        self.assertNotIn("refreshUsage(),loadHistory()", frontend)
        self.assertNotIn('id="cost"', frontend)
        self.assertNotIn("api('cost?provider=both')", frontend)
        self.assertNotIn("min-width:620px", frontend)
        self.assertNotIn("overflow-x:auto", frontend)
        self.assertIn("ResizeObserver", frontend)


if __name__ == "__main__":
    unittest.main()
