# WorkBuddyCCswitch：交给 AI Agent 的部署与升级手册

**给 Agent 的任务：** 在目标主机上执行下面的流程。先识别现有实例，再决定是全新部署还是升级；不要只复述命令。完成后提供访问地址、实际运行版本和验证结果。不要求安装或修改 Hermes、CC Switch。

- 仓库：`https://github.com/richducks/WorkbuddyCCswitch.git`
- 默认版本：执行时 GitHub 的 `origin/main`。用户指定分支、标签或提交时，使用指定版本并解析成完整 SHA。本文以已发布的 `e16b7ec` 为基准；它不是强制固定版本。
- 默认服务：`workbuddy-hermes`。管理网页默认宿主端口 `8080`，本机已有部署是 `18080`；以实际发布端口为准。模型网关默认 `8317`，用量桥默认 `8318`。
- 可选输入：目标主机、现有部署目录、目标版本、三项宿主端口。如果只有一个明确匹配的实例，直接执行；如果有多个无法区分的实例、目标机器不可达或需要交互授权，才向用户确认。

## 1. 预检：先识别，再操作

1. 在 **Docker 实际运行的主机**执行。确认 Git、Python 3.9+、Docker 和 Compose 可用。Go 以目标提交的 `scripts/build_gateway.py` 和固定上游的最低版本为准；当前基准的上游 `go.mod` 要求至少 Go 1.26.5。安装 Docker 或系统依赖需要宿主机许可；只安装缺失项。Windows/macOS 使用 Docker Desktop 的 **Linux 容器**，Windows 使用 `python` 替代 `python3`。Apple Silicon 默认使用 `arm64`，Intel/AMD 默认使用 `amd64`，如 Docker 目标与本机不同，用 `scripts/build_gateway.py --goarch` 指定目标。
2. 执行 `docker context show`、`docker info`、`docker compose version`，核对连接的是预期守护进程。连接权限不足时，在 Ubuntu 本机终端用 `sudo docker info` 诊断；只有已获宿主授权才用 `sudo docker …`。Docker Desktop 未启动时先启动。如果 Agent 执行环境拒绝授权，报告拒绝的命令；**不要**放宽 Docker socket 权限、关闭浏览器沙箱或改为公网监听。
3. 枚举 Compose 服务，包括已经停止的实例：

   ```bash
   docker ps -a --filter label=com.docker.compose.service=workbuddy-hermes +     --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}'
   ```

   对选中的实例，用以下命令读取项目、Compose 文件和数据来源。不把完整 `docker inspect` 输出到聊天，因为其中可能含环境变量。示例使用默认容器名，实际执行时替换为刚确认的容器 ID 或名称：

   ```bash
   docker inspect workbuddy-hermes --format 'project={{index .Config.Labels "com.docker.compose.project"}}
   dir={{index .Config.Labels "com.docker.compose.project.working_dir"}}
   compose={{index .Config.Labels "com.docker.compose.project.config_files"}}
   image={{.Image}}{{range .Mounts}}{{println}}{{.Source}} -> {{.Destination}}{{end}}'
   docker port workbuddy-hermes
   ```

4. **分流：** 有匹配容器，或已有项目目录含 `.env` / 账号数据，按升级处理；既无匹配实例也无旧数据才走新部署。只检查私有文件是否存在，不输出其内容。已有目录但无运行实例时，核对其 Compose 项目名与数据来源后，可按升级流程恢复启动；不要擅自新建另一套空账号池。遇到目录或端口冲突，先确认占用方，不停止其他应用。
5. **避免重复重建：** 若已有实例健康，且容器的 `org.opencontainers.image.revision` 或部署记录中的镜像 ID 能与目标完整 SHA 一一对应，只执行第 4 节的验证。仅凭工作区 `git log` 或上游网关版本号不能跳过升级。

## 2. 准备目标版本（两种流程共用）

在独立目录检出目标版本。新部署时该目录就是部署目录；升级时使用持久的独立发布目录，例如现有部署目录旁的 `WorkbuddyCCswitch-releases/<提交号>`。**不要在有未提交改动的部署目录里运行 `git pull`、`git reset --hard`、`git clean`，也不要把私有数据复制进新检出。**

```bash
git clone --branch main https://github.com/richducks/WorkbuddyCCswitch.git <目标目录>
# 如指定了其他远端引用，先获取该引用，再在独立检出中切到对应提交。
git -C <目标目录> rev-parse HEAD
```

在刚检出的目标目录执行：

```bash
python3 -B scripts/selftest.py
python3 -B scripts/test_console.py
python3 -B scripts/test_providers.py
python3 -B scripts/test_inference_probe.py
python3 scripts/build_gateway.py
```

