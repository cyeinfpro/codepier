"""HTTP/CSRF and compact-MCP regression for the room project editor.

TestClient, callback receiver, stores and credentials are all isolated fixtures.
No external provider, production subscription or Agent command is contacted.
"""
import json
import time

import pytest

from tests.collaboration_support import key
from tests.test_collaboration_delegation_bridge import bridge as bridge, panel, rpc, tool


def setup_room(b):
    store = b.app.state.store
    first = store.one("SELECT * FROM projects WHERE id='project'")
    store.execute("""INSERT INTO projects
        (id,alias,alias_key,device_id,root,description,mode,allow_tasks,created)
        VALUES ('otherproject','other-fixture','other-fixture',?,?,'','write',1,?)""",
        (first['device_id'], first['root'] + '-other-isolated', time.time()))
    store.execute('UPDATE grants SET projects=? WHERE id=?',
                  (json.dumps(['project', 'otherproject']), b.grant))
    slot = panel(b, 'dot', {
        **b.scope, 'conversation_id': b.room['id'], 'label': 'Revision fixture dot',
        'duplex': True, 'capabilities': ['read'], 'confirm_tasks': True,
        'idempotency_key': key(),
    })['dot']
    joined = tool(b, 'collaboration', {
        'action': 'join', 'code': slot['join_code'], 'idempotency_key': key(),
    })
    return joined


def selection(b, joined, ids):
    return {'conversation_id': joined['slot']['conversation_id'], 'dot_id': joined['slot']['id'],
            'projects': [{'project': value, 'environment_id': 'production'} for value in ids]}


def mutation(b, joined, ids):
    raw = selection(b, joined, ids)
    preview = panel(b, 'conversation-projects-preview', raw)
    assert preview['ready'], preview
    return {**raw, 'expected_version': preview['conversation_version'],
            'expected_scope_version': preview['scope_version'],
            'selection_digest': preview['selection_digest'], 'confirm_scope': True,
            'idempotency_key': key()}


def connection(b, joined):
    return tool(b, 'collaboration_query', {
        'action': 'dot_connection', **b.scope, 'dot_id': joined['slot']['id'],
    })


def send_owner(b, joined):
    return panel(b, 'message', {
        **b.scope, 'room_id': b.room['id'], 'conversation_id': joined['slot']['conversation_id'],
        'body_text': 'Discuss this synthetic room.', 'mentions': [{'slot_id': joined['slot']['id']}],
        'client_message_id': key(), 'idempotency_key': key(),
    })


def test_both_editor_endpoints_require_owner_session_and_csrf(bridge):
    b = bridge
    joined = setup_room(b)
    args = mutation(b, joined, ['otherproject'])
    store = b.app.state.store
    for path, body in (
        ('conversation-projects-preview', selection(b, joined, ['otherproject'])),
        ('conversation-projects', args),
    ):
        response = b.client.post('/api/collaboration/' + path, json=body,
                                 headers={'X-RD-CSRF': 'invalid'})
        assert response.status_code == 403
        response = b.client.post('/api/collaboration/' + path, json=body,
            headers={'Cookie': 'rd_session=invalid', 'Authorization': 'Bearer ' + b.token})
        assert response.status_code in {401, 403}
    assert not store.all('SELECT * FROM conversation_working_scopes')
    assert store.one('SELECT COUNT(*) AS n FROM conversation_projects WHERE conversation_id=?',
                     (joined['slot']['conversation_id'],))['n'] == 1


def test_http_preview_is_read_only_apply_is_idempotent_and_stale_is_atomic(bridge):
    b = bridge
    joined = setup_room(b)
    store = b.app.state.store
    grants = store.all('SELECT * FROM grants ORDER BY id')
    args = mutation(b, joined, ['otherproject'])
    assert not store.all('SELECT * FROM conversation_working_scopes')
    assert store.all('SELECT * FROM grants ORDER BY id') == grants
    result = panel(b, 'conversation-projects', args)
    assert result['applied']
    assert [p['project_id'] for p in result['conversation']['projects']] == ['otherproject']
    assert panel(b, 'conversation-projects', args) == result
    stale = b.client.post('/api/collaboration/conversation-projects',
                          json={**args, 'idempotency_key': key()})
    assert stale.status_code == 409
    changed = b.client.post('/api/collaboration/conversation-projects',
                            json={**args, 'projects': []})
    assert changed.status_code == 409
    assert len(store.all('SELECT * FROM conversation_working_scopes')) == 1
    assert store.all('SELECT * FROM grants ORDER BY id') == grants


