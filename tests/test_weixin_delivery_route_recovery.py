"""Native same-peer QR recovery preserves original reply parts and send identity."""

import json

import pytest

from hub.collaboration.weixin_delivery import text_items
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_weixin_native import setup_channel
from tests.test_weixin_qr_enrollment import confirm


def parts_for(s, delivery):
    return s.store.all('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (delivery['id'],))


def reply(s, worker, clock, identity, credentials, flow):
    source = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '原输入', create_time_ms=int(clock[0] * 1000), context_token='old-context'))['message_ids'][0]
    entry = next(item for item in s.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == source)
    result = s.room_runtime.reply({**identity, **credentials, **entry['reply_arguments'],
        'body_text': '原结果' * 710, 'idempotency_key': key()}, worker)
    return s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (result['message']['id'],))


def send_observation(s, part, *, accepted):
    channel = s.weixin.delivery
    claim = channel.claim(part['id'])
    content = json.loads(s.store.decrypt(part['body']))
    channel.mark_sending(claim, text_items(content['text']))
    if accepted:
        channel.finish(claim, {'provider_state': 'accepted'})
    else:
        channel.finish(claim, error=DevError('RESULT_LOST', 'synthetic timeout', 503))


async def renew(s, owner, clock, flow, provider, action, *, peer='synthetic-peer'):
    clock[0] += 3
    s.weixin.poll_failed(s.weixin.claim_poll(flow['account_id']),
                         DevError('WEIXIN_SESSION_EXPIRED', 'synthetic account expired', 403))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    flow = await action(flow, 'start_qr', confirm_credential_storage=True, confirm_provider_terms=True)
    confirm(provider)
    flow = await action(flow, 'poll_qr')
    flow = await action(flow, 'accept_account', confirm_scanner_id=flow['qr']['scanner_id'],
                        confirm_replace_existing=True)
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(1, flow['binding']['pairing_challenge'], create_time_ms=int(clock[0] * 1000) + 1,
               context_token='renewed-context', from_user_id=peer))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    if peer == 'synthetic-peer':
        flow = await action(flow, 'confirm_owner', confirm_peer_id=peer)
    return flow


def approval(s, delivery, *part_ids):
    row = s.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (delivery['id'],))
    return {'expected_version': row['version'], 'expected_ordering_revision': row['ordering_revision'],
            'part_ids': list(part_ids), 'idempotency_key': key()}


