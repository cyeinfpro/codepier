"""Real HTTP/MCP chat contract, explicit speaking permission and feature gates."""
import pytest

from tests.collaboration_support import collaboration_stack, key
from tests.support import running_stack
from tests.test_mcp_tasks_http import modern


def test_chat_tool_owner_permission_csrf_and_no_implicit_jobs(collaboration_stack):
    s = collaboration_stack
    scope = {'project': 'ProjectAlpha', 'environment_id': 'production'}
    room = s.must(s.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    status = s.must(s.client.get('/api/collaboration/status'))
    assert status['schema_version'] == 3 and status['capabilities']['plain_messages']
    grant = s.must(s.client.post('/api/grants', json={'label': 'Chat fixture', 'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
    catalog = modern(s, 'tools/list', token=grant['token']).json()['result']['tools']
    assert any(tool['name'] == 'collaboration' for tool in catalog)
    args = {**scope, 'room_id': room['id'], 'body_text': 'ordinary @Work', 'client_message_id': key(), 'idempotency_key': key()}
    assert s.client.post('/api/collaboration/message', json=args, headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    posted = s.must(s.client.post('/api/collaboration/message', json=args))
    assert posted['scheduled'] is False
    denied = modern(s, 'tools/call', {'name': 'collaboration_message_create', 'arguments': args}, token=grant['token']).json()['result']
    assert denied['isError']
    access = s.must(s.client.post('/api/collaboration/message-access', json={**scope, 'room_id': room['id'],
        'grant_id': grant['grant_id'], 'enabled': True, 'expected_version': 0, 'idempotency_key': key()}))
    assert access['version'] == 1
    reply = {**args, 'reply_to_id': posted['message']['id'], 'client_message_id': key(), 'idempotency_key': key()}
    result = modern(s, 'tools/call', {'name': 'collaboration_message_create', 'arguments': reply}, token=grant['token']).json()['result']
    assert not result['isError']
    assert result['structuredContent']['message']['author'] == grant['grant_id']
    assert result['structuredContent']['message']['thread_root_id'] == posted['message']['id']
    overview = s.must(s.client.get('/api/collaboration', params=scope))
    assert overview['schema_version'] == status['schema_version'] == 3
    assert overview['counts']['open_jobs'] == 0 and overview['goals'] == []
    timeline = s.must(s.client.get('/api/collaboration', params={**scope, 'kind': 'timeline', 'room_id': room['id']}))
    assert len(timeline['items']) == 2 and timeline['items'][0]['server_sequence'] < timeline['items'][1]['server_sequence']
    forged = modern(s, 'tools/call', {'name': 'collaboration_message_create', 'arguments': {**reply, 'author': 'owner'}}, token=grant['token']).json()['result']
    assert forged['isError']
    outside = modern(s, 'tools/call', {'name': 'collaboration_message_create', 'arguments': {**reply, 'project': 'missing'}}, token=grant['token']).json()['result']
    assert outside['isError']


def test_disabled_feature_does_not_discover_chat_tool(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'false')
    with running_stack(tmp_path / 'disabled-chat') as s:
        catalog = modern(s, 'tools/list').json()['result']['tools']
        assert all(not tool['name'].startswith('collaboration') for tool in catalog)
        assert len(catalog) == 13
        for name in ('collaboration_query', 'collaboration', 'collaboration_work',
                     'collaboration_read', 'collaboration_join', 'collaboration_work_execute'):
            result = modern(s, 'tools/call', {'name': name, 'arguments': {}}).json()
            assert result.get('error', {}).get('code') == -32602



def test_http_default_room_add_project_and_independent_new_room(collaboration_stack):
    s = collaboration_stack
    project = s.project['id']
    scope = {'project': project}
    default = s.must(s.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    first = s.must(s.client.post('/api/collaboration/message', json={**scope, 'room_id': default['id'], 'body_text': 'original',
        'client_message_id': key(), 'idempotency_key': key()}))['message']
    before = s.must(s.client.get('/api/collaboration', params=scope))['conversation']
    assert before['id'] == default['id']
    added = s.must(s.client.post('/api/collaboration/conversation-project', json={**scope, 'conversation_id': before['id'],
        'expected_version': before['version'], 'environment_id': 'staging', 'idempotency_key': key()}))['conversation']
    assert added['id'] == before['id'] and len(added['projects']) == 2
    staging = next(p for p in added['projects'] if p['environment_id'] == 'staging')
    s.must(s.client.post('/api/collaboration/message', json={**scope, 'environment_id': 'staging', 'room_id': staging['room_id'],
        'conversation_id': added['id'], 'body_text': 'added project partition', 'client_message_id': key(), 'idempotency_key': key()}))
    timeline = s.must(s.client.get('/api/collaboration', params={**scope, 'kind': 'timeline', 'conversation_id': added['id']}))
    assert len(timeline['items']) == 2 and timeline['items'][0]['id'] == first['id']
    fresh = s.must(s.client.post('/api/collaboration/conversation', json={'title': 'Separate', 'projects': [scope],
        'idempotency_key': key()}))['conversation']
    assert s.must(s.client.get('/api/collaboration', params={**scope, 'kind': 'messages', 'conversation_id': fresh['id']}))['items'] == []
    listed = s.must(s.client.get('/api/collaboration/conversations'))['items']
    assert {before['id'], fresh['id']} <= {item['id'] for item in listed}
    stale = s.client.post('/api/collaboration/conversation-project', json={**scope, 'conversation_id': before['id'],
        'expected_version': 1, 'environment_id': 'another', 'idempotency_key': key()})
    assert stale.status_code == 409
