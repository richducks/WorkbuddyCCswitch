"""Small authenticated HTTP API + static page for the config console."""
from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import (CSRF_COOKIE, LOGIN_FAILURE_DELAY, SESSION_COOKIE,
                  SessionStore, clear_cookie_header, cookie_header, parse_cookies)
from controller import Controller

STATIC = Path(__file__).resolve().parent / "static"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
LOGIN_ERROR = "密码不正确，请重试。"


def _check_origin(handler: "Handler") -> bool:
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host")
    if not origin or not host:
        return True
    parsed = urlparse(origin)
    return parsed.netloc == host and parsed.scheme in ("http", "https")


class Handler(BaseHTTPRequestHandler):
    server_version = "WBConsole/1.1"

    # ---------- auth helpers ----------
    def _cookies(self) -> dict[str, str]:
        return parse_cookies(self.headers.get("Cookie", ""))

    def _basic(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = raw.partition(":")
        except Exception:
            return False
        return user == "admin" and hmac.compare_digest(password, cast(ConsoleServer, self.server).token)

    def _session(self) -> tuple[str, dict] | None:
        sid = self._cookies().get(SESSION_COOKIE, "")
        if not sid:
            return None
        row = cast(ConsoleServer, self.server).sessions.lookup(sid)
        return (sid, row) if row else None

    def _mode(self) -> str:
        """Return 'session', 'basic' or '' for the current request."""
        if self._session() is not None:
            return "session"
        return "basic" if self._basic() else ""

    # ---------- responses ----------
    def _send(self, code: int, body: Any, content_type: str = "application/json; charset=utf-8",
              headers: dict[str, str] | None = None, cookies: list[str] | None = None) -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = b""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for value in cookies or []:
            self.send_header("Set-Cookie", value)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self._send(303, "", "text/plain; charset=utf-8", {"Location": location}, cookies=cookies)

    def _login_page(self, code: int, error: str = "") -> None:
        page = (STATIC / "login.html").read_text(encoding="utf-8")
        note = "" if not error else f'<p class="err" role="alert">{error}</p>'
        self._send(code, page.replace("<!--ERROR-->", note), "text/html; charset=utf-8")

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > 1_000_000:
            raise ValueError("请求体为空或过大")
        data = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _drain(self, limit: int = 1_000_000) -> None:
        """Discard an unread request body so the client can read our error reply.

        Without this, a rejected write (401/403/415) closes the connection with
        the body still in flight and some clients see a reset instead of the
        response.
        """
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        remaining = min(max(size, 0), limit)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    def _guard_read(self) -> str:
        """Gate a read: returns the auth mode, or '' after answering the request."""
        mode = self._mode()
        if mode:
            if not _check_origin(self):
                self._send(403, {"error": "跨站请求被拒绝"})
                return ""
            return mode
        if urlparse(self.path).path.startswith("/api/"):
            self._drain()
            self._send(401, {"error": "需要登录"})
        else:
            self._redirect("/login", cookies=[clear_cookie_header(SESSION_COOKIE)])
        return ""

    def _guard_write(self) -> str:
        """Gate a write: session logins must also present their CSRF token."""
        mode = self._mode()
        if not mode:
            self._drain()
            self._send(401, {"error": "需要登录"})
            return ""
        if not _check_origin(self):
            self._drain()
            self._send(403, {"error": "跨站请求被拒绝"})
            return ""
        if mode == "session":
            sid, _row = self._session() or ("", {})
            if not cast(ConsoleServer, self.server).sessions.csrf_ok(sid, self.headers.get("X-CSRF-Token", "")):
                self._drain()
                self._send(403, {"error": "会话校验失败，请刷新页面后重试"})
                return ""
        return mode

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/login":
            if self._mode():
                return self._redirect("/")
            return self._login_page(401 if "error" in parse_qs(u.query) else 200)
        if u.path == "/logout":
            sid = self._cookies().get(SESSION_COOKIE, "")
            if sid:
                cast(ConsoleServer, self.server).sessions.drop(sid)
            return self._redirect("/login", cookies=[clear_cookie_header(SESSION_COOKIE)])
        if not self._guard_read():
            return
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/summary":
                return self._send(200, cast(ConsoleServer, self.server).controller.status())
            if u.path == "/api/session":
                sid = parse_qs(u.query).get("id", [""])[0]
                return self._send(200, cast(ConsoleServer, self.server).controller.get_session(sid))
            if u.path == "/api/hermes/key":
                return self._send(200, {"apiKey": cast(ConsoleServer, self.server).controller.api_key()})
            if u.path == "/api/providers":
                registry = cast(ConsoleServer, self.server).controller.provider_registry
                return self._send(200, {"providers": registry.list()})
            if u.path == "/providers.js":
                return self._send(200, (STATIC / "providers.js").read_text(encoding="utf-8"), "text/javascript; charset=utf-8")
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            return self._login()
        if not self._guard_write():
            return
        try:
            if u.path.startswith("/api/providers/") and self.headers.get_content_type() != "application/json":
                self._drain()
                return self._send(415, {"error": "提供商配置须以 JSON 提交"})
            data = self._body()
            c = cast(ConsoleServer, self.server).controller
            if u.path in ("/api/providers/add", "/api/providers/edit"):
                return self._send(200, {"provider": c.provider_registry.save(data, create=u.path.endswith("/add"))})
            if u.path == "/api/providers/remove":
                if not isinstance(data, dict) or data.keys() != {"id"}:
                    raise ValueError("请选择要移除的提供商")
                c.provider_registry.remove(data["id"])
                return self._send(200, {"ok": True})
            if u.path == "/api/accounts":
                return self._send(200, c.add_account())
            if u.path == "/api/session/cancel":
                c.cancel_session(str(data.get("sessionId", "")))
                return self._send(200, {"ok": True})
            if u.path == "/api/accounts/remove":
                c.remove_account(str(data.get("file", "")))
                return self._send(200, {"ok": True})
            if u.path == "/api/labels":
                c.set_label(str(data.get("file", "")), str(data.get("label", "")))
                return self._send(200, {"ok": True})
            if u.path == "/api/checkin/manual":
                name = data.get("file")
                if not isinstance(name, str):
                    raise ValueError("请选择账号")
                return self._send(200, {"result": c.manual_checkin(name)})
            if u.path == "/api/checkin/all":
                return self._send(200, {"results": c.checkin_all()})
            if u.path == "/api/checkin/settings":
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("自动签到开关必须为布尔值")
                return self._send(200, {"enabled": c.set_auto_checkin(enabled)})
            if u.path == "/api/checkin":
                files = data.get("files")
                if not isinstance(files, list) or not files:
                    raise ValueError("请选择账号")
                return self._send(200, {"results": c.checkin_all([str(x) for x in files])})
            if u.path == "/logout":
                sid = self._cookies().get(SESSION_COOKIE, "")
                if sid:
                    cast(ConsoleServer, self.server).sessions.drop(sid)
                return self._send(200, {"ok": True}, cookies=[clear_cookie_header(SESSION_COOKIE)])
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _login(self) -> None:
        """Password form: constant-time compare; every failure is delayed."""
        server = cast(ConsoleServer, self.server)
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > 4096:
            self._drain()
            return self._login_page(413, "请求无效。")
        try:
            form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
        except Exception:
            return self._login_page(400, "请求无效。")
        supplied = (form.get("token") or [""])[0].strip()
        if not supplied or not hmac.compare_digest(supplied, server.token):
            time.sleep(LOGIN_FAILURE_DELAY)
            return self._login_page(401, LOGIN_ERROR)
        sid, csrf = server.sessions.create()
        self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"}, cookies=[
            cookie_header(SESSION_COOKIE, sid, server.session_ttl, http_only=True),
            cookie_header(CSRF_COOKIE, csrf, server.session_ttl),
        ])

    def log_message(self, format, *args):
        pass


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    controller: Controller
    token: str
    sessions: SessionStore
    session_ttl: int


def build_web_server(controller: Controller, token: str, port: int | None = None) -> ConsoleServer:
    httpd = ConsoleServer(("0.0.0.0", port or int(os.environ.get("WEB_PORT", "8080"))), Handler)
    httpd.controller, httpd.token = controller, token
    httpd.session_ttl = int(float(os.environ.get("WEB_SESSION_HOURS", "12")) * 3600)
    httpd.sessions = SessionStore(ttl=httpd.session_ttl)
    return httpd
