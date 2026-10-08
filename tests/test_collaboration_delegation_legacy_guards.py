"""Authenticated panel guards prevent delegated sources entering legacy routes."""
import json

from tests.collaboration_support import key
from tests.test_collaboration_delegation_bridge import bridge, create_policy, message_args, panel  # noqa: F401


def test_delegated_source_cannot_create_a_second_legacy_task(bridge):
    b = bridge
    policy = create_policy(b)
    sent = panel(b, 'message', message_args(b, policy=policy))
    response = b.client.post('/api/collaboration/message-to-task', json={
        **b.scope, 'room_id': b.room['id'], 'conversation_id': b.room['id'],
        'message_id': sent['message']['id'], 'expected_message_version': sent['message']['version'],
        'assignee_agent_id': 'unused-legacy-agent', 'kind': 'analyze_incident',
        'request': 'Do not duplicate this delegated task', 'acceptance': 'Original task only',
        'idempotency_key': key()})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'MESSAGE_ALREADY_DELEGATED'
    store = b.app.state.store
    assert not store.all('SELECT * FROM collaboration_jobs')
    assert not store.all('SELECT * FROM collaboration_goals')
    assert len(store.all('SELECT * FROM coordination_goals')) == 1
    assert len(store.all('SELECT * FROM coordination_work')) == 1


def test_delegated_source_uses_only_delegation_remind_even_without_display_metadata(bridge):
    b = bridge
    policy = create_policy(b)
    sent = panel(b, 'message', message_args(b, policy=policy))
    store = b.app.state.store
    before = len(store.all('SELECT * FROM mcp_event_outbox'))
    # A stale or missing display field cannot remove the authoritative linkage.
    row = store.one('SELECT * FROM collaboration_messages WHERE id=?', (sent['message']['id'],))
    body = json.loads(row['body'])
    body.pop('delegation')
    store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (json.dumps(body), row['id']))
    response = b.client.post('/api/collaboration/message-remind', json={
        **b.scope, 'room_id': b.room['id'], 'conversation_id': b.room['id'],
        'message_id': row['id'], 'expected_message_version': row['version'],
        'slot_ids': [b.slot['id']], 'idempotency_key': key()})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'MESSAGE_KIND_INVALID'
    assert 'delegation-remind' in response.json()['error']['message']
    assert len(store.all('SELECT * FROM mcp_event_outbox')) == before
    assert len(store.all('SELECT * FROM coordination_work')) == 1
