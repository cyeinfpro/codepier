"""Explicit cross-room host reuse with independent authority and native proofs.

These run actual room/event/consumer services against an in-process synthetic
callback. They do not claim that an external dot has approved a subscription.
"""
import json
from dataclasses import replace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import save_room_permissions
from tests.test_room_members_rebuild import rooms as rooms, create, add, join, subscribe, identity


async def verified(service, actor, created, route='original'):
    joined, _ = join(service, actor, created)
    await subscribe(service, actor, joined, route)
    nonce = service.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'],
                                     'idempotency_key': key()}, actor)['probe_nonce']
    service.rooms.probe_reply({**identity(joined), 'probe_nonce': nonce, 'idempotency_key': key()}, actor)
    return joined


def reuse(service, owner, room, source, **extra):
    return service.rooms.create_member({'conversation_id': room['id'], 'name': '同一 dot 的新房间',
        'reuse_member_id': source['member_id'], 'reuse_binding_generation': source['binding_generation'],
        'idempotency_key': key(), **extra}, owner)


@pytest.mark.asyncio
async def test_owner_list_contains_only_verified_safe_metadata_and_new_member_has_no_copied_identity(rooms):
    s, owner, actor, _ = rooms
    source_room = create(s, owner)
    add(s, owner, source_room, '尚未接入')
    source = await verified(s, actor, add(s, owner, source_room, '原 dot'))
    result = s.room_connections.listing({}, owner)
    assert [item['id'] for item in result['items']] == [source['member_id']]
    assert set(result['items'][0]) <= {'id', 'member_id', 'name', 'conversation_id', 'room_title',
        'binding_generation', 'verified_at', 'state', 'display'}
    public = json.dumps(result)
    assert source['connection_proof'] not in public and 'endpoint' not in public and 'grant_id' not in public
    with pytest.raises(DevError):
        s.room_connections.listing({}, replace(owner, user_id='other', actor='panel:other'))
    with pytest.raises(DevError):
        s.room_connections.listing({}, actor)

    target = reuse(s, owner, create(s, owner), source)
    member = s.rooms.member(target['member']['id'])
    assert member['connection_source_member_id'] == source['member_id']
    assert member['connection_source_generation'] == source['binding_generation']
    assert member['id'] != source['member_id'] and member['status'] == 'invited'
    assert not any(member[field] for field in ('grant_id', 'subscription_id', 'endpoint_digest',
        'connection_proof_hash', 'connection_proof_secret', 'probe_replied_at'))
    assert not s.store.all('SELECT * FROM room_member_scopes WHERE member_id=?', (member['id'],))
    assert not s.store.all('SELECT * FROM dot_consumer_sessions WHERE member_id=?', (member['id'],))


@pytest.mark.asyncio
async def test_reuse_requires_selected_grant_and_independent_proof_but_permits_explicit_native_route(rooms):
    s, owner, actor, _ = rooms
    original = await verified(s, actor, add(s, owner, create(s, owner)))
    created = reuse(s, owner, create(s, owner), original)
    with pytest.raises(DevError) as wrong_grant:
        join(s, replace(actor, grant_id='dot', actor='mcp:dot:fixture'), created)
    assert wrong_grant.value.code == 'ROOM_CONNECTION_IDENTITY_REQUIRED'
    destination, _ = join(s, actor, created)
    assert destination['connection_proof'] != original['connection_proof']
    assert destination['subscription_request']['arguments']['member_id'] != original['member_id']
    with pytest.raises(DevError) as wrong_host:
        await subscribe(s, actor, destination, 'another-host')
    assert wrong_host.value.code == 'ROOM_CONNECTION_HOST_CHANGED'
    # Both endpoint and the callback signing identity are the original host's;
    # the explicit source relationship is the only collision exception.
    await subscribe(s, actor, destination, 'original')
    with pytest.raises(DevError) as wrong_proof:
        s.rooms.probe_read({**identity(destination), 'connection_proof': original['connection_proof'],
            'wake_ticket': destination['wake_ticket'], 'idempotency_key': key()}, actor)
    assert wrong_proof.value.code == 'ROOM_MEMBER_PROOF_REQUIRED'
    nonce = s.rooms.probe_read({**identity(destination), 'wake_ticket': destination['wake_ticket'],
        'idempotency_key': key()}, actor)['probe_nonce']
    s.rooms.probe_reply({**identity(destination), 'probe_nonce': nonce, 'idempotency_key': key()}, actor)
    assert s.rooms.state(s.rooms.member(destination['member_id'])) == 'ready_chat'
    unrelated, _ = join(s, actor, add(s, owner, create(s, owner), '无复用关系的 dot'))
    with pytest.raises(DevError) as collision:
        await subscribe(s, actor, unrelated, 'original')
    assert collision.value.code == 'ROOM_ROUTE_SHARED'
    assert s.rooms.state(s.rooms.member(original['member_id'])) == 'ready_chat'


