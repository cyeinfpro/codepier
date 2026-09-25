"""Approval invalidation events disclose metadata only and preserve one decision."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.util import DevError


@pytest.fixture
def inbox(tmp_path):
    store = Store(tmp_path / 'hub')
    runtime = Runtime(store)
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',
                  ('d', 'Device', store.encrypt('fixture'), time.time()))
    store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,created) VALUES (?,?,?,?,?,?)',
                  ('p', 'Test', 'test', 'd', '/fixture', time.time()))
    request = {'project': {'id': 'p', 'alias': 'Test', 'root': '/fixture'}}
    store.execute('INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  ('op', 'd', 'p', 'panel:admin', 'computer_observe', '{}', 'f', 'running', time.time(), time.time(), store.encrypt(json.dumps(request))))
    sent = []

    async def send(data):
        sent.append(data)

    connection = SimpleNamespace(unusable=False, device_secret=None, send=send)
    runtime.connections['d'] = connection
    events = asyncio.Queue()
    runtime.watchers.add(events)
    data = {'type': 'computer_approval', 'request_id': 'a' * 32, 'session_id': 'b' * 32,
            'project_id': 'p', 'owner': 'panel:admin', 'app': 'Private App', 'operation_id': 'op',
            'message': 'PRIVATE NATIVE MESSAGE', 'expires_at': time.time() + 40}
    yield runtime.computer_approvals, connection, data, events, sent
    store.close()


def changed(events):
    event = events.get_nowait()
    assert event['type'] == 'computer_approval'
    assert event['data'] == {'changed': True}
    assert set(event) == {'type', 'at', 'data'}
    assert events.empty()


@pytest.mark.parametrize('removal', ['closed', 'drop', 'expired', 'revoked'])
def test_inbox_changes_publish_no_native_content(inbox, removal):
    approvals, connection, data, events, _ = inbox
    approvals.receive('d', connection, data)
    changed(events)
    approvals.receive('d', connection, data)
    assert events.empty()  # Duplicate and unrelated messages are not changes.
    if removal == 'closed':
        approvals.receive('d', object(), {'type': 'computer_approval_closed', 'request_id': data['request_id']})
        assert events.empty()
        approvals.receive('d', connection, {'type': 'computer_approval_closed', 'request_id': data['request_id']})
    elif removal == 'drop':
        approvals.drop(connection)
    elif removal == 'expired':
        approvals.pending[data['request_id']]['expires_at'] = time.time() - 1
        assert approvals.list() == []
    else:
        approvals.runtime.store.execute("UPDATE operations SET cancel_requested=1 WHERE id='op'")
        assert approvals.list() == []
    changed(events)
    assert approvals.list() == [] and events.empty()


@pytest.mark.asyncio
async def test_decision_publishes_before_send_and_rejects_second_tab(inbox):
    approvals, connection, data, events, sent = inbox
    approvals.receive('d', connection, data)
    changed(events)
    sending = asyncio.Event()
    release = asyncio.Event()

    async def send(packet):
        sent.append(packet)
        sending.set()
        await release.wait()

    connection.send = send
    principal = Principal('panel:admin', 'u', {'computer'}, ['*'], admin=True)
    first = asyncio.create_task(approvals.decide(data['request_id'], 'decline', principal))
    await sending.wait()
    changed(events)
    with pytest.raises(DevError) as error:
        await approvals.decide(data['request_id'], 'accept', principal)
    assert error.value.code == 'APPROVAL_EXPIRED'
    assert len(sent) == 1 and sent[0]['action'] == 'decline' and events.empty()
    release.set()
    assert (await first)['ok']


@pytest.mark.asyncio
async def test_decision_on_expired_request_publishes_removal_without_sending(inbox):
    approvals, connection, data, events, sent = inbox
    approvals.receive('d', connection, data)
    changed(events)
    approvals.pending[data['request_id']]['expires_at'] = time.time() - 1
    with pytest.raises(DevError) as error:
        await approvals.decide(data['request_id'], 'accept', Principal('panel:admin', 'u', {'computer'}, ['*'], admin=True))
    assert error.value.code == 'APPROVAL_EXPIRED'
    # Worker invalidations are scheduled on the SSE queue's owning loop.
    # Wait for that event, not an arbitrary delay or a test retry.
    event = await asyncio.wait_for(events.get(), 2)
    assert event['type'] == 'computer_approval'
    assert event['data'] == {'changed': True}
    assert set(event) == {'type', 'at', 'data'}
    assert events.empty() and not sent
