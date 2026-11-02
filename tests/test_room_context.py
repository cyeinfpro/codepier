"""Sourced room memory uses real joins, consumer fences and original ledgers."""
import json

import pytest

from hub.collaboration.common import canonical, digest
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import connected
from tests.test_room_goal_scope_boundaries import worker_for, start, admit_read, report


def send(s, owner, identity, text='先核对需求，保持现有权限边界。', **extra):
    return s.room_runtime.send({'conversation_id': identity['conversation_id'],
        'body_text': text, 'client_message_id': key(), 'idempotency_key': key(), **extra}, owner)['message']


def reference(s, message):
    row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message['id'],))
    return s.room_context.reference(row)


def fact(s, message, *, key='objective', kind='objective', text='核对原需求', **extra):
    return {'kind': kind, 'key': key, 'text': text, 'sources': [reference(s, message)], **extra}


def request(s, actor, identity, credentials, **extra):
    return {**identity, **credentials,
        'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision'],
        'expected_context_version': s.room_context.version(identity['conversation_id']),
        'idempotency_key': key(), **extra}


def effect_counts(s):
    return {name: s.store.one('SELECT COUNT(*) n FROM ' + name)['n'] for name in (
        'coordination_goals', 'coordination_work', 'operations', 'delegation_requests', 'dot_mailbox_entries')}


@pytest.mark.asyncio
async def test_context_empty_room_update_is_fenced_idempotent_and_never_work_or_ack(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab)
    message = send(s, owner, identity)
    before = effect_counts(s)
    raw = request(s, actor, identity, credentials, facts=[fact(s, message),
        fact(s, message, key='no_execution', kind='constraint', text='上下文不批准新工作')])
    result = s.invoke('collaboration', {'action': 'room_context_update', **raw}, actor)
    assert result['version'] == 1 and not result['scheduled'] and not result['grants_authority']
    assert effect_counts(s) == before
    assert s.invoke('collaboration', {'action': 'room_context_update', **raw}, actor) == result
    assert any(item['object_id'] == message['id'] for item in s.room_consumers.inbox(identity, actor)['items'])
    with pytest.raises(DevError) as conflict:
        s.room_context.update({**raw, 'facts': [fact(s, message, text='另一份内容')]}, actor)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(DevError) as stale:
        s.room_context.update({**raw, 'idempotency_key': key()}, actor)
    assert stale.value.code == 'CONTEXT_VERSION_CHANGED'
    descriptor = s.room_consumers.inbox(identity, actor)['context_reference']
    assert descriptor['action'] == 'room_context' and descriptor['credential_required']
    assert identity['connection_proof'] not in canonical(descriptor)
    changes = s.store.one('SELECT total_changes() n')['n']
    view = s.invoke('collaboration_query', {'action': descriptor['action'], **identity}, actor)
    assert s.store.one('SELECT total_changes() n')['n'] == changes
    assert view['review_state'] == 'current' and view['current_facts_complete']
    assert view['incomplete'] and not view['history_complete'] and view['read_is_consumption'] is False
    assert {row['id'] for row in view['facts']} == set(result['fact_ids'])
    assert s.rooms.read({'conversation_id': identity['conversation_id']}, owner)['room']['context']['version'] == 1
    assert s.room_runtime.read(identity['conversation_id'], owner)['context_version'] == 1
    send(s, owner, identity, '补充了新的要求，等待主责核对。')
    pending = s.room_context.read(identity, actor)
    assert pending['review_state'] == 'needs_review' and not pending['current_facts_complete']
    assert 'unreviewed_input' in pending['incomplete_reasons']
    assert all(f['review_state'] == 'needs_review' for f in pending['facts'])
    clock[0] += 301
    with pytest.raises(DevError):
        s.room_context.update(request(s, actor, identity, credentials,
            reviewed_sources=[reference(s, message)]), actor)


