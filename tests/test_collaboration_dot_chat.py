"""Bidirectional conversations are not tasks; external model autonomy is not mocked as proven."""
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import MESSAGE_EVENT, DELEGATION_EVENT
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dots import setup, call


def duplex(fixture, **extra):
    return setup(fixture, duplex=True, **extra)


def owner_message(c, body='你好，先聊聊方案。', **extra):
    raw = {**c.scope, 'room_id': c.room['id'], 'conversation_id': c.slot['conversation_id'], 'body_text': body,
        'mentions': [{'slot_id': c.slot['id']}], 'client_message_id': key(), 'idempotency_key': key(), **extra}
    return c.s.chatroom.create(raw, c.owner), raw


def send_dot(c, text, reply='', **extra):
    return c.s.invoke('collaboration', {'action': 'dot_message', **c.scope,
        'dot_id': c.slot['id'], 'body_text': text, 'reply_to_id': reply, 'idempotency_key': key(), **extra}, c.actor)


def inbox(c):
    return call(c, c.connected['inbox_request'])


def no_work(c):
    assert not c.s.store.all('SELECT * FROM coordination_work')
    assert not c.s.store.all('SELECT * FROM operations')
    assert not c.s.store.all('SELECT * FROM delegation_requests')


def test_one_native_subscription_ordinary_roundtrip_and_read_receipts(collab):
    c = duplex(collab)
    assert c.connected['slot']['duplex']
    assert c.connected['subscription_requests'] == [c.connected['subscription_request']]
    assert c.connected['subscription_request']['name'] == MESSAGE_EVENT
    assert c.connected['consumer_configuration']['mode'] == 'bidirectional_chat'
    assert not c.s.store.all('SELECT * FROM conversation_writers')  # no extra generic speaking ACL
    source, raw = owner_message(c)
    assert not source['scheduled']
    assert c.s.chatroom.create(raw, c.owner)['message']['id'] == source['message']['id']
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = inbox(c)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    item = page['items'][0]
    assert item['category'] == 'message' and item['message']['body_text'] == raw['body_text']
    assert item['trusted_author'] == {'kind': 'panel_owner', 'authenticated': True}
    call(c, item['ack_request'])
    assert inbox(c)['items'][0]['state'] == 'read'  # crash after read cannot drop the message
    reply = {'action': 'dot_message', **item['reply_arguments'], 'body_text': '你更重视速度还是界面？'}
    first = c.s.invoke('collaboration', reply, c.actor)
    assert c.s.invoke('collaboration', reply, c.actor)['message']['id'] == first['message']['id']
    assert first['message']['display_name'] == '任务 dot'
    assert first['message']['reply_to_id'] == source['message']['id']
    assert not inbox(c)['items']
    receipts = c.s.chatroom.view(c.s.object('collaboration_messages', c.room, source['message']['id']))['dot_receipts']
    assert receipts[0]['state'] == 'handled' and receipts[0]['received_at']
    assert len(c.s.store.all('SELECT * FROM collaboration_dot_messages')) == 1
    no_work(c)


def test_dot_can_start_topic_then_human_reply_routes_back_without_mention(collab):
    c = duplex(collab)
    proactive = send_dot(c, '我有一个方案问题，需要你确认。')
    assert proactive['proactive'] and not proactive['scheduled']
    assert not proactive['notifications']
    reply, raw = owner_message(c, '先保持现状，我们讨论一下。', mentions=[], reply_to_id=proactive['message']['id'])
    assert reply['message']['mentions'][0]['slot_id'] == c.slot['id']
    received = inbox(c)['items'][0]
    assert received['message_id'] == reply['message']['id']
    response = send_dot(c, '好的，先讨论，不改动。', reply['message']['id'])
    assert response['message']['thread_root_id'] == proactive['message']['id']
    assert not inbox(c)['items']
    no_work(c)


def test_thread_followup_without_mention_remembers_original_dot(collab):
    c = duplex(collab)
    first, _ = owner_message(c)
    dot_reply = send_dot(c, '你希望怎么安排？', first['message']['id'])
    second, _ = owner_message(c, '先说明优缺点。', mentions=[], reply_to_id=dot_reply['message']['id'])
    assert inbox(c)['items'][0]['message_id'] == second['message']['id']
    send_dot(c, '两个方案各有优缺点。', second['message']['id'])
    third, _ = owner_message(c, '然后呢？', mentions=[], reply_to_id=first['message']['id'])
    assert inbox(c)['items'][0]['message_id'] == third['message']['id']
    no_work(c)


