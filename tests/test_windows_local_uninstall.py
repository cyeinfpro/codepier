"""Portable contract tests; Windows native deletion is tested by its PS fixture."""
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import xml.etree.ElementTree as ET

import pytest

from scripts import install_agent as installer
from shared import windows_uninstall as cleanup

pytestmark = pytest.mark.windows_core


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    home = tmp_path.resolve()/'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(cleanup, 'verify_directory_owner', lambda *args: None)
    calls = []
    tasks = {}
    def run(command, **kwargs):
        calls.append(command)
        if '/Delete' in command:
            tasks.pop(command[command.index('/TN')+1])
        return SimpleNamespace(returncode=0)
    helper = SimpleNamespace(set_windows_task_enabled=lambda name, value: calls.append(('enabled', name, value)),
                             windows_task_script=lambda name: name,
                             _run=lambda args: 0)
    api = SimpleNamespace(**vars(installer))
    api.windows_user_sid = lambda: 'S-1-5-21-fixture'
    api.windows_task_registered = lambda name: name in tasks
    api.query_windows_task_xml = lambda name: tasks[name]
    api.require_idle = lambda *args, **kwargs: None
    api.verify_windows_instance_is_stopped = lambda *args: None
    api.load_lifecycle_helper = lambda path: helper
    api.run = run
    api.subprocess = SimpleNamespace(run=run)
    api.time = SimpleNamespace(sleep=lambda seconds: None)
    args = SimpleNamespace(install_dir=None, uninstall_handoff_file=str(tmp_path.resolve()/'handoff.json'),
                           uninstall_preview=False, yes=False, expected_device='different-node', hub='https://different.invalid')
    def root(name='.codepier-agent', device='old-node', config=True, metadata=True):
        base = home/name
        base.mkdir()
        if config:
            (base/'config.json').write_text(json.dumps({'device_id': device, 'hub_url': 'https://old.invalid',
                'secret': 'NEVER-PRINT-THIS', 'allowed_roots': []}))
        if metadata:
            (base/'management.json').write_text(json.dumps({'managed': True, 'layout': 'managed-runtime',
                'service_kind': 'schtasks', 'service_scope': 'user'}))
        return base
    return SimpleNamespace(home=home, calls=calls, tasks=tasks, api=api, args=args, root=root, helper=helper)


def xml(base, sid='S-1-5-21-fixture'):
    ns = cleanup.NS['t']
    ET.register_namespace('', ns)
    task = ET.Element('{'+ns+'}Task')
    principals = ET.SubElement(task, '{'+ns+'}Principals')
    principal = ET.SubElement(principals, '{'+ns+'}Principal')
    ET.SubElement(principal, '{'+ns+'}UserId').text = sid
    actions = ET.SubElement(task, '{'+ns+'}Actions')
    action = ET.SubElement(actions, '{'+ns+'}Exec')
    for key, value in {'WorkingDirectory': str(base/'runtime'),
                       'Command': str(base/'runtime/.venv/Scripts/python.exe'),
                       'Arguments': subprocess.list2cmdline([str(base/'runtime/run-service.py')])}.items():
        ET.SubElement(action, '{'+ns+'}'+key).text = value
    return ET.tostring(task, encoding='unicode')


def test_two_nodes_one_confirmation_no_enrollment_or_identity_gate(fixture, monkeypatch, capsys):
    f = fixture
    bases = [f.root(), f.root('.remote-dev-agent', 'older-node')]
    answers = []
    monkeypatch.setattr('builtins.input', lambda prompt: answers.append(prompt) or 'UNINSTALL CODEPIER')
    cleanup.run(f.args, f.api)
    plan = json.loads(Path(f.args.uninstall_handoff_file).read_text())
    assert len(answers) == 1 and len(plan['roots']) == 2
    assert {row['source'] for row in plan['roots']} == {str(base) for base in bases}
    assert all((base/'.install.lock/uninstall.json').exists() for base in bases)
    assert all((base/'config.json').exists() for base in bases)
    assert 'NEVER-PRINT-THIS' not in capsys.readouterr().out
    assert not f.calls


@pytest.mark.parametrize('answer', ['no', '', None])
def test_cancel_changes_nothing(fixture, monkeypatch, answer):
    f = fixture
    base = f.root()
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    def confirm(prompt):
        if answer is None:
            raise EOFError
        return answer
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(ValueError, match='confirmation|cancelled'):
        cleanup.run(f.args, f.api)
    assert before == {p.name: p.read_bytes() for p in base.iterdir()}
    assert not f.calls and not Path(f.args.uninstall_handoff_file).exists()


