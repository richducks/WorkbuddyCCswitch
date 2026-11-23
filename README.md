# Hermes 模型接入控制台（Docker）

把现有的「WorkBuddy Gateway 多微信号池 + Hermes 自定义 provider + CC Switch 分账号用量」方案打包成一个 Docker 镜像，
并提供本地配置网页：管理 WorkBuddy 微信账号与签到，或新增独立的 Hermes 模型提供商预设。

## 范围

- **容器内**：基于 WorkBuddy Gateway v1.14.47 固定源码构建的本地版本、只读用量桥、配置网页。手动签到只通过容器内私有 Unix 套接字调用，不增加宿主机端口。
- **宿主机**：可保留现有 Hermes / CC Switch，按实际地址和各自的凭据接入，不需要把 Hermes 一起部署进容器。
- **网络**：所有 Docker 端口仅发布到 `127.0.0.1`；远程访问可通过 SSH 本地端口转发，不直接发布管理网页。

## 结构

```
┌─ container: workbuddy-hermes ─────────────────────────────┐
│  docker-entrypoint.sh  (修正卷属主 → 降权到 wb 用户)      │
│  boot.py                                                  │
│    ├─ workbuddy-gateway serve   0.0.0.0:8317  (API)        │
│    ├─ usage_bridge              0.0.0.0:8318  (/usage)     │
│    └─ web console               0.0.0.0:8080  (配置网页)   │
│  /data/gateway  ← ./data/gateway  (凭据/config/签到状态/日志)│
│  /data/web      ← ./data/web      (token/标签/登录二维码)   │
└──────────────────────────────────────────────────────────┘
          │ 127.0.0.1 端口映射
          └─ Hermes (providers.workbuddy) / CC Switch (用量查询)
```

## 快速开始

支持 Linux、macOS 和 Windows，三平台运行同一个 Docker 栈（容器内行为完全一致）。宿主机需要：Docker（Linux 用 Docker Engine + compose 插件；macOS / Windows 用 Docker Desktop，Windows 需启用 WSL2 后端）、Git、Python 3.9+，以及 Go 1.26+（构建网关用；无系统 Go 时用 `--go` 或环境变量 `GO` 指定）。Apple Silicon 原生构建 arm64 容器，Intel/AMD 为 amd64。

仓库不包含编译后的网关；构建脚本会拉取固定的 WorkBuddy Gateway v1.14.47 源码、校验提交、应用本地补丁（手动签到 + 到期优先调度）并运行签到与调度策略测试。

```bash
cd WorkbuddyCCswitch
cp -n .env.example .env                    # Windows PowerShell: Copy-Item .env.example .env
# 按需调整 .env 中的 UID/GID 和宿主机端口。
python3 scripts/build_gateway.py           # Windows: python scripts\build_gateway.py
docker compose up -d --build
docker compose ps
```

更省事的路径：直接用跨平台的部署脚本 `python3 skill/scripts/deploy_workbuddy_ccswitch.py deploy --dir ./WorkbuddyCCswitch`（克隆、构建、启动、健康检查一条命令完成），Windows 上把 `python3` 换成 `python`。

全新电脑可先运行 `python3 skill/scripts/deploy_workbuddy_ccswitch.py deps`：自动检测并安装缺失的 docker/git/go（go 装到用户目录，无需 root），也可下载最新版 Hermes Agent 与 CC Switch（`--only hermes,ccswitch`，SHA256 校验）。加 `--dry-run` 先看计划。

`scripts/backup-current.sh` 与 `scripts/rollback.sh` 是 bash 脚本（备份、回滚 systemd 旧方案），仅 Linux/macOS 可直接运行，Windows 用户请在 WSL2 中执行或手动复制 `data/`。

默认管理网页为 `http://127.0.0.1:8080/`，打开后先进入 `/login` 登录页，输入控制台密码（首次启动生成的私有文件 `data/web/web.token` 的内容）。会话默认 12 小时，右上角可「退出登录」；脚本仍可用 `curl -u admin:<token>`。网关 API Key 和 CC Switch 用量 token 是另外两份凭据。

**可信局域网直接访问（可选，优先用 SSH 隧道）**：在 `.env` 中将 `BIND_IP` 改为 Docker 宿主机的具体私有 IPv4 地址，再运行 `docker compose up -d --build`。不支持 `0.0.0.0` 或公网地址。三个端口按配置发布到同一个地址：

