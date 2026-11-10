"""Exercise independent host registration and owner operations through room entry points."""
import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.public_collaboration import resolve
from shared.unified_contracts import COLLAB_ACTIONS
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import rooms as rooms, create, add, join, identity


@pytest.fixture
def independent(rooms):
    service, owner, actor, clock = rooms
    service.config = replace(service.config, host_connections_mode='on')
    return service, owner, actor, clock


async def native_subscribe(service, actor, joined, secret):
    received = []

    async def callback(_url, body, _headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        received.append(payload['data'])
        return Reply(204)

    service.events = EventService(service, callback)
    await service.events.subscribe({**joined['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://callback.example.invalid/shared',
        'secret': 'whsec_' + base64.b64encode(secret * 32).decode()}}, actor)
    await service.events.tick()
    return next(item['wake_ticket'] for item in received if item.get('member_id') == joined['member_id'])


def finish_probe(service, actor, joined, ticket):
    probe = service.rooms.probe_read({**identity(joined), 'wake_ticket': ticket, 'idempotency_key': key()}, actor)
    request = {**identity(joined), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}
    return service.rooms.probe_reply(request, actor), request


@pytest.mark.asyncio
async def test_bootstrap_cannot_open_business_and_same_address_keeps_separate_hosts(independent):
    service, owner, actor, _ = independent
    first, _ = join(service, actor, add(service, owner, create(service, owner), '同名助手'))
    first_ticket = await native_subscribe(service, actor, first, b'a')
    assert not service.rooms.member(first['member_id'])['host_connection_id']
    with pytest.raises(DevError):
        service.room_consumers.begin({**identity(first), 'wake_ticket': first_ticket,
            'consumer_run_id': key(), 'idempotency_key': key()}, actor)
    with pytest.raises(DevError):
        service.rooms.member_scope(actor, first['conversation_id'], first['member_id'], first['binding_generation'])
    result, request = finish_probe(service, actor, first, first_ticket)
    first_id = result['member']['host_connection_id']
    assert first_id and result['control_handle']
    assert service.rooms.probe_reply(request, actor)['control_handle'] == result['control_handle']

    second, _ = join(service, actor, add(service, owner, create(service, owner), '同名助手'))
    second_ticket = await native_subscribe(service, actor, second, b'b')
    other, _ = finish_probe(service, actor, second, second_ticket)
    second_id = other['member']['host_connection_id']
    assert second_id and first_id != second_id
    listing = service.rooms.invoke('room_connection_list', {}, owner)
    assert {item['host_connection_id'] for item in listing['items']} == {first_id, second_id}
    assert result['control_handle'] not in json.dumps(listing)
    with pytest.raises(DevError) as denied:
        service.rooms.invoke('room_connection_view', {'host_connection_id': first_id}, actor)
    assert denied.value.code == 'OWNER_REQUIRED'


def test_batch_persists_original_children_and_isolates_stale_generation(independent):
    service, owner, _, _ = independent
    first = add(service, owner, create(service, owner))
    second = add(service, owner, create(service, owner))
    args = {'members': [
        {'member_id': first['member']['id'], 'expected_generation': 9},
        {'member_id': second['member']['id'], 'expected_generation': 1}], 'idempotency_key': key()}
    batch = service.rooms.invoke('room_connection_rebind_batch', args, owner)
    failed, prepared = batch['items']
    assert failed['status'] == 'failed' and failed['error_code'] == 'ROOM_BINDING_CHANGED'
    assert prepared['status'] == 'invited' and prepared['candidate_id']
    assert service.rooms.pending.current(second['member']['id'])['id'] == prepared['candidate_id']
    assert service.rooms.invoke('room_connection_rebind_batch', args, owner) == batch
    page = service.rooms.invoke('room_connection_rebind_status', {'batch_id': batch['batch_id'], 'limit': 1}, owner)
    next_page = service.rooms.invoke('room_connection_rebind_status',
        {'batch_id': batch['batch_id'], 'limit': 1, 'cursor': page['next_cursor']}, owner)
    assert page['items'] == [failed] and next_page['items'] == [prepared]
    assert not next_page['complete']
    assert 'invite_code' not in json.dumps(batch)


@pytest.mark.parametrize('tool,group,action,args', [
    ('query', 'collab.list', 'room_connection_list', {}),
    ('query', 'collab.get', 'room_connection_view', {'host_connection_id': 'host_example'}),
    ('query', 'collab.get', 'room_connection_impact', {'host_connection_id': 'host_example'}),
    ('query', 'collab.get', 'room_connection_rebind_status', {'batch_id': 'batch_example'}),
    ('act', 'collab.connect', 'room_connection_revoke', {'host_connection_id': 'host_example',
        'expected_authority_version': 1, 'impact_digest': 'a' * 64, 'idempotency_key': 'test:revoke:1'}),
    ('act', 'collab.connect', 'room_connection_control', {'owner_operation_id': 'operation_example',
        'challenge_proof': 'a' * 32, 'idempotency_key': 'test:control:1'}),
])
def test_unified_resource_actions_use_the_public_room_contract(tool, group, action, args):
    destination, action_name = COLLAB_ACTIONS[tool][group][action]
    assert resolve(destination, {'action': action_name, **args})[0] == 'collaboration_' + action


@pytest.mark.asyncio
async def test_paused_revoked_member_can_validate_replacement_without_restoring_old_route(independent):
    service, owner, actor, _ = independent
    created = add(service, owner, create(service, owner))
    original, _ = join(service, actor, created)
    ticket = await native_subscribe(service, actor, original, b'a')
    result, _ = finish_probe(service, actor, original, ticket)
    host_id = result['member']['host_connection_id']
    flow_id = created['flow']['id']
    flow = service.rooms.connect_read({'flow_id': flow_id}, owner)['flow']
    paused = service.rooms.connect_action({'flow_id': flow_id, 'action': 'pause',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    impact = service.room_connections.impact({'host_connection_id': host_id}, owner)
    service.room_connections.revoke({'host_connection_id': host_id,
        'expected_authority_version': impact['authority_version'],
        'impact_digest': impact['impact_digest'], 'idempotency_key': key()}, owner)

    replacement = service.rooms.connect_action({'flow_id': flow_id, 'action': 'rebind',
        'expected_version': paused['flow']['version'], 'idempotency_key': key()}, owner)
    pending, _ = join(service, actor, replacement)
    pending_ticket = await native_subscribe(service, actor, pending, b'b')
    assert service.rooms.member(original['member_id'])['status'] == 'paused'
    assert service.rooms.member(original['member_id'])['host_connection_id'] == host_id
    verified, _ = finish_probe(service, actor, pending, pending_ticket)
    assert verified['binding_activated'] and verified['member']['host_connection_id'] != host_id
    assert service.room_connections.read_host(host_id)['state'] == 'revoked'
