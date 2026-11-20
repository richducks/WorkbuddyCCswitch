#!/usr/bin/env bash
# 回滚：停掉 Docker 实例，恢复宿主机原有的 systemd 网关 + 用量桥。
# 凭据是复制进 Docker 卷的，宿主机原文件从未删除，因此回滚无损。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" != "--yes" ]; then
  echo "这将停止 Docker 实例并恢复宿主机 workbuddy-gateway / workbuddy-usage 服务。"
  echo "注意：仅在 Docker 网页中新添加的账号不会自动回到宿主机（凭据仍保留在 data/gateway/）。"
  read -r -p "确认继续？输入 yes: " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

docker compose down
systemctl --user start workbuddy-gateway.service
sleep 2
systemctl --user start workbuddy-usage.service

echo "=== 服务状态 ==="
systemctl --user is-active workbuddy-gateway.service workbuddy-usage.service
echo "=== 监听端口 ==="
ss -ltnp 2>/dev/null | grep -E ':(8317|8318)\b' || true
echo "回滚完成。Hermes / CC Switch 配置无需改动（端点与 Key 均未变）。"
