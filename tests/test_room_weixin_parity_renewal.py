"""Subscription renewal uses an isolated callback, never the real dot host."""
import base64
import json

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.lifecycle import CollaborationLoops
from hub.collaboration.network import Reply
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import add, create, join, rooms as rooms
from tests.test_room_consumer_sessions import send, verify


async def start(rooms, *, managed=False, ttl=60_000):
    from types import SimpleNamespace

    s, owner, actor, clock = rooms
    room = create(s, owner)
    if managed:
        created = s.rooms.create_member({'conversation_id': room['id'], 'name': '长期连接',
            'keep_connected': True, 'idempotency_key': key()}, owner)
    else:
        created = add(s, owner, room)
    joined, _ = join(s, actor, created)
    c = SimpleNamespace(s=s, owner=owner, actor=actor, clock=clock, room=room, joined=joined,
                        challenges=0, fail_callback=False, deliveries=[])
    async def callback(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            c.challenges += 1
            if c.fail_callback:
                return Reply(503)
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        c.deliveries.append(payload)
        return Reply(204)
    c.callback = callback
    s.events = EventService(s, callback)
    c.raw = {**joined['subscription_request'], 'ttlMs': ttl, 'delivery': {
        'mode': 'webhook', 'url': 'https://callback.example.invalid/renewal',
        'secret': 'whsec_' + base64.b64encode(b'r' * 32).decode()}}
    c.subscription = await s.events.subscribe(c.raw, actor)
    await s.events.tick()
    c.ticket = c.deliveries[-1]['data']['wake_ticket']
    verify(c)
    return c


def subscription(c):
    return c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (c.subscription['id'],))


@pytest.mark.asyncio
async def test_managed_subscription_survives_ttl_and_dispatcher_restart(rooms):
    c = await start(rooms, managed=True, ttl=86_400_000)
    initial = subscription(c)
    c.clock[0] = initial['expires_at'] - 1799
    c.s.events = EventService(c.s, c.callback)
    await CollaborationLoops(c.s).renew_room_subscriptions()
    renewed = subscription(c)
    assert c.challenges == 2
    assert renewed['expires_at'] == c.clock[0] + 86400
    assert renewed['id'] == initial['id']
    c.clock[0] = initial['expires_at'] + 1
    message = send(c, '跨过原订阅到期点后仍可交办')['message']
    await c.s.events.tick()
    assert message['display']['state'] == 'queued'
    assert c.deliveries[-1]['data']['wake_ticket']
    assert c.s.rooms.member_view(c.s.rooms.member(c.joined['member_id']))['subscription_renewal']['state'] == 'active'


@pytest.mark.asyncio
async def test_managed_short_ttl_retries_without_claiming_host_is_ready(rooms):
    c = await start(rooms, managed=True)
    original = subscription(c)
    c.clock[0] = original['expires_at'] - 14
    c.fail_callback = True
    await CollaborationLoops(c.s).renew_room_subscriptions()
    assert subscription(c)['expires_at'] == original['expires_at']
    c.clock[0] = original['expires_at'] + 1
    message = send(c, '到期时先保存')['message']
    assert message['display']['state'] == 'waiting'
    c.fail_callback = False
    await CollaborationLoops(c.s).renew_room_subscriptions()
    await c.s.events.tick()
    assert subscription(c)['expires_at'] > c.clock[0]
    assert c.deliveries[-1]['data']['wake_ticket']


@pytest.mark.asyncio
async def test_host_restores_same_connection_without_resetting_probe(rooms):
    c = await start(rooms)
    before = c.s.rooms.member(c.joined['member_id'])
    c.clock[0] = subscription(c)['expires_at'] + 1
    await c.s.events.tick()
    assert subscription(c)['state'] == 'expired'
    message = send(c, '宿主恢复后处理')['message']
    assert message['display']['reason_code'] == 'member_not_connected'
    renewed = await c.s.events.subscribe({**c.raw, 'cursor': c.subscription['cursor']}, c.actor)
    after = c.s.rooms.member(c.joined['member_id'])
    assert renewed['id'] == before['subscription_id']
    assert renewed['renewal']['mode'] == 'host'
    assert after['probe_replied_at'] == before['probe_replied_at']
    await c.s.events.tick()
    assert c.deliveries[-1]['data']['wake_ticket']