def test_only_explicit_model_handoff_of_owner_work_creates_one_managed_task(collab):
    c = duplex(collab)
    source, raw = owner_message(c, '请修改 README 中的示例并运行检查。')
    no_work(c)
    request = inbox(c)['items'][0]['task_request']
    task = call(c, request)
    assert call(c, {**request, 'arguments': {**request['arguments'], 'idempotency_key': key()}})['delegation_id'] == task['delegation_id']
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1
    assert not c.s.store.all('SELECT * FROM operations')  # creating work is not execution
    fresh = call(c, task['read_request'])
    assert fresh['trusted_author']['authenticated']
    assert fresh['delegation']['message_id'] == source['message']['id']
    assert c.s.chatroom.create(raw, c.owner)['message']['id'] == source['message']['id']
    assert not inbox(c)['items']
    work_page = call(c, inbox(c)['work_inbox_request'])
    assert work_page['items'][0]['category'] == 'claimable'
    call(c, work_page['items'][0]['claim_request'])
    wakes = c.s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (MESSAGE_EVENT,))
    assert len(wakes) == 1  # work is also recoverable on the sole conversation subscription
    assert json.loads(wakes[0]['data'])['message_id'] == source['message']['id']


@pytest.mark.parametrize('source', ['proactive', 'unaddressed', 'other_dot', 'old_version'])
def test_untrusted_or_unaddressed_sources_cannot_become_work(collab, source):
    c = duplex(collab)
    if source == 'proactive':
        message = send_dot(c, '我不是房主，不能自建授权。')['message']
    elif source == 'unaddressed':
        message = owner_message(c, mentions=[])[0]['message']
    elif source == 'other_dot':
        other = duplex(collab)
        message = owner_message(other)[0]['message']
    else:
        message = owner_message(c)[0]['message']
    with pytest.raises(DevError):
        c.s.invoke('collaboration_work', {'action': 'from_message', **c.scope, 'dot_id': c.slot['id'],
            'message_id': message['id'], 'expected_version': 9 if source == 'old_version' else message['version'],
            'confirm_task': True, 'idempotency_key': key()}, c.actor)
    no_work(c)


