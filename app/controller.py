"""Controller for the WorkBuddy <-> Hermes Docker stack.

All credential files stay inside the private gateway data volume. This module
only exposes non-identifying labels to the web console.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from checkin import LABELS as CHECKIN_LABELS, atomic, candidates, day, lock, snapshot
from manual_checkin import request_manual_checkin
from provider_registry import ProviderRegistry

BJ = timezone(timedelta(hours=8))
ROOT = Path(os.environ.get("GW_ROOT", "/data/gateway"))
WEB_DATA = Path(os.environ.get("WEB_DATA", "/data/web"))
BIN = Path(os.environ.get("GW_BIN", "/app/bin/workbuddy-gateway"))
GW_PORT = int(os.environ.get("GW_PORT", "8317"))
HOST_GW_PORT = int(os.environ.get("HOST_GW_PORT", str(GW_PORT)))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8318"))
HOST_BRIDGE_PORT = int(os.environ.get("HOST_BRIDGE_PORT", str(BRIDGE_PORT)))
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT", "300"))
AUTO_DISABLED_FILE = "auto-checkin.disabled"
FILE_RE = re.compile(r"^workbuddy[0-9]*\.json$")
URL_RE = re.compile(r"https://copilot\.tencent\.com/login\?[^\s\u2580-\u25ff]+")


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


class Gateway:
    """Owns the single `serve` child process so check-in can restart it safely."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log = None
        self._lock = threading.Lock()
        self.version = "unknown"
        try:
            out = subprocess.run([str(BIN), "version"], cwd=ROOT, capture_output=True,
                                 text=True, check=True, timeout=10).stdout.strip()
            self.version = out.splitlines()[-1] if out else "unknown"
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
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

    def stop(self, timeout: int = 20) -> None:
        with self._lock:
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
            if self._log:
                self._log.close()
                self._log = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def shutdown(self) -> None:
        self.stop()


class LoginSession:
    def __init__(self, sid: str, filename: str, proc: subprocess.Popen, log_path: Path):
        self.sid = sid
        self.filename = filename
        self.proc = proc
        self.log_path = log_path
        self.started = time.time()
        self.url = ""
        self.done = False
        self.returncode: int | None = None