def test_preview_needs_no_handoff_and_does_not_mutate(fixture):
    f = fixture
    base = f.root()
    f.args.uninstall_handoff_file = ''
    f.args.uninstall_preview = True
    cleanup.run(f.args, f.api)
    assert not (base/'.install.lock').exists() and not f.calls


def test_missing_task_and_service_definition_support_failed_install(fixture):
    f = fixture
    base = f.root(config=False)
    f.args.yes = True
    cleanup.run(f.args, f.api)
    assert json.loads(Path(f.args.uninstall_handoff_file).read_text())['roots'][0]['source'] == str(base)


def test_managed_no_service_install_is_included(fixture):
    f = fixture
    base = f.root()
    record = json.loads((base/'management.json').read_text())
    record.update(service_kind='none', service_scope='none')
    (base/'management.json').write_text(json.dumps(record))
    f.args.yes = True
    cleanup.run(f.args, f.api)
    assert json.loads(Path(f.args.uninstall_handoff_file).read_text())['roots'][0]['source'] == str(base)


def test_live_owned_task_without_service_xml_is_removed(fixture):
    f = fixture
    base = f.root()
    f.tasks['CodePierAgent'] = xml(base)
    f.args.yes = True
    cleanup.run(f.args, f.api)
    assert not f.tasks
    assert f.calls[0] == ('enabled', 'CodePierAgent', False)
    assert any('/Delete' in command for command in f.calls)
    assert (base/'config.json').exists()


@pytest.mark.parametrize('malform', ['wrong-sid', 'extra-exec', 'foreign-command', 'missing-root'])
def test_foreign_or_unproven_tasks_are_preserved(fixture, malform):
    f = fixture
    base = f.root()
    value = xml(base, sid='wrong' if malform == 'wrong-sid' else 'S-1-5-21-fixture')
    if malform == 'extra-exec':
        value = value.replace('</Actions>', '<Exec><Command>other.exe</Command></Exec></Actions>')
    if malform == 'foreign-command':
        value = value.replace('python.exe', 'unrelated.exe')
    if malform == 'missing-root':
        value = xml(f.home/'missing-custom')
    f.tasks['CodePierAgent'] = value
    f.args.yes = True
    with pytest.raises(ValueError):
        cleanup.run(f.args, f.api)
    assert not f.calls and f.tasks and (base/'config.json').exists()


@pytest.mark.parametrize('unsafe', ['unknown', 'project', 'external-state', 'symbolic-link', 'ancestor-link', 'home'])
def test_unsafe_roots_are_preserved(fixture, unsafe):
    f = fixture
    base = f.root()
    if unsafe == 'unknown':
        (base/'my-project').mkdir()
    elif unsafe == 'project':
        (base/'runtime/.git').mkdir(parents=True)
    elif unsafe == 'external-state':
        record = json.loads((base/'config.json').read_text())
        record['state_dir'] = str(f.home/'private-state')
        (base/'config.json').write_text(json.dumps(record))
    elif unsafe == 'symbolic-link':
        (base/'runtime').symlink_to(f.home, target_is_directory=True)
    elif unsafe == 'ancestor-link':
        alias = f.home/'alias'
        alias.symlink_to(f.home, target_is_directory=True)
        f.args.install_dir = str(alias/'.codepier-agent')
    else:
        f.args.install_dir = str(f.home)
    f.args.yes = True
    with pytest.raises(ValueError):
        cleanup.run(f.args, f.api)
    assert not f.calls and (base/'config.json').exists()


def test_changed_config_during_confirmation_preserved_and_lock_released(fixture, monkeypatch):
    f = fixture
    base = f.root()
    def answer(prompt):
        value = json.loads((base/'config.json').read_text())
        value['device_id'] = 'changed-node'
        (base/'config.json').write_text(json.dumps(value))
        return 'UNINSTALL CODEPIER'
    monkeypatch.setattr('builtins.input', answer)
    with pytest.raises(ValueError, match='changed'):
        cleanup.run(f.args, f.api)
    assert not (base/'.install.lock').exists() and not f.calls


def test_failed_lock_write_does_not_leave_empty_lock(fixture):
    f = fixture
    base = f.root()
    f.args.yes = True
    f.api.atomic_json_file = lambda *args: (_ for _ in ()).throw(OSError('disk full'))
    with pytest.raises(OSError, match='disk full'):
        cleanup.run(f.args, f.api)
    assert not (base/'.install.lock').exists() and (base/'config.json').exists()


