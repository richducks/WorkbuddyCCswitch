"""Controller for the WorkBuddy <-> Hermes Docker stack.

All credential files stay inside the private gateway data volume. This module
only exposes non-identifying labels to the web console.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from checkin import LABELS as CHECKIN_LABELS, atomic, candidates, day, lock, snapshot
from manual_checkin import request_manual_checkin
from provider_registry import ProviderRegistry

BJ = timezone(timedelta(hours=8))
ROOT = Path(os.environ.get("GW_ROOT", "/data/gateway"))
WEB_DATA = Path(os.environ.get("WEB_DATA", "/data/web"))
BIN = Path(os.environ.get("GW_BIN", "/app/bin/workbuddy-gateway"))
GW_PORT = int(os.environ.get("GW_PORT", "8317"))


def bind_ip(value: str) -> str:
    addr = IPv4Address(value)
    if addr.is_loopback or any(addr in IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        return str(addr)
    raise ValueError("BIND_IP 必须是回环或专用局域网 IPv4 地址，不能是 0.0.0.0 或公网地址")


HOST_BIND_IP = bind_ip(os.environ.get("HOST_BIND_IP", "127.0.0.1"))
HOST_GW_PORT = int(os.environ.get("HOST_GW_PORT", str(GW_PORT)))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8318"))
HOST_BRIDGE_PORT = int(os.environ.get("HOST_BRIDGE_PORT", str(BRIDGE_PORT)))
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT", "300"))
AUTO_DISABLED_FILE = "auto-checkin.disabled"
ACCOUNT_AUTO_CHECKIN_SUFFIX = ".auto-checkin"
FILE_RE = re.compile(r"^workbuddy[0-9]*\.json$")
URL_RE = re.compile(r"https://copilot\.tencent\.com/login\?[^\s\u2580-\u25ff]+")


def checkin_only_marker(filename: str) -> Path:
    return ROOT / (filename + ".checkin-only")


def is_checkin_only(filename: str) -> bool:
    try:
        checkin_only_marker(filename).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def private_write(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as f:
        f.write(data)


def private_read(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "r") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("不是普通文件")
        return f.read()


def mask(value: str) -> str:
    if len(value) <= 10:
        return "***"
    return value[:4] + "***" + value[-4:]


def _epoch(value: object) -> int | None:
    """Normalise a gateway epoch-seconds field; anything else means unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value > 0 else None


def _expire_in(expire_at: int | None, now: float | None = None) -> int | None:
    """Whole days left before the quota window closes (negative once passed).

    Future windows round up so a TTL of 23h still reads "1 天"; past ones round
    down so "已过期" never shows 0.
    """
    if expire_at is None:
        return None
    delta = expire_at - (time.time() if now is None else now)
    return math.floor(delta / 86400) if delta < 0 else math.ceil(delta / 86400)


def expiry_windows(accounts: list[dict], now: float | None = None) -> dict:
    """Sum still-usable quota per expiry window for the console warning bar.

    Windows are measured from now to the earliest quota period end reported by
    the gateway (`quotaExpireAt`). Accounts with unknown or expired windows are
    left out entirely so the numbers never overstate what can still be spent.
    """
    now = time.time() if now is None else now
    windows = {"within3d": 3, "within7d": 7}
    out: dict[str, dict[str, float]] = {name: {"quota": 0.0, "accounts": 0.0} for name in windows}
    earliest: int | None = None
    for account in accounts:
        expire_at = _epoch(account.get("expireAt"))
        if expire_at is None:
            continue
        remaining = (account.get("quota") or {}).get("remaining")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining <= 0:
            continue
        earliest = expire_at if earliest is None else min(earliest, expire_at)
        for name, days in windows.items():
            if expire_at - now <= days * 86400:
                out[name]["quota"] += float(remaining)
                out[name]["accounts"] += 1
    return {**out, "earliestAt": earliest}


