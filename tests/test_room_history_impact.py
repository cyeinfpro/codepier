"""Bounded history and revocation previews retain exact original identities."""
import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, collaboration_stack as collaboration_stack, key
from tests.test_collaboration_coordination import setup_goal
from tests.test_room_goal_scope_boundaries import two_project_members, admit_read


def test_more_than_one_hundred_original_goals_remain_reachable_when_an_old_goal_updates(collab):
    goals = []
    for _ in range(105):
        coordinator, s, owner, worker, actor, goal, scope, raw = setup_goal(collab, approve=False)
        goals.append(goal)
    conversation_id = goals[0]['conversation_id']
    first = s.room_runtime.work_page(conversation_id, owner, limit=40)
    assert len(first['works']) == 40 and not first['pagination_complete']
    seen = [item['id'] for item in first['works']]
    old = next(goal for goal in goals if goal['id'] not in seen)
    collab[6][0] += 60
    approved = coordinator.approve({**scope, 'goal_id': old['id'], 'expected_version': old['version'],
        'digest': old['digest'], 'idempotency_key': key()}, owner)['goal']
    cursor = first['next_cursor']
    while cursor:
        page = s.room_runtime.work_page(conversation_id, owner, limit=40, cursor=cursor)
        seen.extend(item['id'] for item in page['works'])
        cursor = page['next_cursor']
    assert page['pagination_complete']
    assert len(seen) == len(set(seen)) == 105
    assert set(seen) == {goal['id'] for goal in goals}
    exact = s.room_runtime.work_read(old['id'], owner)
    assert exact['work']['version'] == approved['version']
    restored = s.room_runtime.work_page(conversation_id, owner, limit=20, cursor=exact['history_cursor'])
    assert restored['works'][0]['id'] == old['id']
    assert restored['visibility_revision'] == exact['visibility_revision']
    assert len(restored['works']) <= 20
    other = s.rooms.create({'title': 'Another room', 'idempotency_key': key()}, owner)['room']
    with pytest.raises(DevError) as denied:
        s.room_runtime.work_page(other['id'], owner, cursor=first['next_cursor'])
    assert denied.value.code == 'INVALID_CURSOR'


@pytest.mark.asyncio
async def test_project_removal_preview_is_read_only_and_matches_only_affected_original_receipts(collab):
    s, owner, a, b, lead, lead_credentials, peer, peer_credentials, goal, b_item = await two_project_members(collab)
    a_item = s.store.one('SELECT * FROM coordination_work WHERE goal_id=? AND project_id=?', (goal['goal_id'], 'proj'))
    _, a_op = admit_read(s, lead, lead_credentials, a, goal['goal_id'], a_item, 'proj')
    _, b_op = admit_read(s, peer, peer_credentials, b, goal['goal_id'], b_item, 'otherproj')
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    removed = next(item for item in room['projects'] if item['project_id'] == 'proj')
    changes = s.store.one('SELECT total_changes() AS n')['n']
    request = {'conversation_id': room['id'], 'expected_version': room['version'],
               'removed_partition_ids': [removed['room_id']]}
    preview = s.rooms.project_impact(request, owner)
    assert s.store.one('SELECT total_changes() AS n')['n'] == changes
    assert preview['read_only'] and not preview['grants_authority']
    assert {row['id'] for row in preview['impact']['work_items']} == {a_item['id']}
    assert {row['id'] for row in preview['impact']['operations']} == {a_op['operation_id']}
    assert {row['member_id'] for row in preview['impact']['members']} == {lead['member_id']}
    assert not preview['impact']['running_operations_confirmed_stopped']
    applied = s.rooms.projects({'conversation_id': room['id'], 'expected_version': room['version'],
        'projects': [{'project': 'otherproj'}], 'idempotency_key': key()}, owner)
    assert set(applied['impact']['blocked_work_item_ids']) == {a_item['id']}
    assert not s.store.one('SELECT cancel_requested FROM operations WHERE id=?', (b_op['operation_id'],))['cancel_requested']
    with pytest.raises(DevError) as stale:
        s.rooms.project_impact(request, owner)
    assert stale.value.code == 'STALE_VERSION'