@pytest.mark.asyncio
async def test_context_current_lead_source_exactness_and_owner_decisions(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab)
    _, _, _, _, peer, peer_credentials = await connected(collab, conversation_id=identity['conversation_id'])
    message = send(s, owner, identity)
    with pytest.raises(DevError) as nonlead:
        s.room_context.update(request(s, actor, peer, peer_credentials, facts=[fact(s, message)]), actor)
    assert nonlead.value.code == 'CONTEXT_LEAD_REQUIRED'
    for bad in ({**reference(s, message), 'version': 2},
                {**reference(s, message), 'digest': 'a' * 64}):
        with pytest.raises(DevError) as invalid:
            s.room_context.update(request(s, actor, identity, credentials,
                facts=[{**fact(s, message), 'sources': [bad]}]), actor)
        assert invalid.value.code == 'CONTEXT_SOURCE_CHANGED'
    other = s.rooms.create({'title': 'Other room', 'idempotency_key': key()}, owner)['room']
    foreign = send(s, owner, {'conversation_id': other['id']}, '另一房间的私有记录。')
    with pytest.raises(DevError) as crossroom:
        s.room_context.update(request(s, actor, identity, credentials, facts=[fact(s, foreign)]), actor)
    assert crossroom.value.code == 'CONTEXT_SOURCE_NOT_FOUND'
    reply = s.room_runtime.reply({**identity, **credentials,
        'source_message_id': message['id'], 'source_version': message['version'],
        'source_digest': reference(s, message)['digest'],
        'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision'],
        'body_text': '助手建议扩大任务。', 'complete': False, 'idempotency_key': key()}, actor)['message']
    with pytest.raises(DevError) as proposal:
        s.room_context.update(request(s, actor, identity, credentials,
            facts=[fact(s, reply, kind='decision', text='本人已决定扩大范围')]), actor)
    assert proposal.value.code == 'CONTEXT_OWNER_SOURCE_REQUIRED'
    assert not s.room_context.read(identity, actor)['facts']
    assert not s.store.all('SELECT * FROM coordination_goals')


@pytest.mark.asyncio
async def test_context_explicit_replacement_and_owner_correction_are_atomic(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab)
    original = send(s, owner, identity, '使用原方案。')
    saved = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, original, text='使用原方案')]), actor)
    correction = send(s, owner, identity, '纠正：现在使用新方案。')
    replacement = fact(s, correction, text='使用新方案')
    with pytest.raises(DevError) as explicit:
        s.room_context.update(request(s, actor, identity, credentials, facts=[replacement]), actor)
    assert explicit.value.code == 'CONTEXT_REPLACEMENT_REQUIRED'
    changed = s.room_context.update(request(s, actor, identity, credentials,
        facts=[replacement], supersedes=saved['fact_ids']), actor)
    view = s.room_context.read(identity, actor)
    assert [f['text'] for f in view['facts']] == ['使用新方案']
    history = s.store.one('SELECT * FROM room_context_supersessions WHERE fact_id=?', (saved['fact_ids'][0],))
    assert history['message_id'] == correction['id'] and history['replacement_fact_id'] == changed['fact_ids'][0]
    before = effect_counts(s)
    messages = s.store.one('SELECT COUNT(*) n FROM collaboration_messages')['n']
    with pytest.raises(DevError) as stale:
        send(s, owner, identity, '撤回旧事实。', supersedes_context_ids=changed['fact_ids'], expected_context_version=1)
    assert stale.value.code == 'CONTEXT_VERSION_CHANGED'
    assert s.store.one('SELECT COUNT(*) n FROM collaboration_messages')['n'] == messages
    assert effect_counts(s) == before
    cleared = send(s, owner, identity, '撤回：尚未作最后决定。',
        supersedes_context_ids=changed['fact_ids'], expected_context_version=changed['version'])
    context = s.room_context.read(identity, actor)
    assert context['version'] == 3 and not context['facts'] and context['review_state'] == 'needs_review'
    assert s.store.one('SELECT message_id FROM room_context_supersessions WHERE fact_id=?',
        (changed['fact_ids'][0],))['message_id'] == cleared['id']
    with pytest.raises(DevError) as resurrected:
        s.room_context.update(request(s, actor, identity, credentials,
            facts=[fact(s, original, text='拿到新 fence 后偷偷恢复原方案')]), actor)
    assert resurrected.value.code == 'CONTEXT_CORRECTION_SOURCE_REQUIRED'
    assert not s.room_context.read(identity, actor)['facts']
    restored = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, cleared, text='尚未作最后决定')]), actor)
    assert restored['version'] == 4
    assert [f['text'] for f in s.room_context.read(identity, actor)['facts']] == ['尚未作最后决定']


