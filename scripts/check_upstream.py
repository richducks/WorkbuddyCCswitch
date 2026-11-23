#!/usr/bin/env python3
"""Check whether the pinned WorkBuddy Gateway can safely move to a newer tag.

The Docker image pins an upstream commit because `gateway-overlay/` patches two
hot spots (check-in executor, account selection). This script answers the two
questions that gate an upgrade:

1. Is there a newer upstream tag than the pinned one?
2. Do both patches still apply cleanly to that source tree?

Usage:
  python3 scripts/check_upstream.py                 # 在线：列出比当前钉住版本更新的 tag
  python3 scripts/check_upstream.py --local ../workbuddy-gateway
                                                    # 离线：对本地上游源码做补丁试打
  python3 scripts/check_upstream.py --report /tmp/upgrade.md
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "gateway-overlay"
CHECKLIST = [
    "核对上游 `checkin.go` 的签到执行器、状态锁与重试计划是否仍与 manual_checkin 补丁一致",
    "核对上游 `main.go` 选号层级（免费耗尽 → 有余额 → 零余额探测）与 expire-first 补丁的注入点",
    "核对状态快照字段（`path`/`quotaKnown`/`quotaExpireAt`/`checkin.manualReady`）是否改名或改单位",
    "在本地运行 `python3 scripts/build_gateway.py`（含签到/调度 Go 测试）确认补丁通过",
    "用新二进制跑一次 `python3 -B scripts/selftest.py` 与容器冒烟（`/api/summary`、`/v1/models`、`/usage`）",
]


def fail(message: str) -> None:
    print(f"✗ {message}", file=sys.stderr)
    sys.exit(1)


def pinned() -> tuple[str, str, str]:
    """Read TAG/REVISION/UPSTREAM from build_gateway.py without importing it."""
    text = (ROOT / "scripts" / "build_gateway.py").read_text(encoding="utf-8")
    values = {}
    for name in ("TAG", "REVISION", "UPSTREAM"):
        match = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
        if not match:
            fail(f"build_gateway.py 里找不到 {name}")
        values[name] = match.group(1)
    return values["TAG"], values["REVISION"], values["UPSTREAM"]


def verkey(tag: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", tag)) or (0,)


def api(path: str) -> object:
    url = "https://api.github.com" + path
    request = urllib.request.Request(url, headers={"User-Agent": "wbcc-upgrade-check", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def newer_tags(upstream: str, tag: str, limit: int = 50) -> list[str]:
    owner_repo = re.sub(r"^https?://github\.com/|\.git$", "", upstream)
    rows = api(f"/repos/{owner_repo}/tags?per_page={limit}")
    tags = [row["name"] for row in rows if isinstance(row, dict) and "name" in row]
    return sorted((t for t in tags if verkey(t) > verkey(tag)), key=verkey, reverse=True)


def patch_report(source: Path) -> list[tuple[str, bool, str]]:
    """Dry-run every overlay patch against a local upstream tree."""
    results = []
    for patch_file in sorted(OVERLAY.glob("*.patch")):
        proc = subprocess.run(
            ["git", "apply", "--check", str(patch_file)], cwd=source,
            capture_output=True, text=True,
        )
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        results.append((patch_file.name, proc.returncode == 0, detail[0] if detail else ""))
    return results


def local_head(source: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True)
    if proc.returncode:
        fail(f"{source} 不是 git 仓库，无法核对上游提交")
    return proc.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--local", default="", help="本地上游源码目录（做补丁试打）")
    parser.add_argument("--report", default="", help="把报告写到文件（Markdown）")
    args = parser.parse_args()

    tag, revision, upstream = pinned()
    lines = [f"# 上游升级检查（当前钉住 {tag} / {revision[:12]}）", ""]
    status = 0

    if args.local:
        source = Path(args.local).expanduser().resolve()
        if not source.is_dir():
            fail(f"目录不存在：{source}")
        head = local_head(source)
        lines.append(f"- 本地上游 HEAD：`{head[:12]}` " + ("（与钉住提交一致）" if head == revision else "（与钉住提交不同，注意风险）"))
        lines.append("")
        lines.append("| 补丁 | 能否干净应用 | 说明 |")
        lines.append("|---|---|---|")
        for name, ok, detail in patch_report(source):
            lines.append(f"| `{name}` | {'✅ 可以' if ok else '❌ 冲突'} | {detail or '—'} |")
            if not ok:
                status = 1
        lines.append("")
        if status:
            lines.append("> 有补丁无法应用：先按 `gateway-overlay/` 的注释重写对应 hunk，再重建二进制。")
        else:
            lines.append("> 补丁可干净应用，仍需按下面的检查单逐项确认语义。")
    else:
        try:
            newer = newer_tags(upstream, tag)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            fail(f"无法查询上游 tag（{type(exc).__name__}）：离线时用 --local <上游源码目录>")
        if newer:
            lines.append(f"- 上游有 {len(newer)} 个更新的 tag，最新：`{newer[0]}`")
            lines.append("- 先用 `git clone --branch <tag> --depth 1` 取该 tag，再执行：")
            lines.append(f"  `python3 scripts/check_upstream.py --local <克隆目录>`")
        else:
            lines.append(f"- 上游没有比 `{tag}` 更新的 tag，无需升级。")

    lines += ["", "## 升级检查单", ""]
    lines += [f"{i}. {item}" for i, item in enumerate(CHECKLIST, 1)]
    lines += ["", "## 升级后更新钉住版本", "", "改 `scripts/build_gateway.py` 的 `TAG` 与 `REVISION`，再重建镜像。", ""]

    report = "\n".join(lines)
    print(report)
    if args.report:
        Path(args.report).expanduser().write_text(report, encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    sys.exit(status)


if __name__ == "__main__":
    main()