def test_another_account_directory_is_preserved(fixture, monkeypatch):
    f = fixture
    base = f.root()
    f.args.yes = True
    monkeypatch.setattr(cleanup, 'verify_directory_owner', lambda *args: (_ for _ in ()).throw(ValueError('owner differs')))
    with pytest.raises(ValueError, match='owner differs'):
        cleanup.run(f.args, f.api)
    assert not f.calls and not (base/'.install.lock').exists()


def test_busy_process_after_task_stop_is_not_success(fixture):
    f = fixture
    base = f.root()
    f.tasks['CodePierAgent'] = xml(base)
    f.api.verify_windows_instance_is_stopped = lambda *args: (_ for _ in ()).throw(ValueError('still owns lock'))
    f.args.yes = True
    with pytest.raises(ValueError, match='still owns'):
        cleanup.run(f.args, f.api)
    assert not f.tasks and not (base/'.install.lock').exists()
    assert not Path(f.args.uninstall_handoff_file).exists() and (base/'config.json').exists()
    # Safe retry accepts the now-missing task and preserved evidence.
    f.api.verify_windows_instance_is_stopped = lambda *args: None
    cleanup.run(f.args, f.api)
    assert Path(f.args.uninstall_handoff_file).exists()


def test_no_installations_is_idempotent_but_access_error_is_not_missing(fixture):
    f = fixture
    cleanup.run(f.args, f.api)
    plan = json.loads(Path(f.args.uninstall_handoff_file).read_text())
    assert plan == {'schema': 1, 'roots': [], 'aliases': []}
    Path(f.args.uninstall_handoff_file).unlink()
    f.api.windows_task_registered = lambda name: (_ for _ in ()).throw(ValueError('Access denied'))
    with pytest.raises(ValueError, match='Access denied'):
        cleanup.run(f.args, f.api)
    assert not Path(f.args.uninstall_handoff_file).exists()


def test_recovery_backup_and_resume_marker_are_discovered(fixture):
    f = fixture
    backup = f.root('.codepier-agent.recovery-20261012-130000-12345678')
    partial = f.root(config=False, metadata=False)
    (partial/'.uninstall.json').write_text(json.dumps({'schema': 1, 'kind': 'codepier-local-uninstall',
        'source': str(partial), 'owner_sid': f.api.windows_user_sid(), 'stage': 'files_pending'}))
    roots, tasks, aliases = cleanup.inventory(f.args, f.api)
    assert {r['base'] for r in roots} == {backup, partial}
    assert not tasks and not aliases


@pytest.mark.parametrize('valid', [True, False])
def test_downloaded_installer_loads_helpers_from_verified_package(fixture, monkeypatch, valid):
    from hub.agent_install import AgentPackage
    f = fixture
    package = AgentPackage(Path(installer.__file__).resolve().parents[1]).build()
    archive = f.home/'package.zip'
    archive.write_bytes(package.content)
    calls = []
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer.sys, 'argv', ['downloaded-install-agent.py', '--uninstall',
        '--archive', str(archive), '--sha256', package.sha256 if valid else '0'*64])
    monkeypatch.setattr(installer, '__file__', str(f.home/'downloaded-install-agent.py'))
    def load(source):
        assert source.name == 'windows_uninstall.py' and source.is_file()
        assert source.read_text().startswith('"""Owner-verified')
        def run(args, api):
            assert Path(api.__file__).with_name('agent_lifecycle.py').is_file()
            calls.append(source)
        return SimpleNamespace(run=run)
    monkeypatch.setattr(installer, 'load_lifecycle_helper', load)
    if valid:
        installer.main()
        assert len(calls) == 1 and not calls[0].exists()
    else:
        with pytest.raises(ValueError, match='checksum'):
            installer.main()
        assert not calls


def test_dangling_migration_alias_is_still_in_cleanup_plan(fixture):
    f = fixture
    alias = f.home/'.remote-dev-agent'
    alias.symlink_to(f.home/'.codepier-agent', target_is_directory=True)
    roots, tasks, aliases = cleanup.inventory(f.args, f.api)
    assert not roots and not tasks and aliases == [str(alias)]


def test_main_dispatches_windows_uninstall_before_identity_acl_or_default_root(fixture, monkeypatch):
    f = fixture
    calls = []
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer.sys, 'argv', ['install_agent.py', '--uninstall', '--expected-device', 'wrong-node'])
    monkeypatch.setattr(installer, 'default_install_base', lambda: pytest.fail('must not select one node'))
    monkeypatch.setattr(installer, 'load_lifecycle_helper', lambda path: SimpleNamespace(run=lambda args, api: calls.append(args)))
    installer.main()
    assert len(calls) == 1 and calls[0].expected_device == 'wrong-node'


