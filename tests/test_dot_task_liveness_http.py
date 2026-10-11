"""Actual in-process HTTP/MCP contracts; only synthetic local test identities."""
from tests.collaboration_support import key
from tests.test_collaboration_delegation_bridge import bridge as bridge, panel, tool


def test_recovery_panel_and_mcp_use_same_original_work_without_mutation(bridge):
    b = bridge
    dot = panel(b, 'dot', {**b.scope, 'label': 'Recovery HTTP fixture', 'duplex': True,
        'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key()})['dot']
    joined = tool(b, 'collaboration_join', {'code': dot['join_code'], 'idempotency_key': key()})
    message = panel(b, 'message', {**b.scope, 'conversation_id': joined['slot']['conversation_id'],
        'room_id': b.room['id'], 'body_text': 'Read the fixture and report its state.',
        'mentions': [{'slot_id': dot['id']}], 'client_message_id': key(), 'idempotency_key': key()})['message']
    inbox_request = joined['inbox_request']
    inbox = tool(b, inbox_request['tool'], inbox_request['arguments'])
    request = inbox['items'][0]['task_request']
    work = tool(b, request['tool'], request['arguments'])
    delegated = b.app.state.store.one('SELECT * FROM delegation_requests WHERE id=?', (work['delegation_id'],))
    tables = ('coordination_work', 'coordination_operations', 'operations',
              'collaboration_dot_messages', 'dot_delivery_receipts', 'dot_work_recovery_wakes',
              'collaboration_idempotency', 'mcp_event_outbox')
    def snapshot():
        return {t: b.app.state.store.all('SELECT * FROM ' + t + ' ORDER BY rowid') for t in tables}
    before = snapshot()
    args = {**b.scope, 'dot_id': dot['id']}
    response = b.client.get('/api/collaboration/dot-recovery', params=args)
    assert response.status_code == 200, response.text
    public = tool(b, 'collaboration_query', {'action': 'dot_recovery', **args})
    assert public['items'] == response.json()['items']
    assert public['items'][0]['work_item_id'] == delegated['work_item_id']
    assert public['items'][0]['message_id'] == message['id']
    assert public['read_only'] and not public['grants_authority']
    assert not public['scope_boundary']['host_outbox_verified']
    receipt = tool(b, 'collaboration_query', {'action': 'dot_receipt', **args, 'request_key': key()})
    assert not receipt['committed']
    evidence = tool(b, 'collaboration_query', {'action': 'work_evidence', **b.scope,
        'goal_id': delegated['goal_id'], 'work_item_id': delegated['work_item_id']})
    assert evidence['items'] == [] and evidence['read_only']
    assert b.client.get('/api/collaboration/dot-recovery', params={**args, 'limit': 51}).status_code == 422
    assert snapshot() == before
    b.client.cookies.clear()
    assert b.client.get('/api/collaboration/dot-recovery', params=args).status_code in (401, 403)
    assert snapshot() == before
