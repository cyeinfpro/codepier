"""Synthetic coverage for Hub read isolation and queue visibility."""
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError
from tests.test_reliability import runtime, Peer
from tests.test_mcp_tasks import task_runtime


@pytest.mark.asyncio
async def test_read_pool_parallel_query_only_and_fresh_snapshots(tmp_path):
    store = Store(tmp_path / 'hub')
    store.execute("INSERT INTO meta VALUES('fixture','old')")
    entered, release = threading.Event(), threading.Event()
    def write():
        with store.transaction():
            store.execute("UPDATE meta SET value='new' WHERE key='fixture'")
            entered.set()
            assert release.wait(5)
    task = asyncio.create_task(store.run(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        barrier = threading.Barrier(3)
        def read():
            barrier.wait(timeout=2)
            return store.one("SELECT value FROM meta WHERE key='fixture'")
        rows = await asyncio.wait_for(asyncio.gather(*(store.run_read(read) for _ in range(3))), 3)
        assert rows == [{'value': 'old'}] * 3
    finally:
        release.set()
        await task
    assert (await store.run_read(store.one, "SELECT value FROM meta WHERE key='fixture'"))['value'] == 'new'
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        await store.run_read(store.execute, "INSERT INTO meta VALUES('forbidden','write')")
    assert store.one("SELECT value FROM meta WHERE key='forbidden'") is None
    store.close()


@pytest.mark.asyncio
async def test_read_phase_retries_external_commit(tmp_path):
    store = Store(tmp_path / 'hub')
    store.execute("INSERT INTO meta VALUES('fixture','old')")
    entered, release, seen = threading.Event(), threading.Event(), []
    def read():
        row = store.one("SELECT value FROM meta WHERE key='fixture'")
        seen.append(row['value'])
        if len(seen) == 1:
            entered.set()
            assert release.wait(3)
        return row
    task = asyncio.create_task(store.run_read(read))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with sqlite3.connect(store.state_directory / 'hub.sqlite3') as other:
            other.execute("UPDATE meta SET value='new' WHERE key='fixture'")
        release.set()
        assert (await task)['value'] == 'new'
        assert seen == ['old', 'new']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
async def test_cancelled_reader_drains_and_closes(tmp_path):
    store = Store(tmp_path / 'hub')
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        assert release.wait(3)
        return store.one('SELECT 1 AS n')
    task = asyncio.create_task(store.run_read(read))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await store.aclose()
    with pytest.raises(RuntimeError):
        await store.run_read(lambda: None)


async def submit(r, p, key):
    return (await r.invoke('shell_exec', {'project': 'ProjectAlpha',
        'command': 'synthetic fixture', 'idempotency_key': key}, p))['operation_id']


async def subscribed(r, ids):
    async with asyncio.timeout(2):
        while not all(r.operation_waiters.get(identifier) for identifier in ids):
            await asyncio.sleep(.001)


@pytest.mark.asyncio
async def test_public_batch_wait_any_ignores_progress(runtime):
    r, p = runtime
    a, b = await submit(r, p, 'wait-first'), await submit(r, p, 'wait-second')
    task = asyncio.create_task(r.invoke('task_query', {'operation': 'wait',
        'operation_ids': [a, b], 'wait_seconds': 10}, p))
    try:
        await subscribed(r, [a, b])
        r.notify_operation(a)
        await asyncio.sleep(.02)
        assert not task.done()
        await r.store.run(r.complete, {'id': a}, {'ok': True, 'data': {'exit_code': 0}})
        result = await asyncio.wait_for(task, 2)
        assert [row['state'] for row in result['operations']] == ['succeeded', 'queued']
        assert not r.operation_waiters
        assert r.store.one('SELECT sum(cancel_requested) n FROM operations')['n'] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_batch_wait_cancellation_unsubscribes_all(runtime):
    r, p = runtime
    ids = [await submit(r, p, 'cancel-first'), await submit(r, p, 'cancel-second')]
    task = asyncio.create_task(r.wait_operations(ids, p, 10))
    await subscribed(r, ids)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not r.operation_waiters


@pytest.mark.asyncio
async def test_batch_wait_reauthorizes_after_wake(runtime):
    r, p = runtime
    ids = [await submit(r, p, 'revoke-first'), await submit(r, p, 'revoke-second')]
    task = asyncio.create_task(r.invoke('task_query', {'operation': 'wait',
        'operation_ids': ids, 'wait_seconds': 10}, p))
    await subscribed(r, ids)
    r.store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    r.notify_operation(ids[0])
    result = await asyncio.wait_for(task, 2)
    assert all('error' in row and 'result' not in row for row in result['operations'])
    assert not r.operation_waiters


@pytest.mark.asyncio
async def test_queue_and_live_hide_unauthorized_operation_ids(runtime, monkeypatch):
    r, p = runtime
    visible, hidden, waiting = [await submit(r, p, key) for key in ('visible-writer', 'hidden-writer', 'waiting-reader')]
    original = r.operation_row
    def authorized(identifier, *args, **kwargs):
        if identifier == hidden:
            raise DevError('NOT_FOUND', 'restricted', 404)
        return original(identifier, *args, **kwargs)
    monkeypatch.setattr(r, 'operation_row', authorized)
    r.diagnostics.record(waiting, 'waiting_resource', source='agent', seq=1,
        detail={'queue_reason': 'resource_conflict', 'lane': 'read', 'blocked_by': [visible, hidden]})
    receipt = r._pending_receipt(waiting, p)
    assert receipt['queue_stage'] == 'agent_waiting'
    assert [row['operation_id'] for row in receipt['waiting']['blocked_by']] == [visible]
    assert receipt['waiting']['blocker_visibility'] == 'restricted_or_unavailable'
    raw = {'ok': True, 'data': {'concurrency': {'mode': 'live', 'overlapping_writers': [visible, hidden]}}}
    result = r.authorized_result(waiting, raw, p)
    assert [row['operation_id'] for row in result['concurrency']['overlapping_writers']] == [visible]
    assert hidden not in json.dumps(result)
    assert raw['data']['concurrency']['overlapping_writers'] == [visible, hidden]


@pytest.mark.parametrize('args', [
    {'resources': [{'mode': 'read_stable'}]}, {'isolation': 'snapshot'},
    {'options': {'order': 'strict'}}, {'options': {'wait_policy': 'fail_fast'}},
])
def test_new_semantics_capability_gate(args):
    with pytest.raises(DevError, match='protocol 2'):
        Runtime.require_resource_semantics(SimpleNamespace(), args)
    Runtime.require_resource_semantics(SimpleNamespace(resource_semantics_protocol=2), args)
    Runtime.require_resource_semantics(SimpleNamespace(), {'options': {'order': 'default', 'wait_policy': 'queue'}})


@pytest.mark.asyncio
async def test_device_dispatch_locks_are_independent(runtime, monkeypatch):
    r, p = runtime
    monkeypatch.setattr(r, '_admit_operation', lambda *a: ('other-operation', {'ok': True, 'data': {}}))
    monkeypatch.setattr(r, 'authorized_result', lambda *a: {'operation_id': 'other-operation'})
    async with r.device_dispatch_lock('busy-device'):
        result = await asyncio.wait_for(r.dispatch('read', {}, {'device_id': 'other-device'}, p), 1)
    assert result['operation_id'] == 'other-operation'


@pytest.mark.asyncio
async def test_delivery_hot_projection_omits_cold_bodies(runtime):
    r, p = runtime
    identifier = await submit(r, p, 'skinny-delivery')
    r.connections['dev'] = Peer()
    statements = []
    with r.store.lock:
        r.store.db.set_trace_callback(statements.append)
    await r.deliver(identifier)
    reads = [q for q in statements if 'FROM operations WHERE id=' in q and q.startswith('SELECT')]
    assert reads and all('SELECT *' not in q for q in reads)
    assert all('result' not in q.split(' FROM ')[0] and 'output' not in q.split(' FROM ')[0] for q in reads)

@pytest.mark.asyncio
async def test_ordinary_exec_keeps_legacy_fingerprint_and_wire(runtime):
    from shared.contracts import TOOLS
    r, p = runtime
    raw = {'project': 'ProjectAlpha', 'command': 'true', 'yield_seconds': 0,
           'idempotency_key': 'legacy-defaults'}
    modern = TOOLS['exec'].model.model_validate(raw).model_dump()
    modern.pop('workspace_id', None)
    legacy = {key: value for key, value in modern.items() if key not in {'isolation', 'snapshot_artifacts', 'options'}}
    project = r.project('ProjectAlpha', p)
    identifier = r._admit_operation('exec', legacy, project, p)[0]
    # Identical pre-upgrade request must recover, never conflict or replay.
    assert r._admit_operation('exec', modern, project, p)[0] == identifier
    peer = Peer()
    r.connections['dev'] = peer
    await r.deliver(identifier)
    args = peer.packets[0]['args']
    assert not {'isolation', 'snapshot_artifacts', 'options'} & args.keys()
    assert args['command'] == 'true'


@pytest.mark.asyncio
async def test_resource_protocol_downgrade_fences_final_send(runtime):
    from shared.contracts import TOOLS
    r, p = runtime
    args = TOOLS['exec'].model.model_validate({'project': 'ProjectAlpha', 'command': 'true',
        'resources': [{'name': '.', 'mode': 'read_stable'}], 'yield_seconds': 0,
        'idempotency_key': 'protocol-downgrade'}).model_dump()
    args.pop('workspace_id', None)
    peer = Peer()
    peer.resource_semantics_protocol = 2
    r.connections['dev'] = peer
    identifier = r._admit_operation('exec', args, r.project('ProjectAlpha', p), p)[0]
    device, con, packets = r._prepare_delivery(identifier)
    assert packets[0]['type'] == 'call'
    peer.resource_semantics_protocol = 0
    assert not r._delivery_packet_current(identifier, device, con, packets[0])


def test_bypass_event_retains_bounded_count(runtime):
    r, _ = runtime
    r.diagnostics.ingest('synthetic-bypass', [{'stage': 'bypassed', 'seq': 1,
        'elapsed_ms': 10, 'detail': {'bypassed_count': 3}}])
    row = r.store.one("SELECT detail FROM operation_events WHERE operation_id='synthetic-bypass'")
    assert json.loads(row['detail'])['bypassed_count'] == 3

@pytest.mark.asyncio
async def test_frozen_mcp_result_filters_current_blocker_visibility(task_runtime, monkeypatch):
    from tests.test_mcp_tasks import create_task
    from hub.operation_history import unseal
    r, p, tasks = task_runtime
    identifier, _, _ = await create_task(r, p, tasks)
    writer, _, _ = await create_task(r, p, tasks)
    result = {'ok': True, 'data': {'concurrency': {'mode': 'live', 'overlapping_writers': [writer]}}}
    r.store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?", (json.dumps(result), identifier))
    frozen = r.store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result']
    original = r.operation_row
    def authorized(value, *args, **kwargs):
        if value == writer:
            raise DevError('NOT_FOUND', 'restricted', 404)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(r, 'operation_row', authorized)
    public = tasks.get(identifier, p)
    assert writer not in json.dumps(public)
    assert writer in unseal(r.store, frozen)
    assert r.store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result'] == frozen
