"""M3 uses real room/connection ledgers; callbacks here are synthetic transports.

No fixture flips room_protocol or fabricates a ready member. Legacy bindings
are prepared, subscribed, ticket-read and nonce-replied before owner cutover.
"""
from dataclasses import replace
import base64
import json
from types import SimpleNamespace

import pytest

from hub.collaboration.common import digest
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, inbox
from tests.test_collaboration_dots import call, setup
from tests.test_room_members_rebuild import join, subscribe, identity
from tests.test_room_managed_work import save_room_permissions


def legacy(fixture):
    c = duplex(fixture)
    c.conversation_id = c.slot['conversation_id']
    c.s.config = replace(c.s.config, events_enabled=True)
    c.member = c.s.store.one('SELECT * FROM room_members WHERE legacy_dot_id=?', (c.slot['id'],))
    return c


def current(c):
    return c.s.rooms.read({'conversation_id': c.conversation_id}, c.owner)['room']


def activate(c, **changes):
    raw = {'conversation_id': c.conversation_id, 'action': 'activate',
        'expected_version': current(c)['version'], 'idempotency_key': key(), **changes}
    return c.s.rooms.migrate_room(raw, c.owner)


def connect_action(c, member, action):
    flow = c.s.store.one('SELECT * FROM room_connect_flows WHERE member_id=?', (member['id'],))
    return c.s.rooms.connect_action({'flow_id': flow['id'], 'action': action,
        'expected_version': flow['version'], 'idempotency_key': key()}, c.owner)


