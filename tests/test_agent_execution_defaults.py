"""Fresh local execution consent and old-configuration preservation, no service changes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from agent.config import validate_config
from hub.agent_install import InstallTicketInput, install_command

BASE = Path(__file__).resolve().parents[1]


def invoke(config, *arguments):
    return subprocess.run([sys.executable, '-m', 'agent', '--config', str(config), *map(str, arguments)],
                          cwd=BASE, capture_output=True, text=True, timeout=15)


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / 'projects'; root.mkdir()
    config = tmp_path / 'agent' / 'config.json'
    pairing = tmp_path / 'pairing.json'
    identity = {'device_id': 'a' * 32, 'secret': 's' * 43, 'hub_url': 'http://127.0.0.1:8765', 'name': 'fixture'}
    pairing.write_text(json.dumps(identity))
    return config, pairing, root, identity


@pytest.mark.parametrize('mode,enabled', [(None, True), ('full', True), ('disabled', False)])
def test_fresh_init_defaults_to_execution_with_explicit_opt_out(installation, mode, enabled):
    config, pairing, root, identity = installation
    args = ['init', '--pairing-file', pairing, '--allow', root]
    if mode is not None:
        args += ['--shell', mode]
    result = invoke(config, *args)
    assert result.returncode == 0, result.stderr
    data = json.loads(config.read_text())
    assert data['shell']['enabled'] is enabled
    assert data['shell']['projects'] == (['*'] if enabled else [])
    assert data['allowed_roots'][0]['allow_tasks'] is enabled
    assert data['computer']['enabled'] is False
    assert {key: data[key] for key in identity} == identity
    assert identity['secret'] not in result.stdout
    assert 'Shell / 目录任务' in result.stdout
    if os.name != 'nt':
        assert config.stat().st_mode & 0o777 == 0o600


def test_repair_cannot_silently_change_disabled_execution(installation):
    config, pairing, root, _ = installation
    assert invoke(config, 'init', '--pairing-file', pairing, '--allow', root, '--shell', 'disabled').returncode == 0
    before = config.read_bytes()
    assert invoke(config, 'init', '--pairing-file', pairing, '--allow', root).returncode != 0
    assert config.read_bytes() == before
    assert invoke(config, 'init', '--re-pair', '--pairing-file', pairing, '--shell', 'full').returncode != 0
    assert config.read_bytes() == before
    result = invoke(config, 'init', '--re-pair', '--pairing-file', pairing)
    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text()) == json.loads(before)


def test_legacy_missing_shell_is_not_migrated_to_enabled(installation):
    config, _pairing, root, identity = installation
    raw = {**identity, 'allowed_roots': [{'path': str(root), 'writable': True}], 'tasks': {}}
    validated = validate_config(raw, config)
    assert validated['shell']['enabled'] is False
    # The legacy schema permits this field to be absent and preserves that absence.
    assert 'allow_tasks' not in validated['allowed_roots'][0]
    assert validated['allowed_roots'][0].get('allow_tasks', False) is False
    assert validated['computer']['enabled'] is False


def test_configure_full_preserves_identity_and_read_only_root(installation):
    config, pairing, root, identity = installation
    assert invoke(config, 'init', '--pairing-file', pairing, '--allow', root, '--shell', 'disabled').returncode == 0
    other = root.parent / 'read-only'; other.mkdir()
    data = json.loads(config.read_text())
    data['allowed_roots'].append({'path': str(other), 'writable': False, 'allow_tasks': False})
    config.write_text(json.dumps(data))
    result = invoke(config, 'configure', '--shell', 'full')
    assert result.returncode == 0, result.stderr
    after = json.loads(config.read_text())
    assert {key: after[key] for key in identity} == identity
    assert after['state_dir'] == data['state_dir']
    assert after['tasks'] == data['tasks']
    assert after['allowed_roots'][0]['allow_tasks'] is True
    assert after['allowed_roots'][1] == data['allowed_roots'][1]
    assert after['computer']['enabled'] is False


@pytest.mark.parametrize('platform', ['posix', 'windows'])
@pytest.mark.parametrize('enabled', [True, False])
def test_generated_bootstrap_preserves_execution_choice(platform, enabled):
    body = InstallTicketInput(hub_url='http://hub.invalid', platform=platform,
                              allow_root='/tmp' if platform == 'posix' else 'C:\\Projects',
                              enable_execution=enabled)
    command = install_command(platform, body.hub_url, 'rdi_fixture', 'a' * 64, body.allow_root,
                              enable_execution=body.enable_execution)
    mode = 'full' if enabled else 'disabled'
    assert ('--shell ' + mode if platform == 'posix' else "-Shell '" + mode + "'") in command
    for action in ['upgrade', 'uninstall']:
        maintenance = install_command(platform, body.hub_url, '', 'a' * 64, '', action=action,
                                      expected_device='a' * 32, enable_execution=enabled)
        assert '--shell' not in maintenance and '-Shell' not in maintenance


@pytest.mark.parametrize('value', ['true', 1, None, {}, []])
def test_ticket_execution_flag_is_strict(value):
    with pytest.raises(ValidationError):
        InstallTicketInput(hub_url='http://hub.invalid', platform='posix', allow_root='/tmp', enable_execution=value)


def test_ticket_execution_default_is_enabled():
    assert InstallTicketInput(hub_url='http://hub.invalid', platform='posix', allow_root='/tmp').enable_execution is True
