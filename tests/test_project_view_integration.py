"""Actual loopback Hub/Agent acceptance; all permission changes are fixture-only."""
from copy import deepcopy
import hashlib
import json
import uuid
import pytest
from tests.support import running_stack, wait_for


@pytest.fixture(scope='module')
def view_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('project-view-http')) as stack:
        yield stack


def value(stack, name, arguments, token=None):
    response = stack.mcp(name, arguments, token_value=token)
    assert not response['isError'], response
    data = response['structuredContent']
    if data.get('pending'):
        identifier = data['operation_id']
        def terminal():
            result = stack.mcp('task_query', {'operation': 'wait', 'operation_ids': [identifier],
                'wait_seconds': 5}, token_value=token)
            assert not result['isError'], result
            operation = result['structuredContent']['operations'][0]
            if operation.get('pending'):
                return None
            assert operation['state'] == 'succeeded', operation
            return {'operation_id': identifier, **operation['result']['data']}
        data = wait_for(terminal, 20)
    return data


def query(stack, operation, token=None, **options):
    args = {'project': 'ProjectAlpha', 'operation': operation}
    if options:
        args['options'] = options
    return value(stack, 'project_query', args, token)


def test_real_minimal_open_document_preview_and_original_receipt(view_stack):
    s = view_stack
    (s.projectalpha/'MEMORY.md').write_text('Fixture-only project rules')
    minimal = query(s, 'open')
    assert minimal['mcp_view_version'] == 1
    assert 'execution' not in minimal['context'] and 'tasks' not in minimal['context']
    assert all('content' not in item for item in minimal['context']['documents'])
    assert any(item['path'] == 'MEMORY.md' and item['state'] == 'available'
               for item in minimal['context']['instruction_index'])
    detailed = query(s, 'open', context_detail='documents')
    assert any('Fixture-only project rules' in item.get('content', '') for item in detailed['context']['documents'])
    assert 'execution' not in detailed['context']
    poll = s.mcp('task_query', {'operation': 'get', 'operation_ids': [minimal['operation_id']]})
    assert not poll['isError'], poll
    row = poll['structuredContent']['operations'][0]
    assert 'device_id' not in row
    assert 'content' not in row['result']['data']['context']['documents'][0]
    original = s.poll(minimal['operation_id'])
    assert original['device_id'] == s.device
    assert original['result']['data']['workspace']['root'] == str(s.projectalpha)


