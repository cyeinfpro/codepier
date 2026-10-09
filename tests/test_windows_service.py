"""Windows startup/recovery regressions. Service-manager calls are isolated fakes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from agent import service_watchdog
from agent.lifecycle import LifecycleManager
from scripts import agent_lifecycle as lifecycle
from scripts import install_agent as installer

ROOT = Path(__file__).resolve().parents[1]
NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
SID = 'S-1-5-21-123-456-789-1001'


@pytest.fixture
def windows_install(tmp_path, monkeypatch):
    base = tmp_path / "agent & $cash' 空间"
    runtime = base/'runtime'
    python = runtime/'.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    python.with_name('pythonw.exe').touch()
    (runtime/'agent').mkdir()
    (runtime/'agent/service_watchdog.py').touch()
    (base/'config.json').write_text(json.dumps({'device_id': 'fixture', 'hub_url': 'https://hub.example'}))
    (base/'management.json').write_text(json.dumps({'service_kind': 'schtasks', 'service_scope': 'user'}))
    monkeypatch.setattr(installer, 'service_identity', lambda: ('schtasks', 'user'))
    return base, python


def test_task_runs_before_login_and_retries_without_an_expiration(windows_install):
    base, python = windows_install
    task = ET.fromstring(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    assert task.find('t:Triggers/t:BootTrigger', NS) is not None
    assert task.findtext('t:Principals/t:Principal/t:LogonType', namespaces=NS) == 'S4U'
    assert task.findtext('t:Principals/t:Principal/t:UserId', namespaces=NS) == SID
    assert task.findtext('t:Principals/t:Principal/t:RunLevel', namespaces=NS) == 'LeastPrivilege'
    repeat = task.find('t:Triggers/t:TimeTrigger/t:Repetition', NS)
    assert repeat.findtext('t:Interval', namespaces=NS) == 'PT1M'
    assert repeat.find('t:Duration', NS) is None
    assert task.find('t:Triggers/t:TimeTrigger/t:EndBoundary', NS) is None
    settings = task.find('t:Settings', NS)
    for key in ('DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'RunOnlyIfNetworkAvailable'):
        assert settings.findtext('t:'+key, namespaces=NS) == 'false'
    assert settings.findtext('t:ExecutionTimeLimit', namespaces=NS) == 'PT0S'
    assert settings.findtext('t:MultipleInstancesPolicy', namespaces=NS) == 'IgnoreNew'
    action = task.find('t:Actions/t:Exec', NS)
    assert action.findtext('t:Command', namespaces=NS) == str(python.with_name('pythonw.exe'))
    assert action.findtext('t:Arguments', namespaces=NS) == subprocess.list2cmdline([str(base/'runtime/run-service.py')])
    assert action.findtext('t:WorkingDirectory', namespaces=NS) == str(base/'runtime')


def test_missing_windowless_python_never_falls_back_to_a_console(windows_install):
    base, python = windows_install
    python.with_name('pythonw.exe').unlink()
    with pytest.raises(ValueError, match='pythonw.exe'):
        installer.windows_task_xml(base, python, 'CodePierAgent', SID)


def test_generated_wrapper_enters_supervised_runtime(windows_install, monkeypatch):
    base, _ = windows_install
    installer.write_windows_wrapper(base/'runtime')
    calls = []
    monkeypatch.setattr(service_watchdog, 'run_service', calls.append)
    runpy.run_path(str(base/'runtime/run-service.py'))
    assert calls == [base]


def test_uac_failure_keeps_previous_task_definition_and_wrapper(windows_install, monkeypatch):
    base, python = windows_install
    saved = base/'service.xml'
    previous_task = ET.fromstring(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    previous_task.find('t:Principals/t:Principal/t:LogonType', NS).text = 'InteractiveToken'
    previous = ET.tostring(previous_task, encoding='utf-16')
    saved.write_bytes(previous)
    wrapper = base/'runtime/run-service.py'
    wrapper.write_text('previous wrapper')
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(installer, 'verify_service_ownership', lambda *_a: None)
    monkeypatch.setattr(installer, 'require_idle', lambda *_a: None)
    monkeypatch.setattr(installer, 'windows_user_sid', lambda: SID)
    def reject(*_a):
        raise ValueError('UAC cancelled')
    monkeypatch.setattr(installer, 'register_windows_task', reject)
    with pytest.raises(ValueError, match='UAC cancelled'):
        installer.start_service(base, python)
    assert saved.read_bytes() == previous
    assert wrapper.read_text() == 'previous wrapper'
    assert not (base/'service-pending.xml').exists()


def test_existing_boot_task_starts_without_requesting_uac_again(windows_install, monkeypatch):
    base, python = windows_install
    saved = installer.windows_task_xml(base, python, 'CodePierAgent', SID)
    (base/'service.xml').write_bytes(saved)
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *_a, **_kw: saved.decode('utf-16').encode('utf-8'))
    monkeypatch.setattr(installer, 'windows_user_sid', lambda: SID)
    monkeypatch.setattr(installer, 'register_windows_task', lambda *_a: pytest.fail('must not request UAC again'))
    monkeypatch.setattr(installer.time, 'sleep', lambda *_a: None)
    events = []
    monkeypatch.setattr(installer, 'load_lifecycle_helper', lambda *_a:
                        SimpleNamespace(stop_service=lambda _base: events.append('stopped'),
                                        start_service=lambda _base: events.append('started'),
                                        verify_service=lambda _base: events.append('verified')))
    monkeypatch.setattr(installer, 'run', lambda command, **_kw: events.append(command))
    installer.start_service(base, python)
    assert events == ['stopped', 'started', 'verified']
    assert installer.windows_task_has_recovery((base/'service.xml').read_bytes(), SID)
    assert json.loads((base/'management.json').read_text())['startup'] == 'boot'


def test_uac_registers_only_task_and_keeps_user_paths_literal(tmp_path, monkeypatch):
    import base64
    import ctypes
    target = tmp_path / "O'Brien $cash & test.xml"
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False)), raising=False)
    commands = []
    monkeypatch.setattr(installer, 'run', lambda command, **_kw: commands.append(command))
    installer.register_windows_task('CodePierAgent', target)
    launch = commands[0][-1]
    encoded = launch.split("'-EncodedCommand','", 1)[1].split("'", 1)[0]
    script = base64.b64decode(encoded).decode('utf-16-le')
    assert '/XML '+"'"+str(target).replace("'", "''")+"'" in script
    assert 'Start-Process powershell.exe -Verb RunAs' in launch
    assert 'python' not in script


def test_foreign_task_is_not_replaced_or_stopped(windows_install, monkeypatch):
    base, python = windows_install
    saved = installer.windows_task_xml(base, python, 'CodePierAgent', SID)
    (base/'service.xml').write_bytes(saved)
    other = ET.fromstring(saved)
    other.find('t:Actions/t:Exec/t:WorkingDirectory', NS).text = 'C:\\someone-else'
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *_a, **_kw: ET.tostring(other))
    monkeypatch.setattr(installer, 'register_windows_task', lambda *_a: pytest.fail('must preserve foreign task'))
    with pytest.raises(ValueError, match='different Agent'):
        installer.start_service(base, python)
    assert (base/'service.xml').read_bytes() == saved
    assert not (base/'runtime/run-service.py').exists()


@pytest.mark.parametrize('remove', [False, True])
def test_maintenance_disables_recovery_before_stopping(windows_install, monkeypatch, remove):
    base, python = windows_install
    (base/'service.xml').write_bytes(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    state = {'enabled': True, 'running': True, 'deleted': False}
    def run(command, **_kw):
        if '/End' in command:
            # Simulate the timer firing just after the process stops.
            state['running'] = state['enabled']
        elif '/Delete' in command:
            assert not state['enabled'] and not state['running']
            state['deleted'] = True
        return 0
    monkeypatch.setattr(lifecycle, '_run', run)
    monkeypatch.setattr(lifecycle, 'set_windows_task_enabled', lambda _name, enabled: state.update(enabled=enabled))
    monkeypatch.setattr(lifecycle, 'service_is_running', lambda *_: state['running'])
    lifecycle.stop_service(base, remove=remove)
    assert state == {'enabled': False, 'running': False, 'deleted': remove}
    assert (base/'service.xml').exists() is not remove


def test_disable_failure_preserves_running_service_and_files(windows_install, monkeypatch):
    base, _ = windows_install
    calls = []
    def run(command, **_kw):
        calls.append(command)
        raise RuntimeError('access denied')
    monkeypatch.setattr(lifecycle, '_run', run)
    with pytest.raises(RuntimeError, match='access denied'):
        lifecycle.stop_service(base, remove=True)
    assert len(calls) == 1 and '$t.Enabled=$false' in calls[0][-1]
    assert (base/'config.json').exists()


def test_restart_reenables_periodic_recovery(windows_install, monkeypatch):
    base, _ = windows_install
    calls = []
    monkeypatch.setattr(lifecycle, '_run', lambda command, **_kw: calls.append(command))
    lifecycle.start_service(base)
    assert '$t.Enabled=$true' in calls[0][-1] and '/Run' in calls[1]


def test_failed_restart_restores_disabled_recovery(windows_install, monkeypatch):
    base, _ = windows_install
    def fail(_base):
        raise RuntimeError('stop failed after disabling task')
    monkeypatch.setattr(lifecycle, 'stop_service', fail)
    starts = []
    monkeypatch.setattr(lifecycle, 'start_service', starts.append)
    with pytest.raises(RuntimeError, match='stop failed'):
        lifecycle.restart(base, 0)
    assert starts == [base]


def test_failed_windows_query_is_not_mistaken_for_stopped(windows_install, monkeypatch):
    base, _ = windows_install
    monkeypatch.setattr(lifecycle, '_run', lambda *_a, **_kw: 1)
    with pytest.raises(RuntimeError, match='cannot verify'):
        lifecycle.service_is_running(base)


def test_no_login_lifecycle_handoff_inherits_service_identity(windows_install, monkeypatch):
    base, python = windows_install
    (base/'service.xml').write_bytes(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    manager = LifecycleManager(base/'config.json', base/'state', lambda: {})
    manager.base = base
    manager.plan_dir.mkdir(parents=True)
    monkeypatch.setattr(manager, '_service_kind', lambda: ('schtasks', 'user'))
    monkeypatch.setattr(manager, '_service_target', lambda: base/'service.xml')
    monkeypatch.setattr('agent.lifecycle.shutil.which', lambda _name: 'schtasks.exe')
    monkeypatch.setattr('agent.lifecycle.subprocess.check_output', lambda *_a, **_kw: 'fallback-user')
    tasks = []
    def run(command, **_kw):
        if '/XML' in command:
            tasks.append(ET.fromstring(Path(command[command.index('/XML')+1]).read_bytes()))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('agent.lifecycle.subprocess.run', run)
    manager._launch_helper('fixture', ['python.exe', 'helper.py'])
    assert len(tasks) == 1
    assert tasks[0].findtext('t:Principals/t:Principal/t:LogonType', namespaces=NS) == 'S4U'
    assert tasks[0].findtext('t:Principals/t:Principal/t:UserId', namespaces=NS) == SID


def test_network_outage_does_not_expire_watchdog_and_resume_has_grace():
    now = [0.0]
    watchdog = service_watchdog.Watchdog(timeout=120, clock=lambda: now[0])
    # A responsive event loop remains healthy without any network heartbeat.
    for _ in range(100):
        now[0] += 10
        watchdog.beat()
        assert not watchdog.stalled()
    now[0] += 3600
    assert not watchdog.stalled()
    for _ in range(23):
        now[0] += 5
        assert not watchdog.stalled()
    now[0] += 5
    assert watchdog.stalled()


def test_watchdog_terminates_a_real_stalled_process():
    program = """import time