- 构建网关的脚本固定上游源码并校验提交，生成 `bin/workbuddy-gateway-manual`。二进制不在 GitHub 源码里，**不能仅凭旧文件存在就跳过构建**。Go 不在 PATH 时，使用 `--go /绝对路径/go`；仓库的 `skill/scripts/deploy_workbuddy_ccswitch.py deps --only go` 可安装经校验的用户级工具链。
- 启用角色权限模式的实例，可在临时虚拟环境安装 `requirements-permissions.txt`，再运行 `scripts/test_permissions.py`。构建与必要测试失败时先修复，不切换现有服务。若测试需要本地回环监听而沙箱禁止，在已授权的宿主环境重试，不跳过构建脚本的测试。
- 仓库自带 `skill/scripts/deploy_workbuddy_ccswitch.py deploy` 可用于简单的新部署；**不要**用它直接升级脏工作区或复用旧网关二进制。

## 3A. 全新部署

1. 仅在 `.env` 不存在时，从 `.env.example` 复制。Linux 输入目标宿主用户的真实 UID/GID；Docker Desktop 可先保留默认配置。保留 `BIND_IP=127.0.0.1`。控制台只有账号密码一种登录方式：初始账号 `admin`，初始密码 `123456`，仅限本机 `BIND_IP=127.0.0.1` 使用；登录后顶部有可关闭的改密提示，`BIND_IP` 指向局域网前必须先修改初始密码（或在启动前把至少 12 个字符的密码写入 `0600` 的 `data/web/permissions-bootstrap`）。按预检结果选择空闲端口，记录最终值。
2. 在新部署目录运行以下命令；如上一步使用了合法的 sudo 授权，给这些 Docker 命令加相同前缀：

   ```bash
   docker compose config --quiet
   docker compose build workbuddy-hermes
   docker compose up -d --no-build --wait --wait-timeout 180 workbuddy-hermes
   docker compose ps workbuddy-hermes
   ```

3. 执行第 4 节。新实例没接账号时，空账号池是正常状态。需要登录时，指引用户在控制台安全地使用本机凭据；需要接入微信账号时由用户本人扫码。不要求用户在聊天传密码、令牌、Cookie 或登录二维码。部署不自动发起签到或消耗积分的模型请求。
4. 在部署目录外保留一份本地部署记录：项目名、部署目录、目标完整 SHA、实际运行镜像 ID 和验证时间。不记录环境变量值或凭据。

## 3B. 升级现有实例（保留原数据和本地改动）

### 在中断服务前准备

1. 记录原实例 ID、Compose 项目名、工作目录、原 Compose 文件顺序、实际使用的 `.env` / `--env-file`、UID/GID、认证模式和宿主端口。核对数据挂载：`/data/gateway` 和 `/data/web` 必须指向原实例的同一对持久化目录。不要把空的发布目录当作数据目录。解析已知 Compose 配置时只提取需要比对的字段，不打印完整配置。
2. 准备两个 Compose 命令前缀（用平台合适的参数数组，路径逐项引用；后文用 `OLD_COMPOSE`、`NEW_COMPOSE` 表示，不是要输入的命令名）：

   - **旧前缀**：`docker compose --project-directory <原部署目录> -p <原项目名>`，沿用实例的 `--env-file` 和原有 `-f` 文件顺序。没有自定义环境文件时，沿用原部署目录下的默认 `.env`。
   - **新前缀**：保留上述项目名、项目目录、环境文件和用户的附加覆盖文件，只把原仓库的基础 `docker-compose.yml` 换为目标发布目录中的同名文件，并把下一步的发布覆盖文件加在最后。如果原实例由本手册升级过，只从新前缀中去掉上次生成的 `compose.release.json`，旧前缀原样保留供回滚。对于手写 Compose 或多个无法自动判定的基础文件，先确认目标配置，不以猜测替换正在运行的项目。

3. 在发布目录新建 `compose.release.json`（JSON 是 Compose 接受的 YAML 子集）。替换值，不写凭据。用原容器名，可防止无意中创建第二套运行相同账号的实例；用带提交号的镜像标签保留版本身份。Windows 路径用生成 JSON 的工具正确转义。

   ```json
   {
     "services": {
       "workbuddy-hermes": {
         "build": {"context": "<发布目录绝对路径>"},
         "container_name": "<原容器名>",
         "image": "workbuddy-hermes-stack:release-<目标短SHA>",
         "labels": {"org.opencontainers.image.revision": "<目标完整SHA>"}
       }
     }
   }
   ```

