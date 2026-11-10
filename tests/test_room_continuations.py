"""Room permissions and explicitly selected old work share one save receipt."""
import json

import pytest

from hub.collaboration.common import canonical
from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401
from tests.test_room_goal_scope_boundaries import admit_read, mutation, report, start
from tests.test_room_managed_work import connected, permission_rules, save_room_permissions
from tests.test_room_weixin_native import setup_channel
from tests.test_room_weixin_permissions import receive, start_work


def draft(service, owner, room, *, scopes=None):
    policy = service.room_permissions.read(room, owner)
    return {'conversation_id': room, 'expected_version': policy['version'],
            'scope': {'project_scopes': permission_rules(scopes or [{'project_id': 'proj'}])},
            'channels': {name: {'mode': value['mode'], 'scope': value['scope']}
                         for name, value in policy['channels'].items()}, 'idempotency_key': key()}


def selected(body, preview, *ids):
    return {**body, 'continue_selected': True, 'selected_task_ids': list(ids),
            **{k: preview[k] for k in ('policy_preview_digest', 'continuation_preview_id')}}


@pytest.mark.asyncio
async def test_reapprove_selected_expired_goal_and_reuse_original_goal_and_work(collab):
    clock = collab[6]
    s, owner, actor, _, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj', 'lifetime': {'mode': 'timed', 'expires_at': clock[0] + 180}}])
    room = identity['conversation_id']
    first, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    second, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    original = s.room_runtime.goal(first['goal_id'])
    unrelated = s.room_runtime.goal(second['goal_id'])
    old_item = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (original['id'],))
    clock[0] = original['expires_at'] + 1
    continuations = s.room_goal_recovery.continuations
    body = draft(s, owner, room)
    preview = continuations.preview(body, owner)
    assert {item['task_id'] for item in preview['candidates']} == {original['id'], unrelated['id']}
    result = continuations.save(selected(body, preview, original['id']), owner)
    assert result['continuation']['counts'] == {'continued': 1, 'skipped': 0, 'pending': 0, 'waiting_operations': 0}
    resumed = s.room_runtime.goal(original['id'])
    assert resumed['id'] == original['id'] and resumed['approval_id'] != original['approval_id']
    assert resumed['digest'] == original['digest'] and resumed['expires_at'] > clock[0]
    assert s.room_runtime.intent_revision(original['id']) == 2
    assert s.room_goal_recovery.epoch(original['id']) == 2
    assert s.room_runtime.goal(unrelated['id'])['approval_id'] == unrelated['approval_id']
    assert s.store.one('SELECT * FROM coordination_work WHERE id=?', (old_item['id'],))['state'] == 'queued'
    assert continuations.save(selected(body, preview, original['id']), owner)['continuation'] == result['continuation']
    assert len(s.store.all('SELECT * FROM room_continuation_proofs')) == 1
    assert not s.store.all('SELECT * FROM operations')
    with pytest.raises(DevError) as error:
        continuations.save(selected(body, preview, unrelated['id']), owner)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_continuation_batch_skips_source_changed_after_preview(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj'}])
    room = identity['conversation_id']
    source_ids = []
    for prompt in ('第一条需要继续的原始输入', '第二条需要继续的原始输入'):
        message = s.room_runtime.send({'conversation_id': room, 'body_text': prompt,
            'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
        source_ids.append(message['id'])
        row = s.store.one('SELECT body FROM collaboration_messages WHERE id=?', (message['id'],))
        payload = json.loads(row['body'])
        payload['room_routing']['state'] = 'waiting'
        s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(payload), message['id']))
    body = draft(s, owner, room)
    continuations = s.room_goal_recovery.continuations
    preview = continuations.preview(body, owner)
    assert {item['task_id'] for item in preview['candidates']} == set(source_ids)
    s.store.execute('UPDATE collaboration_messages SET version=version+1 WHERE id=?', (source_ids[0],))
    result = continuations.save(selected(body, preview, *source_ids), owner)['continuation']
    assert result['counts'] == {'continued': 1, 'skipped': 1, 'pending': 0, 'waiting_operations': 0}
    assert result['results'][0]['reason_code'] == 'ROOM_SOURCE_CHANGED'
    rows = s.room_consumers.inbox(identity, actor)['items']
    assert any(item['kind'] == 'message' and item['object_id'] == source_ids[1]
               and item['work_start_arguments']['source_message_id'] == source_ids[1] for item in rows)
    assert continuations.status(room, result['batch_id'], owner) == result


@pytest.mark.asyncio
async def test_save_without_selection_and_expired_preview_do_not_resume(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj'}])
    room = identity['conversation_id']
    created, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal_id = created['goal_id']
    save_room_permissions(s, owner, room, [])
    body = draft(s, owner, room)
    continuations = s.room_goal_recovery.continuations
    preview = continuations.preview(body, owner)
    clock[0] = preview['expires_at'] + 1
    with pytest.raises(DevError) as error:
        continuations.save(selected(body, preview, goal_id), owner)
    assert error.value.code == 'ROOM_CONTINUATION_PREVIEW_EXPIRED'
    saved = continuations.save(body, owner)
    assert saved['version'] == body['expected_version'] + 1
    assert s.room_goal_recovery.epoch(goal_id) == 1
    assert not s.store.all('SELECT * FROM room_continuation_batches')


@pytest.mark.asyncio
async def test_waiting_operation_keeps_original_receipt_when_continuation_recovers(collab):
    clock = collab[6]
    s, owner, actor, _, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj', 'lifetime': {'mode': 'timed', 'expires_at': clock[0] + 180}}])
    room = identity['conversation_id']
    started, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal = s.room_runtime.goal(started['goal_id'])
    item = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal['id'],))
    _, operation = admit_read(s, identity, credentials, actor, goal['id'], item, 'proj')
    operation_id = operation['operation_id']
    s.store.execute("UPDATE operations SET state='unknown',attempts=1 WHERE id=?", (operation_id,))
    clock[0] = goal['expires_at'] + 1
    body = draft(s, owner, room)
    continuations = s.room_goal_recovery.continuations
    preview = continuations.preview(body, owner)
    batch = continuations.save(selected(body, preview, goal['id']), owner)['continuation']
    assert batch['results'][0]['state'] == 'waiting_operations'
    assert s.room_goal_recovery.epoch(goal['id']) == 1
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,)),
        {'ok': True, 'data': {'content': 'completed by original worker'}})
    clock[0] += 31
    assert continuations.recover() == 1
    current = continuations.status(room, batch['batch_id'], owner)
    assert current['results'][0]['state'] == 'continued'
    assert s.room_goal_recovery.epoch(goal['id']) == 2
    assert s.store.all('SELECT id,state FROM operations') == [{'id': operation_id, 'state': 'succeeded'}]


