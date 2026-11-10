"""Ordering regressions across durable Weixin input, reply and delivery records."""

import json

from hub.collaboration import weixin_delivery_order as order
from hub.collaboration.common import canonical
from hub.collaboration.weixin_delivery import text_items
from shared.util import DevError
from tests.collaboration_support import collab as collab
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
    assert [row['id'] for row in channel.delivery.candidates(c.clock[0])] == [part_for(store, later)['id']]
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
    assert [row['id'] for row in channel.delivery.candidates(c.clock[0])] == [part['id']]
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
