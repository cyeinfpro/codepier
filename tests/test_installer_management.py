"""Installer lifecycle regression tests. OS service commands are never run.

Fixtures are private temporary installations and fake service managers, not the
Agent executing this test suite. Downloads are stubbed or served by TestClient.
"""
from __future__ import annotations

from argparse import Namespace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from hub.agent_install import AgentPackage, install_command
from scripts import install_agent as installer
from scripts import agent_lifecycle as helper

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle():
    return AgentPackage(ROOT).build()


@pytest.fixture
def installation(tmp_path, monkeypatch, bundle):
    home = tmp_path / 'home'
    home.mkdir()
    base = home / '.codepier-agent'
    base.mkdir()
    archive = tmp_path / 'agent.zip'
    archive.write_bytes(bundle.content)
    installer.unpack(archive, bundle.sha256, base / 'runtime')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'important.txt').write_text('preserve this project')
    current = {'device_id': 'd' * 32, 'name': 'Fixture Node', 'hub_url': 'https://hub.example',
               'secret': 'fixture-secret', 'state_dir': str(base / 'state'),
               'allowed_roots': [{'path': str(project), 'writable': True, 'allow_tasks': False}],
               'tasks': {'example': ['echo', 'fixture']}, 'custom_setting': {'preserve': True}}
    (base / 'config.json').write_text(json.dumps(current, indent=3))
    python = base / 'runtime/.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('fixture interpreter')
    uv = base / 'tools/uv'
    uv.parent.mkdir()
    uv.write_text('fixture uv')
    (base / 'management.json').write_text(json.dumps({'managed': True, 'layout': 'managed-runtime',
        'service_kind': 'systemd', 'service_scope': 'user', 'service': True, 'uv': str(uv),
        'helper_python': str(Path(sys.executable).resolve())}))
    monkeypatch.setattr(installer.Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(installer.sys, 'platform', 'linux')
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 1000)
    service = home / '.config/systemd/user/codepier-agent.service'
    service.parent.mkdir(parents=True)
    command = [str(python), '-m', 'agent', '--config', str(base / 'config.json'), 'run']
    service.write_text('[Service]\nWorkingDirectory=' + str(base / 'runtime') + '\nExecStart=' +
                       ' '.join(installer.systemd_quote(x) for x in command) + '\n')
    return Namespace(base=base, current=current, archive=archive, sha=bundle.sha256,
                     home=home, project=project, service=service, uv=uv)


def options(**patch):
    args = dict(uv=None, archive=None, sha256=None, yes=False)
    args.update(patch)
    return Namespace(**args)


def invoke(monkeypatch, fixture, *arguments):
    monkeypatch.setattr(sys, 'argv', ['install_agent.py', '--install-dir', str(fixture.base), *arguments])
    installer.main()


def test_installed_aliases_forward_actions_without_repair_ticket(installation):
    f = installation
    installer.install_cli_links(f.base)
    posix = (f.base / 'agentctl').read_text()
    windows = (f.base / 'agentctl.ps1').read_text()
    assert 'install-from-hub.sh' in posix and '"$@"' in posix
    assert 'install-from-hub.ps1' in windows and '@args' in windows
    assert 'fixture-secret' not in posix + windows
    assert (f.base / 'agentctl').stat().st_mode & 0o777 == 0o700


def test_status_is_read_only_and_never_prints_credentials(installation, monkeypatch, capsys):
    f = installation
    before = (f.base / 'config.json').read_bytes()
    invoke(monkeypatch, f, '--status')
    output = capsys.readouterr().out
    assert json.loads(output)['installed'] is True
    assert 'fixture-secret' not in output
    assert not (f.base / '.install.lock').exists()
    assert (f.base / 'config.json').read_bytes() == before


@pytest.mark.parametrize('action', ['--upgrade', '--uninstall'])
def test_wrong_device_command_rejected_before_service_or_download(installation, monkeypatch, action):
    f = installation
    monkeypatch.setattr(installer, 'run', lambda *_a, **_k: pytest.fail('must not run commands'))
    before = (f.base / 'config.json').read_bytes()
    with pytest.raises(ValueError, match='different device'):
        invoke(monkeypatch, f, action, '--expected-device', 'e' * 32)
    assert (f.base / 'config.json').read_bytes() == before
    assert not (f.base / '.install.lock').exists()


