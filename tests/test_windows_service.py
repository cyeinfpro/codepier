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
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *_a, **_kw: saved)
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
