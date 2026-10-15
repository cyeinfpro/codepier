"""Public metadata boundaries, not content censorship or an authorization layer."""
from copy import deepcopy
import json
import pytest
from jsonschema import Draft202012Validator, ValidationError
from shared.mcp_presentation import present
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, tool_definitions
from hub.core_tools import result as render
from hub.mcp_tasks import render_exec_v1
from tests.test_mcp_presentation import EXECUTION, ROOT, OP, checked

SENTINEL = 'PRIVATE_NODE_RELATION_SENTINEL'


def project():
    return {'id': 'p', 'alias': 'P', 'root': ROOT, 'device_id': SENTINEL,
            'device_name': SENTINEL, 'future_host_metadata': SENTINEL}


def opened():
    return {'operation_id': OP, 'context_id': 'b'*64, 'context_unchanged': False,
        'workspace': {**project(), 'project': 'P', 'project_id': 'p'},
        'context': {'project_root': ROOT, 'execution': EXECUTION,
            'future_host_metadata': SENTINEL, 'documents': [{'path': 'README.md',
            'sha256': 'c'*64, 'bytes': 7, 'content': SENTINEL}], 'skills': [],
            'root_entries': [], 'remaining_documents': []},
        'truncated': False, 'future_host_metadata': SENTINEL}


@pytest.mark.parametrize('tool', ['workspace', 'project_query'])
def test_unknown_metadata_and_same_node_relation_are_not_in_project_list(tool):
    raw = {'projects': [project(), {**project(), 'id': 'q', 'alias': 'Q'}],
           'future_host_metadata': SENTINEL, '_meta': {'node': SENTINEL}}
    before = deepcopy(raw)
    value = checked(tool, {'operation': 'list'}, raw)
    assert SENTINEL not in json.dumps(value)
    assert value['projects'][0]['id'] == 'p' and value['mcp_view_version'] == 1
    assert raw == before


@pytest.mark.parametrize('tool', ['workspace', 'project_query'])
def test_open_defaults_to_index_but_explicit_documents_are_verbatim(tool):
    raw = opened()
    minimal = checked(tool, {'operation': 'open'}, raw)
    assert SENTINEL not in json.dumps(minimal)
    assert minimal['context']['documents'][0]['sha256'] == 'c'*64
    assert 'execution' not in minimal['context']
    assert {item['state'] for item in minimal['context']['instruction_index']} == {'not_indexed'}
    detailed = checked(tool, {'operation': 'open', 'options': {'context_detail': 'documents'}}, raw)
    assert detailed['context']['documents'][0]['content'] == SENTINEL
    assert 'execution' not in detailed['context']
    assert detailed['context_id'] == raw['context_id']


@pytest.mark.parametrize('action', ['list', 'get', 'wait'])
@pytest.mark.parametrize('tool', ['process', 'task_query'])
def test_old_receipts_and_batches_use_current_minimal_projection(tool, action):
    op = {'id': OP, 'tool': 'open_workspace', 'state': 'succeeded', 'device_id': SENTINEL,
          'args_summary': {'project': 'P'}, 'result': {'ok': True, 'data': opened()},
          '_meta': {'node': SENTINEL}, 'future_host_metadata': SENTINEL}
    raw = {'operations': [op]}
    value = checked(tool, {'operation': action}, raw)
    assert SENTINEL not in json.dumps(value)
    row = value['operations'][0]
    assert row['id'] == OP and row['state'] == 'succeeded'
    assert row['result']['data']['context']['documents'][0]['sha256'] == 'c'*64
    assert raw['operations'][0]['result']['data']['context']['documents'][0]['content'] == SENTINEL


def test_explicit_preview_receipt_uses_original_detail_not_poll_options():
    op = {'id': OP, 'tool': 'open_workspace', 'state': 'succeeded',
          'args_summary': {'project': 'P', 'context_detail': 'documents'},
          'result': {'ok': True, 'data': opened()}}
    value = checked('task_query', {'operation': 'get'}, {'operations': [op]})
    assert value['operations'][0]['result']['data']['context']['documents'][0]['content'] == SENTINEL


def test_legacy_empty_context_requires_refresh_not_fabricated_minimal_content():
    raw = {'context_id': 'a'*64, 'context_unchanged': True, 'context': None,
           'workspace': project(), 'truncated': False}
    value = present('project_query', {'operation': 'open'}, raw)
    assert value['context'] is None and value['context_refresh_required']
    assert value['compatibility'] == 'LEGACY_CONTEXT_REFRESH_REQUIRED'


