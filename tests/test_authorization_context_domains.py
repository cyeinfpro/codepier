"""Profile metadata, direct queries and task approval are independent domains."""
import json
import re

import jsonschema
import pytest

from hub import mcp_request_audit as audit
from shared.access_profile_contracts import AUTHORIZATION_CONTEXT_VERSION, AUTHORIZATION_DOMAINS
from shared.contracts import tool_definitions
from shared.util import DevError, VERSION
from tests.test_audit_api import api as api, rpc
from tests.test_access_profiles import create, pat, call, data
from tests.test_roles import setup_role
from tests.collaboration_support import collaboration_stack as collaboration_stack


@pytest.mark.parametrize('kind', ['fixed', 'profile', 'role'])
@pytest.mark.parametrize('scopes', [['read'], ['read', 'write', 'execute']])
def test_context_distinguishes_profile_and_delegation_without_mutation(api, kind, scopes):
    app, client, _ = api
    app.state.store.execute('UPDATE projects SET allow_tasks=1')
    if kind == 'role':
        _, _, credential = setup_role(client, project_rules=[{'actions': scopes, 'projects': ['project']}])
    elif kind == 'profile':
        credential = pat(client, create(client, scopes=scopes))
    else:
        response = client.post('/api/grants', json={'label': 'ordinary fixture', 'scopes': scopes, 'projects': ['project']})
        assert response.status_code == 200, response.text
        credential = response.json()
    store = app.state.store
    tables = ['grants', 'tokens', 'access_profiles', 'delegation_policies', 'delegation_requests', 'coordination_work']
    before = {table: store.all('SELECT * FROM ' + table + ' ORDER BY rowid') for table in tables}
    response = call(client, credential['token'], 'get_access_context')
    context = data(response)
    assert context['access_profile_managed'] is (kind != 'fixed')
    assert context['managed'] is context['access_profile_managed']
    assert context['managed_field_meaning'] == 'access_profile_binding'
    assert context['authorization_context_version'] == AUTHORIZATION_CONTEXT_VERSION == 2
    assert context['server_version'] == VERSION
    assert context['authorization_domains'] == AUTHORIZATION_DOMAINS
    assert set(context['scopes']) >= set(scopes)
    assert context['authorization_mode'] == ('role' if kind == 'role' else 'fixed')
    assert [p['id'] for p in context['projects']] == ['project']
    assert 'delegation_id' not in context and 'consumer_mode' not in context
    definition = next(t for t in tool_definitions(authorization=context['authorization_mode']) if t['name'] == 'get_access_context')
    jsonschema.validate(context, definition['outputSchema'])
    assert definition['outputSchema']['properties']['managed']['deprecated'] is True
    assert 'not a task execution mode' in definition['description']
    assert {table: store.all('SELECT * FROM ' + table + ' ORDER BY rowid') for table in tables} == before
    rid = response.headers['X-CodePier-Request-ID']
    assert response.json()['result']['_meta']['com.codepier/requestId'] == rid
    assert re.fullmatch('[a-f0-9]{32}', rid)


@pytest.mark.parametrize('field', ['managed', 'access_profile_managed', 'authorization_domains', 'delegation_id'])
def test_context_output_cannot_be_used_as_an_input_exemption(api, field):
    app, client, token = api
    response = call(client, token, 'get_access_context', {field: False})
    assert response.json()['result']['isError']
    assert response.json()['result']['structuredContent']['error']['code'] == 'INVALID_ARGUMENTS'
    assert not app.state.store.all('SELECT * FROM operations')


@pytest.mark.parametrize('code', ['DELEGATION_NOT_FOUND', 'DELEGATION_GRANT_REQUIRED',
    'DELEGATION_POLICY_INACTIVE', 'WORK_LEASE_EXPIRED', 'GOAL_CAPABILITY_DENIED'])
def test_tool_denials_keep_original_code_and_server_owned_correlation(api, monkeypatch, code):
    app, client, token = api
    captured = []
    monkeypatch.setattr(audit, 'write_event', lambda event: captured.append(dict(event)))
    original = app.state.runtime.invoke
    async def denied(name, args, principal):
        if name == 'project_query':
            raise DevError(code, 'synthetic denial', 403)
        return await original(name, args, principal)
    monkeypatch.setattr(app.state.runtime, 'invoke', denied)
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'project_query', 'arguments': {'operation': 'list'},
        '_meta': {'com.codepier/requestId': 'UNTRUSTED-CLIENT-ID'}}}
    response = rpc(client, token, body)
    result = response.json()['result']
    rid = response.headers['X-CodePier-Request-ID']
    assert result['isError'] and result['structuredContent']['error']['code'] == code
    assert result['_meta']['com.codepier/requestId'] == rid != 'UNTRUSTED-CLIENT-ID'
    assert any(e.get('error_code') == code and e['request_id'] == rid for e in captured)
    assert 'UNTRUSTED-CLIENT-ID' not in json.dumps(captured)
    assert not app.state.store.all('SELECT * FROM operations')


def test_unknown_error_strings_still_cannot_enter_diagnostic_logs(monkeypatch):
    captured = []
    monkeypatch.setattr(audit, 'write_event', lambda event: captured.append(dict(event)))
    trace = audit.Trace(True, 'POST')
    trace.record('tool_error', error_code='DELEGATION_PRIVATE_TOKEN_do_not_log')
    assert captured[-1]['error_code'] == 'OTHER'
    assert 'PRIVATE_TOKEN' not in json.dumps(captured)


@pytest.mark.integration
@pytest.mark.parametrize('profile_bound', [False, True])
def test_real_query_status_does_not_create_delegation_or_require_its_id(collaboration_stack, profile_bound):
    stack = collaboration_stack
    project_id = stack.project['id']
    if profile_bound:
        selected = create(stack.client, projects=[project_id], scopes=['read'])
        credential = pat(stack.client, selected)['token']
    else:
        credential = stack.must(stack.client.post('/api/grants', json={
            'label': 'standalone status fixture', 'scopes': ['read'], 'projects': [project_id]}))['token']
    def invoke(name, args):
        result = stack.mcp(name, args, credential)
        assert not result.get('isError'), result
        return result['structuredContent']
    context = invoke('get_access_context', {})
    assert context['managed'] is context['access_profile_managed'] is profile_bound
    result = invoke('project_query', {'operation': 'status', 'project': project_id})
    operation_id = result['operation_id']
    for _ in range(6):
        receipt = invoke('task_query', {'operation': 'wait', 'operation_ids': [operation_id], 'wait_seconds': 5})['operations'][0]
        assert receipt['operation_id'] == operation_id
        if not receipt['pending']:
            break
    assert receipt['state'] == 'succeeded', receipt
    assert 'shell' in receipt['result']['data'] and 'tools' in receipt['result']['data']
    assert invoke('collaboration_query', {'action': 'rooms', 'project': project_id})['items'] == []
    assert invoke('collaboration_query', {'action': 'delegation_policies', 'project': project_id})['setup_required']
    denied = stack.mcp('collaboration_query', {'action': 'delegation', 'project': project_id,
        'delegation_id': 'not-a-real-delegation'}, credential)
    assert denied['isError']
    denied_write = stack.mcp('write', {'project': project_id, 'path': 'must-not-create.txt',
        'content': 'no', 'expected_sha256': 'new', 'idempotency_key': 'no-write-authorization-fixture'}, credential)
    assert denied_write['isError'] and denied_write['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'
    assert not (stack.projectalpha / 'must-not-create.txt').exists()
