"""Multi-Dot server contracts; these tests do not assert live host delivery."""
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


def setup_team(fixture, *, shared_grant=False, writable=False, **team_options):
    s, owner, worker, dot, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    actors = [worker, worker if shared_grant else dot]
    if writable:
        actors[0] = replace(worker, grant_id='broad', actor='mcp:broad:team')
    slots = []
    for index, actor in enumerate(actors):
        created = s.dots.create({**scope, 'conversation_id': room['id'], 'label': 'Dot ' + str(index + 1),
            'capabilities': ['read'], 'duplex': True, 'confirm_tasks': True, 'idempotency_key': key()}, owner)['dot']
        joined = s.dots.join({'code': created['join_code'], 'idempotency_key': key()}, actor)
        slots.append(joined['slot'])
    raw = {**scope, 'conversation_id': room['id'], 'label': '审核团队',
        'dot_ids': [d['id'] for d in slots], 'coordinator_dot_id': slots[0]['id'],
        'project_ids': ['proj'], 'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key(), **team_options}
    team = s.coordination.teams.create(raw, owner)['team']
    return SimpleNamespace(s=s, q=s.coordination, t=s.coordination.teams, owner=owner, actors=actors,
        slots=slots, room=room, scope=scope, team=team, create=raw, clock=clock)


def source(c, *, selected=True, text='检查 README 并让另一位成员复核结果'):
    result = c.s.chatroom.create({**c.scope, 'room_id': c.room['id'], 'conversation_id': c.room['id'],
        'body_text': text, 'mentions': [{'slot_id': c.slots[0]['id']}],
        'client_message_id': key(), 'idempotency_key': key()}, c.owner)
    row = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (result['message']['id'],))
    if selected:
        c.t.bind_message(c.room, row, c.team['id'], c.owner)
    return {**c.scope, 'team_id': c.team['id'], 'message_id': row['id'],
        'source_version': row['version'], 'source_digest': digest(json.loads(row['body'])['body_text']),
        'idempotency_key': key()}


