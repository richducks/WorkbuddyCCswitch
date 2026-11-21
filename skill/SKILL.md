---
name: deploy-workbuddy-ccswitch
description: 从 GitHub 部署 WorkbuddyCCswitch Docker 控制台并接入本地 Hermes。
license: MIT
metadata:
  short-description: 一键部署 WorkbuddyCCswitch 并打通 Hermes 对接
  version: "1.0.0"
---

# deploy-workbuddy-ccswitch

把 GitHub 上的 [richducks/WorkbuddyCCswitch](https://github.com/richducks/WorkbuddyCCswitch) 部署成 Docker 栈：WorkBuddy 网关（多微信账号 + 北京时间自动签到）+ CC Switch 用量桥 + 「Hermes 模型接入控制台」网页。技能脚本只做克隆、固定源码构建、启动与健康检查，不改动宿主机的 Hermes / CC Switch 配置，也不会把任何凭据写进仓库。

## 适用场景

- 在一台 Linux x86_64 主机（本地或服务器）全新部署本项目，或把已有部署更新到 main 最新。
- 已有部署的健康检查，以及「服务器跑 Docker、本地跑 Hermes」的隧道对接。
- 不适用于：上游 WorkBuddy Gateway 的开发修改；非 Docker 部署；ARM64（网关二进制按 amd64 构建，ARM 需在目标机自行构建）。

## 前置条件

- Linux x86_64，Docker Engine（含 compose 插件）、git；构建网关需要 Go 1.26+（无系统 Go 时用 `GO=/path/to/go/bin/go` 指定）。
- 仓库当前为私有：执行机需具备 GitHub 访问权限（`gh auth login`、部署用 token 或 SSH key）。仓库转公开后此限制消失。
- 网页密码、网关 API Key、bridge token 首次启动自动生成，分别保存在数据卷 `data/web/` 与 `data/gateway/`（0600，不入 Git）。

## 部署（一条命令）

```bash
python3 skill/scripts/deploy_workbuddy_ccswitch.py deploy --dir ./WorkbuddyCCswitch
```

脚本依次执行：clone main → 校验固定上游 commit 并构建 `bin/workbuddy-gateway-manual`（构建前跑签到测试）→ 生成 `.env` → `docker compose up -d --build` → 等待 healthy → 打印网页地址。目录已存在时执行 = 拉取更新并按需重建。

- 端口冲突：编辑 `.env` 的 `HOST_WEB_PORT` / `HOST_GW_PORT` / `HOST_BRIDGE_PORT`；容器名/镜像名冲突时在部署目录加 `docker-compose.override.yml` 覆盖 `container_name` 与 `image`（compose 自动合并）。脚本会在启动前检查宿主端口占用并给出明确报错。
- 宿主机已跑着别的网关：改端口再执行，不要停别人的服务。

## 验证

```bash
python3 skill/scripts/deploy_workbuddy_ccswitch.py verify --dir ./WorkbuddyCCswitch
```

检查容器 healthy、网页对未认证请求返回 401。通过后：

1. 浏览器打开 `http://127.0.0.1:<HOST_WEB_PORT>/`，用户名 `admin`，密码来自 `docker compose exec workbuddy-hermes cat /data/web/web.token`（在部署主机上执行）。
2. 「添加微信账号」→ 目标微信号本人扫码 → 凭据写入独立 JSON，网关热加载自动入池。
3. 自动签到默认开启：北京时间 08:00–09:30 随机发起，失败 09:30–10:00 重试；网页可一键签到、按账号签到、开关自动签到。

## 服务器部署 + 本地 Hermes

Docker 在服务器、Hermes 在本地时，不要公网暴露管理网页与用量桥；用 SSH 隧道把网关 API 转到本地：

```bash
python3 skill/scripts/deploy_workbuddy_ccswitch.py tunnel user@server --port 18317
```

隧道运行后，在本地按控制台「模型提供商」卡给出的命令导入 provider（把端口换成隧道端口 18317）。三条转发、CC Switch 用量桥、公网 HTTPS 反代 `/v1/` 的安全边界、跨机数据迁移，见 [references/remote-hermes.md](references/remote-hermes.md)。

## 陷阱

- **端口只绑 127.0.0.1**：compose 已固定 loopback，不要改成 `0.0.0.0` 发布；公网只经 HTTPS 反代暴露 `/v1/`。
- **web.token / 网关 apiKey / bridge token 互不相同**：迁移时用到哪份拷哪份；全新部署会生成全新的，Hermes 侧要同步换 `key_env`。
- **两套实例不得共用同一批账号状态**：迁移 `data/gateway` + `data/web` 前先停旧实例，排除 `*.sock` 与日志，且按目标机用户修正 UID/GID。
- healthcheck 的 `Basic` 字符串拼接、`accounts: null` 兼容、秒/毫秒时间戳兼容已在仓库代码内处理，不要"顺手简化"。
- 私库 clone 失败先查凭据（`gh auth status`），不要把 token 拼进 URL。

## 完成标准

- `verify` 输出 PASS（healthy + 401）。
- 网页可登录，「添加微信账号」能出二维码（扫码由账号本人完成）。
- 本地 Hermes 的 `providers.workbuddy` 指向可用 `/v1`，完成一次真实对话。
