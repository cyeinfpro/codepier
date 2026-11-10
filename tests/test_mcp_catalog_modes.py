"""Catalog slimming is a presentation choice, never routing authority."""
import json
import pytest
from shared.contracts import tool_definitions, CODING_TOOLS
from shared.core_contracts import CORE_INSTRUCTIONS, CORE_WORKFLOW_INSTRUCTIONS, READ_BATCH_PAYLOAD_BYTES
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from tests.test_audit_api import api


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def test_catalog_modes_and_budget():
    full = tool_definitions()
    lean = tool_definitions(schemas='lean')
    # Includes the bounded fs_search result union in both workspace/query catalogs.
    assert size(full) < 91000
    assert size(lean) < 30000
    assert size(tool_definitions('coding', schemas='lean')) < 23000
    assert {x['name'] for x in tool_definitions('coding')} == CODING_TOOLS
    assert all('outputSchema' not in x for x in tool_definitions(schemas='none'))
    assert full == tool_definitions()  # mode projection does not mutate cached schemas
    assert [x['inputSchema'] for x in full] == [x['inputSchema'] for x in lean]
    assert len(CORE_INSTRUCTIONS) < 2100 < len(CORE_WORKFLOW_INSTRUCTIONS)
    assert READ_BATCH_PAYLOAD_BYTES == 48 * 1024
    with pytest.raises(ValueError):
        tool_definitions(schemas='invalid')



def test_catalog_preserves_room_for_small_contract_additions():
    # Keep growth room below the existing hard budgets, not just a passing byte.
    assert len(json.dumps(tool_definitions()).encode()) + 512 < 95000
    assert size(tool_definitions(schemas='lean')) + 256 < 30000
    assert size(tool_definitions('coding', schemas='lean')) + 512 < 23000


def call(client, credential, method, params=None, query='', modern=False):
    params = dict(params or {})
    if modern:
        params['_meta'] = {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}
    body = {'jsonrpc': '2.0', 'id': 'catalog-mode', 'method': method, 'params': params}
    headers = {'Authorization': 'Bearer ' + credential, 'Accept': 'application/json, text/event-stream'}
    if modern:
        headers.update(request_headers(body))
    return client.post('/mcp' + query, json=body, headers=headers)


@pytest.mark.parametrize('modern', [False, True])
def test_http_catalog_and_hidden_route(api, modern):
    _, client, credential = api
    base = call(client, credential, 'tools/list', modern=modern).json()['result']
    lean = call(client, credential, 'tools/list', query='?profile=coding&schemas=lean', modern=modern).json()['result']
    assert {x['name'] for x in lean['tools']} <= CODING_TOOLS
    assert {'query', 'get_profile'} <= {x['name'] for x in base['tools']}
    assert 'get_access_context' not in {x['name'] for x in base['tools']}
    legacy = call(client, credential, 'tools/list', query='?profile=core', modern=modern).json()['result']
    assert 'get_access_context' in {x['name'] for x in legacy['tools']}
    assert base['_meta']['com.codepier/catalogSha256'] != lean['_meta']['com.codepier/catalogSha256']
    result = call(client, credential, 'tools/call', {'name': 'get_access_context', 'arguments': {}},
                  query='?profile=coding&schemas=none', modern=modern).json()['result']
    assert not result.get('isError'), result
    assert 'com.codepier/serviceVersion' not in result['_meta']
    assert 'com.codepier/requestId' in result['_meta']
    if modern:
        assert PREFIX + 'serverInfo' in result['_meta']
    old = call(client, credential, 'tools/call', {'name': 'get_access_context', 'arguments': {}},
               query='?view=1', modern=modern).json()['result']
    assert 'com.codepier/serviceVersion' in old['_meta']


@pytest.mark.parametrize('query', ['?schemas=bad', '?profile=bad', '?view=4', '?verbosity=bad', '?text=bad', '?output=bad', '?mirror=bad', '?mirror=meta'])
def test_invalid_modes_rejected(api, query):
    _, client, credential = api
    response = call(client, credential, 'tools/list', query=query)
    assert response.status_code == 400
    assert response.json()['error']['code'] == -32602