def test_paused_task_policy_does_not_block_ordinary_conversation(collab):
    c = duplex(collab)
    c.s.delegation.control({**c.scope, 'policy_id': c.slot['policy_id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    source, _ = owner_message(c)
    page = inbox(c)
    assert page['work_inbox_request'] is None and page['items'][0]['task_request'] is None
    send_dot(c, '执行权限暂不可用，但可以继续沟通。', source['message']['id'])
    assert c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)['consumer_configuration']['mode'] == 'bidirectional_chat'
    no_work(c)


def test_messages_queue_offline_and_before_join_without_historical_backfill(collab):
    c = duplex(collab, join=False)
    source, _ = owner_message(c)
    assert not source['scheduled']
    c.connected = c.s.joining.join(c.join_raw, c.actor)
    c.slot = c.connected['slot']
    assert inbox(c)['items'][0]['message_id'] == source['message']['id']
    for _ in range(2):
        c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)
        assert inbox(c)['items'][0]['message_id'] == source['message']['id']
    no_work(c)


def test_messages_pagination_does_not_skip_unhandled_earlier_items(collab):
    c = duplex(collab)
    sources = [owner_message(c, f'讨论 {i}')[0]['message'] for i in range(5)]
    request = {**c.connected['inbox_request'], 'arguments': {**c.connected['inbox_request']['arguments'], 'limit': 2}}
    found = []
    while request:
        page = call(c, request)
        found.extend(item['message_id'] for item in page['items'])
        request = page['next_page_request']
    assert found == [row['id'] for row in sources]
    assert call(c, page['resume_request'])['items'][0]['message_id'] == sources[0]['id']
    with pytest.raises(DevError):
        call(c, {**c.connected['inbox_request'], 'arguments': {**c.connected['inbox_request']['arguments'], 'cursor': 'tampered'}})


@pytest.mark.parametrize('operation', ['inbox', 'send', 'ack'])
def test_other_connection_and_revoked_slot_cannot_send_or_read(collab, operation):
    c = duplex(collab)
    source, _ = owner_message(c)
    actor = replace(c.actor, grant_id='second', actor='mcp:second:other')
    request = {'tool': 'collaboration', 'arguments': {'action': 'dot_message', **c.scope, 'dot_id': c.slot['id'],
        'body_text': 'must reject', 'idempotency_key': key()}}
    if operation == 'inbox':
        request = c.connected['inbox_request']
    elif operation == 'ack':
        request = inbox(c)['items'][0]['ack_request']
    with pytest.raises(DevError):
        call(c, request, actor)
    slot = c.s.joining.slot(c.room, c.slot['id'])
    c.s.joining.control({**c.scope, 'slot_id': slot['id'], 'expected_version': slot['version'],
        'action': 'revoke', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        call(c, request)


def test_old_task_only_dot_cannot_gain_chat_rights(collab):
    c = setup(collab)
    with pytest.raises(DevError) as exc:
        send_dot(c, 'old task slot must not gain speaking rights')
    assert exc.value.code == 'DOT_CHAT_NOT_APPROVED'
    assert c.connected['subscription_request']['name'] == DELEGATION_EVENT
    no_work(c)


@pytest.mark.asyncio
async def test_message_event_delivered_then_proactive_and_replies_do_not_echo(collab):
    import base64
    import secrets
    from hub.collaboration.events import EventService
    from tests.test_collaboration_events import Receiver
    c = duplex(collab)
    c.s.config = replace(c.s.config, events_enabled=True)
    receiver = Receiver()
    c.s.events = events = EventService(c.s, receiver)
    await events.subscribe({**c.connected['subscription_request'], 'delivery': {'mode': 'webhook',
        'url': 'https://duplex.example.invalid/events', 'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}, c.actor)
    first, _ = owner_message(c)
    await events.tick()
    assert json.loads(receiver.requests[-1][1])['data']['message_id'] == first['message']['id']
    before = len(receiver.requests)
    reply = send_dot(c, '这是普通回复。', first['message']['id'])
    send_dot(c, '这是主动发言。')
    await events.tick()
    assert len(receiver.requests) == before
    followup, _ = owner_message(c, '接着说。', mentions=[], reply_to_id=reply['message']['id'])
    await events.tick()
    assert json.loads(receiver.requests[-1][1])['data']['message_id'] == followup['message']['id']
    assert len(receiver.requests) == before + 1
    no_work(c)


def test_finished_conversation_message_cannot_later_replay_as_new_task(collab):
    c = duplex(collab)
    source, _ = owner_message(c, '先聊方案，不执行。')
    request = inbox(c)['items'][0]['task_request']
    send_dot(c, '好的，先讨论。', source['message']['id'])
    with pytest.raises(DevError) as exc:
        call(c, request)
    assert exc.value.code == 'DOT_MESSAGE_HANDLED'
    no_work(c)


def test_changed_source_is_not_advertised_as_trusted_owner(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    c.s.store.execute("UPDATE collaboration_messages SET origin='bound_connector' WHERE id=?", (source['message']['id'],))
    item = inbox(c)['items'][0]
    assert not item['trusted_author']['authenticated'] and item['task_request'] is None
    no_work(c)


def test_same_grant_two_dots_same_client_key_keep_distinct_speakers(collab):
    first, second = duplex(collab), duplex(collab)
    idem = key()
    a = send_dot(first, '第一个 dot 主动发言。', idempotency_key=idem)
    b = send_dot(second, '第二个 dot 主动发言。', idempotency_key=idem)
    assert a['message']['id'] != b['message']['id']
    assert a['message']['sender_dot_id'] != b['message']['sender_dot_id']
    no_work(first)


def test_dot_cannot_reply_across_room_or_change_permissions(collab):
    c = duplex(collab)
    grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    other_room = c.s.conversations.create({'title': '其他房间', 'projects': [c.scope], 'idempotency_key': key()}, c.owner)['conversation']
    other = c.s.chatroom.create({**c.scope, 'room_id': c.room['id'], 'conversation_id': other_room['id'],
        'body_text': '另一房间的消息', 'client_message_id': key(), 'idempotency_key': key()}, c.owner)['message']
    with pytest.raises(DevError):
        send_dot(c, '不能跨房间回复', other['id'])
    send_dot(c, '但可以在本房间主动发言。')
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == grants
    no_work(c)
