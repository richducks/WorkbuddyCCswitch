"""Container entrypoint: initialise secrets, then run gateway + bridge + web UI."""
from __future__ import annotations

import json
import os
import secrets
import signal
import stat
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin import atomic
from controller import BRIDGE_PORT, HOST_BIND_IP, ROOT, WEB_DATA, Controller, Gateway, private_read, private_write
from usage_bridge import build_server
import notify

TEMPLATE = Path(os.environ.get("CONFIG_TEMPLATE", "/app/config/config.json.example"))
BRIDGE_TOKEN = WEB_DATA / "bridge.token"


def ensure_token(path: Path) -> str:
    try:
        token = private_read(path).strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(36)
        atomic(path, (token + "\n").encode("ascii"))
    if not token:
        raise ValueError(f"令牌文件为空：{path.name}")
    if not token.isascii():
        raise ValueError(f"令牌文件必须使用 ASCII 字符：{path.name}")
    return token


def trim_logs(root: Path, keep_bytes: int = 5 * 1024 * 1024) -> None:
    """Prune old log tails at startup before the gateway opens them."""
    logs = root / "logs"
    if logs.is_symlink() or not logs.is_dir():
        return
    for path in sorted(logs.glob("*.log")):
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size <= keep_bytes * 2:
                    continue
                f.seek(-keep_bytes, os.SEEK_END)
                tail = f.read(keep_bytes)
            atomic(path, tail)
        except OSError:
            pass


def init() -> str:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    WEB_DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT, 0o700)
    trim_logs(ROOT)

    cfg = ROOT / "config.json"
    if not cfg.exists():
        doc = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        g = doc.setdefault("gateway", {})
        g["apiPort"] = int(os.environ.get("GW_PORT", "8317"))
        g["apiKey"] = secrets.token_urlsafe(48)
        g["adminKey"] = secrets.token_urlsafe(48)
        g["apiKeyEnabled"] = True
        private_write(cfg, json.dumps(doc, ensure_ascii=False, indent=2))
        print("[boot] generated private config.json with fresh API/admin keys", flush=True)

    bridge_token = ensure_token(BRIDGE_TOKEN)
    return bridge_token


def build_permissions_server(controller: Controller):
    from waitress import create_server

    from permissions import build_permissions_app

    try:
        bootstrap_password = private_read(WEB_DATA / "permissions-bootstrap").rstrip("\r\n")
    except FileNotFoundError:
        bootstrap_password = None
    app = build_permissions_app(controller, WEB_DATA, bootstrap_password=bootstrap_password, bind_ip=HOST_BIND_IP)
    return create_server(app, host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8080")),
                         max_request_body_size=1_000_000)


def console_server(controller: Controller):
    return build_permissions_server(controller)


def start_notify(controller: Controller) -> None:
    """Optional webhook alerts; off unless WB_NOTIFY_WEBHOOK is set."""
    url = os.environ.get("WB_NOTIFY_WEBHOOK", "").strip()
    if not url:
        return
    kind = os.environ.get("WB_NOTIFY_KIND", "generic").strip().lower()
    if kind not in notify.KINDS:
        print(f"[notify] 未知的 WB_NOTIFY_KIND={kind}，改用 generic", flush=True)
        kind = "generic"
    try:
        interval = max(60, int(float(os.environ.get("WB_NOTIFY_INTERVAL", "900"))))
    except ValueError:
        interval = 900
    # The webhook URL can embed a token, so it is never printed.
    print(f"[notify] 已启用提醒（{kind}，每 {interval} 秒检查一次）", flush=True)
    threading.Thread(target=notify.run, name="notify", daemon=True,
                     args=(controller, WEB_DATA / notify.STATE_FILE, url, kind, interval)).start()


def main() -> None:
    bridge_token = init()
    gateway = Gateway()
    controller = Controller(gateway)
    httpd = console_server(controller)
    gateway.start()
    threading.Thread(target=gateway.supervise, daemon=True, name="gateway-supervisor").start()
    print(f"[boot] gateway {gateway.version} started (supervisor active)", flush=True)
    start_notify(controller)

    bridge = build_server(BRIDGE_PORT, ROOT / "workbuddy-status.json", bridge_token.encode())
    threading.Thread(target=bridge.serve_forever, daemon=True, name="quota-bridge").start()
    print(f"[boot] quota bridge on 0.0.0.0:{BRIDGE_PORT}/usage", flush=True)

    print("=" * 56, flush=True)
    print(f"WEB_CONSOLE_URL = http://{HOST_BIND_IP}:{os.environ.get('HOST_WEB_PORT', '8080')}/", flush=True)
    print(f"WEB_CONSOLE_USER = admin", flush=True)
    print("=" * 56, flush=True)

    def stop(*_args):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.run()
    finally:
        bridge.shutdown()
        gateway.shutdown()
        httpd.close()


if __name__ == "__main__":
    main()
