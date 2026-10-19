"""Loopback MCP discovery follows current grants, independent of project open."""
import json

import pytest
from tests.support import running_stack


@pytest.fixture(scope='module')
def discovery_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('dot-discovery-stack')) as stack:
        grant = stack.must(stack.client.post('/api/grants', json={
            'label': 'Dot discovery read fixture', 'scopes': ['read'],
            'projects': [stack.project['id']], 'days': 1}))
        stack.discovery_reader = grant['token']
        yield stack


@pytest.mark.integration
def test_http_catalog_help_and_bootstrap_share_current_scope(discovery_stack):
    stack = discovery_stack
    token = stack.discovery_reader
    before = stack.rpc('tools/list', token_value=token).json()['result']
    tools = {tool['name']: tool for tool in before['tools']}
    assert 'exec' not in tools and 'edit' not in tools
    assert tools['computer']['inputSchema']['properties']['operation']['enum'] == ['status']
    listed = stack.mcp('project_query', {'operation': 'help'}, token)
    assert not listed['isError']
    assert listed['structuredContent']['tools']['computer']['operations'] == ['status']
    denied = stack.mcp('project_query',
        {'operation': 'help', 'tool': 'computer', 'action': 'action'}, token)
    assert denied['isError']
    assert denied['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'
    opened = stack.mcp('project_query', {'operation': 'open', 'project': 'ProjectAlpha'}, token)
    assert not opened['isError'], opened
    value = opened['structuredContent']
    if value.get('pending'):
        operation = stack.poll(value['operation_id'])
        assert operation['state'] == 'succeeded', operation
        polled = stack.mcp('task_query',
            {'operation': 'get', 'operation_ids': [value['operation_id']]}, token)
        value = polled['structuredContent']['operations'][0]['result']['data']
    assert set(value['capability_index']) == {'search', 'changes', 'tasks', 'artifacts'}
    after = stack.rpc('tools/list', token_value=token).json()['result']
    assert before == after
    # A different grant on the same connection does not inherit the reader's catalog.
    writer = stack.rpc('tools/list').json()['result']
    assert 'exec' in {tool['name'] for tool in writer['tools']}
    assert writer['_meta'] != after['_meta']
    for profile in ('core', 'coding', 'full'):
        reply = stack.client.post('/mcp?profile=' + profile,
            headers={'Accept': 'application/json, text/event-stream',
                     'Authorization': 'Bearer ' + token},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).json()['result']
        assert reply['tools'] == before['tools']
    assert json.loads(listed['content'][0]['text']) == listed['structuredContent']
