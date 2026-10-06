"""Default discovery minimizes host metadata without corrupting work evidence."""
from copy import deepcopy
import json
import uuid

import pytest
from jsonschema import Draft202012Validator, ValidationError

from hub.core_tools import result as core_result
from hub.mcp_tasks import render_exec_v1
from shared.contracts import OUTPUT_SCHEMAS, tool_definitions
from shared.mcp_presentation import error_view, node_label, present
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from shared.util import DevError
from tests.test_audit_api import api, rpc

ROOT = '/Users/private-account/Projects/example'
OP = 'a' * 32
EXECUTION = {
    'operation_id': OP, 'platform': 'Darwin', 'user': 'private-account', 'uid': 501,
    'project_root': ROOT, 'credential_environment_present': {'GH_TOKEN': True},
    'hostname': 'private-host', 'future_host_metadata': {'home': ROOT},
    'tool_lookup': 'Agent process PATH',
    'execution_policy': {'os_sandbox': False, 'effective_block_codex': True},
    'shell': {'enabled': False, 'configured_enabled': True, 'command': ['/bin/zsh', '-lc'],
        'executable_available': True, 'caller_permitted': False, 'effective_ready': False,
        'denial': {'code': 'DENIED', 'message': 'Not allowed'}, 'environment_keys': ['PRIVATE_ENV'],
        'inherit_env': True, 'interactive': False, 'max_timeout_seconds': 600},
    'tools': {'git': '/usr/bin/git', 'uv': '/Users/private-account/.local/bin/uv', 'sshpass': None},
}


def schema(name):
    return next(item['outputSchema'] for item in tool_definitions() if item['name'] == name)


def checked(name, arguments, value):
    before = deepcopy(value)
    result = core_result(name, arguments, value)
    assert value == before, 'MCP presentation must not mutate the durable/panel value'
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    Draft202012Validator(schema(name)).validate(result['structuredContent'])
    return result['structuredContent']


@pytest.mark.parametrize('name', ['workspace', 'project_query'])
def test_status_preserves_actual_permissions_and_exposes_capabilities_only(name):
    value = checked(name, {'operation': 'status'}, EXECUTION)
    serialized = json.dumps(value)
    for private in ('private-account', 'private-host', 'future_host_metadata', 'Darwin', 'GH_TOKEN', 'PRIVATE_ENV', '/usr/bin/git', '/bin/zsh'):
        assert private not in serialized
    assert value['project_root'] == '.'
    assert value['tools'] == {'git': True, 'uv': True, 'sshpass': False}
    assert value['shell']['executable'] == 'zsh'
    assert value['shell']['enabled'] is False and value['shell']['caller_permitted'] is False
    assert value['shell']['denial']['code'] == 'DENIED'
    assert value['execution_policy'] == EXECUTION['execution_policy']
    assert 'not restricted' in value['shell']['filesystem_scope']
    assert present(name, {'operation': 'status'}, value) == value
    invalid = deepcopy(value)
    invalid['tools']['git'] = '/host/path'
    with pytest.raises(ValidationError):
        Draft202012Validator(schema(name)).validate(invalid)
    # Internal wire contract still describes the actual Agent response.
    Draft202012Validator(OUTPUT_SCHEMAS['execution_info']).validate(EXECUTION)


@pytest.mark.parametrize('name', ['workspace', 'project_query'])
def test_open_masks_metadata_but_keeps_source_previews_sha_and_context_id(name):
    source = 'uid = 501\nroot = "' + ROOT + '"\n'
    raw = {'operation_id': OP, 'context_id': 'b' * 64, 'context_unchanged': False,
        'workspace': {'project': 'Example', 'project_id': 'project', 'root': ROOT},
        'context': {'project_root': ROOT, 'execution': EXECUTION,
            'documents': [{'path': 'README.md', 'content': source, 'sha256': 'c' * 64}],
            'codex_skills': [{'skill_id': 'd' * 64, 'source': 'codex',
                'path': '/Users/private-account/.codex/skills/check/SKILL.md'}]},
        'truncated': False, 'baseline_ref': 'e' * 32}
    value = checked(name, {'operation': 'open'}, raw)
    assert value['workspace']['root'] == value['context']['project_root'] == '.'
    assert value['context']['documents'] == raw['context']['documents']
    assert value['context']['codex_skills'][0]['path'] == 'SKILL.md'
    assert value['context_id'] == raw['context_id'] and value['baseline_ref'] == raw['baseline_ref']


