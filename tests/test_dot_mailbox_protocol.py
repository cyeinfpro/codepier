"""Real server subscription/transaction paths with an isolated fake callback.

These tests establish server behavior, never native host consumer adoption.
"""
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import MAILBOX_EVENT, MESSAGE_EVENT, redact
from hub.collaboration.events import EventService
from hub.collaboration.dot_mailbox import PAGE_BYTE_BUDGET, REFERENCE_KEYS
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


def setup(fixture, *, duplex=True):
    service, owner, worker, _, room, _, clock, scope = fixture
    service.runtime.collaboration = service
    actor = replace(worker, grant_id='broad', actor='mcp:broad:mailbox')
    created = service.dots.create({**scope, 'conversation_id': room['id'], 'label': 'Inbox member',
        'capabilities': ['read', 'write', 'execute'], 'acknowledge_unsandboxed_exec': True,
        'confirm_tasks': True, 'duplex': duplex, 'idempotency_key': key()}, owner)
    joined = service.joining.join({'code': created['dot']['join_code'], 'idempotency_key': key()}, actor)
    return SimpleNamespace(s=service, owner=owner, actor=actor, room=room, clock=clock,
                           scope=scope, dot=service.dots.find(joined['slot']['id']), joined=joined)


def configure(c):
    return c.s.dot_mailbox.configure({**c.scope, 'dot_id': c.dot['id'],
        'confirm_consumer_protocol': True, 'idempotency_key': key()}, c.actor)


def message(c, text='Pending input'):
    return c.s.chatroom.create({**c.scope, 'room_id': c.room['id'],
        'conversation_id': c.dot['conversation_id'], 'body_text': text,
        'mentions': [{'slot_id': c.dot['id']}], 'client_message_id': key(),
        'idempotency_key': key()}, c.owner)['message']


def read(c, **extra):
    return c.s.dot_mailbox.read({**c.scope, 'dot_id': c.dot['id'], **extra}, c.actor)


async def subscribe(c, configuration=None):
    c.s.config = replace(c.s.config, events_enabled=True)
    c.requests = []

    async def sender(url, body, headers):
        parsed = json.loads(body)
        c.requests.append(parsed)
        if parsed.get('type') == 'verification':
            return Reply(200, json.dumps({'challenge': parsed['challenge']}).encode())
        return Reply(204)

    c.s.events = EventService(c.s, sender=sender)
    c.subscription_raw = {**(configuration or configure(c))['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://consumer.example.test/' + c.dot['id'],
                     'secret': 'whsec_' + base64.b64encode(b'synthetic-mailbox-secret-32-byte!').decode()}}
    result = await c.s.events.subscribe(c.subscription_raw, c.actor)
    c.subscription_id = result['id']
    return result


def test_explicit_negotiation_and_reads_do_not_activate_or_renew(collab):
    c = setup(collab)
    grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    assert c.joined['subscription_request']['name'] == MESSAGE_EVENT
    selected = configure(c)
    assert selected['state'] == 'prepared' and selected['active_protocol_version'] == 1
    assert selected['subscription_request']['name'] == MAILBOX_EVENT
    assert not selected['permissions_changed'] and not selected['subscription_created']
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = read(c)
    recovered = c.s.dots.connection({**c.scope, 'dot_id': c.dot['id']}, c.actor)
    assert page['read_only'] and not page['read_is_consumption']
    assert recovered['subscription_request'] == selected['subscription_request']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert not c.s.dot_mailbox.enabled(c.dot['id'])
    assert not c.s.store.all('SELECT * FROM mcp_event_subscriptions')
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == grants


def test_task_only_purpose_cannot_upgrade(collab):
    c = setup(collab, duplex=False)
    with pytest.raises(DevError) as exc:
        configure(c)
    assert exc.value.code == 'DOT_MAILBOX_DUPLEX_REQUIRED'
    assert c.s.dot_mailbox.binding(c.dot['id']) is None


