#!/usr/bin/env python3
"""Deploy or verify the WorkbuddyCCswitch Docker stack on a Linux host.

Runs only local commands declared here (git/docker/python3/ssh); it never
sends any token, credential or private data anywhere.
"""
from __future__ import annotations

from typing import Any, NoReturn

import argparse
import http.client
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

REPO = "https://github.com/richducks/WorkbuddyCCswitch.git"
BRANCH = "main"
CC_SWITCH_REPO = "https://api.github.com/repos/farion1231/cc-switch/releases/latest"
HERMES_INSTALL = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"


def go_install_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "wbcc-go"
    return Path.home() / ".local" / "wbcc-go"


def host_os() -> str:
    return platform.system()  # Linux / Darwin / Windows


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


def run_visible(cmd: list[str], cwd: Path | None = None) -> bool:
    """Run a command with inherited stdio; returns True on exit 0."""
    try:
        return subprocess.run(cmd, cwd=cwd).returncode == 0
    except OSError as e:
        print(f"  （无法执行 {cmd[0]}：{e}）")
        return False


def sudo_ready() -> bool:
    if host_os() == "Windows" or shutil.which("sudo") is None:
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def fetch_json(url: str, headers: dict | None = None) -> Any:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as resp:
        return json.load(resp)


def download(url: str, dest: Path, expect_sha256: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {url.split('/')[-1]} → {dest}")
    digest = hashlib.sha256()
    with urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 20):
            digest.update(chunk)
            fh.write(chunk)
    got = digest.hexdigest()
    if expect_sha256 and got.lower() != expect_sha256.lower():
        dest.unlink(missing_ok=True)
        fail(f"SHA256 不匹配（期望 {expect_sha256[:16]}…，实际 {got[:16]}…）；已删除下载文件")
    if expect_sha256:
        print(f"  SHA256 校验通过：{got[:16]}…")
    return dest


def compose(repo_dir: Path, *args: str) -> str:
    return sh(["docker", "compose", *args], cwd=repo_dir, timeout=900)


def published_ports(repo_dir: Path) -> dict[int, tuple[str, int]]:
    try:
        service = json.loads(compose(repo_dir, "config", "--format", "json"))["services"]["workbuddy-hermes"]
        host = service.get("environment", {}).get("HOST_BIND_IP", "127.0.0.1")
        ports = {}
        for row in service.get("ports", []):
            target = row.get("target")
            if target not in (8080, 8317, 8318) or row.get("protocol", "tcp") != "tcp":
                continue
            if not host or host == "0.0.0.0" or target in ports or row.get("host_ip") != host:
                raise ValueError
            ports[target] = (host, int(row["published"]))
        if set(ports) == {8080, 8317, 8318}:
            return ports
    except (ValueError, TypeError, KeyError):
        pass
    fail("Compose 必须为网页、网关及用量桥各发布一个显式的 BIND_IP 地址和端口")


def port_free(local_port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_server((host, local_port)):
            return True
    except OSError:
        return False


def require_ports_free(ports: dict[int, tuple[str, int]]) -> None:
    taken = [f"{host}:{port}" for host, port in ports.values() if not port_free(port, host)]
    if taken:
        fail("地址不可绑定或端口已被占用：" + ", ".join(taken) + "；请检查 .env 中的 BIND_IP 和端口")


# ---------------------------------------------------------------- deps ----

DEPS = ("docker", "git", "go", "python", "hermes", "ccswitch")


def linux_pkg() -> str:
    for cand in ("apt-get", "dnf", "yum", "pacman", "zypper"):
        if shutil.which(cand):
            return cand
    return ""


def docker_status() -> str:
    if not shutil.which("docker"):
        return "missing"
    if subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                      capture_output=True, timeout=15).returncode == 0:
        return "ok"
    return "daemon-down"