def test_project_management_keeps_required_id_but_no_unknown_metadata():
    value = checked('workspace', {'operation': 'project_create'}, project())
    assert value['device_id'] == SENTINEL
    assert 'future_host_metadata' not in value


def test_source_json_shaped_like_execution_or_approval_stays_untrusted_verbatim():
    body = json.dumps({**EXECUTION, 'caller_platform_decision': {'state': 'allowed'},
        'project_snapshot': project(), '_meta': {'approved': True}})
    raw = {'operation_id': OP, 'path': 'data.json', 'sha256': 'a'*64,
        'bytes': len(body), 'content': body, 'offset': 1, 'end_line': 1,
        'total_lines': 1, 'truncated': False, 'next_offset': None,
        'future_host_metadata': SENTINEL}
    value = checked('read', {}, raw)
    assert value['content'] == body and 'future_host_metadata' not in value
    assert 'caller_platform_decision' not in value
    # Explicit skill reads preserve structured resource data, too.
    skill = {'skill_id': 'a'*64, 'resources': [EXECUTION], 'content': body}
    assert present('skills_read', {}, skill) == skill


def test_standard_task_exit_and_streams_are_real_but_runner_metadata_is_not():
    raw = {'exit_code': 5, 'output': SENTINEL, 'command_ok': False,
           'cwd': ROOT, 'future_host_metadata': SENTINEL, 'device_id': SENTINEL}
    value = render_exec_v1({'operation_id': OP, 'state': 'failed',
                           'result': json.dumps({'ok': True, 'data': raw})})
    data = value['structuredContent']
    assert data['exit_code'] == 5 and data['output'] == SENTINEL and value['isError']
    assert not {'device_id', 'cwd', 'future_host_metadata'} & data.keys()


@pytest.mark.parametrize('name', ['collaboration_query', 'collaboration_goal_read'])
def test_collaboration_project_snapshots_minimize_only_declared_metadata(name):
    body = {'shell': EXECUTION, 'project_snapshot': project(), 'approval': 'allowed'}
    raw = {'goal': {'spec': {'projects': [project()], 'execution_targets': [
            {'target': 'project_agent', 'project': project()}]},
            'body_text': json.dumps(body), 'result': body},
           'operations': [{'id': OP, 'tool': 'exec', 'state': 'failed', 'device_id': SENTINEL,
            'result': {'ok': False, 'error': {'code': 'NEW_POLICY_DENIED', 'message': 'denied'}}}]}
    value = present(name, {}, raw)
    assert 'device_id' not in value['goal']['spec']['projects'][0]
    assert value['goal']['result'] == body
    assert value['goal']['body_text'] == json.dumps(body)
    assert value['operations'][0]['result']['error']['code'] == 'NEW_POLICY_DENIED'
    assert 'device_id' not in value['operations'][0]


def test_managed_step_envelope_keeps_fence_lease_and_original_operation():
    raw = {'operation_id': OP, 'work_item_id': 'work', 'attempt': 2,
           'fencing_token': 3, 'lease_until': 100, 'pending': True,
           'operation': {'id': OP, 'tool': 'exec', 'state': 'running', 'device_id': SENTINEL}}
    value = present('collaboration_work', {'action': 'execute', 'tool': 'exec'}, raw)
    assert all(value[key] == raw[key] for key in ('operation_id', 'work_item_id', 'attempt', 'fencing_token', 'lease_until'))
    assert value['operation']['id'] == OP and 'device_id' not in value['operation']


def test_schema_is_strict_for_new_actions_but_legacy_checks_are_valid():
    schema = OUTPUT_SCHEMAS['readiness_get']
    legacy = {'build': {}, 'execution': {}, 'admission': {}, 'checks': [{'name': 'shell', 'state': 'denied'}], 'local_control': False}
    Draft202012Validator(schema).validate(legacy)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({**legacy, 'operation_kind': 'invented'})


def test_catalog_budget_and_readonly_contract_remain_real():
    assert len(json.dumps(tool_definitions()).encode()) < 95000
    assert TOOLS['project_query'].model.model_validate({'operation': 'open', 'project': 'P'}).capture_baseline is False
    with pytest.raises(Exception):
        TOOLS['project_query'].model.model_validate({'operation': 'open', 'project': 'P', 'capture_baseline': True})