4. 分别运行两个前缀的 `config --quiet`，再把 `config --format json` 在本地解析，对照旧容器核实项目、容器名、端口、认证开关，以及两个数据挂载的**绝对源路径与容器目标**。确认新配置的 `build.context` 是独立发布目录。保留已有自定义覆盖；若项目配置会改动已有挂载、认证或外部监听，不切换，先处理差异。
5. 用新前缀执行 `build workbuddy-hermes`，此时旧实例仍运行。记下构建出的镜像 ID。在切换前，把原实例的镜像 ID 标记为一个独立回滚标签，例如：

   ```bash
   docker image tag <原镜像ID> workbuddy-hermes-stack:rollback-<时间戳>
   ```

   如果没有任何旧容器，检查本地是否还有原配置对应的镜像；没有则如实记录「只能恢复数据和配置，不能恢复到原镜像」，不要造一个回滚标签。

### 备份、切换、出错即恢复

6. 在非 Git、非公开同步目录准备私有备份（Linux/macOS 目录权限 `0700`，Windows 限当前用户和管理员）。保留原 Compose 文件、原环境文件的私有副本、原提交或本地版本信息、回滚镜像标签和数据挂载路径。预检查空间与读取权限后，若旧容器正在运行，用旧前缀 `stop workbuddy-hermes` 短暂停服，再备份两个数据目录，保证 SQLite 等状态一致。用平台自己的归档工具保留文件权限和符号链接本身，不跟随链接，跳过套接字；检查归档可读取，校验 SHA-256，不要输出账号文件。若备份失败，停止升级；如果本次操作停了原容器，直接 `docker start <原容器ID>` 恢复它。
7. 用新前缀执行 `up -d --no-build --wait --wait-timeout 180 workbuddy-hermes`，随后完成第 4 节。记录完整新前缀以及覆盖文件位置，供下一次维护继续使用。原工作区不必切换分支；不要在该旧目录用省略了 `-f` 的简写启动旧配置。不要为了升级执行 `down -v` 或清理旧镜像。
8. **服务切换失败或核心健康检查不通过：** 用原 Compose 前缀，附加一个只覆盖 `services.workbuddy-hermes.image` 为回滚标签的 JSON 文件，运行 `up -d --no-build --force-recreate --wait --wait-timeout 180 workbuddy-hermes`。再核对镜像 ID、数据挂载、端口和旧实例健康。若新版本写入了旧版本无法读取的数据，保留失败现场和升级前备份，不把旧快照直接覆盖已经变化的数据；将恢复范围与是否可接受丢失升级后变化交由用户决定。

## 4. 验证真的在运行（不是只看构建成功）

- [ ] 目标 Compose 服务显示 `healthy`，实际镜像 ID 与候选镜像 ID 相同；升级时复核两个持久化挂载源没有变化。
- [ ] 用本机发布端口访问管理网页。匿名访问首页应跳转登录，匿名访问 `/api/summary` 应返回 `401`。账号密码模式（默认）下，已改密的老实例不要尝试初始密码，仅验证登录页可达或用用户提供的凭据登录；新实例可用初始 `admin` / `123456` 登录并确认顶部改密提示条出现。`/healthz` 可用于健康检查。不要把登录跳转误报成失败。若允许使用既有合法会话，可验证登录后页面和账号池，但不将 Cookie 或正文中的个人信息带进日志。
- [ ] 确认镜像含目标代码。对于本文基准，可在目标 Compose 前缀后执行 `exec -T workbuddy-hermes grep -n account-title-height /app/app/static/index.html`。后续版本可用容器内对应文件的 SHA-256 与目标检出对照。运行 `exec -T workbuddy-hermes /app/bin/workbuddy-gateway version` 只能确认上游版本；它不能代替本项目 Git 提交或镜像 ID。
- [ ] 在不消耗积分的前提下检查网关 `/v1/models` 和用量桥 `/usage` 的既定鉴权行为。另获用户许可时，才使用既有账户做真实推理或签到；没有账户不报告这些流程已验证。可使用项目自带 `verify` 做标准部署的只读检查，但非默认端口、独立 Compose 文件或自定义认证模式应按上面实际配置验证。
- [ ] 浏览器有合法会话时，在桌面端上下滚动微信账号池，核对标题、列标题吸顶；手机端仍为卡片。检查独立的自动签到列、置顶图标、点击名称改名入口以及积分批次展开。无需为验证切换真实账号设置。没权限启动浏览器或没登录，就写「浏览器实测未完成」，不要把静态检查当成交互验收。

**最终回报：** 新部署 / 升级 / 已在目标版本；完整 Git SHA 和实际运行镜像 ID；管理网页、网关 `/v1`、用量桥 `/usage` 地址；通过的检查、未验证项和原因；升级备份位置、回滚标签。只报告验证证据，不报告凭据。服务位于远端时，优先按仓库 README 使用 SSH 隧道访问管理网页和用量桥，不自动新增公网映射。