async def verify_legacy_member(c, member):
    prepared = connect_action(c, member, 'migrate')
    joined, _ = join(c.s, c.actor, prepared)
    await subscribe(c.s, c.actor, joined, route=member['id'])
    probe = c.s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key()}, c.actor)
    result = c.s.rooms.probe_reply({**identity(joined), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, c.actor)
    assert result['binding_activated'] and result['room_migration_required']
    assert not result['consumer_lease_created'] and 'next_request' not in result
    return joined


def business_snapshot(c):
    tables = ('collaboration_messages', 'collaboration_dot_messages', 'dot_mailbox_entries',
        'coordination_goals', 'coordination_work', 'operations', 'channel_attachments',
        'room_member_scopes', 'room_mutations', 'room_runtime_receipts')
    return {table: c.s.store.all('SELECT * FROM ' + table + ' ORDER BY rowid') for table in tables}


def test_legacy_new_surface_is_readonly_without_polluting_any_business_ledger(collab):
    c = legacy(collab)
    saved, _ = owner_message(c, '只读历史，不能替我新建工作。')
    room = current(c)
    assert room['legacy'] and room['read_only'] and room['migration']['required']
    assert not room['migration']['eligible']
    before = business_snapshot(c)
    actions = [
        lambda: c.s.room_runtime.send({'conversation_id': c.conversation_id, 'body_text': '新输入',
            'client_message_id': key(), 'idempotency_key': key()}, c.owner),
        lambda: c.s.room_attachments.upload(c.conversation_id, 'new.txt', b'new content', key(), c.owner),
        lambda: c.s.rooms.create_member({'conversation_id': c.conversation_id, 'name': '新成员', 'idempotency_key': key()}, c.owner),
        lambda: c.s.rooms.projects({'conversation_id': c.conversation_id,
            'projects': [{'project': 'proj'}, {'project': 'otherproj'}],
            'expected_version': room['version'], 'idempotency_key': key()}, c.owner),
        lambda: c.s.rooms.projects({'conversation_id': c.conversation_id, 'projects': [{'project': 'proj'}],
            'member_approvals': [{'member_id': c.member['id'], 'project_scopes': [{'project_id': 'proj'}]}],
            'expected_version': room['version'], 'idempotency_key': key()}, c.owner),
    ]
    for action in actions:
        with pytest.raises(DevError) as caught:
            action()
        assert caught.value.code == 'ROOM_MIGRATION_REQUIRED'
        assert business_snapshot(c) == before
    changes = c.s.store.one('SELECT total_changes() n')['n']
    read = c.s.room_runtime.read(c.conversation_id, c.owner, message_id=saved['message']['id'])
    assert read['message']['id'] == saved['message']['id']
    c.s.rooms.diagnostics({'conversation_id': c.conversation_id}, c.owner)
    current(c)
    assert c.s.store.one('SELECT total_changes() n')['n'] == changes


def test_original_goal_can_be_read_and_stopped_but_not_resumed_from_new_surface(collab):
    c = legacy(collab)
    owner_message(c, '请检查 README 后报告结果。')
    task = call(c, inbox(c)['items'][0]['task_request'])
    link = c.s.store.one('SELECT goal_id FROM delegation_requests WHERE id=?', (task['delegation_id'],))
    original = c.s.room_runtime.work_read(link['goal_id'], c.owner)['work']
    assert set(original['allowed_actions']) <= {'pause', 'cancel'}
    stopped = c.s.room_runtime.control({'work_id': original['id'], 'action': 'pause',
        'expected_version': original['version'], 'idempotency_key': key()}, c.owner)['work']
    assert stopped['id'] == original['id'] and stopped['goal_state'] == 'paused'
    before = business_snapshot(c)
    for action in ('resume', 'supplement', 'restore_binding', 'takeover'):
        raw = {'work_id': original['id'], 'action': action, 'expected_version': stopped['version'],
            'idempotency_key': key(), 'body_text': '继续'}
        if action in {'restore_binding', 'takeover'}:
            raw.update(target_member_id=c.member['id'], expected_coordinator_epoch=1, expected_intent_revision=1)
        with pytest.raises(DevError) as caught:
            c.s.room_runtime.control(raw, c.owner)
        assert caught.value.code == 'ROOM_MIGRATION_REQUIRED'
    assert business_snapshot(c) == before
    assert c.s.room_runtime.work_read(original['id'], c.owner)['work']['id'] == original['id']


def test_cutover_blocks_new_manual_goal_admission_but_preserves_original_operation_receipts(collab):
    from tests.test_collaboration_coordination import setup_goal, make_work, lease

    coordinator, service, owner, _, actor, goal, scope, _ = setup_goal(collab)
    c = SimpleNamespace(s=service, owner=owner, conversation_id=goal['conversation_id'])
    create = {**scope, 'goal_id': goal['id'], 'objective': '已批准的原手工工作',
        'acceptance': '核对真实操作回执', 'assignee_grant_id': actor.grant_id,
        'target_project_id': 'proj', 'required_capabilities': ['read', 'execute'], 'idempotency_key': key()}
    created = coordinator.work_create(create, actor)
    original = created['work_item']
    claim = {**scope, 'goal_id': goal['id'], 'work_item_id': original['id'],
        'expected_version': original['version'], 'idempotency_key': key()}
    claimed = coordinator.work_claim(claim, actor)
    item = claimed['work_item']
    queued = make_work(coordinator, scope, goal, actor)
    execute = {**lease(scope, goal, item), 'tool': 'read', 'arguments': {'path': 'README.md'}}
    admitted = coordinator.admit(execute, actor)
    operation_before = service.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    assert current(c)['migration']['eligible']
    activate(c)
    assert service.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],)) == operation_before
    before_replay = business_snapshot(c)
    replayed_create = coordinator.work_create(create, actor)['work_item']
    replayed_claim = coordinator.work_claim(claim, actor)['work_item']
    assert replayed_create['id'] == replayed_claim['id'] == item['id']
    assert replayed_claim['attempt'] == item['attempt'] and replayed_claim['fencing_token'] == item['fencing_token']
    assert coordinator.admit(execute, actor) == admitted
    assert business_snapshot(c) == before_replay
    before = business_snapshot(c)
    actions = [
        lambda: coordinator.work_create({**create, 'idempotency_key': key()}, actor),
        lambda: coordinator.work_assign({**scope, 'goal_id': goal['id'], 'work_item_id': queued['id'],
            'expected_version': queued['version'], 'assignee_grant_id': actor.grant_id, 'idempotency_key': key()}, actor),
        lambda: coordinator.work_claim({**scope, 'goal_id': goal['id'], 'work_item_id': queued['id'],
            'expected_version': queued['version'], 'idempotency_key': key()}, actor),
        lambda: coordinator.admit({**execute, 'idempotency_key': key()}, actor),
        lambda: coordinator.message({**scope, 'goal_id': goal['id'], 'body_text': '旧入口不能发起新的业务对话',
            'idempotency_key': key()}, actor),
    ]
    for action in actions:
        with pytest.raises(DevError) as caught:
            action()
        assert caught.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
        assert business_snapshot(c) == before
    assert set(service.room_runtime.work_read(goal['id'], owner)['work']['allowed_actions']) <= {'pause', 'cancel'}
    for action in ('resume', 'supplement'):
        with pytest.raises(DevError) as caught:
            service.room_runtime.control({'work_id': goal['id'], 'action': action,
                'expected_version': goal['version'], 'body_text': '不能以旧工作重开执行图', 'idempotency_key': key()}, owner)
        assert caught.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    assert service.runtime.operation(admitted['operation_id'], actor,
        {'include_output': False, 'include_result': False})['id'] == admitted['operation_id']
    service.runtime.complete(operation_before, {'ok': True, 'data': {'content': 'original fixture receipt'}})
    result = {**lease(scope, goal, item), 'outcome': 'succeeded',
        'summary': '只归档切换前已准入的原操作', 'operation_ids': [admitted['operation_id']]}
    finished = coordinator.result(result, actor)
    assert finished['work_item']['state'] == 'succeeded'
    assert finished['work_item']['result']['operation_ids'] == [admitted['operation_id']]
    assert coordinator.result(result, actor) == finished
    detail = coordinator.read({**scope, 'goal_id': goal['id']}, owner)
    assert detail['goal']['id'] == goal['id']
    assert any(row['id'] == item['id'] and row['state'] == 'succeeded' for row in detail['work_items'])
    stopped = service.room_runtime.control({'work_id': goal['id'], 'action': 'pause',
        'expected_version': goal['version'], 'idempotency_key': key()}, owner)
    assert stopped['work']['id'] == goal['id'] and stopped['work']['goal_state'] == 'paused'
    assert service.store.one('SELECT COUNT(*) n FROM coordination_operations WHERE goal_id=?', (goal['id'],))['n'] == 1