@pytest.mark.parametrize('status', ['running', 'accepted'])
def test_local_maintenance_refuses_active_operations(installation, status):
    f = installation
    state = f.base / 'state'
    state.mkdir()
    with sqlite3.connect(state / 'agent.sqlite3') as db:
        db.execute('CREATE TABLE calls (status TEXT)')
        db.execute('INSERT INTO calls VALUES (?)', (status,))
    with pytest.raises(ValueError, match='active operations'):
        installer.require_idle(f.base, f.current)


def test_pending_lifecycle_plan_is_not_overwritten(installation):
    f = installation
    folder = f.base / 'state/lifecycle'
    folder.mkdir(parents=True)
    (folder / 'pending.json').write_text('{}')
    with pytest.raises(ValueError, match='pending'):
        installer.require_idle(f.base, f.current)


@pytest.mark.parametrize('failure', ['wrong-service', 'missing-service', 'wrong-account', 'symlink-service'])
def test_service_ownership_is_checked_before_actions(installation, failure):
    f = installation
    if failure == 'wrong-service':
        f.service.write_text('[Service]\nWorkingDirectory=/different/agent\n')
    elif failure == 'missing-service':
        f.service.unlink()
    elif failure == 'wrong-account':
        metadata = json.loads((f.base / 'management.json').read_text())
        metadata['service_scope'] = 'system'
        (f.base / 'management.json').write_text(json.dumps(metadata))
    else:
        other = f.home / 'other-service'
        shutil.copyfile(f.service, other)
        f.service.unlink()
        f.service.symlink_to(other)
    with pytest.raises(ValueError):
        installer.verify_service_ownership(f.base, allow_missing=True)
    assert (f.project / 'important.txt').read_text() == 'preserve this project'


def test_cli_upgrade_keeps_config_byte_identical_and_prepares_before_switch(installation, monkeypatch):
    f = installation
    before = (f.base / 'config.json').read_bytes()
    calls = []
    def run(command, **kwargs):
        command = [str(x) for x in command]
        calls.append(command)
        if '--apply-update' in command:
            assert (f.base / 'config.json').read_bytes() == before
            assert 'import agent.config, agent.runner, agent.lifecycle' in calls[-2]
            candidate = Path(command[-1])
            shutil.rmtree(f.base / 'runtime')
            candidate.rename(f.base / 'runtime')
    monkeypatch.setattr(installer, 'run', run)
    monkeypatch.setattr(installer, 'enroll', lambda *_a: pytest.fail('upgrade must not re-enroll'))
    invoke(monkeypatch, f, '--upgrade', '--archive', str(f.archive), '--sha256', f.sha)
    assert (f.base / 'config.json').read_bytes() == before
    assert (f.base / 'agentctl').is_file()
    assert not list(f.base.glob('.runtime-update-cli-*'))
    assert not (f.base / '.install.lock').exists()
    assert calls[-1][0] == str(installer.external_python(f.base))
    assert not Path(calls[-1][1]).is_relative_to(f.base / 'runtime')


@pytest.mark.parametrize('stage', ['dependency', 'configuration-change', 'switch'])
def test_cli_upgrade_failure_preserves_installed_runtime(installation, monkeypatch, stage):
    f = installation
    before = (f.base / 'config.json').read_bytes()
    marker = f.base / 'runtime/old-marker'
    marker.write_text('original')
    def run(command, **kwargs):
        command = [str(x) for x in command]
        if stage == 'dependency' and 'pip' in command:
            raise subprocess.CalledProcessError(7, command)
        if stage == 'configuration-change' and '-c' in command:
            (f.base / 'config.json').write_text(json.dumps({**f.current, 'name': 'User Edit'}))
        if stage == 'switch' and '--apply-update' in command:
            raise subprocess.CalledProcessError(8, command)
    monkeypatch.setattr(installer, 'run', run)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        invoke(monkeypatch, f, '--upgrade', '--archive', str(f.archive), '--sha256', f.sha)
    assert marker.read_text() == 'original'
    assert not list(f.base.glob('.runtime-update-cli-*'))
    assert not (f.base / '.install.lock').exists()
    if stage != 'configuration-change':
        assert (f.base / 'config.json').read_bytes() == before
    else:
        assert json.loads((f.base / 'config.json').read_text())['name'] == 'User Edit'


