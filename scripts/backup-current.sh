#!/usr/bin/env bash
# 备份当前运行态：Docker 卷（含凭据）、Hermes 配置、CC Switch 数据库、systemd unit。
# 目录权限 700，文件不含符号链接跟随；生成 SHA256SUMS 便于核对。
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date +%Y%m%d-%H%M%S)
BK="$PWD/backup/$STAMP"
mkdir -p "$BK"/{docker-gateway,docker-web,hermes,ccswitch,units}
chmod 700 "$BK" "$BK"/*

cp -a data/gateway/. "$BK"/docker-gateway/ 2>/dev/null || true
cp -a data/web/. "$BK"/docker-web/ 2>/dev/null || true
cp -a "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" "$BK"/hermes/ 2>/dev/null || true
cp -a "$HOME/.cc-switch/cc-switch.db" "$BK"/ccswitch/ 2>/dev/null || true
cp -a "$HOME/.config/systemd/user/workbuddy-gateway.service" \
      "$HOME/.config/systemd/user/workbuddy-usage.service" "$BK"/units/ 2>/dev/null || true

chmod -R go-rwx "$BK"
( cd "$BK" && find . -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
echo "备份完成：$BK"
