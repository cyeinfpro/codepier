#!/usr/bin/env bash
set -euo pipefail
input_directory=$PWD
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "需要 Python 3.12 或更高版本")'
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements-agent.txt
if [[ -n "${1:-}" ]]; then
  pairing=$1
else
  read -r -p '配对文件完整路径：' pairing
fi
if [[ -n "${2:-}" ]]; then
  root=$2
else
  read -r -p '允许访问的项目父目录：' root
fi
[[ -n "$pairing" && -n "$root" ]] || { echo '配对文件和项目父目录不能为空' >&2; exit 1; }
case "$pairing" in /*|~*) ;; *) pairing="$input_directory/$pairing" ;; esac
case "$root" in /*|~*) ;; *) root="$input_directory/$root" ;; esac
.venv/bin/python -m agent init --pairing-file "$pairing" --allow "$root" --shell "${3:-full}"
printf '\n启动：%s/deploy/start-agent.sh\n修改地址：%s/.venv/bin/python -m agent configure --hub http://新IP:端口\n' "$PWD" "$PWD"
