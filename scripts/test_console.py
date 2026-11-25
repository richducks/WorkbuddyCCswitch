#!/usr/bin/env python3
"""Offline regressions for the console. No real credentials or gateway."""
import importlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

boot = importlib.import_module("boot")
ctrl = importlib.import_module("controller")


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.web = self.root / "web"
        self.web.mkdir()
        for name, value in (("ROOT", self.root), ("WEB_DATA", self.web)):
            stub = patch.object(ctrl, name, value)
            stub.start()
            self.addCleanup(stub.stop)
        (self.root / "workbuddy-status.json").write_text('{"accounts": [], "updatedAt": 0}')
        self.console = ctrl.Controller(SimpleNamespace(version="test", alive=lambda: True))

    def test_checkin_only_marker_precedes_login_and_promotion_preserves_credential(self):
        name = "workbuddy2.json"
        credential = self.root / name
        marker = self.root / (name + ".checkin-only")

        def login(*args, **kwargs):
            self.assertTrue(marker.is_file())
            credential.write_text("fixture")
            return Mock(stdout=io.StringIO(), poll=lambda: 0)

        def run_pump(*, target, args, daemon):
            return SimpleNamespace(start=lambda: target(*args))

        with patch.object(ctrl.subprocess, "Popen", side_effect=login), patch.object(
            ctrl.threading, "Thread", side_effect=run_pump
        ):
            self.assertEqual(self.console.add_account(checkin_only=True)["file"], name)
        with self.assertRaisesRegex(ValueError, "仅参与自动签到"):
            self.console.manual_checkin(name)
        with patch.object(self.console, "hermes", return_value={}), patch.object(
            self.console, "model_catalog", return_value=[]
        ):
            self.assertTrue(self.console.status()["accounts"][0]["checkinOnly"])
            self.console.set_account_pool(name, checkin_only=False)
            self.assertFalse(self.console.status()["accounts"][0]["checkinOnly"])
            self.console.set_account_pool(name, checkin_only=True)
            self.assertTrue(self.console.status()["accounts"][0]["checkinOnly"])
            self.assertEqual(credential.read_text(), "fixture")
            self.console.set_account_pool(name, checkin_only=False)
            self.assertFalse(self.console.status()["accounts"][0]["checkinOnly"])
        self.assertEqual(credential.read_text(), "fixture")
        self.assertFalse(marker.exists())

    def test_permissions_mode_never_falls_back_to_legacy(self):
        with patch.dict(ctrl.os.environ, {"WB_CONSOLE_PERMISSIONS": "1", "WB_CONSOLE_NOAUTH": "1"}):
            with patch.object(boot, "build_permissions_server", side_effect=RuntimeError("bootstrap failed")):
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    boot.console_server(self.console, "fixture-token")

    def test_tokenless_console_requires_loopback_binding_and_host(self):
        import web
        from email.message import Message

        handler = object.__new__(web.Handler)
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:18080"
        handler._auth = lambda: False
        handler._cookie_auth = lambda: False
        with patch.object(web, "NOAUTH", True), patch.object(web, "HOST_BIND_IP", "127.0.0.1"):
            self.assertTrue(handler._authed())
            handler.headers.replace_header("Host", "external.example:18080")
            self.assertFalse(handler._authed())
        handler.headers.replace_header("Host", "127.0.0.1:18080")
        with patch.object(web, "NOAUTH", True), patch.object(web, "HOST_BIND_IP", "192.168.1.2"):
            self.assertFalse(handler._authed())
        with patch.object(web, "NOAUTH", False), patch.object(web, "HOST_BIND_IP", "127.0.0.1"):
            self.assertFalse(handler._authed())

    def test_catalog_serves_cache_without_starting_a_second_refresh(self):
        started = threading.Event()
        release = threading.Event()

        def fetch():
            started.set()
            release.wait(3)
            return [{"id": "next"}]

        result = []
        with patch.object(self.console, "_fetch_catalog", side_effect=fetch) as loader:
            first = threading.Thread(target=self.console.model_catalog, daemon=True)
            second = threading.Thread(target=lambda: result.append(self.console.model_catalog()), daemon=True)
            first.start()
            try:
                self.assertTrue(started.wait(2))
                second.start()
                second.join(1)
                self.assertFalse(second.is_alive(), "a second reader must not wait for the gateway")
                self.assertEqual(result, [[]])
                self.assertEqual(loader.call_count, 1)
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
            self.assertEqual(self.console.model_catalog(), [{"id": "next"}])

    def test_malformed_model_catalog_returns_empty(self):
        with patch.object(self.console, "api_key", return_value="fixture"):
            for payload in ([1], {"data": 42}):
                with self.subTest(payload=payload), patch.object(
                    ctrl.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
                ):
                    self.assertEqual(self.console._fetch_catalog(), [])

    def test_catalog_skips_bad_rows_and_capabilities(self):
        payload = {"data": [None, {"id": "ok", "capabilities_by_site": {"cn": True}, "context_length": 8192}]}
        with patch.object(self.console, "api_key", return_value="fixture"), patch.object(
            ctrl.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
        ):
            self.assertEqual(self.console.model_catalog(), [{"id": "ok", "context_length": 8192}])

    def test_supervisor_backs_off_when_new_children_exit_immediately(self):
        gw = ctrl.Gateway.__new__(ctrl.Gateway)
        crashed = SimpleNamespace(poll=lambda: 1, returncode=1)
        gw.proc, gw._log, gw._lock, gw._shutdown = crashed, None, threading.Lock(), False
        delays = []

        def spawn():
            gw.proc = crashed
            gw._shutdown = len(delays) >= 4

        gw._spawn = spawn
        with patch.object(ctrl.time, "sleep", side_effect=delays.append):
            gw.supervise(poll_seconds=5)
        self.assertEqual(delays, [5, 10, 20, 30])

    def test_trim_replaces_large_log_without_touching_stale_temp_link(self):
        logs = self.root / "logs"
        logs.mkdir()
        log = logs / "large.log"
        log.write_bytes(b"X" * 24 + b"retained")
        other = self.root / "unrelated.txt"
        other.write_bytes(b"leave unchanged")
        (logs / "large.log.trim").symlink_to(other)
        boot.trim_logs(self.root, keep_bytes=8)
        self.assertEqual(other.read_bytes(), b"leave unchanged")
        self.assertEqual(log.read_bytes(), b"retained")

    def test_existing_login_must_exit_before_another_can_start(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(ctrl.subprocess, "Popen", return_value=proc) as spawn, patch.object(ctrl.threading, "Thread"):
            sid = self.console.add_account()["sessionId"]
            with self.assertRaises(ValueError):
                self.console.add_account()
            self.console.cancel_session(sid)
            with self.assertRaises(ValueError):
                self.console.add_account()
            self.assertEqual(spawn.call_count, 1)
            proc.terminate.assert_called_once()

    def test_completed_login_leaves_no_qr_log(self):
        proc = Mock(stdout=io.StringIO("fixture login output\n"), returncode=0)
        proc.poll.return_value = 0
        with patch.object(ctrl.subprocess, "Popen", return_value=proc), patch.object(ctrl.threading, "Thread"):
            sid = self.console.add_account()["sessionId"]
        self.console._pump(self.console.sessions[sid])
        self.assertEqual(self.console.get_session(sid)["status"], "done")
        self.assertEqual(self.console.get_session(sid)["output"], "")
        self.assertEqual(list(self.web.glob("login-*.log")), [])

    def test_binding_rejects_wildcard_and_public_addresses(self):
        for addr in ("0.0.0.0", "8.8.8.8", "::"):
            with self.subTest(addr=addr), self.assertRaises(ValueError):
                ctrl.bind_ip(addr)
        self.assertEqual(ctrl.bind_ip("127.0.0.1"), "127.0.0.1")
        self.assertEqual(ctrl.bind_ip("192.168.50.10"), "192.168.50.10")

    def test_configured_host_drives_the_two_client_endpoints(self):
        with patch.object(ctrl, "HOST_BIND_IP", "192.168.50.10", create=True), patch.object(ctrl, "HOST_GW_PORT", 18317), patch.object(
            ctrl, "HOST_BRIDGE_PORT", 18318
        ):
            status = self.console.status()
        self.assertEqual(status["hermes"]["endpoint"], "http://192.168.50.10:18317/v1")
        self.assertEqual(status.get("bridgeUrl"), "http://192.168.50.10:18318")
        self.assertTrue(status["hermes"]["refreshCommand"].endswith("--gateway-host 192.168.50.10 --gateway-port 18317"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
