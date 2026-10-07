"""Real Hub HTTP, session/CSRF boundaries and the unchanged legacy MCP surface."""
import uuid

import pytest
from tests.support import running_stack
from tests.test_mcp_tasks_http import modern
from tests.test_collaboration_service import key


@pytest.fixture
def collaboration_stack(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MCP_EVENTS_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MONITOR_COLLECTOR_ENABLED', 'false')
    monkeypatch.setenv('CODEPIER_ANALYSIS_DISPATCH_ENABLED', 'false')
    with running_stack(tmp_path / 'collaboration-http') as stack:
        yield stack


def test_panel_and_mcp_share_persisted_records_but_not_owner_authority(collaboration_stack):
    s = collaboration_stack
    status = s.must(s.client.get('/api/collaboration/status'))
    assert status['features']['enabled'] and not status['production_actions_enabled']
    scope = {'project': 'ProjectAlpha', 'environment_id': 'production'}
    room = s.must(s.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    grant = s.must(s.client.post('/api/grants', json={'label': 'Read-only collaboration fixture',
                   'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
    agent = s.must(s.client.post('/api/collaboration/agent', json={**scope, 'label': 'Work fixture',
              'kind': 'work_cloud', 'grant_id': grant['grant_id'], 'idempotency_key': key()}))
    args = {**scope, 'room_id': room['id'], 'structured_mentions': [{'agent_id': agent['id']}],
            'request': 'Read synthetic project information', 'source_message_id': key(), 'idempotency_key': key()}
    denied = s.client.post('/api/collaboration/command', json=args, headers={'X-RD-CSRF': 'wrong'})
    assert denied.status_code == 403
    proposed = modern(s, 'tools/call', {'name': 'collaboration_command_create', 'arguments': args}, token=grant['token']).json()['result']
    assert not proposed['isError'] and proposed['structuredContent']['state'] == 'awaiting_approval'
    overview = s.must(s.client.get('/api/collaboration', params=scope))
    assert overview['counts']['open_jobs'] == 0 and overview['counts']['pending_proposals'] == 1
    accepted = s.must(s.client.post('/api/collaboration/control', json={**scope,
        'target_id': proposed['structuredContent']['id'], 'action': 'accept_proposal', 'expected_version': 1,
        'reason': 'Approve the bounded read-only analysis', 'idempotency_key': key()}))
    claim = modern(s, 'tools/call', {'name': 'collaboration_claim', 'arguments': {**scope,
        'job_id': accepted['job_id'], 'expected_version': 1, 'idempotency_key': key()}}, token=grant['token']).json()['result']
    assert not claim['isError'] and claim['structuredContent']['state'] == 'leased'
    malicious = s.client.post('/api/collaboration/command', json={**args, 'author': 'owner'})
    assert malicious.status_code == 422
    outside = modern(s, 'tools/call', {'name': 'collaboration_read', 'arguments': {**scope,
        'project': 'not-authorized', 'kind': 'job', 'id': accepted['job_id']}}, token=grant['token']).json()['result']
    assert outside['isError']


def test_events_discovery_and_old_clients_are_not_conflated(collaboration_stack):
    s = collaboration_stack
    assert 'events' in modern(s, 'server/discover').json()['result']['capabilities']
    old = s.rpc('initialize', {'protocolVersion': '2025-11-25'}).json()['result']
    assert 'events' not in old['capabilities']
    assert s.rpc('events/list', {}).json()['error']['code'] == -32601
    grant = s.must(s.client.post('/api/grants', json={'label': 'events readonly fixture',
                   'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
    catalog = modern(s, 'events/list', token=grant['token']).json()['result']
    assert len(catalog['events']) == 4 and catalog['resultType'] == 'complete'
    args = {'name': 'codepier.collaboration.task_available.v1', 'arguments': {},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.invalid', 'secret': 'invalid'}}
    invalid = modern(s, 'events/subscribe', args, token=grant['token'], headers={'Mcp-Name': 'wrong'})
    assert invalid.json()['error']['code'] == -32020