def test_index_is_transactional_idempotent_and_pages_are_scope_bound(collab):
    c, other = setup(collab), setup(collab)
    configure(c)
    configure(other)
    first, second = message(c), message(c, 'Second input')
    row = c.s.dot_mailbox.binding(c.dot['id'])
    assert row['dirty_revision'] == 2
    page = read(c, limit=1)
    assert [i['object_id'] for i in page['items']] == [first['id']]
    assert page['next_cursor']
    assert read(c, cursor=page['next_cursor'], limit=1)['items'][0]['object_id'] == second['id']
    with pytest.raises(DevError):
        read(other, cursor=page['next_cursor'], limit=1)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    assert read(c)['items']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    with pytest.raises(RuntimeError), c.s.store.transaction():
        c.s.dot_mailbox.mark_dirty(c.dot, 'control_notice', 'rolled-back', 1, {'reason_code': 'fixture'})
        raise RuntimeError('force transaction rollback')
    assert c.s.dot_mailbox.current_revision(c.dot['id']) == 2
    with c.s.store.transaction():
        c.s.dot_mailbox.mark_dirty(c.dot, 'message', first['id'], first['version'],
            {'message_id': first['id'], 'thread_root_id': first['thread_root_id']})
    assert c.s.dot_mailbox.current_revision(c.dot['id']) == 2
    assert not c.s.dot_mailbox.mark_drained(c.dot['id'], row['generation'], 1)
    assert not c.s.dot_mailbox.mark_drained(c.dot['id'], row['generation'], 2)


@pytest.mark.asyncio
async def test_native_activation_catches_pending_input_and_seals_ticket(collab):
    c = setup(collab)
    source = message(c)
    configured = configure(c)
    assert configured['dirty_revision'] == 1
    await subscribe(c, configured)
    assert c.s.dot_mailbox.enabled(c.dot['id'])
    binding = c.s.dot_mailbox.binding(c.dot['id'])
    assert binding['subscription_id'] == c.subscription_id
    await c.s.events.tick()
    hints = [r for r in c.requests if r.get('data', {}).get('schema_version') == 2]
    assert hints and hints[-1]['data']['wake_ticket']
    ticket = hints[-1]['data']['wake_ticket']
    assert hints[-1]['data']['dirty_revision'] == 1
    assert read(c)['items'][0]['object_id'] == source['id']
    delivery = c.s.store.one('SELECT * FROM mcp_event_deliveries WHERE subscription_id=?', (c.subscription_id,))
    assert delivery['body'].startswith('sealed:v1:') and ticket not in delivery['body']
    assert ticket not in json.dumps(read(c))
    assert ticket not in json.dumps(c.s.dots.connection({**c.scope, 'dot_id': c.dot['id']}, c.actor))
    assert all(ticket not in row['data'] for row in c.s.store.all('SELECT data FROM mcp_event_outbox'))
    before = c.s.dot_mailbox.current_revision(c.dot['id'])
    with c.s.store.transaction():
        sub = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (c.subscription_id,))
        c.s.events.queue_test(c.room, sub)
    await c.s.events.tick()
    tests = [r['data'] for r in c.requests if r.get('data', {}).get('test')]
    assert tests and tests[-1]['wake_ticket'] is None
    assert c.s.dot_mailbox.current_revision(c.dot['id']) == before
    assert not c.s.store.all('SELECT * FROM coordination_work')


@pytest.mark.asyncio
async def test_renewal_keeps_generation_and_expiry_never_reopens_legacy(collab):
    c = setup(collab)
    await subscribe(c)
    before = c.s.dot_mailbox.binding(c.dot['id'])
    await c.s.events.subscribe(c.subscription_raw, c.actor)
    after = c.s.dot_mailbox.binding(c.dot['id'])
    assert before['generation'] == after['generation']
    assert before['subscription_id'] == after['subscription_id']
    c.clock[0] += 86401
    assert c.s.dot_mailbox.enabled(c.dot['id'])
    assert c.s.dot_mailbox.health(after)['state'] == 'expired'
    assert c.s.dot_mailbox.recover() == 0
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {'action': 'dot_message', **c.scope, 'dot_id': c.dot['id'],
            'body_text': 'No legacy bypass', 'idempotency_key': key()}, c.actor)


