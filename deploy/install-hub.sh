#!/usr/bin/env bash
set -euo pipefail
caller_directory=$PWD
cd "$(dirname "$0")/.."
root=$(pwd -P)
host='' port='' username='' password_file='' noninteractive=0
usage() {
  cat <<'HELP'
CodePier · 码头 — 面板安装与更新
  bash install.sh
  bash install.sh --host 192.0.2.20 --port 8765 --username admin --password-file /安全路径/admin-password --non-interactive
选项：--host 主机名/IP  --port 端口  --username 管理员  --password-file 私密密码文件
      --non-interactive 无人值守；首次安装必须提供 host 和密码文件或 CODEPIER_ADMIN_PASSWORD
需要 Docker Engine、Compose 插件和主机 Python 3.9+（只使用标准库）。
旧版更新自动迁移已识别的项目名与数据卷；保留账号、密钥、权限和历史。
自定义数据挂载或新旧安装冲突会停止，不会创建空面板冒充升级成功。
HTTPS 安装须保留原 COMPOSE_FILE，连同 Caddy 数据及证书卷一起迁移。
HELP
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host|--port|--username|--password-file)
      [[ $# -ge 2 && -n "$2" ]] || { echo '选项缺少参数' >&2; exit 2; }
      case "$1" in --host) host=$2;; --port) port=$2;; --username) username=$2;; --password-file) password_file=$2;; esac
      shift 2;;
    --non-interactive) noninteractive=1; shift;;
    --help|-h) usage; exit 0;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2;;
  esac
