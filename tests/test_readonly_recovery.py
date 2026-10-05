"""Read-only discovery and receipt recovery must stay on read-only tools."""
import copy

import pytest
from pydantic import ValidationError

from hub.core_tools import public_call, result
from shared.contracts import TOOLS, tool_definitions
from jsonschema import Draft202012Validator
from tests.support import running_stack


@pytest.mark.parametrize('backend', ['operations_get', 'operations_wait', 'operations_trace'])
def test_receipt_recovery_uses_readonly_tool(backend):
    args = {'operation_id': 'original-operation'}
    name, call = public_call(backend, args)
    assert name == 'task_query'
    assert call['operation_ids'] == ['original-operation']
    TOOLS[name].model.model_validate(call)
    assert args == {'operation_id': 'original-operation'}


def test_pending_read_and_nested_wait_keep_original_receipt():
    pending = {'operation_id': 'original-operation', 'state': 'running', 'pending': True,
        'next': 'operations_wait', 'next_call': {'name': 'operations_wait',
            'arguments': {'operation_id': 'original-operation', 'wait_seconds': 5}}}
    original = copy.deepcopy(pending)
    first = result('read', {'project': 'Visible', 'path': 'README.md'}, pending)
    second = result('task_query', {'operation': 'wait'}, {'operations': [pending]})
    for value in (first['structuredContent'], second['structuredContent']['operations'][0]):
        assert value['next'] == 'task_query'
        assert value['next_call']['name'] == 'task_query'
        assert value['next_call']['arguments']['operation_ids'] == ['original-operation']
        assert value['pending'] is True
    assert pending == original


@pytest.mark.parametrize('operation', ['help', 'tree', 'skills', 'skill'])
def test_readonly_project_discovery_is_reachable(operation):
    TOOLS['project_query'].model.model_validate({'operation': operation, 'project': 'Visible'})


@pytest.mark.parametrize('backend,arguments', [
    ('fs_tree', {'project': 'Visible'}),
    ('skills_list', {'project': 'Visible'}),
    ('skills_read', {'project': 'Visible', 'skill_id': 'a' * 64}),
    ('open_workspace', {'project': 'Visible', 'capture_baseline': False}),
    ('workflows_get', {'project': 'Visible', 'workflow_id': 'a' * 32}),
])
def test_readonly_project_hints_use_query(backend, arguments):
    name, call = public_call(backend, arguments)
    assert name == 'project_query'
    TOOLS[name].model.model_validate(call)


def test_mutations_keep_their_permission_boundary():
    assert public_call('operations_cancel', {'operation_id': 'original'})[0] == 'process'
    assert public_call('open_workspace', {'project': 'Visible', 'capture_baseline': True})[0] == 'workspace'
    definitions = {t['name']: t for t in tool_definitions()}
    for name in ['workspace', 'process', 'write', 'edit', 'exec']:
        assert definitions[name]['annotations']['readOnlyHint'] is False
    for operation in ['cancel', 'validate', 'search_start']:
        with pytest.raises(ValidationError):
            TOOLS['task_query'].model.model_validate({'operation': operation})


@pytest.mark.integration
def test_readonly_discovery_and_recovery_over_real_http(tmp_path):
    with running_stack(tmp_path / 'readonly-stack') as s:
        grant = s.must(s.client.post('/api/grants', json={'label': 'readonly-recovery',
            'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
        catalog = {t['name']: t for t in tool_definitions()}
        def call(name, args):
            reply = s.mcp(name, args, token_value=grant['token'])
            assert not reply.get('isError'), reply
            value = reply['structuredContent']
            Draft202012Validator(catalog[name]['outputSchema']).validate(value)
            return value

        for operation, extra in [('help', {}), ('help', {'tool': 'read', 'action': 'lsp'}),
                                 ('tree', {'project': 'ProjectAlpha'}),
                                 ('skills', {'project': 'ProjectAlpha'}),
                                 ('open', {'project': 'ProjectAlpha'})]:
            value = call('project_query', {'operation': operation, **extra})
            if value.get('operation_id'):
                receipt = call('task_query', {'operation': 'wait',
                    'operation_ids': [value['operation_id']], 'wait_seconds': 5})
                assert receipt['operations'][0]['state'] == 'succeeded'

        denied = s.mcp('project_query', {'operation': 'tree', 'project': 'ProjectGamma'},
                       token_value=grant['token'])
        assert denied['isError']
        for args in ({'operation': 'open', 'project': 'ProjectAlpha', 'capture_baseline': True},
                     {'operation': 'open', 'project': 'ProjectAlpha', 'options': {'capture_baseline': True}},
                     {'operation': 'workflow_create', 'project': 'ProjectAlpha'}):
            assert s.mcp('project_query', args, token_value=grant['token'])['isError']
