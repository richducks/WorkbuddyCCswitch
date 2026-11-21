#!/usr/bin/env python3
"""把 Docker 网关当前的 /v1/models 同步到宿主机 Hermes 的 workbuddy provider。

仅在宿主机运行；先备份 config.yaml，只替换 providers.workbuddy.models，
不改动默认模型、其他 provider，也不触碰任何 API Key。
"""
import argparse
import json
import os
import shutil
import urllib.request
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "data/gateway/config.json"
HERMES = Path(os.environ.get("HERMES_CONFIG_PATH", str(Path.home() / ".hermes/config.yaml")))


def fetch_models(host: IPv4Address, port: int) -> list[dict]:
    cfg = json.loads(KEY_FILE.read_text())
    key = str((cfg.get("gateway") or {}).get("apiKey") or "")
    if not key:
        raise SystemExit("无法从 data/gateway/config.json 读取 apiKey")
    req = urllib.request.Request(f"http://{host}:{port}/v1/models")
    req.add_unredirected_header("Authorization", "Bearer " + key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as resp:
        items = json.load(resp).get("data") or []
    rows: list[dict] = []
    for item in items:
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        caps = ((item.get("capabilities_by_site") or {}).get("cn") or {})
        row: dict[str, object] = {"id": mid}
        for field in ("context_length", "max_output_tokens"):
            value = caps.get(field, item.get(field, item.get("default_" + field)))
            if type(value) is int and value > 0:
                row[field] = value
        rows.append(row)
    if not rows:
        raise SystemExit("网关未返回任何模型，配置未改动")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="同步本机 WorkBuddy 网关的模型目录到 Hermes")
    parser.add_argument("--gateway-host", type=IPv4Address, default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=os.environ.get("HOST_GW_PORT", "8317"))
    args = parser.parse_args()
    if args.gateway_host.is_unspecified or not args.gateway_host.is_private:
        parser.error("网关地址必须是本机回环或专用局域网 IPv4 地址")
    if not 1 <= args.gateway_port <= 65535:
        parser.error("网关端口必须为 1–65535")
    try:
        import yaml
    except ImportError:
        raise SystemExit("需要 PyYAML：python3 -m pip install pyyaml")
    rows = fetch_models(args.gateway_host, args.gateway_port)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkdir = ROOT / "backup"
    bkdir.mkdir(mode=0o700, exist_ok=True)
    bak = bkdir / f"{stamp}-hermes-config.yaml"
    shutil.copy2(HERMES, bak)
    os.chmod(bak, 0o600)

    doc = yaml.safe_load(HERMES.read_text()) or {}
    providers = doc.setdefault("providers", {})
    workbuddy = providers.get("workbuddy")
    if not isinstance(workbuddy, dict):
        raise SystemExit("Hermes 配置中没有 providers.workbuddy，未做修改")
    workbuddy["models"] = rows
    tmp = HERMES.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, HERMES)

    print(f"已同步 {len(rows)} 个模型到 Hermes providers.workbuddy.models")
    print("默认模型保持不变。")
    print(f"备份：{bak}")
    print("CC Switch 在启动时导入模型列表，重启 CC Switch 后新模型才会出现在下拉框。")
    print("本次只改 config.yaml；~/.hermes/.env 与任何 API Key 均未改动。")


if __name__ == "__main__":
    main()
