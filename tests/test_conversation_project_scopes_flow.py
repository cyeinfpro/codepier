"""Disposable HTTP Hub/Agent integration; no real dot host or production grant."""
import pytest

from shared.util import atomic_json
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_dots_flow import create_dot, join_dot, mcp, call, send, scope, overview

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def approval(stack, joined):
    project = stack.projects[1]
    body = {'project': project['id'], 'environment_id': 'production',
            'conversation_id': joined['slot']['conversation_id']}
    preview = stack.must(stack.client.post('/api/collaboration/conversation-project-preview', json=body))
    assert preview['ready'], preview
    return {**body, 'expected_version': preview['scope']['conversation_version'],
            'authorization': {'dot_id': preview['scope']['dot_id'], 'scope_digest': preview['scope_digest'],
                              'confirm_scope': True}, 'idempotency_key': key()}


def test_http_exact_scope_csrf_idempotency_and_revoke(collaboration_stack):
    stack = collaboration_stack
    joined = join_dot(stack, create_dot(stack))
    args = approval(stack, joined)
    assert stack.client.post('/api/collaboration/conversation-project', json=args,
                             headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    assert len(overview(stack)['conversation']['projects']) == 1
    result = stack.must(stack.client.post('/api/collaboration/conversation-project', json=args))
    assert result['execution_authorized'] and len(result['conversation']['projects']) == 2
    assert stack.must(stack.client.post('/api/collaboration/conversation-project', json=args)) == result
    restored = mcp(stack, 'collaboration_query', {'action': 'dot_connection', **scope(stack),
                                               'dot_id': joined['slot']['id']})
    assert restored['subscription_requests'] == joined['subscription_requests']
    assert restored['inbox_request'] == joined['inbox_request']
    assert restored['slot']['policy_version'] == joined['slot']['policy_version']
    ref = result['authorization']
    control = {'conversation_id': args['conversation_id'], 'scope_id': ref['id'],
               'expected_version': ref['version'], 'idempotency_key': key()}
    assert stack.client.post('/api/collaboration/conversation-project-scope-control', json=control,
                             headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    revoked = stack.must(stack.client.post('/api/collaboration/conversation-project-scope-control', json=control))
    assert revoked['revoked']
    assert stack.client.post('/api/collaboration/conversation-project', json=args).status_code == 409


def test_new_project_real_agent_file_command_and_original_thread_result(collaboration_stack):
    stack = collaboration_stack
    # Only this disposable Agent config and temporary fixture projects change.
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': [stack.project['alias'], stack.projects[1]['alias']],
                             'command': ['/bin/sh', '-c']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    joined = join_dot(stack, create_dot(stack))
    prior = send(stack, joined, '原项目的旧任务必须保持原范围。')
    args = approval(stack, joined)
    added = stack.must(stack.client.post('/api/collaboration/conversation-project', json=args))
    source = send(stack, joined, '在新增项目创建一个合成验收文件并核对实际命令回执。')
    inbox = call(stack, joined['inbox_request'])
    item = next(row for row in inbox['items'] if row['delegation_id'] == source['delegation_id'])
    evidence = call(stack, item['read_request'])
    assert evidence['trusted_author']['authenticated']
    assert evidence['request_scope']['project_scope_approvals'] == [added['authorization']]
    assert set(evidence['request_scope']['project_ids']) == {stack.project['id'], stack.projects[1]['id']}
    old = next(row for row in inbox['items'] if row['delegation_id'] == prior['delegation_id'])
    old_evidence = call(stack, old['read_request'])
    assert old_evidence['goal']['project_ids'] == [stack.project['id']]
    work = mcp(stack, 'collaboration_work', {
        'action': 'create', **scope(stack), 'goal_id': source['goal_id'],
        'target_project_id': stack.projects[1]['id'], 'objective': 'Verify added project',
        'acceptance': 'Real file and command receipt', 'responsibility': 'fixture verification',
        'assignee_grant_id': stack.grant, 'required_capabilities': ['read', 'write', 'execute'],
        'idempotency_key': key()})['work_item']
    claimed = mcp(stack, 'collaboration_work', {
        'action': 'claim', **scope(stack), 'goal_id': source['goal_id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key()})['work_item']
    lease = {**scope(stack), 'goal_id': source['goal_id'], 'work_item_id': work['id'],
             'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token']}
    operations = []
    for tool, arguments in [
        ('write', {'path': 'explicit-project-scope.txt', 'content': 'synthetic scope verified\n', 'expected_sha256': 'new'}),
        ('read', {'path': 'explicit-project-scope.txt'}),
        ('exec', {'command': 'cat explicit-project-scope.txt', 'timeout_seconds': 15}),
    ]:
        admitted = mcp(stack, 'collaboration_work', {
            'action': 'execute', **lease, 'tool': tool, 'arguments': arguments, 'idempotency_key': key()})
        receipt = stack.poll(admitted['operation_id'], timeout=30)
        assert receipt['state'] == 'succeeded', (receipt.get('error'), receipt.get('output'), receipt.get('result'))
        assert receipt['project_id'] == stack.projects[1]['id']
        if tool == 'exec':
            assert receipt['result']['data']['exit_code'] == 0
            assert 'synthetic scope verified' in receipt['output']
        operations.append(admitted['operation_id'])
    mcp(stack, 'collaboration_work', {'action': 'result', **lease, 'outcome': 'succeeded',
        'summary': '新增项目的文件与命令已由隔离 Agent 验证。', 'operation_ids': operations, 'idempotency_key': key()})
    finals = [row for row in overview(stack)['messages']
              if row['kind'] == 'delegation_result' and row.get('reply_to_id') == source['message']['id']]
    assert len(finals) == 1 and finals[0]['body']['execution_verified']
    assert finals[0]['conversation_id'] == joined['slot']['conversation_id']