@pytest.mark.asyncio
@pytest.mark.parametrize('renamed', [False, True])
async def test_context_same_update_cannot_review_correction_and_reinsert_older_source(collab, renamed):
    s, owner, actor, _, identity, credentials = await connected(collab)
    source = send(s, owner, identity, '原决定。')
    original = s.room_context.update(request(s, actor, identity, credentials, facts=[fact(s, source)]), actor)
    correction = send(s, owner, identity, '新决定替代原决定。')
    raw = request(s, actor, identity, credentials, supersedes=original['fact_ids'],
        reviewed_sources=[reference(s, correction)],
        facts=[fact(s, source, key='renamed' if renamed else 'objective', text='重放旧结论')])
    with pytest.raises(DevError) as replay:
        s.room_context.update(raw, actor)
    assert replay.value.code == 'CONTEXT_CORRECTION_SOURCE_REQUIRED'
    assert s.room_context.version(identity['conversation_id']) == 1
    assert not s.store.all('SELECT * FROM room_context_supersessions')
    assert s.store.one('SELECT fact_id FROM room_context_current')['fact_id'] == original['fact_ids'][0]
    fixed = {**raw, 'facts': [fact(s, correction, key='renamed' if renamed else 'objective', text='新决定')]}
    assert s.room_context.update(fixed, actor)['version'] == 2


@pytest.mark.asyncio
async def test_renamed_fact_cannot_restore_or_withdraw_using_pre_correction_source(collab):
    s, owner, actor, _, identity, credentials = await connected(collab)
    original = send(s, owner, identity, '原方案。')
    first = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, original, key='plan', text='原方案')]), actor)
    correction = send(s, owner, identity, '纠正：改用新方案。',
        supersedes_context_ids=first['fact_ids'], expected_context_version=first['version'])
    current = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, correction, key='plan', text='新方案')]), actor)
    for new_key in ('renamed_plan', 'renamed_again'):
        current = s.room_context.update(request(s, actor, identity, credentials,
            supersedes=current['fact_ids'], facts=[fact(s, correction, key=new_key, text='新方案')]), actor)
    evidence = s.store.one('SELECT * FROM room_context_facts WHERE id=?', (current['fact_ids'][0],))
    source_sequence = s.store.one('SELECT conversation_sequence n FROM collaboration_messages WHERE id=?', (correction['id'],))['n']
    assert evidence['source_floor_sequence'] >= source_sequence and evidence['source_floor_owner']
    for extra in ({'facts': [fact(s, original, key='renamed_again', text='偷偷恢复原方案')]},
                  {'reviewed_sources': [reference(s, original)]}):
        with pytest.raises(DevError) as stale:
            s.room_context.update(request(s, actor, identity, credentials,
                supersedes=current['fact_ids'], **extra), actor)
        assert stale.value.code == 'CONTEXT_CORRECTION_SOURCE_REQUIRED'
        assert s.room_context.version(identity['conversation_id']) == current['version']
        assert [f['text'] for f in s.room_context.read(identity, actor)['facts']] == ['新方案']
    latest = send(s, owner, identity, '本人进一步修订方案。')
    s.room_context.update(request(s, actor, identity, credentials, supersedes=current['fact_ids'],
        facts=[fact(s, latest, key='renamed_again', text='进一步修订方案')]), actor)
    assert [f['text'] for f in s.room_context.read(identity, actor)['facts']] == ['进一步修订方案']