@pytest.mark.asyncio
async def test_unsubscribe_stops_managed_renewal(rooms):
    c = await start(rooms, managed=True)
    await c.s.events.call('events/unsubscribe', {**c.joined['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': c.raw['delivery']['url']}}, c.actor)
    c.clock[0] = subscription(c)['expires_at'] + 300
    await CollaborationLoops(c.s).renew_room_subscriptions()
    await c.s.events.tick()
    assert subscription(c)['state'] == 'unsubscribed'
    assert c.challenges == 1
    state = c.s.rooms.member_view(c.s.rooms.member(c.joined['member_id']))['subscription_renewal']
    assert state['state'] == 'stopped' and state['reason_code'] == 'unsubscribed'


@pytest.mark.asyncio
async def test_grant_deadline_narrowing_cannot_extend_managed_approval_later(rooms):
    c = await start(rooms, managed=True)
    previous = subscription(c)
    cutoff = c.clock[0] + 30
    c.s.store.execute("INSERT INTO grant_expirations(grant_id,expires) VALUES (?,?) ON CONFLICT(grant_id) DO UPDATE SET expires=excluded.expires",
                      (c.actor.grant_id, cutoff))
    c.s.store.execute("UPDATE grant_expirations SET expires=? WHERE grant_id=?", (cutoff + 3600, c.actor.grant_id))
    c.clock[0] = cutoff + 1
    await CollaborationLoops(c.s).renew_room_subscriptions()
    member = c.s.rooms.member(c.joined['member_id'])
    renewal = c.s.rooms.renewals.view(member)
    assert renewal['approved_until'] == cutoff
    assert renewal['reason_code'] == 'authorization_expired'
    assert renewal['state'] == 'stopped'
    assert subscription(c)['expires_at'] == previous['expires_at']


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_pause', [False, True])
async def test_member_resume_requeues_only_transiently_paused_work(collab, manual_pause):
    from tests.test_room_goal_scope_boundaries import start as start_work
    from tests.test_room_managed_work import connected

    s, owner, actor, _, identity, credentials = await connected(
        collab, [{'project': 'proj'}], [{'project_id': 'proj'}])
    started, _ = start_work(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal_id = started['goal_id']
    work_id = started['next_request']['arguments']['work_item_id']
    previous_approval = s.room_runtime.goal(goal_id)['approval_id']

    flow_id = s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (identity['member_id'],))['id']

    def change_member(action):
        flow = s.rooms.connect_read({'flow_id': flow_id}, owner)['flow']
        return s.rooms.connect_action({'flow_id': flow_id, 'action': action,
            'expected_version': flow['version'], 'idempotency_key': key()}, owner)

    change_member('pause')
    s.coordination.reconcile()
    current = s.room_runtime.goal(goal_id)
    assert current['state'] == 'active'
    assert s.coordination.work(current, work_id)['reason_code'] == 'ROOM_MEMBER_PAUSE'
    if manual_pause:
        s.room_runtime.control({'work_id': goal_id, 'action': 'pause',
            'expected_version': current['version'], 'idempotency_key': key()}, owner)
    change_member('resume')
    s.rooms.recover_paused_work()
    goal = s.room_runtime.goal(goal_id)
    assert goal['approval_id'] == previous_approval
    assert goal['state'] == ('paused' if manual_pause else 'active')
    assert s.coordination.work(goal, work_id)['state'] == ('blocked' if manual_pause else 'queued')


