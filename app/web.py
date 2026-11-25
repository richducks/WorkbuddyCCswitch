"""Small authenticated HTTP API + static page for the config console."""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import HOST_BIND_IP, Controller
from console_routes import dispatch_console_api

STATIC = Path(__file__).resolve().parent / "static"


def _check_origin(handler: "Handler") -> bool:
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host")
    if not origin or not host:
        return True
    parsed = urlparse(origin)
    return parsed.netloc == host and parsed.scheme in ("http", "https")


SESSION_COOKIE = "wb_session"
SESSION_TTL = 12 * 3600
# 仅对明确开启免令牌访问的本机控制台生效。
NOAUTH = os.environ.get("WB_CONSOLE_NOAUTH") == "1"


class Handler(BaseHTTPRequestHandler):
    server_version = "WBConsole/1.0"

    def _auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = raw.partition(":")
        except Exception:
            return False
        return user == "admin" and hmac.compare_digest(password, cast(ConsoleServer, self.server).token)

    def _cookie_auth(self) -> bool:
        sid = None
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                sid = v
                break
        if not sid:
            return False
        srv = cast(ConsoleServer, self.server)
        with srv.session_lock:
            exp = srv.sessions.get(sid)
            if exp is None:
                return False
            if exp < time.time():
                srv.sessions.pop(sid, None)
                return False
        return True

    def _local_noauth(self) -> bool:
        host = self.headers.get("Host", "").partition(":")[0].lower()
        return NOAUTH and HOST_BIND_IP == "127.0.0.1" and host in ("127.0.0.1", "localhost")

    def _authed(self) -> bool:
        if self._local_noauth():
            return True
        return self._auth() or self._cookie_auth()

    def _require(self) -> bool:
        if self._authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="WorkBuddy Console", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_login(self) -> None:
        if self._authed():
            return self._redirect("/")
        self._send(200, (STATIC / "login.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")

    def _handle_login(self) -> None:
        if not _check_origin(self):
            return self._send(403, {"error": "跨站请求被拒绝"})
        try:
            data = self._body()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        srv = cast(ConsoleServer, self.server)
        token = data.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, srv.token):
            time.sleep(0.4)  # 轻微延迟，增加在线爆破成本
            return self._send(401, {"error": "令牌错误"})
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with srv.session_lock:
            srv.sessions[sid] = now + SESSION_TTL
            for k in [k for k, v in srv.sessions.items() if v < now]:
                srv.sessions.pop(k, None)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, body: Any, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = b""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > 1_000_000:
            raise ValueError("请求体为空或过大")
        data = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/login":
            return self._serve_login()
        if not self._authed():
            # 浏览器直接访问（无 Basic 头）：跳登录页；curl/脚本带 Basic 头或访问 API 时维持 401
            if not self.headers.get("Authorization") and not u.path.startswith("/api/"):
                return self._redirect("/login")
            if not self._require():
                return
        if not _check_origin(self):
            return self._send(403, {"error": "跨站请求被拒绝"})
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if u.path.startswith("/api/"):
                status, body = dispatch_console_api(self.server.controller, "GET", u.path, parse_qs(u.query), {})
                return self._send(status, body)
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
            return self._handle_login()
        if not self._require():
            return
        if not _check_origin(self):
            return self._send(403, {"error": "跨站请求被拒绝"})
        try:
            if self._local_noauth() and self.headers.get_content_type() != "application/json":
                return self._send(415, {"error": "本机免令牌请求须以 JSON 提交"})
            if u.path.startswith("/api/providers/") and self.headers.get_content_type() != "application/json":
                return self._send(415, {"error": "提供商配置须以 JSON 提交"})
            data = self._body()
            status, body = dispatch_console_api(self.server.controller, "POST", u.path, {}, data)
            self._send(status, body)
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, format, *args):
        pass


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    controller: Controller
    token: str
    sessions: dict[str, float]
    session_lock: threading.Lock


def build_web_server(controller: Controller, token: str, port: int | None = None) -> ConsoleServer:
    import os
    httpd = ConsoleServer(("0.0.0.0", port or int(os.environ.get("WEB_PORT", "8080"))), Handler)
    httpd.controller, httpd.token = controller, token
    httpd.sessions, httpd.session_lock = {}, threading.Lock()
    return httpd
