#!/usr/bin/env python3
"""Regressions for the optional webhook alerts: local receiver, no real services."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

try:  # controller.py imports checkin.py, which needs flock() at runtime only.
    import fcntl  # noqa: F401
except ImportError:  # Windows dev box
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import notify  # noqa: E402

DAY = 86400


class Receiver:
    """Minimal HTTP endpoint that records alert payloads."""

    def __init__(self, status: int = 200):
        self.payloads: list[dict] = []
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                receiver.payloads.append(json.loads(self.rfile.read(size) or b"{}"))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class StubController:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def status_with(accounts):
    return {"accounts": accounts, "updatedAt": 0}


def account(name, *, checkin_status="success", days=None, remaining=1000, label=None):
    expire_at = None if days is None else int(time.time() + days * DAY)
    return {
        "file": name, "label": label or name, "expireAt": expire_at,
        "quota": {"remaining": remaining, "known": True},
        "checkin": {"date": "2026-11-23", "status": checkin_status, "attempts": 2, "message": "网关返回 429"},
    }


class AlertContentTests(unittest.TestCase):
    def test_failed_checkin_raises_one_alert(self):
        status = status_with([account("workbuddy.json", checkin_status="failed")])
        alerts = notify.pending_alerts(status, {})
        self.assertEqual(len(alerts), 1)
        key, text = alerts[0]
        self.assertIn("今日签到失败", text)
        self.assertIn("2 次尝试", text)
        self.assertEqual(key, "checkin-failed:workbuddy.json:2026-11-23")

    def test_successful_checkin_is_silent(self):
        self.assertEqual(notify.pending_alerts(status_with([account("a.json")]), {}), [])

    def test_expiry_alert_covers_three_days_only(self):
        soon = notify.pending_alerts(status_with([account("a.json", days=2)]), {})
        later = notify.pending_alerts(status_with([account("b.json", days=10)]), {})
        self.assertEqual(len(soon), 1)
        self.assertIn("到期", soon[0][1])
        self.assertEqual(later, [])

    def test_zero_balance_is_not_an_expiry_alert(self):
        self.assertEqual(notify.pending_alerts(status_with([account("a.json", days=1, remaining=0)]), {}), [])

    def test_already_notified_keys_are_skipped(self):
        status = status_with([account("a.json", checkin_status="failed"), account("b.json", days=1)])
        first = dict(notify.pending_alerts(status, {}))
        self.assertEqual(len(first), 2)
        notified = {key: int(time.time()) for key in first}
        self.assertEqual(notify.pending_alerts(status, notified), [])

    def test_payload_shape_per_receiver(self):
        self.assertEqual(notify.format_message("telegram", "hi")["text"], "hi")
        self.assertEqual(notify.format_message("dingtalk", "hi")["msgtype"], "text")
        self.assertEqual(notify.format_message("feishu", "hi")["msg_type"], "text")
        self.assertEqual(notify.format_message("generic", "hi")["text"], "hi")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.state = Path(work.name) / notify.STATE_FILE
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)

    def test_alerts_are_delivered_once(self):
        controller = StubController(status_with([account("a.json", checkin_status="failed")]))
        sent = notify.notify_once(controller, self.state, self.receiver.url, timeout=3)
        self.assertEqual(sent, ["checkin-failed:a.json:2026-11-23"])
        time.sleep(0.1)
        self.assertEqual(len(self.receiver.payloads), 1)
        self.assertIn("签到失败", json.dumps(self.receiver.payloads[0], ensure_ascii=False))
        self.assertEqual(notify.notify_once(controller, self.state, self.receiver.url, timeout=3), [])
        time.sleep(0.1)
        self.assertEqual(len(self.receiver.payloads), 1, "重复提醒应被去重")

    def test_receiver_error_is_not_marked_as_sent(self):
        broken = Receiver(status=500)
        self.addCleanup(broken.close)
        controller = StubController(status_with([account("a.json", checkin_status="failed")]))
        with self.assertRaises(ValueError):
            notify.notify_once(controller, self.state, broken.url, timeout=3)
        self.assertEqual(notify.load_state(self.state), {})
        with self.assertRaises(OSError):  # 连不上的接收端也不能写进去重状态
            notify.notify_once(controller, self.state, "http://127.0.0.1:9/hook", timeout=2)
        self.assertEqual(notify.load_state(self.state), {})

    def test_state_file_is_private_on_posix(self):
        if os.name == "nt":
            self.skipTest("Windows 不保留 POSIX 权限位")
        notify.save_state(self.state, {"k": int(time.time())})
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o600")

    def test_stale_state_keys_are_pruned(self):
        old = int(time.time()) - notify.STATE_TTL - 10
        notify.save_state(self.state, {"old": old, "new": int(time.time())})
        self.assertEqual(list(notify.load_state(self.state)), ["new"])

    def test_loop_sends_then_stops(self):
        controller = StubController(status_with([account("a.json", days=1)]))
        stop = threading.Event()
        thread = threading.Thread(target=notify.run, args=(controller, self.state, self.receiver.url, "generic", 60),
                                  kwargs={"stop": stop}, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not self.receiver.payloads and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        thread.join(2)
        self.assertEqual(len(self.receiver.payloads), 1)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
