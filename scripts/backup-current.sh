#!/usr/bin/env bash
# 备份当前运行态：Docker 卷（含凭据）、Hermes 配置、CC Switch 数据库、systemd unit。
# 先停止 Docker 实例再运行。目录权限 700，不跟随卷内符号链接。
#
# 用法：bash scripts/backup-current.sh [--keep N] [--backup-dir DIR]
#   --keep N        只保留最近 N 份备份（默认 7；0 = 不清理）
#   --backup-dir D  备份根目录（默认 <仓库>/backup）
#
# 定时执行示例（crontab -e，每天 03:10 一份、保留 14 份）：
#   10 3 * * * cd /path/to/WorkbuddyCCswitch && bash scripts/backup-current.sh --keep 14 >> backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP=7
BACKUP_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep)
      KEEP="${2:-}"
      shift 2 || { echo "--keep 需要一个数字" >&2; exit 2; }
      ;;
    --backup-dir)
      BACKUP_ROOT="${2:-}"
      [ -n "$BACKUP_ROOT" ] || { echo "--backup-dir 需要一个目录" >&2; exit 2; }
      shift 2 || { echo "--backup-dir 需要一个目录" >&2; exit 2; }
      ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数：$1（见 --help）" >&2
      exit 2
      ;;
  esac
done
case "$KEEP" in
  ''|*[!0-9]*) echo "--keep 必须是非负整数，收到：$KEEP" >&2; exit 2 ;;
esac

umask 077
BACKUP_ROOT="${BACKUP_ROOT:-$PWD/backup}"
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT" 2>/dev/null || true

STAMP=$(date +%Y%m%d-%H%M%S)
BK=$(mktemp -d "$BACKUP_ROOT/$STAMP.XXXXXX")
trap 'printf "备份未完成，请勿用于恢复：%s\n" "$BK" >&2' ERR
chmod 700 "$BK" "$BK"/* 2>/dev/null || true
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

# 保留策略：目录名以时间戳开头，按名称倒序保留最近 KEEP 份。
if [ "$KEEP" -gt 0 ]; then
  removed=0
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    if [ -d "$old" ] && [ "$old" != "$BK" ]; then
      case "$(basename "$old")" in
        [0-9]*-*) rm -rf -- "$old"; removed=$((removed + 1)) ;;
      esac
    fi
  done < <(printf '%s\n' "$BACKUP_ROOT"/[0-9]*-* 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
  if [ "$removed" -gt 0 ]; then
    echo "已清理 $removed 份旧备份（保留 $KEEP 份）"
  fi
fi