def python_alias(base, monkeypatch, *, target=None, name='cpython-3.13-windows-x86_64-none'):
    """Real Windows junction; a narrowly modelled junction on portable runners."""
    import os
    folder = base/'python'
    folder.mkdir(exist_ok=True)
    target = target or folder/'cpython-3.13.12-windows-x86_64-none'
    target.mkdir(parents=True, exist_ok=True)
    (target/'python.exe').write_bytes(b'synthetic Python executable')
    alias = folder/name
    if os.name == 'nt':
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(target)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(target, target_is_directory=True)
        original_symlink = Path.is_symlink
        original_junction = getattr(Path, 'is_junction', lambda path: False)
        monkeypatch.setattr(Path, 'is_symlink', lambda path: False if path == alias else original_symlink(path))
        monkeypatch.setattr(Path, 'is_junction', lambda path: path == alias or original_junction(path), raising=False)
    return alias, target


def test_managed_python_junction_preview_preserves_working_runtime(fixture, monkeypatch):
    f = fixture
    base = f.root('.remote-dev-agent')
    alias, target = python_alias(base, monkeypatch)
    (base/'runtime').mkdir()
    config = base/'runtime/pyvenv.cfg'
    config.write_text('home = '+str(alias))
    f.args.uninstall_preview = True
    cleanup.run(f.args, f.api)
    assert alias.exists() and (alias/'python.exe').read_bytes() == (target/'python.exe').read_bytes()
    assert config.read_text() == 'home = '+str(alias)
    assert not f.calls and not (base/'.install.lock').exists()
    assert not Path(f.args.uninstall_handoff_file).exists()


def test_managed_python_junction_is_handed_off_without_early_unlink(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, target = python_alias(base, monkeypatch)
    f.args.yes = True
    cleanup.run(f.args, f.api)
    row = json.loads(Path(f.args.uninstall_handoff_file).read_text())['roots'][0]
    assert row['python_junctions'] == [{'path': str(alias), 'target': str(target)}]
    assert alias.exists() and (alias/'python.exe').is_file()


@pytest.mark.parametrize('unsafe', ['outside', 'different-version', 'different-architecture', 'wrong-name',
                                   'missing-python', 'linked-python', 'chained-target', 'unmanaged'])
def test_only_proven_managed_python_junctions_are_allowed(fixture, monkeypatch, unsafe):
    f = fixture
    base = f.root()
    target = None
    if unsafe == 'outside':
        target = f.home/'cpython-3.13.12-windows-x86_64-none'
    elif unsafe == 'different-version':
        target = base/'python/cpython-3.12.12-windows-x86_64-none'
    elif unsafe == 'different-architecture':
        target = base/'python/cpython-3.13.12-windows-aarch64-none'
    alias, target = python_alias(base, monkeypatch, target=target,
                                name='arbitrary-link' if unsafe == 'wrong-name' else 'cpython-3.13-windows-x86_64-none')
    if unsafe == 'missing-python':
        (target/'python.exe').unlink()
    elif unsafe == 'linked-python':
        (target/'python.exe').unlink()
        (target/'python.exe').symlink_to(base/'management.json')
    elif unsafe == 'chained-target':
        actual = target.with_name(target.name+'-actual')
        target.rename(actual)
        target.symlink_to(actual, target_is_directory=True)
    elif unsafe == 'unmanaged':
        (base/'management.json').unlink()
        (base/'runtime/agent').mkdir(parents=True)
        (base/'runtime/agent/__main__.py').write_text('')
    f.args.yes = True
    with pytest.raises(ValueError):
        cleanup.run(f.args, f.api)
    assert alias.exists() and (base/'config.json').exists()
    assert not f.calls and not (base/'.install.lock').exists()
    assert not Path(f.args.uninstall_handoff_file).exists()


def test_python_shaped_symbolic_link_is_not_a_junction(fixture):
    f = fixture
    base = f.root()
    target = base/'python/cpython-3.13.12-windows-x86_64-none'
    target.mkdir(parents=True)
    (target/'python.exe').write_bytes(b'synthetic')
    alias = target.with_name('cpython-3.13-windows-x86_64-none')
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='Linked contents'):
        cleanup.inspect_root(base, f.api)
    assert alias.is_symlink()