def test_secret_redaction_covers_new_protocol_keys():
    assert redact({'wake_ticket': 'abc', 'consumer_proof': 'def'}) == {
        'wake_ticket': '[REDACTED]', 'consumer_proof': '[REDACTED]'}
    assert 'abc' not in redact('wake_ticket=abc consumer_proof=def')


@pytest.mark.asyncio
async def test_retry_keeps_event_and_ticket_bytes_and_recovery_is_bounded(collab):
    c = setup(collab)
    message(c)
    await subscribe(c)
    replies = []

    async def transient(url, body, headers):
        replies.append(body)
        return Reply(503 if len(replies) == 1 else 204)

    c.s.events.sender = transient
    await c.s.events.tick()
    first = json.loads(replies[0])
    delivery = c.s.store.one('SELECT * FROM mcp_event_deliveries WHERE event_id=?', (first['eventId'],))
    assert delivery['state'] == 'retry_wait'
    c.clock[0] = delivery['next_at'] + 1
    await c.s.events.tick()
    retried = [raw for raw in replies if json.loads(raw)['eventId'] == first['eventId']]
    assert len(retried) == 2 and retried[0] == retried[1]
    # The dispatcher may have consumed the first 30-second reminder budget.
    for delay in (31, 121, 601, 601):
        c.clock[0] += delay
        c.s.dot_mailbox.recover(max_wakes=1)
    assert c.s.dot_mailbox.binding(c.dot['id'])['wake_count'] == 3
    before = c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (MAILBOX_EVENT,))['n']
    c.clock[0] += 601
    assert c.s.dot_mailbox.recover() == 0
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (MAILBOX_EVENT,))['n'] == before
    raw = {**c.scope, 'dot_id': c.dot['id'],
           'binding_generation': c.s.dot_mailbox.binding(c.dot['id'])['generation'], 'idempotency_key': key()}
    receipt = c.s.dot_mailbox.request_recovery(raw, c.actor)
    assert receipt['wakeup_queued'] and 'wake_ticket' not in receipt
    assert c.s.dot_mailbox.request_recovery(raw, c.actor) == receipt
    with pytest.raises(DevError) as exc:
        c.s.dot_mailbox.request_recovery({**raw, 'idempotency_key': key()}, c.actor)
    assert exc.value.code == 'DOT_MAILBOX_RECOVERY_BUDGET'
    await c.s.events.tick()
    fresh = json.loads(replies[-1])
    assert fresh['eventId'] != first['eventId']
    assert fresh['data']['wake_ticket'] != first['data']['wake_ticket']
    assert fresh['data']['dirty_revision'] == first['data']['dirty_revision']


@pytest.mark.asyncio
async def test_permanent_rejection_and_pause_do_not_create_recovery_events(collab):
    c = setup(collab)
    message(c)
    await subscribe(c)

    async def gone(url, body, headers):
        return Reply(410)

    c.s.events.sender = gone
    await c.s.events.tick()
    c.clock[0] += 601
    count = c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox')['n']
    assert c.s.dot_mailbox.recover() == 0
    binding = c.s.dot_mailbox.binding(c.dot['id'])
    with pytest.raises(DevError) as exc:
        c.s.dot_mailbox.request_recovery({**c.scope, 'dot_id': c.dot['id'],
            'binding_generation': binding['generation'], 'idempotency_key': key()}, c.actor)
    assert exc.value.code == 'DOT_MAILBOX_ROUTE_REJECTED'
    c.s.store.execute("UPDATE mcp_event_subscriptions SET state='paused' WHERE id=?", (c.subscription_id,))
    assert c.s.dot_mailbox.recover() == 0
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox')['n'] == count
    assert c.s.dot_mailbox.enabled(c.dot['id'])