def test_real_file_roundtrip_without_command_or_computer_permission(view_stack):
    s = view_stack
    original = {key: s.project[key] for key in ('alias', 'device_id', 'root', 'description', 'mode', 'allow_tasks')}
    grant = s.must(s.client.post('/api/grants', json={'label': 'File-only fixture',
        'scopes': ['read', 'write'], 'projects': [s.project['id']], 'days': 1}))
    try:
        s.must(s.client.put('/api/projects/'+s.project['id'], json={**original, 'allow_tasks': False}))
        ready = query(s, 'readiness', grant['token'], operation_kind='file_write')
        assert ready['codepier_authorization']['state'] == 'allowed', ready
        assert ready['execution_readiness']['state'] == 'ready'
        assert ready['caller_platform_decision']['state'] == 'not_observed'
        assert ready['operation_evidence'] is None
        path = 'src/view-'+uuid.uuid4().hex+'.txt'
        create = {'project': 'ProjectAlpha', 'path': path, 'content': 'first\n',
                  'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex}
        first = value(s, 'write', create, grant['token'])
        replay = value(s, 'write', create, grant['token'])
        assert first['operation_id'] == replay['operation_id']
        read = value(s, 'read', {'project': 'ProjectAlpha', 'path': path}, grant['token'])
        assert read['content'] == 'first\n' and read['sha256'] == hashlib.sha256(b'first\n').hexdigest()
        edited = value(s, 'edit', {'project': 'ProjectAlpha', 'path': path,
            'expected_sha256': read['sha256'], 'edits': [{'old_text': 'first', 'new_text': 'second'}],
            'idempotency_key': uuid.uuid4().hex}, grant['token'])
        readback = value(s, 'read', {'project': 'ProjectAlpha', 'path': path}, grant['token'])
        assert readback['content'] == 'second\n' and readback['sha256'] == edited['sha256']
        conflict = s.mcp('write', {**create, 'idempotency_key': uuid.uuid4().hex}, token_value=grant['token'])
        assert conflict['isError'] and conflict['structuredContent']['error']['code'] == 'SHA_CONFLICT'
        denied = s.mcp('exec', {'project': 'ProjectAlpha', 'command': 'echo fixture',
            'idempotency_key': uuid.uuid4().hex}, token_value=grant['token'])
        assert denied['isError'] and denied['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'
        command = query(s, 'readiness', operation_kind='command_exec')
        assert command['codepier_authorization']['state'] == 'denied'
        assert command['codepier_authorization']['code'] == 'TASKS_DISABLED'
    finally:
        s.must(s.client.put('/api/projects/'+s.project['id'], json=original))
        s.must(s.client.delete('/api/grants/'+grant['grant_id']))


def test_real_readonly_caller_gets_action_denial_not_false_ready(view_stack):
    s = view_stack
    grant = s.must(s.client.post('/api/grants', json={'label': 'Read-only fixture',
        'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
    try:
        result = query(s, 'readiness', grant['token'], operation_kind='file_write')
        assert result['codepier_authorization']['state'] == 'denied', result
        assert result['caller_platform_decision']['state'] == 'not_observed'
        assert 'execution' not in result and 'local_authorization' not in result
    finally:
        s.must(s.client.delete('/api/grants/'+grant['grant_id']))


def test_real_old_context_cannot_reappear_after_mapping_change(view_stack):
    s = view_stack
    opened = query(s, 'open')
    original = {key: s.project[key] for key in ('alias', 'device_id', 'root', 'description', 'mode', 'allow_tasks')}
    try:
        s.must(s.client.put('/api/projects/'+s.project['id'], json={**original, 'root': str(s.projectgamma)}))
        result = s.mcp('task_query', {'operation': 'get', 'operation_ids': [opened['operation_id']]})
        assert result['isError'], result
        row = result['structuredContent']['operations'][0]
        assert row['error']['code'] == 'PROJECT_CONTEXT_CHANGED'
        assert 'result' not in row
    finally:
        s.must(s.client.put('/api/projects/'+s.project['id'], json=original))


def test_real_authorization_filtered_search_resource_and_management(view_stack):
    s = view_stack
    grant = s.must(s.client.post('/api/grants', json={'label': 'Single project fixture',
        'scopes': ['read'], 'projects': [s.project['id']], 'days': 1}))
    try:
        page = value(s, 'project_query', {'operation': 'list', 'query': 'project', 'limit': 1}, grant['token'])
        assert page['total'] == 1 and page['next_offset'] is None
        assert page['projects'][0]['id'] == s.project['id']
        assert all('device_id' not in row for row in page['projects'])
        empty = value(s, 'project_query', {'operation': 'list', 'query': 'Gamma'}, grant['token'])
        assert empty['total'] == 0 and empty['projects'] == []
        resource = s.rpc('resources/read', {'uri': 'rd://projects'}, token_value=grant['token']).json()['result']
        rows = json.loads(resource['contents'][0]['text'])
        assert [row['id'] for row in rows] == [s.project['id']]
        assert 'device_id' not in rows[0]
        # A project-scoped token is not a device-management grant.
        denied = s.mcp('workspace', {'operation': 'devices'}, token_value=grant['token'])
        assert denied['isError']
        management = s.must(s.client.get('/api/devices'))  # Panel's own device-management API.
        assert any(row['id'] == s.device for row in management['devices'])
    finally:
        s.must(s.client.delete('/api/grants/'+grant['grant_id']))


def test_project_policy_change_invalidates_mcp_context_but_preserves_panel_history(view_stack):
    s = view_stack
    opened = query(s, 'open')
    original = {key: s.project[key] for key in ('alias', 'device_id', 'root', 'description', 'mode', 'allow_tasks')}
    try:
        s.must(s.client.put('/api/projects/'+s.project['id'], json={**original, 'allow_tasks': False}))
        result = s.mcp('task_query', {'operation': 'get', 'operation_ids': [opened['operation_id']]})
        assert result['isError']
        assert result['structuredContent']['operations'][0]['error']['code'] == 'PROJECT_CONTEXT_CHANGED'
        historical = s.poll(opened['operation_id'])
        assert historical['state'] == 'succeeded'
        assert historical['result']['data']['workspace']['allow_tasks']
    finally:
        s.must(s.client.put('/api/projects/'+s.project['id'], json=original))
