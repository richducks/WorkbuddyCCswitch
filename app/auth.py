"""Console authentication state: cookie sessions, CSRF tokens, login throttle.

Sessions live in memory only. A container restart therefore invalidates every
browser session (and only the operator's cookie), which keeps the control-plane
secret out of the data volume; the gateway API key, the console password and the
usage-bridge token are unaffected. Basic Auth stays available for scripts.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time

SESSION_COOKIE = "wb_session"
CSRF_COOKIE = "wb_csrf"
SESSION_TTL = 12 * 3600
MAX_SESSIONS = 5
LOGIN_FAILURE_DELAY = 0.4


class SessionStore:
    """Bounded set of live browser sessions, each with its own CSRF token."""

    def __init__(self, ttl: float = SESSION_TTL, max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for sid in [s for s, row in self._rows.items() if now - row["created"] >= self.ttl]:
            del self._rows[sid]

    def create(self) -> tuple[str, str]:
        """Return (session id, CSRF token); evicts the oldest session past the cap."""
        now = time.monotonic()
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            while len(self._rows) >= self.max_sessions:
                oldest = min(self._rows, key=lambda s: self._rows[s]["created"])
                del self._rows[oldest]
            self._rows[sid] = {"csrf": csrf, "created": now}
        return sid, csrf

    def lookup(self, sid: str) -> dict | None:
        if not sid:
            return None
        now = time.monotonic()
        with self._lock:
            row = self._rows.get(sid)
            if row is None:
                return None
            if now - row["created"] >= self.ttl:
                del self._rows[sid]
                return None
            return dict(row)

    def csrf_ok(self, sid: str, token: str) -> bool:
        row = self.lookup(sid)
        return bool(row and token and hmac.compare_digest(str(row["csrf"]), token))

    def drop(self, sid: str) -> None:
        with self._lock:
            self._rows.pop(sid, None)

    def count(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._rows)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def parse_cookies(header: str) -> dict[str, str]:
    """Parse a Cookie header without trusting any part of it."""
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.partition("=")
        if not sep:
            continue
        name = name.strip()
        if name and name not in out:
            out[name] = value.strip().strip('"')
    return out


def cookie_header(name: str, value: str, max_age: int, *, http_only: bool = False) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={int(max_age)}", "SameSite=Strict"]
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


def clear_cookie_header(name: str) -> str:
    return f"{name}=; Path=/; Max-Age=0; SameSite=Strict"
