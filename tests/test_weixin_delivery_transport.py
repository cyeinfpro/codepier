"""Persisted contact admission, late evidence and precise owner controls."""

import asyncio

import pytest

from hub.collaboration.telemetry import CollaborationTelemetry
from hub.collaboration.weixin_delivery import WeixinDelivery, text_items
from hub.collaboration import weixin_delivery_transport as transport
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import fixture_channel, packet, reply
from tests.test_room_weixin_native import setup_channel


def clone(store, table, row, **fields):
    value = {**row, **fields}
    columns = ','.join(value)
    values = ','.join('?' for _ in value)
    store.execute(f'INSERT INTO {table} ({columns}) VALUES ({values})', tuple(value.values()))


def test_four_admission_batches_serve_64_contacts_and_resume_persisted_order(collab, monkeypatch):
    c, channel, account_id, _, _ = fixture_channel(collab)
    _, _, first = reply(c, channel, account_id)
    store = c.s.store
    account = store.one('SELECT * FROM weixin_accounts WHERE id=?', (first['account_id'],)) if 'account_id' in first else store.one(
        'SELECT a.* FROM weixin_accounts a JOIN weixin_bindings b ON b.account_id=a.id WHERE b.id=?', (first['binding_id'],))
    binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (first['binding_id'],))
    context = store.one('SELECT * FROM weixin_contexts WHERE account_id=?', (account['id'],))
    first_part = store.one('SELECT * FROM weixin_parts WHERE delivery_id=?', (first['id'],))
    part_ids = {}

    def seed_contact(contact, *, ready):
        account_key, binding_key, peer = (f'{name}-{contact}' for name in ('account', 'binding', 'peer'))
        clone(store, 'weixin_accounts', account, id=account_key, bot_id=f'bot-{contact}')
        clone(store, 'weixin_bindings', binding, id=binding_key, account_id=account_key,
              peer_id=peer, conversation_id=f'conversation-{contact}')
        if ready:
            clone(store, 'weixin_contexts', context, account_id=account_key, peer_id=peer)
        for backlog in range(5 if ready else 1):
            identifier = f'delivery-{contact}-{backlog}'
            clone(store, 'weixin_deliveries', first, id=identifier, binding_id=binding_key,
                  message_id=identifier, peer_id=peer, created=c.clock[0] + contact, ordering_key=f'contact:{contact}')
            part_id = f'part-{contact}-{backlog}'
            clone(store, 'weixin_parts', first_part, id=part_id, delivery_id=identifier, client_id=part_id,
                  state='queued' if ready else 'waiting_context')
            part_ids[part_id] = (contact, backlog)

    with store.transaction():
        store.execute("UPDATE weixin_parts SET state='accepted' WHERE id=?", (first_part['id'],))
        for contact in range(-16, 64):
            seed_contact(contact, ready=contact >= 0)

    def validation(delivery):
        bound = store.one('SELECT * FROM weixin_bindings WHERE id=?', (delivery['binding_id'],))
        linked = store.one('SELECT * FROM weixin_accounts WHERE id=?', (bound['account_id'],))
        return bound, linked, c.actor, None

    seen = set()
    delivery = channel.delivery
    for batch in range(4):
        if batch == 2:
            delivery = WeixinDelivery(channel)
        monkeypatch.setattr(delivery, 'validate', validation)
        candidates = delivery.candidates(c.clock[0])
        assert len(candidates) == 16
        for candidate in candidates:
            contact, backlog = part_ids[candidate['id']]
            assert contact not in seen and backlog == 0
            seen.add(contact)
            claim = delivery.claim(candidate['id'])
            delivery.mark_sending(claim, text_items('original'))
            delivery.finish(claim, {'provider_state': 'accepted'})
    assert len(seen) == 64
    assert store.one('SELECT COUNT(*) AS count FROM weixin_peer_admission')['count'] == 64
    assert [part_ids[row['id']][1] for row in delivery.candidates(c.clock[0])] == [1] * 16
    clone(store, 'weixin_contexts', context, account_id='account--16', peer_id='peer--16')
    assert delivery.candidates(c.clock[0])[0]['id'] == 'part--16-0'


