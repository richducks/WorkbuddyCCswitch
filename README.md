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

默认管理网页为 `http://127.0.0.1:8080/`，用户名 `admin`。首次启动生成的网页登录密码保存在私有文件 `data/web/web.token`；网关 API Key 和 CC Switch 用量 token 是另外两份凭据。

启用角色权限时，在 `.env` 设置 `WB_CONSOLE_PERMISSIONS=1`，首次启动前将初始密码写入权限为 `0600` 的 `data/web/permissions-bootstrap`。用 `admin` 登录后按照页面提示更换为至少 6 位的新密码，数据库初始化完成后可移除初始密码文件。权限管理入口在控制台右上角，权限模式优先于本机免令牌配置。

### 在 Linux 上重建现有容器

在本机终端进入现有部署目录，先核对容器所属项目和数据挂载：

```bash
sudo docker inspect workbuddy-hermes --format '部署目录: {{index .Config.Labels "com.docker.compose.project.working_dir"}}{{range .Mounts}}{{println}}{{.Source}} -> {{.Destination}}{{end}}'
```

确认部署目录是当前目录，且 `data/gateway` 与 `data/web` 分别挂载到 `/data/gateway` 和 `/data/web`；目录不一致时先切到实际部署目录，不要用另一套空数据重建。如果网关补丁有更新，先运行 `python3 scripts/build_gateway.py`。备份本地数据后，重建并验证：

```bash
sudo docker compose up -d --build --wait workbuddy-hermes &&
sudo docker compose exec -T workbuddy-hermes \
  grep -n 'account-title-height' /app/app/static/index.html
```

如果本机的 `docker ps` 不需要 sudo，可以省略 sudo。无需放宽 Docker socket 权限。重建后刷新原控制台地址并检查账号池向上、向下滚动时标题保持可见。


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
| 模型提供商 | WorkBuddy 作为内置项；其他服务可新增、编辑、移除接入预设，并导出命名的 Hermes 配置命令 |
| 微信账号池 | 逐号显示凭据文件名、可自定义备注、加载状态、额度及分批已用、剩余和到期时间（≤3 天高亮）、今日签到徽章（已签到 / 未签到 / 签到失败 / 待签到）、最近签到时间 |
| 添加微信账号 | 点击后容器执行 `login -auth workbuddyN.json`，网页实时显示终端二维码和备用登录链接；本人用目标微信号扫码授权，凭据写入独立文件，网关热加载自动加入账号池 |
| 一键签到 / 单号签到 | 处理尚未尝试的待签或漏签国内个人账号；批量逐个执行并显示各账号结果。跳过已签到、已尝试或正在签到的账号；无需重启网关。 |
| 自动签到 | 在账号所在行勾选，仅影响该账号的自动签到与自动补领，不影响手动签到。设置在重启和移入另一账号池后保留；首次升级时未单独设置的账号继承原全局状态。 |
| 代签到 | 可扫码添加，也可与微信账号池双向移动。代签到账号只参与自动签到，网关会从模型调用和价格探测中排除这些账号。移动保留签到记录 |
| 改名 / 移除 | 点击账号名称编辑，备注只存本地标签文件；移除是把凭据移到 `data/gateway/.disabled/`，不删除文件 |
| Hermes 对接 | 显示 `base_url`、默认模型、脱敏 API Key；可复制 provider JSON、配置命令和完整 Key |
| 模型目录 | 自动从网关 `/v1/models` 读取全部模型（含上下文/输出上限）；复制的 provider JSON 已含全量模型；新增模型后可一键复制刷新命令同步到宿主机 Hermes |
| 用量桥 | 显示 CC Switch 应填写的 Base URL；用量刷新只读，不触发签到 |

## 备份与迁移

`data/gateway/` 和 `data/web/` 包含真实凭据和运行状态，不能上传到 Git。跨服务器迁移时，停掉旧实例后通过可信的私有通道复制这两个目录，跳过日志和 Unix 套接字；不要使用同一批账号同时运行两套实例。

恢复备份时请复制为独立文件，不要创建指向备份的硬链接。容器启动会调整已恢复的目录和单链接普通文件的属主，不会跟随符号链接或更改多链接文件的属主。

```bash
bash scripts/backup-current.sh      # 本机私有备份，不上传 Git
```

`scripts/rollback.sh` 仅适用于原本就运行了旧版 systemd 网关和用量桥的机器，不是新安装的通用回滚方式。

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

