"""Independent HTTP/MCP host bridge contracts; all identities and callbacks are fixtures.

These tests prove the server contract, not an installed plugin rescan or a real
ChatGPT subscription. No network callback, native automation, or model is used.
"""
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from hub.app import create_app
from hub.collaboration.network import Reply
from hub.principal import Principal
from shared.crypto import digest, password_hash
from tests.collaboration_support import collaboration_stack, key  # noqa: F401
from tests.legacy_iam_fixture import attach_session_security

MESSAGE_EVENT = 'codepier.collaboration.message_mentioned.v1'
WORK_EVENT = 'codepier.collaboration.work_available.v1'
DELEGATION_EVENT = 'codepier.collaboration.delegation_available.v1'
LEGACY_EVENTS = {
    'codepier.collaboration.task_available.v1', 'codepier.monitor.status_changed.v1',
    'codepier.monitor.result_ready.v1', 'codepier.monitor.incident_changed.v1',
}
SECRET = 'whsec_' + base64.b64encode(b'bridge-fixture-signing-material!!').decode()


def rpc(bridge, method, params=None, *, token=None):
    params = dict(params or {})
    headers = {
        'Authorization': 'Bearer ' + (token or bridge.token),
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': method,
    }
    if 'name' in params:
        headers['Mcp-Name'] = params['name']
    params['_meta'] = {
        'io.modelcontextprotocol/protocolVersion': '2026-07-28',
        'io.modelcontextprotocol/clientCapabilities': {},
    }
    return bridge.client.post('/mcp', headers=headers, json={
        'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params,
    })


def tool(bridge, name, arguments, *, ok=True, token=None):
    response = rpc(bridge, 'tools/call', {'name': name, 'arguments': arguments}, token=token)
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert bool(result.get('isError')) is not ok, result
    return result.get('structuredContent', result)


def panel(bridge, operation, arguments):
    response = bridge.client.post('/api/collaboration/' + operation, json=arguments)
    assert response.is_success, response.text
    return response.json()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MCP_EVENTS_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MONITOR_COLLECTOR_ENABLED', 'false')
    monkeypatch.setenv('CODEPIER_ANALYSIS_DISPATCH_ENABLED', 'false')
    app = create_app(str(tmp_path / 'hub'))
    store = app.state.store
    store.execute('INSERT INTO users VALUES (?,?,?,?)',
                  ('owner', 'admin', password_hash('fixture-password'), time.time()))
    store.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                  (digest('session'), 'owner', 'csrf', time.time() + 3600))
    attach_session_security(store)
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',
                  ('device', 'fixture', store.encrypt('x' * 43), time.time()))
    store.execute(
        'INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        ('project', 'fixture', 'fixture', 'device', str(tmp_path), '', 'write', 1, time.time()),
    )
    owner = Principal('panel:admin', 'owner', {'read', 'write', 'execute'}, ['*'], admin=True)
    issued = app.state.auth.issue_grant(owner, 'bridge fixture', ['read'], ['project'])
    deliveries = []

    async def receive(url, body, headers):
        assert url == 'https://example.invalid/bridge'
        signed = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + body
        expected = base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]), signed, hashlib.sha256).digest()).decode()
        assert hmac.compare_digest(headers['webhook-signature'], 'v1,' + expected)
        value = json.loads(body)
        if value.get('type') == 'verification':
            return Reply(200, json.dumps({'challenge': value['challenge']}).encode())
        assert headers['webhook-id'] == value['eventId']
        deliveries.append(value)
        return Reply(200, b'{}')

    app.state.runtime.collaboration.events.sender = receive
    with TestClient(app) as client:
        client.cookies.set('rd_session', 'session')
        client.headers['X-RD-CSRF'] = 'csrf'
        b = SimpleNamespace(app=app, client=client, token=issued['token'], grant=issued['grant_id'],
                            scope={'project': 'project', 'environment_id': 'production'},
                            deliveries=deliveries)
        b.room = panel(b, 'room', {**b.scope, 'idempotency_key': key()})['room']
        invited = panel(b, 'join-slot', {
            **b.scope, 'label': 'dot bridge fixture', 'kind': 'dot', 'idempotency_key': key(),
        })['slot']
        b.joined = tool(b, 'collaboration_join', {'code': invited['join_code'], 'idempotency_key': key()})
        b.slot = b.joined['slot']
        yield b


