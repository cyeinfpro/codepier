"""Real isolated HTTP verification for read-only receipt/diagnostic endpoints."""
from tests.collaboration_support import collaboration_stack as collaboration_stack, key


def test_authoritative_receipt_and_redacted_diagnostics_http(collaboration_stack):
    stack = collaboration_stack
    scope = {'project': stack.project['id'], 'environment_id': 'production'}
    room = stack.must(stack.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    client_id = key()
    query = {**scope, 'conversation_id': room['id'], 'client_message_id': client_id}
    response = stack.client.get('/api/collaboration/message-receipt', params=query)
    assert response.status_code == 200
    assert 'no-store' in response.headers['cache-control']
    assert response.json()['state'] == 'not_committed'
    assert response.json()['authoritative']
    body = {**scope, 'room_id': room['id'], 'conversation_id': room['id'], 'body_text': 'private synthetic content',
            'client_message_id': client_id, 'idempotency_key': key()}
    saved = stack.must(stack.client.post('/api/collaboration/message', json=body))['message']
    receipt = stack.must(stack.client.get('/api/collaboration/message-receipt', params=query))
    assert receipt['state'] == 'committed' and receipt['message']['id'] == saved['id']
    denied = stack.client.get('/api/collaboration/message-receipt', params={**query, 'conversation_id': 'foreign'})
    assert denied.status_code in {403, 404}
    assert denied.json().get('state') != 'not_committed'
    diagnostic = stack.client.get('/api/collaboration/diagnostics', params=scope)
    assert diagnostic.status_code == 200
    assert 'no-store' in diagnostic.headers['cache-control']
    assert 'private synthetic content' not in diagnostic.text
    assert diagnostic.json()['redacted'] and diagnostic.json()['read_only']
    missing = stack.client.get('/api/collaboration/diagnostics', params={**scope, 'project': 'missing'})
    assert missing.status_code in {403, 404}