@pytest.mark.asyncio
async def test_all_original_members_verify_before_explicit_room_cutover(collab):
    c = legacy(collab)
    other = setup(collab, duplex=True, label='第二位原 dot')
    second = c.s.store.one('SELECT * FROM room_members WHERE legacy_dot_id=?', (other.slot['id'],))
    source, old_raw = owner_message(c, '保留原始聊天。')
    first = await verify_legacy_member(c, c.member)
    room = current(c)
    assert room['legacy'] and room['read_only'] and room['migration']['member_counts']['verified'] == 1
    with pytest.raises(DevError) as blocked:
        activate(c)
    assert blocked.value.code == 'ROOM_MIGRATION_BLOCKED'
    assert blocked.value.details['not_saved'] is True
    with pytest.raises(DevError) as consumer:
        c.s.room_consumers.inbox(identity(first), c.actor)
    assert consumer.value.code == 'ROOM_MIGRATION_REQUIRED'
    c.clock[0] += 65
    await verify_legacy_member(c, second)
    ready = current(c)
    assert ready['migration']['eligible'] and ready['read_only']
    request = {'conversation_id': c.conversation_id, 'action': 'activate',
        'expected_version': ready['version'], 'idempotency_key': key()}
    result = c.s.rooms.migrate_room(request, c.owner)
    assert result['activated'] and not result['room']['legacy'] and not result['room']['read_only']
    assert result['room']['migration']['status'] == 'complete'
    assert c.s.chatroom.create(old_raw, c.owner)['message']['id'] == source['message']['id']
    before = business_snapshot(c)
    assert c.s.rooms.migrate_room(request, c.owner)['activated']
    assert business_snapshot(c) == before
    sent = c.s.room_runtime.send({'conversation_id': c.conversation_id, 'member_id': first['member_id'],
        'body_text': '新入口的新消息', 'client_message_id': key(), 'idempotency_key': key()}, c.owner)
    assert sent['message']['source_room_id'] is None
    assert not c.s.store.one('SELECT 1 FROM collaboration_dot_messages WHERE message_id=?', (sent['message']['id'],))
    assert c.s.store.one('SELECT 1 FROM dot_mailbox_entries WHERE member_id=? AND object_id=?',
        (first['member_id'], sent['message']['id']))
    with pytest.raises(DevError) as old_write:
        c.s.chatroom.create({**old_raw, 'body_text': '旧写路新消息', 'client_message_id': key(), 'idempotency_key': key()}, c.owner)
    assert old_write.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    with pytest.raises(DevError) as old_dot:
        c.s.dots.create({**c.raw, 'idempotency_key': key()}, c.owner)
    assert old_dot.value.code == 'ROOM_LEGACY_WRITE_DISABLED'


