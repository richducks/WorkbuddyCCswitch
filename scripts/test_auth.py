#!/usr/bin/env python3
"""Regressions for the console login gate (real HTTP, no gateway, no credentials).

Runs the actual ThreadingHTTPServer on 127.0.0.1 with a stub controller, so the
cookie/session/CSRF path is exercised end to end instead of being asserted from
the source text.
"""
import http.client
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from urllib.parse import urlencode

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

try:  # checkin.py locks itself with flock(); only needed at runtime, not here.
    import fcntl  # noqa: F401
except ImportError:  # Windows dev box: the auth layer is platform independent.
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import web  # noqa: E402
from auth import CSRF_COOKIE, SESSION_COOKIE, SessionStore  # noqa: E402

TOKEN = "test-token-" + "a" * 24


class FakeRegistry:
    def list(self):
        return [{"id": "relay", "name": "中转"}]


class FakeController:
    """Only the surface the console touches while gating requests."""

    def __init__(self):
        self.provider_registry = FakeRegistry()
        self.labels = {}

    def status(self):
        return {"accounts": [], "updatedAt": 0, "version": "test", "gatewayAlive": True}

    def get_session(self, sid):
        raise ValueError("登录会话不存在")

    def api_key(self):
        return "k" * 40

    def set_label(self, filename, label):
        self.labels[filename] = label

    def add_account(self):
        return {"sessionId": "s", "file": "workbuddy2.json"}


