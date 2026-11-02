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


def send(c, text='你好，帮我看看这段想法。'):
    return c.s.room_runtime.send({'conversation_id': c.room['id'], 'member_id': c.joined['member_id'],
        'body_text': text, 'client_message_id': key(), 'idempotency_key': key()}, c.owner)


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
