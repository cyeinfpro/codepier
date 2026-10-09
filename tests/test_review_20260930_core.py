"""2026-09-30 scheduler/import regressions; all state and I/O are disposable."""
import asyncio
import json
import threading
import time
import uuid
from dataclasses import replace

import pytest

from agent import incoming_artifacts as incoming
from agent.resource_queue import Claim, canonical_path, claims_for
from shared.file_sources import safe_import_error_detail
from shared.util import DevError
from tests.review_support import DATA, Peer, download, eventually, local_agent, project, request, runtime, waiting


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['write', 'fs_write'])
async def test_slow_import_does_not_hold_global_write_lock(local_agent, monkeypatch, tool):
    agent, root = local_agent
    started, release = threading.Event(), threading.Event()

    def stream(*args, providers=()):
        assert not providers
        started.set()
        assert release.wait(10)
        yield DATA

    monkeypatch.setattr(incoming, 'download_chunks', stream)
    call = download(root)
    job = asyncio.create_task(agent.handle(call))
    try:
        await eventually(started.is_set)
        sibling = root / 'other-project'
        sibling.mkdir()
        write = request(sibling, tool, path='new.txt', content='progress',
                        expected_sha256='new', idempotency_key=uuid.uuid4().hex)
        await asyncio.wait_for(agent.handle(write), 2)
        assert agent.journal.status(write['id'])['result']['ok']
        assert (sibling / 'new.txt').read_text() == 'progress'
        assert not job.done()
    finally:
        release.set()
        await asyncio.wait_for(job, 3)
    assert agent.journal.status(call['id'])['result']['ok']


@pytest.mark.asyncio
@pytest.mark.parametrize('shared', [False, True])
async def test_core_named_task_obeys_local_read_concurrency(local_agent, monkeypatch, shared):
    agent, root = local_agent
    (root / 'old.txt').write_text('before')
    agent.config['tasks']['check'] = {'command': ['fixture'], 'projects': ['fixture'], 'allow_read_concurrency': shared}
    started, release = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        await release.wait()
        return {'exit_code': 0, 'output': 'fixture completed'}

    monkeypatch.setattr(agent, 'run_process', run)
    task = request(root, 'exec', task='check', idempotency_key=uuid.uuid4().hex,
                   resources=[{'kind': 'path', 'name': 'unrelated', 'mode': 'read'}])
    task_job = asyncio.create_task(agent.handle(task))
    read = request(root, 'read', path='old.txt')
    write = request(root, 'write', path='new.txt', content='after',
                    expected_sha256='new', idempotency_key=uuid.uuid4().hex)
    jobs = []
    try:
        await asyncio.wait_for(started.wait(), 2)
        jobs.append(asyncio.create_task(agent.handle(read)))
        if shared:
            await asyncio.wait_for(jobs[0], 2)
            assert agent.journal.status(read['id'])['result']['ok']
        else:
            await eventually(lambda: waiting(agent, read))
        jobs.append(asyncio.create_task(agent.handle(write)))
        await eventually(lambda: waiting(agent, write))
        assert not (root / 'new.txt').exists()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task_job, *jobs), 3)
    assert agent.journal.status(task['id'])['result']['data']['resource_coordination'] == 'configured_task'
    assert (root / 'new.txt').read_text() == 'after'


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,args', [
    ('fs_tree', {'path': 'artifacts'}),
    ('fs_search', {'path': 'artifacts', 'query': 'marker'}),
    ('fs_read', {'path': 'artifacts/log.txt'}),
    ('fs_read_many', {'paths': ['artifacts/log.txt']}),
])
async def test_waiting_scoped_read_does_not_block_unrelated_write(local_agent, tool, args):
    agent, root = local_agent
    (root / 'artifacts').mkdir()
    (root / 'artifacts/log.txt').write_text('marker')
    call = request(root, tool, **args)
    job = None
    try:
        async with agent.resources.slot('artifact-writer',
                [Claim('agent', 'path', canonical_path(root / 'artifacts'), True)], lambda _: None):
            job = asyncio.create_task(agent.handle(call))
            await eventually(lambda: waiting(agent, call))
            write = request(root, 'write', path='source.py', content='unrelated',
                            expected_sha256='new', idempotency_key=uuid.uuid4().hex)
            await asyncio.wait_for(agent.handle(write), 2)
            assert agent.journal.status(write['id'])['result']['ok']
            assert not job.done()
        await asyncio.wait_for(job, 2)
        assert agent.journal.status(call['id'])['result']['ok']
    finally:
        if job:
            await asyncio.wait_for(job, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['cancel_call', 'cancel_pending_call'])
