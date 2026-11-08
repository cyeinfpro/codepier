"""Public catalogs contain core tools, explicit queries and stable identity tools."""
from shared.knowledge_contracts import KNOWLEDGE_TOOLS
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS
from shared.query_contracts import QUERY_TOOLS
from shared.mcp_protocol import MODERN, PREFIX, request_headers


def profile_catalog(stack, profile, *, token=None):
    """Discover an explicit compatibility profile without changing the default endpoint."""
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {
        '_meta': {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}}}
    response = stack.client.post('/mcp', params={'profile': profile}, json=body, headers={
        'Authorization': 'Bearer ' + (token or stack.pat),
        'Accept': 'application/json, text/event-stream', **request_headers(body)})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert 'error' not in reply, reply
    return reply['result']


def assert_task_catalog(catalog, model_count=15, *, expected_names=None):
    names = {tool['name'] for tool in catalog}
    expected = (CORE_TOOLS | QUERY_TOOLS | KNOWLEDGE_TOOLS | {'get_profile','get_access_context'}
                if expected_names is None else set(expected_names))
    assert len(catalog) == len(names) == len(expected)
    assert names == expected
    assert not names.intersection(REPLACED_MCP_TOOLS)
    visible = [tool for tool in catalog if tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app']) != ['app']]
    assert len(visible) == model_count == len(expected)
    assert not [tool for tool in catalog if tool not in visible]
    for tool in catalog:
        assert 'password' not in tool['inputSchema']['properties']
        if tool.get('_meta', {}).get('ui', {}).get('resourceUri'):
            assert 'model' in tool['_meta']['ui']['visibility']
            assert tool['_meta'].get('openai/visibility', 'public') == 'public'
