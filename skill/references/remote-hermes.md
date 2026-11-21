# 远程部署 + 本地 Hermes 对接

适用于：Docker 栈跑在服务器上，Hermes / CC Switch 跑在你自己的电脑上。

## 端口与暴露边界

| 端口（容器内） | 用途 | 允许的暴露方式 |
|---|---|---|
| 8317 `/v1` | 模型 API（Hermes 调用） | SSH 隧道；或 HTTPS 反代公开 `/v1/`（带强 Bearer key） |
| 8080 | 管理网页 | 仅 SSH 隧道 / 本机 |
| 8318 | CC Switch 用量桥 | 仅 SSH 隧道 / 本机 |

compose 固定发布到宿主机 `127.0.0.1`。不要改成 `0.0.0.0`；公开只做反代，且只反代 `/v1/`。

## 方式一：SSH 隧道（推荐）

在本地电脑执行（三种写法等价，选其一）：

```bash
# 技能脚本（网关→18317、网页→18417、用量桥→18318）
python3 skill/scripts/deploy_workbuddy_ccswitch.py tunnel user@server --port 18317

# 等价手写命令
ssh -NT -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18317:127.0.0.1:8317 \
  -L 127.0.0.1:18417:127.0.0.1:8080 \
  -L 127.0.0.1:18318:127.0.0.1:8318 \
  user@server
```

服务器 `.env` 改过端口时，把 `-L` 里的服务器侧端口换成 `.env` 里的值。

隧道运行中，本地浏览器开 `http://127.0.0.1:18417/` 管理账号；本地 Hermes 用 `http://127.0.0.1:18317/v1`。

## 方式二：公网 HTTPS 反代（仅 /v1/）

只有模型 API 适合公网；网页和用量桥保持私有。

```caddy
# Caddy 示例：只代理 /v1/，网关自带的 Bearer 鉴权仍然生效
example.com {
    handle /v1/* {
        reverse_proxy 127.0.0.1:8317
    }
}
```

- 无域名用 IP 证书时注意证书有效期较短（约 6 天），必须配自动续期。
- key 用网关 `config.json` 的 `gateway.apiKey`；泄露立即在服务器上轮换。

## 本地 Hermes 导入 provider

控制台「模型提供商」卡的 WorkBuddy 条目可直接复制配置命令。隧道场景把 `base_url` 换成隧道地址，形如：

```bash
hermes config set --force providers.workbuddy '{
  "base_url": "http://127.0.0.1:18317/v1",
  "transport": "chat_completions",
  "default_model": "<网关模型>",
  "models": [<控制台复制的模型列表>],
  "key_env": "WORKBUDDY_API_KEY"
}'
```

- 实际 key 只写入本地 Hermes 凭据环境（`WORKBUDDY_API_KEY`），不要写进命令行历史或聊天。
- 导入后 `hermes model` 选择；CC Switch 的用量行把 `baseUrl` 指到 `http://127.0.0.1:18318/usage`，bridge token 用服务器 `data/web/bridge.token` 的值。
- 控制台导入 ≠ 推理可用：导入后跑一次真实对话才算打通。

## 跨机迁移已有账号/签到状态

把旧实例的账号、签到计划、provider 预设带到服务器：

1. 旧机：`docker compose down`（停旧实例，避免两套实例共用账号状态）。
2. 打包 `data/gateway` 与 `data/web`，**排除** `*.sock`、`logs/`、二维码缓存：
   `tar --exclude='*.sock' --exclude='*/logs/*' -czf wbcc-data.tgz data/`
3. 经可信私有通道传到服务器，解到新部署目录，`chown -R <部署用户>:<同GID>` 后 `chmod 700 data/gateway data/web`。
4. 迁移保留原 apiKey/bridge token → Hermes/CC Switch 零改动；全新部署则生成新凭据 → 本地 `key_env` 与 bridge token 同步替换。
5. `docker compose up -d` 后用技能 `verify` 确认 healthy，再核对网页账号与签到计划是否完整。

## 故障速查

| 现象 | 处理 |
|---|---|
| `verify` 报容器不 healthy | `docker compose logs` 看启动日志；确认 `.env` UID/GID 对 `data/` 有写权限 |
| 网页 401 但密码正确 | 密码来自部署主机 `data/web/web.token`；隧道端口 ≠ 容器端口，别混用 |
| Hermes 连 8317 超时 | 隧道没起或服务器侧端口改过；先 `curl http://127.0.0.1:18317/v1/models -H "Authorization: Bearer <key>"` 分辨本地/服务器问题 |
| clone 报权限 | 私库需要 GitHub 凭据：`gh auth status`、部署 token 或 SSH key |
