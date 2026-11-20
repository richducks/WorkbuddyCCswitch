#!/usr/bin/env python3
"""Offline self-test: no credentials, no network, no real gateway touched."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from usage_bridge import payload  # noqa: E402
from checkin import candidates, queue, run_checkin, uid_key  # noqa: E402
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
doc = {"schema": 1, "date": today, "plans": {picked["workbuddy2.json"]: {
    "status": "missed", "attempts": 0, "lastAt": 0, "startupAt": 123}}}
queue(doc, picked, today)
p = doc["plans"][picked["workbuddy2.json"]]
check("checkin: plan becomes pending", p["status"] == "pending")
check("checkin: startupAt removed", "startupAt" not in p)
try:
    queue(doc, picked, today)
    check("checkin: refuses double queue", False)
except ValueError:
    check("checkin: refuses double queue", True)

check("mask: never shows full key", mask("abcdefghijklmnopqrstuvwxyz") == "abcd***wxyz")

class FakeGW:
    def __init__(self, root):
        self.root = root
        self.version = "v1.14.47"
        self.events = []

    def alive(self):
        return True

    def stop(self):
        self.events.append("stop")

    def start(self):
        self.events.append("start")
        p = self.root / "checkin-state.json"
        d = json.loads(p.read_text())
        for row in d["plans"].values():
            if row["status"] == "pending":
                row.update(status="success", attempts=1, lastAt=int(time.time() * 1000) + 2000)
        p.write_text(json.dumps(d))

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

    fake = FakeGW(root)
    key = uid_key("uid-test")
    (root / "workbuddy-status.json").write_text(json.dumps({
        "updatedAt": int(time.time()),
        "accounts": [{
            "path": str(root / "workbuddy.json"), "edition": "cn", "state": "active",
            "schedulingEnabled": True, "uid": "uid-test",
            "checkin": {"date": today, "status": "missed", "attempts": 0, "lastAt": 0},
        }],
    }))
    (root / "checkin-state.json").write_text(json.dumps({
        "schema": 1, "date": today,
        "plans": {key: {"status": "missed", "attempts": 0, "lastAt": 0}},
    }))
    for p in (root / "workbuddy-status.json", root / "checkin-state.json"):
        p.chmod(0o600)
    result = run_checkin(root, fake, ["workbuddy.json"], timeout=10)
    check("checkin: end-to-end result reported", result.get("workbuddy.json") == "已签到")
    check("checkin: gateway restarted exactly once", fake.events == ["stop", "start"])
    check("checkin: original state backed up", bool(list((root / ".manual-checkin-backups").glob("*.json"))))

page = (APP / "static" / "index.html").read_text(encoding="utf-8")
check("web: add-account button present", "添加微信账号" in page)
check("web: check-in controls present", "补签" in page and "checkinAllBtn" in page)
check("web: separate manual check-in action", 'class="sm manualCheckinBtn"' in page and '/api/checkin/manual' in page)
check("web: check-in status rendering present", "badge" in page and "今日签到" in page)

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
