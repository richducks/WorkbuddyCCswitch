#!/usr/bin/env python3
"""Offline self-test: no credentials, no network, no real gateway touched."""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from usage_bridge import payload  # noqa: E402
from checkin import candidates  # noqa: E402
from controller import Controller, expiry_windows, mask  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


now = time.time()
today = time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600))
state = {
    "updatedAt": int(now),
    "accounts": [
        {
            "path": "/data/gateway/workbuddy.json", "edition": "cn", "state": "active",
            "schedulingEnabled": True, "uid": "uid-A", "quotaTotal": 1000,
            "quotaUsed": 120.5, "quotaRemaining": 879.5, "quotaKnown": True,
            "planLabel": "个人版",
            "checkin": {"date": today, "status": "success", "attempts": 1, "lastAt": int(now * 1000), "message": "ok"},
        },
        {
            "path": "/data/gateway/workbuddy2.json", "edition": "cn", "state": "active",
            "schedulingEnabled": True, "uid": "uid-B", "quotaTotal": 2000,
            "quotaUsed": 0, "quotaRemaining": 2000, "quotaKnown": True,
            "planLabel": "个人版",
            "checkin": {"date": today, "status": "missed", "attempts": 0, "lastAt": 0, "message": ""},
        },
        {
            "path": "/data/gateway/workbuddy3.json", "edition": "intl", "state": "active",
            "schedulingEnabled": True, "uid": "uid-C", "quotaKnown": False,
            "checkin": {"date": today, "status": "missed", "attempts": 0},
        },
    ],
}
rows = payload(state, now)
check("bridge: one row per account", len(rows["entries"]) == 3)
check("bridge: stable alias from credential file", rows["entries"][0]["planName"] == "workbuddy.json")
check("bridge: per-account quota not merged", rows["entries"][1]["remaining"] == 2000)
check("bridge: today's check-in status shown", "已签到" in rows["entries"][0]["extra"])
check("bridge: missed account shown", "未签到" in rows["entries"][1]["extra"])
check("bridge: unknown quota marked invalid", rows["entries"][2]["isValid"] is False)
stale = payload({**state, "updatedAt": int(now) - 600}, now)
check("bridge: stale snapshot disables success claim", "未确认" in stale["entries"][0]["extra"])

# 积分到期窗口：只统计仍可用额度，3 天/7 天分层，未知到期不夸大
expiring = expiry_windows([
    {"expireAt": int(now) + 2 * 86400, "quota": {"remaining": 500}},
    {"expireAt": int(now) + 5 * 86400, "quota": {"remaining": 200}},
    {"expireAt": int(now) + 30 * 86400, "quota": {"remaining": 9000}},
    {"expireAt": None, "quota": {"remaining": 77}},
])
check("expiry: 3-day window sums only near-term quota", expiring["within3d"] == {"quota": 500.0, "accounts": 1})
check("expiry: 7-day window adds the 5-day account", expiring["within7d"] == {"quota": 700.0, "accounts": 2})
check("expiry: earliest end reported", expiring["earliestAt"] == int(now) + 2 * 86400)
check("expiry: unknown/inactive accounts ignored", expiry_windows(
    [{"expireAt": 0, "quota": {"remaining": 5}}, {"expireAt": int(now) + 86400, "quota": {"remaining": 0}}]
)["within7d"] == {"quota": 0.0, "accounts": 0})

by_file = {Path(a["path"]).name: a for a in state["accounts"]}
picked = candidates(by_file, list(by_file), today)
check("checkin: only eligible account is queued", list(picked) == ["workbuddy2.json"])
try:
    dup = {**by_file, "workbuddy4.json": {**by_file["workbuddy2.json"], "uid": "uid-B"}}
    candidates(dup, list(dup), today)
    check("checkin: duplicate uid refused", False)
except ValueError:
    check("checkin: duplicate uid refused", True)

check("mask: never shows full key", mask("abcdefghijklmnopqrstuvwxyz") == "abcd***wxyz")


class FakeProc:
    def __init__(self, alive):
        self._alive = alive
        self.returncode = None if alive else 1

    def poll(self):
        return None if self._alive else 1


