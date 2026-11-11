"""Synthetic regressions for S09 and S12: no network or real credentials."""
import asyncio
import os
import signal
import sys
from types import SimpleNamespace

import pytest

import agent.runner as runner
from agent.journal import Journal
from agent.runner import Agent
from shared.crypto import token
from docs.examples.dot_outbox import DotOutbox
from shared.util import DevError, atomic_json


@pytest.fixture
def shutdown_agent(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    path = tmp_path / 'config.json'
    atomic_json(path, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture', 'secret': token(),
                      'state_dir': str(tmp_path / 'state'),
                      'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}]})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


def write_request(root):
    return {'id': 'queued-write', 'tool': 'fs_write',
            'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write'},
            'args': {'project': 'fixture', 'path': 'result.txt', 'content': 'once',
                     'expected_sha256': 'new', 'idempotency_key': 'shutdown-fixture'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False, True])
async def test_unstarted_shutdown_recovers_but_user_cancel_is_terminal(shutdown_agent, explicit):
    agent, root = shutdown_agent
    accepted = asyncio.Event()

    async def send(packet):
        if packet['type'] == 'accepted':
            accepted.set()
            await asyncio.Event().wait()

    agent.send = send
    request = write_request(root)
    task = asyncio.create_task(agent.handle(request))
    await accepted.wait()
    if explicit:
        agent.journal.cancel(request['id'])
    agent.stop_event.set()
    task.cancel()
    if explicit:
        await task
        assert agent.journal.status(request['id'])['result']['error']['code'] == 'CANCELLED'
    else:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.journal.status(request['id'])['status'] == 'accepted'
        recovered = Journal(agent.journal.directory)
        try:
            assert recovered.status(request['id'])['status'] == 'retryable'
        finally:
            recovered.db.close()
    assert not (root / 'result.txt').exists()


@pytest.mark.asyncio
async def test_shutdown_keeps_completed_process_distinct_from_live_process(shutdown_agent, monkeypatch):
    agent, _ = shutdown_agent

    async def idle(*args):
        await asyncio.Event().wait()

    async def noop(*args, **kwargs):
        pass

    for name in ('watch_config', 'output_sender', 'watch_scheduler', 'maintain_journal'):
        monkeypatch.setattr(agent, name, idle)
    monkeypatch.setattr('agent.brand_upgrade.watch', idle)
    monkeypatch.setattr('agent.coding_reviews.watch_cleanup', idle)
    monkeypatch.setattr(agent.integrations, 'start', noop)
    monkeypatch.setattr(agent.integrations, 'close', noop)
    monkeypatch.setattr(agent.computer, 'start_guard', lambda: None)
    monkeypatch.setattr(agent.computer, 'close', noop)
    monkeypatch.setattr(agent, 'kill_process', noop)
    agent.processes = {'completed': SimpleNamespace(returncode=0), 'live': SimpleNamespace(returncode=None)}
    for name in agent.processes:
        agent.journal.start(name, {'tool': 'exec'})
        agent.journal.mark_running(name)
        agent.jobs[name] = asyncio.create_task(noop())
    agent.stop_event.set()
    await agent.run()
    assert agent.cancelled == set()
    assert agent.shutdown_interrupted == {'live'}


@pytest.mark.asyncio
async def test_reaped_posix_pid_never_signalled_even_for_cleanup(monkeypatch):
    signals = []
    monkeypatch.setattr(runner, 'os', SimpleNamespace(name='posix', killpg=lambda *args: signals.append(args)))
    await Agent.kill_process(SimpleNamespace(returncode=0, pid=42), include_finished=True)
    assert signals == []


@pytest.mark.asyncio
async def test_legacy_posix_leader_reaped_during_term_is_not_killed_again(monkeypatch):
    signals = []
    process = SimpleNamespace(returncode=None, pid=42)

    async def wait():
        process.returncode = 0
        return 0

    process.wait = wait
    monkeypatch.setattr(runner, 'os', SimpleNamespace(name='posix', killpg=lambda *args: signals.append(args)))
    await Agent.kill_process(process)
    assert signals == [(42, signal.SIGTERM)]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == 'nt', reason='POSIX WNOWAIT ownership')