@pytest.mark.parametrize('tool,args', [('fs_tree', {'path': '.'}), ('project_context', {})])
async def test_accepted_read_cancellation_cleans_resource_waiters(local_agent, method, tool, args):
    agent, root = local_agent
    call = request(root, tool, **args)
    async with agent.resources.slot('writer', [Claim('agent', 'path', canonical_path(root), True)], lambda _: None):
        job = asyncio.create_task(agent.handle(call))
        agent.jobs[call['id']] = job
        try:
            await eventually(lambda: waiting(agent, call))
            getattr(agent, method)(call['id'])
            await asyncio.wait_for(job, 2)
            assert agent.journal.status(call['id'])['result']['error']['code'] == 'CANCELLED'
            assert not agent.resources.waiting
        finally:
            agent.jobs.pop(call['id'], None)
    assert not agent.resources.active
    write = request(root, 'fs_write', path='after-cancel.txt', content='continued',
                    expected_sha256='new', idempotency_key=uuid.uuid4().hex)
    await asyncio.wait_for(agent.handle(write), 2)
    assert agent.journal.status(write['id'])['result']['ok']
    assert (root / 'after-cancel.txt').read_text() == 'continued'


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,args', [
    ('read', {'path': 'file'}),
    ('fs_tree', {'path': '.'}),
    ('fs_read', {'path': 'file'}),
    ('fs_read_many', {'paths': ['file']}),
    ('fs_search', {'query': 'marker'}),
    ('project_context', {}),
])
async def test_hub_allows_durable_dispatched_read_cancel(runtime, tool, args):
    hub, principal = runtime
    receipt = await hub.invoke(tool, {'project': 'ProjectAlpha', **args}, principal)
    identifier = receipt['operation_id']
    hub.store.execute('UPDATE operations SET attempts=1,accepted_at=? WHERE id=?', (time.time(), identifier))
    reply = await hub.cancel(identifier, principal)
    assert reply['cancel_requested'] and reply['state'] == 'cancelling'
    peer = Peer()
    hub.connections['dev'] = peer
    await hub.deliver(identifier)
    assert [packet['type'] for packet in peer.packets] == ['cancel', 'probe']
    assert hub.operation(identifier, principal)['pending']