@pytest.mark.asyncio
async def test_candidate_failure_and_paused_original_never_activate_room(collab):
    c = legacy(collab)
    old_slot = c.s.store.one('SELECT * FROM collaboration_join_slots WHERE id=?', (c.slot['id'],))
    prepared = connect_action(c, c.member, 'migrate')
    joined, _ = join(c.s, c.actor, prepared)
    await subscribe(c.s, c.actor, joined)
    before = current(c)
    with pytest.raises(DevError) as nonce:
        c.s.rooms.probe_reply({**identity(joined), 'probe_nonce': 'invalid-nonce-' * 3, 'idempotency_key': key()}, c.actor)
    assert nonce.value.code == 'ROOM_PROBE_MISMATCH'
    assert c.s.store.one('SELECT * FROM collaboration_join_slots WHERE id=?', (c.slot['id'],)) == old_slot
    assert current(c)['version'] == before['version']
    with pytest.raises(DevError):
        activate(c)
    connect_action(c, c.member, 'pause')
    assert current(c)['migration']['required'] and not current(c)['migration']['eligible']
    connect_action(c, c.member, 'resume')
    assert c.s.store.one('SELECT state FROM delegation_policies WHERE id=?', (c.s.dots.find(c.slot['id'])['policy_id'],))['state'] == 'paused'
    c.clock[0] += 65
    await verify_legacy_member(c, c.s.rooms.member(c.member['id']))
    assert current(c)['migration']['eligible'] and current(c)['read_only']
    activate(c)


def test_removed_members_do_not_reappear_and_room_pause_can_be_resolved_without_approval(collab):
    c = legacy(collab)
    connect_action(c, c.member, 'remove')
    view = current(c)
    paused = c.s.rooms.update({'conversation_id': c.conversation_id, 'state': 'paused',
        'expected_version': view['version'], 'idempotency_key': key()}, c.owner)['room']
    assert paused['read_only'] and not paused['migration']['eligible']
    assert any(b['code'] == 'ROOM_PAUSED' for b in paused['migration']['blockers'])
    resumed = c.s.rooms.update({'conversation_id': c.conversation_id, 'state': 'active',
        'expected_version': paused['version'], 'idempotency_key': key()}, c.owner)['room']
    assert resumed['read_only'] and resumed['migration']['eligible']
    assert not c.s.store.one("SELECT 1 FROM collaboration_join_slots WHERE id=? AND state!='revoked'", (c.slot['id'],))
    assert activate(c)['room']['migration']['member_counts'] is None
    from hub.collaboration.rooms import migrate
    with c.s.store.transaction():
        migrate(c.s.store.db)
    assert c.s.rooms.member(c.member['id'])['status'] == 'removed'
    assert not current(c)['legacy']


@pytest.mark.asyncio
async def test_default_room_cutover_keeps_project_wide_subscription(collab):
    from tests.test_events_multidot_dispatch import subscriptions
    c = legacy(collab)
    _, _, subs = await subscriptions(collab)
    shared = subs[0]
    assert shared['room_id'] == c.conversation_id
    assert not json.loads(shared['arguments']).get('conversation_id')
    connect_action(c, c.member, 'remove')
    activate(c)
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (shared['id'],))['state'] == 'active'


@pytest.mark.asyncio
async def test_cutover_retires_exact_residual_legacy_subscription_without_replaying_input(collab):
    import base64
    from hub.collaboration.events import EventService
    from tests.test_events_multidot_dispatch import Receiver
    c = legacy(collab)
    c.s.events = EventService(c.s, Receiver())
    old = await c.s.events.subscribe({**c.connected['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://old.example.invalid/legacy',
        'secret': 'whsec_' + base64.b64encode(b'o' * 32).decode()}}, c.actor)
    source, _ = owner_message(c, '原输入保留原账本，不因切换伪造回复或重建工作。')
    await verify_legacy_member(c, c.member)
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old['id'],))['state'] == 'active'
    original_messages = c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id')
    original_deliveries = c.s.store.all('SELECT * FROM collaboration_dot_messages ORDER BY message_id')
    activate(c)
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old['id'],))['state'] == 'unsubscribed'
    assert c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id') == original_messages
    assert c.s.store.all('SELECT * FROM collaboration_dot_messages ORDER BY message_id') == original_deliveries
    assert c.s.room_runtime.read(c.conversation_id, c.owner, message_id=source['message']['id'])['message']['id'] == source['message']['id']
    assert not c.s.store.one('SELECT 1 FROM coordination_goals')