def supervisor_restarts_crashed_child():
    import controller as ctrl
    gw = ctrl.Gateway.__new__(ctrl.Gateway)
    gw.proc = FakeProc(False)
    gw._log = None
    gw._lock = threading.Lock()
    gw._shutdown = False
    spawned = []

    def spawn():
        spawned.append(1)
        gw.proc = FakeProc(True)
        gw._shutdown = True

    gw._spawn = spawn
    gw.supervise(poll_seconds=0.05)
    return spawned


check("gateway: supervisor restarts crashed child", supervisor_restarts_crashed_child() == [1])

class FakeGW:
    def __init__(self, root):
        self.root = root
        self.version = "v1.14.47"

    def alive(self):
        return True


import controller as ctrl  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    ctrl.ROOT = root
    ctrl.WEB_DATA = root / "web"
    (root / "workbuddy.json").write_text("{}")
    (root / "workbuddy2.json").write_text("{}")
    c = Controller(FakeGW(root))
    check("controller: next free credential filename", c.next_filename() == "workbuddy3.json")
    c.set_label("workbuddy.json", "我的主号")
    check("controller: custom label stored", c.labels()["workbuddy.json"] == "我的主号")
    (root / "config.json").write_text(json.dumps({"gateway": {"apiKey": "k" * 40}}))
    (root / "workbuddy-status.json").write_text(json.dumps({
        "updatedAt": int(now),
        "accounts": [{
            "path": str(root / "workbuddy.json"), "edition": "cn", "state": "active",
            "schedulingEnabled": True, "uid": "uid-A", "quotaTotal": 100, "quotaUsed": 10,
            "quotaRemaining": 90, "quotaKnown": True, "quotaExpireAt": int(now) + 86400,
            "checkin": {"date": today, "status": "success", "attempts": 1},
        }],
    }, ensure_ascii=False))
    check("controller: hermes endpoint built", c.hermes()["endpoint"].endswith("/v1"))
    check("controller: key masked in view", "k" * 40 not in json.dumps(c.status(), ensure_ascii=False))
    check("controller: failed catalog fetch cached briefly", c.model_catalog() == [])
    status = c.status()
    check("controller: quota expiry exposed per account", status["accounts"][0]["expireAt"] == int(now) + 86400)
    check("controller: expiry remaining days computed", status["accounts"][0]["expireIn"] == 1)
    check("controller: expiry window summarised", status["expiry"]["within3d"] == {"quota": 90.0, "accounts": 1})

page = (APP / "static" / "index.html").read_text(encoding="utf-8")
check("web: add-account button present", "添加微信账号" in page)
check("web: check-in controls present", "补签" in page and "checkinAllBtn" in page)
check("web: separate manual check-in action", 'class="sm manualCheckinBtn"' in page and '/api/checkin/manual' in page)
check("web: check-in status rendering present", "badge" in page and "今日签到" in page)

nav = page.split('<nav class="nav"', 1)[-1].split("</nav>", 1)[0]
check("web: sidebar order is accounts → models → usage",
      re.findall(r'class="nav-item[^"]*"[^>]*data-panel="([a-z]+)"', nav) == ["accounts", "models", "usage"])
check("web: first nav item is the default tab", 'class="nav-item is-active"' in nav.split("</button>", 1)[0])
check("web: default panel shown, others hidden",
      'id="panel-accounts" data-panel="accounts">' in page
      and 'id="panel-models" data-panel="models" hidden' in page
      and 'id="panel-usage" data-panel="usage" hidden' in page)
check("web: sidebar collapses to chips below 920px", "@media (max-width: 920px)" in page and "overflow-x: auto" in page)
check("web: expiry warnings rendered", "expiryNote" in page and 'id="expiryBar"' in page and "b-num" in page)
check("web: provider JSON collapsed by default", '<details class="json">' in page)
check("web: provider presets stay hidden pending the product decision", 'id="providersCard" hidden' in page)
check("web: writes carry the CSRF token", "X-CSRF-Token" in page and "wb_csrf" in page)
check("web: expired session sends the browser to the login page", "loginRedirect" in page and "/login" in page)

login = (APP / "static" / "login.html").read_text(encoding="utf-8")
check("login: password form posts to /login", 'action="/login"' in login and 'type="password"' in login)
check("login: form is self-contained (no inline script)", "<script" not in login)

compose = (APP.parent / "docker-compose.yml").read_text(encoding="utf-8")
check("compose: console authentication cannot be disabled by an env flag", "NOAUTH" not in compose)

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
