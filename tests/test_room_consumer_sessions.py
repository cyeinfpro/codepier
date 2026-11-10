"""Native room consumption with real service transactions and a fake network.

The receiver is in-process; these tests do not claim an external host or a
human-operated WeChat scan has been accepted.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from hub.collaboration.common import canonical
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
