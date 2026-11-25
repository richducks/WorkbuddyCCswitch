"""Business actions shared by the legacy and role-based console adapters."""

from controller import Controller


def dispatch_console_api(c: Controller, method: str, path: str, query: dict, data: dict) -> tuple[int, dict]:
    if method == "GET":
        if path == "/api/summary":
            return 200, c.status()
        if path == "/api/session":
            sid = query.get("id", [""])[0]
            return 200, c.get_session(sid)
        if path == "/api/hermes/key":
            return 200, {"apiKey": c.api_key()}
        if path == "/api/providers":
            registry = c.provider_registry
            return 200, {"providers": registry.list()}
    elif method == "POST":
        if path in ("/api/providers/add", "/api/providers/edit"):
            return 200, {"provider": c.provider_registry.save(data, create=path.endswith("/add"))}
        if path == "/api/providers/remove":
            if not isinstance(data, dict) or data.keys() != {"id"}:
                raise ValueError("请选择要移除的提供商")
            c.provider_registry.remove(data["id"])
            return 200, {"ok": True}
        if path == "/api/accounts":
            checkin_only = data.get("checkinOnly", False)
            if not isinstance(checkin_only, bool):
                raise ValueError("账号用途参数必须为布尔值")
            return 200, c.add_account(checkin_only=checkin_only)
        if path in ("/api/accounts/move", "/api/accounts/promote"):
            checkin_only = False if path.endswith("/promote") else data.get("checkinOnly")
            if not isinstance(checkin_only, bool):
                raise ValueError("请选择目标账号池")
            c.set_account_pool(str(data.get("file", "")), checkin_only=checkin_only)
            return 200, {"ok": True}
        if path == "/api/session/cancel":
            c.cancel_session(str(data.get("sessionId", "")))
            return 200, {"ok": True}
        if path == "/api/accounts/remove":
            c.remove_account(str(data.get("file", "")))
            return 200, {"ok": True}
        if path == "/api/labels":
            c.set_label(str(data.get("file", "")), str(data.get("label", "")))
            return 200, {"ok": True}
        if path == "/api/checkin/manual":
            name = data.get("file")
            if not isinstance(name, str):
                raise ValueError("请选择账号")
            return 200, {"result": c.manual_checkin(name)}
        if path == "/api/checkin/all":
            return 200, {"results": c.checkin_all()}
        if path == "/api/checkin/settings":
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("自动签到开关必须为布尔值")
            return 200, {"enabled": c.set_auto_checkin(enabled)}
        if path == "/api/checkin":
            files = data.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("请选择账号")
            return 200, {"results": c.checkin_all([str(x) for x in files])}
    return 404, {"error": "not found"}
