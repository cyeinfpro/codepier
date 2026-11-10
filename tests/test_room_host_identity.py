"""Stable host identity with real binding/ledger checks and synthetic routes."""

import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical, redact
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import add, create, identity, join, rooms as rooms


def selected_member(service, owner, host_id):
    room = create(service, owner)
    return service.rooms.create_member({
        'conversation_id': room['id'], 'name': '共享 dot', 'host_connection_id': host_id,
        'idempotency_key': key(),
    }, owner)


def invitation(service, created):
    flow = service.store.one('SELECT * FROM room_connect_flows WHERE id=?', (created['flow']['id'],))
    return {'invite_code': service.store.decrypt(flow['invite_secret']), 'idempotency_key': key()}


async def host_probe(service, actor, joined, *, route='shared', signing=b'r'):
    received = []

    async def callback(url, body, headers):
        event = json.loads(body)
        if event.get('type') == 'verification':
            return Reply(200, canonical({'challenge': event['challenge']}).encode())
        received.append(event)
        return Reply(204)

    service.events = EventService(service, callback)
    await service.events.subscribe({
        **joined['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/room/' + route,
                     'secret': 'whsec_' + base64.b64encode(signing * 32).decode()},
    }, actor)
    await service.events.tick()
    event = next(event['data'] for event in received
                 if event['data'].get('member_id') == joined['member_id']
                 and event['data'].get('binding_generation') == joined['binding_generation'])
    joined['wake_ticket'] = event['wake_ticket']
    nonce = service.rooms.probe_read({
        **identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key(),
    }, actor)['probe_nonce']
    return {**identity(joined), 'probe_nonce': nonce, 'idempotency_key': key()}


async def independent_host(service, owner, actor, **route):
    room = create(service, owner)
    created = add(service, owner, room)
    joined, _ = join(service, actor, created)
    probe = await host_probe(service, actor, joined, **route)
    return room, created, joined, service.rooms.probe_reply(probe, actor)


@pytest.mark.asyncio
async def test_real_probe_adopts_distinct_hosts_on_same_endpoint_without_leaking_handle(rooms):
    service, owner, actor, _ = rooms
    joined, _ = join(service, actor, add(service, owner, create(service, owner)))
    probe = await host_probe(service, actor, joined)
    assert not service.room_connections.hosts.listing({}, owner)['items']
    with pytest.raises(DevError) as invalid:
        service.rooms.probe_reply({**probe, 'probe_nonce': 'x' * 40}, actor)
    assert invalid.value.code == 'ROOM_PROBE_MISMATCH'
    result = service.rooms.probe_reply(probe, actor)
    assert result['host_connection_id'] == result['member']['host_connection_id']
    assert service.rooms.probe_reply(probe, actor)['host_control_handle'] == result['host_control_handle']
    assert 'host_control_handle' not in service.rooms.probe_reply({**probe, 'idempotency_key': key()}, actor)
    _, _, _, other = await independent_host(service, owner, actor, signing=b's')
    assert result['host_connection_id'] != other['host_connection_id']

    view = service.room_connections.hosts.listing({}, owner)
    assert len(view['items']) == 2
    token = result['host_control_handle']
    assert token not in json.dumps(view)
    assert token not in canonical(redact({'host_control_handle': token, 'text': 'value ' + token}))
    assert all(token not in str(row) for row in service.store.all('SELECT * FROM room_host_control_handles'))


@pytest.mark.asyncio
async def test_verified_reuse_keeps_target_after_origin_pause_and_replacement(rooms):
    service, owner, actor, _ = rooms
    source_room, source, original, verified = await independent_host(service, owner, actor)
    created = selected_member(service, owner, verified['host_connection_id'])
    raw = invitation(service, created)
    with pytest.raises(DevError) as missing:
        service.rooms.join(raw, actor)
    assert missing.value.code == 'ROOM_CONNECTION_CONTROL_REQUIRED'
    with pytest.raises(DevError) as other_grant:
        service.rooms.join({**raw, 'host_control_handle': verified['host_control_handle']},
                           replace(actor, grant_id='dot', actor='mcp:dot:fixture'))
    assert other_grant.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'
    member = service.rooms.join({**raw, 'host_control_handle': verified['host_control_handle']}, actor)
    assert member['connection_proof'] != original['connection_proof']
    assert not member['member']['host_connection_id']
    assert not service.room_connections.identity(service.rooms.member(member['member_id']))['host_connection_id']

    probe = await host_probe(service, actor, member)
    adopted = service.rooms.probe_reply(probe, actor)
    assert adopted['host_connection_id'] == verified['host_connection_id']
    assert 'host_control_handle' not in adopted

    source_room = service.rooms.read({'conversation_id': source_room['id']}, owner)['room']
    service.rooms.update({'conversation_id': source_room['id'], 'state': 'paused',
                          'expected_version': source_room['version'], 'idempotency_key': key()}, owner)
    assert service.room_consumers.inbox(identity(member), actor)['items'] == []
    source_room = service.rooms.read({'conversation_id': source_room['id']}, owner)['room']
    service.rooms.update({'conversation_id': source_room['id'], 'state': 'active',
                          'expected_version': source_room['version'], 'idempotency_key': key()}, owner)
    flow = service.rooms.connect_read({'flow_id': source['flow']['id']}, owner)['flow']
    pending = service.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    replacement, _ = join(service, actor, pending)
    probe = await host_probe(service, actor, replacement, route='replacement')
    renewed = service.rooms.probe_reply(probe, actor)
    assert renewed['host_connection_id'] != adopted['host_connection_id']
    assert service.room_consumers.inbox(identity(member), actor)['items'] == []


