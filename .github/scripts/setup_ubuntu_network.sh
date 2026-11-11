#!/usr/bin/env bash
set -euo pipefail
# Disposable GitHub Linux runners only; preserve the existing verified-source policy.
sudo env GITHUB_ACTIONS="$GITHUB_ACTIONS" RUNNER_OS="$RUNNER_OS" python3 .github/scripts/ci_ubuntu_archive.py --mode direct-source
sudo tee /etc/apt/apt.conf.d/zzz-codepier-ci-network >/dev/null <<'APT'
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
Acquire::Retries "2";
APT
sudo apt-config dump | grep -Fx 'Acquire::http::Timeout "30";'
sudo apt-config dump | grep -Fx 'Acquire::https::Timeout "30";'
sudo apt-config dump | grep -Fx 'Acquire::Retries "2";'
