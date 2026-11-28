"""Optional outbound alerts: failed check-in and near-term quota expiry.

Disabled unless WB_NOTIFY_WEBHOOK is set. The console keeps working when the
webhook is slow or broken: a failed delivery is logged and retried on the next
round, and nothing is marked as notified until the receiver accepted it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_FILE = "notify-state.json"
STATE_TTL = 30 * 86400
EXPIRY_WARN_DAYS = 3
KINDS = ("generic", "telegram", "dingtalk", "feishu")


def format_message(kind: str, text: str) -> dict:
    """Wrap plain text in the payload shape each receiver expects."""
    if kind == "telegram":
        return {"text": text, "disable_web_page_preview": True}
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"source": "workbuddy-hermes", "text": text}


def send(url: str, kind: str, text: str, timeout: float = 10.0) -> None:
    """POST one alert; raises OSError/ValueError so the caller can retry later."""
    body = json.dumps(format_message(kind, text), ensure_ascii=False).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 300:
                raise ValueError(f"通知服务返回 HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"通知服务返回 HTTP {exc.code}") from None


def _account_name(account: dict) -> str:
    return str(account.get("label") or account.get("file") or "账号")


def pending_alerts(status: dict, notified: dict[str, int], now: float | None = None) -> list[tuple[str, str]]:
    """Return [(dedupe key, text)] for conditions that were not alerted yet."""
    now = time.time() if now is None else now
    alerts: list[tuple[str, str]] = []
    for account in status.get("accounts") or []:
        name = _account_name(account)
        checkin = account.get("checkin") or {}
        today = checkin.get("date") or ""
        if checkin.get("status") == "failed" and today:
            key = f"checkin-failed:{account.get('file')}:{today}"
            if key not in notified:
                attempts = checkin.get("attempts")
                detail = f"（{attempts} 次尝试）" if attempts else ""
                message = str(checkin.get("message") or "").strip()
                alerts.append((key, f"【WorkBuddy】{name} 今日签到失败{detail}" + (f"：{message}" if message else "")))
        expire_at = account.get("expireAt")
        remaining = (account.get("quota") or {}).get("remaining")
        if isinstance(expire_at, int) and not isinstance(expire_at, bool) and remaining:
            days = (expire_at - now) / 86400
            if -1 <= days <= EXPIRY_WARN_DAYS:
                key = f"quota-expiring:{account.get('file')}:{time.strftime('%Y-%m-%d', time.gmtime(expire_at + 8 * 3600))}"
                if key not in notified:
                    when = time.strftime("%Y-%m-%d", time.gmtime(expire_at + 8 * 3600))
                    left = "已到期" if days < 0 else f"剩 {max(1, round(days))} 天"
                    alerts.append((key, f"【WorkBuddy】{name} 的 {remaining:g} 积分将在 {when} 到期（{left}）"))
    return alerts


def load_state(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


def save_state(path: Path, state: dict[str, int], now: float | None = None) -> None:
    """Write private state, dropping keys older than STATE_TTL to bound its size."""
    now = time.time() if now is None else now
    fresh = {k: v for k, v in state.items() if now - v < STATE_TTL}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(fresh, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def notify_once(controller, state_path: Path, url: str, kind: str = "generic", timeout: float = 10.0) -> list[str]:
    """Send every pending alert once; returns the keys that were delivered."""
    state = load_state(state_path)
    delivered: list[str] = []
    for key, text in pending_alerts(controller.status(), state):
        send(url, kind, text, timeout=timeout)
        state[key] = int(time.time())
        delivered.append(key)
    if delivered:
        save_state(state_path, state)
    return delivered


def run(controller, state_path: Path, url: str, kind: str = "generic",
        interval: int = 900, stop: threading.Event | None = None) -> None:
    """Background loop used by boot.py; never raises into the caller."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            delivered = notify_once(controller, state_path, url, kind)
            for key in delivered:
                print(f"[notify] 已发送提醒 {key.split(':')[0]}", flush=True)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"[notify] 发送失败，稍后重试：{type(exc).__name__}", flush=True)
        stop.wait(interval)
