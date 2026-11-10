"""Durable room/Weixin input recovery with synthetic callback and provider."""
import json

import pytest

from hub.collaboration import room_waiting_inputs, room_weixin_native
from hub.collaboration.common import canonical
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_consumer_sessions import connect, send, verify
from tests.test_room_managed_work import connected, save_room_permissions
from tests.test_room_members_rebuild import rooms as rooms
from tests.test_room_weixin_native import setup_channel


def member_action(s, owner, member_id, action):
    flow = s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (member_id,))
    current = s.rooms.connect_read({'flow_id': flow['id']}, owner)['flow']
    return s.rooms.connect_action({'flow_id': flow['id'], 'action': action,
        'expected_version': current['version'], 'idempotency_key': key()}, owner)


def change_lead(s, owner, conversation_id, member_id):
    room = s.rooms.read({'conversation_id': conversation_id}, owner)['room']
    s.rooms.update({'conversation_id': conversation_id, 'lead_member_id': member_id,
                    'expected_version': room['version'], 'idempotency_key': key()}, owner)


def inbox_messages(s, member_id):
    return [row['object_id'] for row in s.store.all("""SELECT object_id FROM dot_mailbox_entries
        WHERE kind='message' AND member_id=? ORDER BY sequence""", (member_id,))]


@pytest.mark.asyncio
async def test_room_pause_resume_dispatches_more_than_two_batches_in_order(rooms, monkeypatch):
    monkeypatch.setattr(room_waiting_inputs, 'BATCH_SIZE', 2)
    c = await connect(rooms)
    verify(c)
    member_id = c.joined['member_id']
    member_action(c.s, c.owner, member_id, 'pause')
    inputs = [send(c, '排队消息 ' + str(i)) for i in range(5)]
    assert all(item['routing']['reason_code'] == 'member_paused' for item in inputs)
    assert not inbox_messages(c.s, member_id)
    member_action(c.s, c.owner, member_id, 'resume')
    later = send(c, '恢复后发送')
    assert later['routing']['reason_code'] == 'waiting_earlier_input'
    for _ in range(6):
        room_waiting_inputs.recover_waiting(c.s)
    assert inbox_messages(c.s, member_id) == [item['message']['id'] for item in [*inputs, later]]
    assert not c.s.store.one('SELECT 1 FROM room_waiting_inputs')


@pytest.mark.asyncio
async def test_weixin_keeps_intake_without_lead_and_fences_old_poll_on_lead_change(collab):
    (s, owner, _, clock, first, _), flow, _, action = await setup_channel(collab, tasks=True)
    assert flow['routing_mode'] == 'member'
    preview = flow['follow_task_scope']
    flow = await action(flow, 'approve_tasks', expected_scope_digest=preview['scope_digest'],
        confirm_follow_room=True, confirm_owner_identity=True, confirm_future_tasks=True,
        confirm_result_delivery=True, confirm_until_revoked=True, confirm_pairing_notices=True)
    assert flow['routing_mode'] == 'follow_room'

    def updates(identifier, body):
        return packet(identifier, body, create_time_ms=int(clock[0] * 1000))

    def receive(identifier, body):
        claim = s.weixin.claim_poll(flow['account_id'])
        result = s.weixin.commit_updates(claim, updates(identifier, body))
        clock[0] += 3
        return result['message_ids'][0]

    earlier = receive(2, '仍归原 dot 的任务')
    _, _, _, _, second, _ = await connected(collab, conversation_id=first['conversation_id'])
    change_lead(s, owner, first['conversation_id'], '')
    waiting = receive(3, '没有主责时仍保存')
    waiting_view = s.room_runtime.read(first['conversation_id'], owner, message_id=waiting)['message']
    assert waiting_view['routing']['reason_code'] == 'lead_member_required'
    assert waiting not in inbox_messages(s, first['member_id'])
    assert waiting not in inbox_messages(s, second['member_id'])

    stale = s.weixin.claim_poll(flow['account_id'])
    change_lead(s, owner, first['conversation_id'], second['member_id'])
    with pytest.raises(DevError) as rejected:
        s.weixin.commit_updates(stale, updates(4, '旧轮询租约不能提交'))
    assert rejected.value.code == 'WEIXIN_POLL_FENCE_LOST'
    latest = receive(4, '新任务由当前主责收取')
    assert inbox_messages(s, first['member_id']) == [earlier]
    assert inbox_messages(s, second['member_id']) == [waiting, latest]
    assert not s.store.one('SELECT 1 FROM room_waiting_inputs')