def test_busy_native_state_does_not_break_python_alias(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, target = python_alias(base, monkeypatch)
    f.api.require_idle = lambda *args: (_ for _ in ()).throw(ValueError('native session access denied'))
    f.args.yes = True
    with pytest.raises(ValueError, match='native session'):
        cleanup.run(f.args, f.api)
    assert alias.exists() and (alias/'python.exe').is_file() and (target/'python.exe').is_file()
    assert not f.calls and not (base/'.install.lock').exists()
    assert not Path(f.args.uninstall_handoff_file).exists()


def test_python_junction_change_during_confirmation_is_refused(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, target = python_alias(base, monkeypatch)
    target2 = target.with_name('cpython-3.13.13-windows-x86_64-none')
    target2.mkdir()
    (target2/'python.exe').write_bytes(b'synthetic')
    def answer(prompt):
        # readlink's second observation models a same-account junction swap;
        # no services or target files have yet been touched.
        monkeypatch.setattr(cleanup.os, 'readlink', lambda path, **kw: str(target2) if Path(path) == alias else str(target))
        original_resolve = Path.resolve
        monkeypatch.setattr(Path, 'resolve', lambda path, **kw: target2 if path == alias else original_resolve(path, **kw))
        return 'UNINSTALL CODEPIER'
    monkeypatch.setattr('builtins.input', answer)
    with pytest.raises(ValueError, match='changed'):
        cleanup.run(f.args, f.api)
    assert alias.exists() and not f.calls and not (base/'.install.lock').exists()


def test_verified_junction_is_pruned_from_directory_walk(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, _ = python_alias(base, monkeypatch)
    walk = cleanup.os.walk
    visited = []
    def watched(*args, **kwargs):
        for folder, directories, files in walk(*args, **kwargs):
            yield folder, directories, files
            if Path(folder) == alias.parent:
                assert alias.name not in directories
            visited.append(Path(folder))
    monkeypatch.setattr(cleanup.os, 'walk', watched)
    cleanup.inspect_root(base, f.api)
    assert alias not in visited


def test_unknown_reparse_tag_is_treated_as_a_link(monkeypatch, tmp_path):
    path = tmp_path/'opaque'
    path.mkdir()
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda p: SimpleNamespace(st_file_attributes=0x400, st_mode=original(p).st_mode) if p == path else original(p))
    assert cleanup.linked(path)


def test_tree_read_error_cannot_be_mistaken_for_an_empty_directory(fixture, monkeypatch):
    f = fixture
    base = f.root()
    def walk(path, *, followlinks, onerror=None):
        assert followlinks is False
        error = PermissionError('unreadable install contents')
        if onerror:
            onerror(error)
        return iter(())
    monkeypatch.setattr(cleanup.os, 'walk', walk)
    with pytest.raises(PermissionError, match='unreadable'):
        cleanup.run(f.args, f.api)
    assert (base/'config.json').exists() and not f.calls
    assert not (base/'.install.lock').exists()


def test_extra_unknown_link_remains_forbidden_with_valid_python_junction(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, _ = python_alias(base, monkeypatch)
    (base/'runtime').mkdir()
    (base/'runtime/unknown').symlink_to(f.home, target_is_directory=True)
    with pytest.raises(ValueError, match='Linked contents'):
        cleanup.run(f.args, f.api)
    assert alias.exists() and not f.calls


def test_junction_change_after_service_stop_is_not_handed_off(fixture, monkeypatch):
    f = fixture
    base = f.root()
    alias, target = python_alias(base, monkeypatch)
    f.tasks['CodePierAgent'] = xml(base)
    f.args.yes = True
    target2 = target.with_name('cpython-3.13.13-windows-x86_64-none')
    target2.mkdir()
    (target2/'python.exe').write_bytes(b'synthetic')
    def stopped(*args):
        original_readlink = cleanup.os.readlink
        original_resolve = Path.resolve
        monkeypatch.setattr(cleanup.os, 'readlink',
                            lambda path, **kw: str(target2) if Path(path) == alias else original_readlink(path, **kw))
        monkeypatch.setattr(Path, 'resolve', lambda path, **kw: target2 if path == alias else original_resolve(path, **kw))
    f.api.verify_windows_instance_is_stopped = stopped
    with pytest.raises(ValueError, match='changed during cleanup'):
        cleanup.run(f.args, f.api)
    assert not f.tasks and alias.exists()
    assert not (base/'.install.lock').exists()
    assert not Path(f.args.uninstall_handoff_file).exists()
