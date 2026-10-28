"""Operation completion reaches one stable team inbox without extra subscriptions.

Real Store/Runtime admission and completion are exercised with an injected event
receiver and synthetic Agent receipts. No live Dot host is asserted here.
"""
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import MAILBOX_EVENT, OPERATION_EVENT, canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


async def running_team_operation(fixture):
    s, owner, actor, _, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    s.config = replace(s.config, events_enabled=True)

    async def receiver(url, body, headers):
        body = json.loads(body)
        return Reply(200, canonical({'challenge': body['challenge']}).encode()) if body.get('type') == 'verification' else Reply(204)

    s.events = EventService(s, receiver)
    c = SimpleNamespace(s=s, owner=owner, actor=actor, room=room, clock=clock, scope=scope, dots=[], sessions=[], bindings=[])
    for number in range(2):
        created = s.dots.create({**scope, 'conversation_id': room['id'], 'label': 'worker ' + str(number),
            'capabilities': ['read'], 'duplex': True, 'confirm_tasks': True, 'idempotency_key': key()}, owner)['dot']
        s.dots.join({'code': created['join_code'], 'idempotency_key': key()}, actor)
        dot = s.dots.find(created['id'])
        configured = s.dot_mailbox.configure({**scope, 'dot_id': dot['id'], 'protocol_version': 2,
            'confirm_consumer_protocol': True, 'idempotency_key': key()}, actor)
        sub = await s.events.subscribe({**configured['subscription_request'],
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/completion/' + dot['id'],
                'secret': 'whsec_' + base64.b64encode(b'z' * 32).decode()}}, actor)
        binding = s.dot_mailbox.binding(dot['id'])
        saved_sub = s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],))
        ticket = s.dot_consumers.issue_ticket(room, dot, saved_sub, binding['generation'], key())
        session = s.dot_consumers.begin({**scope, 'dot_id': dot['id'], 'binding_generation': binding['generation'],
            'wake_ticket': ticket, 'consumer_run_id': key(), 'idempotency_key': key()}, actor)
        c.dots.append(dot)
        c.sessions.append(session)
        c.bindings.append(binding)
    c.team = s.coordination.teams.create({**scope, 'conversation_id': room['id'], 'label': 'completion team',
        'dot_ids': [d['id'] for d in c.dots], 'coordinator_dot_id': c.dots[0]['id'],
        'project_ids': ['proj'], 'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key()}, owner)['team']
    message = s.chatroom.create({**scope, 'conversation_id': room['id'], 'room_id': room['id'],
        'team_id': c.team['id'], 'body_text': '请在同一个任务中读取文件并核对结果',
        'mentions': [{'slot_id': c.dots[0]['id']}], 'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message['id'],))
    s.coordination.teams.bind_message(room, source, c.team['id'], owner)
    c.goal = s.coordination.teams.from_message({**scope, **proof(c, 0), 'team_id': c.team['id'],
        'message_id': source['id'], 'source_version': source['version'],
        'source_digest': digest(json.loads(source['body'])['body_text']), 'idempotency_key': key()}, actor)['goal']
    c.ctx = {**scope, 'goal_id': c.goal['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1}
    c.work = s.coordination.work_create({**c.ctx, **proof(c, 0), 'objective': '核对文件',
        'acceptance': '给出原始读取回执', 'target_project_id': 'proj', 'assignee_grant_id': actor.grant_id,
        'assignee_dot_id': c.dots[1]['id'], 'idempotency_key': key()}, actor)['work_item']
    c.work = s.coordination.work_claim({**c.ctx, **proof(c, 1), 'work_item_id': c.work['id'],
        'expected_version': c.work['version'], 'idempotency_key': key()}, actor)['work_item']
    c.receipt = await s.runtime.invoke('collaboration_work_execute', {**c.ctx, **proof(c, 1),
        'work_item_id': c.work['id'], 'attempt': c.work['attempt'], 'fencing_token': c.work['fencing_token'],
        'tool': 'read', 'arguments': {'path': 'README.md'}, 'idempotency_key': key()}, actor)
    assert c.receipt['pending'] and c.receipt['state'] == 'queued'
    assert s.store.one('SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE name=?', (OPERATION_EVENT,))['n'] == 0
    return c


def proof(c, index):
    return {'dot_id': c.dots[index]['id'],
        **{k: c.sessions[index][k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')},
        'expected_inbox_revision': c.s.dot_mailbox.current_revision(c.dots[index]['id'])}


def operation(c):
    return c.s.store.one('SELECT * FROM operations WHERE id=?', (c.receipt['operation_id'],))


def entries(c, index):
    return c.s.store.all("""SELECT * FROM dot_mailbox_entries WHERE dot_id=? AND kind='result_ready'
        AND json_extract(reference,'$.operation_id')=? ORDER BY sequence""",
        (c.dots[index]['id'], c.receipt['operation_id']))


def page(c, index):
    return c.s.dot_mailbox.read({**c.scope, 'dot_id': c.dots[index]['id'],
        'binding_generation': c.bindings[index]['generation']}, c.actor)['items']


@pytest.mark.asyncio
async def test_multidot_team_completion_uses_original_operation_exact_slot_and_deduplicates(collab):
    c = await running_team_operation(collab)
    c.s.runtime.complete(operation(c), {'ok': True, 'data': {'content': 'fixture result: private file contents'}})
    target = entries(c, 1)
    assert len(target) == 1 and not entries(c, 0)
    assert target[0]['object_id'] == 'operation:' + c.receipt['operation_id'] + ':succeeded'
    assert target[0]['object_version'] == operation(c)['output_seq']
    assert 'private file contents' not in target[0]['reference']
    assert any(item['reference'].get('operation_id') == c.receipt['operation_id'] for item in page(c, 1))
    assert not any(item['reference'].get('operation_id') == c.receipt['operation_id'] for item in page(c, 0))
    original_revision = c.s.dot_mailbox.current_revision(c.dots[1]['id'])
    c.s.runtime.complete(operation(c), {'ok': True, 'data': {'content': 'duplicate result'}})
    with c.s.store.transaction():
        c.s.events.operation_completed(c.receipt['operation_id'])
    assert len(entries(c, 1)) == 1
    assert c.s.dot_mailbox.current_revision(c.dots[1]['id']) == original_revision
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_subscriptions')['n'] == 2


@pytest.mark.asyncio
async def test_multidot_team_completion_state_change_with_same_output_sequence_wakes_again(collab):
    c = await running_team_operation(collab)
    c.s.runtime.complete(operation(c), {'ok': False, 'error': {'code': 'INTERRUPTED', 'message': 'original read needs review'}})
    before = operation(c)
    assert before['state'] == 'needs_review'
    assert len(entries(c, 1)) == 1
    c.s.runtime.complete(before, {'ok': True, 'data': {'content': 'recovered original read'}})
    after = operation(c)
    assert after['state'] == 'succeeded' and after['output_seq'] == before['output_seq']
    receipts = entries(c, 1)
    assert len(receipts) == 2 and not entries(c, 0)
    assert {json.loads(row['reference'])['reason_code'] for row in receipts} == {'needs_review', 'succeeded'}
    assert len({row['object_id'] for row in receipts}) == 2


@pytest.mark.asyncio
async def test_multidot_team_completion_rollback_has_no_mailbox_or_native_wake(collab, monkeypatch):
    c = await running_team_operation(collab)
    wakes = []
    monkeypatch.setattr(c.s, 'loops', SimpleNamespace(wake_events=lambda: wakes.append('committed')), raising=False)
    old_revision = c.s.dot_mailbox.current_revision(c.dots[1]['id'])
    old_outbox = c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (MAILBOX_EVENT,))['n']
    with pytest.raises(RuntimeError):
        with c.s.store.transaction():
            c.s.store.execute("UPDATE operations SET state='succeeded' WHERE id=?", (c.receipt['operation_id'],))
            c.s.events.operation_completed(c.receipt['operation_id'])
            assert not wakes
            raise RuntimeError('fixture rollback')
    assert operation(c)['state'] == 'queued'
    assert not entries(c, 1) and not entries(c, 0) and not wakes
    assert c.s.dot_mailbox.current_revision(c.dots[1]['id']) == old_revision
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (MAILBOX_EVENT,))['n'] == old_outbox
    c.s.runtime.complete(operation(c), {'ok': True, 'data': {'content': 'committed original receipt'}})
    assert len(entries(c, 1)) == 1 and wakes == ['committed']


@pytest.mark.asyncio
async def test_multidot_team_completion_after_pause_preserves_receipt_without_starting_work(collab):
    c = await running_team_operation(collab)
    c.s.store.execute("UPDATE operations SET attempts=1,state='running',accepted_at=? WHERE id=?",
                      (c.clock[0], c.receipt['operation_id']))
    c.s.coordination.teams.control({**c.scope, 'team_id': c.team['id'], 'expected_version': c.team['version'],
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    assert operation(c)['state'] == 'cancelling'
    c.s.runtime.complete(operation(c), {'ok': True, 'data': {'content': 'original read settled after pause'}})
    assert len(entries(c, 1)) == 1 and not entries(c, 0)
    assert not any(item['kind'] == 'work_ready' for item in page(c, 1))
    assert c.s.store.one('SELECT state FROM coordination_work WHERE id=?', (c.work['id'],))['state'] == 'blocked'
    assert c.s.store.one('SELECT COUNT(*) AS n FROM coordination_operations')['n'] == 1
    with pytest.raises(DevError) as exc:
        c.s.coordination.work_claim({**c.ctx, **proof(c, 1), 'work_item_id': c.work['id'],
            'expected_version': c.work['version'], 'idempotency_key': key()}, c.actor)
    assert exc.value.code == 'TEAM_INACTIVE'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation_state', ['running', 'unknown'])
async def test_multidot_team_context_loss_recovers_original_work_and_operations_read_only(collab, operation_state):
    c = await running_team_operation(collab)
    c.s.store.execute('UPDATE operations SET state=?,attempts=1 WHERE id=?', (operation_state, c.receipt['operation_id']))
    original_session = c.sessions[1]
    c.clock[0] = original_session['lease_until'] + 1
    assert c.work['lease_until'] > c.clock[0]
    binding = c.s.dot_mailbox.binding(c.dots[1]['id'])
    sub = c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (binding['subscription_id'],))
    ticket = c.s.dot_consumers.issue_ticket(c.room, c.dots[1], sub, binding['generation'], key())
    replacement = c.s.dot_consumers.begin({**c.scope, 'dot_id': c.dots[1]['id'],
        'binding_generation': binding['generation'], 'wake_ticket': ticket,
        'consumer_run_id': key(), 'idempotency_key': key()}, c.actor)
    assert replacement['consumer_fence'] > original_session['consumer_fence']
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    inbox = c.s.invoke('collaboration_query', {**c.scope, 'action': 'dot_inbox', 'dot_id': c.dots[1]['id']}, c.actor)
    recovered = c.s.invoke(inbox['recovery_request']['tool'], inbox['recovery_request']['arguments'], c.actor)
    matching = [item for item in recovered['items'] if item['work_item_id'] == c.work['id']]
    assert len(matching) == 1
    item = matching[0]
    assert item['attempt'] == c.work['attempt'] and item['fencing_token'] == c.work['fencing_token']
    assert item['lease_until'] == c.work['lease_until'] and item['execution_state'] == 'running'
    assert item['lease_state'] == 'active' and item['assignee_dot_id'] == c.dots[1]['id']
    assert item['wait_reason'] == ('original_operation_uncertain' if operation_state == 'unknown' else 'original_operation_pending')
    evidence = c.s.invoke(item['operations_request']['tool'], item['operations_request']['arguments'], c.actor)
    assert [(op['operation_id'], op['state']) for op in evidence['items']] == [(c.receipt['operation_id'], operation_state)]
    details = c.s.invoke(item['read_request']['tool'], item['read_request']['arguments'], c.actor)
    assert details['goal']['id'] == c.goal['id']
    other = c.s.invoke('collaboration_query', {**c.scope, 'action': 'dot_recovery', 'dot_id': c.dots[0]['id']}, c.actor)
    assert all(row['work_item_id'] != c.work['id'] for row in other['items'])
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert c.s.store.one('SELECT COUNT(*) AS n FROM coordination_operations')['n'] == 1
    assert c.s.store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == 2
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_subscriptions')['n'] == 2


@pytest.mark.asyncio
async def test_multidot_team_recovery_paginates_filters_completion_and_rechecks_visibility(collab):
    c = await running_team_operation(collab)
    extra = []
    for number in range(2):
        extra.append(c.s.coordination.work_create({**c.ctx, **proof(c, 0),
            'objective': '恢复分页工作 ' + str(number), 'acceptance': '复核原目标',
            'target_project_id': 'proj', 'assignee_grant_id': c.actor.grant_id,
            'assignee_dot_id': c.dots[1]['id'], 'idempotency_key': key()}, c.actor)['work_item'])
    lease = c.s.coordination.work_claim({**c.ctx, **proof(c, 1), 'work_item_id': extra[0]['id'],
        'expected_version': extra[0]['version'], 'idempotency_key': key()}, c.actor)['work_item']
    c.s.coordination.result({**c.ctx, **proof(c, 1), 'work_item_id': extra[0]['id'],
        'attempt': lease['attempt'], 'fencing_token': lease['fencing_token'],
        'outcome': 'succeeded', 'summary': '已核对只读工作', 'operation_ids': [], 'idempotency_key': key()}, c.actor)
    raw = {**c.scope, 'action': 'dot_recovery', 'dot_id': c.dots[1]['id'], 'limit': 1, 'include_completed': True}
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    first = c.s.invoke('collaboration_query', raw, c.actor)
    assert first['next_cursor']
    with pytest.raises(DevError) as exc:
        c.s.invoke('collaboration_query', {**raw, 'dot_id': c.dots[0]['id'], 'cursor': first['next_cursor']}, c.actor)
    assert exc.value.code == 'INVALID_CURSOR'
    with pytest.raises(DevError):
        c.s.invoke('collaboration_query', {**raw, 'include_completed': False, 'cursor': first['next_cursor']}, c.actor)
    items, page = [], first
    while True:
        items.extend(page['items'])
        if not page['next_page_request']:
            break
        request = page['next_page_request']
        page = c.s.invoke(request['tool'], request['arguments'], c.actor)
    assert {item['work_item_id'] for item in items} == {c.work['id'], extra[0]['id'], extra[1]['id']}
    assert len(items) == 3
    active = c.s.invoke('collaboration_query', {**raw, 'limit': 50, 'include_completed': False}, c.actor)
    assert {item['work_item_id'] for item in active['items']} == {c.work['id'], extra[1]['id']}
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    # Current source visibility remains authoritative even for an old cursor.
    source = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=(SELECT current_message_id FROM team_goals WHERE goal_id=?)', (c.goal['id'],))
    body = json.loads(source['body'])
    body['provenance_project_ids'] = ['otherproj']
    c.s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), source['id']))
    hidden = c.s.invoke('collaboration_query', {**raw, 'limit': 50}, c.actor)
    assert hidden['items'] == []
