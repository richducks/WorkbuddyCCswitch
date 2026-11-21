"""Read-only quota bridge for CC Switch.

Binds inside the container; Compose publishes to loopback by default.
No route performs check-ins.
"""
from __future__ import annotations

import hmac
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from checkin import read_private

BJ = timezone(timedelta(hours=8))
LABELS = dict(
    success="已签到", failed="签到失败", missed="未签到", pending="待签到",
    running="签到中", unsupported="不支持签到", unknown="签到状态未知",
)


def number(value):
    if type(value) in (int, float) and 0 <= value <= 1e15 and math.isfinite(value):
        return round(value, 2)
    return None


def payload(state: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, BJ).date().isoformat()
    updated = state.get("updatedAt")
    u = float(updated) if type(updated) in (int, float) else None  # type: ignore[arg-type]
    fresh = u is not None and -5 <= now - u <= 45
    rows = []
    for i, account in enumerate(sorted(state.get("accounts") or [], key=lambda a: a.get("path", "")), 1):
        name = Path(account.get("path") or "").name
        alias = name if re.fullmatch(r"workbuddy[0-9]*\.json", name) else f"账号 {i}"
        sign = account.get("checkin") or {}
        status = LABELS.get(str(sign.get("status", "")), "签到状态未知") if sign.get("date") == today else "今日签到未确认"
        if status == "已签到" and not fresh:
            status = "今日签到未确认"
        remaining = number(account.get("quotaRemaining"))
        valid = fresh and account.get("state") == "active" and account.get("quotaKnown") is True and remaining is not None
        row = dict(planName=alias, extra=status + " · " + today, isValid=valid, unit="积分")
        if valid:
            for output, field in [("total", "quotaTotal"), ("used", "quotaUsed"), ("remaining", "quotaRemaining")]:
                n = number(account.get(field))
                if n is not None:
                    row[output] = n
        else:
            row["invalidMessage"] = "网关快照过期" if not fresh else "账号不可用或额度未知"
        rows.append(row)
    return dict(entries=rows or [dict(isValid=False, invalidMessage="未读取到账号")])


class UsageServer(ThreadingHTTPServer):
    state: Path
    token: bytes
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/usage":
            return self.send_error(404)
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        quota = cast(UsageServer, self.server)
        if not hmac.compare_digest(supplied, b"Bearer " + quota.token):
            return self.send_error(401)
        try:
            data = payload(json.loads(read_private(quota.state)))
        except (OSError, ValueError, AttributeError, TypeError):
            return self.send_error(503, "Snapshot unavailable")
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def build_server(port: int, state: Path, token: bytes, bind: str = "0.0.0.0") -> UsageServer:
    http = UsageServer((bind, port), Handler)
    http.state, http.token = state, token
    return http
