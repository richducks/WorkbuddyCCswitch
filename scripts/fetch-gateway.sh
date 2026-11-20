#!/usr/bin/env bash
# Re-vendor the pinned WorkBuddy Gateway binary with SHA256 verification.
set -euo pipefail
VERSION="${VERSION:-v1.14.47}"
EXPECTED="${EXPECTED_SHA256:-68ac686e536711cb2e98b3018b26b4ced534bd954d886e088a5f4f30dfb55579}"
URL="https://github.com/CangShui/workbuddy-gateway/releases/download/${VERSION}/workbuddy-gateway-linux-amd64"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading ${VERSION} ..."
curl -fL --retry 3 --connect-timeout 20 -o "$TMP/workbuddy-gateway" "$URL"
ACTUAL="$(sha256sum "$TMP/workbuddy-gateway" | awk '{print $1}')"
if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  echo "SHA256 mismatch: expected $EXPECTED, got $ACTUAL" >&2
  exit 1
fi
install -m 755 "$TMP/workbuddy-gateway" "$HERE/bin/workbuddy-gateway"
echo "Verified and installed: $ACTUAL"