@pytest.mark.asyncio
async def test_same_room_or_duplicate_source_is_rejected_without_stranded_member_or_flow(rooms):
    s, owner, actor, _ = rooms
    room = create(s, owner)
    source = await verified(s, actor, add(s, owner, room))
    count = s.store.one('SELECT COUNT(*) n FROM room_members')['n']
    with pytest.raises(DevError) as same:
        reuse(s, owner, room, source)
    assert same.value.code == 'ROOM_CONNECTION_ALREADY_PRESENT'
    assert s.store.one('SELECT COUNT(*) n FROM room_members')['n'] == count
    target_room = create(s, owner)
    target = reuse(s, owner, target_room, source)
    with pytest.raises(DevError) as duplicate:
        reuse(s, owner, target_room, source)
    assert duplicate.value.code == 'ROOM_CONNECTION_ALREADY_PRESENT'
    assert s.store.one('SELECT COUNT(*) n FROM room_members WHERE conversation_id=?', (target_room['id'],))['n'] == 1
    assert s.rooms.connect_read({'flow_id': target['flow']['id']}, owner)['member']['id'] == target['member']['id']


@pytest.mark.asyncio
async def test_source_pause_and_actual_rebind_block_old_reuse_without_transferring_authority(rooms):
    s, owner, actor, _ = rooms
    original_created = add(s, owner, create(s, owner))
    original = await verified(s, actor, original_created)
    target = await verified(s, actor, reuse(s, owner, create(s, owner), original))
    flow = s.rooms.connect_read({'flow_id': original_created['flow']['id']}, owner)['flow']
    paused = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'pause',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    assert s.rooms.state(s.rooms.member(target['member_id'])) == 'degraded'
    with pytest.raises(DevError):
        s.room_consumers.inbox(identity(target), actor)
    restored = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'resume',
        'expected_version': paused['flow']['version'], 'idempotency_key': key()}, owner)
    assert s.rooms.state(s.rooms.member(target['member_id'])) == 'ready_chat'
    prepared = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': restored['flow']['version'], 'idempotency_key': key()}, owner)
    assert s.rooms.state(s.rooms.member(target['member_id'])) == 'ready_chat'
    replacement = await verified(s, actor, prepared, 'new-native-host')
    assert replacement['binding_generation'] == original['binding_generation'] + 1
    assert s.rooms.state(s.rooms.member(target['member_id'])) == 'degraded'
    with pytest.raises(DevError) as changed:
        s.room_consumers.inbox(identity(target), actor)
    assert changed.value.code == 'ROOM_CONNECTION_CHANGED'
    with pytest.raises(DevError) as stale:
        reuse(s, owner, create(s, owner), original)
    assert stale.value.code == 'ROOM_CONNECTION_CHANGED'


@pytest.mark.asyncio
async def test_reused_host_scopes_messages_and_watermarks_remain_separate(rooms):
    s, owner, actor, _ = rooms
    actor = replace(actor, grant_id='broad', actor='mcp:broad:fixture')
    source_room = create(s, owner, [{'project': 'proj', 'environment_id': 'production'}])
    save_room_permissions(s, owner, source_room['id'],
        [{'project_id': 'proj', 'capabilities': ['read', 'write']}])
    original = await verified(s, actor, s.rooms.create_member({'conversation_id': source_room['id'],
        'name': '原授权', 'idempotency_key': key()}, owner))
    target_room = create(s, owner, [{'project': 'proj', 'environment_id': 'production'}])
    target = await verified(s, actor, reuse(s, owner, target_room, original))
    assert s.rooms.scopes(s.rooms.member(original['member_id']), s.rooms.principal(s.rooms.member(original['member_id'])))[0]['capabilities'] == ['read', 'write']
    assert s.rooms.scopes(s.rooms.member(target['member_id']), s.rooms.principal(s.rooms.member(target['member_id'])))[0]['capabilities'] == ['read']
    source_message = s.room_runtime.send({'conversation_id': source_room['id'], 'body_text': '只属于原房间',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    target_message = s.room_runtime.send({'conversation_id': target_room['id'], 'body_text': '只属于新房间',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    first = s.room_consumers.inbox(identity(original), actor)
    second = s.room_consumers.inbox(identity(target), actor)
    assert {item['object_id'] for item in first['items']} == {source_message['id']}
    assert {item['object_id'] for item in second['items']} == {target_message['id']}
    s.room_consumers.ack_message(s.rooms.member(original['member_id']), source_message['id'])
    assert not s.room_consumers.inbox(identity(original), actor)['items']
    assert s.room_consumers.inbox(identity(target), actor)['items']
