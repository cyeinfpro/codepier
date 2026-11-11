"""Slow desktop-provider preparation and cleanup stay owned by the caller."""
import asyncio
from pathlib import Path
import sys
import threading

import pytest

from agent.computer_appserver import AppServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['prepare', 'cleanup'])
async def test_slow_appserver_storage_survives_repeated_cancellation(tmp_path, monkeypatch, stage):
    client = AppServerClient({'available': True, 'app_server': sys.executable,
                              'launcher': '/fixture/provider', 'codex_home': '/fixture/home'})
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    observed, directories = [], []
    spawned = []

    async def no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError('cancelled preparation must not start a provider')

    monkeypatch.setattr('agent.computer_process.spawn_provider', no_spawn)

    def block():
        observed.append(threading.get_ident())
        entered.set()
        if not release.wait(2):
            raise AssertionError('event loop did not release storage')

    if stage == 'prepare':
        original = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == 'config.toml':
                directories.append(path.parent)
                block()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'write_text', write)
        operation = client.start()
    else:
        import tempfile
        temporary = tempfile.TemporaryDirectory(dir=tmp_path)
        client.temporary = temporary
        directories.append(Path(temporary.name))
        original = temporary.cleanup

        def cleanup():
            block()
            original()

        monkeypatch.setattr(temporary, 'cleanup', cleanup)
        operation = client.close()
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert observed and observed[0] != loop_thread
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
    assert not spawned
    assert client.closed and client.temporary is None
    assert directories and all(not directory.exists() for directory in directories)


@pytest.mark.asyncio
async def test_appserver_config_write_failure_cleans_temporary_home(monkeypatch):
    client = AppServerClient({'available': True, 'app_server': sys.executable,
                              'launcher': '/fixture/provider', 'codex_home': '/fixture/home'})
    directories = []
    original = Path.write_text

    def fail(path, *args, **kwargs):
        if path.name == 'config.toml':
            directories.append(path.parent)
            raise OSError('synthetic config write failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail)
    try:
        with pytest.raises(OSError, match='synthetic config'):
            await client.start()
        assert client.closed and client.temporary is None
        assert directories and all(not directory.exists() for directory in directories)
    finally:
        await client.close()
