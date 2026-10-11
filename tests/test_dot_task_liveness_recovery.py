"""Isolated recovery contracts; no native Dot host or Weixin handset is claimed."""
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import MESSAGE_EVENT, digest
from hub.collaboration.schema import migrate
from hub.collaboration.task_liveness import TaskLiveness, freshness
from shared.public_collaboration import tool_definitions, resolve
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, inbox
from tests.test_collaboration_dots import call, lease as dot_lease
from tests.test_collaboration_dot_relay import interim, update, subscribe


def task(c, *, claimed=True):
    source, _ = owner_message(c, '检查原任务并回报证据')
    submitted = call(c, inbox(c)['items'][0]['task_request'])
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (submitted['delegation_id'],))
    item = c.s.coordination.work(c.s.coordination.object(c.room, link['goal_id']), link['work_item_id'])
    if claimed:
        item = c.s.coordination.work_claim({**c.scope, 'goal_id': item['goal_id'], 'work_item_id': item['id'],
            'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    return source['message'], item


def read_operation(c, item, **extra):
    return c.s.coordination.admit({**dot_lease(c, item), 'tool': 'read', 'arguments': {'path': 'README.md'}, **extra},
                                 c.actor)['operation_id']


def settle(c, identifier, ok=True):
    op = c.s.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    c.s.runtime.complete(op, {'ok': ok, 'data': {'fixture_only': True} if ok else None,
                              'error': None if ok else {'code': 'NOT_FOUND', 'message': 'synthetic missing read'}})


def recover(c, **extra):
    return c.s.invoke('collaboration_query', {'action': 'dot_recovery', **c.scope, 'dot_id': c.slot['id'], **extra}, c.actor)


def receipt(c, request_key, kind='message', actor=None):
    return c.s.invoke('collaboration_query', {'action': 'dot_receipt', **c.scope, 'dot_id': c.slot['id'],
                                            'request_key': request_key, 'kind': kind}, actor or c.actor)


def test_migration_is_additive_and_never_fabricates_old_evidence(collab):
    s, *_ = collab
    with s.store.transaction():
        migrate(s.store.db)
        migrate(s.store.db)
    c = duplex(collab)
    _, item = task(c)
    op = read_operation(c, item)
    row = c.s.store.one('SELECT * FROM operations WHERE id=?', (op,))
    assert row['last_output_at'] is None and row['last_operation_verified_at'] is None
    proof = recover(c)['items'][0]
    assert proof['operation_freshness'] == 'unknown'
    assert proof['observed_at'] is None
    assert proof['model_online'] == 'unknown'
    assert recover(c)['scope_boundary']['host_outbox_verified'] is False


@pytest.mark.parametrize('value,now,expected', [(None,100,'unknown'), (101,100,'unknown'), (90,100,'fresh'), (55,100,'stale')])
def test_freshness_has_no_clock_or_read_side_effect(value, now, expected):
    assert freshness(value, now, 45) == expected


def test_handled_chat_work_is_readable_without_claim_or_heartbeat(collab):
    c = duplex(collab)
    message, item = task(c, claimed=False)
    assert not inbox(c)['items']
    baseline = c.s.store.one('SELECT total_changes() AS n')['n']
    page = recover(c)
    assert len(page['items']) == 1
    state = page['items'][0]
    assert state['work_item_id'] == item['id'] and state['message_id'] == message['id']
    assert state['wait_reason'] == 'awaiting_consumer'
    assert page['read_only'] and not page['grants_authority']
    assert page['scope_boundary']['ordinary_tools_room_scoped'] is False
    assert call(c, page['items'][0]['operations_request'])['items'] == []
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == baseline
    assert c.s.coordination.work(c.s.coordination.object(c.room, item['goal_id']), item['id'])['attempt'] == 0


def test_recovery_pages_and_rejects_cross_dot_cursor(collab):
    c = duplex(collab)
    expected = {task(c, claimed=False)[1]['id'] for _ in range(4)}
    page = recover(c, limit=1)
    cursor = page['next_cursor']
    seen = set()
    while True:
        seen.update(row['work_item_id'] for row in page['items'])
        if not page['next_page_request']:
            break
        page = call(c, page['next_page_request'])
    assert seen == expected
    second = duplex(collab)
    with pytest.raises(DevError, match='游标'):
        recover(second, limit=1, cursor=cursor)


def test_repeated_message_does_not_refresh_activity_and_receipt_is_read_only(collab):
    c = duplex(collab)
    message, _ = owner_message(c)
    args = {**inbox(c)['items'][0]['interim_reply_arguments'], 'body_text': '阶段回复'}
    first = c.s.dot_chat.message(args, c.actor)
    old = c.s.store.one('SELECT * FROM collaboration_dot_messages WHERE message_id=?', (message['message']['id'],))
    c.clock[0] += 123
    repeated = c.s.dot_chat.message(args, c.actor)
    assert repeated['message']['id'] == first['message']['id']
    current = c.s.store.one('SELECT * FROM collaboration_dot_messages WHERE message_id=?', (message['message']['id'],))
    assert current['last_activity_at'] == old['last_activity_at']
    baseline = c.s.store.one('SELECT total_changes() AS n')['n']
    proof = receipt(c, args['idempotency_key'])
    assert proof['committed'] and proof['committed_version'] == 1
    assert proof['request_digest'] == digest(__import__('shared.collaboration_contracts', fromlist=['DotChatMessage']).DotChatMessage.model_validate(args).model_dump())
    assert not receipt(c, key())['committed']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == baseline


def test_update_receipt_keeps_original_version_after_newer_update(collab):
    c = duplex(collab)
    owner_message(c)
    first = interim(c)
    args = first['update_arguments']
    second = update(c, args, '第二版')
    update(c, second['update_arguments'], '最终版', complete=True)
    proof = receipt(c, args['idempotency_key'], kind='update')
    assert proof['committed_version'] == 2 and proof['current_version'] == 3
    assert proof['committed']
    with pytest.raises(DevError):
        receipt(c, args['idempotency_key'], kind='update', actor=replace(c.actor, grant_id='second'))


def test_receipt_rollback_keeps_reply_and_source_pending(collab):
    c = duplex(collab)
    message, _ = owner_message(c)
    args = {**inbox(c)['items'][0]['reply_arguments'], 'body_text': '不能只保存半边'}
    c.s.store.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON dot_delivery_receipts BEGIN SELECT RAISE(ABORT,'receipt fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match='receipt fault'):
        c.s.dot_chat.message(args, c.actor)
    assert len(inbox(c)['items']) == 1
    assert not receipt(c, args['idempotency_key'])['committed']
    assert c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == 1
    assert c.s.store.one('SELECT state FROM collaboration_dot_messages WHERE message_id=?', (message['message']['id'],))['state'] == 'pending'


def test_heartbeat_replay_and_reads_do_not_freshen_observations(collab):
    c = duplex(collab)
    _, item = task(c)
    request = dot_lease(c, item)
    c.s.coordination.heartbeat(request, c.actor)
    proof = recover(c)['items'][0]
    c.clock[0] += 91
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    c.s.coordination.heartbeat(request, c.actor)
    next_proof = recover(c)['items'][0]
    assert next_proof['last_consumer_heartbeat_at'] == proof['last_consumer_heartbeat_at']
    assert next_proof['consumer_freshness'] == 'stale'
    assert next_proof['last_operation_verified_at'] is None
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_progress_is_distinct_from_pure_consumer_heartbeat(collab):
    c = duplex(collab)
    _, item = task(c)
    c.s.coordination.progress({**dot_lease(c, item), 'summary': '<步骤>只是一条实际进展'}, c.actor)
    first = recover(c)['items'][0]
    c.clock[0] += 20
    c.s.coordination.heartbeat(dot_lease(c, item), c.actor)
    after = recover(c)['items'][0]
    assert after['last_progress_at'] == first['last_progress_at']
    assert after['last_consumer_heartbeat_at'] > after['last_progress_at']
    assert after['last_progress_summary'] == '<步骤>只是一条实际进展'


def test_original_operation_pages_include_uncertainty_without_outputs(collab):
    c = duplex(collab)
    _, item = task(c)
    ops = [read_operation(c, item) for _ in range(3)]
    c.s.store.execute("UPDATE operations SET state='needs_review' WHERE id=?", (ops[1],))
    page = recover(c)
    assert page['items'][0]['uncertain_count'] == 1
    assert page['items'][0]['wait_reason'] == 'original_operation_uncertain'
    request = page['items'][0]['operations_request']
    request['arguments']['limit'] = 1
    seen = []
    while request:
        result = call(c, request)
        seen.extend(result['items'])
        request = result['next_page_request']
    assert {row['operation_id'] for row in seen} == set(ops)
    assert all('output' not in row and 'arguments' not in row for row in seen)
    with pytest.raises(DevError) as err:
        c.s.coordination.result({**dot_lease(c, item), 'outcome': 'failed', 'summary': '不确定不是失败', 'operation_ids': ops}, c.actor)
    assert err.value.code == 'WORK_OPERATION_PENDING'


def test_synthetic_completion_does_not_claim_agent_contact(collab):
    c = duplex(collab)
    _, item = task(c)
    op = read_operation(c, item)
    settle(c, op)
    state = recover(c)['items'][0]
    assert state['last_operation_verified_at'] is None
    assert state['wait_reason'] == 'awaiting_result_publication'
    assert state['last_result_recorded_at'] is not None


def test_only_current_authenticated_journal_packets_write_evidence(collab):
    c = duplex(collab)
    _, item = task(c)
    op = read_operation(c, item)
    runtime = c.s.runtime
    peer = SimpleNamespace(journal_id='journal-test', last_seen=time.time(), unusable=False)
    runtime.connections['dev'] = peer
    c.s.store.execute("UPDATE operations SET journal_id='journal-test' WHERE id=?", (op,))
    assert runtime.connection_authorized('dev', peer)
    runtime._receive_operation_event('dev', SimpleNamespace(journal_id='journal-test'), {'type': 'status', 'id': op, 'status': 'running'})
    assert c.s.store.one('SELECT last_operation_verified_at AS t FROM operations WHERE id=?', (op,))['t'] is None
    runtime._receive_operation_event('dev', peer, {'type': 'status', 'id': op, 'status': 'running'})
    verified = c.s.store.one('SELECT last_operation_verified_at AS t FROM operations WHERE id=?', (op,))['t']
    assert verified is not None
    other = read_operation(c, item)
    c.s.store.execute("UPDATE operations SET journal_id='other-journal' WHERE id=?", (other,))
    runtime._receive_operation_event('dev', peer, {'type': 'status', 'id': other, 'status': 'running'})
    row = c.s.store.one('SELECT * FROM operations WHERE id=?', (other,))
    assert row['state'] == 'needs_review' and row['last_operation_verified_at'] is None


def test_archive_expired_result_is_atomic_and_does_not_restore_lease(collab):
    c = duplex(collab)
    _, item = task(c)
    op = read_operation(c, item)
    settle(c, op)
    c.clock[0] += 301
    c.s.coordination.reconcile()
    old = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (item['id'],))
    assert old['reason_code'] == 'LEASE_EXPIRED'
    args = {**dot_lease(c, item), 'outcome': 'succeeded', 'summary': '原回执已经完成', 'operation_ids': [op],
            'expected_version': old['version']}
    result = c.s.invoke('collaboration_work', {'action': 'archive_result', **args}, c.actor)['work_item']
    assert result['state'] == 'succeeded' and result['lease_until'] is None
    assert result['fencing_token'] == old['fencing_token'] > item['fencing_token']
    assert result['result']['late_archive'] and not result['result']['acceptance_verified_by_owner']
    assert c.s.invoke('collaboration_work', {'action': 'archive_result', **args}, c.actor)['work_item']['id'] == item['id']
    assert len(c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='delegation_result'")) == 1
    assert not recover(c)['items']
    assert len(recover(c, include_completed=True)['items']) == 1


@pytest.mark.parametrize('tamper', ['version','fence','new_attempt','assignee','cancelled','paused_goal','uncertain'])
def test_expired_archive_respects_original_authority_and_fences(collab, tamper):
    c = duplex(collab)
    _, item = task(c)
    op = read_operation(c, item)
    settle(c, op)
    c.clock[0] += 301
    c.s.coordination.reconcile()
    current = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (item['id'],))
    args = {**dot_lease(c, item), 'outcome': 'succeeded', 'summary': '不允许伪造', 'operation_ids': [op], 'expected_version': current['version']}
    if tamper == 'version': args['expected_version'] -= 1
    if tamper == 'fence': args['fencing_token'] += 1
    if tamper == 'new_attempt': c.s.store.execute('UPDATE coordination_work SET attempt=attempt+1 WHERE id=?',(item['id'],))
    if tamper == 'assignee': c.s.store.execute("UPDATE coordination_work SET assignee_grant_id='second' WHERE id=?",(item['id'],))
    if tamper == 'cancelled': c.s.store.execute("UPDATE coordination_work SET state='cancelled' WHERE id=?",(item['id'],))
    if tamper == 'paused_goal': c.s.store.execute("UPDATE coordination_goals SET state='paused' WHERE id=?",(item['goal_id'],))
    if tamper == 'uncertain': c.s.store.execute("UPDATE operations SET state='needs_review' WHERE id=?",(op,))
    with pytest.raises(DevError):
        c.s.coordination.archive_result(args, c.actor)
    assert not c.s.store.one('SELECT result FROM coordination_work WHERE id=?',(item['id'],))['result']


def test_failed_exploratory_read_recovered_but_old_result_stays_strict(collab):
    c = duplex(collab)
    _, item = task(c)
    failed = read_operation(c, item, operation_role='exploratory')
    settle(c, failed, False)
    replacement = read_operation(c, item, supersedes_operation_id=failed)
    settle(c, replacement)
    args = {**dot_lease(c, item), 'outcome': 'succeeded', 'summary': '读取路径已经纠正', 'operation_ids': [failed, replacement]}
    with pytest.raises(DevError) as err:
        c.s.coordination.result(args, c.actor)
    assert err.value.code == 'WORK_OPERATION_FAILED'
    result = c.s.invoke('collaboration_work', {'action': 'result_v2', **args}, c.actor)['work_item']['result']
    assert result['execution_health'] == 'recovered_failures'
    assert result['acceptance_status'] == 'unverified'
    assert result['operation_ids'] == [failed, replacement]
    assert c.s.store.one('SELECT state FROM operations WHERE id=?',(failed,))['state'] == 'failed'
    assert len(result['operation_dispositions']) == 2


@pytest.mark.parametrize('wrong', ['required','uncertain','other_work'])
def test_only_same_attempt_failed_exploration_can_be_superseded(collab, wrong):
    c = duplex(collab)
    _, item = task(c)
    failed = read_operation(c, item, operation_role='required' if wrong=='required' else 'exploratory')
    settle(c, failed, False)
    if wrong == 'uncertain': c.s.store.execute("UPDATE operations SET state='needs_review' WHERE id=?", (failed,))
    if wrong == 'other_work': _, item = task(c)
    with pytest.raises(DevError) as err:
        read_operation(c, item, supersedes_operation_id=failed)
    assert err.value.code == 'WORK_SUPERSESSION_DENIED'


def test_exploratory_role_cannot_hide_side_effects(collab):
    c = duplex(collab)
    _, item = task(c)
    with pytest.raises(DevError) as err:
        c.s.coordination.admit({**dot_lease(c,item), 'operation_role': 'exploratory', 'tool': 'write',
            'arguments': {'path': 'README.md', 'content': 'not executed', 'expected_sha256': 'new'}}, c.actor)
    assert err.value.code == 'WORK_EXPLORATION_READ_ONLY'
    assert c.s.store.one('SELECT COUNT(*) AS n FROM operations')['n'] == 0


def test_legacy_execute_digest_is_not_changed_by_new_default_metadata(collab):
    c = duplex(collab)
    _, item = task(c)
    args = {**dot_lease(c,item), 'tool': 'read','arguments': {'path': 'README.md'}}
    first = c.s.coordination.admit(args, c.actor)
    again = c.s.coordination.admit({**args,'operation_role':'required','supersedes_operation_id':''}, c.actor)
    assert first['operation_id'] == again['operation_id']
    row = c.s.store.one('SELECT request_digest FROM coordination_operations WHERE operation_id=?',(first['operation_id'],))
    assert row['request_digest'] == digest(args)


@pytest.mark.asyncio
async def test_work_wake_survives_restart_and_stops_at_bounded_budget(collab):
    c = duplex(collab)
    source, item = task(c, claimed=False)
    await subscribe(c)
    c.clock[0] += 301
    assert c.s.task_liveness.recover() == 1
    events = c.s.store.all("SELECT data FROM mcp_event_outbox WHERE object_id LIKE 'dot-work-recover:%'")
    assert len(events) == 1 and json.loads(events[0]['data'])['message_id'] == source['id']
    assert not c.s.store.all('SELECT * FROM operations')
    assert c.s.store.one('SELECT attempt FROM coordination_work WHERE id=?', (item['id'],))['attempt'] == 0
    for delay in (121, 601):
        c.clock[0] += delay
        c.s.task_liveness = TaskLiveness(c.s)
        assert c.s.task_liveness.recover() == 1
    c.clock[0] += 601
    c.s.task_liveness = TaskLiveness(c.s)
    assert c.s.task_liveness.recover() == 0
    assert c.s.store.one('SELECT wake_count FROM dot_work_recovery_wakes')['wake_count'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('stopped',['no_subscription','revoked','paused','budget'])
async def test_wake_never_bypasses_missing_consent_or_scope(collab, stopped):
    c = duplex(collab)
    _, item = task(c, claimed=False)
    if stopped != 'no_subscription': await subscribe(c)
    if stopped == 'revoked': c.s.store.execute("UPDATE grants SET revoked=1 WHERE id=?",(c.actor.grant_id,))
    if stopped == 'paused': c.s.store.execute("UPDATE coordination_goals SET state='paused' WHERE id=?",(item['goal_id'],))
    c.clock[0] += 301
    if stopped == 'budget':
        for i in range(30):
            c.s.emit(c.room, MESSAGE_EVENT, 'budget-'+str(i), 1, {'conversation_id': c.slot['conversation_id'],
                'room_id': c.room['id'], 'message_id': 'fixture', 'message_version': 1,
                'recipient_slot_id': c.slot['id'], 'thread_root_id': 'fixture'}, grant_id=c.actor.grant_id)
    assert c.s.task_liveness.recover() == 0
    assert not c.s.store.all('SELECT * FROM operations')


def test_public_schema_keeps_flat_discovery_and_strict_server_actions():
    tools = {item['name']:item for item in tool_definitions()}
    query = tools['collaboration_query']['inputSchema']
    assert {'dot_recovery','dot_receipt','work_evidence'} <= set(query['properties']['action']['enum'])
    assert not any(key in query for key in ('oneOf','anyOf'))
    with pytest.raises(DevError):
        resolve('collaboration_query', {'action':'dot_recovery','project':'proj','dot_id':'dot','command':'not allowed'})
