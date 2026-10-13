"""Real localhost Hub + disposable Agent prove room permission enforcement.

All project roots, credentials and shell settings belong to collaboration_stack.
No production grant, filesystem or remote SSH target is touched.
"""
from pathlib import Path

import pytest

from shared.util import atomic_json
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_dots_flow import create_dot, join_dot, mcp, overview
from tests.test_collaboration_delivery_flow import scope

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_added_project_permissions_complete_real_read_write_exec_in_only_selected_project(collaboration_stack):
    stack = collaboration_stack
    primary, secondary, excluded = stack.projects[0], stack.projects[1], stack.projects[2]
    # Enable only this disposable fixture Agent's existing /bin/sh executor.
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': [secondary['alias']], 'command': ['/bin/sh', '-c']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    joined = join_dot(stack, create_dot(stack, label='Room permission fixture'))
    room_id = joined['slot']['conversation_id']
    raw = {'conversation_id': room_id, 'dot_id': joined['slot']['id'], 'projects': [
        {'project': primary['id'], 'environment_id': 'production',
         'capabilities': ['read'], 'execution_targets': ['project_agent']},
        {'project': secondary['id'], 'environment_id': 'production',
         'capabilities': ['read', 'write', 'execute'], 'execution_targets': ['project_agent']},
    ]}
    preview = stack.must(stack.client.post('/api/collaboration/conversation-projects-preview', json=raw))
    assert preview['ready'], preview
    saved = stack.must(stack.client.post('/api/collaboration/conversation-projects', json={
        **raw, 'expected_version': preview['conversation_version'], 'expected_scope_version': preview['scope_version'],
        'selection_digest': preview['selection_digest'], 'confirm_scope': True, 'confirm_unsandboxed_exec': True,
        'idempotency_key': key(),
    }))
    assert saved['applied'] and not saved['changes_underlying_grant']
    assert {p['project_id'] for p in overview(stack)['conversation']['projects']} == {primary['id'], secondary['id']}
    created = stack.must(stack.client.post('/api/collaboration/goal-create', json={
        **scope(stack), 'conversation_id': room_id, 'objective': 'Operate only on the newly added B fixture.',
        'acceptance': 'Verify actual read write and exec receipts', 'project_ids': [secondary['id']],
        'participant_grant_ids': [stack.grant], 'capabilities': ['read', 'write', 'execute'], 'idempotency_key': key(),
    }))['goal']
    approved = stack.must(stack.client.post('/api/collaboration/goal-approve', json={
        **scope(stack), 'goal_id': created['id'], 'expected_version': created['version'],
        'digest': created['digest'], 'idempotency_key': key(),
    }))['goal']
    item = mcp(stack, 'collaboration_work', {
        'action': 'create', **scope(stack), 'goal_id': approved['id'], 'target_project_id': secondary['id'],
        'objective': 'Write read and execute only inside B.', 'acceptance': 'Actual Agent receipts',
        'assignee_grant_id': stack.grant, 'required_capabilities': ['read', 'write', 'execute'],
        'idempotency_key': key(),
    })['work_item']
    leased = mcp(stack, 'collaboration_work', {
        'action': 'claim', **scope(stack), 'goal_id': approved['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key(),
    })['work_item']
    lease = {**scope(stack), 'goal_id': approved['id'], 'work_item_id': leased['id'],
             'attempt': leased['attempt'], 'fencing_token': leased['fencing_token']}
    filename, content = 'room-permission-proof.txt', 'isolated-room-permission-proof\n'
    operations = []
    for tool, arguments in [
        ('write', {'path': filename, 'content': content, 'expected_sha256': 'new'}),
        ('read', {'path': filename}),
        ('exec', {'command': 'printf isolated-room-exec-proof', 'target': 'agent', 'timeout_seconds': 15}),
    ]:
        request = {'action': 'execute', **lease, 'tool': tool, 'arguments': arguments, 'idempotency_key': key()}
        operation = mcp(stack, 'collaboration_work', request)['operation_id']
        assert mcp(stack, 'collaboration_work', request)['operation_id'] == operation
        settled = stack.poll(operation, timeout=30)
        assert settled['state'] == 'succeeded', settled
        if tool == 'read':
            assert settled['result']['data']['content'] == content
        if tool == 'exec':
            assert settled['result']['data']['exit_code'] == 0
            assert 'isolated-room-exec-proof' in settled['output']
        operations.append(operation)
    assert (Path(secondary['root']) / filename).read_text() == content
    assert not (Path(primary['root']) / filename).exists()
    assert not (Path(excluded['root']) / filename).exists()
    finished = mcp(stack, 'collaboration_work', {
        'action': 'result', **lease, 'outcome': 'succeeded', 'summary': 'Actual isolated Agent receipts verified',
        'operation_ids': operations, 'idempotency_key': key(),
    })
    assert finished