def install_docker() -> None:
    s = host_os()
    if s == "Linux":
        if sudo_ready():
            print("  运行官方安装脚本（get.docker.com，需要 sudo）…")
            if run_visible(["sudo", "sh", "-c", "curl -fsSL https://get.docker.com | sh"]):
                run_visible(["sudo", "usermod", "-aG", "docker", os.environ.get("USER", "")] if
                            os.environ.get("USER") else ["true"])
                print("  已安装。请注销重新登录（或 newgrp docker）使用免 sudo 的 docker。")
                return
        print("  请手动执行：sudo sh -c 'curl -fsSL https://get.docker.com | sh'")
        return
    if s == "Darwin":
        if shutil.which("brew"):
            print("  brew install --cask docker …")
            if run_visible(["brew", "install", "--cask", "docker"]):
                print("  已安装。请手动启动 Docker.app 完成首次初始化。")
                return
        else:
            print("  未找到 Homebrew；先安装：/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
        print("  或从 https://www.docker.com/products/docker-desktop/ 下载 Docker Desktop")
        return
    if shutil.which("winget"):
        print("  winget 安装 Docker Desktop …")
        if run_visible(["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                        "--accept-source-agreements", "--accept-package-agreements"]):
            print("  已安装。请启动 Docker Desktop 并等待其就绪。")
            return
    print("  请从 https://www.docker.com/products/docker-desktop/ 下载 Docker Desktop（Windows 需 WSL2）")


def install_git() -> None:
    s = host_os()
    if s == "Linux" and sudo_ready():
        pkg = linux_pkg()
        cmd = {"apt-get": ["sudo", "apt-get", "install", "-y", "git"],
               "dnf": ["sudo", "dnf", "install", "-y", "git"],
               "yum": ["sudo", "yum", "install", "-y", "git"],
               "pacman": ["sudo", "pacman", "-S", "--noconfirm", "git"],
               "zypper": ["sudo", "zypper", "-n", "in", "git"]}.get(pkg, [])
        if cmd and run_visible(cmd):
            return
    elif s == "Darwin" and shutil.which("brew"):
        if run_visible(["brew", "install", "git"]):
            return
    elif s == "Windows" and shutil.which("winget"):
        if run_visible(["winget", "install", "-e", "--id", "Git.Git",
                        "--accept-source-agreements", "--accept-package-agreements"]):
            return
    print(f"  请手动安装 git（{s} 官网 https://git-scm.com/downloads）")


def install_go() -> Path:
    """User-level Go toolchain from go.dev with SHA256 verification; no root needed."""
    s = host_os()
    osmap = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}
    amap = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    goos, goarch = osmap[s], amap.get(platform.machine().lower(), "")
    if not goarch:
        fail(f"架构 {platform.machine()} 不受支持，无法自动安装 Go")
    releases = fetch_json("https://go.dev/dl/?mode=json")
    files = [f for f in releases[0]["files"]
             if f["os"] == goos and f["arch"] == goarch and f["kind"] == "archive"]
    if not files:
        fail(f"go.dev 上没有 {goos}/{goarch} 的归档")
    meta = files[0]
    tmp = Path(tempfile.mkdtemp(prefix="wbcc-go-"))
    archive = download("https://go.dev/dl/" + meta["filename"], tmp / meta["filename"],
                       meta["sha256"])
    install_dir = go_install_dir()
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    print(f"  解压到 {install_dir}")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(install_dir)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(install_dir)
    shutil.rmtree(tmp, ignore_errors=True)
    go_bin = install_dir / "go" / "bin" / ("go.exe" if s == "Windows" else "go")
    if not go_bin.exists():
        fail("Go 安装后未找到可执行文件")
    print(f"  Go {meta['version']} 安装完成：{go_bin}")
    return go_bin


def go_ready() -> bool:
    return bool(shutil.which("go") or os.environ.get("GO"))


def hermes_installed() -> bool:
    return bool(shutil.which("hermes"))


def install_hermes() -> None:
    s = host_os()
    if s == "Windows":
        if shutil.which("bash"):
            print("  通过 bash（Git Bash/WSL）运行官方安装脚本…")
            if run_visible(["bash", "-lc", HERMES_INSTALL]):
                return
        print("  Windows 安装 Hermes：在 WSL 中执行 curl -fsSL "
              "https://hermes-agent.nousresearch.com/install.sh | bash（详见 "
              "https://hermes-agent.nousresearch.com/docs/）")
        return
    print("  运行 Hermes 官方安装脚本…")
    if run_visible(["bash", "-c", HERMES_INSTALL]):
        print("  已安装。首次使用运行 hermes setup 完成初始化。")
    else:
        print(f"  请手动执行：{HERMES_INSTALL}")


def ccswitch_release() -> dict | None:
    headers = {"User-Agent": "wbcc-deploy", "Accept": "application/vnd.github+json"}
    try:
        return fetch_json(CC_SWITCH_REPO, headers=headers)
    except HTTPError as e:
        if e.code in (403, 429) and shutil.which("gh"):
            out = subprocess.run(["gh", "api", "repos/farion1231/cc-switch/releases/latest"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                print("  （匿名 API 被限流，改用 gh 认证查询）")
                return json.loads(out.stdout)
        print(f"  无法获取最新版本（GitHub API {e.code}）；请到 "
              "https://github.com/farion1231/cc-switch/releases 手动下载")
        return None
    except OSError as e:
        print(f"  无法获取最新版本（{e}）；请到 "
              "https://github.com/farion1231/cc-switch/releases 手动下载")
        return None


def ccswitch_installed() -> bool:
    s = host_os()
    if s == "Darwin":
        return (Path("/Applications/CC Switch.app").exists()
                or (Path.home() / "Applications" / "CC Switch.app").exists())
    if s == "Windows":
        bases = [os.environ.get(k) for k in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(k)]
        for base in bases:
            for name in ("cc-switch", "CC Switch", "CC-Switch"):
                if (Path(base) / name).exists():
                    return True
        return False
    if shutil.which("cc-switch"):
        return True
    for d in (Path("/usr/share/applications"), Path.home() / ".local/share/applications"):
        if d.is_dir():
            for df in d.glob("*.desktop"):
                text = df.read_text(errors="ignore").lower()
                if "cc-switch" in text or ("cc switch" in text):
                    return True
    return False


def install_ccswitch() -> None:
    s = host_os()
    if s == "Darwin" and shutil.which("brew"):
        print("  brew install --cask cc-switch …")
        if run_visible(["brew", "install", "--cask", "cc-switch"]):
            return
    amap = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
    arch = amap.get(platform.machine().lower(), "")
    ext = {"Darwin": ".dmg", "Windows": ".msi", "Linux": ".appimage"}.get(s, "")
    if not ext or (s != "Darwin" and not arch):
        print(f"  请到 https://github.com/farion1231/cc-switch/releases 手动下载 {s} 安装包")
        return

    def asset_ok(name: str) -> bool:
        lower = name.lower()
        if not lower.endswith(ext):
            return False
        if s == "Darwin":
            return "macos" in lower and "arm64" not in lower  # universal build
        if s == "Windows":
            return ("arm64" in lower) == (arch == "arm64")
        return arch in lower

    release = ccswitch_release()
    if not release:
        return
    version = release.get("tag_name", "")
    assets = [a for a in release.get("assets", []) if asset_ok(a["name"])]
    if not assets:
        print(f"  最新版 {version} 没有 {s}/{arch or '*'}{ext} 资产；请到 releases 页手动选择")
        return
    asset = assets[0]
    digest = (asset.get("digest") or "").removeprefix("sha256:") or None
    downloads = Path.home() / "Downloads"
    dest = download(asset["browser_download_url"], downloads / asset["name"], digest)
    if ext == ".AppImage":
        dest.chmod(0o755)
        print(f"  已就绪：直接运行 {dest}（如缺 libfuse2：sudo apt install libfuse2；"
              "或改用 releases 页的 .deb/.rpm）")
    elif ext == ".dmg":
        print(f"  已下载 {dest}：双击安装，或执行 open '{dest}'")
    else:
        print(f"  已下载 {dest}：双击运行安装，或执行 start /wait \"\" \"{dest}\"")


def cmd_deps(args) -> None:
    only = {x.strip() for x in args.only.split(",")} if args.only else set(DEPS)
    unknown = only - set(DEPS)
    if unknown:
        fail(f"未知依赖：{unknown}；可选 {DEPS}")
    plan = []
    if args.dry_run:
        print("== dry-run：仅显示将要执行的检查/安装 ==")
    if "docker" in only:
        status = docker_status()
        if args.dry_run:
            plan.append(f"docker: {status}" + ("；缺失时按平台安装（get.docker.com / brew cask / winget）" if status != "ok" else ""))
        elif status == "ok":
            print("[OK] docker：CLI 与守护进程正常")
        else:
            print(f"[..] docker：{status}")
            install_docker()
    if "git" in only:
        if args.dry_run:
            plan.append(f"git: {'ok' if shutil.which('git') else '缺失时按平台安装（apt/dnf/brew/winget）'}")
        elif shutil.which("git"):
            print("[OK] git")
        else:
            print("[..] git 未安装")
            install_git()
    if "go" in only:
        if args.dry_run:
            plan.append(f"go: {'ok' if go_ready() else '缺失时自动安装用户级工具链到 ' + str(go_install_dir()) + '（go.dev 最新版，SHA256 校验）'}")
        elif go_ready():
            print("[OK] go（PATH 或 GO 环境变量）")
        else:
            print("[..] go 未安装")
            go_bin = install_go()
            os.environ["GO"] = str(go_bin)
            print(f"  后续使用：export GO={go_bin}（Windows: set GO={go_bin}）")
    if "python" in only:
        if args.dry_run:
            plan.append(f"python: {'ok ('+platform.python_version()+')' if sys.version_info >= (3,9) else '版本过旧，需 3.9+'}")
        elif sys.version_info >= (3, 9):
            print(f"[OK] python {platform.python_version()}")
        else:
            print(f"[..] python {platform.python_version()} 过旧（需 3.9+）；"
                  "Windows: winget install Python.Python.3.13；macOS: brew install python@3.13")
    if "hermes" in only:
        if args.dry_run:
            plan.append(f"hermes: {'ok' if hermes_installed() else '缺失时运行官方安装脚本 ' + HERMES_INSTALL}")
        elif hermes_installed():
            print("[OK] hermes")
        else:
            print("[..] hermes 未安装")
            install_hermes()
    if "ccswitch" in only:
        if args.dry_run:
            plan.append("ccswitch: 检测常见安装位置；缺失时从 farion1231/cc-switch 最新 release 下载"
                        "对应平台安装包（SHA256 校验，保存到 ~/Downloads）")
        elif ccswitch_installed():
            print("[OK] ccswitch（检测到已安装）")
        else:
            print("[..] ccswitch 未检测到")
            install_ccswitch()
    if args.dry_run:
        for line in plan:
            print("  - " + line)
    else:
        print("提示：服务器只需要 docker/git/go；hermes 与 ccswitch 装在你使用 AI 工具的电脑上。")


# --------------------------------------------------------------- deploy ----

def cmd_deploy(args) -> None:
    status = docker_status()
    if status == "missing":
        fail("docker 未安装：先运行 deps 子命令自动安装，或按平台手动安装")
    if status == "daemon-down":
        hint = {"Darwin": "请先启动 Docker Desktop", "Windows": "请先启动 Docker Desktop"}.get(
            host_os(), "请启动 docker 服务（sudo systemctl start docker）")
        fail(f"docker 守护进程未运行：{hint}")
    if not shutil.which("git"):
        fail("git 未安装：先运行 deps 子命令")
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
    if not go_ready():
        print("[..] go 未安装——自动安装用户级工具链")
        os.environ["GO"] = str(install_go())
    if not (repo_dir / "bin/workbuddy-gateway-manual").exists():
        print("Building pinned gateway binary (first run downloads upstream source)")
        sh([sys.executable, "scripts/build_gateway.py"], cwd=repo_dir, timeout=1800)
    if not (repo_dir / ".env").exists():
        shutil.copy(repo_dir / ".env.example", repo_dir / ".env")
        print("已从 .env.example 生成 .env；如需修改 UID/GID 或端口请先编辑")
    ports = published_ports(repo_dir)
    require_ports_free(ports)
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
    web_host, web_port = ports[8080]
    print(f"管理网页：http://{web_host}:{web_port}/"
          "（用户名 admin，密码在容器内 /data/web/web.token）")
    print("下一步：docker compose exec workbuddy-hermes cat /data/web/web.token 查看网页密码；"
          "再按 README 或 skill/references/remote-hermes.md 配置 Hermes 对接")


class ProbeValidationError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_lines(stream, deadline: float | None):
    pending = b""
    received = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("SSE probe deadline expired")
        chunk = stream.read1(8192)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("SSE probe deadline expired")
        if not chunk:
            if pending:
                yield pending
            return
        received += len(chunk)
        if received > 2 * 1024 * 1024:
            raise ProbeValidationError("SSE probe response exceeded 2 MiB")
        *lines, pending = (pending + chunk).split(b"\n")
        yield from lines


def check_inference_stream(stream, model: str, started: float,
                           deadline: float | None = None) -> tuple[float, float]:
    first = None
    content = []
    finish = None
    reported_model = False
    for line in probe_lines(stream, deadline):
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            if finish != "stop":
                raise ProbeValidationError("SSE did not finish with stop")
            if not reported_model:
                raise ProbeValidationError("SSE did not report a model")
            if first is None or "".join(content).strip() != "WBCC_OK":
                raise ProbeValidationError("SSE reply did not match the probe marker")
            return first, time.monotonic() - started
        event = json.loads(data)
        if not isinstance(event, dict) or "error" in event:
            raise ProbeValidationError("SSE returned an error event")
        if "model" in event:
            if event["model"] != model:
                raise ProbeValidationError("SSE returned an unexpected model")
            reported_model = True
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if first is None:
                    first = time.monotonic() - started
                content.append(text)
            finish = choice.get("finish_reason") or finish
    raise ProbeValidationError("SSE ended without [DONE]")


def verify_gateway(repo_dir: Path, host: str, port: int, model: str | None) -> None:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = f"http://{host}:{port}/v1"
    stage = "模型目录"
    try:
        config = json.loads((repo_dir / "data/gateway/config.json").read_text(encoding="utf-8"))
        key = config["gateway"]["apiKey"]
        if not isinstance(key, str) or not key:
            raise ProbeValidationError("缺少网关 API Key")
        req = Request(url + "/models")
        req.add_unredirected_header("Authorization", "Bearer " + key)
        with opener.open(req, timeout=10) as resp:
            ids = {item["id"] for item in json.load(resp)["data"]}
        if not ids:
            raise ProbeValidationError("模型目录为空")
        print(f"PASS: 模型目录可读，返回 {len(ids)} 个模型")
        if model is None:
            return
        stage = "流式推理"
        if model not in ids:
            raise ProbeValidationError("指定模型不在网关目录中")
        body = {
            "model": model, "stream": True, "max_tokens": 256,
            "messages": [{"role": "user", "content": "Reply with exactly WBCC_OK."}],
        }
        req = Request(url + "/chat/completions", json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        req.add_unredirected_header("Authorization", "Bearer " + key)
        started = time.monotonic()
        with opener.open(req, timeout=60) as resp:
            if resp.headers.get_content_type() != "text/event-stream":
                raise ProbeValidationError("网关未返回 SSE")
            first, total = check_inference_stream(resp, model, started, deadline=started + 90)
    except HTTPError as e:
        with e:
            try:
                kind = json.loads(e.read(4096))["error"]["type"]
            except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
                kind = None
        detail = f" {kind}" if isinstance(kind, str) and re.fullmatch(r"[a-z0-9_]{1,64}", kind) else ""
        fail(f"{stage}失败（HTTP {e.code}{detail}）")
    except ProbeValidationError as e:
        fail(f"{stage}失败：{e}")
    except TimeoutError:
        fail(f"{stage}失败：探测超时")
    except PermissionError:
        fail(f"{stage}失败：当前用户无法读取网关配置，请使用部署用户运行，或核对 Compose 的 UID/GID")
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError) as e:
        fail(f"{stage}失败（{type(e).__name__}）")
    print(f"PASS: 流式推理完成，模型 {model}，首文本 {first:.2f}s，总耗时 {total:.2f}s")


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
    print("PASS: 容器健康检查通过")
    ports = published_ports(repo_dir)
    verify_gateway(repo_dir, *ports[8317], args.inference_model)
    web_host, web_port = ports[8080]
    try:
        with build_opener(ProxyHandler({})).open(f"http://{web_host}:{web_port}/api/providers", timeout=5):
            pass
    except HTTPError as e:
        if e.code != 401:
            fail(f"console check failed: HTTP {e.code}")
    except OSError as e:
        fail(f"console check failed: {e}")
    else:
        fail("网页未要求鉴权——配置异常，请检查 .env 与 compose")
    print(f"PASS: 网页 {web_host}:{web_port} 鉴权正常（未认证返回 401）")


def cmd_tunnel(args) -> None:
    if not port_free(args.port):
        fail(f"本地端口 {args.port} 已被占用")
    if shutil.which("ssh") is None:
        hint = {"Windows": "启用 Windows 可选功能 OpenSSH 客户端，或安装 Git for Windows"}.get(
            host_os(), "安装 openssh-client")
        fail(f"未找到 ssh：{hint}")
    cmd = ["ssh", "-NT", "-o", "ExitOnForwardFailure=yes",
           "-L", f"127.0.0.1:{args.port}:127.0.0.1:{args.server_gw}",
           "-L", f"127.0.0.1:{args.port + 100}:127.0.0.1:{args.server_web}"]
    if args.server_bridge:
        cmd += ["-L", f"127.0.0.1:{args.port + 1}:127.0.0.1:{args.server_bridge}"]
    cmd.append(args.ssh_target)
    print(f"隧道映射：本地 :{args.port} → 服务器网关 API，:{args.port + 100} → 服务器网页")
    print(f"Hermes Base URL: http://127.0.0.1:{args.port}/v1")
    try:
        subprocess.run(cmd)
    except OSError as e:
        fail(f"无法启动 ssh：{e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "WorkbuddyCCswitch deployment helper").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    deps = sub.add_parser("deps", help="检测并安装缺失依赖（docker/git/go/python/hermes/ccswitch）")
    deps.add_argument("--only", default="", help="只处理指定依赖，逗号分隔")
    deps.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何安装")
    deps.set_defaults(func=cmd_deps)

    deploy = sub.add_parser("deploy", help="克隆、构建并启动 Docker 栈")
    deploy.add_argument("--dir", default="./WorkbuddyCCswitch")
    deploy.set_defaults(func=cmd_deploy)

    verify = sub.add_parser("verify", help="检查本机容器健康、模型目录和网页鉴权")
    verify.add_argument("--dir", default="./WorkbuddyCCswitch")
    verify.add_argument("--inference-model", help="额外验证指定模型的完整流式推理，会消耗上游用量")
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
