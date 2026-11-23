#!/usr/bin/env bash
# 备份当前运行态：Docker 卷（含凭据）、Hermes 配置、CC Switch 数据库、systemd unit。
# 目录权限 700，不跟随符号链接；生成 SHA256SUMS 便于核对。
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
BACKUP_ROOT="$PWD/backup"
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
      sed -n '2,12p' "$0"
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
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"

STAMP=$(date +%Y%m%d-%H%M%S)
BK="$BACKUP_ROOT/$STAMP"
mkdir -p "$BK"/{docker-gateway,docker-web,hermes,ccswitch,units}
chmod 700 "$BK" "$BK"/*

cp -a data/gateway/. "$BK"/docker-gateway/ 2>/dev/null || true
cp -a data/web/. "$BK"/docker-web/ 2>/dev/null || true
cp -a "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" "$BK"/hermes/ 2>/dev/null || true
cp -a "$HOME/.cc-switch/cc-switch.db" "$BK"/ccswitch/ 2>/dev/null || true
cp -a "$HOME/.config/systemd/user/workbuddy-gateway.service" \
      "$HOME/.config/systemd/user/workbuddy-usage.service" "$BK"/units/ 2>/dev/null || true

chmod -R go-rwx "$BK"
( cd "$BK" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS ) 2>/dev/null \
  || ( cd "$BK" && : > SHA256SUMS )
echo "备份完成：$BK"

# 保留策略：目录名即时间戳，按名称倒序保留最近 KEEP 份。
if [ "$KEEP" -gt 0 ]; then
  removed=0
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    if [ -d "$old" ] && [ "$old" != "$BK" ]; then
      case "$(basename "$old")" in
        [0-9]*-[0-9]*) rm -rf -- "$old"; removed=$((removed + 1)) ;;
      esac
    fi
  done < <(printf '%s\n' "$BACKUP_ROOT"/[0-9]*-[0-9]* 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
  if [ "$removed" -gt 0 ]; then
    echo "已清理 $removed 份旧备份（保留 $KEEP 份）"
  fi
fi
