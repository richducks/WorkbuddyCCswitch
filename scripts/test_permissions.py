#!/usr/bin/env python3
"""Exercise the actual FAB session and WorkBuddy authorization with isolated data."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from permissions import build_permissions_app


class PermissionsTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.controller = Mock()
        self.controller.gateway.alive.return_value = True
        self.controller.status.return_value = {
            "gatewayAlive": True,
            "hermes": {"hasKey": True, "keyMasked": "secret-fragment", "providerEntry": {}},
            "sessions": [{"sessionId": "sensitive"}],
        }
        self.app = self.build()
        self.client = self.app.test_client()

    def build(self, password="123456", bind_ip="127.0.0.1"):
        return build_permissions_app(self.controller, self.root, bootstrap_password=password, bind_ip=bind_ip)

    def csrf(self, path):
        response = self.client.get(path)
        return re.search(r'name="csrf_token"[^>]+value="([^"]+)"', response.text)[1]

    def login(self, user, password):
        return self.client.post("/login/", data={
            "username": user, "password": password, "csrf_token": self.csrf("/login/"),
        })

    def change_password(self, old="123456", new="A7b2C9"):
        return self.client.post("/initial-password", data={
            "current_password": old, "new_password": new,
            "csrf_token": self.csrf("/initial-password"),
        })

    def test_account_pages_use_simplified_chinese_with_a_previous_english_session(self):
        with self.client.session_transaction() as saved:
            saved["locale"] = "en"
        login = self.client.get("/login/")
        self.assertIn("用户名", login.text)
        self.assertIn("密码", login.text)
        self.assertNotIn(">Sign In<", login.text)
        self.login("admin", "123456")
        for path, labels in (
            ("/users/userinfo/", ("用户信息", "用户名", "重设密码")),
            ("/users/list/", ("用户列表", "用户名", "用户组", "取消")),
            ("/users/add", ("用于登录的密码", "停用账号即可禁止该用户登录")),
            ("/roles/list/", ("角色列表",)),
            ("/resetmypassword/form", ("密码", "确认密码")),
        ):
            response = self.client.get(path, follow_redirects=True)
            self.assertEqual(response.status_code, 200, path)
            for label in labels:
                self.assertIn(label, response.text, path)

    def test_default_seed_allows_open_box_login_and_flags_pending_password(self):
        # 开箱即用：没有 bootstrap 文件时 admin/123456 直接可用，API 不再强制阻断改密。
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.login("admin", "123456").status_code, 302)
        summary = self.client.get("/api/summary")
        self.assertEqual(summary.status_code, 200)
        self.assertTrue(summary.json["permissions"]["initialPassword"])
        console = self.client.get("/")
        self.assertIn("wb-initial-banner", console.text)
        self.assertIn("默认密码 123456", console.text)
        # 改密后标记消失，提示条不再注入。
        self.assertEqual(self.change_password().status_code, 302)
        summary = self.client.get("/api/summary")
        self.assertEqual(summary.status_code, 200)
        self.assertFalse(summary.json["permissions"]["initialPassword"])
        self.assertNotIn("wb-initial-banner", self.client.get("/").text)

    def test_password_change_validation_and_restart_persistence(self):
        self.assertEqual(self.client.get("/api/summary", headers={
            "Authorization": "Basic YWRtaW46Zml4dHVyZS10b2tlbg==",
        }).status_code, 401)
        self.assertEqual(self.login("admin", "123456").status_code, 302)
        wrong_password = self.change_password(old="incorrect")
        self.assertIn("当前密码错误", wrong_password.text)
        short_password = self.change_password(new="A7b2C")
        self.assertIn("6 至 256 字符", short_password.text)
        same_as_default = self.change_password(new="123456")
        self.assertIn("默认密码", same_as_default.text)
        # 失败的修改不消耗待改密标记。
        self.assertTrue(self.client.get("/api/summary").json["permissions"]["initialPassword"])
        self.assertEqual(self.change_password().status_code, 302)
        self.assertFalse(self.client.get("/api/summary").json["permissions"]["initialPassword"])
        # 改密后 CSRF/越权防护保持不变。
        csrf = re.search(r'name="wb-csrf" content="([^"]+)"', self.client.get("/").text)[1]
        body = {"file": "workbuddy2.json", "checkinOnly": True}
        self.assertEqual(self.client.post("/api/accounts/move", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/accounts/move", json=body, headers={
            "Origin": "http://cross-origin.invalid", "X-CSRFToken": csrf,
        }).status_code, 403)
        self.controller.set_account_pool.assert_not_called()
        self.assertEqual(self.client.post("/api/accounts/move", json=body, headers={
            "X-CSRFToken": csrf,
        }).status_code, 200)
        self.controller.set_account_pool.assert_called_once_with("workbuddy2.json", checkin_only=True)
        # 用同一数据目录重建应用实例（模拟重启）后：旧密码失效，新密码仍然有效。
        self.client = self.build(password=None).test_client()
        self.login("admin", "123456")
        self.assertEqual(self.client.get("/api/summary").status_code, 401)
        self.login("admin", "A7b2C9")
        self.assertEqual(self.client.get("/api/summary").status_code, 200)

    def test_read_only_rejects_mutations_and_applies_role_revocation(self):
        with self.app.app_context():
            sm = self.app.appbuilder.sm
            self.assertTrue(sm.add_user("reader", "Test", "Reader", "reader@example.invalid",
                                        sm.find_role("WorkBuddy Read Only"), password="fixture-reader"))
        self.login("reader", "fixture-reader")
        result = self.client.get("/api/summary")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["sessions"], [])
        self.assertNotIn("secret-fragment", result.text)
        self.assertEqual(self.client.get("/api/session?id=test").status_code, 403)
        self.assertEqual(self.client.get("/api/hermes/key").status_code, 403)
        csrf = re.search(r'name="wb-csrf" content="([^"]+)"', self.client.get("/").text)[1]
        for route in ("move", "promote", "remove"):
            response = self.client.post("/api/accounts/" + route, json={"file": "workbuddy2.json"},
                                        headers={"X-CSRFToken": csrf})
            self.assertEqual(response.status_code, 403)
        self.controller.set_account_pool.assert_not_called()
        response = self.client.post("/api/checkin/settings", json={"file": "workbuddy2.json", "enabled": False},
                                    headers={"X-CSRFToken": csrf})
        self.assertEqual(response.status_code, 403)
        self.controller.set_account_auto_checkin.assert_not_called()
        self.controller.usage_snapshot.return_value = {"available": False, "hint": "missing"}
        self.assertFalse(result.json["permissions"]["usage_refresh"])
        self.assertEqual(self.client.get("/api/usage").status_code, 200)
        response = self.client.post("/api/usage/refresh", json={}, headers={"X-CSRFToken": csrf})
        self.assertEqual(response.status_code, 403)
        self.controller.usage_refresh.assert_not_called()
        self.assertEqual(self.change_password("fixture-reader", "AnotherReader123").status_code, 302)
        with self.assertRaisesRegex(RuntimeError, "修改初始密码"):
            self.build(bind_ip="192.168.1.2")
        with self.app.app_context():
            sm = self.app.appbuilder.sm
            role = sm.find_role("WorkBuddy Read Only")
            sm.del_permission_role(role, sm.find_permission_view_menu("can_status", "WorkBuddy"))
        self.assertEqual(self.client.get("/api/summary").status_code, 403)
        with self.app.app_context():
            sm = self.app.appbuilder.sm
            reader = sm.find_user(username="reader")
            reader.active = False
            sm.update_user(reader)
        self.assertEqual(self.client.get("/api/summary").status_code, 401)

    def test_weak_bootstrap_password_cannot_be_used_on_lan(self):
        with self.assertRaisesRegex(RuntimeError, "先在本机修改初始密码"):
            build_permissions_app(self.controller, self.root, bootstrap_password=None, bind_ip="192.168.1.2")

    def test_default_password_refused_on_fresh_lan_install(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        with self.assertRaisesRegex(RuntimeError, "permissions-bootstrap"):
            build_permissions_app(self.controller, Path(work.name), bootstrap_password=None, bind_ip="192.168.1.2")

    def test_strong_bootstrap_password_allows_fresh_lan_install(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        app = build_permissions_app(self.controller, Path(work.name),
                                    bootstrap_password="L0calOnly!L0calOnly", bind_ip="192.168.1.2")
        client = app.test_client()
        page = client.get("/login/")
        token = re.search(r'name="csrf_token"[^>]+value="([^"]+)"', page.text)[1]
        self.assertEqual(client.post("/login/", data={
            "username": "admin", "password": "L0calOnly!L0calOnly", "csrf_token": token,
        }).status_code, 302)
        summary = client.get("/api/summary")
        self.assertEqual(summary.status_code, 200)
        # 自定义 bootstrap 密码不是默认密码，不应出现待改密标记。
        self.assertFalse(summary.json["permissions"]["initialPassword"])
        self.assertNotIn("wb-initial-banner", client.get("/").text)

    def test_usage_endpoint_is_registered_and_session_gated(self):
        # /api/usage 必须注册在 PERMISSIONS，否则只读角色访问会 404（历史上 /api/pins 踩过这个坑）。
        self.controller.usage_snapshot.return_value = {"available": False, "hint": "missing"}
        self.assertEqual(self.client.get("/api/usage").status_code, 401)
        self.assertEqual(self.controller.usage_snapshot.call_count, 0)
        self.login("admin", "123456")
        response = self.client.get("/api/usage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"available": False, "hint": "missing"})
        self.assertEqual(self.controller.usage_snapshot.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