@pytest.mark.asyncio
async def test_native_expired_restore_reissues_hint_without_replaying_work(collab):
    c = setup(collab)
    message(c)
    await subscribe(c)
    await c.s.events.tick()
    before = c.s.dot_mailbox.binding(c.dot['id'])
    original = [r for r in c.requests if r.get('data')][-1]
    c.clock[0] += 86401
    await c.s.events.subscribe(c.subscription_raw, c.actor)
    current = c.s.dot_mailbox.binding(c.dot['id'])
    assert current['generation'] == before['generation']
    assert current['dirty_revision'] == before['dirty_revision']
    assert current['wake_generation'] == before['wake_generation'] + 1
    await c.s.events.tick()
    latest = [r for r in c.requests if r.get('data')][-1]
    assert latest['eventId'] != original['eventId']
    assert latest['data']['wake_ticket'] != original['data']['wake_ticket']
    assert len(c.s.store.all('SELECT * FROM dot_mailbox_entries')) == 1
    assert not c.s.store.all('SELECT * FROM coordination_work')


@pytest.mark.asyncio
async def test_explicit_control_ack_and_revision_checked_drain(collab):
    c = setup(collab)
    await subscribe(c)
    with c.s.store.transaction():
        c.s.dot_mailbox.mark_dirty(c.dot, 'control_notice', 'owner-control', 1, {'reason_code': 'fixture'})
    await c.s.events.tick()
    hint = [r['data'] for r in c.requests if r.get('data')][-1]
    session = c.s.dot_consumers.begin({**c.scope, 'dot_id': c.dot['id'],
        'binding_generation': hint['binding_generation'], 'wake_ticket': hint['wake_ticket'],
        'consumer_run_id': key(), 'idempotency_key': key()}, c.actor)
    proof = {k: session[k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}
    page = read(c)
    assert c.s.dot_mailbox.has_ready(c.dot['id'])
    entry = page['items'][0]
    raw = {**entry['ack_request']['arguments'], **proof}
    raw.pop('action')
    c.s.dot_mailbox.ack(raw, c.actor)
    assert not c.s.dot_mailbox.has_ready(c.dot['id'])
    with c.s.store.transaction():
        assert c.s.dot_mailbox.mark_drained(c.dot['id'], hint['binding_generation'], page['dirty_revision'])
    assert read(c)['drained_revision'] == page['dirty_revision']
    message(c, 'Arrived after the drain transaction')
    assert c.s.dot_mailbox.current_revision(c.dot['id']) > page['dirty_revision']
    assert c.s.dot_mailbox.has_ready(c.dot['id'])
    with c.s.store.transaction():
        assert not c.s.dot_mailbox.mark_drained(c.dot['id'], hint['binding_generation'], page['dirty_revision'])


@pytest.mark.asyncio
async def test_expiry_maintenance_is_once_per_original_version_and_never_renews(collab):
    c = setup(collab)
    await subscribe(c)
    original = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (c.subscription_id,))
    binding = c.s.dot_mailbox.binding(c.dot['id'])
    assert c.s.dot_mailbox.recover() == 0
    c.clock[0] = original['expires_at'] - 3599
    assert c.s.dot_mailbox.recover(max_wakes=1) == 1
    page = read(c)
    notice = page['items'][0]
    assert notice['kind'] == 'control_notice'
    assert notice['maintenance']['kind'] == 'renew_original_native_subscription'
    assert notice['maintenance']['subscription_request'] == c.joined['consumer_configuration']['protocol'].get(
        'subscription_request', c.s.dot_mailbox.subscription(c.room, c.dot))
    assert not notice['maintenance']['automatically_renewed']
    assert c.s.dot_mailbox.recover(max_wakes=1) == 0
    assert len(c.s.store.all("SELECT * FROM dot_mailbox_entries WHERE kind='control_notice'")) == 1
    unchanged = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (c.subscription_id,))
    assert unchanged['expires_at'] == original['expires_at'] and unchanged['version'] == original['version']
    assert c.s.dot_mailbox.binding(c.dot['id'])['generation'] == binding['generation']
    # The actual original host renewal makes its previous maintenance hint stale.
    await c.s.events.subscribe(c.subscription_raw, c.actor)
    assert not read(c)['items']
    assert c.s.dot_mailbox.recover() == 0