@pytest.mark.asyncio
async def test_host_revoke_blocks_current_effects_and_keeps_independent_candidate(rooms):
    service, owner, actor, _ = rooms
    _, source, original, verified = await independent_host(service, owner, actor)
    hosts = service.room_connections.hosts
    reference = {'host_connection_id': verified['host_connection_id']}
    preview = hosts.impact(reference, owner)
    created = selected_member(service, owner, verified['host_connection_id'])
    args = {**reference, 'expected_authority_version': preview['authority_version'],
            'impact_digest': preview['impact_digest'], 'idempotency_key': key()}
    with pytest.raises(DevError) as stale:
        hosts.revoke(args, owner)
    assert stale.value.code == 'ROOM_CONNECTION_IMPACT_CHANGED'

    target = service.rooms.join({**invitation(service, created),
                                'host_control_handle': verified['host_control_handle']}, actor)
    service.rooms.probe_reply(await host_probe(service, actor, target), actor)
    _, _, independent, separate = await independent_host(service, owner, actor, route='independent')
    flow = service.rooms.connect_read({'flow_id': source['flow']['id']}, owner)['flow']
    pending = service.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    candidate, _ = join(service, actor, pending)
    candidate_probe = await host_probe(service, actor, candidate, route='independent-candidate')

    message = service.room_runtime.send({
        'conversation_id': original['conversation_id'], 'body_text': '核对宿主撤销前原回执。',
        'client_message_id': key(), 'idempotency_key': key(),
    }, owner)['message']
    session = service.room_consumers.begin({**identity(original), 'wake_ticket': original['wake_ticket'],
        'consumer_run_id': key(), 'idempotency_key': key()}, actor)
    entry = next(item for item in service.room_consumers.inbox(identity(original), actor)['items']
                 if item['object_id'] == message['id'])
    reply = {**identity(original), **entry['reply_arguments'], 'idempotency_key': key(),
        'body_text': '原回执内容。', **{k: session[k] for k in (
            'consumer_session_id', 'consumer_fence', 'consumer_proof')}}
    original_reply = service.room_runtime.reply(reply, actor)

    preview = hosts.impact(reference, owner)
    assert preview['room_count'] == 2 and len(preview['members']) == 2
    args.update(expected_authority_version=preview['authority_version'], impact_digest=preview['impact_digest'])
    revoked = hosts.revoke(args, owner)
    assert revoked['host_revocation_epoch'] == 1
    assert hosts.revoke(args, owner) == revoked
    for joined in (original, target):
        with pytest.raises(DevError) as denied:
            service.room_consumers.inbox(identity(joined), actor)
        assert denied.value.code == 'ROOM_CONNECTION_REVOKED'
    with pytest.raises(DevError) as receipt_denied:
        service.room_runtime.reply(reply, actor)
    assert receipt_denied.value.code == 'ROOM_CONNECTION_REVOKED'
    assert service.store.one('SELECT state FROM dot_consumer_sessions WHERE id=?',
                             (session['consumer_session_id'],))['state'] == 'active'
    assert service.store.one('SELECT id FROM collaboration_messages WHERE id=?',
                             (original_reply['message']['id'],))
    assert service.room_consumers.inbox(identity(independent), actor)['items'] == []
    assert hosts.view({'host_connection_id': separate['host_connection_id']}, owner)['state'] == 'active'

    service.reconcile()
    assert service.store.one('SELECT state,processed_count FROM room_host_revoke_jobs') == {
        'state': 'done', 'processed_count': 2}
    assert service.rooms.pending.current(original['member_id'])['binding_generation'] == candidate['binding_generation']
    replacement = service.rooms.probe_reply(candidate_probe, actor)
    assert replacement['host_connection_id'] not in {separate['host_connection_id'], verified['host_connection_id']}
    assert service.room_consumers.inbox(identity(candidate), actor)['items'] == []
