"""Consumer ownership across lost replies, reconnects and raced inbox updates.

Only an in-process callback is used; no real Dot host or external execution is
represented as verified by these tests.
"""
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.principal import refresh_principal
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


async def connect(fixture):
    s, owner, worker, _, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    s.config = replace(s.config, events_enabled=True)

    async def receiver(url, body, headers):
        payload = json.loads(body)
        return Reply(200, canonical({'challenge': payload['challenge']}).encode()) if payload.get('type') == 'verification' else Reply(204)

    s.events = EventService(s, receiver)
    c = SimpleNamespace(s=s, owner=owner,
        actor=replace(worker, grant_id='broad', actor='mcp:broad:consumer'),
        room=room, clock=clock, scope=scope)
    c.dot, c.binding, c.sub = await add_dot(c)
    c.actor = refresh_principal(s.store, c.actor)
    return c


async def add_dot(c, *, unified=True):
    dot = c.s.dots.create({**c.scope, 'conversation_id': c.room['id'],
        'label': 'consumer fixture', 'capabilities': ['read', 'write', 'execute'],
        'acknowledge_unsandboxed_exec': True, 'confirm_tasks': True,
        'duplex': True, 'idempotency_key': key()}, c.owner)['dot']
    c.s.joining.join({'code': dot['join_code'], 'idempotency_key': key()}, c.actor)
    dot = c.s.dots.find(dot['id'])
    if not unified:
        return dot, None, None
    configured = c.s.dot_mailbox.configure({**c.scope, 'dot_id': dot['id'],
        'protocol_version': 2, 'confirm_consumer_protocol': True, 'idempotency_key': key()}, c.actor)
    subscribed = await c.s.events.subscribe({**configured['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/consumer/' + dot['id'],
                     'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}}, c.actor)
    return dot, c.s.dot_mailbox.binding(dot['id']), c.s.store.one(
        'SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscribed['id'],))


def request(c, *, dot=None, binding=None, sub=None, ticket=None):
    dot, binding, sub = dot or c.dot, binding or c.binding, sub or c.sub
    ticket = ticket or c.s.dot_consumers.issue_ticket(c.room, dot, sub, binding['generation'], key())
    return {**c.scope, 'dot_id': dot['id'], 'binding_generation': binding['generation'],
            'wake_ticket': ticket, 'consumer_run_id': key(), 'idempotency_key': key()}


def proof(session):
    return {field: session[field] for field in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}


def lease(c, session, **extra):
    return {**c.scope, 'dot_id': c.dot['id'], **proof(session), 'idempotency_key': key(), **extra}


def message(c, text='Please review the current plan.'):
    return c.s.chatroom.create({**c.scope, 'room_id': c.room['id'],
        'conversation_id': c.dot['conversation_id'], 'body_text': text,
        'mentions': [{'slot_id': c.dot['id']}], 'client_message_id': key(),
        'idempotency_key': key()}, c.owner)['message']


@pytest.mark.asyncio
async def test_lost_begin_reply_replays_exact_proof_without_extending_lease(collab):
    c = await connect(collab)
    raw = request(c)
    first = c.s.dot_consumers.begin(raw, c.actor)
    c.clock[0] += 20
    replay = c.s.dot_consumers.begin(raw, c.actor)
    assert proof(first) == proof(replay)
    assert first['lease_until'] == replay['lease_until'] and replay['replayed']
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.begin({**raw, 'consumer_run_id': key(), 'idempotency_key': key()}, c.actor)
    assert error.value.code == 'DOT_WAKE_TICKET_REDEEMED'
    session = c.s.store.one('SELECT * FROM dot_consumer_sessions')
    stored_ticket = c.s.store.one('SELECT * FROM dot_wake_tickets')
    assert first['consumer_proof'] not in canonical(session)
    assert raw['wake_ticket'] not in canonical(stored_ticket)
    assert first['consumer_proof'] not in canonical(c.s.store.all('SELECT * FROM collaboration_idempotency'))
    assert raw['wake_ticket'] not in canonical(c.s.store.all('SELECT * FROM collaboration_audit'))


@pytest.mark.asyncio
async def test_ticket_is_slot_bound_and_expired_owner_cannot_write(collab):
    c = await connect(collab)
    other, binding, sub = await add_dot(c)
    raw = request(c)
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.begin(request(c, dot=other, binding=binding, sub=sub, ticket=raw['wake_ticket']), c.actor)
    assert error.value.code == 'DOT_WAKE_TICKET_INVALID'
    first = c.s.dot_consumers.begin(raw, c.actor)
    next_raw = request(c)
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.begin(next_raw, c.actor)
    assert error.value.code == 'DOT_CONSUMER_BUSY'
    c.clock[0] = first['lease_until'] + 1
    second = c.s.dot_consumers.begin(next_raw, c.actor)
    assert second['consumer_fence'] > first['consumer_fence']
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.guard(proof(first), c.actor, c.room, c.dot)
    assert error.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'
    assert c.s.dot_consumers.guard(proof(second), c.actor, c.room, c.dot)['id'] == second['consumer_session_id']
    assert c.s.dot_consumers.begin(raw, c.actor)['state'] == 'expired'


@pytest.mark.asyncio
async def test_status_and_inbox_reads_have_no_writes_or_credentials(collab):
    c = await connect(collab)
    message(c)
    raw = request(c)
    first = c.s.dot_consumers.begin(raw, c.actor)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    status = c.s.dot_consumers.read({**c.scope, 'dot_id': c.dot['id']}, c.actor)
    page = c.s.dot_mailbox.read({**c.scope, 'dot_id': c.dot['id'],
        'binding_generation': c.binding['generation']}, c.actor)
    inbox = c.s.dot_chat.inbox({**c.scope, 'dot_id': c.dot['id']}, c.actor)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert inbox['items'][0]['reply_arguments']['expected_inbox_revision'] == page['dirty_revision']
    assert inbox['items'][0]['handling_arguments']['expected_inbox_revision'] == page['dirty_revision']
    assert first['consumer_proof'] not in canonical([status, page, inbox])
    assert raw['wake_ticket'] not in canonical([status, page])
    assert status['consumer']['lease_until'] == first['lease_until']


@pytest.mark.asyncio
async def test_heartbeat_replay_and_boolean_fields_do_not_extend_authority(collab):
    c = await connect(collab)
    first = c.s.dot_consumers.begin(request(c), c.actor)
    raw = lease(c, first)
    c.clock[0] += 10
    renewed = c.s.dot_consumers.heartbeat(raw, c.actor)
    c.clock[0] += 15
    assert c.s.dot_consumers.heartbeat(raw, c.actor) == renewed
    assert renewed['lease_until'] == first['lease_until'] + 10
    for changed in ({'consumer_fence': True}, {'lease_seconds': True}):
        with pytest.raises(DevError):
            c.s.dot_consumers.heartbeat({**raw, **changed}, c.actor)
    with pytest.raises(DevError):
        c.s.dot_consumers.yield_session(lease(c, first, expected_inbox_revision=False), c.actor)
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.guard({**proof(first), 'expected_inbox_revision': False}, c.actor, c.room, c.dot, require_revision=True)
    assert error.value.code == 'DOT_INBOX_CHANGED'


@pytest.mark.asyncio
async def test_yield_cannot_drop_new_or_unacknowledged_ready_input(collab):
    c = await connect(collab)
    first = c.s.dot_consumers.begin(request(c), c.actor)
    old_revision = c.s.dot_mailbox.current_revision(c.dot['id'])
    c.s.dot_mailbox.mark_dirty(c.dot, 'control_notice', 'new-control', 1, {'reason_code': 'scope_notice'})
    new_revision = c.s.dot_mailbox.current_revision(c.dot['id'])
    assert new_revision > old_revision
    raced = c.s.dot_consumers.yield_session(lease(c, first, expected_inbox_revision=old_revision), c.actor)
    assert raced['more'] and raced['state'] == 'active'
    unread = c.s.dot_consumers.yield_session(lease(c, first, expected_inbox_revision=new_revision), c.actor)
    assert unread['more']
    page = c.s.dot_mailbox.read({**c.scope, 'dot_id': c.dot['id'], 'binding_generation': c.binding['generation']}, c.actor)
    ack = dict(page['items'][0]['ack_request']['arguments'])
    ack.pop('action')
    c.s.dot_mailbox.ack({**ack, **proof(first)}, c.actor)
    raw = lease(c, first, expected_inbox_revision=new_revision)
    released = c.s.dot_consumers.yield_session(raw, c.actor)
    assert not released['more'] and released['state'] == 'yielded'
    assert c.s.dot_mailbox.binding(c.dot['id'])['drained_revision'] == new_revision
    assert c.s.dot_consumers.yield_session(raw, c.actor) == released
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.heartbeat(lease(c, first), c.actor)
    assert error.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'


@pytest.mark.asyncio
async def test_public_reply_requires_proof_and_fresh_revision(collab):
    c = await connect(collab)
    source = message(c)
    raw = {**c.scope, 'dot_id': c.dot['id'], 'body_text': 'Working on this.',
        'reply_to_id': source['id'], 'complete': False, 'idempotency_key': key()}
    with pytest.raises(DevError) as error:
        c.s.dot_chat.message(raw, c.actor)
    assert error.value.code == 'DOT_CONSUMER_PROOF_REQUIRED'
    first = c.s.dot_consumers.begin(request(c), c.actor)
    interim = c.s.dot_chat.message({**raw, **proof(first)}, c.actor)
    assert not interim['complete']
    observed = c.s.dot_mailbox.current_revision(c.dot['id'])
    message(c, 'Please include the newly clarified requirement.')
    final = {**raw, **proof(first), 'body_text': 'Completed.', 'complete': True,
             'expected_inbox_revision': observed, 'idempotency_key': key()}
    with pytest.raises(DevError) as error:
        c.s.dot_chat.message(final, c.actor)
    assert error.value.code == 'DOT_INBOX_CHANGED'
    final['expected_inbox_revision'] = c.s.dot_mailbox.current_revision(c.dot['id'])
    assert c.s.dot_chat.message(final, c.actor)['complete']


@pytest.mark.asyncio
async def test_subscription_expiry_never_reopens_legacy_write_path(collab):
    c = await connect(collab)
    first = c.s.dot_consumers.begin(request(c), c.actor)
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?', (c.clock[0] - 1, c.sub['id']))
    assert c.s.dot_consumers.active_binding(c.dot['id'])
    for credentials in ({}, proof(first)):
        with pytest.raises(DevError) as error:
            c.s.dot_consumers.guard(credentials, c.actor, c.room, c.dot)
        assert error.value.code == 'DOT_CONSUMER_SUBSCRIPTION_INACTIVE'


@pytest.mark.asyncio
async def test_scope_change_blocks_begin_replay_and_unmapped_work_cannot_downgrade(collab, monkeypatch):
    c = await connect(collab)
    raw = request(c)
    first = c.s.dot_consumers.begin(raw, c.actor)
    legacy, _, _ = await add_dot(c, unified=False)
    goal = {'id': 'unmapped-goal', 'room_id': c.room['id'], 'conversation_id': c.dot['conversation_id']}
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.guard_work({'dot_id': legacy['id']}, c.actor, goal)
    assert error.value.code == 'DOT_CONSUMER_PROOF_REQUIRED'
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.guard_work({**proof(first), 'dot_id': legacy['id']}, c.actor, goal)
    assert error.value.code == 'DOT_CONSUMER_WORK_MISMATCH'
    monkeypatch.setattr(c.s.message_handling, 'scope_digest', lambda room, dot: 'new-scope')
    with pytest.raises(DevError) as error:
        c.s.dot_consumers.begin(raw, c.actor)
    assert error.value.code == 'DOT_CONSUMER_SCOPE_CHANGED'


@pytest.mark.asyncio
async def test_legacy_cpj_delegation_keeps_its_authority_beside_same_grant_v2_dot(collab):
    c = await connect(collab)
    session = c.s.dot_consumers.begin(request(c), c.actor)
    slot = c.s.joining.create({**c.scope, 'label': 'legacy CPJ', 'kind': 'dot',
        'idempotency_key': key()}, c.owner)['slot']
    c.s.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, c.actor)
    assert c.s.dots.find(slot['id']) is None
    policy = c.s.delegation.set_policy({**c.scope, 'conversation_id': c.room['id'],
        'slot_id': slot['id'], 'expected_version': 0, 'purpose': 'Review the existing approved scope',
        'capabilities': ['read'], 'acknowledge_unsandboxed_exec': False,
        'idempotency_key': key()}, c.owner)['policy']
    sent = c.s.chatroom.create({**c.scope, 'room_id': c.room['id'], 'conversation_id': c.room['id'],
        'body_text': 'Read and report the existing approved fixture.',
        'mentions': [{'slot_id': slot['id']}], 'client_message_id': key(), 'idempotency_key': key(),
        'delegation': {'policy_id': policy['id'], 'policy_version': policy['version'],
                       'acceptance': 'Report the evidence.'}}, c.owner)
    goal = c.s.coordination.object(c.room, sent['goal_id'])
    item = c.s.coordination.work(goal, sent['work_item_id'])
    claimed = c.s.invoke('collaboration_work', {**c.scope, 'action': 'claim',
        'goal_id': goal['id'], 'work_item_id': item['id'], 'expected_version': item['version'],
        'idempotency_key': key()}, c.actor)['work_item']
    assert claimed['state'] == 'leased'
    for supplied in ({'dot_id': c.dot['id']}, proof(session)):
        with pytest.raises(DevError) as error:
            c.s.invoke('collaboration_work', {**c.scope, 'action': 'heartbeat',
                'goal_id': goal['id'], 'work_item_id': item['id'], 'attempt': claimed['attempt'],
                'fencing_token': claimed['fencing_token'], 'idempotency_key': key(),
                **supplied}, c.actor)
        assert error.value.code == 'DOT_CONSUMER_WORK_MISMATCH'
    assert c.s.coordination.work(goal, item['id'])['fencing_token'] == claimed['fencing_token']