@pytest.mark.asyncio
async def test_verified_paused_member_remains_paused_across_explicit_room_cutover(collab):
    c = legacy(collab)
    joined = await verify_legacy_member(c, c.member)
    connect_action(c, c.s.rooms.member(c.member['id']), 'pause')
    assert current(c)['migration']['eligible']
    result = activate(c)
    assert not result['room']['read_only']
    assert c.s.rooms.member(joined['member_id'])['status'] == 'paused'
    assert not c.s.store.one('SELECT 1 FROM dot_consumer_sessions WHERE member_id=?', (joined['member_id'],))
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?',
        (c.s.rooms.member(joined['member_id'])['subscription_id'],))['state'] == 'paused'


def test_native_room_projection_does_not_reinspect_migration_candidates(collab, monkeypatch):
    service, owner, *_ = collab
    room = service.rooms.create({'title': '新房间', 'idempotency_key': key()}, owner)['room']
    save_room_permissions(service, owner, room['id'], [])
    service.rooms.create_member({'conversation_id': room['id'], 'name': '尚未接入', 'idempotency_key': key()}, owner)
    def unexpected(*_args, **_kwargs):
        raise AssertionError('native projection must not scan legacy eligibility')
    monkeypatch.setattr(service.rooms.migration, 'old_dots', unexpected)
    monkeypatch.setattr(service.rooms.migration, 'verified', unexpected)
    before = service.store.one('SELECT total_changes() n')['n']
    value = service.rooms.read({'conversation_id': room['id']}, owner)['room']['migration']
    assert value['status'] == 'not_required' and value['member_counts'] is None
    assert not value['eligibility_checked'] and not value['eligible'] and not value['blockers']
    assert service.store.one('SELECT total_changes() n')['n'] == before


def test_migration_requires_current_owner_version_and_immutable_request(collab):
    c = legacy(collab)
    connect_action(c, c.member, 'remove')
    request = {'conversation_id': c.conversation_id, 'action': 'activate',
        'expected_version': current(c)['version'], 'idempotency_key': key()}
    for actor in (c.actor, replace(c.owner, user_id='other')):
        with pytest.raises(DevError) as denied:
            c.s.rooms.migrate_room(request, actor)
        assert 'not_saved' not in denied.value.details
    with pytest.raises(DevError) as stale:
        c.s.rooms.migrate_room({**request, 'expected_version': request['expected_version'] + 1}, c.owner)
    assert stale.value.code == 'STALE_VERSION'
    assert stale.value.details['not_saved'] is True
    result = c.s.rooms.migrate_room(request, c.owner)
    c.s.rooms.update({'conversation_id': c.conversation_id, 'title': '后来改名',
        'expected_version': result['room']['version'], 'idempotency_key': key()}, c.owner)
    assert c.s.rooms.migrate_room(request, c.owner)['room']['title'] == '后来改名'
    with pytest.raises(DevError) as conflict:
        c.s.rooms.migrate_room({**request, 'expected_version': current(c)['version']}, c.owner)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert 'not_saved' not in conflict.value.details
    assert c.s.store.one('SELECT COUNT(*) n FROM room_migrations')['n'] == 1


