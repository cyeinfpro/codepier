"""Passive relay guarantees; no test receiver is presented as a real dot model."""
import base64
import json
import secrets
from dataclasses import replace

import pytest

from hub.collaboration.common import MESSAGE_EVENT
from hub.collaboration.events import EventService
from hub.collaboration.dot_relay import DotRelay
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, inbox, no_work
from tests.test_collaboration_dots import call
from tests.test_collaboration_events import Receiver


def interim(c, text='正在整理第一部分。'):
    item = inbox(c)['items'][0]
    return c.s.invoke('collaboration', {'action': 'dot_message', **item['interim_reply_arguments'], 'body_text': text}, c.actor)


def update(c, args, text='这是更新后的内容。', complete=False, actor=None):
    return c.s.invoke('collaboration', {'action': 'dot_update', **args, 'body_text': text, 'complete': complete}, actor or c.actor)


def source(c, identifier):
    return c.s.chatroom.view(c.s.object('collaboration_messages', c.room, identifier))


async def subscribe(c):
    c.s.config = replace(c.s.config, events_enabled=True)
    receiver = Receiver()
    c.s.events = EventService(c.s, receiver)
    sub = await c.s.events.subscribe({**c.connected['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://relay.example.invalid/events',
        'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}, c.actor)
    return receiver, sub


def test_reply_updates_keep_identity_and_terminal_text_under_replay_and_reordering(collab):
    c = duplex(collab)
    original, _ = owner_message(c)
    first = interim(c)
    original_id = first['message']['id']
    revised = update(c, first['update_arguments'], '# 第一部分\n\n```python\nprint(1)\n```')
    assert revised['message']['id'] == original_id and revised['message']['version'] == 2
    assert not revised['complete'] and inbox(c)['items'][0]['state'] == 'read'
    final = update(c, revised['update_arguments'], '# 最终回复\n\n全部说明已经写好。', complete=True)
    assert final['complete'] and final['message']['id'] == original_id
    assert final['message']['version'] == 3
    assert not inbox(c)['items']
    assert update(c, first['update_arguments'], '# 第一部分\n\n```python\nprint(1)\n```')['message']['version'] == 3
    assert source(c, original_id)['body_text'] == '# 最终回复\n\n全部说明已经写好。'
    with pytest.raises(DevError) as failure:
        update(c, {**first['update_arguments'], 'idempotency_key': key()})
    assert failure.value.code == 'STALE_VERSION'
    with pytest.raises(DevError) as failure:
        update(c, final['update_arguments'])
    assert failure.value.code == 'DOT_REPLY_FINISHED'
    assert len(c.s.store.all('SELECT * FROM collaboration_messages WHERE reply_to_id=?', (original['message']['id'],))) == 1
    assert source(c, original['message']['id'])['relay_receipts'][0]['state'] == 'replied'
    no_work(c)


@pytest.mark.parametrize('case', ['other_grant', 'other_dot', 'owner_message', 'revoked'])
def test_update_cannot_cross_original_sender_or_revoke_boundaries(collab, case):
    c = duplex(collab)
    original, _ = owner_message(c)
    reply = interim(c)
    args = dict(reply['update_arguments'])
    actor = c.actor
    if case == 'other_grant':
        actor = replace(actor, grant_id='second', actor='mcp:second:relay')
    elif case == 'other_dot':
        args['dot_id'] = duplex(collab).slot['id']
    elif case == 'owner_message':
        args['message_id'] = original['message']['id']
    else:
        current = c.s.joining.slot(c.room, c.slot['id'])
        c.s.joining.control({**c.scope, 'slot_id': current['id'], 'expected_version': current['version'],
                            'action': 'revoke', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        update(c, args, actor=actor)
    assert source(c, reply['message']['id'])['version'] == 1
    no_work(c)


def test_bulk_sync_reads_updates_without_mutation_and_filters_foreign_topic(collab):
    c = duplex(collab)
    original, _ = owner_message(c)
    first = interim(c)
    before = c.s.dot_relay.sync({**c.scope, 'conversation_id': c.slot['conversation_id']}, c.owner, '')
    update(c, first['update_arguments'], 'same message new revision')
    another = c.s.conversations.create({'title': 'Private other room', 'projects': [c.scope], 'idempotency_key': key()}, c.owner)['conversation']
    foreign = c.s.chatroom.create({**c.scope, 'room_id': c.room['id'], 'conversation_id': another['id'],
        'body_text': 'different room secret', 'client_message_id': key(), 'idempotency_key': key()}, c.owner)['message']
    changes = c.s.store.one('SELECT total_changes() AS n')['n']
    synced = c.s.dot_relay.sync({**c.scope, 'conversation_id': c.slot['conversation_id'], 'after': before['after_cursor']},
                               c.owner, ','.join([first['message']['id'], original['message']['id'], foreign['id']]))
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == changes
    assert not synced['items']
    assert {row['id'] for row in synced['updates']} == {first['message']['id'], original['message']['id']}
    assert synced['updates'][0]['body_text'] == 'same message new revision'
    assert synced['read_only'] and synced['visibility_token']
    with pytest.raises(DevError):
        c.s.dot_relay.sync(c.scope, c.owner, ','.join(['x'] * 25))


@pytest.mark.asyncio
async def test_offline_message_recovers_once_after_subscription_without_resending_user_text(collab):
    c = duplex(collab)
    messages = [owner_message(c, f'one conversation item {i}')[0]['message'] for i in range(6)]
    c.clock[0] += 31
    assert c.s.dot_relay.recover() == 0
    receiver, _ = await subscribe(c)
    assert c.s.dot_relay.recover() == 1
    assert c.s.dot_relay.recover() == 0
    await c.s.events.tick()
    assert json.loads(receiver.requests[-1][1])['data']['message_id'] == messages[0]['id']
    wakes = c.s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (MESSAGE_EVENT,))
    assert len(wakes) == 1
    assert len(inbox(c)['items']) == 6
    assert all(row['wake_count'] == 1 for row in c.s.store.all('SELECT * FROM collaboration_dot_messages'))
    assert len(c.s.store.all('SELECT * FROM collaboration_messages')) == 6
    no_work(c)


@pytest.mark.asyncio
async def test_accepted_event_is_not_receipt_and_recovery_is_bounded_across_restart(collab):
    c = duplex(collab)
    await subscribe(c)
    original, _ = owner_message(c)
    await c.s.events.tick()
    assert source(c, original['message']['id'])['relay_receipts'][0]['state'] == 'saved'
    for seconds in (31, 121, 601):
        c.clock[0] += seconds
        assert c.s.dot_relay.recover() == 1
        c.s.dot_relay = DotRelay(c.s)
        assert c.s.dot_relay.recover() == 0
    c.clock[0] += 1000
    assert c.s.dot_relay.recover() == 0
    assert c.s.store.one('SELECT wake_count FROM collaboration_dot_messages')['wake_count'] == 3
    assert source(c, original['message']['id'])['relay_receipts'][0]['model_online'] == 'unknown'
    no_work(c)


@pytest.mark.asyncio
async def test_interim_activity_delays_recovery_and_final_reply_stops_it(collab):
    c = duplex(collab)
    await subscribe(c)
    owner_message(c)
    partial = interim(c)
    c.clock[0] += 290
    assert c.s.dot_relay.recover() == 0
    update(c, partial['update_arguments'], 'still actually replying')
    c.clock[0] += 290
    assert c.s.dot_relay.recover() == 0
    c.clock[0] += 11
    assert c.s.dot_relay.recover() == 1
    current = source(c, partial['message']['id'])
    update(c, c.s.dot_relay.update_arguments(c.room, c.s.dots.find(c.slot['id']), current), complete=True)
    c.clock[0] += 1000
    assert c.s.dot_relay.recover() == 0
    no_work(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'revoke', 'grant'])
async def test_recovery_does_not_reactivate_paused_or_revoked_routes(collab, action):
    c = duplex(collab)
    _, sub = await subscribe(c)
    owner_message(c)
    if action == 'pause':
        c.s.store.execute("UPDATE mcp_event_subscriptions SET state='paused' WHERE id=?", (sub['id'],))
    elif action == 'grant':
        c.s.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (c.actor.grant_id,))
    else:
        row = c.s.joining.slot(c.room, c.slot['id'])
        c.s.joining.control({**c.scope, 'slot_id': row['id'], 'expected_version': row['version'],
            'action': 'revoke', 'idempotency_key': key()}, c.owner)
    c.clock[0] += 1000
    before = len(c.s.store.all('SELECT * FROM mcp_event_outbox'))
    assert c.s.dot_relay.recover() == 0
    assert len(c.s.store.all('SELECT * FROM mcp_event_outbox')) == before
    no_work(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [410, 413])
async def test_recovery_respects_permanent_callback_refusal(collab, status):
    c = duplex(collab)
    receiver, _ = await subscribe(c)
    original, _ = owner_message(c)
    receiver.status = status
    await c.s.events.tick()
    assert c.s.store.one('SELECT status_code FROM mcp_event_deliveries')['status_code'] == status
    c.clock[0] += 31
    assert c.s.dot_relay.recover() == 0
    assert c.s.store.one('SELECT wake_count FROM collaboration_dot_messages')['wake_count'] == 0
    assert len(inbox(c)['items']) == 1
    no_work(c)


@pytest.mark.asyncio
async def test_recovery_uses_shared_notification_budget_and_keeps_pending_messages(collab):
    c = duplex(collab)
    await subscribe(c)
    for index in range(30):
        owner_message(c, f'message {index}')
    c.clock[0] += 31
    assert c.s.dot_relay.recover() == 0
    assert len(c.s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (MESSAGE_EVENT,))) == 30
    c.clock[0] += 31
    assert c.s.dot_relay.recover() == 1
    assert len(inbox(c)['items']) == 30
    no_work(c)
