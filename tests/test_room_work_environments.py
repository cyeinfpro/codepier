"""Original per-work environments survive cross-project receipts and removal."""
import json

import pytest

from hub.collaboration.common import digest
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import connected, save_room_permissions
from tests.test_room_goal_scope_boundaries import worker_for, mutation, start, report, final_request, renew_consumer


async def cross_environment_work(collab):
    s, owner, worker, *_ = collab
    a = worker_for(s, worker, 'environment-a', scopes=('read',), projects=('proj',))
    b = worker_for(s, worker, 'environment-b', scopes=('read',), projects=('otherproj',))
    projects = [{'project': 'proj', 'environment_id': 'production'},
                {'project': 'otherproj', 'environment_id': 'staging'}]
    s, owner, a, clock, lead, acredentials = await connected(collab, projects,
        [{'project_id': p['project'], 'environment_id': p['environment_id']} for p in projects], worker_override=a)
    _, _, b, _, peer, bcredentials = await connected(collab,
        worker_override=b, conversation_id=lead['conversation_id'])
    goal, _ = start(s, owner, a, lead, acredentials, participant_member_ids=[peer['member_id']])
    b_item = s.coordination.work_create({**mutation(s, lead, acredentials, goal['goal_id']),
        'target_project_id': 'otherproj', 'objective': '核对 B staging 文档', 'acceptance': '原操作读取回执',
        'assignee_grant_id': b.grant_id, 'assignee_member_id': peer['member_id']}, a)['work_item']
    a_item = s.store.one('SELECT * FROM coordination_work WHERE id=?', (goal['next_request']['arguments']['work_item_id'],))
    # A later room expansion must not rewrite the original work environments.
    save_room_permissions(s, owner, lead['conversation_id'],
        [{'project_id': p['project'], 'environment_id': p['environment_id']} for p in projects]
        + [{'project_id': 'otherproj', 'environment_id': 'production'}])
    return s, owner, a, b, lead, acredentials, peer, bcredentials, goal, a_item, b_item


def admit(s, identity, credentials, actor, goal_id, item, project, environment):
    raw = {**mutation(s, identity, credentials, goal_id, project=project), 'environment_id': environment}
    claimed = s.coordination.work_claim({**raw, 'work_item_id': item['id'], 'expected_version': item['version']}, actor)['work_item']
    lease = {**raw, 'work_item_id': item['id'], 'attempt': claimed['attempt'],
             'fencing_token': claimed['fencing_token'], 'idempotency_key': key(),
             'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision']}
    operation = s.coordination.admit({**lease, 'tool': 'read', 'arguments': {'path': 'README.md'}}, actor)
    return lease, operation


@pytest.mark.asyncio
async def test_staging_progress_result_and_publication_use_original_work_partition(collab):
    s, owner, a, b, lead, ac, peer, bc, goal, a_item, b_item = await cross_environment_work(collab)
    alease, aop = admit(s, lead, ac, a, goal['goal_id'], a_item, 'proj', 'production')
    blease, bop = admit(s, peer, bc, b, goal['goal_id'], b_item, 'otherproj', 'staging')
    s.coordination.progress({**blease, 'idempotency_key': key(), 'summary': '正在读取 B staging 的原文档',
        'expected_inbox_revision': s.room_consumers.inbox(peer, b)['inbox_revision']}, b)
    b_done = report(s, peer, bc, b, goal['goal_id'], blease, bop)
    a_done = report(s, lead, ac, a, goal['goal_id'], alease, aop)
    rows = s.store.all('''SELECT m.*,r.project_id,r.environment_id FROM collaboration_messages m
        JOIN collaboration_rooms r ON r.id=m.room_id WHERE m.goal_id=?
        AND json_extract(m.body,'$.work_item_id')=? ORDER BY m.conversation_sequence''', (goal['goal_id'], b_item['id']))
    assert {row['kind'] for row in rows} >= {'delegation_progress', 'delegation_result'}
    assert all((row['project_id'], row['environment_id']) == ('otherproj', 'staging') for row in rows)
    assert all(json.loads(row['body'])['sender_member_id'] == peer['member_id'] for row in rows)
    source = s.store.one("SELECT * FROM collaboration_messages WHERE goal_id=? AND origin='panel_owner'", (goal['goal_id'],))
    refs = [{'work_item_id': item['id'], 'version': item['version'], 'digest': digest(item['result'])}
            for item in (a_done, b_done)]
    request = final_request(s, lead, ac, a, source, refs)
    with pytest.raises(DevError) as denied:
        s.room_runtime.reply(request, a)
    assert denied.value.code == 'ROOM_WORK_ARTIFACT_NOT_VISIBLE'
    assert s.store.one('SELECT COUNT(*) AS n FROM coordination_goals')['n'] == 1
    assert s.store.one('SELECT COUNT(*) AS n FROM operations')['n'] == 2
    assert s.store.one('SELECT COUNT(*) AS n FROM team_publications')['n'] == 0
    assert {row['id'] for row in s.store.all('SELECT id FROM operations')} == {aop['operation_id'], bop['operation_id']}


