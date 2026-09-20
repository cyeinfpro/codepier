#!/usr/bin/env bash
# Served by the panel. Installs only for the account running this command.
set -euo pipefail
umask 077
codepier_hub='' codepier_token='' codepier_sha='' codepier_allow=''
codepier_dir="$HOME/.codepier-agent"
codepier_explicit_dir=0
codepier_service=1 codepier_action=install codepier_yes=0 codepier_expected=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hub|--token|--sha256|--allow|--install-dir|--expected-device)
      [[ $# -ge 2 ]] || { echo 'Missing argument' >&2; exit 2; }
      case "$1" in
        --hub) codepier_hub=$2;; --token) codepier_token=$2;; --sha256) codepier_sha=$2;;
        --allow) codepier_allow=$2;; --install-dir) codepier_dir=$2; codepier_explicit_dir=1;; --expected-device) codepier_expected=$2;;
      esac; shift 2;;
    install|upgrade|uninstall|status|--upgrade|--uninstall|--status)
      [[ "$codepier_action" == install ]] || { echo 'Choose only one action' >&2; exit 2; }
      codepier_action=${1#--}; shift;;
    --yes) codepier_yes=1; shift;;
    --help|-h) echo 'Usage: bash install-from-hub.sh [install|upgrade|uninstall|status] [--install-dir PATH] [--yes for uninstall]'; exit 0;;
    --no-service) codepier_service=0; shift;;
    *) echo 'Unknown installer option' >&2; exit 2;;
  esac
done
# Only the known migration alias is accepted; custom paths are never guessed.
if [[ "$codepier_explicit_dir" == 0 ]]; then
  codepier_old="$HOME/.remote-dev-agent"
  if [[ -e "$codepier_dir" && -e "$codepier_old" && ! "$codepier_dir" -ef "$codepier_old" ]]; then
    echo 'Both CodePier and legacy installations exist; no merge attempted.' >&2; exit 2
  fi
  if [[ ! -e "$codepier_dir" && -e "$codepier_old" ]]; then codepier_dir="$codepier_old"; fi
fi
if [[ -L "$codepier_dir" && "${codepier_dir##*/}" == .remote-dev-agent ]]; then
  codepier_canonical="${codepier_dir%/*}/.codepier-agent"
  [[ -d "$codepier_canonical" && ! -L "$codepier_canonical" && "$codepier_dir" -ef "$codepier_canonical" ]] || { echo 'Unrecognized legacy alias' >&2; exit 2; }
  codepier_dir=$(cd -- "$codepier_canonical" && pwd -P)
