"""Joint grants leave no partial ownership, wake only runnable work and stay fair."""
import asyncio
import time
from contextlib import AsyncExitStack

import pytest

from agent.resource_queue import Claim, ResourceQueue, canonical_path
from agent.scheduler import ProjectScheduler
from shared.contracts import TOOLS
from shared.tool_protocol import wire_version
from tests.test_audit_agent import local_agent


def path(name, write=True):
    return [Claim('agent', 'path', name, write)]


def joint(**config):
    resources = ResourceQueue()
    scheduler = ProjectScheduler({'adaptive': False, **config}, resources=resources)
    return resources, scheduler


@pytest.mark.asyncio
async def test_waiting_for_worker_does_not_own_resource_or_starve_independent_read():
    resources, scheduler = joint(minimum=1, initial=1, maximum=1)
    started = asyncio.Event()
    async def queued():
        async with scheduler.slot('waiting', 'A', claims=path('/alpha')):
            started.set()
    async with scheduler.slot('busy', 'B'):
        task = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert scheduler.snapshot()['queued'] == 1
        assert not resources.active
        assert resources.waiting[0][1] == 'waiting'
        async with scheduler.slot('other-read', 'C', 'read', claims=path('/beta', False)):
            assert set(identifier for identifier, _ in resources.active.values()) == {'other-read'}
            assert not started.is_set()
    await asyncio.wait_for(task, 1)
    assert started.is_set()
    assert not resources.active and not resources.waiting
    assert not scheduler.active and not scheduler.waiting and not scheduler.turn


@pytest.mark.asyncio
async def test_waiting_for_resource_never_occupies_worker_or_partial_multi_resource_set():
    resources, scheduler = joint(minimum=1, initial=1, maximum=1)
    reasons = []
    async def queued():
        async with scheduler.slot('both', 'A', claims=path('/a') + path('/b'),
                                  on_resource_wait=lambda blockers: reasons.append(blockers)):
            pass
    async with resources.slot('external', path('/b/child'), lambda _: None):
        task = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert reasons == [['external']]
        assert scheduler.snapshot()['running'] == 0
        assert list(resources.active.values()) == [('external', tuple(path('/b/child')))]
        async with scheduler.slot('independent', 'B', claims=path('/c')):
            assert scheduler.snapshot()['running'] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not scheduler.waiting and not resources.waiting
    assert not scheduler.active and not resources.active


@pytest.mark.asyncio
async def test_conflicting_writer_keeps_fifo_while_capacity_blocked_and_cancel_unblocks_read():
    resources, scheduler = joint(minimum=1, initial=1, maximum=1)
    entered = []
    async def acquire(name, lane, write):
        async with scheduler.slot(name, name, lane, claims=path('/shared', write)):
            entered.append(name)
    async with scheduler.slot('heavy', 'B'):
        async with resources.slot('reader', path('/shared', False), lambda _: None):
            writer = asyncio.create_task(acquire('writer', 'execution', True))
            await asyncio.sleep(0)
            reader = asyncio.create_task(acquire('later-read', 'read', False))
            await asyncio.sleep(0)
            assert not entered
        # Reader cannot overtake the earlier conflicting writer, although the
        # writer owns neither a path nor an execution slot while it waits.
        assert not entered and not resources.active
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.wait_for(reader, 1)
        assert entered == ['later-read']
    assert not scheduler.active and not scheduler.waiting
    assert not resources.active and not resources.waiting


@pytest.mark.asyncio
async def test_unrelated_release_does_not_wake_or_rewrite_blocked_waiters(monkeypatch):
    resources, scheduler = joint(initial=64, maximum=64)
    release = asyncio.Event()
    reports, wakes = [], []
    async def acquire(index):
        async with scheduler.slot(str(index), str(index), claims=path('/contended'),
                                  on_resource_wait=lambda blockers: reports.append((index, blockers))):
            await release.wait()
    holder = resources.slot('holder', path('/contended'), lambda _: None)
    await holder.__aenter__()
    tasks = [asyncio.create_task(acquire(index)) for index in range(64)]
    await asyncio.sleep(0)
    try:
        assert len(reports) == 64
        for token, event in scheduler._events.items():
            original = event.set
            def counted(token=token, original=original):
                wakes.append(token)
                original()
            monkeypatch.setattr(event, 'set', counted)
        async with resources.slot('unrelated', path('/elsewhere'), lambda _: None):
            pass
        assert not wakes and len(reports) == 64
        await holder.__aexit__(None, None, None)
        assert len(wakes) == 1
        assert len(scheduler.active) == 1 and len(scheduler.waiting) == 63
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await holder.__aexit__(None, None, None)
    assert not scheduler.active and not scheduler.waiting
    assert not resources.active and not resources.waiting
    assert not scheduler._events and not resources._events


