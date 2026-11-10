"""Persisted contact admission, late evidence and precise owner controls."""

import asyncio

import pytest

from hub.collaboration.weixin_delivery import WeixinDelivery, text_items
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
    with store.transaction():
        store.execute("UPDATE weixin_parts SET state='accepted' WHERE id=?", (first_part['id'],))
        for contact in range(64):
            account_key, binding_key, peer = (f'{name}-{contact}' for name in ('account', 'binding', 'peer'))
            clone(store, 'weixin_accounts', account, id=account_key, bot_id=f'bot-{contact}')
            clone(store, 'weixin_bindings', binding, id=binding_key, account_id=account_key,
                  peer_id=peer, conversation_id=f'conversation-{contact}')
            clone(store, 'weixin_contexts', context, account_id=account_key, peer_id=peer)
            for backlog in range(5):
                identifier = f'delivery-{contact}-{backlog}'
                clone(store, 'weixin_deliveries', first, id=identifier, binding_id=binding_key,
                      message_id=identifier, peer_id=peer, created=c.clock[0] + contact, ordering_key=f'contact:{contact}')
                part_id = f'part-{contact}-{backlog}'
                clone(store, 'weixin_parts', first_part, id=part_id, delivery_id=identifier, client_id=part_id)
                part_ids[part_id] = (contact, backlog)
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


@pytest.mark.asyncio
async def test_room_control_previews_confirm_and_stop_exact_parts(collab):
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
