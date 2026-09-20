"""Metadata stays responsive under load without changing file ordering or receipts."""
import asyncio
import time
from dataclasses import replace

import pytest
from tests.test_reliability import runtime
from tests.test_audit_agent import local_agent


@pytest.mark.asyncio
async def test_tasks_list_bypasses_project_and_worker_locks(local_agent):
    agent, root = local_agent
    agent.semaphore = asyncio.Semaphore(0)
    agent.config['tasks'] = {'check': {'command': ['python3'], 'projects': ['fixture'], 'env': {'PRIVATE': 'hidden'}}}
    async with agent.project_slot(root, write=True, operation_id='long-test'):
        await asyncio.wait_for(agent.handle({
            'id': 'metadata', 'tool': 'tasks_list',
            'project': {'root': str(root), 'alias': 'fixture', 'allow_tasks': True},
            'args': {'project': 'fixture'},
        }), 1)
        result = agent.journal.status('metadata')['result']
        assert result['ok'] and result['data']['enabled']
        assert result['data']['tasks'][0]['environment_keys'] == ['PRIVATE']
        assert 'hidden' not in str(result)
        assert agent._active_slots
    assert not agent._active_slots


@pytest.mark.asyncio
async def test_tasks_list_fast_path_preserves_root_authorization(local_agent):
    agent, root = local_agent
    await agent.handle({'id': 'denied', 'tool': 'tasks_list',
        'project': {'root': str(root.parent), 'alias': 'fixture', 'allow_tasks': True},
        'args': {'project': 'fixture'}})
    assert agent.journal.status('denied')['result']['ok'] is False


@pytest.mark.asyncio
async def test_unkeyed_metadata_queries_share_one_receipt_under_concurrent_load(runtime):
    r, p = runtime
    receipts = await asyncio.gather(*(r.invoke('tasks_list', {'project': 'Imago' if i % 2 else 'imago'}, p)
                                      for i in range(80)))
    assert len({x['operation_id'] for x in receipts}) == 1
    assert r.store.one('SELECT count(*) n FROM operations')['n'] == 1
    original = receipts[0]['operation_id']
    # Even at admission capacity an existing receipt remains recoverable.
    for i in range(63):
        await r.invoke('fs_read', {'project': 'Imago', 'path': f'{i}.py'}, p)
    assert (await r.invoke('tasks_list', {'project': 'Imago'}, p))['operation_id'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['completed', 'expired', 'cancelled', 'actor', 'grant', 'root', 'explicit'])
async def test_metadata_coalescing_respects_receipt_boundaries(runtime, change):
    r, p = runtime
    args = {'project': 'Imago'}
    first = await r.invoke('tasks_list', args, p)
    identifier = first['operation_id']
    if change == 'completed':
        r.complete(r.store.one('SELECT * FROM operations WHERE id=?', (identifier,)), {'ok': True, 'data': {'tasks': []}})
    elif change == 'expired':
        r.store.execute('UPDATE operations SET deadline=? WHERE id=?', (time.time()-1, identifier))
    elif change == 'cancelled':
        r.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?', (identifier,))
    elif change == 'actor':
        p = replace(p, actor='different-owner')
    elif change == 'grant':
        p = replace(p, grant_id='different-grant')
    elif change == 'root':
        r.store.execute("UPDATE projects SET root='/tmp/other' WHERE id='proj'")
    elif change == 'explicit':
        args['idempotency_key'] = 'intentional-new-query'
    second = await r.invoke('tasks_list', args, p)
    assert second['operation_id'] != identifier
    if change == 'explicit':
        assert (await r.invoke('tasks_list', args, p))['operation_id'] == second['operation_id']
        assert (await r.invoke('tasks_list', {'project': 'Imago'}, p))['operation_id'] == identifier


@pytest.mark.asyncio
async def test_file_reads_keep_distinct_receipts(runtime):
    r, p = runtime
    args = {'project': 'Imago', 'path': 'source.py'}
    first = await r.invoke('fs_read', args, p)
    second = await r.invoke('fs_read', args, p)
    assert first['operation_id'] != second['operation_id']
