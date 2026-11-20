"""Container entrypoint: initialise secrets, then run gateway + bridge + web UI."""
from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import BRIDGE_PORT, ROOT, WEB_DATA, Controller, Gateway, private_read, private_write
from usage_bridge import build_server

TEMPLATE = Path(os.environ.get("CONFIG_TEMPLATE", "/app/config/config.json.example"))
WEB_TOKEN = WEB_DATA / "web.token"
BRIDGE_TOKEN = WEB_DATA / "bridge.token"


def ensure_token(path: Path) -> str:
    try:
        return private_read(path).strip()
    except OSError:
        token = secrets.token_urlsafe(36)
        private_write(path, token + "\n")
        return token


def init() -> tuple[str, str]:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    WEB_DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT, 0o700)

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

    web_token = ensure_token(WEB_TOKEN)
    bridge_token = ensure_token(BRIDGE_TOKEN)
    return web_token, bridge_token


def main() -> None:
    web_token, bridge_token = init()
    gateway = Gateway()
    controller = Controller(gateway)
    gateway.start()
    print(f"[boot] gateway {gateway.version} started", flush=True)

    bridge = build_server(BRIDGE_PORT, ROOT / "workbuddy-status.json", bridge_token.encode())
    threading.Thread(target=bridge.serve_forever, daemon=True, name="quota-bridge").start()
    print(f"[boot] quota bridge on 0.0.0.0:{BRIDGE_PORT}/usage", flush=True)

    from web import build_web_server
    httpd = build_web_server(controller, web_token)
    print("=" * 56, flush=True)
    print(f"WEB_CONSOLE_URL = http://127.0.0.1:{os.environ.get('HOST_WEB_PORT', '8080')}/", flush=True)
    print(f"WEB_CONSOLE_USER = admin", flush=True)
    print("WEB_CONSOLE_TOKEN_FILE = /data/web/web.token", flush=True)
    print("=" * 56, flush=True)

    def stop(*_args):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        bridge.shutdown()
        gateway.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