def test_manual_repair_failure_restores_original_configuration(installation, monkeypatch):
    f = installation
    before = (f.base / 'config.json').read_bytes()
    monkeypatch.setenv('CODEPIER_INSTALL_TOKEN', 'rdi_fixture')
    monkeypatch.setattr(installer, 'enroll', lambda *_a: {**f.current, 'secret': 'new-secret'})
    def run(command, **kwargs):
        if '--apply-update' in [str(x) for x in command]:
            raise subprocess.CalledProcessError(9, command)
    monkeypatch.setattr(installer, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        invoke(monkeypatch, f, '--archive', str(f.archive), '--sha256', f.sha,
               '--hub', f.current['hub_url'], '--allow', str(f.project), '--uv', str(f.uv))
    assert (f.base / 'config.json').read_bytes() == before
    assert not (f.base / '.install.lock').exists()


def test_local_uninstall_requires_confirmation_and_keeps_projects(installation, monkeypatch):
    f = installation
    monkeypatch.setattr(installer, 'load_lifecycle_helper', lambda *_a: helper)
    calls = []
    monkeypatch.setattr(helper, 'stop_service', lambda *_a, **_k: calls.append('stopped'))
    monkeypatch.setattr('builtins.input', lambda *_a: 'wrong name')
    with pytest.raises(ValueError, match='cancelled'):
        invoke(monkeypatch, f, '--uninstall')
    assert not calls and (f.base / 'config.json').is_file()
    monkeypatch.setattr('builtins.input', lambda *_a: 'Fixture Node')
    invoke(monkeypatch, f, '--uninstall')
    assert calls == ['stopped']
    assert not f.base.exists()
    assert (f.project / 'important.txt').read_text() == 'preserve this project'
    invoke(monkeypatch, f, '--uninstall', '--yes')


def test_uninstall_refuses_authorized_directory_inside_installation(installation, monkeypatch):
    f = installation
    nested = f.base / 'my-project'
    nested.mkdir()
    (nested / 'important.txt').write_text('keep')
    (f.base / 'config.json').write_text(json.dumps({**f.current, 'allowed_roots': [{'path': str(nested)}]}))
    with pytest.raises(ValueError, match='inside'):
        invoke(monkeypatch, f, '--uninstall', '--yes')
    assert (nested / 'important.txt').read_text() == 'keep'


@pytest.mark.parametrize('name', ['home', 'root', 'symlink'])
def test_dangerous_install_directory_never_deleted(installation, monkeypatch, name):
    f = installation
    path = f.home if name == 'home' else Path('/')
    if name == 'symlink':
        path = f.home / 'alias'
        path.symlink_to(f.base, target_is_directory=True)
    monkeypatch.setattr(sys, 'argv', ['install_agent.py', '--install-dir', str(path), '--uninstall', '--yes'])
    with pytest.raises(ValueError):
        installer.main()
    assert f.base.is_dir()


def test_failed_service_stop_does_not_delete_runtime_or_service(installation, monkeypatch):
    f = installation
    monkeypatch.setattr(helper, '_run', lambda *_a, **_k: 1)
    monkeypatch.setattr(helper, 'service_is_running', lambda *_a: True)
    monkeypatch.setattr(helper.time, 'sleep', lambda *_a: None)
    with pytest.raises(RuntimeError, match='did not stop'):
        helper.uninstall(f.base, 0)
    assert f.service.exists() and (f.base / 'config.json').exists()


@pytest.mark.parametrize('watchdog', [True, False])
def test_update_refreshes_windows_wrapper_and_supports_older_packages(installation, monkeypatch, watchdog):
    f = installation
    wrapper = f.base / 'runtime/run-service.py'
    wrapper.write_text('# original service wrapper\n')
    candidate = f.base / '.runtime-update-test'
    installer.unpack(f.archive, f.sha, candidate)
    if not watchdog:
        (candidate / 'agent/service_watchdog.py').unlink()
    for name in ['stop_service', 'start_service', 'verify_service', '_wait_for_exit']:
        monkeypatch.setattr(helper, name, lambda *_a, **_k: None)
    helper.apply_update(f.base, candidate, 0)
    if watchdog:
        assert 'from agent.service_watchdog import run_service' in wrapper.read_text()
        assert (f.base / '.runtime-previous/run-service.py').read_text() == '# original service wrapper\n'
    else:
        assert wrapper.read_text() == '# original service wrapper\n'
    assert (f.base / 'agentctl').is_file()
    assert (f.base / 'agentctl.ps1').is_file()


def test_update_download_ignores_manifest_supplied_foreign_url(installation, monkeypatch, tmp_path):
    f = installation
    raw = f.archive.read_bytes()
    manifest = {'sha256': f.sha, 'bytes': len(raw), 'version': 'fixture', 'url': 'https://other.invalid/steal'}
    requests = []
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_a): self.close()
    class Opener:
        def open(self, url, timeout):
            requests.append(url)
            return Response(json.dumps(manifest).encode() if len(requests) == 1 else raw)
    monkeypatch.setattr(installer.urllib.request, 'build_opener', lambda *_a: Opener())
    result, sha = installer.fetch_update(f.base, f.current, tmp_path)
    assert result.read_bytes() == raw and sha == f.sha
    assert requests == ['https://hub.example/agent/manifest.json', 'https://hub.example/agent/agent.zip?sha256=' + f.sha]


