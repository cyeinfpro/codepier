"""Receipt-only fixtures: no listener, real project, or desktop control."""
import asyncio
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.integration_local import LocalServer
from agent.journal import Journal
from shared.util import DevError

BODY = dict(project='fixture', action='pause', confirm='fixture',
            idempotency_key='fixture-local-control-key')
IDENTIFIER = hashlib.sha256(('local-control:' + BODY['idempotency_key']).encode()).hexdigest()[:32]


@pytest.fixture
def local(tmp_path):
    journal = Journal.__new__(Journal)
    journal.lock = threading.RLock()
    journal.db = sqlite3.connect(':memory:')
    journal.db.row_factory = sqlite3.Row
    journal.db.execute("""CREATE TABLE calls (id TEXT PRIMARY KEY, fingerprint TEXT,
        status TEXT, result TEXT, acked INTEGER DEFAULT 0, at REAL, tool TEXT,
        output TEXT DEFAULT '', output_seq INTEGER DEFAULT 0)""")
    project = dict(id='fixture-project', alias='fixture', device_id='fixture-device', root='/fixture')
    action = AsyncMock(return_value={'paused': True})
    agent = SimpleNamespace(state_dir=tmp_path, config={'integrations': {'local_control': True}},
        journal=journal, engine=SimpleNamespace(root=lambda project: (None, None)),
        integrations=SimpleNamespace(known_projects=lambda: [project],
                                     control=SimpleNamespace(action=action)))
    try:
        yield LocalServer(agent, None)
    finally:
        journal.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [None, DevError('FIXTURE_REJECTED', 'expected rejection'),
                                  OSError('private-storage-path'), ValueError('private-payload'),
                                  RuntimeError('private-provider-detail')])
async def test_control_terminal_receipt_replays_without_executing_again(local, error):
    action = local.agent.integrations.control.action
    action.side_effect = error
    first = await local.route('/control', BODY)
    again = await local.route('/control', BODY)
    assert again == first
    assert first['ok'] is (error is None)
    if isinstance(error, DevError):
        assert first['error']['code'] == error.code
    elif error is not None:
        assert first['error']['code'] == 'LOCAL_ERROR'
        assert str(error) not in json.dumps(first)
    assert action.await_count == 1
    assert local.agent.journal.status(IDENTIFIER)['status'] == 'done'
    assert local.agent.journal.db.execute('SELECT acked FROM calls').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_control_cancel_persists_uncertainty_and_does_not_replay(local):
    started = asyncio.Event()
    async def interrupted(*args):
        started.set()
        await asyncio.Event().wait()
    action = local.agent.integrations.control.action
    action.side_effect = interrupted
    task = asyncio.create_task(local.route('/control', BODY))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = local.agent.journal.status(IDENTIFIER)
    assert status['status'] == 'done'
    assert status['result']['ok'] is False
    assert status['result']['error']['code'] == 'INTERRUPTED'
    assert await local.route('/control', BODY) == status['result']
    assert action.await_count == 1


@pytest.mark.asyncio
async def test_failed_receipt_write_never_reports_success_or_repeats_action(local, monkeypatch):
    journal = local.agent.journal
    ack = Mock()
    monkeypatch.setattr(journal, 'ack', ack)
    monkeypatch.setattr(journal, 'finish', Mock(side_effect=OSError('fixture-disk-full')))
    with pytest.raises(OSError):
        await local.route('/control', BODY)
    assert journal.status(IDENTIFIER)['status'] == 'running'
    assert not ack.called
    with pytest.raises(DevError) as caught:
        await local.route('/control', BODY)
    assert caught.value.code == 'ALREADY_RUNNING'
    assert local.agent.integrations.control.action.await_count == 1


def http_fixture(local):
    body = json.dumps(BODY).encode()
    reader = asyncio.StreamReader()
    reader.feed_data((f'POST /control HTTP/1.1\r\nHost: 127.0.0.1:{local.port}\r\n'
                     f'Authorization: Bearer {local.token}\r\nContent-Type: application/json\r\n'
                     f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
    reader.feed_eof()
    writes = []
    writer = SimpleNamespace(get_extra_info=lambda key: ('127.0.0.1', 1),
        write=writes.append, drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    return reader, writer, writes


@pytest.mark.asyncio
async def test_receipt_write_error_is_generic_http_failure(local, monkeypatch):
    monkeypatch.setattr(local.agent.journal, 'finish', Mock(side_effect=OSError('private-fixture-path')))
    reader, writer, writes = http_fixture(local)
    await local.client(reader, writer)
    raw = b''.join(writes)
    assert raw.startswith(b'HTTP/1.1 500 ')
    assert b'LOCAL_ERROR' in raw and b'private-fixture-path' not in raw
    assert writer.close.called and not local.tasks
    assert local.agent.journal.status(IDENTIFIER)['status'] == 'running'
    assert local.agent.integrations.control.action.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_http_control_closes_connection_and_task_tracking(local):
    started = asyncio.Event()
    async def interrupted(*args):
        started.set()
        await asyncio.Event().wait()
    local.agent.integrations.control.action.side_effect = interrupted
    reader, writer, writes = http_fixture(local)
    task = asyncio.create_task(local.client(reader, writer))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.close.called and task not in local.tasks
    assert not writes
    assert local.agent.journal.status(IDENTIFIER)['result']['error']['code'] == 'INTERRUPTED'