@pytest.mark.asyncio
async def test_context_scope_intersection_correction_provenance_and_revocation(collab):
    s, owner, worker, *_ = collab
    lead_actor = worker_for(s, worker, 'context-lead', scopes=('read',), projects=('proj', 'otherproj'))
    s, owner, lead_actor, _, lead, credentials = await connected(collab,
        [{'project': 'proj'}, {'project': 'otherproj'}],
        [{'project_id': 'proj'}, {'project_id': 'otherproj'}], worker_override=lead_actor)
    only_a = worker_for(s, worker, 'context-a', scopes=('read',), projects=('proj',))
    _, _, _, _, peer, _ = await connected(collab,
        scopes=[{'project_id': 'proj'}, {'project_id': 'otherproj'}], worker_override=only_a,
        conversation_id=lead['conversation_id'])
    source = send(s, owner, lead, '由本人记录两个项目的独立范围。')
    saved = s.room_context.update(request(s, lead_actor, lead, credentials, facts=[
        fact(s, source, key='a', kind='decision', text='A 决定', project_ids=['proj']),
        fact(s, source, key='b', kind='decision', text='B 私有决定', project_ids=['otherproj'])]), lead_actor)
    peer_view = s.room_context.read(peer, only_a)
    assert [f['key'] for f in peer_view['facts']] == ['a']
    assert {p['project_id'] for p in peer_view['active_projects']} == {'proj'}
    b_id = saved['fact_ids'][1]
    corrected = send(s, owner, lead, 'B 的决定需要重新核对。',
        supersedes_context_ids=[b_id], expected_context_version=1)
    row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (corrected['id'],))
    assert json.loads(row['body'])['provenance_project_ids'] == ['otherproj']
    assert not s.room_consumers._visible_message(s.rooms.member(peer['member_id']), only_a, row)
    assert corrected['id'] not in {r['message_id'] for r in s.room_context.read(peer, only_a)['recent_messages']}
    added = s.room_context.update(request(s, lead_actor, lead, credentials,
        facts=[fact(s, corrected, key='b', kind='decision', text='B 待重新核对')]), lead_actor)
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    old_page = s.room_context.read({**lead, 'limit': 1}, lead_actor)
    s.rooms.projects({'conversation_id': room['id'], 'expected_version': room['version'],
        'projects': [{'project': 'proj'}], 'idempotency_key': key()}, owner)
    current = s.room_context.read(lead, lead_actor)
    assert [f['key'] for f in current['facts']] == ['a']
    assert {p['project_id'] for p in current['active_projects']} == {'proj'}
    with pytest.raises(DevError) as stale:
        s.room_context.read({**lead, 'cursor': old_page['next_cursor']}, lead_actor)
    assert stale.value.code == 'INVALID_CURSOR'
    with pytest.raises(DevError) as unavailable:
        send(s, owner, lead, '不能借纠正泄漏已移出项目。',
            supersedes_context_ids=added['fact_ids'], expected_context_version=added['version'])
    assert unavailable.value.code == 'CONTEXT_FACT_NOT_FOUND'


@pytest.mark.asyncio
async def test_context_migrated_partition_source_respects_narrow_member_scope(collab):
    s, owner, worker, *_ = collab
    broad = worker_for(s, worker, 'context-wide-grant', scopes=('read',), projects=('proj', 'otherproj'))
    s, owner, actor, _, lead, credentials = await connected(collab,
        [{'project': 'proj'}, {'project': 'otherproj'}],
        [{'project_id': 'proj'}, {'project_id': 'otherproj'}], worker_override=broad)
    _, _, _, _, peer, _ = await connected(collab, scopes=[{'project_id': 'proj'}],
        worker_override=broad, conversation_id=lead['conversation_id'])
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    partition = next(p['room_id'] for p in room['projects'] if p['project_id'] == 'otherproj')
    message = s.chatroom.create({'project': 'otherproj', 'environment_id': 'production',
        'room_id': partition, 'conversation_id': room['id'], 'body_text': '原 B 分区记录。',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message['id'],))
    assert row['room_id'] == partition and not json.loads(row['body']).get('provenance_project_ids')
    s.room_context.update(request(s, actor, lead, credentials, facts=[fact(s, message)]), actor)
    assert s.room_context.read(lead, actor)['facts'][0]['project_ids'] == ['otherproj']
    assert not s.room_context.read(peer, actor)['facts']
    assert not s.room_consumers._visible_message(s.rooms.member(peer['member_id']), actor, row)