@pytest.mark.asyncio
async def test_same_peer_can_resume_only_selected_unsent_original_part(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await setup_channel(collab)
    delivery = reply(s, worker, clock, identity, credentials, flow)
    first, second, third = parts_for(s, delivery)
    send_observation(s, first, accepted=True)
    flow = await renew(s, owner, clock, flow, provider, action)
    assert flow['binding']['generation'] > delivery['generation']
    projection = s.weixin.delivery.for_messages([delivery['message_id']])[delivery['message_id']][0]
    assert [part['id'] for part in projection['parts'] if part.get('route_recovery_available')] == [second['id']]

    args = approval(s, delivery, second['id'])
    result = s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'], args, owner)
    assert result == {'delivery_id': delivery['id'], 'queued_parts': 1, 'route_recovery': True}
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((peer, context, client_id, items[0]['text_item']['text']))
            return {'provider_state': 'accepted'}

    s.weixin.client_factory = lambda *_: Provider()
    await s.weixin.delivery.tick()
    await s.weixin.delivery.tick()
    content = json.loads(s.store.decrypt(second['body']))
    assert calls == [('synthetic-peer', 'renewed-context', second['client_id'], content['text'])]
    assert s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'], args, owner) == result
    assert [part['state'] for part in parts_for(s, delivery)] == ['accepted', 'accepted', 'cancelled']
    assert s.store.one('SELECT generation FROM weixin_deliveries WHERE id=?', (delivery['id'],))['generation'] == delivery['generation']
    assert len(s.store.all('SELECT id FROM weixin_deliveries')) == 1
    saved = s.store.one('SELECT * FROM weixin_part_route_recoveries WHERE part_id=?', (second['id'],))
    assert saved['channel_generation'] == flow['binding']['generation']
    assert 'renewed-context' not in saved['evidence'] and 'old-context' not in saved['evidence']
    assert third['client_id'] == parts_for(s, delivery)[2]['client_id']


@pytest.mark.asyncio
async def test_unknown_and_its_unsent_tail_cannot_bypass_recovery_order(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await setup_channel(collab)
    delivery = reply(s, worker, clock, identity, credentials, flow)
    first, second, third = parts_for(s, delivery)
    send_observation(s, first, accepted=False)
    await renew(s, owner, clock, flow, provider, action)

    for part_ids in ((first['id'],), (second['id'], third['id'])):
        with pytest.raises(DevError) as failed:
            s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'],
                approval(s, delivery, *part_ids), owner)
        assert failed.value.code == 'WEIXIN_RECOVERY_PART_UNAVAILABLE'
    assert [part['state'] for part in parts_for(s, delivery)] == ['unknown', 'cancelled', 'cancelled']
    assert not s.store.all('SELECT * FROM weixin_part_route_recoveries')
    assert not s.weixin.delivery.candidates(clock[0])


@pytest.mark.asyncio
async def test_different_peer_cannot_recover_old_route(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await setup_channel(collab)
    delivery = reply(s, worker, clock, identity, credentials, flow)
    first, *_ = parts_for(s, delivery)
    flow = await renew(s, owner, clock, flow, provider, action, peer='different-peer')
    assert flow['binding']['state'] == 'pairing'
    with pytest.raises(DevError) as failed:
        s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'], approval(s, delivery, first['id']), owner)
    assert failed.value.code == 'WEIXIN_BINDING_INACTIVE'
    assert not s.store.all('SELECT * FROM weixin_part_route_recoveries')
    assert all(part['state'] == 'cancelled' for part in parts_for(s, delivery))


@pytest.mark.asyncio
async def test_account_replacement_after_approval_does_not_move_original_parts(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await setup_channel(collab)
    delivery = reply(s, worker, clock, identity, credentials, flow)
    first, *_ = parts_for(s, delivery)
    flow = await renew(s, owner, clock, flow, provider, action)
    s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'], approval(s, delivery, first['id']), owner)
    previous_account = flow['account_id']
    await action(flow, 'disconnect')

    flow = s.room_channels.create({'conversation_id': identity['conversation_id'], 'member_id': identity['member_id'],
        'idempotency_key': key()}, owner)['flow']
    flow = await action(flow, 'start_qr', confirm_credential_storage=True, confirm_provider_terms=True)
    confirm(provider)
    provider.response['ilink_bot_id'] = 'other-bot'
    flow = await action(flow, 'poll_qr')
    flow = await action(flow, 'accept_account', confirm_scanner_id=flow['qr']['scanner_id'])
    flow = await action(flow, 'bind', confirm_external_data_ingress=True, allow_replies=True, confirm_until_revoked=True)
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(1, flow['binding']['pairing_challenge'], create_time_ms=int(clock[0] * 1000) + 1,
               to_user_id='other-bot'))
    flow = await action(s.room_channels.read(flow['id'], owner)['flow'], 'confirm_owner', confirm_peer_id='synthetic-peer')
    assert flow['stage'] == 'ready' and flow['account_id'] != previous_account
    assert not s.weixin.delivery.candidates(clock[0])
    with pytest.raises(DevError) as rejected:
        s.weixin.delivery.retry_room(identity['conversation_id'], delivery['id'], approval(s, delivery, first['id']), owner)
    assert rejected.value.code == 'WEIXIN_BINDING_INACTIVE'
    assert s.store.one('SELECT binding_id FROM weixin_deliveries WHERE id=?', (delivery['id'],))['binding_id'] == delivery['binding_id']
    assert len(s.store.all('SELECT * FROM weixin_part_route_recoveries')) == 1
    assert all(part['state'] == 'cancelled' for part in parts_for(s, delivery))