@pytest.mark.parametrize('bad', ['size', 'checksum', 'manifest'])
def test_update_download_rejects_invalid_data(installation, monkeypatch, tmp_path, bad):
    f = installation
    raw = f.archive.read_bytes()
    manifest = {'sha256': f.sha, 'bytes': len(raw)}
    if bad == 'size': manifest['bytes'] += 1
    if bad == 'checksum': manifest['sha256'] = '0' * 64
    if bad == 'manifest': manifest['bytes'] = True
    responses = [json.dumps(manifest).encode(), raw]
    class Opener:
        def open(self, *_a, **_k): return io.BytesIO(responses.pop(0))
    monkeypatch.setattr(installer.urllib.request, 'build_opener', lambda *_a: Opener())
    with pytest.raises(ValueError):
        installer.fetch_update(f.base, f.current, tmp_path)


def test_maintenance_command_does_not_mint_or_embed_pairing_ticket(bundle):
    for platform in ['posix', 'windows']:
        for action in ['upgrade', 'uninstall']:
            command = install_command(platform, 'https://hub.example', '', bundle.sha256, '',
                'b' * 64, action=action, expected_device='d' * 32)
            assert 'rdi_' not in command and 'fixture-secret' not in command
            assert 'd' * 32 in command and 'b' * 64 in command and bundle.sha256 in command
            assert '--yes' not in command and '-Yes' not in command
            assert action in command


def test_native_terminal_sessions_block_local_maintenance(installation):
    f = installation
    folder = f.base / 'state/native-cli'
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / 'native.sqlite3') as db:
        db.execute('CREATE TABLE sessions(status TEXT)')
        db.execute("INSERT INTO sessions VALUES ('orphaned')")
    with pytest.raises(ValueError, match='Native terminal sessions'):
        installer.require_idle(f.base, f.current)


def test_source_uninstall_uses_current_helper_not_old_installed_helper(installation, monkeypatch):
    f = installation
    loaded = []
    def load(source):
        loaded.append(source)
        return Namespace(uninstall=lambda base, pid: shutil.rmtree(base))
    monkeypatch.setattr(installer, 'load_lifecycle_helper', load)
    invoke(monkeypatch, f, '--uninstall', '--yes')
    assert loaded == [Path(installer.__file__).resolve().with_name('agent_lifecycle.py')]
    assert not f.base.exists()
    assert (f.project / 'important.txt').is_file()


def test_local_status_alias_runs_without_any_network(installation):
    f = installation
    python = f.base / 'runtime/.venv/bin/python'
    python.unlink()
    python.symlink_to(Path(sys.executable).resolve())
    installer.install_cli_links(f.base)
    result = subprocess.run([str(f.base / 'agentctl'), 'status'],
        env={**os.environ, 'HOME': str(f.home)}, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['device_id'] == f.current['device_id']
    assert 'fixture-secret' not in result.stdout