def test_manifest_distinguishes_exact_task_projects_stable_transport_and_grant(bridge):
    b = bridge
    joined = setup_room(b)
    before = connection(b, joined)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    current = connection(b, joined)
    manifest = current['consumer_configuration']['scope_manifest']
    assert manifest['selected_room_project_ids'] == ['otherproject']
    assert {r['project_id'] for r in manifest['task_project_rules']} == {'otherproject'}
    assert manifest['message_connection_project_id'] == 'project'
    assert manifest['effective_project_ids'] == ['otherproject', 'project']
    assert manifest['connection_exceeds_selected_room']
    assert manifest['working_scope_applies_to'] == 'managed_room_tasks_only'
    assert not manifest['ordinary_tools_room_scoped']
    assert not manifest['chat_only']
    assert current['inbox_request'] == before['inbox_request']
    assert current['subscription_requests'] == before['subscription_requests']
    first, second = b.room['id'], current['consumer_configuration']['scope_manifest']['conversation_id']
    assert first == second
    panel(b, 'conversation-projects', mutation(b, joined, []))
    empty = connection(b, joined)
    manifest = empty['consumer_configuration']['scope_manifest']
    assert manifest['chat_only']
    assert manifest['selected_room_project_ids'] == [] and manifest['task_project_rules'] == []
    assert manifest['effective_project_ids'] == ['otherproject', 'project']
    assert empty['subscription_requests'] == before['subscription_requests']
    source = send_owner(b, joined)
    inbox = tool(b, current['inbox_request']['tool'], current['inbox_request']['arguments'])
    assert inbox['items'][0]['message_id'] == source['message']['id']
    assert inbox['items'][0]['task_request'] is None
    posted = tool(b, 'collaboration', {
        'action': 'dot_message', **inbox['items'][0]['reply_arguments'],
        'body_text': 'We can keep discussing with no active task project.',
    })
    assert posted['message']['reply_to_id'] == source['message']['id']


def test_bad_preview_and_unknown_project_never_commit_a_revision(bridge):
    b = bridge
    joined = setup_room(b)
    args = mutation(b, joined, ['otherproject'])
    response = b.client.post('/api/collaboration/conversation-projects',
                             json={**args, 'selection_digest': '0' * 64})
    assert response.status_code == 409
    response = b.client.post('/api/collaboration/conversation-projects-preview',
                             json=selection(b, joined, ['not-authorized-project']))
    assert response.is_success and response.json()['ready'] is False
    assert not b.app.state.store.all('SELECT * FROM conversation_working_scopes')


@pytest.mark.parametrize('operation', ['dot_inbox', 'dot_connection', 'dot_message'])
def test_revoking_underlying_grant_still_blocks_message_connection(bridge, operation):
    b = bridge
    joined = setup_room(b)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    send_owner(b, joined)
    store = b.app.state.store
    message_count = store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n']
    store.execute('UPDATE grants SET revoked=1 WHERE id=?', (b.grant,))
    args = {'action': operation, **b.scope, 'dot_id': joined['slot']['id']}
    name = 'collaboration_query'
    if operation == 'dot_message':
        name = 'collaboration'
        args.update(body_text='This revoked fixture must not post.', idempotency_key=key())
    response = rpc(b, 'tools/call', {'name': name, 'arguments': args})
    if response.status_code == 200:
        data = response.json()
        assert 'error' in data or data.get('result', {}).get('isError'), data
    else:
        assert response.status_code in {401, 403}
    assert store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == message_count
    assert len(store.all('SELECT * FROM conversation_working_scopes')) == 1
