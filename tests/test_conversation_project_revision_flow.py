"""Real loopback Hub/MCP/Agent admission and replies for room revisions.

The filesystem and identities are disposable. Only subscription verification uses
an injected Receiver; later callbacks are held in the synthetic outbox so the
standalone Hub cannot contact an external service. No browser is used.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import EventService
from hub.collaboration.service import CollaborationService
from hub.runtime import Runtime
from hub.store import Store
from shared.util import atomic_json
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_events import Receiver
from tests.test_mcp_tasks_http import modern
from tests.support import wait_for

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def mcp(stack, name, arguments, *, ok=True):
    response = modern(stack, 'tools/call', {'name': name, 'arguments': arguments}, token=stack.pat)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert 'error' not in envelope, envelope
    result = envelope['result']
    assert bool(result.get('isError')) is not ok, result
    return result.get('structuredContent', result)


def call(stack, request):
    return mcp(stack, request['tool'], request['arguments'])


def panel(stack, action, raw):
    return stack.must(stack.client.post('/api/collaboration/' + action, json=raw))


def rows(stack, query, parameters=()):
    store = Store(stack.hubdir)
    try:
        return store.all(query, parameters)
    finally:
        store.close()


def stable_state(stack):
    return {
        'grants': rows(stack, 'SELECT * FROM grants ORDER BY id'),
        'dots': rows(stack, 'SELECT * FROM collaboration_dots ORDER BY id'),
        'routes': rows(stack, 'SELECT * FROM collaboration_join_routes ORDER BY rowid'),
        'subscriptions': rows(stack, """SELECT id,name,arguments,grant_id,room_id,state,
            version,expires_at,principal FROM mcp_event_subscriptions ORDER BY id"""),
    }


def subscribe_without_external_delivery(stack, joined):
    store = Store(stack.hubdir)
    try:
        service = CollaborationService(Runtime(store), CollaborationConfig(enabled=True, events_enabled=True))
        receiver = Receiver()
        service.events = events = EventService(service, receiver)
        dot = service.dots.find(joined['slot']['id'])
        room = store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
        actor = service.grant_reader(room, stack.grant)
        asyncio.run(events.subscribe({
            **joined['subscription_request'],
            'delivery': {'mode': 'webhook', 'url': 'https://revision-flow.example.invalid/callback',
                         'secret': 'whsec_' + base64.b64encode(b'revision-flow-synthetic-key-32bit!').decode()},
        }, actor))
        assert len(receiver.requests) == 1  # Synthetic verification challenge only.
        # The real Hub may queue events, but this disposable fixture never sends
        # callback traffic. This does not alter the active subscription or grant.
        store.execute("""CREATE TRIGGER revision_flow_hold_callbacks
            AFTER INSERT ON mcp_event_deliveries BEGIN
            UPDATE mcp_event_deliveries SET next_at=NEW.created+86400 WHERE id=NEW.id;
            END""")
    finally:
        store.close()


def revise(stack, joined, ids):
    raw = {'conversation_id': joined['slot']['conversation_id'], 'dot_id': joined['slot']['id'],
           'projects': [{'project': identifier, 'environment_id': 'production'} for identifier in ids]}
    preview = panel(stack, 'conversation-projects-preview', raw)
    assert preview['ready'], preview
    return panel(stack, 'conversation-projects', {
        **raw, 'expected_version': preview['conversation_version'],
        'expected_scope_version': preview['scope_version'],
        'selection_digest': preview['selection_digest'], 'confirm_scope': True,
        'idempotency_key': key(),
    })


def message(stack, joined, scope, body, *, reply_to_id=''):
    return panel(stack, 'message', {
        **scope, 'room_id': joined['slot']['scope_manifest']['room_id'],
        'conversation_id': joined['slot']['conversation_id'], 'body_text': body,
        'mentions': [{'slot_id': joined['slot']['id']}], 'reply_to_id': reply_to_id,
        'client_message_id': key(), 'idempotency_key': key(),
    })


def test_real_agent_uses_revised_project_and_original_dot_thread_then_chat_only(collaboration_stack):
    stack = collaboration_stack
    anchor, selected = stack.project, stack.projects[1]
    scope = {'project': anchor['id'], 'environment_id': 'production'}
    stack.stop_agent()
    stack.config['shell'] = {
        'enabled': True, 'projects': [anchor['alias'], selected['alias']], 'command': ['/bin/sh', '-c'],
    }
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    panel(stack, 'room', {**scope, 'idempotency_key': key()})
    dot = panel(stack, 'dot', {
        **scope, 'label': 'Room revision integration dot', 'duplex': True,
        'capabilities': ['read', 'write', 'execute'], 'acknowledge_unsandboxed_exec': True,
        'confirm_tasks': True, 'idempotency_key': key(),
    })['dot']
    joined = mcp(stack, 'collaboration', {
        'action': 'join', 'code': dot['join_code'], 'idempotency_key': key(),
    })
    subscribe_without_external_delivery(stack, joined)
    assert len(rows(stack, 'SELECT * FROM conversation_rooms')) == 1
    assert not rows(stack, 'SELECT * FROM collaboration_rooms WHERE project_id=?', (selected['id'],))
    before = stable_state(stack)
    changed = revise(stack, joined, [selected['id']])
    assert [p['project_id'] for p in changed['conversation']['projects']] == [selected['id']]
    assert len(rows(stack, 'SELECT * FROM conversation_rooms')) == 1
    assert len(rows(stack, 'SELECT * FROM collaboration_rooms WHERE project_id=?', (selected['id'],))) == 1
    assert stable_state(stack) == before

    # Exercise real startup/migration, not just a manually re-run migration.
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.start_hub()
    wait_for(lambda: any(device['id'] == stack.device and device['online']
                         for device in stack.client.get('/api/devices').json().get('devices', [])), timeout=20)
    restored = mcp(stack, 'collaboration_query', {
        'action': 'dot_connection', **scope, 'dot_id': dot['id'],
    })
    assert restored['inbox_request'] == joined['inbox_request']
    assert restored['subscription_requests'] == joined['subscription_requests']
    assert len(rows(stack, 'SELECT * FROM conversation_rooms')) == 1
    assert stable_state(stack) == before

    source = message(stack, joined, scope, 'Write and verify only the selected synthetic project.')
    item = next(row for row in call(stack, joined['inbox_request'])['items']
                if row['message_id'] == source['message']['id'])
    task = call(stack, item['task_request'])
    assert call(stack, item['task_request'])['delegation_id'] == task['delegation_id']
    work_page = call(stack, restored['work_inbox_request'])
    pending = next(row for row in work_page['items'] if row['delegation_id'] == task['delegation_id'])
    evidence = call(stack, pending['read_request'])
    assert evidence['trusted_author']['authenticated']
    assert evidence['goal']['project_ids'] == [selected['id']]
    claimed = call(stack, pending['claim_request'])['work_item']
    assert claimed['project_id'] == selected['id']
    lease = {**scope, 'goal_id': claimed['goal_id'], 'work_item_id': claimed['id'],
             'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token']}

    count_before_denial = len(rows(stack, 'SELECT * FROM operations'))
    denied = mcp(stack, 'collaboration_work', {
        'action': 'execute', **lease, 'tool': 'exec',
        'arguments': {'project': anchor['id'], 'command': 'printf must-not-run', 'target': 'agent'},
        'idempotency_key': key(),
    }, ok=False)
    assert denied
    assert len(rows(stack, 'SELECT * FROM operations')) == count_before_denial

    filename, expected = 'room-revision-proof.txt', 'synthetic-other-project-only\n'
    receipts = []
    for tool, arguments in [
        ('write', {'path': filename, 'content': expected, 'expected_sha256': 'new'}),
        ('read', {'path': filename}),
        ('exec', {'command': 'cat ' + filename, 'target': 'agent', 'timeout_seconds': 15}),
    ]:
        admitted = mcp(stack, 'collaboration_work', {
            'action': 'execute', **lease, 'tool': tool, 'arguments': arguments, 'idempotency_key': key(),
        })
        receipt = stack.poll(admitted['operation_id'], timeout=30)
        assert receipt['state'] == 'succeeded', receipt
        assert receipt['project_id'] == selected['id']
        if tool == 'exec':
            assert receipt['result']['data']['exit_code'] == 0
            assert expected.strip() in receipt['output']
        receipts.append(admitted['operation_id'])
    assert (Path(selected['root']) / filename).read_text() == expected
    assert not (Path(anchor['root']) / filename).exists()
    mcp(stack, 'collaboration_work', {
        'action': 'result', **lease, 'outcome': 'succeeded',
        'summary': 'The selected fixture project was written, read and checked by its Agent.',
        'operation_ids': receipts, 'idempotency_key': key(),
    })
    overview = stack.must(stack.client.get('/api/collaboration', params={
        **scope, 'conversation_id': joined['slot']['conversation_id'],
    }))
    finals = [row for row in overview['messages'] if row['kind'] == 'delegation_result'
              and row.get('reply_to_id') == source['message']['id']]
    assert len(finals) == 1 and finals[0]['body']['execution_verified']
    assert finals[0]['conversation_id'] == joined['slot']['conversation_id']

    chat = revise(stack, joined, [])
    assert chat['conversation']['projects'] == []
    empty = mcp(stack, 'collaboration_query', {
        'action': 'dot_connection', **scope, 'dot_id': dot['id'],
    })
    assert empty['consumer_configuration']['scope_manifest']['chat_only']
    assert empty['inbox_request'] == joined['inbox_request']
    assert empty['subscription_requests'] == joined['subscription_requests']
    followup = message(stack, joined, scope, 'Now only explain; do not create more work.',
                       reply_to_id=finals[0]['id'])
    item = next(row for row in call(stack, joined['inbox_request'])['items']
                if row['message_id'] == followup['message']['id'])
    assert item['task_request'] is None
    answer = mcp(stack, 'collaboration', {
        'action': 'dot_message', **item['reply_arguments'],
        'body_text': 'The result is in the original thread. This room is now chat-only.',
    })
    assert answer['message']['thread_root_id'] == source['message']['thread_root_id']
    assert stable_state(stack) == before
    assert len(rows(stack, 'SELECT * FROM conversation_rooms')) == 1
    assert len(rows(stack, 'SELECT * FROM delegation_requests')) == 1
    deliveries = rows(stack, 'SELECT attempt FROM mcp_event_deliveries')
    assert all(row['attempt'] == 0 for row in deliveries)