@pytest.mark.asyncio
async def test_continuation_carries_completed_receipt_and_queues_unstarted_dependency(collab):
    clock = collab[6]
    s, owner, actor, _, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj', 'lifetime': {'mode': 'timed', 'expires_at': clock[0] + 180}}])
    room = identity['conversation_id']
    started, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal_id = started['goal_id']
    original = s.room_runtime.goal(goal_id)
    first = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal_id,))
    lease, operation = admit_read(s, identity, credentials, actor, goal_id, first, 'proj')
    finished = report(s, identity, credentials, actor, goal_id, lease, operation)
    second = s.coordination.work_create({**mutation(s, identity, credentials, goal_id),
        'target_project_id': 'proj', 'objective': '继续核对下一份资料', 'acceptance': '沿用前一步已有回执',
        'required_capabilities': ['read'], 'assignee_grant_id': actor.grant_id,
        'assignee_member_id': identity['member_id'], 'dependencies': [first['id']]}, actor)['work_item']
    clock[0] = original['expires_at'] + 1
    body = draft(s, owner, room)
    continuations = s.room_goal_recovery.continuations
    preview = continuations.preview(body, owner)
    batch = continuations.save(selected(body, preview, goal_id), owner)['continuation']
    assert batch['results'][0]['state'] == 'continued'
    current = s.room_runtime.goal(goal_id)
    refs = s.room_goal_recovery.reused_artifact_refs(current)
    assert len(refs) == 1 and refs[0]['work_item_id'] == first['id'] and refs[0]['version'] == finished['version']
    assert s.coordination.teams.artifacts(current, refs) == refs
    queued = s.coordination.work(current, second['id'])
    assert queued['state'] == 'queued' and json.loads(queued['dependencies']) == [first['id']]
    assert s.coordination.work(current, first['id'])['approval_id'] == original['approval_id']
    assert len(s.coordination.operations(current)) == 1


