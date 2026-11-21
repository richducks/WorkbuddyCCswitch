#!/usr/bin/env python3
"""Build the pinned WorkBuddy Gateway with the local overlay for the Docker image.

Works on Linux, macOS and Windows (needs git + Go 1.26+; set GO=/path/to/go when
go is not on PATH). Tests run on the host platform; the produced binary always
targets linux/<host-arch> because it runs inside the Docker container.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REVISION = "e142dd7640fc5e87541a3e5a51d3ae2e52c4317e"
UPSTREAM = "https://github.com/CangShui/workbuddy-gateway.git"
TAG = "v1.14.47"
ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def fail(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode:
        detail = "\n".join(
            line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()
        )[-1500:]
        fail(f"`{' '.join(cmd)}` 失败\n{detail}")
    return proc.stdout


def goarch() -> str:
    return ARCH_MAP.get(platform.machine().lower(), "")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--go", default="", help="Go 可执行文件路径（默认取 PATH 中的 go）")
    parser.add_argument("--goarch", default="", choices=["", "amd64", "arm64"],
                        help="覆盖容器目标架构（默认跟随本机）")
    args = parser.parse_args()

    go_cmd = args.go or os.environ.get("GO") or shutil.which("go")
    if not go_cmd:
        fail("未找到 go：请安装 Go 1.26+，或用 --go / 环境变量 GO 指定 go 可执行文件")
    if not shutil.which("git"):
        fail("未找到 git，请先安装")
    target_arch = args.goarch or goarch()
    if not target_arch:
        fail(f"本机架构 {platform.machine()} 不受支持；用 --goarch amd64|arm64 显式指定")

    root = Path(__file__).resolve().parent.parent
    overlay_dir = root / "gateway-overlay"
    patch = overlay_dir / "scheduler.patch"
    overlay_go = sorted(overlay_dir.glob("*.go"))
    if not patch.is_file() or not overlay_go:
        fail(f"overlay 不完整：{overlay_dir}")

    tmp = Path(tempfile.mkdtemp(prefix="wbcc-build-"))
    try:
        src = tmp / "src"
        sh(["git", "clone", "--quiet", "--depth", "1", "--branch", TAG, "--", UPSTREAM, str(src)])
        if sh(["git", "rev-parse", "HEAD"], cwd=src).strip() != REVISION:
            fail(f"上游 {TAG} HEAD 与固定提交 {REVISION[:12]} 不符，拒绝构建")
        sh(["git", "apply", str(patch)], cwd=src)
        expire_patch = overlay_dir / "expire-first.patch"
        if not expire_patch.is_file():
            fail(f"overlay 不完整：缺少 {expire_patch}")
        sh(["git", "apply", str(expire_patch)], cwd=src)
        for f in overlay_go:
            shutil.copy(f, src / f.name)

        # Tests execute on the host platform so they actually run on macOS/Windows too.
        test_env = os.environ.copy()
        test_env.update({"GOTOOLCHAIN": "local", "CGO_ENABLED": "0"})
        print("Running check-in and scheduling tests")
        sh([str(go_cmd), "test", "-run", r"Checkin|CheckIn|ExpireFirst|EarliestQuotaEnd", "."], cwd=src, env=test_env)

        build_env = os.environ.copy()
        build_env.update({"GOTOOLCHAIN": "local", "CGO_ENABLED": "0",
                          "GOOS": "linux", "GOARCH": target_arch})
        out_dir = root / "bin"
        out_dir.mkdir(exist_ok=True)
        candidate = out_dir / ".manual-gateway-tmp"
        print(f"Building linux/{target_arch} gateway binary")
        sh([str(go_cmd), "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w",
            "-o", str(candidate), "."], cwd=src, env=build_env)
        candidate.chmod(0o755)
        final = out_dir / "workbuddy-gateway-manual"
        if final.exists():
            final.unlink()
        candidate.replace(final)
        print(f"OK: {final} (linux/{target_arch})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
