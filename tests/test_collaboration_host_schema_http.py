"""Discover over HTTP, import the real schema, then use the whole dot flow.

The Hub/Agent and MCP transport are real disposable processes. The restricted
schema importer and external dot consumer are test fixtures, not a live host.
"""
import pytest
from jsonschema import Draft202012Validator

from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_dots_flow import task_stack as task_stack
from tests import test_collaboration_dots_flow as flow
from tests.test_collaboration_delivery_flow import scope
from tests.test_collaboration_host_schema import first_branch_import
from tests.test_mcp_tasks_http import modern

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_discovered_host_schema_accepts_dot_inbox_replies_and_real_managed_steps(task_stack, monkeypatch):
    stack = task_stack
    response = modern(stack, 'tools/list', {}, token=stack.pat).json()
    assert 'error' not in response, response
    tools = {tool['name']: tool for tool in response['result']['tools']}
    assert len(tools) == 16
    names = ('collaboration_query', 'collaboration', 'collaboration_work')
    validators = {}
    for name in names:
        schema = tools[name]['inputSchema']
        assert 'action' in schema['properties'] and 'oneOf' not in schema
        assert tools[name]['_meta']['codepier/inputSchemaVersion'] == 'object-envelope-v1'
        validators[name] = Draft202012Validator(first_branch_import(schema))
    original = flow.mcp
    observed = []
    def imported_call(stack, name, arguments):
        validators[name].validate(arguments)
        observed.append((name, arguments['action'], arguments.get('tool')))
        return original(stack, name, arguments)
    monkeypatch.setattr(flow, 'mcp', imported_call)
    dot = stack.must(stack.client.post('/api/collaboration/dot', json={**scope(stack),
        'label': 'Schema fixture dot', 'duplex': True, 'capabilities': ['read', 'write', 'execute'],
        'acknowledge_unsandboxed_exec': True, 'confirm_tasks': True, 'idempotency_key': key()}))['dot']
    joined = flow.join_dot(stack, dot)
    recovered = flow.mcp(stack, 'collaboration_query', {'action': 'dot_connection',
        **scope(stack), 'dot_id': dot['id']})
    assert joined['inbox_request'] == recovered['inbox_request']
    room = flow.overview(stack)['room']
    source = stack.must(stack.client.post('/api/collaboration/message', json={**scope(stack),
        'conversation_id': joined['slot']['conversation_id'], 'room_id': room['id'],
        'body_text': 'Create a fixture file, read it and verify its contents.',
        'mentions': [{'slot_id': dot['id']}], 'client_message_id': key(), 'idempotency_key': key()}))
    item = flow.call(stack, joined['inbox_request'])['items'][0]
    assert item['message_id'] == source['message']['id']
    flow.call(stack, item['ack_request'])
    partial = flow.mcp(stack, 'collaboration', {'action': 'dot_message',
        **item['interim_reply_arguments'], 'body_text': 'Received the fixture request.'})
    flow.mcp(stack, 'collaboration', {'action': 'dot_update', **partial['update_arguments'],
        'body_text': 'Starting the real fixture file check.', 'complete': False})
    task = flow.call(stack, item['task_request'])
    operations = flow.complete_real_task(stack, joined, task['delegation_id'], 1)
    assert len(set(operations)) == 3
    assert ('collaboration_query', 'dot_inbox', None) in observed
    assert ('collaboration_query', 'dot_connection', None) in observed
    assert ('collaboration', 'join', None) in observed
    assert ('collaboration', 'dot_message', None) in observed
    assert ('collaboration', 'dot_update', None) in observed
    assert ('collaboration_work', 'from_message', None) in observed
    for step in ('write', 'read', 'exec'):
        assert ('collaboration_work', 'execute', step) in observed
    assert ('collaboration_work', 'result', None) in observed
    assert not flow.call(stack, recovered['inbox_request'])['items']
    # A broad discovery envelope does not weaken exact validation at tools/call.
    for invalid in (
        {'action': 'dot_inbox', **scope(stack)},
        {'action': 'dot_inbox', **scope(stack), 'dot_id': dot['id'], 'policy_id': 'foreign'},
        {'action': 'execute', **scope(stack), 'tool': 'exec', 'arguments': {'command': 'printf forbidden'}},
    ):
        tool = 'collaboration_work' if invalid['action'] == 'execute' else 'collaboration_query'
        result = modern(stack, 'tools/call', {'name': tool, 'arguments': invalid}, token=stack.pat).json()
        assert 'error' in result or result.get('result', {}).get('isError') is True, result