@pytest.mark.asyncio
async def test_weixin_continuation_requires_current_original_channel_consent(collab):
    (s, owner, actor, clock, identity, credentials), flow, _, action = await setup_channel(collab, tasks=True)
    room = identity['conversation_id']
    original = receive(s, flow, clock, 2)
    started = start_work(s, actor, identity, credentials, original, project_ids=['proj'])
    pending = receive(s, flow, clock, 3)
    revoked = await action(flow, 'revoke_tasks')
    continuations = s.room_goal_recovery.continuations
    body = draft(s, owner, room)
    locked = continuations.preview(body, owner)
    expected = {started['goal_id'], pending['id']}
    assert {item['task_id'] for item in locked['candidates']} == expected
    assert all(not item['eligible'] and item['reason_code'] == 'CHANNEL_OWNER_NOT_APPROVED'
               for item in locked['candidates'])
    current = await action(revoked, 'approve_tasks', expected_scope_digest=revoked['task_scope']['scope_digest'],
        confirm_owner_identity=True, confirm_future_tasks=True, confirm_result_delivery=True,
        confirm_until_revoked=True, confirm_pairing_notices=True)
    assert current['work_authorized']
    preview = continuations.preview(body, owner)
    assert {item['task_id'] for item in preview['candidates'] if item['eligible']} == expected
    batch = continuations.save(selected(body, preview, started['goal_id'], pending['id']), owner)['continuation']
    assert batch['counts']['continued'] == 2
    goal = s.room_runtime.goal(started['goal_id'])
    work_proof = continuations.work_proof(goal, original)
    source_proof = continuations.source_proof(pending, s.rooms.member(identity['member_id']))
    assert work_proof['channel']['binding_id'] == source_proof['channel']['binding_id'] == flow['binding']['id']
    assert work_proof['channel']['authorization_version'] == source_proof['channel']['authorization_version']
    assert len(s.store.all('SELECT * FROM coordination_goals')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mixed', [False, True])
async def test_continuation_ack_requires_all_remaining_work_to_be_reusable(collab, mixed):
    clock = collab[6]
    s, owner, actor, _, identity, credentials = await connected(collab, [{'project': 'proj'}],
        [{'project_id': 'proj', 'lifetime': {'mode': 'timed', 'expires_at': clock[0] + 180}}])
    started, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal = s.room_runtime.goal(started['goal_id'])
    first = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal['id'],))
    if mixed:
        _, operation = admit_read(s, identity, credentials, actor, goal['id'], first, 'proj')
        s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],)),
            {'ok': True, 'data': {'content': 'original receipt awaiting work result'}})
    second = s.coordination.work_create({**mutation(s, identity, credentials, goal['id']),
        'target_project_id': 'proj', 'objective': '独立的未执行步骤', 'acceptance': '核对结果',
        'required_capabilities': ['read'], 'assignee_grant_id': actor.grant_id,
        'assignee_member_id': identity['member_id'], 'dependencies': []}, actor)['work_item']
    clock[0] = goal['expires_at'] + 1
    continuations = s.room_goal_recovery.continuations
    body = draft(s, owner, identity['conversation_id'])
    preview = continuations.preview(body, owner)
    batch = continuations.save(selected(body, preview, goal['id']), owner)['continuation']
    assert batch['counts']['continued'] == 1
    current = s.room_runtime.goal(goal['id'])
    resumed = s.coordination.work(current, second['id'])
    assert resumed['approval_id'] == current['approval_id'] and resumed['state'] == 'queued'
    first_now = s.coordination.work(current, first['id'])
    assert first_now['approval_id'] == (goal['approval_id'] if mixed else current['approval_id'])
    source = s.store.one('SELECT source_message_id FROM room_work_intents WHERE goal_id=?', (goal['id'],))
    entries = s.store.all("SELECT acknowledged_at FROM dot_mailbox_entries WHERE kind='message' AND object_id=?",
                         (source['source_message_id'],))
    assert entries
    assert all((entry['acknowledged_at'] is None) == mixed for entry in entries)
