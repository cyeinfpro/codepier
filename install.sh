#!/usr/bin/env bash
# Stable entry point in the complete CodePier source package.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ -f "$root/deploy/install-hub.sh" && -f "$root/compose.yml" ]] || { echo "请先解压完整 CodePier 源码包，再执行 install.sh。" >&2; exit 1; }
exec bash "$root/deploy/install-hub.sh" "$@"
