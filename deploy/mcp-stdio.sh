#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Optional owner-controlled environment file. Do not store secrets inside source control.
if [[ -f private/bridge.env ]]; then
  set -a
  source private/bridge.env
  set +a
fi
export CODEPIER_HUB_URL=${CODEPIER_HUB_URL:-${REMOTE_DEV_HUB_URL:-http://127.0.0.1:8765}}
export CODEPIER_TOKEN_FILE=${CODEPIER_TOKEN_FILE:-${REMOTE_DEV_TOKEN_FILE:-"$PWD/private/token.txt"}}
exec "${CODEPIER_PYTHON:-${REMOTE_DEV_PYTHON:-$PWD/.venv/bin/python}}" -m scripts.mcp_stdio_bridge