def test_paused_disabled_cooled_and_waiting_media_lanes_resume_when_ready(collab):
    c, channel, account_id, _, _ = fixture_channel(collab)
    _, _, row = reply(c, channel, account_id)
    store, sender = c.s.store, channel.delivery
    binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (row['binding_id'],))
    part = store.one('SELECT id FROM weixin_parts WHERE delivery_id=?', (row['id'],))

    def candidate_ids():
        return [candidate['id'] for candidate in sender.candidates(c.clock[0])]

    assert candidate_ids() == [part['id']]
    store.execute("UPDATE conversation_rooms SET state='paused' WHERE id=?", (binding['conversation_id'],))
    assert not candidate_ids()
    store.execute("UPDATE conversation_rooms SET state='active' WHERE id=?", (binding['conversation_id'],))
    store.execute("""INSERT INTO collaboration_channel_settings
        (space_id,owner_user_id,channel,enabled,version,updated) VALUES (?,?,'weixin',0,1,?)""",
        (binding['space_id'], binding['owner_user_id'], c.clock[0]))
    assert not candidate_ids()
    store.execute("UPDATE collaboration_channel_settings SET enabled=1 WHERE channel='weixin'")
    assert candidate_ids() == [part['id']]
    transport.cooldown(store, binding['account_id'], c.clock[0] + 30)
    assert not candidate_ids()
    c.clock[0] += 30
    assert candidate_ids() == [part['id']]

    with store.transaction():
        store.execute("UPDATE weixin_parts SET kind='attachment',state='waiting_media' WHERE id=?", (part['id'],))
        store.execute("""INSERT INTO channel_attachments
            (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,
             binding_id,generation,peer_id,kind,name,mime,state,private_source,expires_at,created,updated)
            VALUES ('attachment',?,?,?,?,?,?,0,'outbound',?,?,?,'file','sample','application/octet-stream',
                    'pending','',?,?,?)""",
            (binding['space_id'], binding['owner_user_id'], binding['room_id'], binding['conversation_id'],
             binding['project_id'], row['message_id'], binding['id'], binding['generation'], binding['peer_id'],
             c.clock[0] + 300, c.clock[0], c.clock[0]))
        store.execute("INSERT INTO channel_attachment_transfers(attachment_id,channel,part_id) VALUES ('attachment','weixin',?)",
                      (part['id'],))
    assert not candidate_ids()
    store.execute("UPDATE channel_attachments SET state='ready' WHERE id='attachment'")
    assert candidate_ids() == [part['id']]


