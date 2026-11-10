"""Host control exchanges and target binding tests with synthetic native callbacks."""
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import add, create, identity, join, rooms as rooms, subscribe


def enable(service):
    service.config = replace(service.config, host_connections_mode='on')


async def probe(service, actor, joined, route):
    await subscribe(service, actor, joined, route)
    read = service.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'],
                                    'idempotency_key': key()}, actor)
    args = {**identity(joined), 'probe_nonce': read['probe_nonce'], 'idempotency_key': key()}
    return service.rooms.probe_reply(args, actor), args


def attach(service, owner, host_id):
    room = create(service, owner)
    return service.rooms.create_member({'conversation_id': room['id'], 'name': '复用 dot',
        'host_connection_id': host_id, 'idempotency_key': key()}, owner)


async def control_callback(service, member_id):
    delivered = []

    async def sender(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        if payload.get('data', {}).get('host_control_request'):
            delivered.append(payload['data'])
        return Reply(204)

    service.events = EventService(service, sender)
    await service.events.tick()
    return next(payload for payload in delivered if payload['member_id'] == member_id)


def invoke(service, request, principal):
    return service.invoke(request['tool'], request['arguments'], principal)


@pytest.mark.asyncio
async def test_verified_source_callback_controls_independent_target(rooms):
    s, owner, actor, _ = rooms
    enable(s)
    source_created = add(s, owner, create(s, owner))
    source, _ = join(s, actor, source_created)
    source_verified, _ = await probe(s, actor, source, 'source-host')
    host = s.rooms.member(source['member_id'])['host_connection_id']

    target_created = attach(s, owner, host)
    assert 'join_instructions' not in target_created
    with pytest.raises(DevError) as early:
        join(s, actor, target_created)
    assert early.value.code == 'ROOM_CONNECTION_CONTROL_REQUIRED'

    callback = await control_callback(s, source['member_id'])
    assert callback['wake_ticket'] is None
    request = callback['host_control_request']
    assert request['arguments']['owner_operation_id'] == target_created['flow']['id']
    assert 'invite_code' not in request['arguments'] and 'control_handle' not in request['arguments']
    assert source_verified['control_handle'] not in json.dumps(callback)
    with pytest.raises(DevError) as wrong_grant:
        invoke(s, request, replace(actor, grant_id='dot', actor='mcp:dot:fixture'))
    assert wrong_grant.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'

    receipt = invoke(s, request, actor)
    assert receipt == invoke(s, request, actor)
    with pytest.raises(DevError) as forged_replay:
        invoke(s, {**request, 'arguments': {**request['arguments'], 'idempotency_key': key()}}, actor)
    assert forged_replay.value.code == 'ROOM_CONNECTION_CONTROL_REDEEMED'
    with pytest.raises(DevError) as wrong_join:
        join(s, actor, target_created)
    assert wrong_join.value.code == 'ROOM_CONNECTION_CONTROL_REQUIRED'

    target = invoke(s, receipt['next_request'], actor)
    assert target['connection_proof'] != source['connection_proof']
    assert target['conversation_id'] != source['conversation_id']
    assert target['subscription_request']['arguments']['member_id'] != source['member_id']
    source_flow = s.rooms.connect_read({'flow_id': source_created['flow']['id']}, owner)['flow']
    s.rooms.connect_action({'flow_id': source_flow['id'], 'action': 'remove',
        'expected_version': source_flow['version'], 'idempotency_key': key()}, owner)

    verified, _ = await probe(s, actor, target, 'independent-target-host')
    member = s.rooms.member(target['member_id'])
    assert member['host_connection_id'] == host and s.rooms.state(member) == 'ready_chat'
    assert 'control_handle' not in verified
    assert not s.store.all('SELECT * FROM dot_consumer_sessions')


@pytest.mark.asyncio
async def test_handle_replay_bound_to_original_exchange_and_selected_host(rooms):
    s, owner, actor, _ = rooms
    enable(s)
    first, _ = join(s, actor, add(s, owner, create(s, owner)))
    established, original = await probe(s, actor, first, 'handle-source')
    handle = established['control_handle']
    host = s.rooms.member(first['member_id'])['host_connection_id']
    assert s.rooms.probe_reply(original, actor)['control_handle'] == handle
    assert 'control_handle' not in s.rooms.probe_reply({**original, 'idempotency_key': key()}, actor)
    assert handle not in json.dumps(s.room_connections.host_view({'host_connection_id': host}, owner))

    second, _ = join(s, actor, add(s, owner, create(s, owner)))
    other, _ = await probe(s, actor, second, 'other-handle-source')
    target = attach(s, owner, host)
    args = {'owner_operation_id': target['flow']['id'], 'idempotency_key': key()}
    with pytest.raises(DevError) as mismatch:
        s.rooms.host_proofs.control({**args, 'control_handle': other['control_handle']}, actor)
    assert mismatch.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'
    receipt = s.rooms.host_proofs.control({**args, 'control_handle': handle}, actor)
    assert receipt['state'] == 'control_verified'
    assert invoke(s, receipt['next_request'], actor)['member_id'] == target['member']['id']


@pytest.mark.asyncio
async def test_refresh_invalidates_frozen_challenge_and_preserves_original_target(rooms):
    s, owner, actor, _ = rooms
    enable(s)
    source, _ = join(s, actor, add(s, owner, create(s, owner)))
    await probe(s, actor, source, 'refresh-source')
    target = attach(s, owner, s.rooms.member(source['member_id'])['host_connection_id'])
    original = (await control_callback(s, source['member_id']))['host_control_request']
    s.rooms.connect_action({'flow_id': target['flow']['id'], 'action': 'refresh_invite',
        'expected_version': target['flow']['version'], 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as expired:
        invoke(s, original, actor)
    assert expired.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'
    replacement = (await control_callback(s, source['member_id']))['host_control_request']
    assert replacement['arguments']['owner_operation_id'] == original['arguments']['owner_operation_id']
    assert replacement['arguments']['challenge_proof'] != original['arguments']['challenge_proof']
    assert invoke(s, replacement, actor)['member_id'] == target['member']['id']


@pytest.mark.asyncio
async def test_opt_in_route_round_trip_rotates_handle_and_replays_exact_exchange(rooms):
    s, owner, actor, _ = rooms
    enable(s)
    source, _ = join(s, actor, add(s, owner, create(s, owner)))
    established, first_probe = await probe(s, actor, source, 'rotate-old')
    original = established['control_handle']
    member = s.rooms.member(source['member_id'])
    approval = {'conversation_id': source['conversation_id'], 'member_id': source['member_id'],
        'expected_route_generation': member['route_generation'],
        'callback_url': 'https://callback.example.invalid/room/rotate-new',
        'rotate_control_handle': True, 'idempotency_key': key()}
    route = s.rooms.routes.prepare(approval, owner)
    assert route == s.rooms.routes.prepare(approval, owner)
    control = {'owner_operation_id': route['owner_operation_id'], 'control_handle': original,
        'idempotency_key': key()}
    s.rooms.host_proofs.control(control, actor)
    assert s.rooms.probe_reply(first_probe, actor)['control_handle'] == original

    rotated, rotation_probe = await probe(s, actor, source, 'rotate-new')
    current = rotated['control_handle']
    assert current != original and rotated['state'] == 'active'
    assert s.rooms.probe_reply(rotation_probe, actor)['control_handle'] == current
    assert 'control_handle' not in s.rooms.probe_reply({**rotation_probe, 'idempotency_key': key()}, actor)
    assert s.rooms.host_proofs.control(control, actor)['state'] == 'active'

    target = attach(s, owner, member['host_connection_id'])
    new_control = {'owner_operation_id': target['flow']['id'], 'idempotency_key': key()}
    with pytest.raises(DevError) as retired:
        s.rooms.host_proofs.control({**new_control, 'control_handle': original}, actor)
    assert retired.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'
    assert s.rooms.host_proofs.control({**new_control, 'control_handle': current}, actor)['state'] == 'control_verified'
