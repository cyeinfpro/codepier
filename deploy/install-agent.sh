#!/usr/bin/env bash
set -euo pipefail
input_directory=$PWD
source_directory=$(cd "$(dirname "$0")/.." && pwd)
install_directory="${HOME}/.codepier-agent"
if [[ ! -e "$install_directory" && -d "${HOME}/.remote-dev-agent" ]]; then
  install_directory="${HOME}/.remote-dev-agent"
fi
bootstrap_python=${PYTHON:-python3}
if [[ -x "$install_directory/runtime/.venv/bin/python" && -z "${PYTHON:-}" ]]; then
  bootstrap_python="$install_directory/runtime/.venv/bin/python"
fi
"$bootstrap_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,13) else "需要 Python 3.13 或更高版本")'
pairing=${1:-}
root=${2:-}
shell_mode=${3:-full}
if [[ ! -f "$install_directory/config.json" ]]; then
  [[ -n "$pairing" ]] || read -r -p '配对文件完整路径：' pairing
  [[ -n "$root" ]] || read -r -p '允许访问的项目父目录：' root
  [[ -n "$pairing" && -n "$root" ]] || { echo '配对文件和项目父目录不能为空' >&2; exit 1; }
fi
args=("$source_directory/scripts/install_agent.py" --source "$source_directory" --shell "$shell_mode")
if [[ -n "$pairing" ]]; then
  case "$pairing" in /*|~*) ;; *) pairing="$input_directory/$pairing" ;; esac
  args+=(--pairing-file "$pairing")
fi
if [[ -n "$root" ]]; then
  case "$root" in /*|~*) ;; *) root="$input_directory/$root" ;; esac
  args+=(--allow "$root")
fi
# The installer may replace runtime; execute with its external base interpreter.
bootstrap_python=$("$bootstrap_python" -c 'import sys; print(getattr(sys, "_base_executable", None) or sys.executable)')
exec "$bootstrap_python" "${args[@]}"
