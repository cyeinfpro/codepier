#!/usr/bin/env python3
"""Real Windows Task Scheduler recovery acceptance using a disposable Agent stub.

Requires an elevated Windows terminal. Never touches the installed CodePier task,
configuration, credentials, projects, network or model services. No reboot/logout.
"""
from __future__ import annotations

import argparse
import json
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
        base = Path(folder)
        runtime = base/'runtime'
        (runtime/'agent').mkdir(parents=True)
        (runtime/'scripts').mkdir()
        (runtime/'scripts/__init__.py').write_text('')
        shutil.copyfile(ROOT/'scripts/agent_lifecycle.py', runtime/'scripts/agent_lifecycle.py')
        (runtime/'agent/__init__.py').write_text('')
        shutil.copyfile(ROOT/'agent/service_watchdog.py', runtime/'agent/service_watchdog.py')
        (base/'task-name').write_text(name)
        (runtime/'agent/__main__.py').write_text('''import asyncio,json,os,time
from pathlib import Path
from agent import service_watchdog
from scripts.agent_lifecycle import set_windows_task_enabled
original = service_watchdog.Watchdog
service_watchdog.Watchdog = lambda: original(timeout=12, interval=.5)
base = Path(__file__).resolve().parents[2]
def main():
    async def run():
        with service_watchdog.watch_event_loop():
            while True:
                temporary = base/'progress.tmp'
                temporary.write_text(json.dumps({'pid':os.getpid(),'at':time.time()}))
                temporary.replace(base/'progress.json')
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
        run([sys.executable, '-m', 'venv', '--without-pip', runtime/'.venv'])
        python = runtime/'.venv/Scripts/python.exe'
        installer.write_windows_wrapper(runtime)
        definition = base/'service.xml'
        definition.write_bytes(installer.windows_task_xml(base, python, name, installer.windows_user_sid()))
        def progress():
            try:
                return json.loads((base/'progress.json').read_text())
            except (OSError, ValueError):
                return {}
        created = False
        try:
            # A separate schtasks process starts the task and exits immediately.
            run(['schtasks.exe', '/Create', '/TN', name, '/XML', definition])
            created = True
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
            second = wait(lambda: p if (p := progress()).get('pid') != first['pid'] else None)
            result['killed_process_recovered'] = True
            print('Windowless startup and killed-process recovery passed.', flush=True)
            (base/'hang').touch()
            # The watchdog kills the stuck process; remove the fault before the
            # following timer launch so the new instance can stay healthy.
            wait_process_exit(second['pid'])
            (base/'hang').unlink()
            third = wait(lambda: p if (p := progress()).get('pid') != second['pid'] else None)
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