@pytest.mark.asyncio
@pytest.mark.parametrize('removed', [('proj', 'production'), ('otherproj', 'staging'), ('otherproj', 'production')])
async def test_removal_preview_and_commit_match_exact_original_work_environment(collab, removed):
    s, owner, a, b, lead, ac, peer, bc, goal, a_item, b_item = await cross_environment_work(collab)
    alease, aop = admit(s, lead, ac, a, goal['goal_id'], a_item, 'proj', 'production')
    blease, bop = admit(s, peer, bc, b, goal['goal_id'], b_item, 'otherproj', 'staging')
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    partition = next(p for p in room['projects'] if (p['project_id'], p['environment_id']) == removed)
    selected = {'proj': (a_item, aop), 'otherproj': (b_item, bop)}.get(removed[0]) if removed != ('otherproj', 'production') else None
    expected_work = {selected[0]['id']} if selected else set()
    expected_operation = {selected[1]['operation_id']} if selected else set()
    before = s.store.one('SELECT total_changes() AS n')['n']
    preview = s.rooms.project_impact({'conversation_id': room['id'], 'expected_version': room['version'],
                                    'removed_partition_ids': [partition['room_id']]}, owner)
    assert s.store.one('SELECT total_changes() AS n')['n'] == before
    assert {row['id'] for row in preview['impact']['work_items']} == expected_work
    assert {row['id'] for row in preview['impact']['operations']} == expected_operation
    kept = [{'project': p['project_id'], 'environment_id': p['environment_id']} for p in room['projects']
            if p['room_id'] != partition['room_id']]
    save_room_permissions(s, owner, room['id'],
        [{'project_id': p['project'], 'environment_id': p['environment_id']} for p in kept])
    blocked_work = {row['id'] for row in s.store.all(
        "SELECT id FROM coordination_work WHERE goal_id=? AND state='blocked'", (goal['goal_id'],))}
    stopped_operations = {row['id'] for row in s.store.all(
        "SELECT id FROM operations WHERE cancel_requested=1 OR state='cancelled'")}
    assert blocked_work == expected_work
    assert stopped_operations == expected_operation
    collab[6][0] += 91  # Renew fenced room consumers without expiring original work leases.
    assert s.room_runtime.goal(goal['goal_id'])['state'] == 'active'
    remaining = [(lead, ac, a, alease, aop, a_item), (peer, bc, b, blease, bop, b_item)]
    for identity, credentials, actor, lease, operation, item in remaining:
        current = s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],))
        if item['id'] in expected_work:
            assert current['cancel_requested'] or current['state'] == 'cancelled'
            with pytest.raises(DevError):
                s.coordination.admit({**lease, 'tool': 'read', 'arguments': {'path': 'README.md'},
                    'idempotency_key': key(), 'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision']}, actor)
        else:
            assert not current['cancel_requested']
            credentials = await renew_consumer(s, identity, actor, credentials)
            lease = {**lease, **credentials}
            done = report(s, identity, credentials, actor, goal['goal_id'], lease, operation)
            assert done['state'] == 'succeeded'
    assert s.store.one('SELECT COUNT(*) AS n FROM coordination_goals')['n'] == 1
    assert s.store.one('SELECT COUNT(*) AS n FROM operations')['n'] == 2


@pytest.mark.asyncio
async def test_same_project_room_environments_require_explicit_work_selection(collab):
    s, owner, actor, *_ = collab
    s, owner, actor, clock, lead, ac = await connected(collab,
        [{'project': 'proj', 'environment_id': 'production'}, {'project': 'proj', 'environment_id': 'staging'}],
        [{'project_id': 'proj', 'environment_id': env} for env in ('production', 'staging')])
    _, _, peer_actor, _, peer, _ = await connected(collab, conversation_id=lead['conversation_id'])
    with pytest.raises(DevError) as refused:
        start(s, owner, actor, lead, ac, project_ids=['proj'], participant_member_ids=[peer['member_id']])
    assert refused.value.code == 'ROOM_WORK_ENVIRONMENT_REQUIRED'
    assert not s.store.one('SELECT 1 FROM coordination_goals')
    assert not s.store.one('SELECT 1 FROM room_native_work_scopes')
    assert not s.store.one('SELECT 1 FROM operations')
    result, _ = start(s, owner, actor, lead, ac, project_ids=['proj'], environment_id='production')
    scope = s.room_work_bridge.native.original(s.room_work_bridge.native.reference(s.room_runtime.goal(result['goal_id'])))
    assert {rule['environment_id'] for member in scope['members'] for rule in member['projects']} == {'production'}
    assert scope['participant_grant_ids'] == [actor.grant_id]
