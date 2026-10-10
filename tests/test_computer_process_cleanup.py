"""Disposable provider trees; never starts a real desktop app or native provider."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sys
import time
import subprocess

import pytest

from agent.computer import NativeClient

PROVIDER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path.cwd()
code = """import signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
(root / 'ready').write_text('ready')
try:
    while not (root / 'fixture-stop').exists():
        with (root / 'ticks').open('ab', buffering=0) as stream:
            stream.write(b'x')
        time.sleep(.01)
finally:
    (root / 'descendant-exited').write_text('done')
"""
child = subprocess.Popen([sys.executable, '-c', code, str(root)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not (root / 'ready').exists():
    time.sleep(.01)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    if request['method'] == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'disposable-fixture'}}
    else:
        result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}}
                            for name in ('list_apps', 'get_app_state')]}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if request['method'] == 'tools/list' and (root / 'early-exit').exists():
        break
'''


async def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(.01)
    assert predicate(), 'disposable fixture did not reach its expected state'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX provider descendant ownership')
@pytest.mark.asyncio
@pytest.mark.parametrize('early_exit', [False, True])
async def test_close_stops_owned_descendant_even_after_provider_leader_exits(tmp_path, early_exit):
    provider = tmp_path / 'provider.py'
    provider.write_text(PROVIDER)
    launcher = tmp_path / 'launcher'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(provider)]) + '\n')
    launcher.chmod(0o700)
    if early_exit:
        (tmp_path / 'early-exit').write_text('fixture')
    client = NativeClient({'available': True, 'launcher': str(launcher),
                           'plugin_root': str(tmp_path), 'codex_home': str(tmp_path)})
    try:
        await client.start()
        await wait_until(lambda: (tmp_path / 'ready').exists())
        if early_exit:
            await wait_until(lambda: client.process.returncode is not None)
        await client.close()
        await client.close()
        count = (tmp_path / 'ticks').stat().st_size
        await asyncio.sleep(.15)
        assert (tmp_path / 'ticks').stat().st_size == count, 'descendant still produced side effects after close'
    finally:
        # The fixture cooperatively exits even when the implementation leaks it.
        # Do not signal a historical PID or use broad process-name matching.
        (tmp_path / 'fixture-stop').write_text('stop')
        await client.close()
        await asyncio.sleep(.1)


def test_windows_provider_command_keeps_existing_direct_path():
    from agent.computer_process import provider_argv
    command = ['fixture-provider.exe', 'mcp']
    assert provider_argv(command, 'nt') == command
    assert provider_argv(command, 'posix')[-3:] == ['--', *command]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX provider supervisor')
@pytest.mark.asyncio
async def test_provider_launch_failure_is_reaped(tmp_path):
    from shared.util import DevError
    client = NativeClient({'available': True, 'launcher': str(tmp_path / 'missing-provider'),
                           'plugin_root': str(tmp_path), 'codex_home': str(tmp_path)})
    with pytest.raises(DevError):
        await client.start()
    assert client.closed and client.process.returncode is not None
    await client.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX provider supervisor')
@pytest.mark.asyncio
async def test_cancellation_while_spawn_completes_reaps_only_owned_provider(tmp_path, monkeypatch):
    import agent.computer_process as transport

    provider = tmp_path / 'provider.py'
    provider.write_text(PROVIDER)
    launcher = tmp_path / 'launcher'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(provider)]) + '\n')
    launcher.chmod(0o700)
    client = NativeClient({'available': True, 'launcher': str(launcher),
                           'plugin_root': str(tmp_path), 'codex_home': str(tmp_path)})
    spawned, release = asyncio.Event(), asyncio.Event()
    original = transport.spawn_provider

    async def delayed(*command, **kwargs):
        process = await original(*command, **kwargs)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(transport, 'spawn_provider', delayed)
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             start_new_session=True)
    task = asyncio.create_task(client.start())
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        await wait_until(lambda: (tmp_path / 'ready').exists())
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed and client.process.returncode is not None
        assert other.poll() is None, 'unrelated fixture process must remain alive'
        count = (tmp_path / 'ticks').stat().st_size
        await asyncio.sleep(.15)
        assert (tmp_path / 'ticks').stat().st_size == count
    finally:
        release.set()
        (tmp_path / 'fixture-stop').write_text('stop')
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        if other.poll() is None:
            other.terminate()
        other.wait(timeout=5)