from agent.service_watchdog import Watchdog
w = Watchdog(timeout=.15, interval=.02)
w.thread.start()
while True:
    time.sleep(.02)
"""
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            capture_output=True, timeout=5)
    assert result.returncode == 1
    assert b'event loop stalled' in result.stderr


@pytest.mark.asyncio
async def test_watchdog_heartbeat_has_margin_for_short_timeout(monkeypatch):
    watchdog = service_watchdog.Watchdog(timeout=12, interval=.05,
                                        terminate=lambda _code: pytest.fail('healthy loop was terminated'))
    monkeypatch.setattr(service_watchdog, 'Watchdog', lambda: watchdog)
    loop = asyncio.get_running_loop()
    original = loop.call_later
    delays = []

    def call_later(delay, callback, *args, **kwargs):
        delays.append(delay)
        return original(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, 'call_later', call_later)
    with service_watchdog.watch_event_loop():
        assert delays[0] <= watchdog.timeout / 4


@pytest.mark.asyncio
async def test_watchdog_stops_when_service_exits(monkeypatch):
    watchdog = service_watchdog.Watchdog(timeout=.15, interval=.02,
                                        terminate=lambda _code: pytest.fail('watchdog leaked after exit'))
    monkeypatch.setattr(service_watchdog, 'Watchdog', lambda: watchdog)
    with service_watchdog.watch_event_loop():
        await asyncio.sleep(.02)
    await asyncio.sleep(.2)
    assert not watchdog.thread.is_alive()


def test_source_adoption_preserves_config_and_uses_managed_runtime(tmp_path, monkeypatch):
    base = tmp_path/'agent'
    base.mkdir()
    config = base/'config.json'
    original = '{"device_id":"same-node", "hub_url":"https://hub.example", "secret":"preserve"}\n'
    config.write_text(original)
    monkeypatch.setattr(installer, 'service_identity', lambda: ('schtasks', 'user'))
    monkeypatch.setattr(installer, 'service_preflight', lambda *_a: None)
    monkeypatch.setattr(installer, 'find_uv', lambda *_a: tmp_path/'uv.exe')
    calls = []
    monkeypatch.setattr(installer, 'run', lambda command, **_kw: calls.append([str(p) for p in command]))
    def start(actual_base, python):
        assert actual_base == base and python == base/'runtime/.venv/Scripts/python.exe'
        assert (base/'runtime/agent/service_watchdog.py').exists()
        assert config.read_text() == original
    monkeypatch.setattr(installer, 'start_service', start)
    installer.install_source(base, ROOT)
    assert config.read_text() == original
    assert all('init' not in command for command in calls)
    assert '--relocatable' in calls[0]
    assert not (base/'runtime/config.json').exists()
    assert (base/'codepier-agent.ps1').exists()


def test_source_dependency_failure_is_retryable_with_old_config(tmp_path, monkeypatch):
    base = tmp_path/'agent'
    base.mkdir()
    config = base/'config.json'
    original = '{"device_id":"same-node", "hub_url":"https://hub.example"}'
    config.write_text(original)
    monkeypatch.setattr(installer, 'service_preflight', lambda *_a: None)
    monkeypatch.setattr(installer, 'find_uv', lambda *_a: tmp_path/'uv.exe')
    def fail(command, **_kw):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(installer, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install_source(base, ROOT)
    assert config.read_text() == original
    assert not (base/'runtime').exists()


def test_source_adoption_refuses_live_foreground_process(tmp_path, monkeypatch):
    from shared.instance_lock import InstanceLock
    base = tmp_path/'agent'
    base.mkdir()
    config = base/'config.json'
    config.write_text('{"device_id":"same-node", "hub_url":"https://hub.example"}')
    monkeypatch.setattr(installer, 'service_preflight', lambda *_a: None)
    with InstanceLock(base/'state/.agent.lock'):
        with pytest.raises(ValueError, match='foreground Agent'):
            installer.install_source(base, ROOT)
    assert not (base/'runtime').exists()


@pytest.fixture
def acceptance_stub(tmp_path, monkeypatch):
    from scripts import check_windows_service as acceptance
    runtime = tmp_path/'runtime'
    (runtime/'agent').mkdir(parents=True)
    acceptance.write_agent_stub(runtime)
    # The real generated module configures Watchdog; restore it after this test.
    monkeypatch.setattr(service_watchdog, 'Watchdog', service_watchdog.Watchdog)
    source = runtime/'agent/__main__.py'
    namespace = {'__file__': str(source), '__name__': 'acceptance_stub'}
    exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), namespace)
    return acceptance, namespace, source


def sharing_error(winerror):
    error = PermissionError('simulated Windows file sharing conflict')
    error.winerror = winerror
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize('winerror', [5, 32, 33])
async def test_acceptance_stub_retries_sharing_conflict_and_yields(
        acceptance_stub, monkeypatch, tmp_path, winerror):
    acceptance, namespace, _ = acceptance_stub
    previous = {'pid': 1, 'at': 1}
    (tmp_path/'progress.json').write_text(json.dumps(previous))
    original_replace = Path.replace
    original_sleep = asyncio.sleep
    attempts, pauses, heartbeat = [], [], []

    def replace(path, target):
        attempts.append(target)
        if len(attempts) <= 2:
            # Failed replacement must leave the previous complete snapshot intact.
            assert acceptance.read_progress(tmp_path) == previous
            raise sharing_error(winerror)
        return original_replace(path, target)

    async def pause(seconds):
        pauses.append(seconds)
        await original_sleep(0)

    async def beat():
        heartbeat.append(True)

    monkeypatch.setattr(Path, 'replace', replace)
    namespace['asyncio'] = SimpleNamespace(sleep=pause)
    beat_task = asyncio.create_task(beat())
    await namespace['write_progress']()
    assert beat_task.done() and heartbeat == [True]
    assert len(attempts) == 3 and pauses == [.05, .05]
    progress = acceptance.restarted_progress(tmp_path, previous)
    assert progress and progress['pid'] == namespace['os'].getpid()
    assert not (tmp_path/'progress.tmp').exists()


@pytest.mark.asyncio
async def test_acceptance_stub_sharing_conflict_has_bounded_original_failure(
        acceptance_stub, monkeypatch, tmp_path):
    acceptance, namespace, _ = acceptance_stub
    previous = {'pid': 1, 'at': 1}
    (tmp_path/'progress.json').write_text(json.dumps(previous))
    error = sharing_error(5)
    now, attempts = [0.0], []

    def replace(_path, _target):
        attempts.append(now[0])
        raise error

    async def pause(seconds):
        assert seconds == .05
        now[0] += .5

    namespace['time'] = SimpleNamespace(time=lambda: 10.0, monotonic=lambda: now[0])
    namespace['asyncio'] = SimpleNamespace(sleep=pause)
    monkeypatch.setattr(Path, 'replace', replace)
    with pytest.raises(PermissionError) as caught:
        await namespace['write_progress']()
    assert caught.value is error
    assert now[0] == 2.0 and attempts == [0.0, .5, 1.0, 1.5, 2.0]
    assert acceptance.read_progress(tmp_path) == previous
    assert acceptance.restarted_progress(tmp_path, previous) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [
    PermissionError('ordinary permission denied'),
    sharing_error(87),
    OSError('disk failure'),
    RuntimeError('unexpected writer failure'),
])
async def test_acceptance_stub_does_not_retry_unrelated_failure(
        acceptance_stub, monkeypatch, error):
    _, namespace, _ = acceptance_stub
    attempts = []

    def replace(_path, _target):
        attempts.append(True)
        raise error

    async def pause(_seconds):
        pytest.fail('unrelated errors must not retry')

    namespace['asyncio'] = SimpleNamespace(sleep=pause)
    monkeypatch.setattr(Path, 'replace', replace)
    with pytest.raises(type(error)) as caught:
        await namespace['write_progress']()
    assert caught.value is error
    assert attempts == [True]


def test_acceptance_stub_persistent_conflict_exits_process(acceptance_stub):
    _, _, source = acceptance_stub
    program = """import runpy, sys