async def test_owned_process_observes_exit_without_releasing_identity(tmp_path):
    from agent.owned_subprocess import create_owned_process
    import subprocess
    process = await create_owned_process(sys.executable, '-c', "print('done')", cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert await process.wait() == 0
    assert os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    await Agent.kill_process(process, include_finished=True)
    assert process.returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    await Agent.kill_process(process, include_finished=True)


@pytest.fixture
def outbox(tmp_path):
    clock = [1000.0]
    box = DotOutbox(tmp_path / 'outbox.sqlite3', seal=lambda s: s, unseal=lambda s: s, clock=lambda: clock[0])
    identity = {key: 'synthetic' for key in ('connection_id', 'grant_id', 'space_id', 'owner_user_id', 'conversation_id')}
    entry = box.enqueue(identity, 'message', {'project': 'fixture', 'dot_id': 'a' * 32,
        'body_text': 'synthetic pending body', 'idempotency_key': 'fixture-delivery-key'})
    yield box, clock, entry
    box.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('cancel', 'HOST_INTERRUPTED'), ('timeout', 'TRANSPORT_UNCERTAIN'),
                                         ('permanent', 'DOT_CHAT_NOT_APPROVED')])
async def test_expired_outbox_claim_preserves_original_failure(outbox, kind, expected):
    box, clock, entry = outbox

    async def authorize(envelope):
        clock[0] += 100
        if kind == 'cancel':
            raise asyncio.CancelledError
        if kind == 'timeout':
            raise TimeoutError('synthetic timeout')
        raise DevError('DOT_CHAT_NOT_APPROVED', 'synthetic denied', 409)

    async def unused(envelope):
        pytest.fail('Failure must not trigger sending')

    if kind == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await box.deliver(entry['id'], authorize=authorize, lookup=unused, send=unused)
    else:
        await box.deliver(entry['id'], authorize=authorize, lookup=unused, send=unused)
    row = box.list()['items'][0]
    assert row['error_code'] == expected
    assert row['state'] == ('attention' if kind == 'permanent' else 'checking_receipt')
    assert box.db.execute('SELECT body FROM dot_outbox_entries').fetchone()[0]


@pytest.mark.asyncio
async def test_cancelled_stale_outbox_attempt_cannot_overwrite_successor(outbox):
    box, clock, entry = outbox
    successor = None

    async def authorize(envelope):
        nonlocal successor
        clock[0] += 100
        successor = box._claim(entry['id'], 45)
        box._state(successor, 'attention', error='SUCCESSOR_RESULT')
        raise asyncio.CancelledError

    async def unused(envelope):
        pytest.fail('No resend allowed')

    with pytest.raises(asyncio.CancelledError):
        await box.deliver(entry['id'], authorize=authorize, lookup=unused, send=unused)
    assert box.list()['items'][0]['error_code'] == 'SUCCESSOR_RESULT'
    assert box._current(successor)


@pytest.mark.asyncio
async def test_scheduler_close_preserves_never_started_write(shutdown_agent):
    agent, root = shutdown_agent
    request = write_request(root)
    async with agent.project_slot(root, operation_id='blocking-writer'):
        task = asyncio.create_task(agent.handle(request))
        async with asyncio.timeout(3):
            while not agent.resources.waiting:
                await asyncio.sleep(.01)
        agent.stop_event.set()
        await agent.scheduler.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.journal.status(request['id'])['status'] == 'accepted'
    assert not (root / 'result.txt').exists()


@pytest.mark.asyncio
async def test_owned_cleanup_cancellation_settles_reap_before_unlock(monkeypatch):
    import threading
    import agent.owned_subprocess as owned
    entered, release = threading.Event(), threading.Event()
    calls = []

    def cleanup(pid, **kwargs):
        calls.append(pid)
        entered.set()
        assert release.wait(3)
        return True, 0

    process = owned.OwnedProcess(SimpleNamespace(pid=42, returncode=None))
    process._observed = 0  # deterministic unreaped-child model
    monkeypatch.setattr(owned, 'stop_owned_group', cleanup)
    first = asyncio.create_task(process.stop_owned())
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(.01)
    first.cancel()
    second = asyncio.create_task(process.stop_owned())
    await asyncio.sleep(.02)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert calls == [42] and process._released
