#!/usr/bin/env python3
"""Real Windows Task Scheduler recovery acceptance using a disposable Agent stub.

Requires an elevated Windows terminal. Never touches the installed CodePier task,
configuration, credentials, projects, network or model services. No reboot/logout.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import install_agent as installer
from scripts import agent_lifecycle as lifecycle


def run(command, *, check=True):
    return subprocess.run([str(p) for p in command], check=check, timeout=45,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait(predicate, *, seconds=85):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.25)
    raise RuntimeError('Windows scheduled service acceptance timed out')


def wait_process_exit(pid, seconds=25):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Already exited.
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, int(seconds*1000)) != 0:
            raise RuntimeError('Scheduled Agent process did not exit')
    finally:
        kernel.CloseHandle(handle)


def copy_runtime_support(runtime):
    """Keep the disposable runtime complete, including real stdlib imports."""
    for package in ('agent', 'scripts', 'shared'):
        (runtime/package).mkdir(parents=True, exist_ok=True)
        (runtime/package/'__init__.py').write_text('', encoding='utf-8')
    for relative in ('scripts/agent_lifecycle.py', 'agent/service_watchdog.py',
                     'shared/brand_migration.py'):
        shutil.copyfile(ROOT/relative, runtime/relative)


def write_agent_stub(runtime):
    """Write the exact disposable process exercised by acceptance and regressions."""
    (runtime/'agent/__main__.py').write_text('''import asyncio,json,os,time
from pathlib import Path
from agent import service_watchdog
from scripts.agent_lifecycle import set_windows_task_enabled
original = service_watchdog.Watchdog
service_watchdog.Watchdog = lambda: original(timeout=12, interval=.5)
base = Path(__file__).resolve().parents[2]
async def write_progress():
    temporary = base/'progress.tmp'
    temporary.write_text(json.dumps({'pid':os.getpid(),'at':time.time()}))
    deadline = time.monotonic()+2
    while True:
        try:
            temporary.replace(base/'progress.json')
            return
        except PermissionError as exc:
            # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
            # Bound only known sharing/access conflicts; real failures still exit.
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(.05)
def main():
    async def run():
        with service_watchdog.watch_event_loop():
            while True:
                await write_progress()
                if (base/'check-maintenance').exists():
                    (base/'check-maintenance').unlink()
                    for enabled in (False, True):
                        set_windows_task_enabled((base/'task-name').read_text(), enabled)
                    (base/'maintenance-checked').touch()
                if (base/'hang').exists():
                    while True: time.sleep(.2)
                await asyncio.sleep(.2)
    asyncio.run(run())
''', encoding='utf-8')


def read_progress(base):
    try:
        progress = json.loads((base/'progress.json').read_text())
    except (OSError, ValueError):
        return {}
    if (not isinstance(progress, dict)
            or type(progress.get('pid')) is not int or progress['pid'] <= 0
            or type(progress.get('at')) not in (int, float)
            or not math.isfinite(progress['at']) or progress['at'] <= 0):
        return {}
    return progress


def restarted_progress(base, previous):
    progress = read_progress(base)
    if (progress and progress['pid'] != previous['pid']
            and progress['at'] > previous['at']):
        return progress
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('This acceptance requires real Windows; no platform emulation is used.')
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise SystemExit('Run this isolated acceptance in an administrator terminal.')
    name = 'CodePierAgentTest-'+uuid.uuid4().hex
    result = {'production_changed': False, 'task': name,
              'scope': 'real Windows S4U task, windowless process and recovery',
              'reboot_before_login': 'not_tested'}
    with tempfile.TemporaryDirectory(prefix='codepier-service-test-') as folder:
        base = Path(folder)/"Agent 中文 é space"
        base.mkdir()
        runtime = base/'runtime'
        copy_runtime_support(runtime)
        (base/'task-name').write_text(name)
        write_agent_stub(runtime)
        run([sys.executable, '-m', 'venv', '--without-pip', runtime/'.venv'])
        python = runtime/'.venv/Scripts/python.exe'
        installer.write_windows_wrapper(runtime)
        # Fail immediately with a captured import error instead of waiting for
        # pythonw.exe, which has no console before the log wrapper is imported.
        run([python, '-I', '-c', 'import sys; sys.path.insert(0, '+repr(str(runtime))+'); '
             'import agent.service_watchdog; import scripts.agent_lifecycle'])
        definition = base/'service.xml'
        definition.write_bytes(installer.windows_task_xml(base, python, name, installer.windows_user_sid()))
        def progress():
            return read_progress(base)
        created = False
        try:
            # A separate schtasks process starts the task and exits immediately.
            run(['schtasks.exe', '/Create', '/TN', name, '/XML', definition])
            created = True
            # Exercise the real installer ownership gate, not only Task Scheduler
            # startup. The only substitution is this disposable task's name.
            from unittest.mock import patch
            with patch.object(installer, 'managed_service_name', return_value=name):
                installer.verify_service_ownership(base)
            exported = installer.query_windows_task_xml(name)
            result['installer_live_ownership_unicode'] = True
            sid = installer.windows_user_sid()
            if not installer.windows_task_has_recovery(exported, sid):
                import xml.etree.ElementTree as ET
                ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
                actual, expected = ET.fromstring(exported), ET.fromstring(definition.read_bytes())
                paths = ('Principals/Principal/LogonType', 'Principals/Principal/RunLevel',
                         'Triggers/BootTrigger/Enabled', 'Triggers/TimeTrigger/Enabled',
                         'Triggers/TimeTrigger/Repetition/Interval', 'Triggers/TimeTrigger/Repetition/Duration',
                         'Triggers/TimeTrigger/EndBoundary', 'Settings/MultipleInstancesPolicy',
                         'Settings/ExecutionTimeLimit', 'Settings/DisallowStartIfOnBatteries',
                         'Settings/StopIfGoingOnBatteries', 'Settings/RunOnlyIfNetworkAvailable',
                         'Settings/StartWhenAvailable')
                def field(root, path):
                    return root.findtext('/'.join('t:'+part for part in path.split('/')), namespaces=ns)
                result['recovery_profile_comparison'] = {path: {'expected': field(expected, path),
                    'actual': field(actual, path)} for path in paths}
                result['principal_sid_matches'] = field(actual, 'Principals/Principal/UserId') == sid
                result['principal_is_sid'] = str(field(actual, 'Principals/Principal/UserId')).startswith('S-1-')
                raise AssertionError('Exported recovery profile differs; inspect bounded field comparison')
            result['unicode_task_recovery_verified'] = True
            run(['schtasks.exe', '/Run', '/TN', name])
            first = wait(progress)
            wait(lambda: progress().get('at', 0) >= first['at']+15, seconds=25)
            assert progress()['pid'] == first['pid'], 'responsive Agent must not restart'
            result['survives_launcher_exit'] = True
            result['responsive_watchdog'] = True
            (base/'check-maintenance').touch()
            wait(lambda: (base/'maintenance-checked').exists(), seconds=25)
            result['noninteractive_account_can_manage_recovery'] = True
            # Simulate closing/killing the Agent, without cancelling its task.
            run(['taskkill.exe', '/PID', str(first['pid']), '/F'])
            second = wait(lambda: restarted_progress(base, first))
            result['killed_process_recovered'] = True
            print('Windowless startup and killed-process recovery passed.', flush=True)
            (base/'hang').touch()
            # The watchdog kills the stuck process; remove the fault before the
            # following timer launch so the new instance can stay healthy.
            wait_process_exit(second['pid'])
            (base/'hang').unlink()
            third = wait(lambda: restarted_progress(base, second))
            assert third['pid'] not in {first['pid'], second['pid']}
            result['stalled_process_recovered'] = True
            lifecycle.set_windows_task_enabled(name, False)
            run(['schtasks.exe', '/End', '/TN', name])
            wait_process_exit(third['pid'])
            stopped = progress()['at']
            # Cross a full timer interval to prove maintenance is not undone.
            deadline = time.monotonic()+65
            while time.monotonic() < deadline:
                assert progress()['at'] == stopped, 'disabled task restarted during maintenance'
                time.sleep(.5)
            result['maintenance_stays_stopped'] = True
        except Exception as exc:
            result['error'] = str(exc)
            result['last_progress'] = progress()
            stderr = base/'logs/stderr.log'
            if stderr.exists():
                result['service_stderr_tail'] = stderr.read_text(encoding='utf-8', errors='replace')[-4000:]
            try:
                task = run(['schtasks.exe', '/Query', '/TN', name, '/V', '/FO', 'LIST'], check=False)
                result['task_query'] = {'returncode': task.returncode,
                                        'stdout_tail': task.stdout[-4000:],
                                        'stderr_tail': task.stderr[-1000:]}
            except Exception as query_error:
                result['task_query_error'] = str(query_error)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2), file=sys.stderr, flush=True)
            raise
        finally:
            if created:
                lifecycle.set_windows_task_enabled(name, False)
                run(['schtasks.exe', '/End', '/TN', name], check=False)
                run(['schtasks.exe', '/Delete', '/TN', name, '/F'])
                time.sleep(2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