def create_policy(b):
    return panel(b, 'delegation-policy', {
        **b.scope, 'conversation_id': b.room['id'], 'slot_id': b.slot['id'],
        'expected_version': 0, 'purpose': 'Read and discuss isolated fixture state.', 'capabilities': ['read'],
        'duration_seconds': 3600, 'goal_duration_seconds': 900, 'max_delegations': 5,
        'budget': {}, 'execution_target': 'project_agent', 'acknowledge_unsandboxed_exec': False,
        'idempotency_key': key(),
    })['policy']


def message_args(b, *, policy=None, body='Read and summarize the fixture state.'):
    result = {
        **b.scope, 'conversation_id': b.room['id'], 'room_id': b.room['id'],
        'body_text': body, 'mentions': [{'slot_id': b.slot['id']}],
        'client_message_id': key(), 'idempotency_key': key(),
    }
    if policy is not None:
        result['delegation'] = {'policy_id': policy['id'], 'policy_version': policy['version'],
                                'acceptance': 'Report the evidence and any limitations.'}
    return result


def subscribe(b, request):
    response = rpc(b, 'events/subscribe', {
        **request, 'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
    })
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result['id'] and result['refreshBefore']
    return result


def deliver(b):
    b.client.portal.call(b.app.state.runtime.collaboration.events.tick)


def test_public_catalog_exposes_current_reply_and_delegation_contracts(bridge):
    b = bridge
    discovered = rpc(b, 'server/discover').json()['result']
    assert discovered['capabilities']['events'] == {}
    assert discovered['ttlMs'] == 0 and discovered['cacheScope'] == 'private'
    listed = rpc(b, 'tools/list').json()['result']
    tools = {item['name']: item for item in listed['tools']}
    assert {
        'collaboration_message_create', 'collaboration_delegation_read',
        'collaboration_goal_read', 'collaboration_work_claim',
        'collaboration_work_execute', 'collaboration_work_result',
    } <= tools.keys()
    assert 'conversation_id' in tools['collaboration_read']['inputSchema']['properties']
    assert {'timeline', 'thread', 'message_status'} <= set(
        tools['collaboration_read']['inputSchema']['properties']['kind']['enum'])
    assert not any(name in tools for name in (
        'collaboration_delegation_policy', 'collaboration_goal_approve', 'collaboration_message_remind'))
    for entry in tools.values():
        Draft202012Validator.check_schema(entry['inputSchema'])
    events = {item['name']: item for item in rpc(b, 'events/list').json()['result']['events']}
    expected = {'project_id', 'environment_id', 'conversation_id', 'slot_id', 'policy_id', 'policy_version'}
    assert set(events[DELEGATION_EVENT]['inputSchema']['required']) == expected
    assert set(events[DELEGATION_EVENT]['inputSchema']['properties']) == expected
    assert {'goal_id', 'approval_id'} <= set(events[WORK_EVENT]['inputSchema']['required'])
    assert {'slot_id', 'conversation_id'} <= set(events[MESSAGE_EVENT]['inputSchema']['required'])
    assert {item['name'] for item in b.joined['subscription_requests']} == LEGACY_EVENTS
    assert not b.joined['permissions_changed'] and not b.joined['worker_authorized']