@pytest.mark.asyncio
async def test_context_environment_scope_survives_correction_replies_and_renaming(collab):
    s, owner, actor, _, lead, credentials = await connected(collab,
        [{'project': 'proj', 'environment_id': 'production'}, {'project': 'proj', 'environment_id': 'staging'}],
        [{'project_id': 'proj', 'environment_id': 'staging'}])
    _, _, _, _, peer, peer_credentials = await connected(collab,
        scopes=[{'project_id': 'proj', 'environment_id': 'production'}], conversation_id=lead['conversation_id'])
    room = s.rooms.read({'conversation_id': lead['conversation_id']}, owner)['room']
    partition = next(p['room_id'] for p in room['projects'] if p['environment_id'] == 'staging')
    source = s.chatroom.create({'project': 'proj', 'environment_id': 'staging', 'room_id': partition,
        'conversation_id': room['id'], 'body_text': 'Staging 私有约束。',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    first = s.room_context.update(request(s, actor, lead, credentials,
        facts=[fact(s, source, kind='constraint')]), actor)
    assert not s.room_context.read(peer, actor)['facts']
    expected = [{'project_id': 'proj', 'environment_id': 'staging'}]
    corrected = send(s, owner, lead, 'Staging 约束纠正。',
        supersedes_context_ids=first['fact_ids'], expected_context_version=first['version'])
    owner_reply = send(s, owner, lead, '继续说明 staging 的纠正。', reply_to_id=corrected['id'])
    reply = s.room_runtime.reply({**lead, **credentials,
        'source_message_id': owner_reply['id'], 'source_version': owner_reply['version'],
        'source_digest': reference(s, owner_reply)['digest'],
        'expected_inbox_revision': s.room_consumers.inbox(lead, actor)['inbox_revision'],
        'body_text': 'Staging 纠正已收到。', 'complete': False, 'idempotency_key': key()}, actor)['message']
    continued = send(s, owner, lead, '沿同一 staging 回复继续。', reply_to_id=reply['id'])
    for message in (corrected, owner_reply, reply, continued):
        row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message['id'],))
        assert json.loads(row['body'])['provenance_scopes'] == expected
        assert not s.room_consumers._visible_message(s.rooms.member(peer['member_id']), actor, row)
    wrong_recipient = send(s, owner, lead, '不能给 production-only 成员扩大读取范围。',
        reply_to_id=reply['id'], member_id=peer['member_id'])
    with pytest.raises(DevError):
        s.room_runtime.reply({**peer, **peer_credentials,
            'source_message_id': wrong_recipient['id'], 'source_version': wrong_recipient['version'],
            'source_digest': reference(s, wrong_recipient)['digest'],
            'expected_inbox_revision': s.room_consumers.inbox(peer, actor)['inbox_revision'],
            'body_text': '不得直用已知原消息绕过成员环境范围。', 'idempotency_key': key()}, actor)
    s.room_context.update(request(s, actor, lead, credentials,
        facts=[fact(s, corrected, kind='constraint', text='Staging 新约束')]), actor)
    new_source = send(s, owner, lead, '重新整理约束结构。')
    current = s.room_context.read(lead, actor)['facts'][0]
    s.room_context.update(request(s, actor, lead, credentials,
        supersedes=[current['id']], facts=[fact(s, new_source, key='renamed', kind='constraint')]), actor)
    assert s.room_context.read(lead, actor)['facts'][0]['project_scopes'] == expected
    assert not s.room_context.read(peer, actor)['facts']
    room = s.rooms.read({'conversation_id': room['id']}, owner)['room']
    s.rooms.projects({'conversation_id': room['id'], 'expected_version': room['version'],
        'projects': [{'project': 'proj', 'environment_id': 'production'}], 'idempotency_key': key()}, owner)
    assert not s.room_context.owner_read(room['id'], owner)['facts']