def test_discovery_devices_worktrees_tasks_and_skill_summaries_have_no_host_paths():
    raw = {'projects': [{'id': 'project', 'alias': 'Example', 'root': ROOT,
                         'device_id': 'device', 'device_name': 'Private MacBook'}],
        'devices': [{'id': 'device', 'name': 'Private MacBook', 'platform': 'Darwin',
                     'hostname': 'private-host', 'online': True}],
        'warnings': ['Private MacBook needs restart'],
        'workspaces': [{'workspace_id': OP, 'base_commit': 'b' * 40,
                        'path': '/Users/private-account/Worktrees/one', 'identity': [10, 20]}],
        'tasks': [{'name': 'test', 'cwd': 'src', 'command': ['/Users/private-account/bin/test'],
                   'environment_keys': ['PRIVATE_ENV'], 'executable_available': True}],
        'skills': [{'skill_id': 'd' * 64, 'source': 'codex', 'source_root': ROOT,
                    'skill_dir': ROOT, 'path': ROOT + '/SKILL.md'}],
        'sources': [{'source': 'codex', 'path': ROOT, 'scope': 'user'}]}
    original = deepcopy(raw)
    value = present('workspace', {}, raw)
    assert raw == original
    assert ROOT not in json.dumps(value) and 'Private MacBook' not in json.dumps(value)
    assert value['devices'][0]['name'] == value['projects'][0]['device_name'] == node_label('device')
    assert value['warnings'] == [node_label('device') + ' needs restart']
    assert value['tasks'] == [{'name': 'test', 'cwd': 'src', 'executable_available': True}]
    assert value['workspaces'][0]['path'] == '.' and 'identity' not in value['workspaces'][0]
    assert value['skills'][0]['skill_id'] == 'd' * 64 and value['skills'][0]['path'] == 'SKILL.md'


def test_explicit_skill_resource_locations_survive_direct_reads_and_polling():
    raw = {'operation_id': OP, 'skill_id': 'd' * 64, 'source': 'codex', 'sha256': 'e' * 64,
        'content': '# Inspect this script first', 'truncated': False, 'next_offset': None,
        'skill_dir': ROOT, 'local_path': ROOT + '/SKILL.md', 'path': ROOT + '/SKILL.md',
        'execution': {'cwd': ROOT, 'project_root': ROOT, 'automatic': False}}
    value = checked('workspace', {'operation': 'skill'}, raw)
    assert value == raw
    poll = checked('process', {'operation': 'get'}, {'operations': [
        {'id': OP, 'tool': 'skills_read', 'state': 'succeeded', 'result': {'ok': True, 'data': raw}}]})
    assert poll['operations'][0]['result']['data'] == raw


@pytest.mark.parametrize('name', ['process', 'task_query'])
def test_polled_status_is_projected_and_streams_remain_verbatim(name):
    raw = {'operations': [{'id': OP, 'tool': 'execution_info', 'state': 'succeeded',
        'args_summary': {'cwd': ROOT, 'workspace_id': OP}, 'output': ROOT + '\n', 'output_seq': 3,
        'result': {'ok': True, 'data': EXECUTION}}]}
    value = checked(name, {'operation': 'get'}, raw)['operations'][0]
    assert value['args_summary'] == {'workspace_id': OP}
    assert value['output'] == ROOT + '\n' and value['output_seq'] == 3
    assert 'private-account' not in json.dumps(value['result']['data'])


def test_exec_and_standard_task_keep_exit_and_output_but_drop_runner_argv():
    raw = {'operation_id': OP, 'exit_code': 7, 'output': 'observed ' + ROOT,
        'command_ok': False, 'cwd': ROOT, 'shell': ['/bin/zsh', '-lc'],
        'command': ['/Users/private-account/bin/tool', '--check'], 'duration_ms': 15}
    value = checked('exec', {}, raw)
    assert value['output'] == raw['output'] and value['exit_code'] == 7
    assert not {'cwd', 'shell', 'command'} & value.keys()
    snapshot = {'operation_id': OP, 'state': 'failed', 'result': json.dumps({'ok': True, 'data': raw})}
    before = deepcopy(snapshot)
    task = render_exec_v1(snapshot)
    assert snapshot == before and task['isError'] is True
    assert task['structuredContent'] == value
    failed = render_exec_v1({'operation_id': OP, 'state': 'failed', 'result': json.dumps({
        'ok': False, 'error': {'code': 'FAILED', 'message': 'Cannot read ' + ROOT + '/file'}})})
    assert 'private-account' not in json.dumps(failed) and failed['isError']


