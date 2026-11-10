"""Regression coverage for rollout capabilities, route renewal, and partial replies."""
from dataclasses import replace

import pytest

from hub.collaboration.room_consumers import FAST_WAKE_DELAYS, SLOW_WAKE_DELAY, RoomConsumers
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_consumer_sessions import begin, connect, credentials, dispatch, identity, send, subscribe, verify
from tests.test_room_members_rebuild import rooms as rooms


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['strict', 'shadow', 'decoupled'])
async def test_turn_capabilities_follow_the_policy_pinned_at_begin(rooms, monkeypatch, policy):
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', policy)
    c = await connect(rooms)
    verify(c)
    basic = {'explicit_publication': True, 'reply_yield_after': True}
    gated = {'consumer_continue_later': True, 'expired_route_session_read': True}
    expected = {**basic, **gated} if policy == 'decoupled' else basic
    assert c.joined['turn_capabilities'] == expected
    assert c.joined['turn_capabilities_apply_to'] == 'new_turns'
    raw, turn = begin(c)
    assert turn['turn_capabilities'] == expected

    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'strict' if policy == 'decoupled' else 'decoupled')
    assert c.s.room_consumers.begin(raw, c.actor)['turn_capabilities'] == expected
    request = {**credentials(c, turn), 'expected_inbox_revision': turn['inbox_revision'],
               'yield_mode': 'continue_later', 'idempotency_key': key()}
    if policy == 'decoupled':
        assert c.s.room_consumers.yield_session(request, c.actor)['state'] == 'yielded'
    else:
        with pytest.raises(DevError) as blocked:
            c.s.room_consumers.yield_session(request, c.actor)
        assert blocked.value.code == 'ROOM_CONSUMER_POLICY_REQUIRED'


@pytest.mark.asyncio
@pytest.mark.parametrize(('policy', 'host_mode'), [('decoupled', 'off'), ('strict', 'on')])
async def test_live_renewed_original_route_uses_the_fixed_hard_deadline(rooms, monkeypatch, policy, host_mode):
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', policy)
    rooms[0].config = replace(rooms[0].config, host_connections_mode=host_mode)
    c = await connect(rooms)
    verify(c)
    start = c.clock[0]
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?', (start + 30, c.sub['id']))
    source = send(c, 'Keep the original conversation during renewal.')['message']
    await dispatch(c)
    _, turn = begin(c, c.received[-1][1]['data']['wake_ticket'])
    c.clock[0] += 10
    renewed = await subscribe(c, c.joined)
    assert renewed['id'] == c.sub['id']
    route = c.s.store.one('SELECT expires_at FROM mcp_event_subscriptions WHERE id=?', (renewed['id'],))
    assert route['expires_at'] > turn['hard_deadline']

    consumer = c.s.room_consumers
    heartbeat = {**credentials(c, turn), 'lease_seconds': 300}
    active = consumer.heartbeat({**heartbeat, 'idempotency_key': key()}, c.actor)
    for elapsed in range(250, 3600, 250):
        c.clock[0] = start + elapsed
        active = consumer.heartbeat({**heartbeat, 'idempotency_key': key()}, c.actor)
    assert active['lease_until'] == turn['hard_deadline']
    inbox = consumer.inbox(credentials(c, turn), c.actor)
    item = next(item for item in inbox['items'] if item['object_id'] == source['id'])
    reply = c.s.room_runtime.reply({**credentials(c, turn), **item['reply_arguments'], 'publish_work': False,
                                    'input_handling': 'answered', 'body_text': 'The original turn remains active.',
                                    'idempotency_key': key()}, c.actor)
    assert reply['input_handling'] == 'answered'
    c.clock[0] = turn['hard_deadline']
    with pytest.raises(DevError) as expired:
        consumer.heartbeat({**heartbeat, 'idempotency_key': key()}, c.actor)
    assert expired.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'


@pytest.mark.asyncio
async def test_omitted_input_handling_is_reported_and_pending_recovery_coalesces(rooms):
    c = await connect(rooms)
    verify(c)
    source = send(c, 'Explain the issue, then fix it.')['message']
    _, turn = begin(c)
    consumer = c.s.room_consumers
    inbox = consumer.inbox(identity(c.joined), c.actor)
    item = next(item for item in inbox['items'] if item['object_id'] == source['id'])
    request = {**credentials(c, turn), **item['reply_arguments'], 'publish_work': False,
               'yield_after': True, 'body_text': 'The cause is known, but work admission is still pending.',
               'idempotency_key': key()}
    result = c.s.room_runtime.reply(request, c.actor)
    assert result['input_handling'] == result['historical_result']['input_handling'] == 'pending'
    assert result['reason_code'] == 'pending_input'
    assert c.s.room_runtime.reply(request, c.actor)['input_handling'] == 'pending'
    c.clock[0] = turn['lease_until'] + 1
    for delay in FAST_WAKE_DELAYS:
        c.clock[0] += delay
        assert consumer.recover() == 1

    c.s.room_consumers = consumer = RoomConsumers(c.s)
    c.clock[0] += SLOW_WAKE_DELAY
    assert consumer.recover() == 0
    assert c.s.store.one('SELECT acknowledged_at FROM dot_mailbox_entries WHERE id=?', (item['id'],)) == {
        'acknowledged_at': None}
    assert source['id'] in {item['object_id'] for item in consumer.inbox(identity(c.joined), c.actor)['items']}

    later = send(c, 'A different request has arrived.')['message']
    c.clock[0] += FAST_WAKE_DELAYS[0]
    assert consumer.recover() == 1
    assert later['id'] in {item['object_id'] for item in consumer.inbox(identity(c.joined), c.actor)['items']}
