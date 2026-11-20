#!/usr/bin/env python3
"""把 Docker 网关当前的 /v1/models 同步到宿主机 Hermes 的 workbuddy provider。

仅在宿主机运行；先备份 config.yaml，只替换 providers.workbuddy.models，
不改动默认模型、其他 provider，也不触碰任何 API Key。
"""
import json
import os
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "data/gateway/config.json"
HERMES = Path(os.environ.get("HERMES_CONFIG_PATH", str(Path.home() / ".hermes/config.yaml")))
PORT = int(os.environ.get("HOST_GW_PORT", "8317"))


def fetch_models() -> list[dict]:
    cfg = json.loads(KEY_FILE.read_text())
    key = str((cfg.get("gateway") or {}).get("apiKey") or "")
    if not key:
        raise SystemExit("无法从 data/gateway/config.json 读取 apiKey")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/models",
        headers={"Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        items = json.loads(resp.read().decode()).get("data") or []
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
    try:
        import yaml
    except ImportError:
        raise SystemExit("需要 PyYAML：python3 -m pip install pyyaml")
    rows = fetch_models()
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
    print(f"默认模型保持不变：{doc.get('model')} / {workbuddy.get('default_model')}")
    print(f"备份：{bak}")
    print("CC Switch 在启动时导入模型列表，重启 CC Switch 后新模型才会出现在下拉框。")
    print("本次只改 config.yaml；~/.hermes/.env 与任何 API Key 均未改动。")


if __name__ == "__main__":
    main()
