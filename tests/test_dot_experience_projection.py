"""Evidence projections and request-local batching, using isolated synthetic data."""
import json
from dataclasses import replace

import pytest

from hub.collaboration import experience_projection as ep
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, send_dot
from tests.test_collaboration_dot_relay import interim, subscribe
from tests.test_dot_task_liveness_recovery import task, read_operation, settle


@pytest.mark.parametrize('handling,expected', [
    ({'state': 'waiting_input'}, 'waiting_input'),
    ({'state': 'waiting_authorization'}, 'waiting_authorization'),
    ({'state': 'covered', 'reply_id': 'final'}, 'replied'),
    ({'state': 'no_response_required'}, 'no_response_required'),
    ({'state': 'legacy_handled'}, 'legacy_handled'),
    ({'state': 'covered', 'source_current': False}, 'needs_attention'),
])
def test_handling_is_independent_of_transport(handling, expected):
    recipient = {'dot_id': 'd', 'state': 'handled' if handling['state'] == 'legacy_handled' else 'pending'}
    value = ep.message({'id': 'm'}, recipient, handling=handling)
    assert value['primary_state'] == expected
    assert value['execution']['state'] == 'not_applicable'
    assert value['human_read_verified'] is False
    assert value['observed_at'] is None
    assert value['read_only'] and not value['grants_authority']


@pytest.mark.parametrize('work,expected', [
    ({'execution_state': 'succeeded', 'acceptance_status': 'unverified'}, 'work_completed_acceptance_unverified'),
    ({'execution_state': 'succeeded', 'acceptance_status': 'verified'}, 'completed'),
    ({'execution_state': 'failed'}, 'needs_attention'),
    ({'execution_state': 'running', 'wait_reason': 'original_operation_uncertain'}, 'original_operation_uncertain'),
    ({'execution_state': 'running', 'wait_reason': 'awaiting_result_publication'}, 'awaiting_result_publication'),
    ({'execution_state': 'running', 'consumer_freshness': 'fresh'}, 'processing'),
    ({'execution_state': 'running', 'consumer_freshness': 'stale'}, 'original_operation_uncertain'),
])
def test_reply_does_not_override_work_evidence(work, expected):
    value = ep.message({'id': 'm'}, {'dot_id': 'd', 'state': 'handled'},
                       reply={'id': 'r', 'body': {}, 'version': 1},
                       work={'work_item_id': 'w', **work})
    assert value['primary_state'] == expected
    assert value['publication']['state'] == 'committed'
    assert value['next_action']['mutates'] is False


def test_channel_unknown_is_not_human_read_or_work_failure():
    value = ep.message({'id': 'm'}, {'dot_id': 'd', 'state': 'handled'},
                       reply={'id': 'r', 'body': {}, 'version': 1},
                       channels=[{'id': 'receipt', 'channel': 'weixin', 'state': 'unknown'},
                                 {'id': 'other', 'channel': 'telegram', 'state': 'accepted'}])
    assert value['primary_state'] == 'result_saved_channel_uncertain'
    assert value['next_action'] == {'kind': 'inspect_original_receipt', 'target_ref': 'receipt', 'mutates': False}
    assert not any(d['read_verified'] for d in value['channel_deliveries'])


@pytest.mark.parametrize('slot,expected', [
    ({'state': 'invited'}, 'not_registered'),
    ({'state': 'registered'}, 'waiting_subscription'),
    ({'state': 'registered', 'status': 'subscription_verified'}, 'ready'),
    ({'state': 'registered', 'status': 'subscription_verified', 'task_blocked_reason': 'scope'}, 'chat_only'),
    ({'state': 'registered', 'status': 'authorization_unavailable'}, 'authorization_unavailable'),
    ({'state': 'registered', 'status': 'room_paused'}, 'paused'),
    ({'state': 'revoked', 'status': 'subscription_verified'}, 'revoked'),
])
def test_connection_uses_evidence_and_only_one_navigation(slot, expected):
    value = ep.connection({'id': 'd', 'join_code': 'CPD-secret', **slot})
    assert value['primary_state'] == expected
    assert value['observed_at'] is None
    assert 'CPD-secret' not in json.dumps(value)
    assert not value['next_action'] or value['next_action']['mutates'] is False