fi
[[ "$codepier_dir" == /* ]] || { echo 'Install directory must be absolute' >&2; exit 2; }
[[ "$codepier_dir" != / && "$codepier_dir" != "$HOME" && ! -L "$codepier_dir" ]] || { echo "Refusing unsafe installation directory" >&2; exit 2; }
if [[ "$codepier_action" == uninstall && ! -e "$codepier_dir" ]]; then
  echo "Agent is not installed; nothing to remove."; exit 0
fi
[[ "$codepier_service" == 1 || "$codepier_action" == install ]] || { echo "--no-service is only valid for installation" >&2; exit 2; }
if [[ "$codepier_action" != install && -z "$codepier_hub" ]]; then
  codepier_helper="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/scripts/install_agent.py"
  [[ -f "$codepier_helper" ]] || { echo 'Run the installed agentctl or the script from the complete source package.' >&2; exit 1; }
  codepier_python="$codepier_dir/runtime/.venv/bin/python"
  if [[ ! -x "$codepier_python" ]]; then
    codepier_python=$(command -v python3) || { echo 'No local Python found; use a new panel repair command.' >&2; exit 1; }
  fi
  codepier_python=$("$codepier_python" -c 'import sys; print(getattr(sys, "_base_executable", None) or sys.executable)')
  codepier_args=("--$codepier_action" --install-dir "$codepier_dir")
  [[ "$codepier_yes" == 0 ]] || codepier_args+=(--yes)
  [[ -z "$codepier_expected" ]] || codepier_args+=(--expected-device "$codepier_expected")
  exec "$codepier_python" "$codepier_helper" "${codepier_args[@]}"
fi
[[ "$codepier_hub" == https://* || "$codepier_hub" == http://* ]] || { echo 'Invalid panel URL' >&2; exit 2; }
[[ "$codepier_sha" =~ ^[a-f0-9]{64}$ ]] || { echo 'Invalid package checksum' >&2; exit 2; }
if [[ "$codepier_action" == install ]]; then
  [[ "$codepier_token" == rdi_* ]] || { echo 'Invalid install ticket' >&2; exit 2; }
  [[ "$codepier_allow" == /* && -d "$codepier_allow" ]] || { echo 'Allowed directory must already exist and be absolute' >&2; exit 2; }
fi
# Existing installations are validated by Python and enter the atomic repair path.
command -v curl >/dev/null || { echo 'Install curl first.' >&2; exit 1; }
codepier_tmp=$(mktemp -d)
trap 'rm -rf "$codepier_tmp"' EXIT
mkdir -p "$codepier_dir/tools"
export UV_PYTHON_INSTALL_DIR="$codepier_dir/python"
export UV_NO_MODIFY_PATH=1
if [[ -x "$codepier_dir/tools/uv" ]]; then
  codepier_uv="$codepier_dir/tools/uv"
elif command -v uv >/dev/null; then
  codepier_uv=$(command -v uv)
else
  echo 'Preparing private Python installer…'
  curl -fsSL --max-time 120 --output "$codepier_tmp/uv.sh" https://astral.sh/uv/0.10.8/install.sh
  if command -v sha256sum >/dev/null; then codepier_uv_sha=$(sha256sum "$codepier_tmp/uv.sh");
  else codepier_uv_sha=$(shasum -a 256 "$codepier_tmp/uv.sh"); fi
  [[ "${codepier_uv_sha%% *}" == eae5e1dae89cd0b74d357f549ccd6faa94b2ad6c1d89d78972a625655a4556ae ]] || { echo 'Python installer checksum mismatch' >&2; exit 1; }
  UV_UNMANAGED_INSTALL="$codepier_dir/tools" sh "$codepier_tmp/uv.sh"
  codepier_uv="$codepier_dir/tools/uv"
fi
codepier_python=$("$codepier_uv" python find --managed-python 3.13 2>/dev/null) || {
  "$codepier_uv" python install --no-bin 3.13
  codepier_python=$("$codepier_uv" python find --managed-python 3.13)
}
echo 'Downloading Agent from panel…'
curl -fsSL --max-time 120 --max-filesize 8388608 --output "$codepier_tmp/agent.zip" "${codepier_hub%/}/agent/agent.zip?sha256=$codepier_sha"
# Execute only the helper extracted from the exact package pinned by the panel.
"$codepier_python" - "$codepier_tmp/agent.zip" "$codepier_sha" "$codepier_tmp/install_agent.py" <<'PY'
import hashlib,pathlib,sys,zipfile
archive, expected, target = sys.argv[1:]
raw=pathlib.Path(archive).read_bytes()
if len(raw)>8388608 or hashlib.sha256(raw).hexdigest()!=expected:
    raise SystemExit('Agent package checksum mismatch; generate a new command in the panel.')
with zipfile.ZipFile(archive) as z:
    item=z.getinfo('scripts/install_agent.py')
    if item.file_size>131072: raise SystemExit('Invalid installer size')
    pathlib.Path(target).write_bytes(z.read(item))
PY
export CODEPIER_INSTALL_TOKEN="$codepier_token"
codepier_token=''
codepier_args=(--archive "$codepier_tmp/agent.zip" --sha256 "$codepier_sha" --hub "$codepier_hub" --install-dir "$codepier_dir" --uv "$codepier_uv")
if [[ "$codepier_action" == install ]]; then
  codepier_args+=(--allow "$codepier_allow")
  [[ "$codepier_service" == 1 ]] || codepier_args+=(--no-service)
else
  codepier_args+=("--$codepier_action")
fi
[[ "$codepier_yes" == 0 ]] || codepier_args+=(--yes)
[[ -z "$codepier_expected" ]] || codepier_args+=(--expected-device "$codepier_expected")
"$codepier_python" "$codepier_tmp/install_agent.py" "${codepier_args[@]}"