class Controller:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        self.sessions: dict[str, LoginSession] = {}
        self._sessions_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self.labels_path = WEB_DATA / "labels.json"
        self.provider_registry = ProviderRegistry(WEB_DATA / "providers.json")
        self._catalog: list[dict] = []
        self._catalog_at = 0.0
        self._catalog_lock = threading.Lock()

    # ---------- labels / presentation ----------
    def labels(self) -> dict[str, str]:
        try:
            data = json.loads(private_read(self.labels_path))
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_label(self, filename: str, label: str) -> None:
        if not FILE_RE.match(filename) or not label.strip():
            raise ValueError("文件名或标签无效")
        label = re.sub(r"[\x00-\x1f\x7f]", "", label).strip()[:32]
        data = self.labels()
        data[filename] = label
        private_write(self.labels_path, json.dumps(data, ensure_ascii=False))

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
        auto_enabled = self.auto_checkin_enabled()
        updated = doc.get("updatedAt")
        manual_available = (self.gateway.alive() and (WEB_DATA / "manual-checkin.sock").is_socket()
                            and isinstance(updated, (int, float)) and -5 <= time.time() - updated <= 35)
        labels = self.labels()
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
            c = (a or {}).get("checkin") or {}
            current = c.get("date") == today
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
            eligible = bool(a and name in candidates({name: a}, [name], today))
            accounts.append({
                "file": name,
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
                } if a else None,
                "checkin": checkin,
                "eligible": eligible,
                "manualEligible": bool(manual_available and current and c.get("manualReady") is True),
            })
        with self._sessions_lock:
            sessions = [self._session_view(s) for s in self.sessions.values()]
        return {
            "now": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S CST"),
            "version": self.gateway.version,
            "gatewayAlive": self.gateway.alive(),
            "updatedAt": doc.get("updatedAt"),
            "accounts": accounts,
            "autoCheckinEnabled": auto_enabled,
            "sessions": sessions,
            "hermes": self.hermes(),
            "modelCatalog": self.model_catalog(),
            "bridgePort": HOST_BRIDGE_PORT,
        }

    # ---------- accounts ----------
    def next_filename(self) -> str:
        used = {p.name for p in ROOT.glob("workbuddy*.json")}
        used |= {p.name for p in (ROOT / ".disabled").glob("workbuddy*.json")}
        n = 2
        while f"workbuddy{n}.json" in used:
            n += 1
        return f"workbuddy{n}.json"

    def add_account(self) -> dict:
        if not self._login_lock.acquire(blocking=False):
            raise ValueError("已有登录会话在进行，请等待完成或取消后再试")
        try:
            filename = self.next_filename()
            sid = secrets.token_hex(6)
            log_path = WEB_DATA / f"login-{filename}.log"
            if log_path.exists():
                log_path.unlink()
            private_write(log_path, "")
            proc = subprocess.Popen(
                [str(BIN), "login", "-auth", filename],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, stdin=subprocess.DEVNULL,
            )
            session = LoginSession(sid, filename, proc, log_path)
            with self._sessions_lock:
                self.sessions[sid] = session
            threading.Thread(target=self._pump, args=(session,), daemon=True).start()
            return {"sessionId": sid, "file": filename, "loginUrl": ""}
        finally:
            self._login_lock.release()

    def _pump(self, s: LoginSession) -> None:
        buf: list[str] = []
        try:
            assert s.proc.stdout is not None
            for line in s.proc.stdout:
                buf.append(line.rstrip("\r\n"))
                if len(buf) > 120:
                    buf.pop(0)
                # strip ANSI colour but keep the QR block (box-drawing chars)
                clean = re.sub(r"\x1b\[[0-9;]*m", "", "".join(line))
                with open(s.log_path, "a", encoding="utf-8") as f:
                    f.write(clean)
                if not s.url:
                    m = URL_RE.search(clean)
                    if m:
                        s.url = m.group(0).rstrip(".,;)")
        except Exception:
            pass
        finally:
            if s.proc.poll() is None:
                try:
                    s.proc.terminate()
                    s.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    s.proc.kill()
            s.returncode = s.proc.returncode
            s.done = True
            with open(s.log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[登录进程结束，退出码 {s.returncode}]\n")

    def _session_view(self, s: LoginSession) -> dict:
        expired = time.time() - s.started > LOGIN_TIMEOUT
        if expired and not s.done and s.proc.poll() is None:
            s.proc.terminate()
        try:
            text = private_read(s.log_path)
        except OSError:
            text = ""
        # Keep the terminal QR block, trim long audit log lines.
        lines = [ln for ln in text.splitlines() if "[日志脱敏]" not in ln and "[Log]" not in ln]
        tail = "\n".join(lines[-40:])
        status = "done" if s.done else ("expired" if expired else "waiting")
        return {
            "sessionId": s.sid, "file": s.filename, "status": status,
            "loginUrl": s.url, "output": tail,
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
            if s and not s.done:
                s.proc.terminate()
                s.done = True

    def remove_account(self, filename: str) -> None:
        if not FILE_RE.match(filename):
            raise ValueError("凭据文件名无效")
        src = ROOT / filename
        if not src.is_file():
            raise ValueError("凭据文件不存在")
        disabled = ROOT / ".disabled"
        disabled.mkdir(mode=0o700, exist_ok=True)
        os.replace(src, disabled / filename)

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

    def manual_checkin(self, name: str) -> str:
        if not FILE_RE.fullmatch(name):
            raise ValueError("凭据文件名无效")
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
                if not FILE_RE.fullmatch(name) or state.get("date") != today or state.get("manualReady") is not True:
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
    def model_catalog(self) -> list[dict]:
        with self._catalog_lock:
            if self._catalog and time.time() - self._catalog_at < 300:
                return self._catalog
            key = self.api_key()
            if not key:
                return self._catalog
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{GW_PORT}/v1/models",
                    headers={"Authorization": "Bearer " + key},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode()).get("data") or []
                rows = []
                for item in data:
                    mid = item.get("id")
                    if not isinstance(mid, str) or not mid:
                        continue
                    caps = ((item.get("capabilities_by_site") or {}).get("cn") or {})
                    row: dict[str, object] = {"id": mid}
                    for field in ("context_length", "max_output_tokens"):
                        value = caps.get(field, item.get(field, item.get("default_" + field)))
                        if isinstance(value, int) and value > 0:
                            row[field] = value
                    rows.append(row)
                if rows:
                    self._catalog, self._catalog_at = rows, time.time()
            except (OSError, ValueError, TypeError):
                pass
            return self._catalog

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
            "base_url": f"http://127.0.0.1:{HOST_GW_PORT}/v1",
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
            "configCommand": "hermes config set --force providers.workbuddy '" + json.dumps(entry) + "'",
            "envVar": "WORKBUDDY_GATEWAY_API_KEY",
        }

    def api_key(self) -> str:
        cfg = json.loads(private_read(ROOT / "config.json"))
        return str((cfg.get("gateway") or {}).get("apiKey") or "")
