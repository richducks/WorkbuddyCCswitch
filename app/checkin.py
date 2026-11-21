"""Shared helpers for gateway state snapshots and manual check-in eligibility.

The web console reads the gateway's status snapshot, decides which accounts are
eligible for one manual attempt, and performs the attempt through the gateway's
private Unix control socket (see manual_checkin.py). It never rewrites gateway
state files itself.
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
