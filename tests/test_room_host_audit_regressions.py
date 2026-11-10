"""Host connection audit regressions using a temporary Hub and synthetic callbacks."""

import asyncio
import base64
import json
from threading import Barrier

import pytest

from hub import mcp_request_audit
from hub.collaboration.event_errors import CallbackEndpointError
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.service import CollaborationService
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_host_proofs import attach, enable, invoke, probe
from tests.test_room_members_rebuild import add, create, identity, join, rooms as rooms, subscribe


async def established(service, owner, actor):
    enable(service)
    joined, _ = join(service, actor, add(service, owner, create(service, owner)))
    receipt, _ = await probe(service, actor, joined, 'audit-source')
    return joined, receipt['member']['host_connection_id'], receipt['control_handle']


def target_join(service, owner, actor, host_id, handle):
    target = attach(service, owner, host_id)
    control = service.rooms.host_proofs.control({'owner_operation_id': target['flow']['id'],
        'control_handle': handle, 'idempotency_key': key()}, actor)
    return invoke(service, control['next_request'], actor)


def revoke_request(service, owner, host_id):
    preview = service.room_connections.impact({'host_connection_id': host_id}, owner)
    return {'host_connection_id': host_id, 'impact_digest': preview['impact_digest'],
        'expected_authority_version': preview['authority_version'], 'idempotency_key': key()}


@pytest.mark.asyncio
async def test_concurrent_host_reuse_in_one_room_keeps_single_flow_and_receipt(rooms):
    service, owner, actor, _ = rooms
    _, host_id, _ = await established(service, owner, actor)
    room_id = create(service, owner)['id']
    base = {'conversation_id': room_id, 'name': '同一宿主', 'host_connection_id': host_id}
    requests = [{**base, 'idempotency_key': key()} for _ in range(2)]
    start = Barrier(len(requests))

    def reuse(request):
        start.wait(timeout=5)
        return service.rooms.create_member(request, owner)

    results = await asyncio.gather(*(asyncio.to_thread(reuse, request) for request in requests),
                                   return_exceptions=True)
    winner, = (i for i, result in enumerate(results) if isinstance(result, dict))
    loser = results[1 - winner]
    assert isinstance(loser, DevError) and loser.code == 'ROOM_CONNECTION_ALREADY_PRESENT'
    member_id, flow_id = results[winner]['member']['id'], results[winner]['flow']['id']
    replay = service.rooms.create_member(requests[winner], owner)
    assert (replay['member']['id'], replay['flow']['id']) == (member_id, flow_id)
    assert service.store.all('SELECT id,host_connection_id FROM room_members WHERE conversation_id=?',
                             (room_id,)) == [{'id': member_id, 'host_connection_id': host_id}]
    assert service.store.all('SELECT id FROM room_connect_flows WHERE conversation_id=?',
                             (room_id,)) == [{'id': flow_id}]


@pytest.mark.asyncio
async def test_late_probe_reply_cannot_activate_a_revoked_host(rooms):
    service, owner, actor, _ = rooms
    _, host_id, handle = await established(service, owner, actor)
    target = target_join(service, owner, actor, host_id, handle)
    await subscribe(service, actor, target, 'audit-late-probe')
    probe_read = service.rooms.probe_read({**identity(target), 'wake_ticket': target['wake_ticket'],
        'idempotency_key': key()}, actor)
    member = service.rooms.member(target['member_id'])

    revoked = service.room_connections.revoke(revoke_request(service, owner, host_id), owner)
    assert revoked['cleanup_state'] == 'pending'
    with pytest.raises(DevError) as late:
        service.rooms.probe_reply({**identity(target), 'probe_nonce': probe_read['probe_nonce'],
            'idempotency_key': key()}, actor)
    assert late.value.code == 'ROOM_CONNECTION_REVOKED'
    assert service.rooms.member(member['id']) == member
    assert service.store.all('SELECT id,state FROM room_host_connections') == [{'id': host_id, 'state': 'revoked'}]
    assert service.store.one('SELECT 1 FROM room_host_binding_adoptions WHERE member_id=?',
                             (member['id'],)) is None


