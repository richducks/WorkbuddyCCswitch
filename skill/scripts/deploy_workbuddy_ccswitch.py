#!/usr/bin/env python3
"""Deploy or verify the WorkbuddyCCswitch Docker stack on a Linux host.

Runs only local commands declared here (git/docker/python3/ssh); it never
sends any token, credential or private data anywhere.
"""
from __future__ import annotations

from typing import NoReturn

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

REPO = "https://github.com/richducks/WorkbuddyCCswitch.git"
BRANCH = "main"
CONTAINER = "workbuddy-hermes"


def fail(msg: str) -> NoReturn:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def sh(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        detail = "\n".join(
            line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()
        )[-1500:]
        fail(f"`{' '.join(cmd)}` 失败\n{detail}")
    return proc.stdout


def compose(repo_dir: Path, *args: str) -> str:
    return sh(["docker", "compose", *args], cwd=repo_dir, timeout=900)


def env_value(repo_dir: Path, key: str) -> str:
    env = repo_dir / ".env"
    text = env.read_text() if env.exists() else (repo_dir / ".env.example").read_text()
    for line in text.splitlines():
        if line.strip().startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
            break
    fail(f".env 缺少 {key}，请从 .env.example 重新生成")


def port_free(local_port: int) -> bool:
    try:
        with socket.create_server(("127.0.0.1", local_port)):
            return True
    except OSError:
        return False


def container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def require_ports_free(repo_dir: Path) -> None:
    keys = ("HOST_WEB_PORT", "HOST_GW_PORT", "HOST_BRIDGE_PORT")
    taken = [k for k in keys if not port_free(int(env_value(repo_dir, k)))]
    if taken:
        fail("端口已被占用：" + ", ".join(
            f"{k}={env_value(repo_dir, k)}" for k in taken) +
            "；请在 .env 中改用空闲端口")


def cmd_deploy(args) -> None:
    if not (shutil.which("docker") and shutil.which("git")):
        fail("需要先安装 docker（含 compose 插件）和 git")
    repo_dir = Path(args.dir).resolve()
    if repo_dir.exists():
        if not (repo_dir / ".git").is_dir():
            fail(f"{repo_dir} 已存在但不是 git 仓库；请换 --dir 或先手动清理")
        sh(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", BRANCH])
    else:
        print(f"Cloning {REPO} ({BRANCH}) into {repo_dir}")
        sh(["git", "clone", "--branch", BRANCH, "--", REPO, str(repo_dir)], timeout=300)
    head = sh(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).strip()
    print(f"Checked out {BRANCH} @ {head[:12]}")
    if not (shutil.which("go") or os.environ.get("GO")):
        fail("构建网关需要 Go 1.26+ 工具链；请先安装 Go，或用 GO=/path/to/go 指定")
    if not (repo_dir / "bin/workbuddy-gateway-manual").exists():
        print("Building pinned gateway binary (first run downloads upstream source)")
        sh(["bash", "scripts/build-manual-gateway.sh"], cwd=repo_dir, timeout=1800)
    if not (repo_dir / ".env").exists():
        shutil.copy(repo_dir / ".env.example", repo_dir / ".env")
        print("已从 .env.example 生成 .env；如需修改 UID/GID 或端口请先编辑")
    compose(repo_dir, "config", "--quiet")
    require_ports_free(repo_dir)
    print("Building image and starting stack")
    compose(repo_dir, "up", "-d", "--build")
    deadline = time.time() + 180
    while time.time() < deadline:
        if "(healthy)" in compose(repo_dir, "ps", "--format", "{{.Status}}"):
            break
        time.sleep(5)
    else:
        fail("容器 180 秒内未进入 healthy；请运行 docker compose logs 查看原因")
    print(compose(repo_dir, "ps", "--format", "{{.Name}} {{.Status}}").strip())
    print(f"管理网页：http://127.0.0.1:{env_value(repo_dir, 'HOST_WEB_PORT')}/"
          "（用户名 admin，密码在容器内 /data/web/web.token）")
    print("下一步：docker compose exec workbuddy-hermes cat /data/web/web.token 查看网页密码；"
          "再按 README 或 skill/references/remote-hermes.md 配置 Hermes 对接")


def cmd_verify(args) -> None:
    repo_dir = Path(args.dir).resolve()
    if not (repo_dir / "docker-compose.yml").is_file():
        fail(f"{repo_dir} 不是项目目录")
    rows = [line for line in compose(repo_dir, "ps", "--format", "{{.Name}} {{.Status}}").splitlines() if line.strip()]
    if not rows:
        fail("容器未运行（docker compose ps 为空）")
    unhealthy = [line for line in rows if "(healthy)" not in line]
    if unhealthy:
        fail("容器状态异常：" + "；".join(unhealthy))
    web = int(env_value(repo_dir, "HOST_WEB_PORT"))
    try:
        urlopen(f"http://127.0.0.1:{web}/api/providers", timeout=5)
    except HTTPError as e:
        if e.code != 401:
            fail(f"console check failed: HTTP {e.code}")
    except OSError as e:
        fail(f"console check failed: {e}")
    else:
        fail("网页未要求鉴权——配置异常，请检查 .env 与 compose")
    print(f"PASS: 容器 healthy，网页 {web} 鉴权正常（401 for unauthenticated）")


def cmd_tunnel(args) -> None:
    if not port_free(args.port):
        fail(f"本地端口 {args.port} 已被占用")
    if shutil.which("ssh") is None:
        fail("需要 openssh-client")
    cmd = ["ssh", "-NT", "-o", "ExitOnForwardFailure=yes",
           "-L", f"127.0.0.1:{args.port}:127.0.0.1:{args.server_gw}",
           "-L", f"127.0.0.1:{args.port + 100}:127.0.0.1:{args.server_web}"]
    if args.server_bridge:
        cmd += ["-L", f"127.0.0.1:{args.port + 1}:127.0.0.1:{args.server_bridge}"]
    cmd.append(args.ssh_target)
    print(f"隧道映射：本地 :{args.port} → 服务器网关 API，:{args.port + 100} → 服务器网页")
    print(f"Hermes Base URL: http://127.0.0.1:{args.port}/v1")
    try:
        os.execvp("ssh", cmd)
    except OSError as e:
        fail(f"无法启动 ssh：{e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "WorkbuddyCCswitch deployment helper").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    deploy = sub.add_parser("deploy", help="克隆、构建并启动 Docker 栈")
    deploy.add_argument("--dir", default="./WorkbuddyCCswitch")
    deploy.set_defaults(func=cmd_deploy)

    verify = sub.add_parser("verify", help="检查本机容器健康与网页鉴权")
    verify.add_argument("--dir", default="./WorkbuddyCCswitch")
    verify.set_defaults(func=cmd_verify)

    tunnel = sub.add_parser("tunnel", help="为本地 Hermes 打通到服务器部署的 SSH 隧道")
    tunnel.add_argument("ssh_target", help="user@host")
    tunnel.add_argument("--port", type=int, default=18317, help="本地网关端口")
    tunnel.add_argument("--server-gw", type=int, default=8317)
    tunnel.add_argument("--server-web", type=int, default=8080)
    tunnel.add_argument("--server-bridge", type=int, default=0, help="0 表示不转发")
    tunnel.set_defaults(func=cmd_tunnel)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