@pytest.mark.asyncio
async def test_archived_owner_context_history_is_read_only_and_never_member_authority(collab):
    s, owner, actor, _, identity, credentials = await connected(collab,
        [{'project': 'proj'}], [{'project_id': 'proj'}])
    source = send(s, owner, identity, '归档后仍可核对的本人来源。')
    saved = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, source, project_ids=['proj'])]), actor)
    mutation = request(s, actor, identity, credentials, reviewed_sources=[reference(s, source)])
    room = s.rooms.read({'conversation_id': identity['conversation_id']}, owner)['room']
    s.rooms.update({'conversation_id': room['id'], 'expected_version': room['version'],
        'state': 'archived', 'idempotency_key': key()}, owner)
    changes = s.store.one('SELECT total_changes() n')['n']
    view = s.room_context.owner_read(room['id'], owner)
    assert view['archived'] and not view['writable']
    assert [f['id'] for f in view['facts']] == saved['fact_ids']
    assert s.store.one('SELECT total_changes() n')['n'] == changes
    with pytest.raises(DevError):
        s.room_context.read(identity, actor)
    with pytest.raises(DevError):
        s.room_context.update(mutation, actor)
    with pytest.raises(DevError):
        send(s, owner, identity, '归档后不能通过纠正创建新输入。',
            supersedes_context_ids=saved['fact_ids'], expected_context_version=saved['version'])


@pytest.mark.asyncio
async def test_context_original_completed_artifact_references_do_not_create_execution(collab):
    s, owner, actor, _, identity, credentials = await connected(collab,
        [{'project': 'proj'}], [{'project_id': 'proj'}])
    started, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    work = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (started['goal_id'],))
    lease, operation = admit_read(s, identity, credentials, actor, started['goal_id'], work, 'proj')
    completed = report(s, identity, credentials, actor, started['goal_id'], lease, operation)
    source = s.store.one("SELECT * FROM collaboration_messages WHERE goal_id=? AND kind='delegation_result'", (started['goal_id'],))
    ref = {'work_item_id': completed['id'], 'version': completed['version'], 'digest': digest(completed['result'])}
    value = fact(s, source, key='readme', kind='artifact', text='已完成 README 核对', artifact_refs=[ref])
    before = effect_counts(s)
    with pytest.raises(DevError) as stale:
        s.room_context.update(request(s, actor, identity, credentials,
            facts=[{**value, 'artifact_refs': [{**ref, 'version': ref['version'] - 1}]}]), actor)
    assert stale.value.code == 'CONTEXT_ARTIFACT_CHANGED'
    s.room_context.update(request(s, actor, identity, credentials, facts=[value]), actor)
    assert effect_counts(s) == before
    view = s.room_context.read({**identity, 'input_message_id': source['id']}, actor)
    assert view['facts'][0]['artifact_refs'] == [ref] and view['facts'][0]['project_ids'] == ['proj']
    assert view['current_input']['message_id'] == source['id']
    assert view['related_goals'][0]['id'] == started['goal_id']