def test_one_explicit_policy_subscription_delivers_two_distinct_goals(bridge):
    b = bridge
    policy = create_policy(b)
    request = policy['subscription_request']
    assert request['name'] == DELEGATION_EVENT
    subscribe(b, request)
    first_args = message_args(b, policy=policy)
    first = panel(b, 'message', first_args)
    repeat = panel(b, 'message', first_args)
    assert repeat['delegation_id'] == first['delegation_id']
    second = panel(b, 'message', message_args(b, policy=policy, body='Read another fixture question.'))
    assert first['scheduled'] and second['scheduled']
    assert first['goal_id'] != second['goal_id']
    deliver(b)
    deliver(b)
    delivered = [item for item in b.deliveries if item['name'] == DELEGATION_EVENT]
    assert {item['data']['delegation_id'] for item in delivered} == {first['delegation_id'], second['delegation_id']}
    assert len(delivered) == 2
    schema = next(item['payloadSchema'] for item in rpc(b, 'events/list').json()['result']['events']
                  if item['name'] == DELEGATION_EVENT)
    for event in delivered:
        Draft202012Validator(schema).validate(event['data'])
        assert event['data']['policy_id'] == policy['id']
        assert event['data']['policy_version'] == policy['version']
        assert event['data']['recipient_slot_id'] == b.slot['id']
        assert event['data']['recipient_grant_id'] == b.grant
        assert 'body_text' not in event['data'] and 'request' not in event['data']
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': first['delegation_id']})
    assert fresh['trusted_author'] == {'kind': 'panel_owner', 'user_id': 'owner', 'authenticated': True}
    assert fresh['source_message']['id'] == first['message']['id']
    assert fresh['source_message']['author'] == 'owner'
    assert fresh['source_message']['origin'] == 'panel_owner'
    assert fresh['goal']['id'] == first['goal_id'] and fresh['goal']['state'] == 'active'
    assert any(item['id'] == first['work_item_id'] for item in fresh['work_items'])
    assert len(b.app.state.store.all('SELECT * FROM mcp_event_subscriptions')) == 1


