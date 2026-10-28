"""Synthetic lifecycle safety on every platform; real signature/launcher on macOS."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import macos_app
from scripts import install_agent as installer
from scripts import agent_lifecycle as lifecycle
from shared import brand_migration as migration



@pytest.fixture(autouse=True)
def synthetic_posix_file_metadata(monkeypatch):
    """Windows exercises the POSIX contract, not real macOS permissions/TCC."""
    if os.name == 'nt':
        original = Path.lstat
        def posix_lstat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            fields = list(value)
            fields[0] &= ~0o022  # Windows mode bits do not represent POSIX ACLs.
            return os.stat_result(fields)
        monkeypatch.setattr(Path, 'lstat', posix_lstat)
        def synthetic_fchmod(fd, mode):
            assert stat.S_ISREG(os.fstat(fd).st_mode)
            assert mode & ~0o777 == 0
        monkeypatch.setattr(os, 'fchmod', synthetic_fchmod, raising=False)


def installation(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    base = home / '.codepier-agent'
    (base / 'runtime').mkdir(parents=True)
    (base / 'logs').mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    uid = getattr(os, 'getuid', lambda: 0)()
    monkeypatch.setattr(os, 'getuid', lambda: uid, raising=False)
    target = home / 'Library/LaunchAgents/com.codepier.agent.plist'
    target.parent.mkdir(parents=True)
    direct = [str(base/'runtime/.venv/bin/python'), '-m', 'agent',
              '--config', str(base/'config.json'), 'run']
    return base, target, direct


def definition(base, direct):
    return {'Label': 'com.codepier.agent', 'WorkingDirectory': str(base/'runtime'),
            'ProgramArguments': direct, 'KeepAlive': True}


@pytest.mark.parametrize('module', [installer, lifecycle, migration])
def test_named_launcher_keeps_strict_service_ownership(tmp_path, monkeypatch, module):
    base, target, direct = installation(tmp_path, monkeypatch)
    (base/'management.json').write_text(json.dumps({'managed': True, 'service_kind': 'launchd',
                                                   'service_scope': 'user'}))
    value = definition(base, [str(macos_app.executable(base)), *direct])
    target.write_bytes(plistlib.dumps(value))
    discover = module.service_name if module is migration else module.managed_service_name
    assert discover(base, 'launchd', 'user') == 'com.codepier.agent'
    value['ProgramArguments'][1] = '/unrelated/python'
    target.write_bytes(plistlib.dumps(value))
    with pytest.raises((ValueError, RuntimeError)):
        discover(base, 'launchd', 'user')


def fake_build(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'xcrun':
            output = Path(command[-1])
            output.write_bytes(b'synthetic executable')
            output.chmod(0o700)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(macos_app.subprocess, 'run', run)
    return calls


def test_synthetic_build_reuse_identity_and_signature_requirements(tmp_path, monkeypatch):
    calls = fake_build(monkeypatch)
    binary = macos_app.build(tmp_path)
    before = binary.read_bytes()
    count = len(calls)
    assert macos_app.build(tmp_path) == binary
    assert binary.read_bytes() == before
    assert all(c[0] == 'codesign' and '--verify' in c and '-R' in c for c in calls[count:])
    assert any(c[0] == 'xcrun' for c in calls)
    assert any('=identifier "com.codepier.agent.app"' in c for c in calls)
    info = plistlib.loads((tmp_path/'CodePier Agent.app/Contents/Info.plist').read_bytes())
    assert info['CFBundleIdentifier'] == macos_app.IDENTIFIER


@pytest.mark.parametrize('mutation', ['identifier', 'stamp', 'signature', 'symlink'])
def test_existing_identity_is_never_rebuilt_or_overwritten(tmp_path, monkeypatch, mutation):
    calls = fake_build(monkeypatch)
    binary = macos_app.build(tmp_path)
    original = binary.read_bytes()
    app = tmp_path/'CodePier Agent.app'
    if mutation == 'identifier':
        path = app/'Contents/Info.plist'
        value = plistlib.loads(path.read_bytes())
        value['CFBundleIdentifier'] = 'unrelated.app'
        path.write_bytes(plistlib.dumps(value))
    elif mutation == 'stamp':
        (app/'Contents/Resources/launcher-source.sha256').write_text('different')
    elif mutation == 'symlink':
        if os.name == 'nt':
            # No symlink privilege assumption on Windows: preserve real type
            # elsewhere and inject only this rejected symbolic-link metadata.
            original_lstat = Path.lstat
            def symlink_lstat(path, *args, **kwargs):
                value = original_lstat(path, *args, **kwargs)
                if path == binary:
                    fields = list(value)
                    fields[0] = stat.S_IFLNK | 0o700
                    return os.stat_result(fields)
                return value
            monkeypatch.setattr(Path, 'lstat', symlink_lstat)
        else:
            binary.rename(binary.with_suffix('.original'))
            binary.symlink_to(binary.with_suffix('.original'))
    else:
        def reject(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0])
        monkeypatch.setattr(macos_app.subprocess, 'run', reject)
    count = len(calls)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        macos_app.build(tmp_path)
    assert binary.read_bytes() == original
    assert not any(c[0] == 'xcrun' for c in calls[count:])


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('failure', [None, 'build', 'bootstrap', 'health'])
def test_install_and_owned_service_transition_with_rollback(tmp_path, monkeypatch, existing, failure):
    base, target, direct = installation(tmp_path, monkeypatch)
    raw = plistlib.dumps(definition(base, direct))
    if existing:
        target.write_bytes(raw)
    calls = []
    loaded = existing
    failed = False
    def run(command, **kwargs):
        nonlocal loaded, failed
        command = [str(x) for x in command]
        calls.append(command)
        if command[0] != 'launchctl':
            assert command[:2] == [direct[0], '-I']
            assert 'agent.macos_app import build' in command[-1]
            if failure == 'build':
                raise RuntimeError('synthetic compiler unavailable')
        elif command[1] == 'bootout':
            loaded = False
        elif command[1] == 'bootstrap':
            if failure == 'bootstrap' and not failed:
                failed = True
                raise RuntimeError('synthetic bootstrap failure')
            loaded = True
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(installer, 'run', run)
    monkeypatch.setattr(installer, '_launchd_loaded', lambda domain: loaded)
    monkeypatch.setattr(installer.time, 'sleep', lambda value: None)
    monkeypatch.setattr(installer.subprocess, 'check_output',
                        lambda *args, **kwargs: 'state = stopped' if failure == 'health' else 'state = running')
    if failure:
        with pytest.raises(RuntimeError):
            installer.start_macos_service(base, Path(direct[0]), 'com.codepier.agent', direct)
        assert target.read_bytes() == raw if existing else not target.exists()
        assert loaded == existing
    else:
        installer.start_macos_service(base, Path(direct[0]), 'com.codepier.agent', direct)
        value = plistlib.loads(target.read_bytes())
        assert value['ProgramArguments'] == [str(macos_app.executable(base)), *direct]
        assert value['WorkingDirectory'] == str(base/'runtime')
        assert loaded
        assert any(c[1] == 'bootout' for c in calls if c[0] == 'launchctl') == existing


@pytest.mark.parametrize('mutation', ['label', 'program', 'config', 'extra'])
def test_unowned_plist_rejected_before_build_or_service_calls(tmp_path, monkeypatch, mutation):
    base, target, direct = installation(tmp_path, monkeypatch)
    value = definition(base, list(direct))
    if mutation == 'label':
        value['Label'] = 'unrelated'
    elif mutation == 'program':
        value['Program'] = '/unrelated'
    elif mutation == 'config':
        value['ProgramArguments'][4] = '/unrelated/config'
    else:
        value['ProgramArguments'].append('--extra')
    raw = plistlib.dumps(value)
    target.write_bytes(raw)
    def forbidden(*args, **kwargs):
        raise AssertionError('No service or compiler call is allowed')
    monkeypatch.setattr(installer, 'run', forbidden)
    monkeypatch.setattr(installer, '_launchd_loaded', forbidden)
    with pytest.raises(ValueError):
        installer.start_macos_service(base, Path(direct[0]), 'com.codepier.agent', direct)
    assert target.read_bytes() == raw


def test_platform_launcher_contract(tmp_path):
    # Linux/Windows perform a substantive source/plist contract, not a skipped test.
    assert 'posix_spawn' in macos_app.SOURCE and 'waitpid' in macos_app.SOURCE
    assert 'sigaction(SIGTERM' in macos_app.SOURCE
    assert macos_app.executable(tmp_path).name == 'CodePier Agent'
    if sys.platform == 'darwin':
        binary = macos_app.build(tmp_path)
        before = hashlib.sha256(binary.read_bytes()).hexdigest()
        assert subprocess.run([str(binary), sys.executable, '-c', 'raise SystemExit(7)']).returncode == 7
        worker = subprocess.Popen([str(binary), sys.executable, '-u', '-c',
            'import time; print("ready", flush=True); time.sleep(60)'], stdout=subprocess.PIPE, text=True)
        try:
            assert worker.stdout.readline().strip() == 'ready'
            worker.send_signal(signal.SIGTERM)
            assert worker.wait(timeout=5) == 128 + signal.SIGTERM
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
        assert macos_app.build(tmp_path) == binary
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == before



@pytest.mark.parametrize('code,message,expected', [
    (0, '', True), (113, 'Could not find service "com.codepier.agent"', False),
    (1, 'permission denied', None), (113, 'unknown domain', None),
])
def test_launchd_state_is_not_guessed(monkeypatch, code, message, expected):
    monkeypatch.setattr(installer.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=code, stderr=message))
    if expected is None:
        with pytest.raises(RuntimeError):
            installer._launchd_loaded('gui/501/com.codepier.agent')
    else:
        assert installer._launchd_loaded('gui/501/com.codepier.agent') is expected


def test_build_failure_never_publishes_incomplete_app(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError('synthetic missing Xcode command line tools')
    monkeypatch.setattr(macos_app.subprocess, 'run', fail)
    with pytest.raises(FileNotFoundError):
        macos_app.build(tmp_path)
    assert not (tmp_path/'CodePier Agent.app').exists()
    assert not list(tmp_path.glob('.codepier-app-*'))



@pytest.mark.parametrize('moment', ['build', 'bootout', 'health'])
def test_concurrent_definition_is_never_overwritten_on_publish_or_rollback(tmp_path, monkeypatch, moment):
    base, target, direct = installation(tmp_path, monkeypatch)
    original = plistlib.dumps(definition(base, direct))
    external = plistlib.dumps({**definition(base, direct), 'ExternalEdit': 'preserve'})
    target.write_bytes(original)
    loaded = True
    def run(command, **kwargs):
        nonlocal loaded
        command = [str(x) for x in command]
        if command[0] != 'launchctl' and moment == 'build':
            target.write_bytes(external)
        elif command[:2] == ['launchctl', 'bootout']:
            loaded = False
            if moment == 'bootout':
                target.write_bytes(external)
        elif command[:2] == ['launchctl', 'bootstrap']:
            loaded = True
        return SimpleNamespace(returncode=0)
    def health(*args, **kwargs):
        if moment == 'health':
            target.write_bytes(external)
        return 'state = stopped'
    monkeypatch.setattr(installer, 'run', run)
    monkeypatch.setattr(installer, '_launchd_loaded', lambda domain: loaded)
    monkeypatch.setattr(installer.subprocess, 'check_output', health)
    monkeypatch.setattr(installer.time, 'sleep', lambda value: None)
    with pytest.raises(RuntimeError):
        installer.start_macos_service(base, Path(direct[0]), 'com.codepier.agent', direct)
    assert target.read_bytes() == external


def test_lifecycle_restart_uses_same_verified_installer(tmp_path, monkeypatch):
    base, target, direct = installation(tmp_path, monkeypatch)
    target.write_bytes(plistlib.dumps(definition(base, direct)))
    monkeypatch.setattr(lifecycle, '_service', lambda base: ('launchd', 'user'))
    monkeypatch.setattr(lifecycle, '_service_name', lambda base: 'com.codepier.agent')
    monkeypatch.setattr(lifecycle, '_service_target', lambda *args: target)
    calls = []
    monkeypatch.setattr(lifecycle, '_run', lambda command, **kwargs: calls.append(command))
    lifecycle.start_service(base)
    assert len(calls) == 1
    command = [str(x) for x in calls[0]]
    assert command[:3] == [direct[0], '-I', '-c']
    assert 'from scripts.install_agent import start_service' in command[3]
    assert repr(str(base)) in command[3]
