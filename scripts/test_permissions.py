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

    def build(self, password="123456"):
        return build_permissions_app(self.controller, self.root, bootstrap_password=password, bind_ip="127.0.0.1")

    def csrf(self, path):
        response = self.client.get(path)
        return re.search(r'name="csrf_token"[^>]+value="([^"]+)"', response.text)[1]

    def login(self, user, password):
        return self.client.post("/login/", data={
            "username": user, "password": password, "csrf_token": self.csrf("/login/"),
        })

    def change_password(self):
        return self.client.post("/initial-password", data={
            "current_password": "123456", "new_password": "A7b2C9",
            "csrf_token": self.csrf("/initial-password"),
        })

    def test_bootstrap_requires_password_change_and_survives_restart(self):
        self.assertEqual(self.client.get("/api/summary", headers={
            "Authorization": "Basic YWRtaW46Zml4dHVyZS10b2tlbg==",
        }).status_code, 401)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.login("admin", "123456").status_code, 302)
        self.assertTrue(self.client.get("/users/list/").location.endswith("/initial-password"))
        self.assertEqual(self.client.get("/api/summary").status_code, 403)
        wrong_password = self.client.post("/initial-password", data={
            "current_password": "incorrect", "new_password": "A7b2C9",
            "csrf_token": self.csrf("/initial-password"),
        })
        self.assertIn("当前密码错误", wrong_password.text)
        short_password = self.client.post("/initial-password", data={
            "current_password": "123456", "new_password": "A7b2C",
            "csrf_token": self.csrf("/initial-password"),
        })
        self.assertIn("6 至 256 字符", short_password.text)
        self.assertEqual(self.client.get("/api/summary").status_code, 403)
        self.assertEqual(self.change_password().status_code, 302)
        self.assertEqual(self.client.get("/users/list/").status_code, 200)
        self.assertEqual(self.client.get("/api/summary").status_code, 200)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
