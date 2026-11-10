"""Host identity and revocation through the native room entry points."""
import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.room_connections import RoomConnections
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_connections import verified
from tests.test_room_members_rebuild import add, create, identity, join, rooms as rooms


async def verify_through_shared_gateway(service, actor, created, signing_key):
    joined, _ = join(service, actor, created)
    received = []

    async def receiver(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        received.append(payload['data'])
        return Reply(204)

    service.events = EventService(service, receiver)
    await service.events.subscribe({**joined['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://callback.example.invalid/shared-gateway',
        'secret': 'whsec_' + base64.b64encode(signing_key).decode()}}, actor)
    await service.events.tick()
    ticket = next(data['wake_ticket'] for data in received if data['member_id'] == joined['member_id'])
    nonce = service.rooms.probe_read({**identity(joined), 'wake_ticket': ticket, 'idempotency_key': key()}, actor)
    service.rooms.probe_reply({**identity(joined), 'probe_nonce': nonce['probe_nonce'], 'idempotency_key': key()}, actor)
    return joined


@pytest.fixture
def host_rooms(rooms):
    service, owner, actor, clock = rooms
    service.config = replace(service.config, host_connections_mode='on')
    return service, owner, actor, clock


@pytest.mark.asyncio
async def test_host_revoke_is_immediate_across_rollback_and_preserves_other_hosts(host_rooms):
    service, owner, actor, clock = host_rooms
    first = await verify_through_shared_gateway(service, actor, add(service, owner, create(service, owner)), b'a' * 32)
    second_room = create(service, owner)
    second = await verify_through_shared_gateway(service, actor, add(service, owner, second_room), b'b' * 32)
    connections = service.room_connections
    first_member = service.rooms.member(first['member_id'])
    second_member = service.rooms.member(second['member_id'])
    first_host, second_host = first_member['host_connection_id'], second_member['host_connection_id']
    assert first_host and second_host and first_host != second_host
    assert first_member['grant_id'] == second_member['grant_id']
    assert first_member['endpoint_digest'] == second_member['endpoint_digest']
    available = connections.host_view({'host_connection_id': second_host}, owner)
    assert available['display_state'] == 'connected'
    assert available['single_room_title'] == second_room['title']
    assert available['connection_capabilities']['route_rotation']
    with pytest.raises(DevError):
        connections.host_view({'host_connection_id': first_host}, actor)

    delivery = service.store.one('SELECT id FROM mcp_event_deliveries WHERE subscription_id=?',
                                 (first_member['subscription_id'],))
    service.store.execute("""UPDATE mcp_event_deliveries SET state='leased',lease_until=?,accepted_at=NULL
        WHERE id=?""", (clock[0] + 30, delivery['id']))
    preview = connections.impact({'host_connection_id': first_host}, owner)
    assert preview['room_count'] == 1
    assert preview['items'][0]['member_id'] == first['member_id']
    connections.touch_health(first_host)
    args = {'host_connection_id': first_host, 'impact_digest': preview['impact_digest'],
            'expected_authority_version': preview['authority_version'], 'idempotency_key': key()}
    receipt = connections.revoke(args, owner)
    assert receipt['cleanup_state'] == 'pending'
    assert connections.revoke(args, owner)['operation_id'] == receipt['operation_id']

    service.config = replace(service.config, host_connections_mode='off')
    with pytest.raises(DevError) as error:
        service.room_consumers.inbox(identity(first), actor)
    assert error.value.code == 'ROOM_CONNECTION_REVOKED'
    service.room_consumers.inbox(identity(second), actor)
    assert connections.host_view({'host_connection_id': first_host}, owner)['display_state'] == 'revoked'
    remaining = connections.host_view({'host_connection_id': second_host}, owner)['connection_capabilities']
    assert remaining['host_revoke'] and not remaining['verified_reuse'] and not remaining['route_rotation']
    assert service.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?',
                             (first_member['subscription_id'],))['state'] == 'active'
    assert connections.drain_revoke_jobs(page_size=1) == 1
    assert connections.revoke(args, owner)['cleanup_state'] == 'running'
    assert service.store.one('SELECT reason_code FROM mcp_event_deliveries WHERE id=?',
                             (delivery['id'],))['reason_code'] == 'host_revoked_in_flight'
    clock[0] += 31
    assert connections.drain_revoke_jobs(page_size=1) == 0
    assert connections.revoke(args, owner)['cleanup_state'] == 'done'
    assert service.store.one('SELECT state,reason_code FROM mcp_event_deliveries WHERE id=?',
                             (delivery['id'],)) == {'state': 'abandoned', 'reason_code': 'host_revoked_in_flight_unknown'}
    assert service.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?',
                             (first_member['subscription_id'],))['state'] == 'unsubscribed'
    assert service.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?',
                             (second_member['subscription_id'],))['state'] == 'active'