@pytest.mark.parametrize('path', [ROOT, '/home/private-account/project', r'C:\Users\Private Account\project'])
def test_errors_hide_home_prefix_without_changing_source_or_codes(path):
    value = {'code': 'SHA_CONFLICT', 'message': 'Cannot read "' + path + '/file.txt"',
             'path': path, 'content': path, 'output': path, 'diff': '+' + path}
    shown = error_view(value)
    assert shown['code'] == 'SHA_CONFLICT'
    assert '[account-home]' in shown['message'] and shown['path'].startswith('[account-home]')
    assert shown['content'] == shown['output'] == path and shown['diff'] == '+' + path


def test_file_contents_vps_identity_profile_and_call_arguments_are_preserved():
    content = json.dumps(EXECUTION)
    file = {'operation_id': OP, 'path': 'fixture.json', 'content': content, 'sha256': 'a' * 64,
        'bytes': len(content), 'offset': 1, 'end_line': 1, 'total_lines': 1,
        'truncated': False, 'next_offset': None}
    assert checked('read', {}, file) == file
    assert present('get_profile', {}, {'user': 'real-account'}) == {'user': 'real-account'}
    value = present('vps', {}, {'vps': [{'host': 'example.invalid', 'username': 'root',
        'target': 'vps:' + OP, 'projects': [{'id': 'project', 'alias': 'Example', 'root': ROOT}]}]})
    assert value['vps'][0]['host'] == 'example.invalid' and value['vps'][0]['username'] == 'root'
    assert value['vps'][0]['projects'][0]['root'] == '.'
    args = {'command': 'printf ' + ROOT, 'cwd': ROOT}
    assert present('exec', {}, {'next_call': {'arguments': args}})['next_call']['arguments'] == args


@pytest.mark.parametrize('modern', [False, True])
def test_http_discovery_resources_and_errors_use_one_projection(api, monkeypatch, modern):
    app, client, pat = api
    app.state.store.execute('UPDATE projects SET root=?', (ROOT,))
    app.state.store.execute('UPDATE devices SET name=?', ('Private MacBook',))
    def call(method, params):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': deepcopy(params)}
        if not modern:
            return rpc(client, pat, body).json()['result']
        body['params']['_meta'] = {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}
        return client.post('/mcp', json=body, headers={
            'Authorization': 'Bearer ' + pat, 'Accept': 'application/json, text/event-stream',
            **request_headers(body)}).json()['result']
    for name in ('workspace', 'project_query'):
        result = call('tools/call', {'name': name, 'arguments': {}})
        assert ROOT not in json.dumps(result) and 'Private MacBook' not in json.dumps(result)
        assert result['structuredContent']['projects'][0]['root'] == '.'
        Draft202012Validator(schema(name)).validate(result['structuredContent'])
    resource = call('resources/read', {'uri': 'rd://projects'})
    assert json.loads(resource['contents'][0]['text'])[0]['root'] == '.'
    assert client.get('/api/projects').json()['projects'][0]['root'] == ROOT
    async def failed(*args):
        raise DevError('EXPECTED_FAILURE', 'Cannot access ' + ROOT + '/file', 409)
    monkeypatch.setattr(app.state.runtime, 'invoke', failed)
    error = call('tools/call', {'name': 'project_query', 'arguments': {}})
    assert error['isError'] and 'private-account' not in json.dumps(error)
    assert error['structuredContent']['error']['code'] == 'EXPECTED_FAILURE'


def test_real_agent_discovery_receipts_and_panel_keep_distinct_views(stack):
    def tool(name, args):
        response = stack.mcp(name, args)
        assert not response['isError'], response
        value = response['structuredContent']
        if value.get('pending'):
            polled = stack.mcp('task_query', {'operation': 'wait',
                'operation_ids': [value['operation_id']], 'wait_seconds': 5})
            operation = polled['structuredContent']['operations'][0]
            assert operation['state'] == 'succeeded', operation
            value = {'operation_id': operation['id'], **operation['result']['data']}
        Draft202012Validator(schema(name)).validate(value)
        return value
    for name, action in [('workspace', 'open'), ('project_query', 'status'),
                         ('workspace', 'readiness'), ('project_query', 'tasks')]:
        value = tool(name, {'operation': action, 'project': 'Imago'})
        # Fixture source previews may intentionally mention their own paths.
        metadata = deepcopy(value)
        if metadata.get('context'):
            metadata['context']['documents'] = []
        assert str(stack.imago) not in json.dumps(metadata)
    result = tool('exec', {'project': 'Imago', 'task': 'smoke', 'yield_seconds': 0,
                           'idempotency_key': uuid.uuid4().hex})
    assert result['exit_code'] == 0 and 'PASS: local task completed' in result['output']
    assert 'command' not in result
    original = stack.poll(result['operation_id'])['result']['data']
    assert original['command'] and original['output'] == result['output']
    assert stack.call('projects_resolve', {'project': 'Imago'})['root'] == str(stack.imago)
