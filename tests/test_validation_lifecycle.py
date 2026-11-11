"""Validation receipts stay truthful through slow storage and cancellation."""
import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from agent.runner import Agent
from agent.source_versions import Validations
from shared.util import DevError


class MemoryRecords:
    def __init__(self):
        self.rows = {}

    def save(self, kind, identifier, project, body, *, replace=False):
        if identifier in self.rows and not replace:
            raise DevError('RECORD_EXISTS', 'existing receipt', 409)
        self.rows[identifier] = deepcopy(body)


@pytest.fixture
def validation(tmp_path, monkeypatch):
    import agent.source_versions as versions
    records = MemoryRecords()
    project = {'id': 'fixture', 'root': str(tmp_path)}
    engine = SimpleNamespace(root=lambda project: (tmp_path, {}),
                             path=lambda root, relative: root / relative)
    executions = []

    async def execute(*args):
        executions.append(args)
        return {'exit_code': 0, 'output': 'fixture', 'duration_ms': 1}

    agent = SimpleNamespace(engine=engine, execute=execute, disk_call=Agent.disk_call)
    monkeypatch.setattr(versions, 'source_version',
                        lambda *args: {'complete': True, 'sha256': 'a' * 64})
    args = {'project': 'fixture', 'cwd': '.', 'command': 'fixture',
            'timeout_seconds': 5, 'env': {}, 'label': 'fixture'}
    return Validations(agent, records), project, args, executions


@pytest.mark.asyncio
async def test_post_execution_snapshot_error_is_recorded_as_interrupted(validation, monkeypatch):
    manager, project, args, executions = validation
    calls = 0

    def fingerprint(*unused):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic storage failure')
        return {'complete': True, 'sha256': 'a' * 64}

    monkeypatch.setattr('agent.source_versions.source_version', fingerprint)
    with pytest.raises(OSError, match='synthetic storage'):
        await manager.run('receipt', project, args)
    assert len(executions) == 1
    saved = manager.records.rows['receipt']
    assert saved['state'] == 'interrupted' and saved['reason'] == 'OSError'
    assert saved['finished'] >= saved['created']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['cwd', 'initial_save', 'after_snapshot', 'final_save'])
async def test_slow_validation_io_keeps_loop_live_and_owns_repeated_cancellation(
        validation, monkeypatch, stage):
    manager, project, args, executions = validation
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    io_threads = []

    def block():
        io_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(2):
            raise AssertionError('event loop did not release the storage operation')

    if stage == 'cwd':
        original = manager.agent.engine.path

        def path(*values):
            block()
            return original(*values)

        manager.agent.engine.path = path
    elif stage in {'initial_save', 'final_save'}:
        original = manager.records.save

        def save(kind, identifier, project, body, *, replace=False):
            if (stage == 'initial_save' and not replace
                    or stage == 'final_save' and body['state'] == 'passed'):
                block()
            return original(kind, identifier, project, body, replace=replace)

        manager.records.save = save
    else:
        count = 0

        def fingerprint(*unused):
            nonlocal count
            count += 1
            if count == 2:
                block()
            return {'complete': True, 'sha256': 'a' * 64}

        monkeypatch.setattr('agent.source_versions.source_version', fingerprint)

    work = asyncio.create_task(manager.run('receipt', project, args))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .5)
        assert len(io_threads) == 1 and io_threads[0] != loop_thread
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done(), 'storage must settle before cancellation escapes'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
    if stage == 'cwd':
        assert manager.records.rows == {} and executions == []
    else:
        assert manager.records.rows['receipt']['state'] == 'interrupted'
        assert manager.records.rows['receipt']['reason'] == 'CancelledError'
        assert len(executions) == (0 if stage == 'initial_save' else 1)


@pytest.mark.asyncio
async def test_duplicate_validation_never_rewrites_original_receipt(validation):
    manager, project, args, executions = validation
    original = {'state': 'passed', 'decision': {'action': 'accept'}, 'sentinel': 'keep'}
    manager.records.rows['receipt'] = deepcopy(original)
    with pytest.raises(DevError) as caught:
        await manager.run('receipt', project, args)
    assert caught.value.code == 'RECORD_EXISTS'
    assert manager.records.rows['receipt'] == original and executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cwd', ['../outside', '/outside'])
async def test_validation_cwd_boundary_still_precedes_execution(validation, cwd):
    manager, project, args, executions = validation
    # Use the real path guard for relative traversal; the fixture engine only
    # supplies basic paths, so absolute escape is checked directly by the service.
    if cwd.startswith('..'):
        def reject(*unused):
            raise DevError('PATH_OUTSIDE_ROOT', 'outside', 403)
        manager.agent.engine.path = reject
    with pytest.raises(DevError):
        await manager.run('receipt', project, {**args, 'cwd': cwd})
    assert manager.records.rows == {} and executions == []
