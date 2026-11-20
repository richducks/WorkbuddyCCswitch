"""Local client for the gateway's private manual check-in control socket."""
import http.client
import json
import socket
from urllib.parse import urlencode


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=50)
        self.socket_path = path

    def connect(self) -> None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock = conn
        conn.settimeout(self.timeout)
        conn.connect(self.socket_path)


def request_manual_checkin(socket_path, filename: str) -> str:
    conn = _UnixConnection(str(socket_path))
    try:
        conn.request("POST", "/checkin?" + urlencode({"file": filename}))
        response = conn.getresponse()
        if response.status == 409:
            raise ValueError("账号已不符合手动签到条件，请刷新状态")
        if response.status != 200:
            raise ValueError("签到暂不可用，请刷新状态后再操作")
        result = json.loads(response.read(4096))
    except FileNotFoundError:
        raise ValueError("网关手动签到接口未启动") from None
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        return "尚未确认，请稍后刷新状态"
    finally:
        conn.close()
    status = result.get("status") if isinstance(result, dict) else ""
    if not isinstance(status, str):
        status = ""
    return {"success": "已签到", "failed": "签到失败"}.get(status, "尚未确认，请稍后刷新状态")