@pytest.mark.asyncio
async def test_verified_member_cannot_mutate_business_before_room_activation(collab):
    c = legacy(collab)
    source, _ = owner_message(c)
    joined = await verify_legacy_member(c, c.member)
    credentials = {**identity(joined), 'consumer_session_id': 'unissued-session', 'consumer_fence': 1,
        'consumer_proof': 'never-issued-consumer-proof' * 2, 'expected_inbox_revision': 0, 'idempotency_key': key()}
    source_ref = {'source_message_id': source['message']['id'], 'source_version': source['message']['version'],
        'source_digest': digest(source['message']['body_text'])}
    before = business_snapshot(c)
    calls = [
        lambda: c.s.room_consumers.begin({**identity(joined), 'wake_ticket': joined['wake_ticket'],
            'consumer_run_id': key(), 'idempotency_key': key()}, c.actor),
        lambda: c.s.room_runtime.reply({**credentials, **source_ref, 'body_text': '提前回复'}, c.actor),
        lambda: c.s.room_runtime.start_work({**credentials, **source_ref, 'acceptance': '未经房间激活不应创建',
            'confirm_task': True}, c.actor),
        lambda: c.s.room_context.update({**credentials, 'expected_context_version': 0,
            'reviewed_sources': [{'message_id': source_ref['source_message_id'], 'version': source_ref['source_version'],
                                 'digest': source_ref['source_digest']}]}, c.actor),
    ]
    for invoke in calls:
        with pytest.raises(DevError) as caught:
            invoke()
        assert caught.value.code == 'ROOM_MIGRATION_REQUIRED'
    assert business_snapshot(c) == before
    assert not c.s.store.one('SELECT 1 FROM dot_consumer_sessions WHERE member_id=?', (joined['member_id'],))


@pytest.mark.asyncio
async def test_private_bridge_after_cutover_does_not_reopen_old_public_admission(collab):
    c = legacy(collab)
    joined = await verify_legacy_member(c, c.member)
    activate(c)
    save_room_permissions(c.s, c.owner, c.conversation_id, [{'project_id': 'proj',
        'capabilities': ['read', 'write', 'execute'], 'execution_targets': ['project_agent']}])
    bridge = c.s.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (joined['member_id'],))
    assert bridge and bridge['legacy_dot_id'] != c.slot['id']
    assert c.s.store.one('SELECT 1 FROM room_legacy_member_links WHERE dot_id=?', (c.slot['id'],))
    assert not c.s.store.one('SELECT 1 FROM room_legacy_member_links WHERE dot_id=?', (bridge['legacy_dot_id'],))
    assert current(c)['migration']['status'] == 'complete'
    before = business_snapshot(c)
    with pytest.raises(DevError) as old_project:
        c.s.conversations.add_project({'conversation_id': c.conversation_id, 'project': 'otherproj',
            'expected_version': current(c)['version'], 'idempotency_key': key()}, c.owner)
    assert old_project.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    with pytest.raises(DevError) as old_policy:
        policy = c.s.store.one('SELECT * FROM delegation_policies WHERE id=?', (bridge['policy_id'],))
        c.s.delegation.set_policy({**c.scope, 'conversation_id': c.conversation_id, 'slot_id': bridge['legacy_dot_id'],
            'expected_version': policy['version'], 'purpose': '旧UI不得更新私有范围', 'capabilities': ['read'],
            'execution_target': 'project_agent', 'idempotency_key': key()}, c.owner)
    assert old_policy.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    with pytest.raises(DevError) as old_mailbox:
        c.s.dot_mailbox.configure({**c.scope, 'dot_id': bridge['legacy_dot_id'],
            'confirm_consumer_protocol': True, 'idempotency_key': key()}, c.actor)
    assert old_mailbox.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    from hub.collaboration.common import DELEGATION_EVENT
    subscriptions_before = c.s.store.all('SELECT * FROM mcp_event_subscriptions ORDER BY id')
    with pytest.raises(DevError) as old_subscription:
        await c.s.events.subscribe({'name': DELEGATION_EVENT, 'arguments': {
            'project_id': 'proj', 'environment_id': 'production', 'conversation_id': c.conversation_id,
            'slot_id': bridge['legacy_dot_id'], 'policy_id': bridge['policy_id'], 'policy_version': policy['version']},
            'delivery': {'mode': 'webhook', 'url': 'https://old.example.invalid/private-adapter',
                'secret': 'whsec_' + base64.b64encode(b'v' * 32).decode()}}, c.actor)
    assert old_subscription.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    assert c.s.store.all('SELECT * FROM mcp_event_subscriptions ORDER BY id') == subscriptions_before
    with pytest.raises(DevError) as old_goal:
        c.s.coordination.create({**c.scope, 'conversation_id': c.conversation_id,
            'objective': '旧入口的新目标', 'acceptance': '不应另开执行图', 'project_ids': ['proj'],
            'participant_grant_ids': [c.actor.grant_id], 'capabilities': ['read'], 'idempotency_key': key()}, c.owner)
    assert old_goal.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    assert business_snapshot(c) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('detach_project', [False, True])
