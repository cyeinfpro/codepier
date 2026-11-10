"""Native room consumption with real service transactions and a fake network.

The receiver is in-process; these tests do not claim an external host or a
human-operated WeChat scan has been accepted.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from hub.collaboration.common import canonical, digest
from hub.collaboration.events import EventService, SEALED_BODY_PREFIX
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import rooms as rooms, create, add, join, identity


async def connect(fixture):
    s, owner, actor, clock = fixture
    room = create(s, owner)
    created = add(s, owner, room)
    joined, _ = join(s, actor, created)
    received = []
    async def callback(url, body, headers):
        event = json.loads(body)
        if event.get('type') == 'verification':
            return Reply(200, canonical({'challenge': event['challenge']}).encode())
        received.append((url, event))
        return Reply(204)
    s.events = EventService(s, callback)
    c = SimpleNamespace(s=s, owner=owner, actor=actor, room=room, joined=joined,
                        clock=clock, received=received)
    c.sub = await subscribe(c, joined)
    await dispatch(c)
    c.ticket = received[-1][1]['data']['wake_ticket']
    return c


async def subscribe(c, joined):
    return await c.s.events.subscribe({**joined['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://callback.example.invalid/room/' + joined['member_id'],
        'secret': 'whsec_' + base64.b64encode(joined['member_id'][:32].encode()).decode()}}, c.actor)


async def dispatch(c):
    for item in c.s.events.reserve(4):
        await c.s.events.deliver(item)


def verify(c):
    probe = c.s.rooms.probe_read({**identity(c.joined), 'wake_ticket': c.ticket, 'idempotency_key': key()}, c.actor)
    return c.s.rooms.probe_reply({**identity(c.joined), 'probe_nonce': probe['probe_nonce'],
                                 'idempotency_key': key()}, c.actor)


def begin(c, ticket=None):
    raw = {**identity(c.joined), 'wake_ticket': ticket or c.ticket, 'consumer_run_id': key(), 'idempotency_key': key()}
    return raw, c.s.room_consumers.begin(raw, c.actor)


def credentials(c, session):
    return {**identity(c.joined), **{k: session[k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}}


def send(c, text='你好，帮我看看这段想法。', **routing):
    return c.s.room_runtime.send({'conversation_id': c.room['id'], 'member_id': c.joined['member_id'],
        'body_text': text, 'client_message_id': key(), 'idempotency_key': key(), **routing}, c.owner)


@pytest.mark.asyncio
async def test_callback_acceptance_requires_actual_ticket_nonce_read_and_reply(rooms):
    c = await connect(rooms)
    member = c.s.rooms.member(c.joined['member_id'])
    assert c.s.rooms.state(member) == 'verifying'
    with pytest.raises(DevError, match='票据'):
        c.s.rooms.probe_read({**identity(c.joined), 'wake_ticket': 'x' * 40, 'idempotency_key': key()}, c.actor)
    ready = verify(c)
    assert ready['member']['state'] == 'ready_chat'
    assert not c.s.store.one('SELECT 1 FROM coordination_goals WHERE conversation_id=?', (c.room['id'],))
    assert not c.s.store.one('SELECT 1 FROM collaboration_messages WHERE conversation_id=?', (c.room['id'],))
    delivery = c.s.store.one('SELECT body FROM mcp_event_deliveries WHERE subscription_id=?', (c.sub['id'],))
    assert delivery['body'].startswith(SEALED_BODY_PREFIX)
    assert c.ticket not in delivery['body']


@pytest.mark.asyncio
async def test_shared_grant_members_have_independent_callback_tickets_and_fences(rooms):
    c = await connect(rooms)
    second, _ = join(c.s, c.actor, add(c.s, c.owner, c.room, '第二位助手'))
    await subscribe(c, second)
    await dispatch(c)
    second_ticket = c.received[-1][1]['data']['wake_ticket']
    verify(c)
    nonce = c.s.rooms.probe_read({**identity(second), 'wake_ticket': second_ticket,
                                  'idempotency_key': key()}, c.actor)['probe_nonce']
    c.s.rooms.probe_reply({**identity(second), 'probe_nonce': nonce, 'idempotency_key': key()}, c.actor)
    with pytest.raises(DevError):
        c.s.room_consumers.begin({**identity(c.joined), 'wake_ticket': second_ticket,
            'consumer_run_id': key(), 'idempotency_key': key()}, c.actor)
    with pytest.raises(DevError):
        c.s.room_consumers.inbox({**identity(second), 'connection_proof': c.joined['connection_proof']}, c.actor)
    first_raw, first = begin(c)
    other = c.s.room_consumers.begin({**identity(second), 'wake_ticket': second_ticket,
        'consumer_run_id': key(), 'idempotency_key': key()}, c.actor)
    assert first['consumer_fence'] == other['consumer_fence'] == 1
    assert first['consumer_session_id'] != other['consumer_session_id']
    replay = c.s.room_consumers.begin(first_raw, c.actor)
    assert replay['consumer_proof'] == first['consumer_proof'] and replay['lease_until'] == first['lease_until']


@pytest.mark.asyncio
async def test_message_reply_is_once_in_original_ledger_and_yield_never_drops_input(rooms):
    c = await connect(rooms)
    verify(c)
    sent = send(c)
    _, session = begin(c)
    inbox = c.s.room_consumers.inbox(identity(c.joined), c.actor)
    entry = inbox['items'][0]
    assert entry['message']['id'] == sent['message']['id']
    yielding = {**credentials(c, session), 'expected_inbox_revision': inbox['inbox_revision'], 'idempotency_key': key()}
    assert c.s.room_consumers.yield_session(yielding, c.actor)['more']
    raw = {**credentials(c, session), 'source_message_id': entry['object_id'],
        'source_version': entry['source_version'], 'source_digest': entry['source_digest'],
        'body_text': '我已读到你的想法。', 'complete': True,
        'expected_inbox_revision': inbox['inbox_revision'], 'idempotency_key': key()}
    result = c.s.room_runtime.reply(raw, c.actor)
    replay = c.s.room_runtime.reply(raw, c.actor)
    assert result['message']['id'] == replay['message']['id']
    assert result['message']['reply_to_id'] == sent['message']['id']
    assert c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages WHERE conversation_id=?', (c.room['id'],))['n'] == 2
    yielded = c.s.room_consumers.yield_session({**yielding, 'idempotency_key': key()}, c.actor)
    assert not yielded['more'] and yielded['state'] == 'yielded'


@pytest.mark.asyncio
async def test_new_input_and_pause_fence_old_consumer_without_acknowledging(rooms):
    c = await connect(rooms)
    verify(c)
    first = send(c)
    _, session = begin(c)
    inbox = c.s.room_consumers.inbox(identity(c.joined), c.actor)
    send(c, '追加约束：先不要修改文件。')
    with pytest.raises(DevError) as changed:
        c.s.room_consumers.authorize_member({**credentials(c, session), 'expected_inbox_revision': inbox['inbox_revision']}, c.actor)
    assert changed.value.code == 'ROOM_INBOX_CHANGED'
    room = c.s.rooms.read({'conversation_id': c.room['id']}, c.owner)['room']
    c.s.rooms.update({'conversation_id': c.room['id'], 'state': 'paused',
        'expected_version': room['version'], 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        c.s.room_consumers.heartbeat({**credentials(c, session), 'idempotency_key': key()}, c.actor)
    row = c.s.store.one('SELECT * FROM dot_mailbox_entries WHERE member_id=? AND object_id=?',
                        (c.joined['member_id'], first['message']['id']))
    assert row['acknowledged_at'] is None


@pytest.mark.asyncio
async def test_synthetic_events_have_no_ticket_and_empty_room_never_loops(rooms):
    c = await connect(rooms)
    subscription = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (c.sub['id'],))
    c.s.rooms.event_test(subscription)
    await dispatch(c)
    assert c.received[-1][1]['data']['test'] and not c.received[-1][1]['data'].get('wake_ticket')
    verify(c)
    for delay in (30, 120, 600, 3600):
        c.clock[0] += delay
        assert c.s.room_consumers.recover() == 0
    assert c.s.store.one('SELECT COUNT(*) AS n FROM dot_consumer_sessions WHERE member_id=?',
                         (c.joined['member_id'],))['n'] == 0


@pytest.mark.asyncio
async def test_begin_includes_live_inbox_without_consuming_it(rooms):
    c = await connect(rooms)
    verify(c)
    sent = send(c)
    raw = {**identity(c.joined), 'wake_ticket': c.ticket, 'consumer_run_id': key(),
           'idempotency_key': key(), 'include_inbox': True}
    session = c.s.room_consumers.begin(raw, c.actor)
    entry = session['inbox']['items'][0]
    assert entry['message']['id'] == sent['message']['id']
    assert entry['reply_arguments']['source_message_id'] == entry['object_id']
    assert entry['reply_arguments']['expected_inbox_revision'] == session['inbox_revision']
    assert 'work_start_arguments' not in entry  # chat-only room
    assert session['inbox']['read_is_consumption'] is False

    replay = c.s.room_consumers.begin(raw, c.actor)
    assert replay['replayed'] is True
    assert replay['inbox']['items'][0]['id'] == entry['id']
    assert replay['consumer_proof'] == session['consumer_proof']
    c.clock[0] += 91
    expired = c.s.room_consumers.begin(raw, c.actor)
    assert expired['state'] == 'expired'
    assert 'inbox' not in expired


@pytest.mark.asyncio
async def test_begin_default_fingerprint_and_page_read_failure(rooms, monkeypatch):
    c = await connect(rooms)
    verify(c)
    raw, session = begin(c)
    replay = c.s.room_consumers.begin({**raw, 'include_inbox': False}, c.actor)
    assert replay['consumer_session_id'] == session['consumer_session_id']
    assert replay['replayed'] and 'inbox' not in replay

    c.clock[0] += 91
    send(c)
    await dispatch(c)
    next_raw = {**identity(c.joined), 'wake_ticket': c.received[-1][1]['data']['wake_ticket'],
                'consumer_run_id': key(), 'idempotency_key': key(), 'include_inbox': True}

    def unavailable(*_args):
        raise DevError('INBOX_READ_TEMPORARY', 'temporary projection error', 503)

    with monkeypatch.context() as patch:
        patch.setattr(c.s.room_consumers, 'inbox', unavailable)
        lease = c.s.room_consumers.begin(next_raw, c.actor)
    assert lease['state'] == 'active'
    assert lease['inbox_unavailable'] == 'INBOX_READ_TEMPORARY'
    recovered = c.s.room_consumers.begin(next_raw, c.actor)
    assert recovered['consumer_session_id'] == lease['consumer_session_id']
    assert recovered['inbox']['items']


@pytest.mark.asyncio
async def test_chat_reply_ignores_independent_topic_but_requires_same_thread_refresh(rooms):
    c = await connect(rooms)
    verify(c)
    first = send(c, '第一话题')
    _, session = begin(c)
    page = c.s.room_consumers.inbox(identity(c.joined), c.actor)
    first_entry = page['items'][0]
    unrelated = send(c, '第二话题')
    reply = {**credentials(c, session), **first_entry['reply_arguments'], 'body_text': '回答第一话题。',
             'idempotency_key': key(), 'complete': False}
    assert c.s.room_runtime.reply(reply, c.actor)['message']['reply_to_id'] == first['message']['id']

    send(c, '同一话题的补充', reply_to_id=first['message']['id'])
    with pytest.raises(DevError) as changed:
        c.s.room_runtime.reply({**reply, 'idempotency_key': key()}, c.actor)
    assert changed.value.code == 'ROOM_INBOX_CHANGED'
    page = c.s.room_consumers.inbox(identity(c.joined), c.actor)
    second_entry = next(item for item in page['items'] if item['object_id'] == unrelated['message']['id'])
    member = c.s.rooms.member(c.joined['member_id'])
    c.s.room_consumers.enqueue_ref(member, 'control_notice', key(), 1, {'reason_code': 'channel_owner_revoked'})
    with pytest.raises(DevError) as revoked:
        c.s.room_runtime.reply({**credentials(c, session), **second_entry['reply_arguments'], 'body_text': '回答第二话题。',
                                'idempotency_key': key()}, c.actor)
    assert revoked.value.code == 'ROOM_INBOX_CHANGED'


@pytest.mark.asyncio
async def test_room_wake_recovery_continues_after_fast_retries(rooms):
    c = await connect(rooms)
    verify(c)
    send(c)
    for count, delay in enumerate((30, 120, 600, 1800, 1800), start=1):
        c.clock[0] += delay
        assert c.s.room_consumers.recover() == 1
        assert c.s.rooms.member(c.joined['member_id'])['wake_count'] == count
    c.s.store.execute('UPDATE room_members SET wake_episode_at=? WHERE id=?',
                      (c.clock[0] - 86400, c.joined['member_id']))
    c.clock[0] += 1800
    assert c.s.room_consumers.recover() == 0
    member = c.s.rooms.member(c.joined['member_id'])
    assert member['dirty_revision'] > member['drained_revision']
    c.s.room_consumers.recover_connection(member)
    assert c.s.rooms.member(member['id'])['wake_count'] == 0


@pytest.mark.asyncio
async def test_expired_room_wake_is_dead_lettered_before_callback(rooms):
    c = await connect(rooms)
    verify(c)
    send(c)
    good_sender = c.s.events.sender

    async def temporarily_offline(*_args):
        return Reply(503)

    c.s.events.sender = temporarily_offline
    await dispatch(c)
    delivery = c.s.store.one("""SELECT id FROM mcp_event_deliveries
        WHERE subscription_id=? AND state='retry_wait'""", (c.sub['id'],))
    assert delivery is not None

    c.clock[0] += 700
    c.s.events.sender = good_sender
    before = len(c.received)
    await dispatch(c)
    expired = c.s.store.one('SELECT state,reason_code FROM mcp_event_deliveries WHERE id=?', (delivery['id'],))
    assert expired == {'state': 'dead_letter', 'reason_code': 'wake_ticket_expired'}
    assert len(c.received) == before

    assert c.s.room_consumers.recover() == 1
    await dispatch(c)
    assert len(c.received) == before + 1
    assert c.received[-1][1]['data']['wake_ticket']
    assert c.received[-1][1]['data']['wake_ticket'] != c.ticket


@pytest.mark.asyncio
async def test_decoupled_begin_replays_after_route_expiry_and_reads_require_live_proof(rooms, monkeypatch):
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'decoupled')
    c = await connect(rooms)
    verify(c)
    sent = send(c)['message']
    await dispatch(c)
    other_ticket = c.received[-1][1]['data']['wake_ticket']
    expires_at = c.clock[0] + 35
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?', (expires_at, c.sub['id']))
    raw = {**identity(c.joined), 'wake_ticket': c.ticket, 'consumer_run_id': key(),
           'idempotency_key': key(), 'include_inbox': True}
    session = c.s.room_consumers.begin(raw, c.actor)
    assert session['turn_policy'] == 'decoupled'
    assert session['lease_until'] > expires_at
    c.clock[0] = expires_at + 1

    restored = c.s.room_consumers.begin(raw, c.actor)
    for field in ('consumer_session_id', 'consumer_fence', 'consumer_proof', 'lease_until', 'hard_deadline'):
        assert restored[field] == session[field]
    assert restored['replayed'] and restored['inbox']['items'][0]['object_id'] == sent['id']
    assert c.s.store.one('SELECT COUNT(*) AS n FROM dot_consumer_sessions')['n'] == 1

    for request in (
        lambda: begin(c, other_ticket),
        lambda: c.s.room_consumers.inbox(identity(c.joined), c.actor),
    ):
        with pytest.raises(DevError) as inactive:
            request()
        assert inactive.value.code == 'ROOM_SUBSCRIPTION_INACTIVE'
    with pytest.raises(DevError) as partial:
        c.s.room_consumers.inbox({**identity(c.joined), 'consumer_fence': session['consumer_fence']}, c.actor)
    assert partial.value.code == 'INVALID_ARGUMENTS'
    with pytest.raises(DevError) as conflict:
        c.s.room_consumers.begin({**raw, 'wake_ticket': 'z' * 40}, c.actor)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'

    message = c.s.room_consumers.message({**credentials(c, session), 'message_id': sent['id']}, c.actor)
    assert message['message']['id'] == sent['id']
    c.clock[0] = session['lease_until'] + 1
    with pytest.raises(DevError) as expired:
        c.s.room_consumers.inbox(credentials(c, session), c.actor)
    assert expired.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'
    original = c.s.room_consumers.begin(raw, c.actor)
    assert original['state'] == 'expired' and original['consumer_fence'] == session['consumer_fence']


@pytest.mark.asyncio
async def test_pause_only_transport_keeps_original_lease_until_fixed_route_grace(rooms, monkeypatch):
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'decoupled')
    c = await connect(rooms)
    verify(c)
    expires_at = c.clock[0] + 120
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?', (expires_at, c.sub['id']))
    _, session = begin(c)
    c.s.store.execute("UPDATE mcp_event_subscriptions SET state='paused' WHERE id=?", (c.sub['id'],))
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'strict')
    heartbeat = {**credentials(c, session), 'lease_seconds': 300}
    for elapsed in (80, 350, 620, 890):
        c.clock[0] = expires_at - 120 + elapsed
        active = c.s.room_consumers.heartbeat({**heartbeat, 'idempotency_key': key()}, c.actor)
        assert active['hard_deadline'] == session['hard_deadline'] and active['turn_policy'] == 'decoupled'
    assert active['lease_until'] == expires_at + 900
    assert c.s.room_consumers.inbox(credentials(c, session), c.actor)['pagination_complete']
    c.clock[0] = active['lease_until']
    with pytest.raises(DevError) as expired:
        c.s.room_consumers.heartbeat({**heartbeat, 'idempotency_key': key()}, c.actor)
    assert expired.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'


@pytest.mark.asyncio
async def test_inbox_pagination_keeps_a_fixed_window_and_restarts_on_visibility_change(rooms, monkeypatch):
    c = await connect(rooms)
    verify(c)
    messages = [send(c, f'input {n}')['message']['id'] for n in range(3)]
    consumers = c.s.room_consumers
    original_visible = consumers._visible_message
    visibility = ['old-visibility']
    monkeypatch.setattr(consumers, '_visibility_digest', lambda *_: digest(visibility[0]))
    monkeypatch.setattr(consumers, '_visible_message',
                        lambda member, actor, message: original_visible(member, actor, message)
                        and (message['id'] != messages[0] or visibility[0] != 'old-visibility'))
    request = {**identity(c.joined), 'limit': 1}
    first = consumers.inbox(request, c.actor)
    assert not first['items'] and first['next_cursor'] and not first['pagination_complete']
    late = send(c, 'arrives after the first page')['message']['id']
    visibility[0] = 'new-visibility'
    page = consumers.inbox({**request, 'cursor': first['next_cursor']}, c.actor)
    assert page['visibility_changed'] and page['restart_cursor']
    assert [row['object_id'] for row in page['items']] == messages[1:2]
    restarted = consumers.inbox({**request, 'cursor': page['restart_cursor']}, c.actor)
    assert restarted['items'][0]['object_id'] == messages[0]
    last = consumers.inbox({**request, 'cursor': page['next_cursor']}, c.actor)
    assert last['pagination_complete'] and not last['next_cursor']
    assert [row['object_id'] for row in last['items']] == messages[2:]
    assert late in {row['object_id'] for row in consumers.inbox(identity(c.joined), c.actor)['items']}


@pytest.mark.asyncio
async def test_drain_check_reaches_pending_items_after_a_bounded_invisible_prefix(rooms, monkeypatch):
    from hub.collaboration.room_consumers import DRAIN_SCAN_LIMIT

    c = await connect(rooms)
    verify(c)
    consumers = c.s.room_consumers
    member = c.s.rooms.member(c.joined['member_id'])
    with monkeypatch.context() as patch:
        patch.setattr(consumers, '_wake', lambda *_: None)
        for n in range(DRAIN_SCAN_LIMIT + 1):
            consumers.enqueue_ref(member, 'control_notice', f'notice-{n}', 1, {'reason_code': 'test'})
    original_entry = consumers._entry
    monkeypatch.setattr(consumers, '_entry', lambda row, *args, **kwargs:
                        None if row['object_id'].startswith('notice-') else original_entry(row, *args, **kwargs))
    pending = send(c, 'legitimate input')['message']['id']
    _, session = begin(c)
    current = c.s.rooms.member(c.joined['member_id'])
    request = {**credentials(c, session), 'expected_inbox_revision': current['dirty_revision']}
    first = consumers.yield_session({**request, 'idempotency_key': key()}, c.actor)
    assert first['reason_code'] == 'drain_check_pending' and first['state'] == 'active'
    second = consumers.yield_session({**request, 'idempotency_key': key()}, c.actor)
    assert second['reason_code'] == 'pending_input' and second['state'] == 'active'
    row = c.s.store.one('SELECT acknowledged_at FROM dot_mailbox_entries WHERE object_id=?', (pending,))
    assert row['acknowledged_at'] is None


@pytest.mark.asyncio
async def test_continue_later_persists_one_wake_and_preserves_pending_inputs(rooms, monkeypatch):
    from hub.collaboration.room_consumers import CONTINUATION_DELAY
    from hub.collaboration.service import CollaborationService
    from hub.runtime import Runtime
    from hub.store import Store

    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'decoupled')
    c = await connect(rooms)
    verify(c)
    initial = send(c, 'unfinished input')['message']['id']
    await dispatch(c)
    _, session = begin(c)
    request = {**credentials(c, session), 'expected_inbox_revision': session['inbox_revision'],
               'yield_mode': 'continue_later', 'idempotency_key': key()}
    outcome = c.s.room_consumers.yield_session(request, c.actor)
    assert outcome['state'] == 'yielded' and outcome['pending_preserved'] and outcome['continuation_queued']
    assert c.s.room_consumers.yield_session(request, c.actor) == outcome
    with pytest.raises(DevError) as conflict:
        c.s.room_consumers.yield_session({**request, 'yield_mode': 'if_drained'}, c.actor)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    member = c.s.rooms.member(c.joined['member_id'])
    assert member['drained_revision'] < member['dirty_revision']
    assert member['consumer_continuation_at'] == c.clock[0] + CONTINUATION_DELAY
    previous = c.s
    restarted_store = Store(previous.store.directory)
    try:
        c.s = CollaborationService(Runtime(restarted_store), previous.config, clock=lambda: c.clock[0])
        c.s.events = EventService(c.s, previous.events.sender)
        assert c.s.room_consumers.yield_session(request, c.actor) == outcome
        before = len(c.received)
        latest = send(c, 'new input while continuing later')['message']['id']
        await dispatch(c)
        assert len(c.received) == before

        c.clock[0] += CONTINUATION_DELAY
        assert c.s.room_consumers.recover() == 1
        assert c.s.room_consumers.recover() == 0
        await dispatch(c)
        assert len(c.received) == before + 1
        _, next_session = begin(c, c.received[-1][1]['data']['wake_ticket'])
        assert next_session['consumer_fence'] == session['consumer_fence'] + 1
        items = c.s.room_consumers.inbox(identity(c.joined), c.actor)['items']
        assert {item['object_id'] for item in items} == {initial, latest}
    finally:
        restarted_store.close()


@pytest.mark.asyncio
async def test_heartbeat_and_yield_cannot_cross_the_expiry_of_a_replaced_fence(rooms, monkeypatch):
    import asyncio
    from threading import Barrier

    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'decoupled')
    c = await connect(rooms)
    verify(c)
    pending = send(c, '旧回合尚未确认的输入')['message']['id']
    await dispatch(c)
    old_ticket = c.received[-1][1]['data']['wake_ticket']
    begin_request, old = begin(c, old_ticket)
    next_input = send(c, '新回合输入')['message']['id']
    await dispatch(c)
    next_ticket = c.received[-1][1]['data']['wake_ticket']
    assert next_ticket != old_ticket
    c.clock[0] = old['lease_until']

    start = Barrier(3)
    consumers = c.s.room_consumers
    old_credentials = credentials(c, old)

    def concurrent(action):
        start.wait(timeout=5)
        return action()

    heartbeat, yielding, replacement = await asyncio.gather(
        asyncio.to_thread(concurrent, lambda: consumers.heartbeat({
            **old_credentials, 'lease_seconds': 300, 'idempotency_key': key()}, c.actor)),
        asyncio.to_thread(concurrent, lambda: consumers.yield_session({
            **old_credentials, 'yield_mode': 'continue_later',
            'expected_inbox_revision': old['inbox_revision'], 'idempotency_key': key()}, c.actor)),
        asyncio.to_thread(concurrent, lambda: begin(c, next_ticket)[1]),
        return_exceptions=True,
    )
    for result in (heartbeat, yielding):
        assert isinstance(result, DevError) and result.code == 'DOT_CONSUMER_LEASE_EXPIRED'
    assert replacement['state'] == 'active' and replacement['consumer_fence'] == old['consumer_fence'] + 1
    original = consumers.begin(begin_request, c.actor)
    assert original['state'] == 'expired' and original['hard_deadline'] == old['hard_deadline']

    c.clock[0] += 1
    renewed = consumers.heartbeat({**credentials(c, replacement), 'lease_seconds': 300,
                                  'idempotency_key': key()}, c.actor)
    assert renewed['hard_deadline'] == replacement['hard_deadline']
    assert c.s.store.all("SELECT id,fence FROM dot_consumer_sessions WHERE member_id=? AND state='active'",
        (c.joined['member_id'],)) == [{'id': replacement['consumer_session_id'], 'fence': replacement['consumer_fence']}]
    items = consumers.inbox(identity(c.joined), c.actor)['items']
    assert {item['object_id'] for item in items} == {pending, next_input}
