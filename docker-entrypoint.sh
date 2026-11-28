#!/bin/sh
# Fix ownership for fresh and restored data, but never follow symlinks or
# change regular files with multiple hard links (which can alias a backup).
set -e
WB_UID="$(id -u wb)"
WB_GID="$(id -g wb)"
find /data/gateway /data/web -xdev \( -type d -o \( -type f -links 1 \) \) \
  -exec chown -h "${WB_UID}:${WB_GID}" {} + 2>/dev/null || true
chown "${WB_UID}:${WB_GID}" /data
chmod 700 /data/gateway /data/web
exec gosu wb "$@"