async def test_native_invited_private_adapter_keeps_original_input_until_real_connection(collab, detach_project):
    service, owner, worker, *_ = collab
    service.config = replace(service.config, events_enabled=True)
    room = service.rooms.create({'title': '尚未接入的新房间', 'projects': [{'project': 'proj'}],
        'idempotency_key': key()}, owner)['room']
    c = SimpleNamespace(s=service, owner=owner, conversation_id=room['id'])
    save_room_permissions(service, owner, room['id'],
        [{'project_id': 'proj', 'capabilities': ['read'], 'execution_targets': []}])
    created = service.rooms.create_member({'conversation_id': room['id'], 'name': '等待接入',
        'idempotency_key': key()}, owner)
    member = service.rooms.member(created['member']['id'])
    assert member['status'] == 'invited' and member['legacy_dot_id'] and not member['connection_proof_hash']
    assert service.store.one('SELECT 1 FROM room_member_work_bridges WHERE member_id=?', (member['id'],))
    assert not service.store.one('SELECT 1 FROM room_legacy_member_links WHERE member_id=?', (member['id'],))
    connect_action(c, member, 'pause')
    connect_action(c, member, 'resume')
    if detach_project:
        save_room_permissions(service, owner, room['id'], [])
    member = service.rooms.member(member['id'])
    assert member['status'] == 'invited' and member['legacy_dot_id']
    assert not service.rooms.migration.member_requires_migration(member)
    raw = {'conversation_id': room['id'], 'body_text': '保留原输入，连接完成后再读。',
        'client_message_id': key(), 'idempotency_key': key()}
    sent = service.room_runtime.send(raw, owner)
    assert sent['routing'] == {'state': 'waiting', 'member_id': member['id'], 'reason_code': 'member_not_connected'}
    message = service.store.one('SELECT * FROM collaboration_messages WHERE id=?', (sent['message']['id'],))
    assert message['room_id'] is None and message['conversation_id'] == room['id']
    pending = service.store.one('SELECT * FROM dot_mailbox_entries WHERE member_id=?', (member['id'],))
    assert pending['object_id'] == message['id'] and pending['acknowledged_at'] is None
    for table in ('dot_consumer_sessions', 'coordination_goals', 'coordination_work', 'operations',
                  'mcp_event_outbox', 'mcp_event_subscriptions', 'mcp_event_deliveries'):
        assert not service.store.one('SELECT 1 FROM ' + table)
    assert service.room_runtime.send(raw, owner)['message']['id'] == message['id']
    assert service.store.all('SELECT id FROM collaboration_messages') == [{'id': message['id']}]
    assert service.store.all('SELECT id FROM dot_mailbox_entries') == [{'id': pending['id']}]

    joined, _ = join(service, worker, created)
    await subscribe(service, worker, joined, route=member['id'])
    probe = service.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'],
        'idempotency_key': key()}, worker)
    service.rooms.probe_reply({**identity(joined), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, worker)
    received = service.room_consumers.inbox(identity(joined), worker)
    original = next(item for item in received['items'] if item['object_id'] == message['id'])
    assert original['id'] == pending['id'] and original['ready']
    assert original['message']['id'] == message['id'] and original['message']['body_text'] == raw['body_text']
    assert service.store.all('SELECT * FROM collaboration_messages') == [message]
    assert service.store.all('SELECT id FROM dot_mailbox_entries') == [{'id': pending['id']}]
    for table in ('dot_consumer_sessions', 'coordination_goals', 'coordination_work', 'operations'):
        assert not service.store.one('SELECT 1 FROM ' + table)


def test_paused_original_member_keeps_real_legacy_origin_and_cannot_accept_new_input(collab):
    c = legacy(collab)
    connect_action(c, c.member, 'pause')
    member = c.s.rooms.member(c.member['id'])
    assert member['status'] == 'paused' and not member['connection_proof_hash']
    assert c.s.rooms.migration.member_requires_migration(member)
    assert not current(c)['migration']['eligible']
    before = business_snapshot(c)
    with pytest.raises(DevError) as caught:
        c.s.room_runtime.send({'conversation_id': c.conversation_id, 'member_id': member['id'],
            'body_text': '不能向暂停的原连接发送新业务', 'client_message_id': key(), 'idempotency_key': key()}, c.owner)
    assert caught.value.code == 'ROOM_MIGRATION_REQUIRED'
    assert business_snapshot(c) == before