def test_diagnostics_are_a_strict_allowlist():
    data = ep.diagnostic([{
        'message_id': 'message-private', 'dot_id': 'dot-private', 'primary_state': 'processing',
        'body_text': '/private/project password=secret', 'url': 'https://invalid/?token=secret',
        'reason_code': 'secret', 'next_action': {'kind': 'open_original_message', 'target_ref': 'secret'}}], [])
    text = json.dumps(data)
    assert all(secret not in text for secret in ['private', 'secret', 'https:', 'password'])
    assert data['raw_body_included'] is False
    assert data['entries'][0]['observed_at'] is None


def statements(store, callback):
    queries = []
    with store.lock:
        store.db.set_trace_callback(queries.append)
        try:
            result = callback()
        finally:
            store.db.set_trace_callback(None)
    return result, sum(q.lstrip().upper().startswith('SELECT') for q in queries)


@pytest.mark.parametrize('count', [1, 24, 100])
@pytest.mark.parametrize('recipient_count', [1, 3])
def test_batch_receipts_match_legacy_and_bound_queries(collab, count, recipient_count):
    c = duplex(collab)
    recipients = [c] + [duplex(collab) for _ in range(recipient_count - 1)]
    originals = []
    for index in range(count):
        if index and index % 30 == 0:
            c.clock[0] += 61
        value, _ = owner_message(c, 'synthetic ' + str(index),
            mentions=[{'slot_id': recipient.slot['id']} for recipient in recipients])
        originals.append(c.s.object('collaboration_messages', c.room, value['message']['id']))
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    old, old_sql = statements(c.s.store, lambda: {row['id']: c.s.dot_relay._legacy_receipts(row) for row in originals})
    batch, new_sql = statements(c.s.store, lambda: c.s.dot_relay.receipts_many(originals, principal=c.owner))
    stripped = {identifier: [{k: v for k, v in receipt.items() if k != 'experience'} for receipt in receipts]
                for identifier, receipts in batch.items()}
    assert stripped == old
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert new_sql <= 10
    if count > 1:
        assert new_sql < old_sql / 2
    print({'messages': count, 'recipients': recipient_count,
           'baseline_sql': old_sql, 'full_projection_batch_sql': new_sql})


@pytest.mark.asyncio
async def test_batch_rechecks_revocation_no_cached_authority(collab):
    c = duplex(collab)
    await subscribe(c)
    result, _ = owner_message(c)
    row = c.s.object('collaboration_messages', c.room, result['message']['id'])
    first = c.s.dot_relay.receipts_many([row], principal=c.owner)
    assert first[row['id']][0]['state'] == 'saved'
    c.s.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (c.actor.grant_id,))
    second = c.s.dot_relay.receipts_many([row], principal=c.owner)
    assert second[row['id']][0]['state'] == 'waiting_connection'