签到以外的积分，在 WorkBuddy 官方客户端头像菜单的「Buddy 加油站」「去邀约」「成长计划」，或[个人中心 → 套餐与用量](https://www.workbuddy.cn/profile/plans-usage)按当前账号的资格领取。成长计划中仅标为积分的奖励可计入余额，能量不是积分。已接入账号的官方余额由网关定期查询并同步到控制台和 CC Switch，不需要手工录入；「刷新状态」只是读取最近的网关快照，不会领取奖励。

## 调度策略：有余额的积分批次到期优先（expireFirst）

- 容器默认开启 `WB_SCHEDULING_STRATEGY=expireFirst`。在同一调度层级中，置顶规则不变；普通候选按尚有余额的有效积分批次最早失效时间排序，到期相同或均未知时按轮询。关闭该变量后不按到期排序。
- 控制台可展开查看上游已确认的批次总额、已用、剩余和到期时间。批次截止取积分周期和权益截止中较早的时间，不用套餐日期代替批次余额。已耗尽、已过期、数据缺失或过旧的批次不参与到期排序。
- 只影响网关选择哪个账号；实际扣减批次由 WorkBuddy 决定。免费、普通、零余额探测层级及保留积分、黑白名单、冷却和失效规则不变。
- 未单独设置的账号默认保留 100 积分，已知余额低于保留值时不进入调度；要允许该账号参与零余额免费模型或探测，先将其保留积分设为 `0`。

## 验证

```bash
python3 -B scripts/selftest.py          # 不触碰真实账号
python3 -B scripts/test_providers.py    # 自定义提供商持久化、校验与命令导出
python3 -B scripts/test_console.py      # 守护退避、缓存并发、登录会话与日志修剪
python3 -B scripts/test_permissions.py  # 先在虚拟环境安装 requirements-permissions.txt
python3 -B scripts/test_inference_probe.py # 流式结束、错误帧及模型匹配
docker compose config --quiet
docker compose ps
curl -i http://127.0.0.1:8080/api/summary  # 默认端口，未认证应返回 401
curl -i http://127.0.0.1:8318/usage        # 默认端口，未认证应返回 401
```

运行中的网关可用部署脚本分项检查。省略 `--inference-model` 时只检查模型目录和服务状态。指定模型时会发出一次真实请求并消耗少量上游用量，检查返回模型、文本和 SSE 结束标记。网页鉴权检查要求未登录请求返回 401。

```bash
python3 skill/scripts/deploy_workbuddy_ccswitch.py verify --dir . --inference-model kimi-k3-2
```

### 模型切换与推理失败

- WorkBuddy 网关根据额度、模型冷却和账号可用性为同一请求选账号。模型目录列出服务提供的模型，实际可用性取决于账号。遇到 `503 no_available_account` 时，在控制台核对账号余额及模型冷却，再用相同模型重新探测。
- Hermes 的 `model` 决定主模型，`fallback_providers` 决定自动降级。需要稳定的选模行为时，为目标 profile 选择通过推理验收的明确模型 ID，备用只配置一个独立且已通过验收的提供商。会话已有模型覆盖时，用新会话核对默认路由。主模型遇到限流时，Hermes 可能在冷却期间继续使用备用。
- Hermes 原生 `openai-codex` 使用自己的 OAuth 和 Responses 流。排查 Codex 时，在目标 profile 的新会话直接选定 Codex，检查 Hermes 日志中的首事件耗时、流是否完成和是否实际发生降级。WorkBuddy 侧用网关日志的请求标识、模型和流结束结果定位。
- Chat Completions 流在发送数据前提前关闭且缺少结束原因时，构建版网关可沿用首字节重试预算重试一次。已发出文本、推理或工具数据的流若中断，网关返回不完整错误。

## 安全约定

- 数据卷 `0700`，凭据/状态/token 文件 `0600`；进程降权运行。
- 网页 Basic Auth + 同源校验；API Key 只在 `/api/hermes/key` 返回给已认证会话，页面上默认脱敏。
- 网页不展示 UID、昵称或原始网关日志。新的登录二维码和备用链接只保存在当前会话内存中，不写入登录日志；会话结束后清除。
- 宿主机默认仅发布到 `127.0.0.1`。仅在明确需要、且网络可信时启用上述私有地址绑定；跨公网或不可信网络请保留回环设置并使用 SSH 隧道。
- 只接入本人或明确获授权的微信账号。

## 限制

- 二进制基于 **v1.14.47**；若要重建 `bin/workbuddy-gateway-manual`，运行 `python3 scripts/build_gateway.py`（Go 1.26+，`--goarch amd64|arm64` 可覆盖目标架构，默认跟随本机）。脚本验证上游提交、应用 `gateway-overlay/` 中的补丁并运行签到、调度策略和流式调用测试；升级前必须重新核对上游 `checkin.go`、`main.go` 选号逻辑与状态文件。
- 扫码必须由账号本人在网页显示的二维码/链接上完成，无法自动化。
- 账号池按可用性调度（当前为到期日最短优先，见上文），网页展示分账号信息不代表可在单次请求中固定使用某个账号。
- 二维码登录进程默认等待 `LOGIN_TIMEOUT`（300 秒）；网页状态查询检测到超时后会终止该会话。

## 文件

```
Dockerfile              镜像定义（python:3.11-slim + gosu 降权）
docker-compose.yml      端口映射与数据卷
docker-entrypoint.sh    卷属主修正 + 降权
bin/workbuddy-gateway-manual   本地生成，不纳入 Git
gateway-overlay/               手动签到补丁与到期优先调度补丁（含单元测试）
config/config.json.example  网关配置模板（首次启动生成密钥）
app/boot.py             启动编排（网关守护重启、启动时日志修剪、密钥生成）
app/controller.py       账号/登录会话/标签/Hermes 信息
app/checkin.py          状态快照与手动签到资格判定
app/usage_bridge.py     CC Switch 只读用量桥
app/web.py              网页与 API
app/static/index.html   配置网页
scripts/                跨平台网关构建、离线自测、备份、回滚、模型列表刷新
```
