"""Named Hermes provider presets. The console never connects to custom URLs."""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import threading
from pathlib import Path
from urllib.parse import urlsplit

from checkin import atomic, read_private

_ID = re.compile(r"[a-z][a-z0-9-]{0,30}")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,99}")
_FIELDS = {"id", "name", "url", "transport", "defaultModel", "models", "keyEnv"}
_TRANSPORTS = {"chat_completions", "anthropic_messages", "codex_responses"}
_MAX_PROVIDERS = 50


def _text(value: object, label: str, limit: int, *, optional: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label}格式无效")
    value = value.strip()
    if not value and not optional:
        raise ValueError(f"请填写{label}")
    return value


def _identity(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("标识须以小写字母开头，只能包含小写字母、数字和连字符，最多 31 字符")
    return value


def _endpoint(value: object, key_env: str) -> str:
    url = _text(value, "API 地址", 500)
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("API 地址无效") from None
    if (u.scheme not in ("http", "https") or not u.hostname or u.username is not None
            or "?" in url or "#" in url or "\\" in url or any(c.isspace() for c in url)
            or port == 0):
        raise ValueError("API 地址必须是无用户名、密码、查询或片段的 HTTP(S) Base URL")
    if u.scheme == "http" and key_env:
        host = u.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            raise ValueError("有 API Key 的非本机服务必须使用 HTTPS")
    return url.rstrip("/")


def _validate(data: object) -> dict:
    if not isinstance(data, dict) or data.keys() - _FIELDS:
        raise ValueError("提供商配置包含不支持的字段；请勿填写密钥正文")
    pid = _identity(data.get("id"))
    name = _text(data.get("name"), "显示名称", 64)
    wire = _text(data.get("transport"), "API 协议", 32)
    if wire not in _TRANSPORTS:
        raise ValueError("不支持的 API 协议")
    key_env = _text(data.get("keyEnv", ""), "密钥变量名", 100, optional=True)
    if key_env and not _ENV.fullmatch(key_env):
        raise ValueError("只填写密钥环境变量名，不填写实际密钥")
    default = _text(data.get("defaultModel"), "默认模型 ID", 200)
    models = data.get("models", [])
    if not isinstance(models, list) or len(models) > 100:
        raise ValueError("额外模型最多 100 个")
    extra = list(dict.fromkeys(_text(item, "模型 ID", 200) for item in models))
    return {"id": pid, "name": name, "url": _endpoint(data.get("url"), key_env), "transport": wire,
            "defaultModel": default, "models": [item for item in extra if item != default], "keyEnv": key_env}


def _view(row: dict) -> dict:
    ident = "wbx-" + row["id"]
    entry = {"name": row["name"], "base_url": row["url"], "transport": row["transport"],
             "default_model": row["defaultModel"], "models": [row["defaultModel"], *row["models"]]}
    if row["keyEnv"]:
        entry["key_env"] = row["keyEnv"]
    command = shlex.join(["hermes", "config", "set", "--force", f"providers.{ident}",
                          json.dumps(entry, ensure_ascii=False)])
    return {**row, "provider": f"custom:{ident}", "command": command, "entry": entry}


class ProviderRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            doc = json.loads(read_private(self.path))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError("提供商配置文件不可读，未执行改动") from None
        if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("providers"), list):
            raise ValueError("提供商配置文件格式异常，未执行改动")
        rows = [_validate(row) for row in doc["providers"]]
        by_id = {row["id"]: row for row in rows}
        if len(by_id) != len(rows) or len(rows) > _MAX_PROVIDERS:
            raise ValueError("提供商配置文件存在重复或超限，未执行改动")
        return by_id

    def _write(self, rows: dict[str, dict]) -> None:
        atomic(self.path, (json.dumps({"version": 1, "providers": list(rows.values())},
                                      ensure_ascii=False, indent=2) + "\n").encode())

    def list(self) -> list[dict]:
        with self._lock:
            return [_view(row) for row in self._read().values()]

    def save(self, data: object, *, create: bool) -> dict:
        row = _validate(data)
        pid = row["id"]
        with self._lock:
            rows = self._read()
            if create and pid in rows:
                raise ValueError("该提供商标识已存在")
            if not create and pid not in rows:
                raise ValueError("提供商不存在，请刷新")
            if create and len(rows) >= _MAX_PROVIDERS:
                raise ValueError("提供商数量已达上限")
            rows[pid] = row
            self._write(rows)
        return _view(row)

    def remove(self, identity: object) -> None:
        pid = _identity(identity)
        with self._lock:
            rows = self._read()
            if pid not in rows:
                raise ValueError("提供商不存在，请刷新")
            del rows[pid]
            self._write(rows)