@pytest.mark.asyncio
async def test_revocation_rejects_stale_impact_and_keeps_unrelated_candidate(host_rooms):
    service, owner, actor, _ = host_rooms
    created = add(service, owner, create(service, owner))
    joined = await verified(service, actor, created, 'old-host')
    member = service.rooms.member(joined['member_id'])
    host_id = member['host_connection_id']
    connections = service.room_connections
    impact = connections.impact({'host_connection_id': host_id}, owner)
    args = {'host_connection_id': host_id, 'impact_digest': impact['impact_digest'],
            'expected_authority_version': impact['authority_version'], 'idempotency_key': key()}

    connections.touch_authority(host_id)
    with pytest.raises(DevError) as error:
        connections.revoke(args, owner)
    assert error.value.code == 'ROOM_CONNECTION_IMPACT_CHANGED'
    flow = service.store.one('SELECT * FROM room_connect_flows WHERE member_id=?', (member['id'],))
    prepared = service.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    original_candidate = service.rooms.pending.current(member['id'])
    assert original_candidate['host_connection_id'] == ''
    route = service.rooms.routes.prepare({'conversation_id': member['conversation_id'], 'member_id': member['id'],
        'expected_route_generation': member['route_generation'], 'idempotency_key': key(),
        'callback_url': 'https://callback.example.invalid/rotated'}, owner)

    impact = connections.impact({'host_connection_id': host_id}, owner)
    connections.revoke({**args, 'expected_authority_version': impact['authority_version']}, owner)
    connections.drain_revoke_jobs()
    assert service.rooms.pending.current(member['id'])['id'] == original_candidate['id']
    assert service.rooms.routes.get(route['owner_operation_id'])['state'] == 'cancelled'
    replacement = await verified(service, actor, prepared, 'replacement-host')
    current = service.rooms.member(replacement['member_id'])
    assert current['host_connection_id'] != host_id
    assert current['binding_generation'] == original_candidate['binding_generation']
    assert connections.read_host(host_id)['state'] == 'revoked'


@pytest.mark.asyncio
async def test_revocation_impact_and_cleanup_use_all_pages(host_rooms):
    service, owner, actor, _ = host_rooms
    joined = await verified(service, actor, add(service, owner, create(service, owner)), 'cleanup-host')
    original = service.rooms.member(joined['member_id'])
    host_id = original['host_connection_id']
    connections = service.room_connections
    query = {'host_connection_id': host_id, 'limit': 1}
    initial = connections.impact(query, owner)
    target_ids = [service.rooms.create_member({'conversation_id': create(service, owner)['id'],
        'name': f'待接入 {index}', 'host_connection_id': host_id, 'idempotency_key': key()}, owner)['member']['id']
        for index in range(2)]
    page = connections.impact(query, owner)
    assert connections.host_view({'host_connection_id': host_id}, owner)['single_room_title'] is None
    assert initial['impact_digest'] != page['impact_digest']
    assert page['room_count'] == 3 and len(page['items']) == 1
    second = connections.impact({**query, 'cursor': page['next_cursor']}, owner)
    assert second['impact_digest'] == page['impact_digest']
    assert second['items'][0]['member_id'] != page['items'][0]['member_id']
    args = {'host_connection_id': host_id, 'impact_digest': page['impact_digest'],
            'expected_authority_version': page['authority_version'], 'idempotency_key': key()}
    connections.revoke(args, owner)
    assert connections.drain_revoke_jobs(page_size=1) == 1

    # A fresh worker instance must resume the original job's persisted position.
    resumed = RoomConnections(service)
    assert resumed.drain_revoke_jobs(page_size=1) == 1
    assert resumed.drain_revoke_jobs(page_size=1) == 1
    assert resumed.drain_revoke_jobs(page_size=1) == 0
    receipt = resumed.revoke(args, owner)
    assert receipt['cleanup_state'] == 'done' and receipt['processed_count'] == 3
    for target_id in target_ids:
        flow = service.store.one('SELECT invite_secret FROM room_connect_flows WHERE member_id=?', (target_id,))
        assert flow['invite_secret'] == ''
