#!/usr/bin/env bash
# Build the Docker gateway variant from a pinned upstream release and local overlay.
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
revision=e142dd7640fc5e87541a3e5a51d3ae2e52c4317e
go_cmd=${GO:-go}
tmp=$(mktemp -d)
trap 'if [ -n "${candidate:-}" ]; then rm -f -- "$candidate"; fi; rm -rf -- "$tmp"' EXIT

git -c advice.detachedHead=false clone --quiet --depth 1 --branch v1.14.47 -- \
    https://github.com/CangShui/workbuddy-gateway.git "$tmp/src"
cd "$tmp/src"
if [[ $(git rev-parse HEAD) != "$revision" ]]; then
    printf >&2 'Unexpected WorkBuddy Gateway revision; refusing build.\n'
    exit 1
fi
git apply "$root/gateway-overlay/scheduler.patch"
cp -- "$root"/gateway-overlay/*.go .
export GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=linux GOARCH=amd64
"$go_cmd" test -run 'Checkin|CheckIn' .
mkdir -p "$root/bin"
candidate=$(mktemp "$root/bin/.manual-gateway-XXXXXX")
"$go_cmd" build -trimpath -buildvcs=false -ldflags='-s -w' -o "$candidate" .
chmod 755 "$candidate"
mv -- "$candidate" "$root/bin/workbuddy-gateway-manual"