def test_policy_revocation_blocks_new_messages_and_pending_claims(bridge):
    b = bridge
    policy = create_policy(b)
    posted = panel(b, 'message', message_args(b, policy=policy))
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': posted['delegation_id']})
    work = next(item for item in fresh['work_items'] if item['id'] == posted['work_item_id'])
    pause = {**b.scope, 'policy_id': policy['id'], 'expected_version': policy['version'],
             'action': 'pause', 'idempotency_key': key()}
    rejected = b.client.post('/api/collaboration/delegation-policy-control', json=pause,
                             headers={'X-RD-CSRF': 'wrong'})
    assert rejected.status_code == 403
    panel(b, 'delegation-policy-control', pause)
    assert not b.client.post('/api/collaboration/message', json=message_args(b, policy=policy)).is_success
    tool(b, 'collaboration_work_claim', {
        **b.scope, 'goal_id': posted['goal_id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key(),
    }, ok=False)
    denied = rpc(b, 'events/subscribe', {
        **policy['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
    })
    assert denied.status_code in {403, 409}
    assert not b.deliveries


def test_owner_delegation_cannot_be_forged_by_connector_message(bridge):
    b = bridge
    policy = create_policy(b)
    panel(b, 'message-access', {
        **b.scope, 'conversation_id': b.room['id'], 'room_id': b.room['id'], 'grant_id': b.grant,
        'enabled': True, 'expected_version': 0, 'idempotency_key': key(),
    })
    response = tool(b, 'collaboration_message_create', message_args(b, policy=policy), ok=False)
    assert response.get('error')
    assert not b.app.state.store.all('SELECT * FROM coordination_goals')
    ordinary = panel(b, 'message', message_args(b))
    assert ordinary['scheduled'] is False
    assert ordinary['notifications'][0]['state'] == 'not_subscribed'
    assert not b.app.state.store.all('SELECT * FROM coordination_goals')


def test_explicit_reminder_recovers_missed_mention_without_duplicate_message(bridge):
    b = bridge
    posted = panel(b, 'message', message_args(b))
    assert posted['notifications'][0]['state'] == 'not_subscribed'
    assert posted['notifications'][0]['event_id'] is None
    subscribe(b, {'name': MESSAGE_EVENT, 'arguments': {
        'project_id': b.scope['project'], 'environment_id': b.scope['environment_id'],
        'conversation_id': b.room['id'], 'slot_id': b.slot['id'],
    }})
    deliver(b)
    assert not b.deliveries
    reminder = {
        **b.scope, 'conversation_id': b.room['id'], 'room_id': b.room['id'],
        'message_id': posted['message']['id'], 'expected_message_version': posted['message']['version'],
        'slot_ids': [b.slot['id']], 'idempotency_key': key(),
    }
    assert b.client.post('/api/collaboration/message-remind', json=reminder,
                         headers={'X-RD-CSRF': 'wrong'}).status_code == 403
    reminded = panel(b, 'message-remind', reminder)
    assert reminded['message']['id'] == posted['message']['id'] and not reminded['scheduled']
    panel(b, 'message-remind', reminder)
    panel(b, 'message-remind', {**reminder, 'expected_message_version': reminded['message']['version'],
                                'idempotency_key': key()})
    deliver(b)
    delivered = [item for item in b.deliveries if item['name'] == MESSAGE_EVENT]
    assert len(delivered) == 1 and delivered[0]['data']['message_id'] == posted['message']['id']
    current = tool(b, 'collaboration_read', {
        **b.scope, 'conversation_id': b.room['id'], 'kind': 'message_status', 'id': posted['message']['id'],
    })
    assert current['notifications'][0]['state'] == 'accepted'
    assert not current['notifications'][0]['read_verified']
    assert len(b.app.state.store.all('SELECT * FROM collaboration_messages')) == 1
    assert not b.app.state.store.all('SELECT * FROM coordination_goals')


def test_delegation_result_returns_to_original_thread_without_waking_peers(bridge):
    b = bridge
    policy = create_policy(b)
    subscribe(b, policy['subscription_request'])
    posted = panel(b, 'message', message_args(b, policy=policy))
    deliver(b)
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': posted['delegation_id']})
    work = next(item for item in fresh['work_items'] if item['id'] == posted['work_item_id'])
    work = tool(b, 'collaboration_work_claim', {
        **b.scope, 'goal_id': posted['goal_id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key(),
    })['work_item']
    result_args = {
        **b.scope, 'goal_id': posted['goal_id'], 'work_item_id': work['id'],
        'attempt': work['attempt'], 'fencing_token': work['fencing_token'],
        'outcome': 'succeeded', 'summary': 'Discussion-only conclusion; no operation was executed.',
        'operation_ids': [], 'limitations': ['No execution evidence.'], 'idempotency_key': key(),
    }
    result = tool(b, 'collaboration_work_result', result_args)['work_item']
    assert result['state'] == 'succeeded' and result['result']['execution_verified'] is False
    tool(b, 'collaboration_work_result', result_args)
    thread = tool(b, 'collaboration_read', {
        **b.scope, 'conversation_id': b.room['id'], 'kind': 'thread', 'id': posted['message']['id'],
    })['items']
    replies = [item for item in thread if item['kind'] == 'delegation_result']
    assert len(replies) == 1
    assert replies[0]['thread_root_id'] == posted['message']['thread_root_id']
    assert replies[0]['reply_to_id'] == posted['message']['id']
    assert replies[0]['body']['operation_ids'] == []
    assert replies[0]['body']['execution_verified'] is False
    deliver(b)
    assert len(b.deliveries) == 1


def test_delegation_filters_and_reads_reject_another_connection(bridge):
    b = bridge
    policy = create_policy(b)
    posted = panel(b, 'message', message_args(b, policy=policy))
    owner = Principal('panel:admin', 'owner', {'read', 'write', 'execute'}, ['*'], admin=True)
    outsider = b.app.state.auth.issue_grant(owner, 'other fixture connection', ['read'], ['project'])
    denied = rpc(b, 'events/subscribe', {
        **policy['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
    }, token=outsider['token'])
    assert denied.status_code == 403
    tool(b, 'collaboration_delegation_read', {
        **b.scope, 'delegation_id': posted['delegation_id'],
    }, token=outsider['token'], ok=False)
    changed = dict(policy['subscription_request']['arguments'])
    changed['policy_version'] += 1
    denied = rpc(b, 'events/subscribe', {
        'name': DELEGATION_EVENT, 'arguments': changed,
        'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
    })
    assert denied.status_code in {403, 409}
    assert not b.deliveries


def test_late_subscription_does_not_replay_prior_delegation(bridge):
    b = bridge
    policy = create_policy(b)
    posted = panel(b, 'message', message_args(b, policy=policy))
    subscribe(b, policy['subscription_request'])
    deliver(b)
    assert not b.deliveries
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': posted['delegation_id']})
    assert any(item['id'] == posted['work_item_id'] and item['state'] == 'queued'
               for item in fresh['work_items'])
    assert len(b.app.state.store.all('SELECT * FROM coordination_goals')) == 1


def test_obsolete_replayed_work_does_not_revoke_policy_subscription(bridge):
    b = bridge
    policy = create_policy(b)
    request = policy['subscription_request']
    initial = subscribe(b, request)
    first = panel(b, 'message', message_args(b, policy=policy))
    deliver(b)
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': first['delegation_id']})
    work = next(item for item in fresh['work_items'] if item['id'] == first['work_item_id'])
    work = tool(b, 'collaboration_work_claim', {
        **b.scope, 'goal_id': first['goal_id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key(),
    })['work_item']
    tool(b, 'collaboration_work_result', {
        **b.scope, 'goal_id': first['goal_id'], 'work_item_id': work['id'],
        'attempt': work['attempt'], 'fencing_token': work['fencing_token'], 'outcome': 'succeeded',
        'summary': 'Completed discussion.', 'operation_ids': [], 'idempotency_key': key(),
    })
    stopped = rpc(b, 'events/unsubscribe', {
        **request, 'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge'},
    })
    assert stopped.status_code == 200
    resumed = rpc(b, 'events/subscribe', {
        **request, 'cursor': initial['cursor'],
        'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
    })
    assert resumed.status_code == 200, resumed.text
    second = panel(b, 'message', message_args(b, policy=policy, body='A second distinct question.'))
    deliver(b)
    deliver(b)
    delivered = [item for item in b.deliveries if item['name'] == DELEGATION_EVENT]
    assert sum(item['data']['delegation_id'] == first['delegation_id'] for item in delivered) == 1
    assert sum(item['data']['delegation_id'] == second['delegation_id'] for item in delivered) == 1
    sub = b.app.state.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (initial['id'],))
    assert sub['state'] == 'active'


def test_policy_test_event_creates_no_goal_or_work(bridge):
    b = bridge
    policy = create_policy(b)
    subscribe(b, policy['subscription_request'])
    slots = tool(b, 'collaboration_read', {**b.scope, 'kind': 'join_slots'})['items']
    slot = next(item for item in slots if item['id'] == b.slot['id'])
    panel(b, 'join-slot-control', {
        **b.scope, 'slot_id': slot['id'], 'expected_version': slot['version'],
        'action': 'test', 'idempotency_key': key(),
    })
    deliver(b)
    assert len(b.deliveries) == 1 and b.deliveries[0]['data']['test'] is True
    assert not b.app.state.store.all('SELECT * FROM coordination_goals')
    assert not b.app.state.store.all('SELECT * FROM coordination_work')


def test_valid_other_slot_and_conversation_cannot_retarget_policy(bridge):
    b = bridge
    policy = create_policy(b)
    other = panel(b, 'join-slot', {
        **b.scope, 'label': 'another slot', 'kind': 'dot', 'idempotency_key': key(),
    })['slot']
    tool(b, 'collaboration_join', {'code': other['join_code'], 'idempotency_key': key()})
    other_room = panel(b, 'conversation', {
        'title': 'A different authorized room', 'projects': [b.scope], 'idempotency_key': key(),
    })['conversation']
    for field, value in (('slot_id', other['id']), ('conversation_id', other_room['id'])):
        arguments = {**policy['subscription_request']['arguments'], field: value}
        denied = rpc(b, 'events/subscribe', {
            'name': DELEGATION_EVENT, 'arguments': arguments,
            'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
        })
        assert denied.status_code in {403, 409}, denied.text
    assert not b.app.state.store.all('SELECT * FROM mcp_event_subscriptions')
    assert not b.deliveries


def test_real_hub_agent_delegation_write_read_and_thread_result(collaboration_stack):
    """Local bridge completion uses real durable Agent operations, never a model."""
    s = collaboration_stack
    b = SimpleNamespace(client=s.client, token=s.pat, grant=s.grant,
                        scope={'project': s.project['id'], 'environment_id': 'production'})
    b.room = panel(b, 'room', {**b.scope, 'idempotency_key': key()})['room']
    invitation = panel(b, 'join-slot', {
        **b.scope, 'label': 'local bridge executor fixture', 'kind': 'dot', 'idempotency_key': key(),
    })['slot']
    b.slot = tool(b, 'collaboration_join', {
        'code': invitation['join_code'], 'idempotency_key': key(),
    })['slot']
    policy = panel(b, 'delegation-policy', {
        **b.scope, 'conversation_id': b.room['id'], 'slot_id': b.slot['id'], 'expected_version': 0,
        'purpose': 'Write and read a marker in the isolated test project.',
        'capabilities': ['read', 'write'], 'duration_seconds': 3600, 'goal_duration_seconds': 900,
        'max_delegations': 2, 'budget': {}, 'execution_target': 'project_agent',
        'acknowledge_unsandboxed_exec': False, 'idempotency_key': key(),
    })['policy']
    args = message_args(b, policy=policy, body='Create bridge-marker.txt, read it back, and report evidence.')
    posted = panel(b, 'message', args)
    repeated = panel(b, 'message', args)
    assert repeated['delegation_id'] == posted['delegation_id']
    fresh = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': posted['delegation_id']})
    assert fresh['trusted_author']['authenticated'] and fresh['trusted_author']['kind'] == 'panel_owner'
    assert fresh['source_message']['author'] == fresh['trusted_author']['user_id']
    work = next(item for item in fresh['work_items'] if item['id'] == posted['work_item_id'])
    claim_args = {
        **b.scope, 'goal_id': posted['goal_id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key(),
    }
    work = tool(b, 'collaboration_work_claim', claim_args)['work_item']
    repeated_claim = tool(b, 'collaboration_work_claim', claim_args)['work_item']
    assert repeated_claim['attempt'] == work['attempt']
    assert repeated_claim['fencing_token'] == work['fencing_token']
    lease = {
        **b.scope, 'goal_id': posted['goal_id'], 'work_item_id': work['id'],
        'attempt': work['attempt'], 'fencing_token': work['fencing_token'],
    }
    write_args = {
        **lease, 'tool': 'write', 'arguments': {
            'path': 'bridge-marker.txt', 'expected_sha256': 'new', 'content': 'verified bridge marker\n',
        }, 'idempotency_key': key(),
    }
    written = tool(b, 'collaboration_work_execute', write_args)
    repeated_write = tool(b, 'collaboration_work_execute', write_args)
    assert repeated_write['operation_id'] == written['operation_id']
    write_receipt = s.poll(written['operation_id'])
    assert write_receipt['state'] == 'succeeded', write_receipt
    assert (s.projectalpha / 'bridge-marker.txt').read_text() == 'verified bridge marker\n'
    read_args = {
        **lease, 'tool': 'read', 'arguments': {'path': 'bridge-marker.txt'}, 'idempotency_key': key(),
    }
    observed = tool(b, 'collaboration_work_execute', read_args)
    repeated_read = tool(b, 'collaboration_work_execute', read_args)
    assert repeated_read['operation_id'] == observed['operation_id']
    read_receipt = s.poll(observed['operation_id'])
    assert read_receipt['state'] == 'succeeded', read_receipt
    assert read_receipt['result']['data']['content'] == 'verified bridge marker\n'
    result_args = {
        **lease, 'outcome': 'succeeded', 'summary': 'The fixture marker was written and read back.',
        'operation_ids': [written['operation_id'], observed['operation_id']], 'idempotency_key': key(),
    }
    finished = tool(b, 'collaboration_work_result', result_args)['work_item']
    tool(b, 'collaboration_work_result', result_args)
    assert finished['result']['execution_verified']
    assert finished['result']['acceptance_verified_by_owner'] is False
    thread = tool(b, 'collaboration_read', {
        **b.scope, 'conversation_id': b.room['id'], 'kind': 'thread', 'id': posted['message']['id'],
    })['items']
    results = [item for item in thread if item['kind'] == 'delegation_result']
    assert len(results) == 1 and results[0]['reply_to_id'] == posted['message']['id']
    assert results[0]['thread_root_id'] == posted['message']['thread_root_id']
    assert set(results[0]['body']['operation_ids']) == {written['operation_id'], observed['operation_id']}
    assert results[0]['body']['execution_verified'] is True
    final = tool(b, 'collaboration_delegation_read', {**b.scope, 'delegation_id': posted['delegation_id']})
    assert len(final['operations']) == 2