def test_http_exact_source_and_impact_reads_use_current_room_without_side_effects(collaboration_stack):
    stack = collaboration_stack
    conversation = stack.must(stack.client.post('/api/collaboration/conversation', json={
        'title': 'Exact source fixture', 'projects': [{'project': stack.project['id']}],
        'idempotency_key': key()}))['conversation']
    base = f"/api/collaboration/rooms/{conversation['id']}"
    room = stack.must(stack.client.get(base))['room']
    assert room['legacy'] and room['read_only'] and room['migration']['eligible']
    partition = room['projects'][0]['room_id']
    source = stack.must(stack.client.post('/api/collaboration/message', json={
        'project': stack.project['id'], 'environment_id': 'production',
        'room_id': partition, 'conversation_id': room['id'],
        'body_text': '原始上下文证据', 'client_message_id': key(), 'idempotency_key': key()}))['message']
    page = stack.must(stack.client.get(base + '/work'))
    assert page['works'] == [] and page['pagination_complete']
    proposal = stack.mcp('collaboration_goal_create', {
        'project': stack.project['id'], 'environment_id': 'production', 'conversation_id': room['id'],
        'objective': 'Original history cursor fixture', 'acceptance': 'Read the original proposal',
        'project_ids': [stack.project['id']], 'participant_grant_ids': [stack.grant],
        'capabilities': ['read'], 'idempotency_key': key()})
    assert not proposal.get('isError'), proposal
    goal = proposal['structuredContent']['goal']
    activated = stack.must(stack.client.post(base + '/migration', json={
        'action': 'activate', 'expected_version': room['version'], 'idempotency_key': key()}))
    room = activated['room']
    assert activated['activated'] and not room['legacy'] and not room['read_only']
    assert room['migration']['status'] == 'complete'
    exact = stack.must(stack.client.get(base + '/messages', params={'message_id': source['id']}))
    assert exact['exact_lookup'] and [row['id'] for row in exact['messages']] == [source['id']]
    assert exact['message']['source_room_id'] == partition
    stack.must(stack.client.post(base + '/messages', json={
        'body_text': 'Later input cannot displace the old reading anchor', 'client_message_id': key(), 'idempotency_key': key()}))
    anchored = stack.must(stack.client.get(base + '/messages', params={'cursor': exact['history_cursor']}))
    assert [row['id'] for row in anchored['messages']] == [source['id']]
    assert anchored['visibility_revision'] == exact['visibility_revision']
    other = stack.must(stack.client.post('/api/collaboration/rooms', json={
        'title': 'Other context', 'idempotency_key': key()}))['room']
    assert stack.client.get(f"/api/collaboration/rooms/{other['id']}/messages", params={'message_id': source['id']}).status_code == 404
    work_exact = stack.must(stack.client.get('/api/collaboration/work/' + goal['id']))
    work_restored = stack.must(stack.client.get(base + '/work', params={'cursor': work_exact['history_cursor']}))
    assert work_restored['works'][0]['id'] == goal['id']
    assert work_restored['visibility_revision'] == work_exact['visibility_revision']
    assert stack.client.get(f"/api/collaboration/rooms/{other['id']}/work", params={
        'cursor': work_exact['history_cursor']}).status_code == 400
    preview = stack.must(stack.client.get(base + '/projects/impact', params={
        'expected_version': room['version'], 'removed_partition_ids': room['projects'][0]['room_id']}))
    assert preview['read_only'] and preview['impact']['work_items'] == []
    assert stack.must(stack.client.get(base))['room']['version'] == room['version']
    assert stack.client.get(base + '/projects/impact', params={
        'expected_version': room['version'], 'removed_partition_ids': 'outside-partition'}).status_code == 404


