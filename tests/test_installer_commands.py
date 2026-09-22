"""Exercise public bootstrap commands and panel install flags using fake tools."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.test_agent_install_api import _real_auth_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('action', ['install', 'upgrade', 'uninstall'])
def test_posix_bootstrap_delegates_existing_install_instead_of_already_installed(tmp_path, action):
    home = tmp_path / 'home'
    base = home / '.codepier-agent'
    (base / 'runtime').mkdir(parents=True)
    (base / 'tools').mkdir()
    (base / 'config.json').write_text('{"preserve":"configuration"}')
    project = tmp_path / 'Projects with spaces'
    project.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    archive = tmp_path / 'fixture.zip'
    helper = "import json,os,sys\nfrom pathlib import Path\nPath(os.environ['ARG_LOG']).write_text(json.dumps(sys.argv[1:]))\nprint('DELEGATED_TO_VERIFIED_HELPER')\n"
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('scripts/install_agent.py', helper)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    uv = base / 'tools/uv'
    uv.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$TEST_PYTHON"\n')
    uv.chmod(0o700)
    curl = bindir / 'curl'
    curl.write_text('#!/usr/bin/env bash\nwhile [[ $# -gt 0 ]]; do if [[ "$1" == --output ]]; then out=$2; shift 2; else shift; fi; done\ncp "$TEST_ARCHIVE" "$out"\n')
    curl.chmod(0o700)
    log = tmp_path / 'args.json'
    command = ['bash', str(ROOT / 'deploy/install-from-hub.sh'), action,
               '--hub', 'https://hub.example', '--sha256', digest, '--install-dir', str(base)]
    if action == 'install':
        command += ['--token', 'rdi_fixture', '--allow', str(project)]
    else:
        command += ['--expected-device', 'd' * 32]
    result = subprocess.run(command, text=True, capture_output=True, timeout=20,
        env={**os.environ, 'HOME': str(home), 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
             'TEST_PYTHON': sys.executable, 'TEST_ARCHIVE': str(archive), 'ARG_LOG': str(log)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'DELEGATED_TO_VERIFIED_HELPER' in result.stdout
    assert 'already installed' not in result.stdout + result.stderr
    args = json.loads(log.read_text())
    assert args[args.index('--install-dir') + 1] == str(base)
    if action != 'install':
        assert '--' + action in args and '--expected-device' in args
    assert (base / 'config.json').read_text() == '{"preserve":"configuration"}'


def fake_hub(tmp_path, *, existing=False, probe_status='3'):
    (tmp_path / 'deploy').mkdir()
    (tmp_path / 'bin').mkdir()
    for name in ['install.sh', 'deploy/install-hub.sh', '.env.example', 'compose.yml']:
        shutil.copyfile(ROOT / name, tmp_path / name)
    if existing:
        (tmp_path / '.env').write_text('HUB_PUBLIC_URL=http://preserved.example:9999\nHUB_PORT=9999\n')
    # The install entrypoint is tested here; the actual migrators have separate
    # filesystem/Docker fault-injection tests. This executable records delegation.
    bootstrap = tmp_path / 'bin/bootstrap-python'
    bootstrap.write_text('#!/usr/bin/env bash\nif [[ "$1" == -c ]]; then exec "$REAL_PYTHON" "$@"; fi\nprintf "%s\\n" "$*" >> "$MIGRATION_LOG"\nexit 0\n')
    bootstrap.chmod(0o700)
    docker = tmp_path / 'bin/docker'
    docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  'compose version'|'compose config --quiet'|'compose build hub') exit 0 ;;
  *'python -c '*) exit "$PROBE_STATUS" ;;
  *'python -m hub init'*) [[ "$CODEPIER_ADMIN_PASSWORD" == "$EXPECTED_PASSWORD" ]] || exit 45; exit 0 ;;
  'compose up '*) exit "${START_STATUS:-0}" ;;
esac
exit 99
''')
    docker.chmod(0o700)
    return {**os.environ, 'PATH': str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'],
            'DOCKER_LOG': str(tmp_path / 'docker.log'), 'PROBE_STATUS': probe_status,
            'EXPECTED_PASSWORD': 'fixture-password-long', 'RD_ADMIN_PASSWORD': '', 'CODEPIER_ADMIN_PASSWORD': '',
            'CODEPIER_BOOTSTRAP_PYTHON': str(bootstrap), 'REAL_PYTHON': sys.executable,
            'MIGRATION_LOG': str(tmp_path / 'migration.log')}


def test_hub_one_command_noninteractive_password_not_in_args_or_env_file(tmp_path):
    env = fake_hub(tmp_path)
    password = tmp_path / 'password'
    password.write_text(env['EXPECTED_PASSWORD'] + '\n')
    password.chmod(0o600)
    result = subprocess.run(['bash', str(tmp_path / 'install.sh'), '--non-interactive',
        '--host', '192.0.2.20', '--port', '08080', '--username', 'operator', '--password-file', str(password)],
        env=env, input='', text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / 'docker.log').read_text()
    settings = (tmp_path / '.env').read_text()
    assert '-e CODEPIER_ADMIN_PASSWORD hub python -m hub init --username operator' in log
    assert env['EXPECTED_PASSWORD'] not in log + settings + result.stdout + result.stderr
    assert 'HUB_PORT=8080' in settings and 'http://192.0.2.20:8080' in settings
    assert 'compose up -d --wait --wait-timeout 90' in log
    assert (tmp_path / '.env').stat().st_mode & 0o777 == 0o600


def test_hub_repeat_install_preserves_env_and_existing_admin(tmp_path):
    env = fake_hub(tmp_path, existing=True, probe_status='0')
    original = (tmp_path / '.env').read_bytes()
    result = subprocess.run(['bash', str(tmp_path / 'install.sh'), '--non-interactive', '--host', 'other.example'],
        env=env, input='', text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / '.env').read_bytes() == original
    assert 'python -m hub init' not in (tmp_path / 'docker.log').read_text()
    assert '保留原账号和数据' in result.stdout


def test_hub_noninteractive_without_password_never_initializes(tmp_path):
    env = fake_hub(tmp_path)
    result = subprocess.run(['bash', str(tmp_path / 'install.sh'), '--non-interactive', '--host', '192.0.2.20'],
        env=env, input='', text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    log = (tmp_path / 'docker.log').read_text()
    assert 'python -m hub init' not in log and 'compose up ' not in log


@pytest.mark.parametrize('platform,directory', [('posix', '/tmp/Agent with spaces'), ('windows', r'D:\My Agent')])
def test_management_api_authorization_no_ticket_and_expected_identity(tmp_path, platform, directory):
    app, store, device_id, secret, cookie, csrf = _real_auth_app(tmp_path)
    body = {'hub_url': 'https://hub.example', 'platform': platform, 'install_dir': directory}
    route = f'/api/devices/{device_id}/agent-commands'
    try:
        with TestClient(app) as client:
            assert client.post(route, json=body).status_code == 401
            client.cookies.set('rd_session', cookie)
            assert client.post(route, json=body).status_code == 403
            assert client.post(route, json=body, headers={'X-RD-CSRF': csrf, 'Origin': 'https://other.invalid'}).status_code == 403
            response = client.post(route, json=body, headers={'X-RD-CSRF': csrf})
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            data = response.json()
            assert set(data['commands']) == {'upgrade', 'uninstall'}
            for command in data['commands'].values():
                assert device_id in command and directory in command
                assert secret not in command and 'rdi_' not in command
            assert store.one('SELECT COUNT(*) AS n FROM agent_install_tickets')['n'] == 0
    finally:
        store.close()


def test_manifest_is_public_pinned_bounded_metadata_without_secrets(tmp_path):
    app, store, _id, secret, _cookie, _csrf = _real_auth_app(tmp_path)
    try:
        with TestClient(app) as client:
            response = client.get('/agent/manifest.json')
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            manifest = response.json()
            assert secret not in response.text
            assert set(manifest) == {'url', 'sha256', 'bytes', 'version'}
            package = client.get(manifest['url'])
            assert package.status_code == 200
            assert len(package.content) == manifest['bytes']
            assert hashlib.sha256(package.content).hexdigest() == manifest['sha256']
    finally:
        store.close()


def test_windows_bootstrap_has_no_already_installed_gate_and_checks_cleanup():
    script = (ROOT / 'deploy/install-from-hub.ps1').read_text()
    assert 'Agent already installed' not in script
    assert "[ValidateSet('install','upgrade','uninstall','status','start')]" in script
    assert "$Action -eq 'start') { '--start-service' }" in script
    assert 'Confirm-CodePierRemoval' in script and 'Windows cleanup did not complete' in script
    assert 'if ($LASTEXITCODE -ne 0)' in script


def test_windows_scripts_are_safe_for_legacy_powershell_encoding():
    # Windows PowerShell 5.1 reads BOM-less scripts using the ANSI code page.
    # UTF-8 punctuation can become smart quotes or consume the closing quote.
    for path in (ROOT / 'deploy').glob('*.ps1'):
        raw = path.read_bytes()
        assert raw.isascii(), f'{path.name} must remain ASCII for PowerShell 5.1'
        for encoding in ('cp1252', 'gbk'):
            assert raw.decode(encoding) == raw.decode('ascii')
