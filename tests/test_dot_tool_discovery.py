"""Current-principal discovery contracts; no host desktop or production profile."""
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from hub.core_tools import help_result, result as core_result
from hub.tool_router import ToolRouter
from shared.contracts import tool_definitions
from shared.runtime_results import capability_index
from shared.util import DevError
from tests.test_core_tools import runtime, reader_principal


class Gateway:
    secret = b'discovery-fixture-only'
    def tools(self, principal):
        return []


def catalog(runtime, principal):
    return ToolRouter(runtime.store, Gateway()).list_tools(principal)


def test_read_catalog_prunes_controls_and_related_definitions(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    definitions = {item['name']: item for item in catalog(instance, reader)['tools']}
    assert 'exec' not in definitions and 'edit' not in definitions
    computer = definitions['computer']
    props = computer['inputSchema']['properties']
    assert props['operation']['enum'] == ['status']
    assert props['probe']['const'] is False
    assert not {'action', 'app', 'session_id', 'force'} & props.keys()
    assert '$defs' not in computer['inputSchema']
    assert definitions['browser']['inputSchema']['properties']['operation']['enum'] == ['status']
    assert definitions['write']['inputSchema']['properties']['operation']['enum'] == ['source_check']
    assert definitions['write']['securitySchemes'] == [{'type': 'oauth2', 'scopes': ['read']}]
    # Scope-specific discovery never weakens caller-platform approval annotations.
    original = {item['name']: item for item in tool_definitions()}
    assert computer['annotations'] == original['computer']['annotations']
    assert computer['annotations']['readOnlyHint'] is False
    for item in definitions.values():
        Draft202012Validator.check_schema(item['inputSchema'])


@pytest.mark.asyncio
async def test_help_uses_live_scope_and_argument_sensitive_probe(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    help_value = await instance.invoke('project_query', {'operation': 'help'}, reader)
    assert 'exec' not in help_value['tools'] and 'edit' not in help_value['tools']
    assert help_value['tools']['computer']['operations'] == ['status']
    assert help_value['tools']['write']['operations'] == ['source_check']
    assert 'lsp' not in help_value['tools']['read']['operations']
    status = await instance.invoke('project_query',
        {'operation': 'help', 'tool': 'computer', 'action': 'status'}, reader)
    assert status['scope'] == 'read'
    file_help = await instance.invoke('project_query',
        {'operation': 'help', 'tool': 'read', 'action': 'file'}, reader)
    assert file_help['inputSchema']['properties']['operation']['const'] == 'file'
    assert status['inputSchema']['properties']['probe']['const'] is False
    with pytest.raises(DevError) as denied:
        await instance.invoke('project_query',
            {'operation': 'help', 'tool': 'computer', 'action': 'action'}, reader)
    assert denied.value.code == 'INSUFFICIENT_SCOPE'
    assert denied.value.details['required_scope'] == 'computer'


@pytest.mark.asyncio
async def test_hidden_dispatch_retains_independent_authorization(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    router = ToolRouter(instance.store, Gateway())
    assert router.resolve(reader, 'exec').backend == 'native'
    with pytest.raises(DevError) as denied:
        await instance.invoke('exec',
            {'project': 'Fixture', 'command': 'true', 'idempotency_key': 'discovery-scope-denied'}, reader)
    assert denied.value.code == 'INSUFFICIENT_SCOPE'
    with pytest.raises(DevError) as probe:
        await instance.invoke('computer', {'project': 'Fixture', 'operation': 'status', 'probe': True}, reader)
    assert probe.value.details['required_scope'] == 'computer'
    with pytest.raises(DevError) as legacy:
        router.resolve(reader, 'computer_action')
    assert legacy.value.code == 'TOOL_REMOVED'


def test_scope_revocation_changes_catalog_and_rejects_stale_cursor(runtime, monkeypatch):
    from hub.gateway import catalog as paging
    instance, _ = runtime
    reader = reader_principal(instance)
    instance.store.execute('UPDATE grants SET scopes=? WHERE id=?',
        (json.dumps(['read', 'execute']), reader.grant_id))
    router = ToolRouter(instance.store, Gateway())
    monkeypatch.setattr(paging, 'PAGE_SIZE', 2)
    before = router.list_tools(reader)
    cursor = before['nextCursor']
    instance.store.execute('UPDATE grants SET scopes=? WHERE id=?',
        (json.dumps(['read']), reader.grant_id))
    after = router.list_tools(reader)
    assert before['_meta'] != after['_meta']
    with pytest.raises(DevError) as changed:
        router.list_tools(reader, cursor)
    assert changed.value.code == 'GATEWAY_CATALOG_CHANGED'


def test_hidden_native_namespace_cannot_be_claimed_by_gateway(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    class ConflictingGateway(Gateway):
        def tools(self, principal):
            return [{'name': 'exec', 'inputSchema': {'type': 'object'}}]
    with pytest.raises(DevError) as collision:
        ToolRouter(instance.store, ConflictingGateway()).list_tools(reader)
    assert collision.value.code == 'TOOL_NAME_COLLISION'


@pytest.mark.parametrize('scopes', [
    {'read'}, {'read', 'write'}, {'read', 'execute'}, {'read', 'computer'},
    {'read', 'write', 'execute', 'computer', 'devices.read', 'projects.create'},
])
def test_schema_help_hints_share_one_policy_without_mutating_cached_schema(scopes):
    from hub.tool_discovery import visible_definitions
    original = tool_definitions()
    saved = copy.deepcopy(original)
    projected = visible_definitions(original, scopes)
    assert original == saved
    assert projected == visible_definitions(tool_definitions(), scopes)
    listed = {item['name']: item for item in projected}
    help_tools = help_result(scopes=scopes)['tools']
    for name, value in help_tools.items():
        if name in {'exec', 'vps'}:
            continue
        properties = listed[name]['inputSchema']['properties']
        expected = set(value['operations']) | ({'help'} if name == 'workspace' else set())
        assert set(properties['operation']['enum']) == expected
    for item in projected:
        Draft202012Validator.check_schema(item['inputSchema'])
    assert len(json.dumps(projected).encode()) < 95000


def test_result_hints_hide_unavailable_actions_preserve_original_evidence():
    raw = {'operation_id': 'original', 'state': 'unknown', 'pending': True,
        'output': 'computer.action /Users/fixture/private/path remains raw',
        'output_seq': 7, 'output_truncated': True,
        'result': {'ok': True, 'data': {'capability_index': capability_index('Fixture'),
            'log_ref': {'operation_id': 'original', 'source': 'operation_output'},
            'next': {'name': 'computer_action', 'arguments': {'project': 'Fixture'}},
            'output': 'unchanged'}},
        'next_call': {'name': 'operations_wait', 'arguments': {'operation_id': 'original'}}}
    before = copy.deepcopy(raw)
    public = core_result('task_query', {'operation': 'get'},
        {'operations': [raw]}, scopes={'read'})['structuredContent']['operations'][0]
    data = public['result']['data']
    assert set(data['capability_index']) == {'search', 'changes', 'tasks', 'artifacts'}
    assert data.get('next') is None
    assert public['next_call']['name'] == 'task_query'
    for key in ('operation_id', 'state', 'pending', 'output', 'output_seq',
                'output_truncated'):
        assert public[key] == raw[key]
    assert data['log_ref'] == raw['result']['data']['log_ref']
    assert raw == before


def test_explicit_help_hint_cannot_offer_a_hidden_action():
    from hub.tool_discovery import project_hints
    original = {'capability_index': {'desktop': {'name': 'project_query',
        'arguments': {'operation': 'help', 'tool': 'computer', 'action': 'action'}}},
        'content': {'next': {'name': 'computer_action', 'arguments': {}}},
        'output': 'next_call and /workspace paths in source remain raw'}
    public = project_hints(copy.deepcopy(original), {'read'})
    assert public['capability_index'] == {}
    assert public['content'] == original['content']
    assert public['output'] == original['output']


@pytest.mark.parametrize('arguments', [
    {'operation': 'open', 'project': 'Fixture', 'capture_baseline': True},
    {'operation': 'open', 'project': 'Fixture', 'options': {'capture_baseline': True}},
])
def test_baseline_hints_use_the_same_forwarded_arguments(arguments):
    from hub.tool_discovery import call_visible, project_hints
    assert call_visible('workspace', arguments, {'read'}) is False
    assert call_visible('workspace', arguments, {'read', 'write'}) is True
    assert call_visible('project_query', arguments, {'read', 'write'}) is False
    value = {'next_call': {'name': 'workspace', 'arguments': arguments},
             'operation_id': 'original', 'state': 'unknown'}
    assert project_hints(copy.deepcopy(value), {'read'}) == {**value, 'next_call': None}
    assert call_visible('workspace', {'operation': 'open', 'project': 'Fixture',
        'options': {'capture_baseline': False}}, {'read'}) is True