class ConsoleAuthTests(unittest.TestCase):
    ttl = 12 * 3600

    def setUp(self):
        self.controller = FakeController()
        self.httpd = web.build_web_server(self.controller, TOKEN, port=0)
        self.httpd.sessions = SessionStore(ttl=self.ttl)
        self.thread = __import__("threading").Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    # ---------- helpers ----------
    def request(self, method, path, *, body=None, headers=None, cookies=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = dict(headers or {})
        if cookies:
            merged["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw = None
        if body is not None:
            raw = urlencode(body) if isinstance(body, dict) else body
            merged.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=raw, headers=merged)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def login(self, token=TOKEN):
        resp, _body = self.request("POST", "/login", body={"token": token})
        self.assertEqual(resp.status, 303, "成功登录应返回 303 跳转")
        cookies = {}
        for value in resp.msg.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            cookies[name] = rest.split(";", 1)[0]
        self.assertIn(SESSION_COOKIE, cookies)
        self.assertIn(CSRF_COOKIE, cookies)
        return resp, cookies

    # ---------- tests ----------
    def test_unauthenticated_page_redirects_to_login(self):
        resp, _body = self.request("GET", "/")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.msg["Location"], "/login")

    def test_unauthenticated_api_is_401_json(self):
        resp, body = self.request("GET", "/api/summary")
        self.assertEqual(resp.status, 401)
        self.assertEqual(json.loads(body)["error"], "需要登录")

    def test_login_page_is_public_and_self_contained(self):
        resp, body = self.request("GET", "/login")
        self.assertEqual(resp.status, 200)
        text = body.decode()
        self.assertIn('action="/login"', text)
        self.assertIn('type="password"', text)
        self.assertNotIn("<script", text)

    def test_wrong_password_is_401_and_delayed(self):
        started = time.monotonic()
        resp, body = self.request("POST", "/login", body={"token": "nope"})
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status, 401)
        self.assertIn(web.LOGIN_ERROR, body.decode())
        self.assertGreaterEqual(elapsed, 0.35, "失败登录应引入固定延迟")

    def test_empty_password_is_rejected(self):
        resp, _body = self.request("POST", "/login", body={"token": ""})
        self.assertEqual(resp.status, 401)

    def test_oversized_login_body_is_rejected(self):
        resp, _body = self.request("POST", "/login", body=b"token=" + b"a" * 5000)
        self.assertEqual(resp.status, 413)

    def test_successful_login_sets_hardened_cookies(self):
        resp, cookies = self.login()
        raw = "; ".join(resp.msg.get_all("Set-Cookie") or [])
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Strict", raw)
        self.assertIn("Max-Age=43200", raw)
        self.assertIn("Path=/", raw)
        self.assertTrue(cookies[SESSION_COOKIE])

    def test_session_grants_page_and_api(self):
        _resp, cookies = self.login()
        resp, body = self.request("GET", "/", cookies=cookies)
        self.assertEqual(resp.status, 200)
        self.assertIn("Hermes", body.decode())
        resp, body = self.request("GET", "/api/summary", cookies=cookies)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["version"], "test")

    def test_write_without_csrf_token_is_refused(self):
        _resp, cookies = self.login()
        resp, _body = self.request("POST", "/api/labels", body={"file": "workbuddy.json", "label": "x"},
                                   headers={"Content-Type": "application/json"}, cookies=cookies)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.controller.labels, {})

    def test_write_with_wrong_csrf_token_is_refused(self):
        _resp, cookies = self.login()
        resp, _body = self.request("POST", "/api/labels", body=json.dumps({"file": "workbuddy.json", "label": "x"}),
                                   headers={"Content-Type": "application/json", "X-CSRF-Token": "forged"},
                                   cookies=cookies)
        self.assertEqual(resp.status, 403)

    def test_write_with_csrf_token_is_allowed(self):
        _resp, cookies = self.login()
        resp, body = self.request("POST", "/api/labels", body=json.dumps({"file": "workbuddy.json", "label": "主号"}),
                                  headers={"Content-Type": "application/json", "X-CSRF-Token": cookies[CSRF_COOKIE]},
                                  cookies=cookies)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(self.controller.labels, {"workbuddy.json": "主号"})

    def test_cross_origin_write_is_refused(self):
        _resp, cookies = self.login()
        resp, _body = self.request("POST", "/api/labels", body=json.dumps({"file": "workbuddy.json", "label": "x"}),
                                   headers={"Content-Type": "application/json",
                                            "X-CSRF-Token": cookies[CSRF_COOKIE],
                                            "Origin": "http://evil.example"},
                                   cookies=cookies)
        self.assertEqual(resp.status, 403)

    def test_basic_auth_still_works_for_scripts(self):
        import base64
        creds = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
        resp, body = self.request("GET", "/api/summary", headers={"Authorization": "Basic " + creds})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["version"], "test")

    def test_basic_auth_writes_do_not_need_csrf(self):
        import base64
        creds = base64.b64encode(f"admin:{TOKEN}".encode()).decode()
        resp, _body = self.request("POST", "/api/labels", body=json.dumps({"file": "workbuddy.json", "label": "脚本"}),
                                   headers={"Content-Type": "application/json", "Authorization": "Basic " + creds})
        self.assertEqual(resp.status, 200)

    def test_basic_auth_rejects_wrong_password(self):
        import base64
        creds = base64.b64encode(b"admin:wrong").decode()
        resp, _body = self.request("GET", "/api/summary", headers={"Authorization": "Basic " + creds})
        self.assertEqual(resp.status, 401)

    def test_expired_session_is_refused(self):
        self.httpd.sessions = SessionStore(ttl=0.05)
        _resp, cookies = self.login()
        time.sleep(0.12)
        resp, _body = self.request("GET", "/api/summary", cookies=cookies)
        self.assertEqual(resp.status, 401)

    def test_logout_drops_the_session(self):
        _resp, cookies = self.login()
        resp, _body = self.request("GET", "/logout", cookies=cookies)
        self.assertEqual(resp.status, 303)
        resp, _body = self.request("GET", "/api/summary", cookies=cookies)
        self.assertEqual(resp.status, 401)

    def test_login_page_redirects_an_authenticated_browser(self):
        _resp, cookies = self.login()
        resp, _body = self.request("GET", "/login", cookies=cookies)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.msg["Location"], "/")

    def test_garbage_cookie_is_ignored(self):
        resp, _body = self.request("GET", "/api/summary", cookies={SESSION_COOKIE: "../../etc/passwd"})
        self.assertEqual(resp.status, 401)
        resp, _body = self.request("GET", "/", headers={"Cookie": "wb_session"})
        self.assertEqual(resp.status, 303)

    def test_security_headers_present(self):
        resp, _body = self.request("GET", "/login")
        self.assertEqual(resp.msg["X-Frame-Options"], "DENY")
        self.assertEqual(resp.msg["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.msg["Cache-Control"], "no-store")


class SessionStoreTests(unittest.TestCase):
    def test_oldest_session_is_evicted_past_the_cap(self):
        store = SessionStore(ttl=3600, max_sessions=3)
        sids = [store.create()[0] for _ in range(4)]
        self.assertEqual(store.count(), 3)
        self.assertIsNone(store.lookup(sids[0]))
        self.assertIsNotNone(store.lookup(sids[-1]))

    def test_csrf_token_is_bound_to_its_session(self):
        store = SessionStore()
        sid_a, csrf_a = store.create()
        _sid_b, csrf_b = store.create()
        self.assertTrue(store.csrf_ok(sid_a, csrf_a))
        self.assertFalse(store.csrf_ok(sid_a, csrf_b))
        self.assertFalse(store.csrf_ok(sid_a, ""))

    def test_expiry_follows_the_configured_ttl(self):
        store = SessionStore(ttl=0.05)
        sid, _csrf = store.create()
        self.assertIsNotNone(store.lookup(sid))
        time.sleep(0.12)
        self.assertIsNone(store.lookup(sid))
        self.assertEqual(store.count(), 0)

    def test_cookie_parsing_ignores_junk(self):
        from auth import parse_cookies
        self.assertEqual(parse_cookies("a=1; wb_session=xyz; b"), {"a": "1", "wb_session": "xyz"})
        self.assertEqual(parse_cookies(""), {})
        self.assertEqual(parse_cookies("=; ;"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