def test_sync_and_diagnostics_do_not_write_or_refresh_evidence(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    send_dot(c, 'synthetic answer', source['message']['id'])
    args = {**c.scope, 'conversation_id': c.slot['conversation_id']}
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    first = c.s.dot_relay.sync(args, c.owner, source['message']['id'])
    c.clock[0] += 10
    second = c.s.dot_relay.sync(args, c.owner, source['message']['id'])
    a = first['updates'][0]['relay_receipts'][0]['experience']
    b = second['updates'][0]['relay_receipts'][0]['experience']
    assert a == b
    assert a['primary_state'] == 'replied'
    assert first['connection_readiness'][c.slot['id']]['read_only']
    assert 'synthetic answer' not in json.dumps(first['diagnostic_summary'])
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_terminal_original_operations_require_publication_not_reexecution(collab):
    c = duplex(collab)
    source, work = task(c)
    operation = read_operation(c, work)
    settle(c, operation)
    row = c.s.object('collaboration_messages', c.room, source['id'])
    value = c.s.dot_relay.receipts_many([row], principal=c.owner)[row['id']][0]['experience']
    assert value['primary_state'] == 'awaiting_result_publication'
    assert value['next_action']['target_ref'] == work['id']
    assert len(c.s.store.all('SELECT id FROM operations')) == 1


def test_message_receipt_has_authoritative_absence_only_for_current_scope(collab):
    c = duplex(collab)
    args = {**c.scope, 'conversation_id': c.slot['conversation_id'], 'client_message_id': key()}
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    absent = c.s.dot_relay.message_receipt(args, c.owner)
    assert absent['authoritative'] and absent['state'] == 'not_committed'
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    source, raw = owner_message(c, client_message_id=args['client_message_id'])
    committed = c.s.dot_relay.message_receipt(args, c.owner)
    assert committed['state'] == 'committed' and committed['message']['id'] == source['message']['id']
    with pytest.raises(DevError):
        c.s.dot_relay.message_receipt({**args, 'conversation_id': 'foreign'}, c.owner)
    with pytest.raises(DevError):
        c.s.dot_relay.message_receipt(args, replace(c.owner, user_id='other'))


def test_stale_progress_does_not_look_like_live_thinking(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    interim(c)
    c.clock[0] += 100
    row = c.s.object('collaboration_messages', c.room, source['message']['id'])
    value = c.s.dot_relay.receipts_many([row], principal=c.owner)[row['id']][0]['experience']
    assert value['primary_state'] != 'processing'
    assert value['reason_code'] in {'consumer_progress_stale', 'connection_unavailable'}


@pytest.mark.parametrize('mode', ['legacy', 'projection_off', 'shadow', 'mismatch'])
def test_projection_rollout_can_fall_back_without_changing_facts(collab, monkeypatch, mode):
    c = duplex(collab)
    value, _ = owner_message(c)
    row = c.s.object('collaboration_messages', c.room, value['message']['id'])
    c.s.config = replace(c.s.config, relay_batch_enabled=mode != 'legacy',
        experience_projection_enabled=mode != 'projection_off', relay_batch_shadow=mode in {'shadow', 'mismatch'})
    if mode == 'mismatch':
        original = c.s.dot_relay._legacy_receipts
        monkeypatch.setattr(c.s.dot_relay, '_legacy_receipts', lambda row: [
            {**r, 'state': 'legacy_synthetic_difference'} for r in original(row)])
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    result = c.s.dot_relay.receipts_many([row], principal=c.owner)[row['id']][0]
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    if mode in {'legacy', 'projection_off', 'mismatch'}:
        assert 'experience' not in result
    else:
        assert 'experience' in result
    if mode == 'mismatch':
        assert result['projection_comparison'] == 'mismatch_fallback'


@pytest.mark.slow
@pytest.mark.parametrize('count', [1, 24, 100])
def test_fixed_receipt_performance_samples(collab, count):
    """1000 paired read-only samples, synthetic SQLite; no host/network SLA."""
    import os
    import platform
    import statistics
    import time
    from pathlib import Path

    c = duplex(collab)
    originals = []
    for index in range(count):
        if index and index % 30 == 0:
            c.clock[0] += 61
        created, _ = owner_message(c, 'benchmark fixture ' + str(index))
        originals.append(c.s.object('collaboration_messages', c.room, created['message']['id']))
    functions = {
        'legacy': lambda: [c.s.dot_relay._legacy_receipts(row) for row in originals],
        # Same authorized source set and legacy response fields, then separately
        # measure the additional handling/projection evidence of the new UI.
        'batch_legacy_fields': lambda: c.s.dot_relay.receipts_many(originals),
        'batch_full_projection': lambda: c.s.dot_relay.receipts_many(originals, principal=c.owner),
    }
    samples = {name: [] for name in functions}
    with c.s.store.transaction(immediate=False):
        before = c.s.store.one('SELECT total_changes() AS n')['n']
        for _ in range(5):
            for function in functions.values():
                function()
        for _ in range(1000):
            for name, function in functions.items():
                start = time.perf_counter()
                function()
                samples[name].append((time.perf_counter() - start) * 1000)
        assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    report = {'scenario': 'receipt_batch', 'messages': count, 'recipients': 1,
              'samples_per_path': 1000, 'warmup_per_path': 5,
              'python': platform.python_version(), 'platform': platform.platform(),
              'network': 'none; isolated synthetic SQLite; saved inputs without active subscriptions',
              'external_host_verified': False, 'error_rate': 0,
              'response_bytes': {name: len(json.dumps(function(), ensure_ascii=False).encode())
                                 for name, function in functions.items()},
              'comparison': 'legacy and batch_legacy_fields have equal facts; full_projection adds evidence',
              'timing_ms': {name: {'median': statistics.median(values),
                                  'p95': sorted(values)[949], 'max': max(values)}
                            for name, values in samples.items()}}
    print(json.dumps(report, sort_keys=True))
    destination = os.environ.get('CODEPIER_EXPERIENCE_BENCHMARK_REPORT')
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / ('receipts-' + str(count) + '.json')).write_text(json.dumps(report, indent=2))


def test_pending_scope_is_current_stable_and_checked_in_submission_transaction(collab):
    c = duplex(collab)
    args = {**c.scope, 'conversation_id': c.slot['conversation_id'], 'client_message_id': key()}
    receipt = c.s.dot_relay.message_receipt(args, c.owner)
    revision = receipt['pending_scope_revision']
    assert len(revision) == 64
    c.clock[0] += 1
    assert c.s.dot_relay.message_receipt(args, c.owner)['pending_scope_revision'] == revision
    payload = {**args, 'room_id': c.room['id'], 'body_text': 'original saved intent',
               'idempotency_key': key(), 'mentions': [{'slot_id': c.slot['id']}],
               'pending_scope_revision': revision}
    c.s.store.execute('UPDATE projects SET root=? WHERE id=?', ('/synthetic/new-mapping', c.room['project_id']))
    changed = c.s.dot_relay.message_receipt(args, c.owner)
    assert changed['pending_scope_revision'] != revision
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    with pytest.raises(DevError) as failure:
        c.s.chatroom.create(payload, c.owner)
    assert failure.value.code == 'PENDING_SCOPE_CHANGED'
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert changed['state'] == 'not_committed'


def test_pending_scope_field_does_not_change_original_idempotency_fingerprint(collab):
    c = duplex(collab)
    args = {**c.scope, 'conversation_id': c.slot['conversation_id'], 'client_message_id': key()}
    revision = c.s.dot_relay.message_receipt(args, c.owner)['pending_scope_revision']
    payload = {**args, 'room_id': c.room['id'], 'body_text': 'original intention', 'idempotency_key': key()}
    first = c.s.chatroom.create({**payload, 'pending_scope_revision': revision}, c.owner)
    assert c.s.chatroom.create(payload, c.owner)['message']['id'] == first['message']['id']


def test_unscoped_or_connector_single_message_never_returns_ui_projection(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    row = c.s.object('collaboration_messages', c.room, source['message']['id'])
    assert all('experience' not in r for r in c.s.chatroom.view(row)['relay_receipts'])
    assert all('experience' not in r for r in c.s.chatroom.views([row], principal=c.actor)[0]['relay_receipts'])
    assert 'experience' in c.s.chatroom.views([row], principal=c.owner)[0]['relay_receipts'][0]
    reply = send_dot(c, 'private related answer', row['id'])['message']
    related = c.s.object('collaboration_messages', c.room, reply['id'])
    body = {**json.loads(related['body']), 'provenance_project_ids': ['otherproj']}
    c.s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (json.dumps(body), reply['id']))
    assert not c.s.chatroom.message_visible(c.actor, c.s.object('collaboration_messages', c.room, reply['id']))
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    result = c.s.read({**c.scope, 'conversation_id': c.slot['conversation_id'],
        'kind': 'message_status', 'id': row['id']}, c.actor)
    assert all('experience' not in r for r in result['message']['relay_receipts'])
    assert reply['id'] not in json.dumps(result)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_generic_authorization_wait_does_not_imply_scope_expansion():
    value = ep.message({'id': 'source'}, {'dot_id': 'dot', 'state': 'read'},
        reply={'id': 'explanation', 'body': {'dot_reply_complete': False}},
        handling={'state': 'waiting_authorization', 'reason_code': 'authorization_required', 'reply_id': 'explanation'})
    assert value['next_action'] == {'kind': 'open_original_message', 'target_ref': 'explanation', 'mutates': False}