| 服务 | 访问地址（示例使用默认端口） | 凭据 |
|---|---|---|
| 管理网页 | `http://<BIND_IP>:8080/`（`HOST_WEB_PORT`） | 用户名 `admin`，密码在 `data/web/web.token` |
| Hermes 模型网关 | `http://<BIND_IP>:8317/v1`（`HOST_GW_PORT`） | 网页「复制完整 Key」，或 `data/gateway/config.json` 的 `gateway.apiKey` |
| CC Switch 用量查询 | Base URL `http://<BIND_IP>:8318`（`HOST_BRIDGE_PORT`），查询 `GET /usage` | `data/web/bridge.token` |

三个端口的 HTTP 流量（含鉴权凭据）均未加密。不要在公网、访客网或未隔离的办公网直接发布，也不要做路由器端口转发。在 Linux 上 [Docker 发布的端口可能绕过 UFW](https://docs.docker.com/engine/network/packet-filtering-firewalls/#docker-and-ufw)，不要依靠单独的 UFW 规则隔离这些端口。

如果 Docker 部署在远程服务器上，下面的命令在运行 Hermes 的电脑上执行（替换 SSH 用户及主机名，端口按服务器 `.env` 调整）：

```bash
ssh -NT -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8317:127.0.0.1:8317 \
  -L 127.0.0.1:18080:127.0.0.1:8080 \
  deploy@server.example.com
```

隧道连通后，在本地浏览器打开 `http://127.0.0.1:18080/`，使用控制台给出的 WorkBuddy 配置在本地 Hermes 接入 `http://127.0.0.1:8317/v1`。如有端口冲突，调整相应的本地转发端口及 Hermes 的 Base URL。不需要将 Docker 的业务端口开放到公网。

## 打包 Skill（可选）

仓库自带 `skill/` 目录，可把本项目作为一套 Agent 技能安装给他人使用（克隆仓库后即可部署、验证、打通隧道）：

- `skill/SKILL.md` —— 技能说明与完整流程
- `skill/scripts/deploy_workbuddy_ccswitch.py` —— `deploy` / `verify` / `tunnel` 三合一部署脚本
- `skill/references/remote-hermes.md` —— 服务器部署 + 本地 Hermes 对接细节

安装到 Hermes：把 `skill/` 目录复制为 `~/.hermes/skills/deploy-workbuddy-ccswitch/`（或按所用 Agent 的技能目录放置）。

## 网页功能

| 区域 | 内容 |
|---|---|
| 登录与鉴权 | `/login` 表单页输入控制台密码（`data/web/web.token`），成功后写入 `wb_session` 会话 cookie（HttpOnly、SameSite=Strict、默认 12 小时）与可读的 `wb_csrf`；未认证访问页面 303 跳登录页、访问 API 返回 401；写操作额外校验 `X-CSRF-Token` 与同源；失败登录固定延迟 0.4s。脚本仍可用 `curl -u admin:<token>`（Basic Auth）直接调用 |
| 侧边栏导航 | 三组：账号与签到 → 模型接入 → 用量与状态；首个导航项即默认页；≤920px 折叠为可横向滚动的 chips |
| 微信账号池 | 逐号显示凭据文件名、可自定义备注、加载状态、额度、积分到期时间（≤3 天高亮）、今日签到徽章（已签到 / 未签到 / 签到失败 / 待签到）、最近签到时间 |
| 积分到期汇总 | 账号与签到页顶部的汇总条：7 天内到期积分总量与账号数，另标出 3 天内的部分和最早到期日；数据来自网关权益快照的 `quotaExpireAt` |
| 添加微信账号 | 点击后容器执行 `login -auth workbuddyN.json`，网页实时显示终端二维码和备用登录链接；本人用目标微信号扫码授权，凭据写入独立文件，网关热加载自动加入账号池 |
| 一键签到 / 单号签到 | 处理尚未尝试的待签或漏签国内个人账号；批量逐个执行并显示各账号结果。跳过已签到、已尝试或正在签到的账号；无需重启网关。 |
| 自动签到 | 网页开关默认开启，保存后立即生效并在容器重启后保留。关闭只暂停尚未开始的自动尝试，不影响手动签到。每个账号显示当天的计划签到或重试时间，以及最近实际尝试时间（北京时间）。 |
| 改名 / 移除 | 备注只存本地标签文件；移除是把凭据移到 `data/gateway/.disabled/`，不删除文件 |
| Hermes 对接 | 显示 `base_url`、默认模型、脱敏 API Key；可复制 provider JSON、配置命令和完整 Key |
| 模型目录 | 自动从网关 `/v1/models` 读取全部模型（含上下文/输出上限）；复制的 provider JSON 已含全量模型；新增模型后可一键复制刷新命令同步到宿主机 Hermes |
| 用量桥 | 显示 CC Switch 应填写的 Base URL；用量刷新只读，不触发签到 |
| 模型提供商（暂隐藏） | 预设数据层与 `/api/providers` 接口保留，卡片在页面上暂时 `hidden`（评审意见「暂不做拓展」）；恢复入口时去掉 `hidden` 与导航注释即可 |

## 提醒（可选）

设置 `WB_NOTIFY_WEBHOOK` 后，容器每 `WB_NOTIFY_INTERVAL` 秒（默认 900）检查一次状态快照，命中以下条件时 POST 一条文本消息：

- 某账号**今日签到失败**（每天每个账号只提醒一次）；
- 某账号**积分在 3 天内到期**且仍有余额（每个到期日只提醒一次）。

`WB_NOTIFY_KIND` 决定消息封装：`generic`（默认，`{"source","text"}`）、`telegram`（`{"text":…}`，URL 用 Bot API 的 `sendMessage` 地址）、`dingtalk`、`feishu`。去重状态保存在 `data/web/notify-state.json`（0600，30 天后自动过期）；投递失败不会记为已提醒，下一轮会重试，且**不会影响签到或控制台**。webhook URL 通常自带 token，因此不会出现在日志里。

## 备份与迁移

`data/gateway/` 和 `data/web/` 包含真实凭据和运行状态，不能上传到 Git。跨服务器迁移时，停掉旧实例后通过可信的私有通道复制这两个目录，跳过日志和 Unix 套接字；不要使用同一批账号同时运行两套实例。

恢复备份时请复制为独立文件，不要创建指向备份的硬链接。容器启动会调整已恢复的目录和单链接普通文件的属主，不会跟随符号链接或更改多链接文件的属主。

```bash
bash scripts/backup-current.sh                     # 本机私有备份（默认保留 7 份）
bash scripts/backup-current.sh --keep 14           # 只保留最近 14 份（0 = 不清理）
bash scripts/backup-current.sh --backup-dir /mnt/backup --keep 30
```

脚本以时间戳目录保存 `data/gateway`、`data/web`、Hermes 配置、CC Switch 数据库与 systemd unit，生成 `SHA256SUMS` 便于核对，权限 700/0600，不跟随符号链接。定时执行示例（`crontab -e`）：

```cron
10 3 * * * cd /path/to/WorkbuddyCCswitch && bash scripts/backup-current.sh --keep 14 >> backup.log 2>&1
```

`scripts/rollback.sh` 仅适用于原本就运行了旧版 systemd 网关和用量桥的机器，不是新安装的通用回滚方式。

## 上游升级检查

镜像钉住上游 `v1.14.47`（提交也固定），两个补丁与上游源码强耦合。`scripts/check_upstream.py` 把「要不要升级、补丁还行不行」变成一条命令：

```bash
python3 scripts/check_upstream.py                       # 在线查上游是否发布了更新的 tag
python3 scripts/check_upstream.py --local ../workbuddy-gateway   # 对本地源码试打两个补丁
python3 scripts/check_upstream.py --report /tmp/upgrade.md       # 加 --report 存成 Markdown
```

`--local` 要求传入 `git clone` 的上游源码目录；脚本会核对 HEAD、用 `git apply --check` 逐个试打补丁，并输出升级检查单（`checkin.go` 签到执行器、`main.go` 选号层级、状态快照字段、Go 单测、容器冒烟）。补丁冲突时退出码为 1。升级完成后同步修改 `scripts/build_gateway.py` 的 `TAG` 与 `REVISION`。

## HTTPS（评估结论）

当前三个端口都是明文 HTTP，默认只发布到 `127.0.0.1`，跨机器访问走 SSH 隧道，因此仓库**没有**内置 TLS。可选的两种做法：

1. **SSH 隧道（默认推荐）**：不改任何容器配置，见上文 `ssh -L` 示例；客户端只看到 `127.0.0.1`。
2. **可信局域网 + 反代**：在 Docker 主机上另起一个 Caddy/Nginx，把 8080 暴露为 HTTPS 并转发到 `127.0.0.1:8080`，同时把 `.env` 的 `BIND_IP` 保持回环，只发布反代端口。用自签证书时浏览器会有告警；用内网 CA 或真实域名证书更妥当。网关（8317）与用量桥（8318）同样是明文，是否需要一并加 TLS 取决于你是否只从回环访问它们。

WorkBuddy 本身的模型目录在网关更新后刷新。若宿主机 Hermes 配置也要同步 WorkBuddy 的新模型：

```bash
python3 scripts/refresh-hermes-models.py   # 默认用 127.0.0.1:8317；只改 providers.workbuddy.models
```

局域网绑定或修改了端口时，在运行 Docker 的宿主机上从网页复制刷新命令（含 `--gateway-host` 和 `--gateway-port`）。脚本会备份 Hermes 配置；刷新后重启 CC Switch 以重新导入模型目录。


## 接入非 WorkBuddy 提供商

1. 在「模型提供商」点击「添加提供商」，填名称、标识、API Base URL、协议和默认模型 ID。模型列表手动填写，可选附加模型；支持 Chat Completions、Anthropic Messages 和 Responses。
2. 若需要鉴权，只填独立的密钥变量名，把实际密钥单独写入目标 Hermes profile 的凭据环境。不要把密钥填进 URL、预设或命令。带密钥的非本机端点必须使用 HTTPS。
3. 保存后复制配置命令，在运行 Hermes 的宿主机执行。命令只写 `providers.wbx-<标识>`；然后通过 `hermes model` 选择，不会自动切换默认模型或调整现有备用链。导入同一标识会更新该条配置；移除网页预设不会删除已导入 Hermes 的条目。

预设保存在 `data/web/providers.json`，由 Docker 数据卷持久化。控制台不请求这些自定义地址，也不代理推理；API 地址应由 **Hermes 所在宿主机** 可达。宿主机运行 Hermes 时，本机 Ollama 的 OpenAI 兼容地址可填 `http://127.0.0.1:11434/v1`，模型填写已安装的对话模型 ID。OAuth 提供商仍用 Hermes 原生授权；第三方提供商不参与 WorkBuddy 签到和额度统计。

## 签到说明

- 国内个人账号由网关按北京时间自动签到：08:00–09:30 随机发起，失败在 09:30–10:00 重试。
- Docker 本地版本的「手动签到」复用上游签到执行器和状态锁，先记录尝试再发请求，不重复签到；失败沿用网关原有重试计划。
- 「补签」与「手动签到」均经同一个本地签到执行器，不清零已用尝试次数；启动补签已排队且自动签到开启时，等待原队列执行。接近北京时间午夜不发起跨日请求。
- 关闭自动签到后，已发出的请求不会被取消；重新开启不会改写已有计划。主机休眠期间不会执行自动签到。

## 调度策略：到期日最短优先（expireFirst）

- 容器环境变量 `WB_SCHEDULING_STRATEGY=expireFirst` 开启（仓库默认已开）：同优先级可用账号不再轮换，而是固定选「积分周期到期最早」的账号，把它先用完再自然落到下一个；到期相同/未知时退回轮询顺序。缺省或删除该变量即恢复上游轮询行为。
- 到期时间来自上游完整权益接口中每个有效资源包的扣减周期截止，按 `DeductionEndTime > ExpiredTime > CycleEndTime` 取最早的一个，额度周期刷新后自动更新；账号级展示在状态快照的 `quotaExpireAt` 字段。
- 选号层级保持上游语义（免费耗尽账号优先 → 有余额账号 → 零余额受控探测），expireFirst 只在每一层级内部比较到期时间；账号黑白名单、冷却与失效排除规则不受影响。

## 验证

```bash
python3 -B scripts/selftest.py          # 不触碰真实账号（含导航顺序、到期高亮、鉴权开关断言）
python3 -B scripts/test_auth.py         # 真实 HTTP：登录页 → cookie 会话 → CSRF → 401/403/303 全链路
python3 -B scripts/test_notify.py       # 可选提醒：命中条件、去重、投递失败不记账（本地假接收端）
python3 -B scripts/test_providers.py    # 自定义提供商持久化、校验与命令导出
python3 -B scripts/test_console.py      # 守护退避、缓存并发、登录会话与日志修剪
python3 scripts/check_upstream.py       # 上游是否有新 tag（离线用 --local）
docker compose config --quiet
docker compose ps
curl -i http://127.0.0.1:8080/            # 未登录应 303 → /login
curl -i http://127.0.0.1:8080/api/summary # 未登录应 401；带 -u admin:<token> 应 200
curl -i http://127.0.0.1:8318/usage       # 未认证应 401
```

## 安全约定

- 数据卷 `0700`，凭据/状态/token 文件 `0600`；进程降权运行。
- 控制台：`/login` 单密码表单 → 内存会话（默认 12 小时，最多 5 个并发，超出时淘汰最旧）→ 写操作校验 `X-CSRF-Token` 与同源；未认证页面 303、API 401；失败登录固定延迟 0.4s；响应带 `nosniff`/`X-Frame-Options: DENY`/`no-referrer`。**没有关闭鉴权的环境变量**（旧测试期的 `WB_CONSOLE_NOAUTH` 已移除）。Basic Auth 仍兼容脚本。
- 会话只存在内存：容器重启会让所有人重新登录，但不会影响网关 API Key、用量桥 token 或签到状态。
- API Key 只在 `/api/hermes/key` 返回给已认证会话，页面上默认脱敏。
- 网页不展示 UID、昵称或原始网关日志。新的登录二维码和备用链接只保存在当前会话内存中，不写入登录日志；会话结束后清除。
- 宿主机默认仅发布到 `127.0.0.1`。仅在明确需要、且网络可信时启用上述私有地址绑定；跨公网或不可信网络请保留回环设置并使用 SSH 隧道。
- 只接入本人或明确获授权的微信账号。

## 限制

- 二进制基于 **v1.14.47**；若要重建 `bin/workbuddy-gateway-manual`，运行 `python3 scripts/build_gateway.py`（Go 1.26+，`--goarch amd64|arm64` 可覆盖目标架构，默认跟随本机）。脚本验证上游提交、应用 `gateway-overlay/` 两个补丁并运行签到与调度策略测试；升级前先跑 `scripts/check_upstream.py`。
- 扫码必须由账号本人在网页显示的二维码/链接上完成，无法自动化。
- 账号池按可用性调度（当前为到期日最短优先，见上文），网页展示分账号信息不代表可在单次请求中固定使用某个账号。
- 二维码登录进程默认等待 `LOGIN_TIMEOUT`（300 秒）；网页状态查询检测到超时后会终止该会话。
- 控制台会话不落盘（重启即失效）；「模型提供商」卡片保持隐藏，产品方向待定。

## 文件

```
Dockerfile              镜像定义（python:3.11-slim + gosu 降权）
docker-compose.yml      端口映射、数据卷与环境变量
docker-entrypoint.sh    卷属主修正 + 降权
bin/workbuddy-gateway-manual   本地生成，不纳入 Git
gateway-overlay/               手动签到补丁与到期优先调度补丁（含单元测试）
config/config.json.example  网关配置模板（首次启动生成密钥）
app/boot.py             启动编排（网关守护重启、启动时日志修剪、密钥生成、可选提醒）
app/auth.py             控制台会话与 CSRF（内存、12 小时、最多 5 个）
app/controller.py       账号/登录会话/标签/Hermes 信息/额度到期汇总
app/checkin.py          状态快照与手动签到资格判定
app/notify.py           可选 webhook 提醒（签到失败、积分将到期）
app/usage_bridge.py     CC Switch 只读用量桥
app/web.py              网页与 API（登录页、会话、CSRF、同源校验）
app/static/index.html   配置网页（侧边栏分区）
app/static/login.html   登录页
app/static/providers.js 模型提供商预设（卡片暂时隐藏）
scripts/                跨平台网关构建、离线自测、备份、回滚、模型列表刷新、上游升级检查
```