def goal(c):
    c.input = source(c)
    c.goal = c.t.from_message(c.input, c.actors[0])['goal']
    c.ctx = {**c.scope, 'goal_id': c.goal['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1}
    return c.goal


def work(c, index=1):
    return c.q.work_create({**c.ctx, 'objective': '复核当前请求', 'acceptance': '给出可核对依据',
        'target_project_id': 'proj', 'assignee_grant_id': c.actors[index].grant_id,
        'assignee_dot_id': c.slots[index]['id'], 'idempotency_key': key()}, c.actors[0])['work_item']


def complete(c, item, actor):
    claimed = c.q.work_claim({**c.ctx, 'work_item_id': item['id'], 'expected_version': item['version'],
                             'idempotency_key': key()}, actor)['work_item']
    return c.q.result({**c.ctx, 'work_item_id': item['id'], 'attempt': claimed['attempt'],
        'fencing_token': claimed['fencing_token'], 'outcome': 'succeeded', 'summary': '已核对事实',
        'operation_ids': [], 'idempotency_key': key()}, actor)['work_item']


def test_explicit_owner_confirmation_never_inherits_personal_dot_approval(collab):
    c = setup_team(collab)
    before = len(c.s.store.all('SELECT * FROM collaboration_teams'))
    with pytest.raises(DevError) as exc:
        c.t.create({**c.create, 'idempotency_key': key()}, c.actors[0])
    assert exc.value.code == 'OWNER_REQUIRED'
    assert len(c.s.store.all('SELECT * FROM collaboration_teams')) == before
    raw = source(c, selected=False)
    with pytest.raises(DevError) as exc:
        c.t.from_message(raw, c.actors[0])
    assert exc.value.code == 'TEAM_OWNER_SELECTION_REQUIRED'
    assert not c.s.store.all('SELECT * FROM coordination_goals')


def test_original_source_creates_one_goal_across_retry_keys_and_members(collab):
    c = setup_team(collab)
    first = goal(c)
    second = c.t.from_message({**c.input, 'idempotency_key': key()}, c.actors[0])['goal']
    assert first['id'] == second['id']
    assert len(c.s.store.all('SELECT * FROM coordination_goals')) == 1
    assert len(c.s.store.all('SELECT * FROM coordination_approvals')) == 1
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    with pytest.raises(DevError) as exc:
        c.t.from_message({**c.input, 'idempotency_key': key()}, c.actors[1])
    assert exc.value.code == 'TEAM_COORDINATOR_REQUIRED'


def test_same_key_different_source_is_conflict(collab):
    c = setup_team(collab)
    goal(c)
    other = source(c, text='另一条完全不同的用户任务')
    with pytest.raises(DevError) as exc:
        c.t.from_message({**other, 'idempotency_key': c.input['idempotency_key']}, c.actors[0])
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert len(c.s.store.all('SELECT * FROM coordination_goals')) == 1


def test_only_current_coordinator_mutates_graph_and_frozen_epoch_rejects_stale(collab):
    c = setup_team(collab)
    goal(c)
    item = work(c)
    bad = {**c.ctx, 'work_item_id': item['id'], 'expected_version': item['version'],
        'assignee_grant_id': c.actors[0].grant_id, 'assignee_dot_id': c.slots[0]['id'], 'idempotency_key': key()}
    with pytest.raises(DevError) as exc:
        c.q.work_assign(bad, c.actors[1])
    assert exc.value.code == 'TEAM_COORDINATOR_REQUIRED'
    updated = c.t.takeover({**c.ctx, 'coordinator_dot_id': c.slots[1]['id'], 'idempotency_key': key()}, c.owner)['goal']
    assert updated['team']['coordinator_epoch'] == 2
    with pytest.raises(DevError) as exc:
        c.q.work_assign(bad, c.actors[0])
    assert exc.value.code == 'TEAM_STALE_CONTEXT'
    c.ctx['coordinator_epoch'] = 2
    changed = c.q.work_assign({**bad, 'coordinator_epoch': 2}, c.actors[1])['work_item']
    assert changed['assignee_dot_id'] == c.slots[0]['id']


def test_shared_grant_needs_server_bound_dot_identity(collab):
    c = setup_team(collab, shared_grant=True)
    with pytest.raises(DevError) as exc:
        c.t.from_message({**source(c), 'dot_id': c.slots[0]['id']}, c.actors[0])
    assert exc.value.code in {'TEAM_CONSUMER_IDENTITY_REQUIRED', 'DOT_CONSUMER_REQUIRED'}


def test_handoff_requires_current_artifacts_and_stops_cycles(collab):
    c = setup_team(collab)
    goal(c)
    item = complete(c, work(c), c.actors[1])
    ref = item['artifact_ref']
    raw = {**c.ctx, 'recipient_dot_id': c.slots[0]['id'], 'work_item_id': item['id'],
        'purpose': '请主责核对复核结论', 'artifact_refs': [ref], 'idempotency_key': key()}
    first = c.t.handoff(raw, c.actors[1])['handoff']
    retry = c.t.handoff({**raw, 'idempotency_key': key()}, c.actors[1])['handoff']
    assert first['id'] == retry['id']
    with pytest.raises(DevError) as exc:
        c.t.handoff({**raw, 'artifact_refs': [{**ref, 'version': ref['version'] - 1}], 'idempotency_key': key()}, c.actors[1])
    assert exc.value.code == 'TEAM_ARTIFACT_STALE'
    with pytest.raises(DevError) as exc:
        c.t.handoff({**raw, 'purpose': '新内容使用旧key'}, c.actors[1])
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(DevError) as exc:
        c.t.handoff({**c.ctx, 'recipient_dot_id': c.slots[1]['id'], 'purpose': '再交回去',
            'causation_id': first['id'], 'hop_count': 1, 'idempotency_key': key()}, c.actors[0])
    assert exc.value.code == 'TEAM_HANDOFF_LOOP'


def test_publish_is_one_message_and_requires_all_real_current_results(collab):
    c = setup_team(collab)
    goal(c)
    kickoff = c.q.work_view(c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (c.goal['id'],)))
    refs = [complete(c, kickoff, c.actors[0])['artifact_ref']]
    raw = {**c.ctx, 'summary': '请求已经核对完成。', 'outcome': 'succeeded', 'artifact_refs': refs, 'idempotency_key': key()}
    with pytest.raises(DevError) as exc:
        c.t.publish(raw, c.actors[1])
    assert exc.value.code == 'TEAM_COORDINATOR_REQUIRED'
    first = c.t.publish(raw, c.actors[0])['publication']
    second = c.t.publish({**raw, 'idempotency_key': key()}, c.actors[0])['publication']
    assert first['message_id'] == second['message_id']
    assert len(c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='team_result'")) == 1
    assert c.q.read({**c.scope, 'goal_id': c.goal['id']}, c.owner)['goal']['team']['publication']['state'] == 'committed'


def test_new_intent_blocks_old_work_and_reuses_goal_source_identity(collab):
    c = setup_team(collab)
    goal(c)
    item = work(c)
    new = source(c, text='暂停此前计划，先只核对文档中的一个问题')
    revised = c.t.revise_intent({**c.ctx, **{k: new[k] for k in ('message_id', 'source_version', 'source_digest')},
        'idempotency_key': key()}, c.owner)['goal']
    assert revised['team']['intent_revision'] == 2
    old_handling = c.s.message_handling.current(c.slots[0]['id'], c.input['message_id'])
    assert old_handling['handling_state'] == 'no_response_required'
    assert old_handling['evidence_source'] == 'authenticated_owner_control'
    assert c.q.work(c.q.object(c.room, c.goal['id']), item['id'])['state'] == 'blocked'
    with pytest.raises(DevError) as exc:
        c.q.work_claim({**c.ctx, 'expected_intent_revision': 2, 'work_item_id': item['id'],
            'expected_version': item['version'], 'idempotency_key': key()}, c.actors[1])
    assert exc.value.code == 'TEAM_INTENT_CHANGED'
    assert c.t.from_message(new, c.actors[0])['goal']['id'] == c.goal['id']
    assert len(c.s.store.all('SELECT * FROM coordination_goals')) == 1


def test_owner_pause_blocks_future_mutations_without_replaying_operations(collab):
    c = setup_team(collab)
    goal(c)
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError) as exc:
        work(c)
    assert exc.value.code == 'TEAM_INACTIVE'
    assert c.s.store.one('SELECT state FROM coordination_goals WHERE id=?', (c.goal['id'],))['state'] == 'paused'
    assert c.s.store.one('SELECT state FROM coordination_work WHERE goal_id=?', (c.goal['id'],))['state'] == 'blocked'


def test_team_scope_snapshot_rejects_member_rebinding_and_raw_role_spoof(collab):
    c = setup_team(collab)
    goal(c)
    with pytest.raises(DevError) as exc:
        c.q.work_create({**c.ctx, 'dot_id': c.slots[0]['id'], 'objective': '冒充主责',
            'acceptance': '不应执行', 'target_project_id': 'proj', 'assignee_grant_id': c.actors[1].grant_id,
            'assignee_dot_id': c.slots[1]['id'], 'idempotency_key': key()}, c.actors[1])
    assert exc.value.code == 'TEAM_DOT_IDENTITY_MISMATCH'
    c.s.store.execute("UPDATE collaboration_join_slots SET grant_id='second' WHERE id=?", (c.slots[1]['id'],))
    with pytest.raises(DevError) as exc:
        work(c)
    assert exc.value.code == 'TEAM_MEMBER_CHANGED'


async def activate_consumers(c, *, one=False):
    c.s.config = replace(c.s.config, events_enabled=True)

    async def receiver(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        return Reply(204)

    c.s.events = EventService(c.s, receiver)
    c.sessions, c.bindings = [], []
    for slot, actor in zip(c.slots[:1] if one else c.slots, c.actors[:1] if one else c.actors):
        dot = c.s.dots.find(slot['id'])
        configured = c.s.dot_mailbox.configure({**c.scope, 'dot_id': dot['id'],
            'protocol_version': 2, 'confirm_consumer_protocol': True, 'idempotency_key': key()}, actor)
        subscribed = await c.s.events.subscribe({**configured['subscription_request'],
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/team/' + dot['id'],
                'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}}, actor)
        binding = c.s.dot_mailbox.binding(dot['id'])
        sub = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscribed['id'],))
        ticket = c.s.dot_consumers.issue_ticket(c.room, dot, sub, binding['generation'], key())
        session = c.s.dot_consumers.begin({**c.scope, 'dot_id': dot['id'], 'binding_generation': binding['generation'],
            'wake_ticket': ticket, 'consumer_run_id': key(), 'idempotency_key': key()}, actor)
        c.sessions.append(session)
        c.bindings.append(binding)


def consumer(c, index):
    return {'dot_id': c.slots[index]['id'],
        **{k: c.sessions[index][k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')},
        'expected_inbox_revision': c.s.dot_mailbox.current_revision(c.slots[index]['id'])}


def mailbox(c, index):
    return c.s.dot_mailbox.read({**c.scope, 'dot_id': c.slots[index]['id'],
        'binding_generation': c.bindings[index]['generation']}, c.actors[index])['items']


@pytest.mark.asyncio
async def test_shared_grant_v2_consumers_route_and_finish_one_team_goal(collab):
    c = setup_team(collab, shared_grant=True)
    await activate_consumers(c)
    raw = source(c)
    created = c.t.from_message({**raw, **consumer(c, 0)}, c.actors[0])['goal']
    c.goal = created
    assert c.s.message_handling.current(c.slots[0]['id'], raw['message_id'])['handling_state'] == 'work_linked'
    c.ctx = {**c.scope, 'goal_id': created['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1}
    assigned = c.q.work_create({**c.ctx, **consumer(c, 0), 'objective': '从第二个 dot 复核证据',
        'acceptance': '列出可核对依据', 'target_project_id': 'proj',
        'assignee_grant_id': c.actors[1].grant_id, 'assignee_dot_id': c.slots[1]['id'],
        'idempotency_key': key()}, c.actors[0])['work_item']
    assert any(x['kind'] == 'work_ready' and x['object_id'] == assigned['id'] for x in mailbox(c, 1))
    assert not any(x['kind'] == 'work_ready' and x['object_id'] == assigned['id'] for x in mailbox(c, 0))
    claim = {**c.ctx, 'work_item_id': assigned['id'], 'expected_version': assigned['version'], 'idempotency_key': key()}
    with pytest.raises(DevError) as exc:
        c.q.work_claim({**claim, **consumer(c, 0)}, c.actors[0])
    assert exc.value.code == 'DOT_CONSUMER_WORK_MISMATCH'
    claimed = c.q.work_claim({**claim, **consumer(c, 1)}, c.actors[1])['work_item']
    result = c.q.result({**c.ctx, **consumer(c, 1), 'work_item_id': assigned['id'],
        'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'], 'outcome': 'succeeded',
        'summary': '第二位成员已经完成核对', 'operation_ids': [], 'idempotency_key': key()}, c.actors[1])['work_item']
    handed = c.t.handoff({**c.ctx, **consumer(c, 1), 'recipient_dot_id': c.slots[0]['id'],
        'work_item_id': assigned['id'], 'purpose': '请合并这项已核对结果',
        'artifact_refs': [result['artifact_ref']], 'idempotency_key': key()}, c.actors[1])['handoff']
    assert any(x['kind'] == 'peer_handoff' and x['object_id'] == handed['id'] for x in mailbox(c, 0))
    assert not any(x['kind'] == 'peer_handoff' and x['object_id'] == handed['id'] for x in mailbox(c, 1))
    kickoff = c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=? AND id!=?', (created['id'], assigned['id']))
    lease = c.q.work_claim({**c.ctx, **consumer(c, 0), 'work_item_id': kickoff['id'],
        'expected_version': kickoff['version'], 'idempotency_key': key()}, c.actors[0])['work_item']
    lead_result = c.q.result({**c.ctx, **consumer(c, 0), 'work_item_id': kickoff['id'],
        'attempt': lease['attempt'], 'fencing_token': lease['fencing_token'], 'outcome': 'succeeded',
        'summary': '已整合并核对全部结果', 'operation_ids': [], 'idempotency_key': key()}, c.actors[0])['work_item']
    publication = c.t.publish({**c.ctx, **consumer(c, 0), 'summary': '两位成员完成同一个原始任务。',
        'outcome': 'succeeded', 'artifact_refs': [result['artifact_ref'], lead_result['artifact_ref']],
        'idempotency_key': key()}, c.actors[0])['publication']
    assert publication['state'] == 'committed'
    assert c.s.message_handling.current(c.slots[0]['id'], raw['message_id'])['handling_state'] == 'reply_committed'
    assert not any(x['kind'] == 'message' and x['object_id'] == raw['message_id'] for x in mailbox(c, 0))
    assert len(c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='team_result'")) == 1
    stored = canonical(c.s.store.all('SELECT * FROM collaboration_idempotency'))
    assert all(session['consumer_proof'] not in stored for session in c.sessions)


def test_handoff_budget_is_shared_and_retry_does_not_spend_again(collab):
    c = setup_team(collab)
    goal(c)
    handoff = {**c.ctx, 'recipient_dot_id': c.slots[1]['id'], 'purpose': '核对当前计划',
               'idempotency_key': key()}
    c.t.handoff(handoff, c.actors[0])
    c.t.handoff({**handoff, 'idempotency_key': key()}, c.actors[0])
    assert c.q.object(c.room, c.goal['id'])['messages'] == 1
    budget = c.goal['budget']['max_messages']
    c.s.store.execute('UPDATE coordination_goals SET messages=? WHERE id=?', (budget, c.goal['id']))
    with pytest.raises(DevError) as exc:
        c.t.handoff({**handoff, 'purpose': '新一轮交接', 'idempotency_key': key()}, c.actors[0])
    assert exc.value.code == 'GOAL_MESSAGE_BUDGET'


@pytest.mark.parametrize('field', ['coordinator_epoch', 'expected_intent_revision', 'consumer_fence'])
def test_boolean_context_fields_cannot_impersonate_integer_versions(collab, field):
    c = setup_team(collab)
    goal(c)
    with pytest.raises(DevError) as exc:
        c.t.takeover({**c.ctx, 'coordinator_dot_id': c.slots[1]['id'],
            'idempotency_key': key(), field: True}, c.owner)
    assert exc.value.code == 'INVALID_ARGUMENTS'


def test_owner_resume_preserves_approval_and_old_work_without_replay(collab):
    c = setup_team(collab)
    goal(c)
    approval = c.goal['approval_id']
    original = c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (c.goal['id'],))
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    raw = {**c.scope, 'team_id': c.team['id'], 'expected_version': 2,
           'action': 'resume', 'idempotency_key': key()}
    resumed = c.t.control(raw, c.owner)['team']
    assert resumed['state'] == 'active'
    assert c.t.control(raw, c.owner)['team']['id'] == resumed['id']
    current = c.q.object(c.room, c.goal['id'])
    assert current['state'] == 'active' and current['approval_id'] == approval
    assert c.t.current_intent_revision(c.goal['id']) == 2
    assert c.q.work(current, original['id'])['state'] == 'blocked'
    assert len(c.s.store.all('SELECT * FROM coordination_approvals')) == 1
    assert not c.s.store.all('SELECT * FROM coordination_operations')
    c.ctx['expected_intent_revision'] = 2
    replacement = work(c)
    assert replacement['intent_revision'] == 2
    assert c.q.work(current, original['id'])['attempt'] == 0


def test_resume_cannot_revive_independently_paused_cancelled_or_expired_goals(collab):
    c = setup_team(collab)
    goal(c)
    c.q.control({**c.scope, 'goal_id': c.goal['id'], 'expected_version': c.goal['version'],
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 2,
        'action': 'resume', 'idempotency_key': key()}, c.owner)
    assert c.q.object(c.room, c.goal['id'])['state'] == 'paused'
    assert c.t.current_intent_revision(c.goal['id']) == 1
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 3,
        'action': 'revoke', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 4,
            'action': 'resume', 'idempotency_key': key()}, c.owner)


def test_resume_revalidates_original_immutable_team_scope(collab):
    c = setup_team(collab)
    goal(c)
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    c.s.store.execute("UPDATE projects SET root='/tmp/replaced-project' WHERE id='proj'")
    with pytest.raises(DevError) as exc:
        c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 2,
            'action': 'resume', 'idempotency_key': key()}, c.owner)
    assert exc.value.code == 'TEAM_PROJECT_CHANGED'
    assert c.t.find(c.room, c.team['id'])['state'] == 'paused'


@pytest.mark.asyncio
async def test_fifty_team_goals_keep_one_native_subscription_and_paginate_all_work(collab):
    c = setup_team(collab, max_goals=50)
    await activate_consumers(c, one=True)
    created = []
    for number in range(50):
        raw = source(c, text='独立任务 ' + str(number + 1))
        created.append(c.t.from_message({**raw, **consumer(c, 0)}, c.actors[0])['goal']['id'])
    assert len(set(created)) == 50
    assert len(c.s.store.all('SELECT * FROM mcp_event_subscriptions')) == 1
    args = {**c.scope, 'dot_id': c.slots[0]['id'], 'binding_generation': c.bindings[0]['generation'], 'limit': 17}
    work_ids = set()
    pages = 0
    while True:
        page = c.s.dot_mailbox.read(args, c.actors[0])
        pages += 1
        work_ids.update(item['object_id'] for item in page['items'] if item['kind'] == 'work_ready')
        assert all(not item['ready'] for item in page['items'] if item['kind'] == 'message')
        if not page['next_cursor']:
            break
        args['cursor'] = page['next_cursor']
    assert len(work_ids) == 50 and pages > 1
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 50
    assert len(c.t.goal_summaries({**c.scope, 'conversation_id': c.room['id']}, c.owner)) == 40


def test_resume_waits_for_original_ambiguous_operation_before_new_execution(collab):
    c = setup_team(collab)
    goal(c)
    kickoff = c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (c.goal['id'],))
    lease = c.q.work_claim({**c.ctx, 'work_item_id': kickoff['id'], 'expected_version': kickoff['version'],
        'idempotency_key': key()}, c.actors[0])['work_item']
    original = c.q.admit({**c.ctx, 'work_item_id': kickoff['id'], 'attempt': lease['attempt'],
        'fencing_token': lease['fencing_token'], 'tool': 'read', 'arguments': {'path': 'README.md'},
        'idempotency_key': key()}, c.actors[0])
    c.s.store.execute("UPDATE operations SET state='unknown',attempts=1 WHERE id=?", (original['operation_id'],))
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    c.t.control({**c.scope, 'team_id': c.team['id'], 'expected_version': 2,
        'action': 'resume', 'idempotency_key': key()}, c.owner)
    c.ctx['expected_intent_revision'] = 2
    planned = work(c)
    claim = {**c.ctx, 'work_item_id': planned['id'], 'expected_version': planned['version'], 'idempotency_key': key()}
    with pytest.raises(DevError) as exc:
        c.q.work_claim(claim, c.actors[1])
    assert exc.value.code == 'TEAM_ORIGINAL_OPERATIONS_PENDING'
    assert len(c.s.store.all('SELECT * FROM coordination_operations')) == 1
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (original['operation_id'],))
    c.s.runtime.complete(operation, {'ok': False, 'error': {'code': 'CANCELLED', 'message': 'fixture original operation settled'}})
    assert c.q.work_claim(claim, c.actors[1])['work_item']['state'] == 'leased'
    assert len(c.s.store.all('SELECT * FROM coordination_operations')) == 1


def test_team_write_work_cannot_claim_success_without_real_operation_receipts(collab):
    c = setup_team(collab, writable=True, capabilities=['read', 'write'])
    goal(c)
    item = c.q.work_create({**c.ctx, 'objective': '修改已批准的项目文件', 'acceptance': '报告真实写入回执',
        'target_project_id': 'proj', 'assignee_grant_id': c.actors[0].grant_id,
        'assignee_dot_id': c.slots[0]['id'], 'required_capabilities': ['read', 'write'],
        'idempotency_key': key()}, c.actors[0])['work_item']
    lease = c.q.work_claim({**c.ctx, 'work_item_id': item['id'], 'expected_version': item['version'],
        'idempotency_key': key()}, c.actors[0])['work_item']
    with pytest.raises(DevError) as exc:
        c.q.result({**c.ctx, 'work_item_id': item['id'], 'attempt': lease['attempt'],
            'fencing_token': lease['fencing_token'], 'outcome': 'succeeded', 'summary': '声称已经写好',
            'operation_ids': [], 'idempotency_key': key()}, c.actors[0])
    assert exc.value.code == 'WORK_OPERATION_REQUIRED'