def test_collaboration_shallow_catalog_preserves_fields_and_strict_actions():
    from jsonschema import Draft202012Validator
    from shared.public_collaboration import ACTIONS, action_help, lean_input_schema, resolve
    from shared.public_collaboration import tool_definitions as collaboration_tools
    from shared.util import DevError
    full = collaboration_tools()
    lean = [{**item, 'inputSchema': lean_input_schema(item['inputSchema'])} for item in full]
    assert size(lean) < 32000
    assert size(lean) < size(full) * 0.55
    for original, compact in zip(full, lean):
        assert original['inputSchema']['properties'].keys() == compact['inputSchema']['properties'].keys()
        assert original['inputSchema']['required'] == compact['inputSchema']['required']
        Draft202012Validator.check_schema(compact['inputSchema'])
    for tool, actions in ACTIONS.items():
        assert set(action_help(tool)['tools'][tool]['operations']) == set(actions)
        for action in actions:
            Draft202012Validator.check_schema(action_help(tool, action)['inputSchema'])
    valid = {'action': 'overview', 'project': 'test'}
    precise = action_help('collaboration_query', 'overview')['inputSchema']
    Draft202012Validator(precise).validate(valid)
    assert resolve('collaboration_query', valid)[0] == 'collaboration_read'
    with pytest.raises(DevError):
        resolve('collaboration_query', {**valid, 'command': 'not permitted'})
    with pytest.raises(DevError):
        action_help('collaboration_work', 'execute', scopes=set())
    with pytest.raises(DevError):
        action_help('collaboration_query', 'not_an_action')


def test_collaboration_help_http(api):
    _, client, credential = api
    response = call(client, credential, 'tools/call', {'name': 'project_query', 'arguments': {
        'operation': 'help', 'tool': 'collaboration_work', 'action': 'execute'}}, query='?schemas=lean')
    result = response.json()['result']
    assert not result.get('isError'), result
    assert result['structuredContent']['arguments_location'] == 'top-level'
    assert 'inputSchema' in result['structuredContent']
    from jsonschema import Draft202012Validator
    schema = next(t['outputSchema'] for t in tool_definitions() if t['name'] == 'project_query')
    Draft202012Validator(schema).validate(result['structuredContent'])


@pytest.mark.parametrize('name', [
    'workspace', 'read', 'write', 'edit', 'process', 'project_query', 'task_query',
])
def test_duplicate_options_help_preserves_schema_boundaries_and_cache(name):
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    from shared.contracts import TOOLS, _compact_input_schema
    from shared.core_output_schemas import factor
    from shared.schema_cache import schema_for

    model = TOOLS[name].model
    original = model.model_json_schema()
    cached = deepcopy(schema_for(model))
    catalog = {item['name']: item for item in tool_definitions()}
    advertised = catalog[name]['inputSchema']
    expected = _compact_input_schema(cached)
    options = expected['properties']['options']
    assert options['description'] == 'Help: project_query.'
    if name == 'task_query':
        # Without a help route in the tool description, the field must retain it.
        assert 'project_query' not in catalog[name]['description']
        assert advertised['properties']['options']['description'] == 'Help: project_query.'
    else:
        assert name == 'project_query' or 'project_query' in catalog[name]['description']
        options.pop('description')
        assert 'description' not in advertised['properties']['options']
    # No validation-bearing field or other annotation was removed by help deduplication.
    assert advertised == factor(expected)
    assert catalog['exec']['inputSchema']['properties']['options']['description'] == (
        'order=default|strict; wait_policy=queue|fail_fast.')

    Draft202012Validator.check_schema(original)
    Draft202012Validator.check_schema(advertised)
    source_validator = Draft202012Validator(original)
    catalog_validator = Draft202012Validator(advertised)
    properties = original['properties']
    base = {field: value for field, value in {
        'project': 'Fixture', 'idempotency_key': 'fixture-schema-key',
    }.items() if field in properties}
    cases = [
        (base, True),
        ({**base, 'options': {}}, True),
        ({**base, 'options': {'synthetic_extension': {'value': 1}}}, True),
        ({**base, 'options': []}, False),
        ({**base, 'options': 'not-an-object'}, False),
        ({**base, 'operation': 'not-an-operation'}, False),
        ({**base, 'synthetic_unknown_top_level': True}, False),
    ]
    for field in original.get('required', []):
        cases.append(({key: value for key, value in base.items() if key != field}, False))
    for payload, valid in cases:
        assert source_validator.is_valid(payload) is valid, (name, payload)
        assert catalog_validator.is_valid(payload) is valid, (name, payload)
    assert schema_for(model) == cached
    assert model.model_json_schema() == original
    assert tool_definitions() == list(catalog.values())