@pytest.mark.asyncio
async def test_context_pagination_budget_and_unread_source_are_explicit(collab):
    s, owner, actor, _, identity, credentials = await connected(collab)
    source = send(s, owner, identity, '房间问题清单，不代表新执行授权。')
    for start_index in range(0, 25, 10):
        s.room_context.update(request(s, actor, identity, credentials,
            facts=[fact(s, source, key='q' + str(i), kind='question', text='需核对的问题。' * 40)
                for i in range(start_index, min(25, start_index + 10))]), actor)
    seen = []
    cursor = ''
    while True:
        page = s.room_context.read({**identity, 'limit': 3, 'cursor': cursor, 'byte_budget': 4096}, actor)
        assert len(canonical(page).encode()) <= 4096
        assert page['incomplete'] and not page['history_complete']
        seen.extend(row['id'] for row in page['facts'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 25
    assert page['current_facts_complete'] and page['review_state'] == 'current'
    before = s.store.one('SELECT total_changes() n')['n']
    assert s.room_context.owner_read(identity['conversation_id'], owner)['facts']
    assert s.store.one('SELECT total_changes() n')['n'] == before


@pytest.mark.asyncio
async def test_context_review_does_not_forget_owner_input_outside_recent_window(collab):
    s, owner, actor, _, identity, credentials = await connected(collab)
    source = send(s, owner, identity, '原始目标。')
    s.room_context.update(request(s, actor, identity, credentials, facts=[fact(s, source)]), actor)
    unreviewed = send(s, owner, identity, '这条本人新约束尚未复核。')
    reviewed = [send(s, owner, identity, '可确认的后续输入 ' + str(i)) for i in range(40)]
    for offset in range(0, 40, 32):
        s.room_context.update(request(s, actor, identity, credentials,
            reviewed_sources=[reference(s, row) for row in reviewed[offset:offset + 32]]), actor)
    page = s.room_context.read(identity, actor)
    assert unreviewed['id'] not in {r['message_id'] for r in page['recent_messages']}
    assert page['review_state'] == 'needs_review' and 'unreviewed_input' in page['incomplete_reasons']
    s.room_context.update(request(s, actor, identity, credentials,
        reviewed_sources=[reference(s, unreviewed)]), actor)
    assert s.room_context.read(identity, actor)['review_state'] == 'current'


@pytest.mark.asyncio
async def test_context_wechat_revision_requires_new_source_and_can_retire_old_fact(collab):
    from tests.test_room_weixin_native import setup_channel
    from tests.test_dot_weixin_channel import packet
    (s, owner, actor, clock, identity, credentials), flow, _, action = await setup_channel(collab)
    created_ms = int(clock[0] * 1000)
    original_id = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '本人决定先整理目标。', create_time_ms=created_ms))['message_ids'][0]
    original = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (original_id,))
    saved = s.room_context.update(request(s, actor, identity, credentials,
        facts=[fact(s, original, kind='decision', text='先整理目标')]), actor)
    assert s.room_context.read(identity, actor)['facts']
    assert not s.store.all('SELECT * FROM room_channel_owner_inputs')  # Chat identity never grants task consent.
    clock[0] += 3
    updated_id = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '纠正：先收集问题。', create_time_ms=created_ms, update_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    updated = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (updated_id,))
    view = s.room_context.read(identity, actor)
    assert not view['facts'] and 'source_needs_review' in view['incomplete_reasons']
    s.room_context.update(request(s, actor, identity, credentials,
        supersedes=saved['fact_ids'], facts=[fact(s, updated, kind='decision', text='先收集问题')]), actor)
    assert [f['text'] for f in s.room_context.read(identity, actor)['facts']] == ['先收集问题']
    assert not s.store.all('SELECT * FROM coordination_goals')
    await action(s.room_channels.read(flow['id'], owner)['flow'], 'disconnect')
    invalidated = s.room_context.read(identity, actor)
    assert not invalidated['facts'] and 'source_needs_review' in invalidated['incomplete_reasons']
    current = s.store.one('SELECT fact_id FROM room_context_current WHERE conversation_id=?', (identity['conversation_id'],))
    cleared = send(s, owner, identity, '本人撤回旧渠道上下文。',
        supersedes_context_ids=[current['fact_id']], expected_context_version=2)
    assert cleared['id'] and not s.room_context.read(identity, actor)['facts']