@pytest.mark.asyncio
async def test_member_resume_archives_settled_original_attempt_without_redispatch(collab):
    from shared.util import DevError
    from tests.test_room_goal_scope_boundaries import admit_read, start as start_work
    from tests.test_room_managed_work import connected

    s, owner, actor, _, identity, credentials = await connected(
        collab, [{'project': 'proj'}], [{'project_id': 'proj'}])
    started, _ = start_work(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal_id = started['goal_id']
    goal = s.room_runtime.goal(goal_id)
    work_id = started['next_request']['arguments']['work_item_id']
    original, admitted = admit_read(s, identity, credentials, actor, goal_id, s.coordination.work(goal, work_id), 'proj')
    operation_id = admitted['operation_id']
    s.store.execute("UPDATE operations SET state='running',attempts=1 WHERE id=?", (operation_id,))
    flow_id = s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (identity['member_id'],))['id']
    for action in ('pause', 'resume'):
        flow = s.rooms.connect_read({'flow_id': flow_id}, owner)['flow']
        s.rooms.connect_action({'flow_id': flow_id, 'action': action,
            'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    s.store.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation_id,))
    s.rooms.recover_paused_work()
    page = s.room_consumers.inbox(identity, actor)
    assert not any('archive_arguments' in item for item in page['items'])
    blocked = s.coordination.work(goal, work_id)
    assert blocked['state'] == 'blocked' and blocked['reason_code'] == 'ROOM_MEMBER_PAUSE'

    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,)),
        {'ok': True, 'data': {'content': '# Original read receipt'}})
    s.rooms.recover_paused_work()
    s.rooms.recover_paused_work()
    assert s.coordination.work(goal, work_id)['state'] == 'blocked'
    await s.events.tick()
    ticket = next(event['data']['wake_ticket'] for event in reversed(s.native_test_callbacks)
        if event['data'].get('member_id') == identity['member_id'])
    session = s.room_consumers.begin({**identity, 'wake_ticket': ticket,
        'consumer_run_id': key(), 'idempotency_key': key()}, actor)
    credentials = {name: session[name] for name in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}
    page = s.room_consumers.inbox(identity, actor)
    notices = [item for item in page['items'] if 'archive_arguments' in item]
    assert len(notices) == 1
    notice = notices[0]
    assert notice['operation_receipts'] == [{'operation_id': operation_id, 'state': 'succeeded'}]
    archive = {name: value for name, value in notice['archive_arguments'].items() if name != 'action'}
    assert archive['attempt'] == original['attempt'] and archive['fencing_token'] == original['fencing_token']
    assert archive['expected_version'] == blocked['version']
    with pytest.raises(DevError) as cannot_ack:
        s.room_consumers.ack({**identity, **credentials, 'entry_ids': [notice['id']],
            'expected_inbox_revision': page['inbox_revision'], 'idempotency_key': key()}, actor)
    assert cannot_ack.value.code == 'ROOM_INBOX_ACK_DENIED'
    result = s.coordination.archive_result({**credentials, **archive, 'outcome': 'succeeded',
        'summary': '已依据原读取回执完成结果核对。', 'idempotency_key': key()}, actor)['work_item']
    assert result['state'] == 'succeeded'
    assert result['fencing_token'] == blocked['fencing_token']
    assert s.room_runtime.goal(goal_id)['approval_id'] == goal['approval_id']
    assert [op['id'] for op in s.coordination.operations(goal)] == [operation_id]
    assert not any('archive_arguments' in item for item in s.room_consumers.inbox(identity, actor)['items'])


@pytest.mark.asyncio
async def test_owner_can_opt_in_after_verified_subscription_expires(rooms):
    c = await start(rooms)
    c.clock[0] = subscription(c)['expires_at'] + 1
    await c.s.events.tick()
    assert subscription(c)['state'] == 'expired'
    flow_id = c.s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (c.joined['member_id'],))['id']
    flow = c.s.rooms.connect_read({'flow_id': flow_id}, c.owner)['flow']
    c.s.rooms.connect_action({'flow_id': flow_id, 'action': 'set_renewal', 'keep_connected': True,
        'expected_version': flow['version'], 'idempotency_key': key()}, c.owner)
    await CollaborationLoops(c.s).renew_room_subscriptions()
    assert subscription(c)['expires_at'] == c.clock[0] + 60
    assert c.challenges == 2
