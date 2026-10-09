"""Ownership-bound legacy launchd discovery without personal service identifiers."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from agent.lifecycle import LifecycleManager
from agent.service_watchdog import is_supervised
from scripts import agent_lifecycle as helper
from scripts import install_agent as installer
from shared import brand_migration as migration

CANONICAL = 'com.codepier.agent'
LEGACY = 'com.fixture-owner.remote-dev-agent'
MODULES = [migration, installer, helper]


def definition(base, name):
    return {'Label': name, 'WorkingDirectory': str(base/'runtime'),
            'ProgramArguments': [str(base/'runtime/.venv/bin/python'), '-m', 'agent',
                                 '--config', str(base/'config.json'), 'run']}


def installation(tmp_path, monkeypatch, name=LEGACY, recorded=False, directory='.remote-dev-agent'):
    home = tmp_path/'home'
    base = home/directory
    (base/'runtime/agent').mkdir(parents=True)
    (base/'runtime/agent/__main__.py').write_text('# fixture\n')
    config = {'device_id': 'fixture-device', 'secret': 'fixture-secret',
              'hub_url': 'https://hub.example', 'state_dir': str(base/'state'),
              'allowed_roots': [], 'tasks': {}}
    (base/'config.json').write_text(json.dumps(config))
    metadata = {'managed': True, 'service_kind': 'launchd', 'service_scope': 'user'}
    if recorded:
        metadata['service_name'] = name
    (base/'management.json').write_text(json.dumps(metadata))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    target = home/'Library/LaunchAgents'/(name+'.plist')
    target.parent.mkdir(parents=True)
    target.write_bytes(plistlib.dumps(definition(base, name)))
    calls = []
    def service_call(action, current):
        # Exercise the actual standalone helper's identity checks, without OS commands.
        calls.append((action, helper._service_name(current), str(current)))
    backend = SimpleNamespace(
        _service=helper._service, _service_name=helper._service_name,
        _service_target=helper._service_target,
        stop_service=lambda current: service_call('stop', current),
        start_service=lambda current: service_call('start', current),
        verify_service=lambda current: service_call('verify', current),
        refresh_cli_commands=lambda current: None, _wait_for_exit=lambda pid: None,
        _process_alive=lambda pid: False, _move=lambda old, new: old.rename(new))
    return SimpleNamespace(home=home, base=base, target=target, config=config, calls=calls, backend=backend)


def discover(module, base):
    function = module.service_name if module is migration else module.managed_service_name
    return function(base, 'launchd', 'user')


@pytest.mark.parametrize('module', MODULES)
@pytest.mark.parametrize('recorded', [False, True])
def test_discovers_owned_legacy_namespace_without_static_name(tmp_path, monkeypatch, module, recorded):
    f = installation(tmp_path, monkeypatch, recorded=recorded)
    assert LEGACY not in migration.SERVICE_NAMES['launchd']
    assert discover(module, f.base) == LEGACY


@pytest.mark.parametrize('module', MODULES)
@pytest.mark.parametrize('mutation', [
    'command', 'extra-argument', 'working-directory', 'label', 'program', 'root-list',
    'symlink', 'directory', 'missing', 'owner', 'oversize',
])
def test_recorded_legacy_requires_original_owned_definition(tmp_path, monkeypatch, module, mutation):
    f = installation(tmp_path, monkeypatch, recorded=True)
    value = definition(f.base, LEGACY)
    if mutation == 'command':
        value['ProgramArguments'][0] = '/other/python'
    elif mutation == 'extra-argument':
        value['ProgramArguments'].append('--other')
    elif mutation == 'working-directory':
        value['WorkingDirectory'] = str(f.base/'other')
    elif mutation == 'label':
        value['Label'] = CANONICAL
    elif mutation == 'program':
        value['Program'] = '/other/python'
    elif mutation == 'root-list':
        value = []
    f.target.write_bytes(plistlib.dumps(value))
    if mutation == 'symlink':
        original = f.target.with_suffix('.original')
        f.target.rename(original)
        f.target.symlink_to(original)
    elif mutation == 'directory':
        f.target.unlink()
        f.target.mkdir()
    elif mutation == 'missing':
        f.target.unlink()
    elif mutation == 'oversize':
        f.target.write_bytes(b'x' * (1024 * 1024 + 1))
    elif mutation == 'owner':
        uid = f.target.stat().st_uid
        monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises((ValueError, RuntimeError)):
        discover(module, f.base)
    assert not f.calls


@pytest.mark.parametrize('module', MODULES)
@pytest.mark.parametrize('recorded', [False, True])
def test_invalid_canonical_never_becomes_fallback_control(tmp_path, monkeypatch, module, recorded):
    f = installation(tmp_path, monkeypatch, name=CANONICAL, recorded=recorded)
    f.target.write_bytes(plistlib.dumps({'ProgramArguments': ['/other/python']}))
    with pytest.raises((ValueError, RuntimeError)):
        discover(module, f.base)


@pytest.mark.parametrize('module', MODULES)
def test_unrelated_definitions_and_unsafe_label_formats_are_not_discovered(tmp_path, monkeypatch, module):
    f = installation(tmp_path, monkeypatch)
    f.target.unlink()
    for name in ['other.app', 'com.fixture.remote-dev-agent.lifecycle.id',
                 'com.fixture.extra.remote-dev-agent', 'com.fixture_remote.remote-dev-agent']:
        (f.target.parent/(name+'.plist')).write_bytes(plistlib.dumps(definition(f.base, name)))
    # A legacy-shaped service for a different installation is not ours either.
    f.target.write_bytes(plistlib.dumps(definition(f.home/'other', LEGACY)))
    assert discover(module, f.base) == CANONICAL
    with pytest.raises(ValueError, match='missing'):
        helper._service_name(f.base)


@pytest.mark.parametrize('module', MODULES)
def test_duplicate_owned_services_fail_even_with_recorded_metadata(tmp_path, monkeypatch, module):
    f = installation(tmp_path, monkeypatch, recorded=True)
    (f.target.parent/(CANONICAL+'.plist')).write_bytes(plistlib.dumps(definition(f.base, CANONICAL)))
    with pytest.raises((ValueError, RuntimeError), match='Both'):
        discover(module, f.base)


@pytest.mark.parametrize('module', MODULES)
def test_owner_matching_program_override_remains_compatible(tmp_path, monkeypatch, module):
    f = installation(tmp_path, monkeypatch)
    value = definition(f.base, LEGACY)
    value['Program'] = value['ProgramArguments'][0]
    f.target.write_bytes(plistlib.dumps(value))
    assert discover(module, f.base) == LEGACY


def test_bootstrap_discovery_stays_identical_and_standard_library_only():
    for name in ('is_legacy_launchd_name', '_read_owned_service_file', '_launchd_service_name'):
        assert inspect.getsource(getattr(migration, name)) == inspect.getsource(getattr(installer, name))
        assert inspect.getsource(getattr(installer, name)) == inspect.getsource(getattr(helper, name))
    # The helper is copied out of the installation before replacing its runtime.
    for module in [installer, helper]:
        source = inspect.getsource(module)
        assert 'from shared.brand_migration import' not in source


@pytest.mark.parametrize('recorded', [False, True])
def test_status_marks_discovered_legacy_service_pending_in_custom_directory(tmp_path, monkeypatch, recorded):
    f = installation(tmp_path, monkeypatch, recorded=recorded, directory='owner-selected')
    manager = LifecycleManager(f.base/'config.json', f.base/'state', lambda: f.config)
    manager.base = f.base
    manager.runtime_root = f.base/'runtime'
    monkeypatch.setattr(manager, '_external_python', lambda: None)
    monkeypatch.setattr(manager, '_uv', lambda: None)
    monkeypatch.setattr(manager, '_handoff_ready', lambda: False)
    assert manager.describe()['brand_migration'] == 'pending'


@pytest.mark.parametrize('service,expected', [
    (LEGACY, True), ('com.fixture.remote-dev-agent.lifecycle.id', False),
    ('com.fixture.extra.remote-dev-agent', False), ('other.app', False),
])
def test_watchdog_recognizes_only_legacy_service_format(monkeypatch, service, expected):
    monkeypatch.setenv('XPC_SERVICE_NAME', service)
    monkeypatch.delenv('CODEPIER_SUPERVISED', raising=False)
    assert is_supervised() is expected


@pytest.mark.parametrize('name', [LEGACY, CANONICAL])
@pytest.mark.parametrize('program', [False, True])
def test_real_helper_discovery_migrates_legacy_installation(tmp_path, monkeypatch, name, program):
    f = installation(tmp_path, monkeypatch, name=name, recorded=True)
    if program:
        value = definition(f.base, name)
        value['Program'] = value['ProgramArguments'][0]
        f.target.write_bytes(plistlib.dumps(value))
    result = migration.migrate_agent(f.base, 0, f.backend)
    new = f.home/'.codepier-agent'
    assert result['status'] == 'completed' and helper._service_name(new) == CANONICAL
    assert ('stop', name, str(f.base)) in f.calls
    assert ('start', CANONICAL, str(new)) in f.calls
    assert json.loads((new/'config.json').read_text())['secret'] == f.config['secret']


def interrupt_forward_move(f):
    def interruption(old, new):
        old.rename(new)
        raise KeyboardInterrupt('simulated interruption')
    f.backend._move = interruption
    with pytest.raises(KeyboardInterrupt):
        migration.migrate_agent(f.base, 0, f.backend)
    return f.home/'.codepier-agent'


@pytest.mark.parametrize('name', [LEGACY, CANONICAL])
def test_interrupted_migration_recovers_discovered_label(tmp_path, monkeypatch, name):
    f = installation(tmp_path, monkeypatch, name=name, recorded=True)
    raw = f.target.read_bytes()
    new = interrupt_forward_move(f)
    assert migration.recover_agent(new, f.backend) == f.base
    assert f.target.read_bytes() == raw and helper._service_name(f.base) == name


@pytest.mark.parametrize('mutation', ['old-target', 'new-target', 'backup-path', 'backup-definition',
                                       'backup-symlink', 'target-symlink', 'scope', 'name'])
def test_recovery_rejects_unproven_service_identity_before_actions(tmp_path, monkeypatch, mutation):
    f = installation(tmp_path, monkeypatch, recorded=True)
    new = interrupt_forward_move(f)
    path = new/migration.JOURNAL
    journal = json.loads(path.read_text())
    backup = new/journal['backup']/'service.before'
    if mutation in {'old-target', 'new-target'}:
        journal[mutation.replace('-', '_')] = str(f.home/'other.plist')
    elif mutation == 'backup-path':
        journal['backup'] = '../other'
    elif mutation == 'backup-definition':
        value = definition(f.home/'other', LEGACY)
        raw = plistlib.dumps(value)
        backup.write_bytes(raw)
        journal['service_sha256'] = hashlib.sha256(raw).hexdigest()
    elif mutation == 'backup-symlink':
        original = backup.with_suffix('.original')
        backup.rename(original)
        backup.symlink_to(original)
    elif mutation == 'target-symlink':
        original = f.target.with_suffix('.original')
        f.target.rename(original)
        f.target.symlink_to(original)
    elif mutation == 'scope':
        journal['scope'] = 'system'
    else:
        journal['old_name'] = 'unrelated.app'
    path.write_text(json.dumps(journal))
    before = list(f.calls)
    with pytest.raises(RuntimeError):
        migration.recover_agent(new, f.backend)
    assert f.calls == before


@pytest.mark.parametrize('point', ['metadata-restored', 'directory-restored', 'old-start-failed'])
def test_interrupted_rollback_can_resume_with_real_identity_checks(tmp_path, monkeypatch, point):
    f = installation(tmp_path, monkeypatch, recorded=True)
    original_verify = f.backend.verify_service
    def fail_new_verify(current):
        if current.name == '.codepier-agent':
            raise RuntimeError('new service failed')
        original_verify(current)
    f.backend.verify_service = fail_new_verify
    if point == 'metadata-restored':
        original_restore = migration._restore_files
        def interrupted_restore(base, journal):
            original_restore(base, journal)
            raise KeyboardInterrupt('rollback metadata restored')
        monkeypatch.setattr(migration, '_restore_files', interrupted_restore)
    elif point == 'directory-restored':
        original_replace = os.replace
        def interrupted_replace(source, destination):
            original_replace(source, destination)
            if Path(source).name == '.codepier-agent' and Path(destination) == f.base:
                raise KeyboardInterrupt('rollback directory restored')
        monkeypatch.setattr(os, 'replace', interrupted_replace)
    else:
        original_start = f.backend.start_service
        def fail_old_start(current):
            if current == f.base:
                raise RuntimeError('old service temporarily unavailable')
            original_start(current)
        f.backend.start_service = fail_old_start
    with pytest.raises((KeyboardInterrupt, RuntimeError)):
        migration.migrate_agent(f.base, 0, f.backend)
    if point == 'metadata-restored':
        monkeypatch.setattr(migration, '_restore_files', original_restore)
    elif point == 'directory-restored':
        monkeypatch.setattr(os, 'replace', original_replace)
    else:
        f.backend.start_service = original_start
    current = f.base if f.base.is_dir() and not f.base.is_symlink() else f.home/'.codepier-agent'
    assert migration.recover_agent(current, f.backend) == f.base
    assert helper._service_name(f.base) == LEGACY
    assert json.loads((f.base/migration.JOURNAL).read_text())['stage'] == 'rolled_back'


def test_unmanaged_install_metadata_does_not_discover_another_installation(tmp_path, monkeypatch):
    f = installation(tmp_path, monkeypatch, name=CANONICAL)
    other = f.home/'unmanaged'
    other.mkdir()
    monkeypatch.setattr(installer, 'service_identity', lambda: ('launchd', 'user'))
    installer.update_management(other, service=False, service_kind='none')
    metadata = json.loads((other/'management.json').read_text())
    assert metadata['service'] is False and metadata['service_kind'] == 'none'
    assert 'service_name' not in metadata


@pytest.mark.parametrize('module', MODULES)
@pytest.mark.parametrize('grew_during_read', [False, True])
def test_service_reader_rejects_oversize_files(tmp_path, monkeypatch, module, grew_during_read):
    path = tmp_path/'large.plist'
    path.write_bytes(b'x' * (1024 * 1024 + 1))
    if grew_during_read:
        actual = os.fstat
        def reported_small(fd):
            value = actual(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev,
                                   st_ino=value.st_ino, st_uid=value.st_uid, st_size=1)
        monkeypatch.setattr(os, 'fstat', reported_small)
    with pytest.raises(ValueError, match='size limit'):
        module._read_owned_service_file(path)


@pytest.mark.parametrize('target', ['journal', 'backup'])
def test_recovery_rejects_oversize_journal_or_service_backup(tmp_path, monkeypatch, target):
    f = installation(tmp_path, monkeypatch, recorded=True)
    new = interrupt_forward_move(f)
    journal_path = new/migration.JOURNAL
    journal = json.loads(journal_path.read_text())
    path = journal_path if target == 'journal' else new/journal['backup']/'service.before'
    path.write_bytes(b' ' * (1024 * 1024 + 1))
    before = list(f.calls)
    with pytest.raises((RuntimeError, ValueError)):
        migration.recover_agent(new, f.backend)
    assert f.calls == before