class Gateway:
    """Owns the single `serve` child process; a supervisor restarts it on crash."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log = None
        self._lock = threading.Lock()
        self._shutdown = False
        self.version = "unknown"
        try:
            out = subprocess.run([str(BIN), "version"], cwd=ROOT, capture_output=True,
                                 text=True, check=True, timeout=10).stdout.strip()
            self.version = out.splitlines()[-1] if out else "unknown"
        except Exception:
            pass

    def _close_log(self) -> None:
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _spawn(self) -> None:
        self._close_log()
        (ROOT / "logs").mkdir(mode=0o700, exist_ok=True)
        self._log = (ROOT / "logs" / "stdout.log").open("a", buffering=1)
        env = {**os.environ, "WB_MANUAL_CHECKIN_SOCKET": str(WEB_DATA / "manual-checkin.sock"),
               "WB_AUTO_CHECKIN_DISABLED_FILE": str(WEB_DATA / AUTO_DISABLED_FILE)}
        preserve = WEB_DATA / ".preserve-checkin-once"
        try:
            preserve.unlink()
        except FileNotFoundError:
            pass
        else:
            env["WB_PRESERVE_CHECKIN_SCHEDULE"] = "1"
        self.proc = subprocess.Popen(
            [str(BIN), "serve", "-addr", "0.0.0.0", "-port", str(GW_PORT)],
            cwd=ROOT, stdout=self._log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env,
        )

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._spawn()

    def supervise(self, poll_seconds: int = 5) -> None:
        """Restart the gateway child if it dies; back off if restarts keep failing."""
        delay = poll_seconds
        while not self._shutdown:
            time.sleep(delay)
            if self._shutdown:
                break
            with self._lock:
                if self._shutdown:
                    return
                if self.proc is None or self.proc.poll() is None:
                    delay = poll_seconds
                    continue
                print(f"[boot] 网关进程意外退出（退出码 {self.proc.returncode}），正在重启", flush=True)
                try:
                    self._spawn()
                except OSError as exc:
                    self._close_log()
                    print(f"[boot] 网关重启失败：{type(exc).__name__}: {exc}", flush=True)
                # A successful Popen is not recovery; reset only after a live poll.
                delay = min(delay * 2, max(poll_seconds, 30))

    def stop(self, timeout: int = 20) -> None:
        with self._lock:
            self._shutdown = True
            proc, self.proc = self.proc, None
            if proc is None:
                return
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            self._close_log()

    def alive(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def shutdown(self) -> None:
        self.stop()


class LoginSession:
    def __init__(self, sid: str, filename: str, proc: subprocess.Popen):
        self.sid = sid
        self.filename = filename
        self.proc = proc
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self.output: deque[str] = deque(maxlen=40)
        self.url = ""
        self.stopping = False


class Controller:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        self.sessions: dict[str, LoginSession] = {}
        self._sessions_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._usage_lock = threading.Lock()  # 序列化「刷新数据」触发的容器内 ETL
        self._labels_lock = threading.Lock()
        self.labels_path = WEB_DATA / "labels.json"
        self._reserves_lock = threading.Lock()
        self.reserves_path = ROOT / "reserve.json"
        self._pins_lock = threading.Lock()
        self.pins_path = ROOT / "pins.json"
        self.provider_registry = ProviderRegistry(WEB_DATA / "providers.json")
        snapshot_override = os.environ.get("WB_USAGE_SNAPSHOT")
        self.usage_path = Path(snapshot_override) if snapshot_override else WEB_DATA / "usage_console_snapshot.json"
        # The host ETL can still update its legacy report, but never owns the console snapshot.
        self._usage_read_paths = (self.usage_path,) if snapshot_override else (
            self.usage_path, WEB_DATA / "usage_snapshot.json",
        )
        self._catalog: list[dict] = []
        self._catalog_refresh_at = 0.0
        self._catalog_fetching = False
        self._catalog_lock = threading.Lock()

    # ---------- labels / presentation ----------
    def _read_labels(self) -> dict[str, str]:
        try:
            data = json.loads(private_read(self.labels_path))
            return ({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
                    if isinstance(data, dict) else {})
        except (OSError, ValueError):
            return {}

    def labels(self) -> dict[str, str]:
        with self._labels_lock:
            return self._read_labels()

    def set_label(self, filename: str, label: str) -> None:
        if not FILE_RE.match(filename) or not label.strip():
            raise ValueError("文件名或标签无效")
        label = re.sub(r"[\x00-\x1f\x7f]", "", label).strip()[:32]
        with self._labels_lock:
            data = self._read_labels()
            data[filename] = label
            private_write(self.labels_path, json.dumps(data, ensure_ascii=False))

    # ---------- reserve quotas (gateway hot-reloads ROOT/reserve.json) ----------
    DEFAULT_RESERVE = 100.0

    def reserves(self) -> dict[str, float]:
        try:
            data = json.loads(private_read(self.reserves_path))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items()
                if isinstance(k, str) and isinstance(v, (int, float))
                and not isinstance(v, bool) and v == v and 0 <= v < float("inf")}

    def set_reserve(self, filename: str, reserve: float) -> None:
        if not FILE_RE.match(filename):
            raise ValueError("文件名无效")
        if not isinstance(reserve, (int, float)) or isinstance(reserve, bool) \
                or not (0 <= reserve < float("inf")) or reserve != reserve:
            raise ValueError("保留额度必须是非负数字")
        with self._reserves_lock:
            data = self.reserves()
            data[filename] = reserve
            atomic(self.reserves_path, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # ---------- pinned accounts (gateway hot-reloads ROOT/pins.json) ----------
    def pins(self) -> dict[str, int]:
        """置顶账号：文件名 → 置顶顺序（1 最优先）。网关选号时先消耗置顶账号。"""
        try:
            data = json.loads(private_read(self.pins_path))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {name: rank for name, rank in data.items()
                if isinstance(name, str) and isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1}

    def set_pin(self, filename: str, pinned: bool) -> None:
        if not isinstance(pinned, bool):
            raise ValueError("置顶参数必须为布尔值")
        if not FILE_RE.match(filename):
            raise ValueError("文件名无效")
        with self._pins_lock:
            data = self.pins()
            if pinned:
                if filename not in data:
                    data[filename] = (max(data.values()) + 1) if data else 1
            else:
                data.pop(filename, None)
            atomic(self.pins_path, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def alias(self, filename: str, index: int) -> str:
        return self.labels().get(filename) or f"账号 {index} · {filename}"

    # ---------- status ----------
    def _status_doc(self) -> dict:
        for _ in range(3):
            try:
                return json.loads(private_read(ROOT / "workbuddy-status.json"))
            except (OSError, ValueError):
                time.sleep(1)
        return {"accounts": [], "updatedAt": 0}

    def status(self) -> dict:
        doc = self._status_doc()
        today = day()
        auto_default = self.auto_checkin_enabled()
        updated = doc.get("updatedAt")
        snapshot_fresh = type(updated) in (int, float) and -5 <= time.time() - updated <= 35
        manual_available = (snapshot_fresh and self.gateway.alive()
                            and (WEB_DATA / "manual-checkin.sock").is_socket())
        labels = self.labels()
        reserves = self.reserves()
        pins = self.pins()
        by_file = {}
        for a in doc.get("accounts") or []:
            name = Path(a.get("path") or "").name
            if name:
                by_file[name] = a
        disk_files = sorted(p.name for p in ROOT.glob("workbuddy*.json") if p.is_file() and FILE_RE.match(p.name))
        all_files = sorted(set(by_file) | set(disk_files))
        accounts = []
        for i, name in enumerate(all_files, 1):
            a = by_file.get(name)
            auto_enabled = self.account_auto_checkin_enabled(name, default=auto_default)
            c = (a or {}).get("checkin") or {}
            current = snapshot_fresh and c.get("date") == today
            state = str(c.get("status", "")) if current else ""
            checkin = {
                "date": c.get("date"),
                "status": state,
                "label": CHECKIN_LABELS.get(state, "今日未确认") if current else "今日未确认",
                "attempts": c.get("attempts") if current else None,
                "lastAt": c.get("lastAt") if current else None,
                "nextAt": c.get("nextAt") if current and auto_enabled and manual_available else None,
                "message": c.get("message", "") if current else "",
            }
            eligible = bool(current and a and name in candidates({name: a}, [name], today))
            checkin_only = is_checkin_only(name)
            # 成长值（只读查询结果）：仅展示当日数据，隔天的缓存直接丢弃，避免误报已领取。
            g = (a or {}).get("growth") or {}
            try:
                g_day = datetime.fromtimestamp(g["updatedAt"], BJ).date() if g else None
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                g_day = None
            growth = g if snapshot_fresh and g_day is not None and g_day.isoformat() == today else None
            expire_at = _epoch(a.get("quotaExpireAt")) if a else None
            accounts.append({
                "file": name,
                "checkinOnly": checkin_only,
                "autoCheckinEnabled": auto_enabled,
                "pinRank": pins.get(name, 0),
                "label": labels.get(name) or f"账号 {i} · {name}",
                "loaded": a is not None,
                "edition": a.get("edition") if a else None,
                "state": a.get("state") if a else None,
                "schedulingEnabled": a.get("schedulingEnabled") if a else None,
                "planLabel": a.get("planLabel") if a else None,
                "quota": {
                    "total": a.get("quotaTotal") if a else None,
                    "used": a.get("quotaUsed") if a else None,
                    "remaining": a.get("quotaRemaining") if a else None,
                    "known": a.get("quotaKnown") if a else None,
                    "expireAt": a.get("quotaExpireAt") if a else None,
                    "batches": (a.get("creditBatches") or []) if snapshot_fresh else [],
                    "reserve": reserves.get(name, self.DEFAULT_RESERVE) if a else None,
                } if a else None,
                "expireAt": expire_at,
                "expireIn": _expire_in(expire_at),
                "checkin": checkin,
                "growth": growth,
                "eligible": not checkin_only and eligible,
                "manualEligible": bool(not checkin_only and manual_available and current and c.get("manualReady") is True),
            })
        with self._sessions_lock:
            self._prune_sessions()
            sessions = [self._session_view(s, include_output=False) for s in self.sessions.values()]
        return {
            "now": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S CST"),
            "version": self.gateway.version,
            "gatewayAlive": self.gateway.alive(),
            "updatedAt": doc.get("updatedAt"),
            "accounts": accounts,
            "expiry": expiry_windows(accounts),
            "autoCheckinEnabled": auto_default,
            "sessions": sessions,
            "hermes": self.hermes(),
            "modelCatalog": self.model_catalog(),
            "bridgePort": HOST_BRIDGE_PORT,
            "bridgeUrl": f"http://{HOST_BIND_IP}:{HOST_BRIDGE_PORT}",
        }

    # ---------- usage dashboard ----------
    @staticmethod
    def _usage_payload(path: Path) -> dict:
        data = json.loads(private_read(path))
        if not isinstance(data, dict) or not isinstance(data.get("kpi"), dict) or not isinstance(data.get("days"), list):
            raise ValueError("用量快照格式不正确")
        if type(data["kpi"].get("requests", 0)) is not int:
            raise ValueError("用量快照缺少有效的请求数")
        return data

    def usage_snapshot(self) -> dict:
        """Read the last successful snapshot without triggering imports."""
        for path in self._usage_read_paths:
            try:
                return {"available": True, "payload": self._usage_payload(path)}
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return {"available": False, "hint": "暂时无法读取用量数据。请联系管理员重新刷新。"}
        return {"available": False, "hint": "尚未生成用量数据。管理员可点击「刷新数据」，统计已记录的网关请求。"}

    @staticmethod
    def _usage_keeps_history(previous: dict, current: dict) -> bool:
        # A matching total can hide a lost day or an entire data source.
        def reduced(before, after):
            return type(before) is int and before > 0 and (type(after) is not int or after < before)

        for key in ("requests", "codex_requests", "gateway_requests"):
            if reduced(previous["kpi"].get(key), current["kpi"].get(key)):
                return False
        per_day = {day["day"]: day.get("req") for day in current["days"]}
        return not any(reduced(day.get("req"), per_day.get(day.get("day"))) for day in previous["days"]
                       if isinstance(day, dict))

    def usage_refresh(self) -> dict:
        """Increment the local ledger, then publish a validated snapshot atomically."""
        if not self._usage_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有一次刷新正在进行，请稍候再试。"}
        try:
            default_etl = Path(__file__).resolve().parents[1] / "scripts/usage_etl/etl_behavior_ledger.py"
            etl = Path(os.environ.get("WB_ETL_SCRIPT", default_etl))
            ledger = Path(os.environ.get("WB_LEDGER_DB", WEB_DATA / "usage_ledger.db"))
            cc_export = WEB_DATA / "cc-switch-export.db"
            previous = self.usage_snapshot().get("payload")
            if previous and previous["kpi"].get("requests", 0) and not ledger.is_file():
                return {"ok": False, "error": "已保留旧用量历史。请先按部署文档迁移原账本，再刷新。"}
            with tempfile.TemporaryDirectory(prefix=".usage-", dir=self.usage_path.parent) as work:
                candidate = Path(work) / self.usage_path.name
                env = {**os.environ,
                       "BEHAVIOR_LEDGER_DB": str(ledger),
                       "CC_SWITCH_DB": os.environ.get("CC_SWITCH_DB", str(cc_export) if cc_export.is_file() else ""),
                       "GW_STATS": os.environ.get("GW_STATS", str(ROOT / "stats.json")),
                       "WB_USAGE_SNAPSHOT": str(candidate)}
                proc = subprocess.run(
                    [sys.executable, str(etl)], env=env, capture_output=True, text=True, timeout=300,
                )
                if proc.returncode != 0:
                    raise ValueError(proc.stderr.strip()[-1000:] or f"ETL 退出码 {proc.returncode}")
                summary = json.loads(proc.stdout)
                if not isinstance(summary, dict) or summary.get("snapshot_error") or summary.get("gateway_error"):
                    raise ValueError("ETL 未成功生成完整快照")
                payload = self._usage_payload(candidate)
                count = payload["kpi"].get("requests")
                if count is None or count != summary.get("total_requests"):
                    raise ValueError("用量快照与刷新结果不一致")
                if previous and not self._usage_keeps_history(previous, payload):
                    return {"ok": False, "error": "新账本缺少已有历史，已保留上次数据。请检查迁移的账本。"}
                os.replace(candidate, self.usage_path)
                summary["snapshot_path"] = str(self.usage_path)
                return {"ok": True, "summary": summary, "snapshot": {"available": True, "payload": payload}}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "用量刷新超时，请稍后重试。"}
        except (OSError, ValueError) as exc:
            print(f"[usage] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return {"ok": False, "error": "用量刷新失败。请检查网关统计、可选数据源和数据目录权限。"}
        finally:
            self._usage_lock.release()

    # ---------- accounts ----------
    def next_filename(self) -> str:
        used = {p.name for p in ROOT.glob("workbuddy*.json")}
        used |= {p.name for p in (ROOT / ".disabled").glob("workbuddy*.json")}
        used |= {p.name.removesuffix(".checkin-only") for p in ROOT.glob("workbuddy*.json.checkin-only")}
        n = 2
        while f"workbuddy{n}.json" in used:
            n += 1
        return f"workbuddy{n}.json"

    def _prune_sessions(self) -> None:
        """Drop completed sessions after an hour. Caller holds _sessions_lock."""
        cutoff = time.monotonic() - 3600
        for sid in [sid for sid, s in self.sessions.items() if s.finished_at is not None and s.finished_at < cutoff]:
            del self.sessions[sid]

    def add_account(self, *, checkin_only: bool = False) -> dict:
        if not self._login_lock.acquire(blocking=False):
            raise ValueError("已有登录会话在进行，请等待完成或取消后再试")
        try:
            with self._sessions_lock:
                self._prune_sessions()
                if any(s.finished_at is None for s in self.sessions.values()):
                    raise ValueError("已有登录会话在进行，请等待完成或取消后再试")
            filename = self.next_filename()
            sid = secrets.token_hex(6)
            if checkin_only:
                atomic(checkin_only_marker(filename), b"checkin-only\n")
            proc = subprocess.Popen(
                [str(BIN), "login", "-auth", filename],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, stdin=subprocess.DEVNULL,
            )
            session = LoginSession(sid, filename, proc)
            with self._sessions_lock:
                self.sessions[sid] = session
            threading.Thread(target=self._pump, args=(session,), daemon=True).start()
            return {"sessionId": sid, "file": filename, "loginUrl": ""}
        finally:
            self._login_lock.release()

    def _pump(self, s: LoginSession) -> None:
        try:
            assert s.proc.stdout is not None
            with s.proc.stdout as output:
                for line in output:
                    # Keep a bounded QR preview in memory, never in a shared log file.
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line[:4096]).rstrip("\r\n")
                    with self._sessions_lock:
                        if s.stopping:
                            continue
                        if "[日志脱敏]" not in clean and "[Log]" not in clean:
                            s.output.append(clean)
                        if not s.url and (m := URL_RE.search(clean)):
                            s.url = m.group(0).rstrip(".,;)")
        except (OSError, UnicodeError):
            pass
        finally:
            if s.proc.poll() is None:
                try:
                    s.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    s.proc.terminate()
                    try:
                        s.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        s.proc.kill()
                        s.proc.wait()
            with self._sessions_lock:
                s.output.clear()
                s.url = ""
                s.finished_at = time.monotonic()

    def _stop_login(self, s: LoginSession) -> None:
        if s.finished_at is None and not s.stopping:
            s.stopping = True
            if s.proc.poll() is None:
                s.proc.terminate()

    def _session_view(self, s: LoginSession, *, include_output: bool = True) -> dict:
        expired = time.monotonic() - s.started > LOGIN_TIMEOUT
        if expired:
            self._stop_login(s)
        done = s.finished_at is not None
        status = "done" if done else ("expired" if expired else "waiting")
        show_output = include_output and not done and not s.stopping
        return {
            "sessionId": s.sid, "file": s.filename, "status": status,
            "checkinOnly": is_checkin_only(s.filename),
            "loginUrl": s.url if show_output else "", "output": "\n".join(s.output) if show_output else "",
            "credentialCreated": (ROOT / s.filename).exists(),
        }

    def get_session(self, sid: str) -> dict:
        with self._sessions_lock:
            s = self.sessions.get(sid)
            if s is None:
                raise ValueError("登录会话不存在")
            return self._session_view(s)

    def cancel_session(self, sid: str) -> None:
        with self._sessions_lock:
            s = self.sessions.get(sid)
            if s is not None:
                self._stop_login(s)

    def remove_account(self, filename: str) -> None:
        if not FILE_RE.fullmatch(filename):
            raise ValueError("凭据文件名无效")
        with self._login_lock:
            src = ROOT / filename
            if not src.is_file():
                raise ValueError("凭据文件不存在")
            disabled = ROOT / ".disabled"
            disabled.mkdir(mode=0o700, exist_ok=True)
            os.replace(src, disabled / filename)

    def set_account_pool(self, filename: str, *, checkin_only: bool) -> None:
        if not FILE_RE.fullmatch(filename):
            raise ValueError("凭据文件名无效")
        with self._login_lock:
            if not (ROOT / filename).is_file():
                raise ValueError("凭据文件不存在")
            with self._sessions_lock:
                if any(s.filename == filename and s.finished_at is None for s in self.sessions.values()):
                    raise ValueError("请等待扫码登录完成后再移动账号")
            marker = checkin_only_marker(filename)
            if checkin_only:
                atomic(marker, b"checkin-only\n")
            else:
                marker.unlink(missing_ok=True)

    # ---------- check-in ----------
    def auto_checkin_enabled(self) -> bool:
        try:
            (WEB_DATA / AUTO_DISABLED_FILE).lstat()
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False

    def set_auto_checkin(self, enabled: bool) -> bool:
        flag = WEB_DATA / AUTO_DISABLED_FILE
        if enabled:
            flag.unlink(missing_ok=True)
        else:
            atomic(flag, b"disabled\n")
        return self.auto_checkin_enabled()

    def account_auto_checkin_enabled(self, filename: str, *, default: bool) -> bool:
        if not FILE_RE.fullmatch(filename):
            return default
        try:
            return private_read(ROOT / (filename + ACCOUNT_AUTO_CHECKIN_SUFFIX)) == "enabled\n"
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            return False

    def set_account_auto_checkin(self, filename: str, enabled: bool) -> bool:
        if not FILE_RE.fullmatch(filename):
            raise ValueError("凭据文件名无效")
        with self._login_lock:
            if not (ROOT / filename).is_file():
                raise ValueError("凭据文件不存在")
            value = b"enabled\n" if enabled else b"disabled\n"
            atomic(ROOT / (filename + ACCOUNT_AUTO_CHECKIN_SUFFIX), value)
        return enabled

    def manual_checkin(self, name: str) -> str:
        if not FILE_RE.fullmatch(name):
            raise ValueError("凭据文件名无效")
        if is_checkin_only(name):
            raise ValueError("代签到账号仅参与自动签到")
        with lock(ROOT):
            return request_manual_checkin(WEB_DATA / "manual-checkin.sock", name)

    def checkin_all(self, only: list[str] | None = None) -> dict[str, str]:
        if only is not None and not all(FILE_RE.fullmatch(name) for name in only):
            raise ValueError("凭据文件名无效")
        with lock(ROOT):
            try:
                rows = snapshot(ROOT)
            except OSError as exc:
                raise ValueError("网关状态暂不可用，请稍后刷新") from exc
            if not self.gateway.alive() or not (WEB_DATA / "manual-checkin.sock").is_socket():
                raise ValueError("网关手动签到接口未启动")
            today = day()
            chosen = set(only) if only is not None else rows.keys()
            results = {}
            for name in sorted(chosen):
                state = rows.get(name, {}).get("checkin") or {}
                if (not FILE_RE.fullmatch(name) or is_checkin_only(name)
                        or state.get("date") != today or state.get("manualReady") is not True):
                    continue
                if day() != today:
                    results[name] = "已跨日，未尝试"
                    continue
                try:
                    results[name] = request_manual_checkin(WEB_DATA / "manual-checkin.sock", name)
                except ValueError as exc:
                    results[name] = str(exc)
            return results

    # ---------- model catalog (future models are picked up automatically) ----------
    def _fetch_catalog(self) -> list[dict]:
        try:
            key = self.api_key()
            if not key:
                return []
            req = urllib.request.Request(
                f"http://127.0.0.1:{GW_PORT}/v1/models",
                headers={"Authorization": "Bearer " + key},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                doc = json.load(resp)
        except (OSError, ValueError, TypeError):
            return []
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, list):
            return []
        rows = []
        for item in data:
            mid = item.get("id") if isinstance(item, dict) else None
            if not isinstance(mid, str) or not mid:
                continue
            sites = item.get("capabilities_by_site")
            cn = sites.get("cn") if isinstance(sites, dict) else None
            caps = cn if isinstance(cn, dict) else {}
            row: dict[str, object] = {"id": mid}
            for field in ("context_length", "max_output_tokens"):
                value = caps.get(field, item.get(field, item.get("default_" + field)))
                if type(value) is int and value > 0:
                    row[field] = value
            rows.append(row)
        return rows

    def model_catalog(self) -> list[dict]:
        with self._catalog_lock:
            if self._catalog_fetching or time.monotonic() < self._catalog_refresh_at:
                return self._catalog
            self._catalog_fetching = True
        rows = []
        try:
            rows = self._fetch_catalog()
        finally:
            with self._catalog_lock:
                if rows:
                    self._catalog = rows
                self._catalog_refresh_at = time.monotonic() + (300 if rows else 30)
                self._catalog_fetching = False
                cached = self._catalog
        return cached

    # ---------- Hermes wiring ----------
    def hermes(self) -> dict:
        key = ""
        try:
            cfg = json.loads(private_read(ROOT / "config.json"))
            key = str((cfg.get("gateway") or {}).get("apiKey") or "")
        except (OSError, ValueError):
            pass
        entry: dict[str, object] = {
            "name": "WorkBuddy Gateway",
            "base_url": f"http://{HOST_BIND_IP}:{HOST_GW_PORT}/v1",
            "default_model": "hy3",
            "transport": "chat_completions",
            "key_env": "WORKBUDDY_GATEWAY_API_KEY",
        }
        models = self.model_catalog()
        if models:
            entry["models"] = models
        return {
            "endpoint": entry["base_url"],
            "model": entry["default_model"],
            "keyMasked": mask(key),
            "hasKey": bool(key),
            "providerEntry": entry,
            "configCommand": shlex.join(["hermes", "config", "set", "--force", "providers.workbuddy", json.dumps(entry)]),
            "refreshCommand": f"python3 scripts/refresh-hermes-models.py --gateway-host {HOST_BIND_IP} --gateway-port {HOST_GW_PORT}",
            "envVar": "WORKBUDDY_GATEWAY_API_KEY",
        }

    def api_key(self) -> str:
        cfg = json.loads(private_read(ROOT / "config.json"))
        return str((cfg.get("gateway") or {}).get("apiKey") or "")
