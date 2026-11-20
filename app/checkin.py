"""Manual check-in queue for WorkBuddy Gateway v1.14.47 (Docker-adapted).

Mirrors the safety rules of the workbuddy-ccswitch-checkin skill, but controls a
child process managed by this container instead of a systemd user unit. No
upstream manual check-in API exists in v1.14.47; this queues one startup attempt.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))
LABELS = {
    "pending": "待签到", "running": "签到中", "success": "已签到",
    "missed": "未签到", "failed": "签到失败",
    "unsupported": "不支持签到", "unknown": "未知",
}


def day() -> str:
    return datetime.now(BJ).date().isoformat()


def read_private(path: Path) -> bytes:
    """Read a regular file owned by us with no bits for group/other."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        m = os.fstat(f.fileno())
        if not stat.S_ISREG(m.st_mode) or m.st_uid != os.geteuid() or stat.S_IMODE(m.st_mode) & 0o077:
            raise ValueError("状态文件必须是当前用户的私有普通文件")
        if m.st_size > 16 * 1024 ** 2:
            raise ValueError("状态文件超出合理大小")
        return f.read()


def atomic(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".manual-checkin-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def snapshot(root: Path) -> dict[str, dict]:
    doc = json.loads(read_private(root / "workbuddy-status.json"))
    updated = doc.get("updatedAt")
    if type(updated) not in (int, float) or not -5 <= time.time() - updated <= 35:
        raise ValueError("网关快照过期或系统时钟异动")
    rows: dict[str, dict] = {}
    for a in doc.get("accounts") or []:
        name = Path(a["path"]).name
        if name in rows or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name):
            raise ValueError("凭据文件名不安全或重复")
        rows[name] = a
    return rows


def uid_key(uid: str) -> str:
    return hashlib.sha256(("cn:uid:" + uid).encode()).hexdigest()


def candidates(rows: dict[str, dict], names: list[str], today: str) -> dict[str, str]:
    """Return the accounts eligible for exactly one manual make-up attempt."""
    result: dict[str, str] = {}
    for name in names:
        a = rows[name]
        c = a.get("checkin") or {}
        uid = a.get("uid")
        if (
            a.get("edition") == "cn"
            and a.get("schedulingEnabled") is True
            and a.get("state") == "active"
            and isinstance(uid, str) and uid.strip()
            and c.get("date") == today
            and c.get("status") == "missed"
            and c.get("attempts") == 0
        ):
            key = uid_key(uid)
            if key in result.values():
                raise ValueError("检测到重复账号，不执行")
            result[name] = key
    return result


def queue(doc: dict, selected: dict[str, str], today: str) -> None:
    if doc.get("schema") != 1 or doc.get("date") != today or not isinstance(doc.get("plans"), dict):
        raise ValueError("签到记录日期或格式不匹配")
    plans = doc["plans"]
    if any(p.get("status") in ("pending", "running") for p in plans.values()):
        raise ValueError("仍有签到排队或正在进行；不启动另一轮补签")
    for key in selected.values():
        p = plans.get(key) or {}
        if p.get("status") != "missed" or p.get("attempts") != 0 or p.get("lastAt", 0) != 0:
            raise ValueError("权威记录已变化或已尝试，不执行")
    for key in selected.values():
        p = plans[key]
        p.update(status="pending", message="用户请求手动补签")
        p.pop("startupAt", None)


@contextmanager
def lock(root: Path):
    fd = os.open(root / ".manual-checkin.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("锁路径不是普通文件")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有补签操作在进行") from None
        yield
    finally:
        os.close(fd)


def _poll(path: Path, selected: dict[str, str], today: str, earliest_ms: int, timeout: int) -> dict[str, str]:
    end = time.monotonic() + timeout
    pending = dict(selected)
    results: dict[str, str] = {}
    while pending and time.monotonic() < end and day() == today:
        doc = json.loads(read_private(path))
        if doc.get("date") != today:
            break
        for name, key in list(pending.items()):
            rec = doc.get("plans", {}).get(key, {})
            if rec.get("attempts") == 1 and rec.get("lastAt", 0) >= earliest_ms and rec.get("status") in ("success", "failed"):
                results[name] = rec["status"]
                del pending[name]
        if pending:
            time.sleep(2)
    return results


def run_checkin(root: Path, gateway, names: list[str], timeout: int = 180) -> dict[str, str]:
    """Queue one attempt for eligible accounts, restarting the gateway child.

    `gateway` must expose stop(), start() and alive(). Returns file -> status.
    """
    state = root / "checkin-state.json"
    with lock(root):
        rows = snapshot(root)  # freshness check before the interruption
        if not set(names) <= rows.keys():
            raise ValueError("选中的凭据文件名不在快照中")
        today = day()
        selected = candidates(rows, names, today)
        if not selected:
            return {}
        now = datetime.now(BJ)
        if day() != today or now.hour < 8 or (now.hour == 23 and now.minute >= 50):
            raise ValueError("仅在同一北京时间日期的 08:00–23:50 内执行")
        raw = read_private(state)
        queue(json.loads(raw), selected, today)  # preflight
        backups = root / ".manual-checkin-backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        gateway.stop()
        try:
            raw2 = read_private(state)
            if day() != today:
                raise ValueError("北京时间已跨日，未修改记录")
            doc = json.loads(raw2)
            queue(doc, selected, today)
            backup = backups / f"{today}-{time.time_ns()}.json"
            atomic(backup, raw2)
            atomic(state, json.dumps(doc, ensure_ascii=False).encode())
        finally:
            earliest = time.time_ns() // 1_000_000
            gateway.start()
        results = _poll(state, selected, today, earliest, timeout)
    return {name: LABELS.get(results.get(name, ""), "尚未确认，请稍后只读查询") for name in selected}