@pytest.mark.asyncio
async def test_host_revocation_cleanup_resumes_after_reopening_database(rooms):
    service, owner, actor, clock = rooms
    source, host_id, handle = await established(service, owner, actor)
    targets = [target_join(service, owner, actor, host_id, handle) for _ in range(2)]
    for i, target in enumerate(targets):
        await probe(service, actor, target, f'audit-restart-{i}')
    joined = [source, *targets]
    members = [service.rooms.member(item['member_id']) for item in joined]
    args = revoke_request(service, owner, host_id)
    original = service.room_connections.revoke(args, owner)
    for item in joined:
        with pytest.raises(DevError) as denied:
            service.room_consumers.inbox(identity(item), actor)
        assert denied.value.code == 'ROOM_CONNECTION_REVOKED'
    assert service.room_connections.drain_revoke_jobs(page_size=1) == 1
    checkpoint = service.store.one('SELECT after_member_id,processed_count FROM room_host_revoke_jobs')
    assert checkpoint == {'after_member_id': min(member['id'] for member in members), 'processed_count': 1}

    # Open an independent Store and service against the same durable database.
    restarted_store = Store(service.store.directory)
    try:
        restarted = CollaborationService(Runtime(restarted_store), service.config, clock=lambda: clock[0])
        assert restarted_store.one('SELECT after_member_id,processed_count FROM room_host_revoke_jobs') == checkpoint
        assert restarted.room_connections.drain_revoke_jobs(page_size=1) == 1
        assert restarted.room_connections.drain_revoke_jobs(page_size=1) == 1
        assert restarted.room_connections.drain_revoke_jobs(page_size=1) == 0
        receipt = restarted.room_connections.revoke(args, owner)
        assert receipt['operation_id'] == original['operation_id']
        assert receipt['processed_count'] == len(members) and receipt['cleanup_state'] == 'done'
        for member in members:
            sub = restarted_store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?',
                                      (member['subscription_id'],))
            assert sub['state'] == 'unsubscribed'
    finally:
        restarted_store.close()


@pytest.mark.asyncio
async def test_host_credentials_stay_out_of_public_diagnostics_errors_and_traces(rooms, caplog, monkeypatch):
    service, owner, actor, _ = rooms
    source, host_id, handle = await established(service, owner, actor)
    target = attach(service, owner, host_id)
    challenge = service.store.one('SELECT encrypted_secret FROM room_host_attach_challenges WHERE owner_operation_id=?',
                                  (target['flow']['id'],))
    challenge_proof = service.store.decrypt(challenge['encrypted_secret'])
    invite = add(service, owner, create(service, owner))
    invite_flow = service.store.one('SELECT invite_secret FROM room_connect_flows WHERE id=?',
                                    (invite['flow']['id'],))
    invite_code = service.store.decrypt(invite_flow['invite_secret'])
    signing_key = 'whsec_' + base64.b64encode(b'r' * 32).decode()
    secrets = (handle, source['connection_proof'], challenge_proof, invite_code, signing_key)
    new_member, _ = join(service, actor, invite)

    async def rejected_callback(_url, _body, _headers):
        return Reply(503, ' '.join(secrets).encode())

    trace_events = []
    monkeypatch.setattr(mcp_request_audit, 'write_event', trace_events.append)
    trace = mcp_request_audit._CURRENT.set(mcp_request_audit.Trace(True, 'POST'))
    try:
        with caplog.at_level('INFO'), pytest.raises(CallbackEndpointError) as denied:
            await EventService(service, rejected_callback).subscribe({**new_member['subscription_request'],
                'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/audit-failure',
                             'secret': signing_key}}, actor)
    finally:
        mcp_request_audit._CURRENT.reset(trace)
    assert denied.value.reason == 'http_5xx'
    assert any(event['stage'] == 'event_callback_failed' for event in trace_events)

    room = {'conversation_id': source['conversation_id']}
    listing = service.room_connections.host_listing({}, owner)
    assert any(item['host_connection_id'] == host_id for item in listing['items'])
    outputs = [listing, service.room_connections.host_view({'host_connection_id': host_id}, owner),
        service.rooms.diagnostics(room, owner),
        service.rooms.diagnostics({**room, 'member_id': source['member_id']}, owner),
        service.store.all('SELECT data FROM mcp_event_outbox WHERE conversation_id=?', (room['conversation_id'],)),
        trace_events, caplog.text, str(denied.value)]
    serialized = json.dumps(outputs)
    for secret in secrets:
        assert secret not in serialized