def test_archived_owner_history_keeps_original_goals_and_messages_without_reopening_actions(collab):
    coordinator, s, owner, worker, actor, goal, scope, raw = setup_goal(collab)
    cid = goal['conversation_id']
    room = s.rooms.read({'conversation_id': cid}, owner)['room']
    original_goal = s.store.one('SELECT * FROM coordination_goals WHERE id=?', (goal['id'],))
    original_work = s.store.all('SELECT * FROM coordination_work WHERE goal_id=? ORDER BY id', (goal['id'],))
    activated = s.rooms.migrate_room({'conversation_id': cid, 'action': 'activate',
        'expected_version': room['version'], 'idempotency_key': key()}, owner)
    assert activated['activated'] and not activated['room']['legacy'] and not activated['room']['read_only']
    assert s.store.one('SELECT * FROM coordination_goals WHERE id=?', (goal['id'],)) == original_goal
    assert s.store.all('SELECT * FROM coordination_work WHERE goal_id=? ORDER BY id', (goal['id'],)) == original_work
    sent = [s.room_runtime.send({'conversation_id': cid, 'body_text': 'Original history ' + str(index),
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message'] for index in range(3)]
    room = s.rooms.read({'conversation_id': cid}, owner)['room']
    s.rooms.update({'conversation_id': cid, 'expected_version': room['version'],
        'state': 'archived', 'idempotency_key': key()}, owner)
    changes = s.store.one('SELECT total_changes() AS n')['n']
    page = s.room_runtime.read(cid, owner, limit=2)
    older = s.room_runtime.read(cid, owner, limit=2, cursor=page['next_cursor'])
    assert {row['id'] for row in page['messages'] + older['messages']} == {row['id'] for row in sent}
    exact = s.room_runtime.read(cid, owner, message_id=sent[0]['id'])
    assert exact['message']['id'] == sent[0]['id']
    anchored = s.room_runtime.read(cid, owner, cursor=exact['history_cursor'])
    assert [row['id'] for row in anchored['messages']] == [sent[0]['id']]
    work = s.room_runtime.work_read(goal['id'], owner)['work']
    assert work['room_archived'] and work['allowed_actions'] == [] and 'recovery' not in work
    assert [row['id'] for row in s.room_runtime.work_page(cid, owner)['works']] == [goal['id']]
    work_anchor = s.room_runtime.work_read(goal['id'], owner)['history_cursor']
    assert s.room_runtime.work_page(cid, owner, cursor=work_anchor)['works'][0]['id'] == goal['id']
    assert s.store.one('SELECT total_changes() AS n')['n'] == changes
    with pytest.raises(DevError):
        s.room_runtime.work_read(goal['id'], worker)
    with pytest.raises(DevError):
        s.room_runtime.control({'work_id': goal['id'], 'expected_version': work['version'],
            'action': 'cancel', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError):
        s.room_runtime.send({'conversation_id': cid, 'body_text': 'Cannot continue after archive',
            'client_message_id': key(), 'idempotency_key': key()}, owner)
    partition = s.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
    original = s.room_runtime.goal(goal['id'])
    for principal, active in ((owner, True), (worker, False)):
        with pytest.raises(DevError) as denied:
            coordinator.authorize(principal, partition, original, active=active, include_archived=True)
        assert denied.value.code == 'GOAL_ARCHIVE_READ_ONLY'


@pytest.mark.asyncio
async def test_archived_native_goal_keeps_original_operation_trace_for_owner(collab):
    s, owner, a, b, lead, lead_credentials, peer, peer_credentials, goal, b_item = await two_project_members(collab)
    _, operation = admit_read(s, peer, peer_credentials, b, goal['goal_id'], b_item, 'otherproj')
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    s.rooms.update({'conversation_id': room['id'], 'expected_version': room['version'],
        'state': 'archived', 'idempotency_key': key()}, owner)
    changes = s.store.one('SELECT total_changes() AS n')['n']
    work = s.room_runtime.work_read(goal['goal_id'], owner)['work']
    assert work['room_archived'] and work['allowed_actions'] == []
    assert operation['operation_id'] in {row['id'] for row in work['operations']}
    diagnostic = s.rooms.diagnostics({'conversation_id': room['id']}, owner)
    assert any(row.get('operation_id') == operation['operation_id'] for row in diagnostic['traces'])
    assert s.store.one('SELECT total_changes() AS n')['n'] == changes
    with pytest.raises(DevError):
        s.room_consumers.inbox(peer, b)
    with pytest.raises(DevError):
        s.room_runtime.work_view(s.room_runtime.goal(goal['goal_id']), b, member_id=peer['member_id'])