@pytest.mark.asyncio
async def test_room_control_previews_confirm_and_stop_exact_parts(collab, monkeypatch):
    telemetry = CollaborationTelemetry()
    monkeypatch.setattr(transport, 'collaboration_telemetry', telemetry)
    (service, owner, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab)
    source = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']),
        packet(2, '微信原始输入', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    entry = next(item for item in service.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == source)
    posted = service.room_runtime.reply({**identity, **credentials, **entry['reply_arguments'],
        'body_text': '正文' * 1400, 'idempotency_key': key()}, worker)['message']
    store, delivery = service.store, service.weixin.delivery
    row = store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (posted['id'],))
    parts = store.all('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (row['id'],))
    assert len(parts) == 3
    with store.transaction():
        store.execute("UPDATE weixin_parts SET state='unknown',attempts=1 WHERE delivery_id=? AND ordinal<2", (row['id'],))
        delivery.refresh(row['id'])
    conversation_id = identity['conversation_id']

    def preview(action, part):
        return delivery.control_room(conversation_id, row['id'], {'action': action, 'part_ids': [part['id']]},
                                     owner, preview=True)

    def request(action, part, plan):
        return {'action': action, 'part_ids': [part['id']], 'expected_version': plan['delivery_version'],
                'expected_ordering_revision': plan['ordering_revision'], 'preview_digest': plan['preview_digest'],
                'confirmation_note': '本人核对选中分项', 'idempotency_key': key()}

    stop = request('cancel_unsent', parts[2], preview('cancel_unsent', parts[2]))
    confirmation = preview('confirm_received', parts[0])
    assert confirmation['affected_count'] == 1
    assert [part['part_id'] for part in confirmation['affected_parts']] == [parts[0]['id']]
    confirm = request('confirm_received', parts[0], confirmation)
    result = delivery.control_room(conversation_id, row['id'], confirm, owner)
    assert result == delivery.control_room(conversation_id, row['id'], confirm, owner)
    assert result['provider_delivery_verified'] is False
    audit = store.one("SELECT space_id,owner_user_id,detail FROM audit WHERE action='weixin.delivery_confirm_received'")
    assert (audit['space_id'], audit['owner_user_id']) == (owner.space_id, owner.user_id)
    assert '本人核对选中分项' in audit['detail']
    with pytest.raises(DevError) as exc:
        delivery.control_room(conversation_id, row['id'], stop, owner)
    assert exc.value.code == 'STALE_VERSION'
    stop = request('cancel_unsent', parts[2], preview('cancel_unsent', parts[2]))
    assert delivery.control_room(conversation_id, row['id'], stop, owner)['affected_count'] == 1
    states = store.all('SELECT state,provider_receipt FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (row['id'],))
    assert [part['state'] for part in states] == ['owner_confirmed', 'unknown', 'cancelled']
    assert all(part['provider_receipt'] is None for part in states)
    assert {(item['stage'], item['count']) for item in telemetry.snapshot()['series']} == {
        ('owner_confirmed', 1), ('cancelled', 1)}


@pytest.mark.asyncio
async def test_disconnect_after_dispatch_keeps_late_receipt_and_stops_unsent_tail(collab):
    (service, owner, worker, clock, identity, credentials), flow, _, action = await setup_channel(collab)
    source = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']),
        packet(2, '原输入', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    entry = next(item for item in service.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == source)
    posted = service.room_runtime.reply({**identity, **credentials, **entry['reply_arguments'],
        'body_text': '正文' * 650, 'idempotency_key': key()}, worker)['message']
    sender, store = service.weixin.delivery, service.store
    row = store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (posted['id'],))
    parts = store.all('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (row['id'],))
    assert len(parts) == 2
    claim = sender.claim(parts[0]['id'])
    sender.mark_sending(claim, text_items('正文' * 500))
    await action(service.room_channels.read(flow['id'], owner)['flow'], 'disconnect')

    def states():
        return [(part['state'], part['attempts']) for part in store.all(
            'SELECT state,attempts FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (row['id'],))]

    assert states() == [('unknown', 1), ('cancelled', 0)]
    sender.finish(claim, {'provider_state': 'accepted'})
    assert states() == [('unknown', 1), ('cancelled', 0)]
    service.weixin.client_factory = lambda *_: pytest.fail('revoked delivery must not dispatch again')
    await sender.tick()
    assert states() == [('accepted', 1), ('cancelled', 0)]


def test_attempted_legacy_part_without_exact_rejection_cannot_be_refrozen(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    store, sender = c.s.store, channel.delivery
    part = store.one('SELECT * FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))
    claim = sender.claim(part['id'])
    sender.mark_sending(claim, text_items('原回复'))
    sender.finish(claim, error=DevError('WEIXIN_RATE_LIMITED', 'rate limited', 429, retry_after_seconds=0))
    frozen = store.one('SELECT * FROM weixin_parts WHERE id=?', (part['id'],))
    # Historical writers could replace the original rejection reason before
    # durable rejection proof existed. Do not infer that it was safe to resend.
    with store.transaction():
        store.execute("""UPDATE weixin_parts SET state='waiting_context',retry_rejected=0,
            reason_code='context_missing' WHERE id=?""", (part['id'],))
        store.execute('DELETE FROM weixin_contexts')
    c.clock[0] = frozen['next_attempt']
    channel.client_factory = lambda *_: pytest.fail('unverified historical attempt must not be sent')
    asyncio.run(sender.tick())
    saved = store.one('SELECT * FROM weixin_parts WHERE id=?', (part['id'],))
    assert (saved['state'], saved['attempts'], saved['reason_code']) == ('unknown', 1, 'prior_send_evidence_missing')
    assert saved['wire_body'] == frozen['wire_body'] and saved['client_id'] == frozen['client_id']


def test_preparation_retries_keep_original_identity_and_stop_at_budget(collab):
    c, channel, account_id, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account_id)
    part = c.s.store.one('SELECT * FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))

    def prepare(*_):
        raise TimeoutError()

    channel.client_factory = prepare
    for attempt in range(1, 6):
        asyncio.run(channel.delivery.tick())
        saved = c.s.store.one('SELECT * FROM weixin_parts WHERE id=?', (part['id'],))
        assert saved['client_id'] == part['client_id'] and saved['attempts'] == 0
        assert saved['preparation_failures'] == attempt
        if attempt < 5:
            assert saved['state'] == 'queued'
            c.clock[0] = saved['next_attempt']
    assert (saved['state'], saved['reason_code']) == ('attention', 'WEIXIN_PREPARATION_BUDGET_EXHAUSTED')


def test_send_telemetry_follows_commits_and_late_evidence(collab, monkeypatch):
    telemetry = CollaborationTelemetry()
    monkeypatch.setattr(transport, 'collaboration_telemetry', telemetry)
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    store = c.s.store
    sender = channel.delivery
    part = store.one('SELECT id FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))
    claim = sender.claim(part['id'])

    with pytest.raises(RuntimeError), store.transaction():
        sender.mark_sending(claim, text_items('原正文'))
        assert telemetry.snapshot()['series'] == []
        raise RuntimeError('rollback')
    assert telemetry.snapshot()['series'] == []
    sender.mark_sending(claim, text_items('原正文'))
    leased = store.one('SELECT lease_until FROM weixin_parts WHERE id=?', (part['id'],))
    c.clock[0] = leased['lease_until'] + 1
    sender.reconcile()
    sender.finish(claim, {'provider_state': 'accepted'})
    assert {item['stage']: item['count'] for item in telemetry.snapshot()['series']} == {
        'attempted': 1, 'unknown': 1}
    sender.reconcile()
    sender.reconcile()
    assert {item['stage']: item['count'] for item in telemetry.snapshot()['series']} == {
        'attempted': 1, 'unknown': 1, 'accepted': 1}


def test_adapter_invocation_distinguishes_transport_unknown(collab, monkeypatch):
    telemetry = CollaborationTelemetry()
    monkeypatch.setattr(transport, 'collaboration_telemetry', telemetry)
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    part = c.s.store.one('SELECT id FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))

    class Provider:
        async def send(self, peer, context, client_id, items):
            raise TimeoutError()

    channel.client_factory = lambda *_: Provider()
    asyncio.run(channel.delivery.send_once(part['id']))
    asyncio.run(channel.delivery.send_once(part['id']))
    assert {(item['stage'], item['reason']): item['count'] for item in telemetry.snapshot()['series']} == {
        ('attempted', 'none'): 1, ('dispatched', 'none'): 1, ('unknown', 'network'): 1}


def test_backlog_metrics_use_bounded_indexed_inventory(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    store = c.s.store
    part = store.one('SELECT * FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))
    with store.transaction():
        for offset in range(transport.BACKLOG_SAMPLE_LIMIT + 2):
            clone(store, 'weixin_parts', part, id=f'unknown-{offset}', client_id=f'unknown-{offset}',
                  ordinal=offset + 1, state='unknown', created=c.clock[0] - offset)
    snapshot = transport.backlog_snapshot(store, c.clock[0])
    unknown = next(item for item in snapshot['states'] if item['state'] == 'unknown')
    assert unknown == {'state': 'unknown', 'count_lower_bound': transport.BACKLOG_SAMPLE_LIMIT + 1,
                       'truncated': True, 'oldest_enqueued_age_seconds': transport.BACKLOG_SAMPLE_LIMIT + 1}
    plan = store.all("""EXPLAIN QUERY PLAN SELECT created FROM weixin_parts WHERE state=?
        ORDER BY created,id LIMIT ?""", ('unknown', transport.BACKLOG_SAMPLE_LIMIT + 1))
    assert any('weixin_parts_state_age' in row['detail'] for row in plan)
