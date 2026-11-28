#!/usr/bin/env bash
# 备份当前运行态：Docker 卷（含凭据）、Hermes 配置、CC Switch 数据库、systemd unit。
# 先停止 Docker 实例再运行。目录权限 700，不跟随卷内符号链接。
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backup
BK=$(mktemp -d "$PWD/backup/$STAMP.XXXXXX")
trap 'printf "备份未完成，请勿用于恢复：%s\n" "$BK" >&2' ERR
mkdir -p "$BK"/{docker-gateway,docker-web,hermes,ccswitch,units}

copy_if_present() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        cp -a "$1" "$2"
    fi
}

copy_if_present data/gateway/. "$BK/docker-gateway/"
copy_if_present data/web/. "$BK/docker-web/"
copy_if_present .env "$BK/stack.env"
copy_if_present "$HOME/.hermes/config.yaml" "$BK/hermes/"
copy_if_present "$HOME/.hermes/.env" "$BK/hermes/"
copy_if_present "$HOME/.config/systemd/user/workbuddy-gateway.service" "$BK/units/"
copy_if_present "$HOME/.config/systemd/user/workbuddy-usage.service" "$BK/units/"

# CC Switch may still be running with a WAL; back up the SQLite database, not just its main file.
cc_db="$HOME/.cc-switch/cc-switch.db"
if [ -f "$cc_db" ]; then
    python3 - "$cc_db" "$BK/ccswitch/cc-switch.db" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
with closing(sqlite3.connect(uri, uri=True)) as src, closing(sqlite3.connect(sys.argv[2])) as dst:
    src.backup(dst)
PY
fi

chmod -R go-rwx "$BK"
if command -v sha256sum >/dev/null; then
    hash=(sha256sum)
else
    hash=(shasum -a 256)
fi
(
    cd "$BK"
    find . -type f ! -path './SHA256SUMS' -exec "${hash[@]}" {} +
) > "$BK/SHA256SUMS"
test -s "$BK/SHA256SUMS"
echo "备份完成：$BK"
