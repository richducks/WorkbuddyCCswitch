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
from controller import Controller, mask  # noqa: E402

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
    check("controller: hermes endpoint built", c.hermes()["endpoint"].endswith("/v1"))
    check("controller: key masked in view", "k" * 40 not in json.dumps(c.status(), ensure_ascii=False))
    check("controller: failed catalog fetch cached briefly", c.model_catalog() == [])

page = (APP / "static" / "index.html").read_text(encoding="utf-8")
check("web: add-account button present", "添加微信账号" in page)
check("web: check-in controls present", "补签" in page and "checkinAllBtn" in page)
check("web: separate manual check-in action", 'class="sm manualCheckinBtn"' in page and '/api/checkin/manual' in page)
check("web: check-in status rendering present", "badge" in page and "今日签到" in page)
sidenav = page[page.index('id="sidenav"'):page.index("</nav>")]
nav_hrefs = re.findall(r'<a href="#(\w+)"', re.sub(r"<!--.*?-->", "", sidenav, flags=re.S))
check("web: navigation includes both account pools and all setup pages in order",
      nav_hrefs == ["acctCard", "checkinOnlyCard", "modelsCard", "hermesCard", "bridgeCard", "setupGuideCard"])
check("web: default tab is the first nav item",
      "activateTab(navLinks[0])" in page and nav_hrefs[:1] == ["acctCard"])

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
