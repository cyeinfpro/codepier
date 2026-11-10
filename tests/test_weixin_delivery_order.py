"""Ordering regressions across durable Weixin input, reply and delivery records."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
from threading import Barrier

from hub.collaboration import weixin_delivery_order as order
from hub.collaboration.common import canonical
from hub.collaboration.weixin_delivery import text_items
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet
from tests.test_weixin_delivery_parity import part_for, post_reply


def enqueue(c, source, body):
    message = post_reply(c, source, body)['message']
    return c.s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (message['id'],))


def observe_unknown(c, channel, delivery):
    part = part_for(c.s.store, delivery)
    claim = channel.delivery.claim(part['id'])
    channel.delivery.mark_sending(claim, text_items('原回复'))
    channel.delivery.finish(claim, error=DevError('TRANSPORT_RESULT_LOST', 'timeout', 503))
    return part['id']


def test_edited_input_shares_reply_chain_and_new_question_in_thread_can_send(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    source = ingest(c, channel, account, packet(2, '原问题', create_time_ms=100))['message_ids'][0]
    first = enqueue(c, source, '原回复')
    original_part_id = observe_unknown(c, channel, first)
    edit = ingest(c, channel, account, packet(2, '问题修订', create_time_ms=100, update_time_ms=200))['message_ids'][0]
    edited = enqueue(c, edit, '针对修订的回复')
    question = ingest(c, channel, account, packet(3, '一个新的追问'))['message_ids'][0]
    # Same topic is contextual. It is independent of the provider edit chain.
    store.execute('UPDATE collaboration_messages SET thread_root_id=? WHERE id=?', (source, question))
    later = enqueue(c, question, '追问回复')

    assert first['ordering_key'] == edited['ordering_key'] == 'reply:' + source
    assert edited['source_message_id'] == edit and later['ordering_key'] == 'reply:' + question
    assert edited['ordering_schema_version'] == edited['ordering_checked_version'] == 2
    assert order.dependency_state(store, edited['id']) == 'waiting'
    assert [row['id'] for row in order.blocking_predecessors(store, edited['id'])] == [first['id']]
    assert [row['id'] for row in order.blocked_successors(store, first['id'])] == [edited['id']]
    assert order.dependency_state(store, later['id']) == 'ready'
    assert [candidate['id'] for candidate in channel.delivery.candidates(c.clock[0])] == [part_for(store, later)['id']]
    assert store.one('SELECT state FROM weixin_parts WHERE id=?', (original_part_id,))['state'] == 'unknown'


def test_explicit_success_dependency_survives_proven_relogin_and_stops_on_failure(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    source = ingest(c, channel, account, packet(2, '问题一'))['message_ids'][0]
    first = enqueue(c, source, '原回复')
    original_part_id = observe_unknown(c, channel, first)
    other = ingest(c, channel, account, packet(3, '问题二'))['message_ids'][0]
    second = enqueue(c, other, '依赖原回复')
    binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (second['binding_id'],))
    message = store.one('SELECT * FROM collaboration_messages WHERE id=?', (second['message_id'],))
    input_message = store.one('SELECT * FROM collaboration_messages WHERE id=?', (other,))

    new_binding = {**binding, 'generation': binding['generation'] + 1}
    ordering, evidence, predecessor = order.reply_ordering(store, message, input_message, new_binding,
        predecessor_id=first['id'], dependency_mode='requires_success')
    without_recovery = json.loads(evidence)
    assert without_recovery.pop('recovery_predecessor_id') == first['id']
    store.execute('''UPDATE weixin_deliveries SET generation=?,ordering_key=?,depends_on_delivery_id=?,ordering_evidence=?
        WHERE id=?''', (new_binding['generation'], ordering, predecessor, canonical(without_recovery), second['id']))
    assert order.dependency_state(store, second['id']) == 'ready'
    store.execute('UPDATE weixin_deliveries SET ordering_evidence=? WHERE id=?', (evidence, second['id']))
    assert order.dependency_state(store, second['id']) == 'waiting'
    assert order.successors(store, first['id'])[0]['id'] == second['id']
    assert not channel.delivery.candidates(c.clock[0])

    store.execute("UPDATE weixin_parts SET state='rejected' WHERE id=?", (original_part_id,))
    assert order.dependency_state(store, second['id']) == 'failed'
    assert order.blocking_predecessors(store, second['id'])[0]['decision'] == 'failed'
    part = part_for(store, second)
    store.execute("""UPDATE weixin_parts SET state='waiting_context',attempts=1,retry_rejected=1,
        reason_code='context_missing' WHERE id=?""", (part['id'],))
    store.execute('DELETE FROM weixin_contexts')
    assert [candidate['id'] for candidate in channel.delivery.candidates(c.clock[0])] == [part['id']]
    assert channel.delivery.claim(part['id']) is None
    assert part_for(store, second)['reason_code'] == 'dependency_failed'


def test_legacy_backfill_uses_durable_cursor_and_defers_inflight_snapshots(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    source = ingest(c, channel, account, packet(2, '原输入'))['message_ids'][0]
    first = enqueue(c, source, '原回复')
    other = ingest(c, channel, account, packet(3, '独立追问'))['message_ids'][0]
    second = enqueue(c, other, '独立回复')
    first_part = part_for(store, first)
    store.execute("""UPDATE weixin_parts SET state='preparing',lease_until=? WHERE id=?""",
                  (c.clock[0] + 50, first_part['id']))
    store.execute("""UPDATE weixin_deliveries SET ordering_schema_version=1,ordering_checked_version=0,
        ordering_key='work:old',ordering_evidence=''""")
    store.execute("UPDATE weixin_deliveries SET source_message_id='missing' WHERE id=?", (second['id'],))
    order.backfill(store, c.clock[0])

    job = store.one('SELECT * FROM weixin_order_migrations WHERE name=?', (order.MIGRATION_NAME,))
    assert job['last_delivery_id'] == max(first['id'], second['id'])
    assert job['checked_count'] == job['unresolved_count'] == 1 and job['finished_at'] is None
    held = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (first['id'],))
    assert (held['ordering_schema_version'], held['ordering_revision']) == (1, 1)
    unresolved = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (second['id'],))
    assert (unresolved['ordering_schema_version'], unresolved['ordering_checked_version'],
            unresolved['ordering_revision']) == (1, 2, 2)
    assert unresolved['ordering_key'] == 'work:old'
    assert order.ordering_reason(unresolved['ordering_key'], unresolved['ordering_evidence']) == 'legacy_source_unverified'

    # A later worker reopens the persisted scan and only advances ordering.
    order.backfill(store, c.clock[0])
    c.clock[0] += 51
    channel.delivery.reconcile()
    version = store.one('SELECT version FROM weixin_deliveries WHERE id=?', (first['id'],))['version']
    order.backfill(store, c.clock[0])
    order.backfill(store, c.clock[0])
    job = store.one('SELECT * FROM weixin_order_migrations WHERE name=?', (order.MIGRATION_NAME,))
    recovered = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (first['id'],))
    assert job['checked_count'] == 2 and job['changed_count'] == 1 and job['finished_at'] == c.clock[0]
    assert recovered['ordering_key'] == 'reply:' + source and recovered['ordering_revision'] == 2
    assert recovered['version'] == version
    assert part_for(store, first)['client_id'] == first_part['client_id']


def test_backfill_preserves_existing_typed_success_requirement(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    source = ingest(c, channel, account, packet(2, '原问题'))['message_ids'][0]
    first = enqueue(c, source, '原回复')
    original_part_id = observe_unknown(c, channel, first)
    followup = ingest(c, channel, account, packet(3, '独立问题'))['message_ids'][0]
    second = enqueue(c, followup, '需要原回复成功')
    store.execute("""UPDATE weixin_deliveries SET ordering_evidence='',ordering_checked_version=0,
        canonical_source_id=?,depends_on_delivery_id=?,dependency_mode='requires_success' WHERE id=?""",
        (followup, first['id'], second['id']))

    assert order.dependency_state(store, second['id']) == 'waiting'
    order.backfill(store, c.clock[0])
    row = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (second['id'],))
    assert json.loads(row['ordering_evidence'])['dependency_mode'] == 'requires_success'
    store.execute("UPDATE weixin_parts SET state='rejected' WHERE id=?", (original_part_id,))
    assert order.dependency_state(store, second['id']) == 'failed'


def test_ordering_rollback_preserves_existing_relationships(collab, monkeypatch):
    from hub.collaboration import weixin_delivery

    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    first_source = ingest(c, channel, account, packet(2, '旧问题'))['message_ids'][0]
    first = enqueue(c, first_source, '旧回复')
    c.s.config = replace(c.s.config, weixin_delivery_order_v2=False)
    source = ingest(c, channel, account, packet(3, '回退期间的问题'))['message_ids'][0]
    floor_calls = []
    with monkeypatch.context() as patch:
        patch.setattr(weixin_delivery, 'raise_floor', floor_calls.append)
        later = enqueue(c, source, '回退期间的回复')
    assert not floor_calls
    assert later['ordering_evidence'] == ''
    assert later['ordering_schema_version'] == 1 and later['ordering_checked_version'] == 0
    store.execute('UPDATE weixin_parts SET next_attempt=?', (c.clock[0] + 60,))
    asyncio.run(channel.delivery.tick())
    assert store.one('SELECT ordering_schema_version FROM weixin_deliveries WHERE id=?',
                     (later['id'],))['ordering_schema_version'] == 1
    assert store.one('SELECT ordering_evidence FROM weixin_deliveries WHERE id=?',
                     (first['id'],))['ordering_evidence'] == first['ordering_evidence']

    c.s.config = replace(c.s.config, weixin_delivery_order_v2=True)
    asyncio.run(channel.delivery.tick())
    assert store.one('SELECT ordering_key FROM weixin_deliveries WHERE id=?',
                     (later['id'],))['ordering_key'] == 'reply:' + source


def test_backfill_missing_legacy_source_keeps_original_blocking_scope(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    original_source = ingest(c, channel, account, packet(2, '历史原问题'))['message_ids'][0]
    original = enqueue(c, original_source, '历史未核对回复')
    observe_unknown(c, channel, original)
    grouped_source = ingest(c, channel, account, packet(3, '同一历史工作'))['message_ids'][0]
    grouped = enqueue(c, grouped_source, '同组回复')
    new_source = ingest(c, channel, account, packet(4, '独立新问题'))['message_ids'][0]
    independent = enqueue(c, new_source, '独立回复')
    store.execute("""UPDATE weixin_deliveries SET source_message_id='missing',ordering_key='work:old',
        ordering_evidence='',ordering_schema_version=1,ordering_checked_version=0 WHERE id=?""",
        (original['id'],))
    store.execute("""UPDATE weixin_deliveries SET ordering_key='work:old',ordering_evidence='',
        ordering_schema_version=1 WHERE id=?""", (grouped['id'],))

    def assert_scope():
        assert order.dependency_state(store, grouped['id']) == 'waiting'
        assert order.dependency_state(store, independent['id']) == 'ready'
        assert [row['id'] for row in channel.delivery.candidates(c.clock[0])] == [part_for(store, independent)['id']]

    assert_scope()
    order.backfill(store, c.clock[0])
    retained = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (original['id'],))
    assert (retained['ordering_key'], retained['ordering_schema_version'], retained['ordering_checked_version']) == (
        'work:old', 1, 2)
    assert json.loads(retained['ordering_evidence']) == {'reason_code': 'legacy_source_unverified'}
    assert_scope()

    # Older deployed code could already have marked this v1 record checked.
    old_evidence = canonical({'unresolved': True, 'canonical_input_root_id': new_source})
    store.execute('UPDATE weixin_deliveries SET ordering_evidence=? WHERE id=?', (old_evidence, original['id']))
    assert_scope()
    # Empty legacy keys still retain their recipient-level conservative ordering.
    store.execute("UPDATE weixin_deliveries SET ordering_key='' WHERE id=?", (original['id'],))
    assert order.dependency_state(store, independent['id']) == 'waiting'


def test_corrupt_history_does_not_stall_backfill_or_sending(collab, monkeypatch, caplog):
    c, channel, account, _, _ = fixture_channel(collab)
    store = c.s.store
    old_source = ingest(c, channel, account, packet(2, '历史输入'))['message_ids'][0]
    old = enqueue(c, old_source, '历史回复')
    source = ingest(c, channel, account, packet(3, '新输入'))['message_ids'][0]
    new = enqueue(c, source, '新回复')
    store.execute("""UPDATE weixin_deliveries SET ordering_key='work:old',ordering_evidence='',
        ordering_schema_version=1,ordering_checked_version=0""")
    store.execute('UPDATE collaboration_messages SET body=? WHERE id=?',
                  (canonical({'external_origin': []}), old_source))
    store.execute("UPDATE weixin_parts SET next_attempt=? WHERE delivery_id=?", (c.clock[0] + 60, old['id']))

    order.backfill(store, c.clock[0])
    job = store.one('SELECT * FROM weixin_order_migrations WHERE name=?', (order.MIGRATION_NAME,))
    assert (job['checked_count'], job['changed_count'], job['unresolved_count']) == (2, 1, 1)
    retained = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (old['id'],))
    assert order.ordering_reason(retained['ordering_key'], retained['ordering_evidence']) == (
        'legacy_ordering_inspection_failed')
    assert store.one('SELECT ordering_key FROM weixin_deliveries WHERE id=?', (new['id'],))['ordering_key'] == (
        'reply:' + source)

    def failed_batch(*args):
        raise sqlite3.OperationalError('private exception context')

    class Provider:
        async def send(self, *args):
            return {'provider_state': 'accepted'}

    monkeypatch.setattr(order, 'backfill', failed_batch)
    channel.client_factory = lambda *_: Provider()
    asyncio.run(channel.delivery.tick())
    assert part_for(store, new)['state'] == 'accepted'
    assert part_for(store, old)['state'] == 'queued'
    assert 'Weixin ordering backfill failed (OperationalError)' in caplog.text
    assert 'private exception context' not in caplog.text


def test_backfill_claim_cancel_and_enqueue_keep_independent_fences(collab):
    c, channel, account, _, scope = fixture_channel(collab)
    store = c.s.store
    original_source = ingest(c, channel, account, packet(2, '领取来源'))['message_ids'][0]
    original = enqueue(c, original_source, '领取回复')
    cancelled_source = ingest(c, channel, account, packet(3, '取消来源'))['message_ids'][0]
    cancelled = enqueue(c, cancelled_source, '取消回复')
    new_source = ingest(c, channel, account, packet(4, '新输入'))['message_ids'][0]
    store.execute("""UPDATE weixin_deliveries SET ordering_key='work:old',ordering_evidence='',
        ordering_schema_version=1,ordering_checked_version=0""")
    original_part = part_for(store, original)
    claim = channel.delivery.claim(original_part['id'])
    assert claim is not None
    rendezvous = Barrier(3)

    def run(operation, *args):
        rendezvous.wait(timeout=10)
        return operation(*args)

    cancel = {**scope, 'delivery_id': cancelled['id'], 'action': 'cancel_unsent',
              'expected_version': cancelled['version'], 'idempotency_key': key()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        migrate = pool.submit(run, order.backfill, store, c.clock[0])
        stop = pool.submit(run, channel.delivery.control, cancel, c.owner)
        write = pool.submit(run, enqueue, c, new_source, '新回复')
        migrate.result(timeout=10)
        assert stop.result(timeout=10)['affected_count'] == 1
        new = write.result(timeout=10)

    held = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (original['id'],))
    assert held['ordering_checked_version'] == 0
    assert channel.delivery.mark_sending(claim, text_items('领取回复'))
    channel.delivery.finish(claim, {'provider_state': 'accepted'})
    assert part_for(store, original)['state'] == 'accepted'
    assert part_for(store, cancelled)['state'] == 'cancelled'
    assert new['ordering_key'] == 'reply:' + new_source and new['ordering_checked_version'] == 2
    order.backfill(store, c.clock[0])
    order.backfill(store, c.clock[0])
    checked = store.one('SELECT * FROM weixin_deliveries WHERE id=?', (original['id'],))
    assert checked['ordering_key'] == 'reply:' + original_source and checked['ordering_checked_version'] == 2
    assert part_for(store, original)['client_id'] == original_part['client_id']