done
[[ ! -L .env ]] || { echo '.env 不能是符号链接；未修改任何配置。' >&2; exit 1; }
# Canonical environment wins; the old operator name remains a read-only input alias.
export CODEPIER_ADMIN_PASSWORD="${CODEPIER_ADMIN_PASSWORD:-${RD_ADMIN_PASSWORD:-}}"
if [[ -n "$password_file" ]]; then
  [[ "$password_file" == /* ]] || password_file="$caller_directory/$password_file"
  [[ -f "$password_file" && ! -L "$password_file" && -r "$password_file" ]] || { echo '密码文件不存在、不可读或是符号链接' >&2; exit 1; }
  CODEPIER_ADMIN_PASSWORD="$(cat -- "$password_file")"
fi
bootstrap_python=${CODEPIER_BOOTSTRAP_PYTHON:-python3}
command -v "$bootstrap_python" >/dev/null || { echo '请先安装主机 Python 3.9+，或设置 CODEPIER_BOOTSTRAP_PYTHON。' >&2; exit 1; }
"$bootstrap_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || { echo '主机 Python 版本需要 3.9+' >&2; exit 1; }
command -v docker >/dev/null || { echo '请先安装 Docker Engine 与 Compose 插件。' >&2; exit 1; }
export COMPOSE_PROJECT_NAME=codepier
docker compose version >/dev/null
lock="$root/.codepier-install.lock"
mkdir "$lock" 2>/dev/null || { echo '另一个安装进程或未完成安装锁存在；请先检查 .codepier-install.lock。' >&2; exit 1; }
printf '%s\n' "$$" > "$lock/pid"
migration_started=0
env_tmp=''
cleanup() {
  status=$?
  trap - EXIT
  if ((status != 0 && migration_started == 1)); then
    # Before first new write, restore the old containers. After that boundary,
    # preserve both data versions and report recovery_required, never stale rollback.
    "$bootstrap_python" "$root/scripts/migrate_hub.py" rollback --root "$root" || true
  fi
  [[ -z "$env_tmp" ]] || rm -f -- "$env_tmp"
  unset CODEPIER_ADMIN_PASSWORD RD_ADMIN_PASSWORD
  if [[ -f "$lock/pid" && "$(cat "$lock/pid")" == "$$" ]]; then rm -f -- "$lock/pid"; rmdir "$lock" 2>/dev/null || true; fi
  exit "$status"
}
trap cleanup EXIT
if [[ ! -f .env ]]; then
  if [[ -z "$host" ]]; then
    ((noninteractive == 0)) || { echo '首次无人值守安装需要 --host' >&2; exit 1; }
    read -r -p '云端服务器公网 IP 或主机名（IPv6 请使用方括号）：' host
  fi
  if [[ -z "$port" && "$noninteractive" == 0 ]]; then read -r -p '面板端口 [8765]：' port; fi
  port=${port:-8765}
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo '端口无效' >&2; exit 1; }
  port=$((10#$port))
  ((port >= 1 && port <= 65535)) || { echo '端口无效' >&2; exit 1; }
  [[ "$host" =~ ^[a-zA-Z0-9.-]+$ || "$host" =~ ^\[[a-fA-F0-9:]+\]$ ]] || { echo '主机名无效，请不要填写路径或空格' >&2; exit 1; }
  umask 077
  env_tmp=$(mktemp .env.tmp.XXXXXX)
  sed "s/^HUB_PORT=.*/HUB_PORT=$port/;s|^HUB_PUBLIC_URL=.*|HUB_PUBLIC_URL=http://$host:$port|" .env.example > "$env_tmp"
  mv "$env_tmp" .env
  env_tmp=''
fi
docker compose config --quiet
# Finish the image build while the old installation is still running.
docker compose build hub
"$bootstrap_python" scripts/rename_checkout.py --root "$root" --apply
root=$(pwd -P)
lock="$root/.codepier-install.lock"
migration_started=1
"$bootstrap_python" scripts/migrate_hub.py prepare --root "$root"
# Even an initialization/probe container can create SQLite sidecars or a key;
# cross the recovery boundary before mounting the verified new store writable.
"$bootstrap_python" scripts/migrate_hub.py write-boundary --root "$root"
if docker compose run --rm --no-deps hub python -c 'import os,sys,sqlite3; from pathlib import Path; p=Path(os.environ["HUB_DATA_DIR"])/"hub.sqlite3"; sys.exit(3) if not p.exists() else None; s=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True); found=s.execute("SELECT id FROM users LIMIT 1").fetchone(); s.close(); sys.exit(0 if found else 3)'; then
  echo '检测到已有管理员，保留原账号和数据。'
else
  probe_status=$?
  if ((probe_status != 3)); then echo '无法检查管理员或数据卷；没有尝试重新初始化。' >&2; exit "$probe_status"; fi
  if [[ -z "$username" && "$noninteractive" == 0 ]]; then read -r -p '管理员用户名 [admin]：' username; fi
  if [[ "$noninteractive" == 1 && -z "${CODEPIER_ADMIN_PASSWORD:-}" ]]; then
    echo '首次无人值守初始化需要 --password-file 或 CODEPIER_ADMIN_PASSWORD；未创建账号。' >&2; exit 1
  fi
  if [[ -n "${CODEPIER_ADMIN_PASSWORD:-}" ]]; then
    docker compose run --rm --no-deps -e CODEPIER_ADMIN_PASSWORD hub python -m hub init --username "${username:-admin}"
  else
    docker compose run --rm --no-deps hub python -m hub init --username "${username:-admin}"
  fi
  unset CODEPIER_ADMIN_PASSWORD RD_ADMIN_PASSWORD
fi
# Compose run has now created the new networks. Refresh only explicitly trusted
# legacy host-proxy gateways before the public Hub loads its environment.
"$bootstrap_python" scripts/migrate_hub.py proxy-trust --root "$root"
# Preserve the caller's full Compose selection, including a configured HTTPS proxy.
docker compose up -d --wait --wait-timeout 90
"$bootstrap_python" scripts/migrate_hub.py commit --root "$root"
migration_started=0
echo 'CodePier 已启动且健康检查通过。查看日志：docker compose logs -f hub'
echo '请确认云端及系统防火墙放行所选端口；家里无需开放入站端口。'
echo 'HTTP 不保护浏览器密码与配对文件；首次初始化请使用可信网络或 SSH 转发。'