@pytest.mark.asyncio
async def test_atomic_conversion_failure_keeps_no_provisional_claims():
    resources, scheduler = joint(minimum=1, initial=1, maximum=1)
    async with scheduler.slot('preflight', 'A', 'read') as original:
        async with scheduler.slot('worker', 'B'):
            before = scheduler.sequence, dict(scheduler.turn)
            assert scheduler.try_reserve('preflight', 'A', 'execution',
                                         replace=original, claims=path('/p')) is None
            assert scheduler.active[original.token] is original
            assert (scheduler.sequence, dict(scheduler.turn)) == before
            assert not resources.active and not resources.waiting
        async with resources.slot('writer', path('/p'), lambda _: None):
            assert scheduler.try_reserve('preflight', 'A', 'read',
                                         replace=original, claims=path('/p', False)) is None
            assert not resources.waiting
        assert scheduler.try_reserve('preflight', 'A', 'read',
                                     replace=original, claims=path('/p', False)) is not None
        assert list(resources.active.values()) == [('preflight', tuple(path('/p', False)))]
    assert not resources.active and not resources.waiting
    assert not scheduler.active and not scheduler.waiting


@pytest.mark.asyncio
async def test_joint_grant_cancelled_before_resume_releases_both_budgets():
    resources, scheduler = joint(initial=2)
    async with resources.slot('writer', path('/p'), lambda _: None):
        task = asyncio.create_task(scheduler.slot('queued', 'A', claims=path('/p')).__aenter__())
        await asyncio.sleep(0)
        token = next(iter(resources.active))
        # Exercise the exact event-loop boundary: granted but no waiter resumed.
        async with resources.condition:
            resources.discard(token)
            resources.drain()
            assert scheduler.active and len(resources.active) == 1
            task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not scheduler.active and not scheduler.waiting
    assert not resources.active and not resources.waiting


@pytest.mark.asyncio
@pytest.mark.parametrize('capture,detail', [(True, 'minimal'), (False, 'documents')])
async def test_strict_open_and_baseline_still_wait_for_root_writer(local_agent, capture, detail):
    agent, root = local_agent
    args = TOOLS['open_workspace'].model.model_validate({
        'project': 'fixture', 'capture_baseline': capture, 'context_detail': detail}).model_dump()
    entered = asyncio.Event()
    async def acquire():
        async with agent.execution_slot('strict-open', 'open_workspace',
                {'id': 'fixture', 'root': str(root), 'mode': 'write'}, args, root, False, time.time()+5):
            entered.set()
    async with agent.project_slot(root, operation_id='root-writer'):
        task = asyncio.create_task(acquire())
        async with asyncio.timeout(5):
            while not agent.resources.waiting:
                await asyncio.sleep(.01)
        assert not entered.is_set()
        assert all(ticket.operation_id != 'strict-open' for ticket in agent.scheduler.active.values())
        assert agent.resources.waiting[0][2] == tuple(path(canonical_path(root), False))
        trace = agent.telemetry.snapshot('strict-open')
        assert trace[-1]['stage'] == 'waiting_resource'
        assert trace[-1]['detail']['queue_reason'] == 'resource_conflict'
        assert trace[-1]['detail']['resource_kinds'] == ['path']
    await asyncio.wait_for(task, 5)
    assert entered.is_set()
    assert not agent._active_slots and not agent._waiting_writes


@pytest.mark.asyncio
async def test_minimal_open_uses_one_read_worker_while_root_and_execution_are_busy(local_agent, monkeypatch):
    agent, root = local_agent
    (root / 'AGENTS.md').write_text('Read these instructions before editing.')
    processes = []
    spawn = asyncio.create_subprocess_exec
    async def counted(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', counted)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(agent.project_slot(root, operation_id='root-writer'))
        for index in range(agent.scheduler.lane_limit('execution')):
            await stack.enter_async_context(agent.scheduler.slot(f'heavy-{index}', f'project-{index}'))
        await asyncio.wait_for(agent.handle({'id': 'light-open', 'tool': 'open_workspace',
            'tool_contract_version': wire_version('open_workspace'),
            'project': {'id': 'fixture', 'root': str(root), 'alias': 'fixture', 'mode': 'write',
                        '_coding_owner': 'grant:test'},
            'args': {'project': 'fixture'}}), 5)
        result = agent.journal.status('light-open')['result']
        assert result['ok'], result
        assert result['data']['context']['instruction_index'][0]['state'] == 'available'
        assert len(processes) == 1 and processes[0].returncode == 0
        assert agent._active_slots  # Existing writer ownership is unchanged.
        assert not agent.resources.waiting and not agent._read_workers
    assert not agent.scheduler.active and not agent.resources.active

