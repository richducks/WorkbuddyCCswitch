#!/bin/sh
# Bind mounts are created root-owned by Docker on first run; fix ownership,
# then drop to the unprivileged user for the whole stack.
set -e
WB_UID="$(id -u wb)"
WB_GID="$(id -g wb)"
chown "${WB_UID}:${WB_GID}" /data /data/gateway /data/web 2>/dev/null || true
chmod 700 /data/gateway /data/web 2>/dev/null || true
exec gosu wb python3 /app/app/boot.py "$@"