@pytest.mark.asyncio
@pytest.mark.parametrize('historical', [False, True])
async def test_deferred_weixin_replay_uses_first_observation_or_chat_only(collab, monkeypatch, historical):
    monkeypatch.setattr(room_weixin_native, 'MAX_PENDING_NATIVE_INPUTS', 1)
    (s, owner, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab, tasks=True)
    room_id = identity['conversation_id']
    occupied = s.room_runtime.send({'conversation_id': room_id, 'body_text': '占用一个输入位置',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    response = packet(2, '检查原项目说明', create_time_ms=int(clock[0] * 1000))
    deferred = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), response)
    assert deferred['cursor_persisted'] and not deferred['message_ids']
    failure = s.store.one("SELECT * FROM channel_inbound_failures WHERE state='deferred'")
    saved = json.loads(s.store.decrypt(failure['body']))
    first_policy = saved['authority']['evidence']['policy_version']
    if historical:
        # A legacy deferred packet had the encrypted provider body but no
        # authority snapshot. Replaying it must never infer new authority.
        saved.pop('authority')
        with s.store.transaction():
            s.store.execute('UPDATE channel_inbound_failures SET body=? WHERE id=?',
                            (s.store.encrypt(canonical(saved)), failure['id']))

    policy = save_room_permissions(s, owner, room_id, [
        {'project_id': 'proj', 'capabilities': ['read', 'write']}])
    assert policy['version'] > first_policy
    with s.store.transaction():
        s.room_consumers.ack_message(s.rooms.member(identity['member_id']), occupied['id'])
    s.weixin.replay_deferred(flow['account_id'])
    receipt = s.store.one('SELECT state FROM channel_inbound_failures WHERE id=?', (failure['id'],))
    assert receipt['state'] == 'committed'
    source_id = s.store.one("SELECT message_id FROM weixin_inbound WHERE external_id='2'")['message_id']
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (source_id,))
    proof = s.room_work_bridge.native.input_authority.load(source)
    if historical:
        assert proof is None and not s.channel_owner.author(source)['authenticated']
    else:
        assert proof['policy_version'] == first_policy
        assert proof['room_rules'][0]['capabilities'] == ['read']
        assert s.channel_owner.author(source)['authenticated']


@pytest.mark.asyncio
async def test_follow_room_routing_can_be_approved_for_chat_without_task_consent(collab):
    (s, owner, _, _, identity, _), flow, _, action = await setup_channel(collab)
    assert flow['follow_task_scope']['project_ids'] == []
    flow = await action(flow, 'approve_tasks', confirm_follow_room=True,
                        expected_scope_digest=flow['follow_task_scope']['scope_digest'])
    assert flow['routing_mode'] == 'follow_room' and not flow['work_authorized']
    assert not s.store.one('SELECT 1 FROM room_channel_owner_authorizations WHERE binding_id=?',
                           (flow['binding']['id'],))
    assert s.room_runtime.read(identity['conversation_id'], owner)['messages'] == []


@pytest.mark.asyncio
async def test_weixin_revision_retires_original_waiting_input_at_capacity(collab, monkeypatch):
    monkeypatch.setattr(room_weixin_native, 'MAX_PENDING_NATIVE_INPUTS', 1)
    (s, owner, _, clock, identity, _), flow, _, _ = await setup_channel(collab)
    member_id = identity['member_id']
    member_action(s, owner, member_id, 'pause')
    created = int(clock[0] * 1000)
    original = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '原消息', create_time_ms=created))['message_ids'][0]
    assert s.store.one('SELECT 1 FROM room_waiting_inputs WHERE message_id=?', (original,))
    clock[0] += 3
    revised = packet(2, '修订消息', create_time_ms=created)
    revised['msgs'][0]['update_time_ms'] = int(clock[0] * 1000)
    changed = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), revised)
    assert changed['cursor_persisted'] and len(changed['message_ids']) == 1
    assert changed['message_ids'][0] != original
    assert not s.store.one('SELECT 1 FROM room_waiting_inputs')
    member_action(s, owner, member_id, 'resume')
    assert not inbox_messages(s, member_id)
    stored = s.room_runtime.read(identity['conversation_id'], owner)['messages']
    assert [row['id'] for row in stored] == [original, changed['message_ids'][0]]
    assert all(row['routing']['state'] == 'terminal' for row in stored)


def test_waiting_input_domain_error_rolls_back_only_that_input(collab, monkeypatch):
    service, owner, *_ = collab
    inputs = []
    for title in ('first', 'second'):
        room = service.rooms.create({'title': title, 'idempotency_key': key()}, owner)['room']
        inputs.append(service.room_runtime.send({'conversation_id': room['id'], 'body_text': title,
            'client_message_id': key(), 'idempotency_key': key()}, owner)['message']['id'])
    dispatch = room_waiting_inputs._dispatch
    seen = []

    def dispatch_with_failure(c, row):
        seen.append(row['message_id'])
        if row['message_id'] == inputs[0]:
            c.store.execute("INSERT INTO meta VALUES ('test_waiting_rollback','uncommitted')")
            raise DevError('ROOM_MEMBER_UNBOUND', 'Synthetic recovery failure', 409)
        dispatch(c, row)

    monkeypatch.setattr(room_waiting_inputs, '_dispatch', dispatch_with_failure)
    room_waiting_inputs.recover_waiting(service)
    assert seen == inputs
    assert not service.store.one("SELECT 1 FROM meta WHERE key='test_waiting_rollback'")
    assert service.store.one('SELECT 1 FROM room_waiting_inputs WHERE message_id=?', (inputs[0],))
