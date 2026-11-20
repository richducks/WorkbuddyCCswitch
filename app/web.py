"""Small authenticated HTTP API + static page for the config console."""
from __future__ import annotations

import base64
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import Controller

STATIC = Path(__file__).resolve().parent / "static"


def _check_origin(handler: "Handler") -> bool:
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host")
    if not origin or not host:
        return True
    parsed = urlparse(origin)
    return parsed.netloc == host and parsed.scheme in ("http", "https")


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

    def _require(self) -> bool:
        if self._auth():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="WorkBuddy Console", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

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
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def _guard(self) -> bool:
        if not self._require():
            return False
        if not _check_origin(self):
            self._send(403, {"error": "跨站请求被拒绝"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
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
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/providers/") and self.headers.get_content_type() != "application/json":
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
            self._send(404, {"error": "not found"})
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


def build_web_server(controller: Controller, token: str, port: int | None = None) -> ConsoleServer:
    import os
    httpd = ConsoleServer(("0.0.0.0", port or int(os.environ.get("WEB_PORT", "8080"))), Handler)
    httpd.controller, httpd.token = controller, token
    return httpd