@pytest.mark.asyncio
async def test_short_native_ttl_does_not_immediately_trigger_renewal_loop(collab):
    c = setup(collab)
    await subscribe(c)
    c.subscription_raw['ttlMs'] = 60000
    await c.s.events.subscribe(c.subscription_raw, c.actor)
    assert c.s.dot_mailbox.recover() == 0
    c.clock[0] += 46
    assert c.s.dot_mailbox.recover(max_wakes=1) == 1
    assert c.s.dot_mailbox.recover(max_wakes=1) == 0


def test_serialized_first_page_budget_and_filtered_pages_preserve_every_reference(collab):
    c = setup(collab)
    configure(c)
    reference = {name: 'bounded-' + 'a' * 120 for name in REFERENCE_KEYS}
    with c.s.store.transaction():
        for i in range(80):
            c.s.dot_mailbox.mark_dirty(c.dot, 'control_notice', f'notice-{i}', 1, reference)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = read(c, limit=100)
    contract_hash = page['contract_hash']
    assert len(contract_hash) == 64 and page['budget_limited']
    found = []
    while True:
        assert len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()) <= PAGE_BYTE_BUDGET
        assert page['contract_hash'] == contract_hash
        found.extend(item['object_id'] for item in page['items'])
        if not page['next_cursor']:
            break
        page = read(c, limit=100, cursor=page['next_cursor'])
    assert found == [f'notice-{i}' for i in range(80)]
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


@pytest.mark.asyncio
async def test_burst_coalesces_only_unsent_hints_without_losing_mailbox_inputs(collab):
    c = setup(collab)
    await subscribe(c)
    sources = [message(c, f'Burst input {i}') for i in range(3)]
    await c.s.events.tick()
    hints = [r for r in c.requests if r.get('name') == MAILBOX_EVENT]
    assert len(hints) == 1 and hints[0]['data']['dirty_revision'] == 3
    assert [i['object_id'] for i in read(c)['items']] == [m['id'] for m in sources]
    deliveries = c.s.store.all('SELECT * FROM mcp_event_deliveries WHERE subscription_id=?', (c.subscription_id,))
    assert len([d for d in deliveries if d['state'] == 'accepted']) == 1
    assert len([d for d in deliveries if d['reason_code'] == 'mailbox_coalesced']) == 2
    assert all(d['attempt'] == 0 and d['body'] is None for d in deliveries if d['reason_code'] == 'mailbox_coalesced')


@pytest.mark.asyncio
async def test_expired_native_route_cannot_be_rebound_to_another_same_grant_host(collab):
    c = setup(collab)
    await subscribe(c)
    original = c.s.dot_mailbox.binding(c.dot['id'])
    c.clock[0] += 86401
    redirected = {**c.subscription_raw, 'delivery': {**c.subscription_raw['delivery'],
        'url': 'https://different-consumer.example.test/other-chat'}}
    with pytest.raises(DevError) as exc:
        await c.s.events.subscribe(redirected, c.actor)
    assert exc.value.code == 'DOT_MAILBOX_ORIGINAL_ROUTE_REQUIRED'
    current = c.s.dot_mailbox.binding(c.dot['id'])
    assert current['generation'] == original['generation'] and current['subscription_id'] == original['subscription_id']
    assert len(c.s.store.all('SELECT * FROM mcp_event_subscriptions')) == 1
    assert c.s.dot_mailbox.enabled(c.dot['id'])
