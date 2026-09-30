"""Regression for the real-provider acceptance client, using the real MCP router.

No Docker or external identity provider is needed for transport contract checks.
The full Authentik job separately runs browser login, consent and offboarding.
"""
import json
import time

import httpx
import pytest

from scripts.check_oidc_authentik import (
    MCP_ACCEPTANCE_VERSION, initialize_mcp, mcp_request, required,
)
from tests.test_access_profiles import data
from tests.test_iam_integration import team as team, shared_role
from tests.test_roles import credential, must, profile, update_role


def test_acceptance_client_sends_required_headers_and_initialization():
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        if body['method'] == 'notifications/initialized':
            assert 'id' not in body
            return httpx.Response(202)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'],
            'result': {'protocolVersion': MCP_ACCEPTANCE_VERSION}})

    with httpx.Client(base_url='http://fixture.test', transport=httpx.MockTransport(handle)) as client:
        initialize_mcp(client, 'fixture-token')
        mcp_request(client, 'fixture-token', 'tools/call', {'name': 'get_profile', 'arguments': {}})
    assert len(requests) == 3
    for request in requests:
        assert request.method == 'POST' and request.url.path == '/mcp'
        assert request.headers['authorization'] == 'Bearer fixture-token'
        assert set(request.headers['accept'].split(', ')) == {'application/json', 'text/event-stream'}
        assert request.headers['mcp-protocol-version'] == MCP_ACCEPTANCE_VERSION
        assert request.headers['content-type'] == 'application/json'
        assert 'cookie' not in request.headers and 'x-codepier-space' not in request.headers
    assert json.loads(requests[0].content)['id'] != json.loads(requests[2].content)['id']


@pytest.mark.parametrize('status', [400, 401, 403, 406, 500])
def test_acceptance_client_does_not_retry_mask_or_echo_failure_bodies(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'secret': 'do-not-echo-provider-data'})

    with httpx.Client(base_url='http://fixture.test', transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssertionError, match=f'expected 200, got {status}') as caught:
            initialize_mcp(client, 'private-fixture-token')
    assert len(requests) == 1
    assert 'private-fixture-token' not in str(caught.value)
    assert 'do-not-echo-provider-data' not in str(caught.value)


def test_acceptance_helper_roundtrips_real_mcp_without_panel_credentials(team):
    app, browsers = team
    role = shared_role(app, browsers)
    identity = profile(browsers['alice'], role)
    token = must(credential(browsers['alice'], role, identity))['token']
    client = browsers['alice'].client  # Bare TestClient, not the cookie/Space wrapper.
    assert not client.cookies
    initialize_mcp(client, token)
    result = data(mcp_request(client, token, 'tools/call', {'name': 'get_profile', 'arguments': {}}))
    assert result['id'] == identity['id']
    catalog = required(mcp_request(client, token, 'tools/list'))['result']['tools']
    assert any(tool['name'] == 'get_access_context' for tool in catalog)
    assert not client.cookies


@pytest.mark.parametrize('accept', ['*/*', 'application/json', 'text/event-stream'])
def test_server_keeps_rejecting_incomplete_media_types(team, accept):
    app, browsers = team
    role = shared_role(app, browsers)
    token = must(credential(browsers['alice'], role, profile(browsers['alice'], role)))['token']
    response = browsers['alice'].client.post('/mcp', headers={
        'Authorization': 'Bearer ' + token, 'Accept': accept,
        'MCP-Protocol-Version': MCP_ACCEPTANCE_VERSION,
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert response.status_code == 406
    assert response.json()['error']['code'] == -32600


def test_transport_fix_preserves_live_role_expansion_and_revocation(team):
    app, browsers = team
    role = shared_role(app, browsers)
    identity = profile(browsers['alice'], role)
    grant = must(credential(browsers['alice'], role, identity))
    client, token = browsers['alice'].client, grant['token']
    initialize_mcp(client, token)
    with app.state.store.transaction():
        app.state.store.db.execute('''INSERT INTO projects
            (id,alias,alias_key,device_id,root,space_id,owner_user_id,created)
            VALUES('fixture-new','fixture-new','fixture-new','device-team','/tmp/fixture-new','team','owner',?)''', (time.time(),))
    role = must(update_role(browsers['owner'], role,
        project_rules=[{'actions': ['read', 'write', 'execute'], 'all_projects': True}]))
    context = data(mcp_request(client, token, 'tools/call', {'name': 'get_access_context', 'arguments': {}}))
    assert {project['id'] for project in context['projects']} == {'project-team', 'fixture-new'}
    assert all('execute' in project['actions'] for project in context['project_permissions'])
    app.state.store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
    response = mcp_request(client, token, 'tools/call', {'name': 'projects_list', 'arguments': {}})
    assert response.status_code == 403
    assert app.state.store.one('SELECT id FROM grants WHERE id=?', (grant['grant_id'],))['id'] == grant['grant_id']


def test_mcp_credential_not_overridden_by_another_users_panel_session(team):
    app, browsers = team
    role = shared_role(app, browsers)
    identity = profile(browsers['alice'], role)
    token = must(credential(browsers['alice'], role, identity))['token']
    result = data(mcp_request(browsers['bob'], token, 'tools/call', {'name': 'get_profile', 'arguments': {}}))
    assert result['id'] == identity['id']
    response = mcp_request(browsers['bob'], 'invalid-token', 'tools/list')
    assert response.status_code == 401