@pytest.mark.asyncio
async def test_dispatched_mutation_remains_noncancellable(runtime):
    hub, principal = runtime
    receipt = await hub.invoke('fs_write', {'project': 'ProjectAlpha', 'path': 'file', 'content': 'x',
                              'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex}, principal)
    hub.store.execute('UPDATE operations SET attempts=1 WHERE id=?', (receipt['operation_id'],))
    with pytest.raises(DevError) as error:
        await hub.cancel(receipt['operation_id'], principal)
    assert error.value.code == 'NOT_CANCELLABLE'


@pytest.mark.asyncio
async def test_terminal_blocker_remains_in_authorized_trace(runtime):
    hub, principal = runtime
    blocker = await hub.invoke('fs_read', {'project': 'ProjectAlpha', 'path': 'blocker'}, principal)
    target = await hub.invoke('fs_read', {'project': 'ProjectAlpha', 'path': 'target'}, principal)
    hub.diagnostics.record(target['operation_id'], 'waiting_resource',
                           detail={'blocked_by': [blocker['operation_id']]})
    row = hub.store.one('SELECT * FROM operations WHERE id=?', (blocker['operation_id'],))
    hub.complete(row, {'ok': True, 'data': {'content': 'done'}})
    trace = hub.diagnostics.trace({'operation_id': target['operation_id'], 'after_event_id': 0, 'limit': 100}, principal)
    assert any(event['blocked_by'] == [{'operation_id': blocker['operation_id'], 'tool': 'fs_read',
                                      'state': 'succeeded'}] for event in trace['events'])
    # A different grant must not gain the historical blocker identity.
    outsider = replace(principal, admin=False, grant_id='other-grant', projects=[])
    with pytest.raises(DevError):
        hub.diagnostics.trace({'operation_id': target['operation_id'], 'after_event_id': 0, 'limit': 100}, outsider)


def test_macos_case_aliases_share_claims_including_missing_files(local_agent, monkeypatch):
    agent, root = local_agent
    monkeypatch.setattr('agent.resource_queue.sys.platform', 'darwin')
    one = claims_for(agent.engine, 'write', project(root), {'path': 'Folder/New.txt'}, root)[0]
    two = claims_for(agent.engine, 'exec', project(root),
                     {'cwd': '.', 'resources': [{'kind': 'path', 'name': 'folder', 'mode': 'write'}]}, root)[0]
    assert one.conflicts(two)
    edits = claims_for(agent.engine, 'edit', project(root),
                       {'changes': [{'path': 'FOLDER/NEW.TXT', 'destination': 'Moved.txt'}]}, root)
    assert edits[0].conflicts(one)
    assert canonical_path(root / 'Distinct.txt') == canonical_path(root / 'distinct.txt')


def test_dns_timeout_has_bounded_outstanding_workers(monkeypatch):
    release = threading.Event()
    slots = threading.BoundedSemaphore(2)
    entered = []
    monkeypatch.setattr(incoming, '_DNS_SLOTS', slots)

    def blocked(*args, **kwargs):
        entered.append(threading.current_thread())
        assert release.wait(3)
        return []

    monkeypatch.setattr(incoming.socket, 'getaddrinfo', blocked)
    try:
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(DevError) as error:
                incoming.resolve_addresses('files.oaiusercontent.com', 443, started + .05)
            assert error.value.code == 'ARTIFACT_TIMEOUT'
            assert error.value.details['request_sent'] is False
            assert time.monotonic() - started < 1
        assert len(entered) == 2
        assert all(thread.daemon for thread in entered)
    finally:
        release.set()
        for thread in entered:
            thread.join(1)
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    slots.release()
    slots.release()


@pytest.mark.asyncio
async def test_import_diagnostics_survive_agent_replay_and_hub_receipt(local_agent, runtime, monkeypatch):
    agent, root = local_agent
    hub, principal = runtime
    call = download(root)
    secret = 'DO_NOT_LEAK_SOURCE_TICKET'

    def fail(*args, providers=()):
        assert not providers
        raise DevError('ARTIFACT_DOWNLOAD_FAILED', 'source rejected', 502,
                       source_host='files.oaiusercontent.com', source_scheme='https',
                       reason='source_access_or_expiry', stage='response', http_status=403,
                       request_sent=True, recovery='refresh_native_file',
                       download_url='https://files.oaiusercontent.com/private?sig=' + secret,
                       file_id=secret, unexpected=secret)

    monkeypatch.setattr(incoming, 'download_chunks', fail)
    await agent.handle(call)
    first = agent.journal.status(call['id'])['result']
    await agent.handle(call)
    assert agent.journal.status(call['id'])['result'] == first
    assert first['error']['recovery'] == 'refresh_native_file'
    assert first['error']['http_status'] == 403
    # The public v1.22 entry requires the online authenticated relay; it must
    # never silently fall back to a legacy Agent download when unavailable.
    with pytest.raises(DevError) as offline:
        await hub.invoke('download_artifact', {**call['args'], 'project': 'ProjectAlpha'}, principal)
    assert offline.value.code == 'FILE_IMPORT_DEVICE_OFFLINE'
    # Seed an already-accepted legacy receipt in this disposable Hub to verify
    # historical Agent journal diagnostics still survive upgrade and recovery.
    receipt = await hub.dispatch('download_artifact', call['args'],
                                 hub.project('ProjectAlpha', principal), principal)
    row = hub.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    hub.complete(row, first)
    recovered = hub.operation(receipt['operation_id'], principal)
    assert recovered['result']['error']['request_sent'] is True
    assert recovered['result']['error']['source_host'] == 'files.oaiusercontent.com'
    assert secret not in json.dumps(recovered)
    assert not (root / call['args']['path']).exists()


@pytest.mark.parametrize('detail', [
    {'reason': ['dns_failed'], 'http_status': True, 'request_sent': 'false'},
    {'source_host': 'https://host.invalid/private?secret=yes', 'recovery': 'secret-value',
     'stage': 'private/path', 'source_scheme': 'x' * 100},
])
def test_import_diagnostic_whitelist_rejects_raw_unbounded_details(detail):
    assert safe_import_error_detail(detail) == {}



@pytest.mark.parametrize('status', ['missing', 'accepted', 'retryable', 'running', 'done', 'finishing'])
def test_revocation_fence_is_atomic_and_never_kills_started_work(local_agent, status):
    from unittest.mock import Mock
    agent, root = local_agent
    call = request(root, 'read', path='file')
    identifier = call['id']
    if status != 'missing':
        agent.journal.start(identifier, {key: call[key] for key in ('tool', 'project', 'args')})
    if status == 'running':
        agent.journal.mark_running(identifier)
    elif status == 'done':
        agent.journal.finish(identifier, {'ok': True, 'data': {'content': 'preserved'}})
    elif status == 'retryable':
        with agent.journal.lock, agent.journal.db:
            agent.journal.db.execute("UPDATE calls SET status='retryable' WHERE id=?", (identifier,))
    elif status == 'finishing':
        agent.finishing.add(identifier)
    job = Mock()
    if status != 'missing':
        agent.jobs[identifier] = job
    try:
        cancelled = agent.cancel_pending_call(identifier)
        assert cancelled is (status in {'missing', 'accepted', 'retryable'})
        assert agent.journal.is_cancelled(identifier) is cancelled
        assert job.cancel.call_count == (1 if status == 'accepted' else 0)
        if status == 'done':
            assert agent.journal.status(identifier)['result']['data']['content'] == 'preserved'
    finally:
        agent.jobs.pop(identifier, None)
        agent.finishing.discard(identifier)


@pytest.mark.asyncio
@pytest.mark.parametrize('capability', [0, 1])
@pytest.mark.parametrize('accepted_ack', [False, True])
async def test_revoked_ambiguous_delivery_fences_only_capable_agent(runtime, capability, accepted_ack):
    hub, principal = runtime
    receipt = await hub.invoke('fs_read', {'project': 'ProjectAlpha', 'path': 'file'}, principal)
    identifier = receipt['operation_id']
    hub.store.execute('UPDATE operations SET attempts=1,accepted_at=? WHERE id=?',
                      (time.time() if accepted_ack else None, identifier))
    hub.store.execute("UPDATE projects SET root='/tmp/revoked-mapping' WHERE id='proj'")
    peer = Peer(capability)
    hub.connections['dev'] = peer
    await hub.deliver(identifier)
    expected = ['cancel_pending', 'probe'] if capability else ['probe']
    assert [packet['type'] for packet in peer.packets] == expected
    assert not hub.operation(identifier, principal)['cancel_requested']
    # Reconnection repeats the conditional fence and probe, never a call/kill.
    replacement = Peer(capability)
    hub.connections['dev'] = replacement
    await hub.deliver(identifier)
    assert [packet['type'] for packet in replacement.packets] == expected
    # A started job is allowed to deliver its real terminal result.
    row = hub.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    hub.complete(row, {'ok': True, 'data': {'content': 'already ran'}})
    assert hub.operation(identifier, principal)['state'] == 'succeeded'


@pytest.mark.asyncio
async def test_revocation_before_delivery_never_sends_conditional_or_real_call(runtime):
    hub, principal = runtime
    receipt = await hub.invoke('fs_read', {'project': 'ProjectAlpha', 'path': 'file'}, principal)
    hub.store.execute("UPDATE projects SET root='/tmp/revoked-mapping' WHERE id='proj'")
    peer = Peer(1)
    hub.connections['dev'] = peer
    await hub.deliver(receipt['operation_id'])
    assert peer.packets == []
    assert hub.operation(receipt['operation_id'], principal)['result']['error']['code'] == 'AUTHORIZATION_CHANGED'


@pytest.mark.asyncio
async def test_revocation_fence_survives_missing_call_and_duplicate_delivery(local_agent):
    agent, root = local_agent
    call = request(root, 'write', path='never-written', content='private',
                   expected_sha256='new', idempotency_key=uuid.uuid4().hex)
    assert agent.cancel_pending_call(call['id'])
    await agent.handle(call)
    first = agent.journal.status(call['id'])['result']
    await agent.handle(call)
    assert first['error']['code'] == 'CANCELLED'
    assert agent.journal.status(call['id'])['result'] == first
    assert not (root / 'never-written').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_path', ['.env', '../outside.txt', 'linked.txt', 'missing.txt'])
async def test_batch_read_preserves_per_path_denial(local_agent, bad_path):
    agent, root = local_agent
    (root / 'ok.txt').write_text('visible')
    (root / '.env').write_text('must remain private')
    (root.parent / 'outside.txt').write_text('outside private')
    (root / 'linked.txt').symlink_to(root.parent / 'outside.txt')
    call = request(root, 'fs_read_many', paths=['ok.txt', bad_path])
    await agent.handle(call)
    result = agent.journal.status(call['id'])['result']
    assert result['ok'], result
    items = result['data']['files']
    assert items[0]['ok'] and items[0]['content'] == 'visible'
    assert not items[1]['ok'] and items[1]['path'] == bad_path
    assert 'private' not in json.dumps(result)
    assert result['data']['remaining_paths'] == []
    assert not result['data']['truncated']


@pytest.mark.asyncio
async def test_batch_read_recovered_path_remains_coordinated(local_agent):
    agent, root = local_agent
    (root / 'ok.txt').write_text('visible')
    changing = root / 'changing.txt'
    changing.symlink_to(root / 'ok.txt')
    call = request(root, 'fs_read_many', paths=['ok.txt', 'changing.txt'])
    job = None
    try:
        async with agent.resources.slot('path-writer',
                [Claim('agent', 'path', canonical_path(root) + '/changing.txt', True)], lambda _: None):
            job = asyncio.create_task(agent.handle(call))
            await eventually(lambda: waiting(agent, call))
            changing.unlink()
            changing.write_text('recovered')
            assert not job.done()
        await asyncio.wait_for(job, 2)
        result = agent.journal.status(call['id'])['result']
        assert result['ok'], result
        assert [item['content'] for item in result['data']['files']] == ['visible', 'recovered']
    finally:
        if job:
            await asyncio.wait_for(job, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('paths', [[], ['ok.txt', 1], 'ok.txt'])
async def test_batch_read_invalid_argument_shape_still_rejected(local_agent, paths):
    agent, root = local_agent
    (root / 'ok.txt').write_text('visible')
    call = request(root, 'fs_read_many', paths=paths)
    await agent.handle(call)
    result = agent.journal.status(call['id'])['result']
    assert not result['ok']
    assert 'data' not in result


def test_batch_read_path_io_error_uses_conservative_claim(local_agent, monkeypatch):
    agent, root = local_agent

    def transient_error(*args):
        raise OSError('transient metadata failure')

    monkeypatch.setattr(agent.engine, 'path', transient_error)
    assert claims_for(agent.engine, 'fs_read_many', project(root),
                      {'paths': ['ok.txt', 'changing.txt']}, root) == [
        Claim('agent', 'path', canonical_path(root), False)]
    with pytest.raises(OSError):
        claims_for(agent.engine, 'fs_read', project(root), {'path': 'ok.txt'}, root)
