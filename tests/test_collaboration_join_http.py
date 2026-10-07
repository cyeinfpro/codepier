"""Real HTTP/modern MCP enrollment uses existing grants and panel CSRF."""
import pytest

from hub.collaboration.common import EVENTS, MESSAGE_EVENT, WORK_EVENT
from tests.collaboration_support import collaboration_stack
from tests.collaboration_support import key
from tests.test_mcp_tasks_http import modern


def test_panel_to_mcp_join_flow_preserves_authority_and_scope(collaboration_stack):
    s = collaboration_stack
    scope = {'project': s.project['id'], 'environment_id': 'production'}
    s.must(s.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))
    request = {**scope, 'label': 'dot enrollment fixture', 'kind': 'dot', 'idempotency_key': key()}
    assert s.client.post('/api/collaboration/join-slot', json=request, headers={'X-RD-CSRF': 'wrong'}).status_code == 403
    item = s.must(s.client.post('/api/collaboration/join-slot', json=request))['slot']
    grant = s.must(s.client.post('/api/grants', json={'label': 'Existing synthetic connection',
                   'scopes': ['read', 'write', 'execute'], 'projects': [s.project['id']], 'days': 1}))
    catalog = modern(s, 'tools/list', token=grant['token']).json()['result']['tools']
    definition = next(tool for tool in catalog if tool['name'] == 'collaboration_join')
    assert definition['annotations']['readOnlyHint'] is False
    args = {'code': item['join_code'], 'idempotency_key': key()}
    joined = modern(s, 'tools/call', {'name': 'collaboration_join', 'arguments': args}, token=grant['token']).json()['result']
    assert not joined['isError']
    body = joined['structuredContent']
    from shared.collaboration_contracts import JoinResult
    JoinResult.model_validate(body)
    assert body['registered'] and body['slot']['status'] == 'waiting_subscription'
    assert not body['worker_authorized'] and not body['permissions_changed']
    assert len(body['subscription_requests']) == 4
    repeated = modern(s, 'tools/call', {'name': 'collaboration_join', 'arguments': args}, token=grant['token']).json()['result']
    assert repeated['structuredContent'] == body
    overview = s.must(s.client.get('/api/collaboration', params=scope))
    assert overview['agents'] == [] and overview['jobs'] == []
    assert overview['join_slots'][0]['id'] == item['id']
    assert not overview['features']['collector_enabled'] and not overview['features']['analysis_dispatch_enabled']
    read = modern(s, 'tools/call', {'name': 'collaboration_read', 'arguments': {**scope, 'kind': 'join_slots'}},
                  token=grant['token']).json()['result']['structuredContent']
    assert read['items'][0]['join_code'] is None
    forged = modern(s, 'tools/call', {'name': 'collaboration_join',
                    'arguments': {**args, 'chat_identity_verified': True}}, token=grant['token']).json()['result']
    assert forged['isError']
    denied_test = s.client.post('/api/collaboration/join-slot-control', json={**scope,
        'slot_id': item['id'], 'expected_version': overview['join_slots'][0]['version'],
        'action': 'test', 'idempotency_key': key()})
    assert denied_test.status_code == 409


def test_event_discovery_advertises_slot_filter_without_assuming_host_binding(collaboration_stack):
    s = collaboration_stack
    grant = s.must(s.client.post('/api/grants', json={'label': 'Discovery fixture', 'scopes': ['read'],
                   'projects': [s.project['id']], 'days': 1}))
    catalog = modern(s, 'events/list', token=grant['token']).json()['result']
    assert {event['name'] for event in catalog['events']} == set(EVENTS)
    for event in catalog['events']:
        schema = event['inputSchema']
        required = set(schema.get('required', []))
        if event['name'] == WORK_EVENT:
            assert set(schema['properties']) == {'project_id', 'environment_id', 'conversation_id', 'goal_id', 'approval_id'}
            assert required == set(schema['properties'])
        else:
            assert 'slot_id' in schema['properties']
            if event['name'] == MESSAGE_EVENT:
                assert {'slot_id', 'conversation_id'} <= required
            else:
                assert 'slot_id' not in required
    assert s.must(s.client.get('/api/collaboration', params={'project': s.project['id']}))['room'] is None