from pathlib import Path
def denied(*_args):
    error = PermissionError('persistent progress write failure')
    error.winerror = 5
    raise error
namespace = runpy.run_path(sys.argv[1])
Path.replace = denied
namespace['main']()
"""
    completed = subprocess.run([sys.executable, '-c', program, str(source)],
                               cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert completed.returncode == 1
    assert 'PermissionError: persistent progress write failure' in completed.stderr
    assert 'event loop stalled' not in completed.stderr


@pytest.mark.parametrize('snapshot', [
    '{}', 'null', '[]', '{"at":2}', '{"pid":2}', '{"pid":0,"at":2}',
    '{"pid":true,"at":2}', '{"pid":"2","at":2}', '{"pid":2,"at":"2"}',
    '{"pid":2,"at":false}', '{"pid":2,"at":NaN}', '{"pid":2,"at":Infinity}',
    '{"pid":2,"at":0}', '{"pid":',
])
def test_acceptance_invalid_progress_never_proves_recovery(tmp_path, snapshot):
    from scripts import check_windows_service as acceptance
    (tmp_path/'progress.json').write_text(snapshot)
    assert acceptance.read_progress(tmp_path) == {}
    assert acceptance.restarted_progress(tmp_path, {'pid': 1, 'at': 1}) is None


def test_acceptance_unreadable_progress_recovers_without_false_restart(tmp_path, monkeypatch):
    from scripts import check_windows_service as acceptance
    previous = {'pid': 1, 'at': 1}
    assert acceptance.restarted_progress(tmp_path, previous) is None
    current = {'pid': 2, 'at': 2}
    path = tmp_path/'progress.json'
    path.write_text(json.dumps(current))
    original_read = Path.read_text
    attempts = []

    def read(target, *args, **kwargs):
        attempts.append(target)
        if len(attempts) == 1:
            raise sharing_error(32)
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    assert acceptance.restarted_progress(tmp_path, previous) is None
    assert acceptance.restarted_progress(tmp_path, previous) == current


@pytest.mark.parametrize('current', [{'pid': 1, 'at': 2}, {'pid': 2, 'at': 1}])
def test_acceptance_recovery_requires_new_pid_and_new_progress(tmp_path, current):
    from scripts import check_windows_service as acceptance
    (tmp_path/'progress.json').write_text(json.dumps(current))
    assert acceptance.restarted_progress(tmp_path, {'pid': 1, 'at': 1}) is None


def test_acceptance_missing_progress_still_times_out(tmp_path, monkeypatch):
    from scripts import check_windows_service as acceptance
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(acceptance, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep))
    with pytest.raises(RuntimeError, match='acceptance timed out'):
        acceptance.wait(lambda: acceptance.restarted_progress(
            tmp_path, {'pid': 1, 'at': 1}), seconds=1)
    assert now[0] == 1.0


@pytest.mark.parametrize('bom', [False, True])
def test_live_task_unicode_transport_handles_utf16_declaration_in_utf8(windows_install, monkeypatch, bom):
    import base64
    base, python = windows_install
    saved = installer.windows_task_xml(base, python, 'CodePierAgent', SID)
    (base/'service.xml').write_bytes(saved)
    # Exact failure shape reported from schtasks stdout: no BOM, UTF-8 bytes,
    # but the XML declaration still says UTF-16.
    live = saved.decode('utf-16').encode('utf-8')
    with pytest.raises(ET.ParseError, match='encoding specified'):
        ET.fromstring(live)
    calls = []
    def query(command, **kwargs):
        calls.append((command, kwargs))
        return (b'\xef\xbb\xbf' if bom else b'') + live
    monkeypatch.setattr(installer.subprocess, 'check_output', query)
    installer.verify_service_ownership(base)
    parsed = installer.query_windows_task_xml("Literal ' $task")
    assert installer.windows_task_has_recovery(parsed, SID)
    command, options = calls[-1]
    assert command[:4] == ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand']
    assert options == {'timeout': 30}
    script = base64.b64decode(command[-1]).decode('utf-16-le')
    assert '[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)' in script
    assert ".GetTask('Literal '' $task')" in script
    assert '[Console]::Write([string]$t.Xml)' in script
    assert 'schtasks' not in script and 'RunAs' not in script
    assert (base/'service.xml').read_bytes() == saved


@pytest.mark.parametrize('bad', [
    b'', b'<broken>', b'\x00<Task/>', b'<Task>\xff</Task>',
    '<?xml version="1.0" encoding="UTF-16"?><Task/>'.encode('utf-16'),
    '<Task>中文路径</Task>'.encode('cp936'),
    b'<!DOCTYPE Task [<!ENTITY x "text">]><Task>&x;</Task>',
    b'<Task/>' + b' '*(1024*1024),
], ids=['empty', 'malformed', 'nul', 'invalid-utf8', 'unexpected-utf16',
        'legacy-codepage', 'dtd-entity', 'oversize'])
def test_live_task_transport_rejects_ambiguous_or_invalid_xml_without_replacing(windows_install, monkeypatch, bad):
    base, python = windows_install
    saved = installer.windows_task_xml(base, python, 'CodePierAgent', SID)
    (base/'service.xml').write_bytes(saved)
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *_a, **_kw: bad)
    with pytest.raises(ValueError, match='service was preserved'):
        installer.verify_service_ownership(base)
    assert (base/'service.xml').read_bytes() == saved
    assert not (base/'service-pending.xml').exists()


@pytest.mark.parametrize('field', ['Command', 'Arguments', 'WorkingDirectory'])
def test_unicode_live_task_foreign_action_still_rejected(windows_install, monkeypatch, field):
    base, python = windows_install
    saved = installer.windows_task_xml(base, python, 'CodePierAgent', SID)
    (base/'service.xml').write_bytes(saved)
    live = ET.fromstring(saved)
    live.find('t:Actions/t:Exec/t:'+field, NS).text = 'C:\\foreign\\中文'
    # Unicode COM export transported as UTF-8, not a locale-decoded path.
    output = ET.tostring(live, encoding='unicode').encode('utf-8')
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *_a, **_kw: output)
    with pytest.raises(ValueError, match='different Agent'):
        installer.verify_service_ownership(base)
    assert (base/'service.xml').read_bytes() == saved


EXPORTED_DEFAULT_PATHS = (
    'Principals/Principal/RunLevel', 'Triggers/BootTrigger/Enabled',
    'Triggers/TimeTrigger/Enabled', 'Settings/RunOnlyIfNetworkAvailable',
)


def task_field(root, path):
    return root.find('/'.join('t:'+part for part in path.split('/')), NS)


def test_windows_export_omitted_defaults_preserve_same_recovery_policy(windows_install):
    base, python = windows_install
    root = ET.fromstring(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    for path in EXPORTED_DEFAULT_PATHS:
        parent = task_field(root, path.rsplit('/', 1)[0])
        parent.remove(task_field(root, path))
    raw = ET.tostring(root, encoding='unicode')
    assert installer.windows_task_has_recovery(raw, SID)
    assert not installer.windows_task_has_recovery(raw, 'S-1-5-21-999-999-999-999')


@pytest.mark.parametrize('path,value', [
    ('Principals/Principal/RunLevel', 'HighestAvailable'),
    ('Triggers/BootTrigger/Enabled', 'false'),
    ('Triggers/TimeTrigger/Enabled', 'false'),
    ('Settings/RunOnlyIfNetworkAvailable', 'true'),
] + [(path, '') for path in EXPORTED_DEFAULT_PATHS])
def test_windows_export_explicit_nondefault_or_empty_still_rejected(windows_install, path, value):
    base, python = windows_install
    root = ET.fromstring(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    task_field(root, path).text = value
    assert not installer.windows_task_has_recovery(ET.tostring(root, encoding='unicode'), SID)


@pytest.mark.parametrize('path', [
    'Principals/Principal', 'Triggers/BootTrigger', 'Triggers/TimeTrigger', 'Settings',
])
def test_missing_parent_cannot_gain_export_defaults(windows_install, path):
    base, python = windows_install
    root = ET.fromstring(installer.windows_task_xml(base, python, 'CodePierAgent', SID))
    parent = task_field(root, path.rsplit('/', 1)[0]) if '/' in path else root
    parent.remove(task_field(root, path))
    assert not installer.windows_task_has_recovery(ET.tostring(root, encoding='unicode'), SID)
