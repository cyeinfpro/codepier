"""Room and member invariants with isolated synthetic native subscriptions."""
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from shared.collaboration_contracts import JoinCode
from shared.public_collaboration import resolve
from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.rooms import Rooms, migrate
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import save_room_permissions


@pytest.fixture
def rooms(collab):
    service, owner, worker, _, _, _, clock, _ = collab
    with service.store.transaction():
        migrate(service.store.db)
    service.rooms = Rooms(service)
    service.config = replace(service.config, events_enabled=True)
    return service, owner, worker, clock


def create(service, owner, projects=None):
    room = service.rooms.create({'title': '工作房间', 'idempotency_key': key()}, owner)['room']
    save_room_permissions(service, owner, room['id'], [
        {'project_id': item['project'], 'environment_id': item.get('environment_id', 'production'),
         'capabilities': ['read']} for item in projects or []])
    return service.rooms.read({'conversation_id': room['id']}, owner)['room']


def add(service, owner, room, name='助手'):
    return service.rooms.create_member({'conversation_id': room['id'], 'name': name, 'idempotency_key': key()}, owner)


def join(service, worker, created):
    flow = service.store.one('SELECT * FROM room_connect_flows WHERE id=?', (created['flow']['id'],))
    flow = service.rooms.pending.current(flow['member_id']) or flow
    args = {'invite_code': service.store.decrypt(flow['invite_secret']), 'idempotency_key': key()}
    return service.rooms.join(args, worker), args


async def subscribe(service, worker, joined, route='a'):
    received = []
    async def receiver(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') != 'verification':
            received.append(payload)
        return Reply(200, canonical({'challenge': payload['challenge']}).encode()) if payload.get('type') == 'verification' else Reply(204)
    service.events = EventService(service, receiver)
    raw = {**joined['subscription_request'], 'delivery': {'mode': 'webhook',
        'url': 'https://callback.example.invalid/room/' + route,
        'secret': 'whsec_' + base64.b64encode(b'r' * 32).decode()}}
    result = await service.events.subscribe(raw, worker)
    await service.events.tick()
    payloads = [item['data'] for item in received if item.get('data', {}).get('member_id') == joined['member_id']
        and item['data'].get('binding_generation') == joined['binding_generation']]
    assert payloads and payloads[0]['wake_ticket']
    joined['wake_ticket'] = payloads[0]['wake_ticket']
    return result


def identity(joined):
    return {k: joined[k] for k in ('conversation_id', 'member_id', 'binding_generation', 'connection_proof')}



def invitation(service, owner, created):
    instructions = service.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['join_instructions']
    request, _ = json.JSONDecoder().raw_decode(instructions[instructions.index('{'):])
    return request, instructions


def room_join_canonical(request):
    return {'action': 'room_connect_join', 'invite_code': request['code'],
            'idempotency_key': request['idempotency_key']}


@pytest.mark.parametrize('code', ['CPR-AbC_xyz-09' + 'aB' * 16, 'CPR-' + 'a' * 43])
def test_room_code_preserves_case_in_cached_and_canonical_contracts(code):
    raw = {'code': code, 'idempotency_key': key()}
    assert JoinCode.model_validate(raw).code == code
    target, args = resolve('collaboration', {'action': 'join', **raw})
    assert target == 'collaboration_join'
    assert args == raw


@pytest.mark.parametrize('code', ['CPR-short', 'CPR-' + 'a' * 81,
    'CPR-' + 'a b' * 14, 'CPR-' + 'a/b' * 14, ' CPR-' + 'a' * 43, 'cpr-' + 'a' * 43])
def test_room_code_never_uses_legacy_whitespace_or_case_normalization(code):
    with pytest.raises(ValidationError):
        JoinCode.model_validate({'code': code, 'idempotency_key': key()})


@pytest.mark.parametrize('prefix', ['cpj', 'cpd'])
def test_legacy_hex_code_normalization_is_preserved(prefix):
    code = prefix + '- abcd-0123-4567-89ef '
    assert JoinCode.model_validate({'code': code, 'idempotency_key': key()}).code == prefix.upper() + '-ABCD-0123-4567-89EF'


def test_generated_invitation_fits_cached_catalog_without_new_fields(rooms):
    s, owner, _, _ = rooms
    request, instructions = invitation(s, owner, add(s, owner, create(s, owner)))
    Draft202012Validator({'type': 'object', 'additionalProperties': False,
        'properties': {'action': {'enum': ['join']}, 'code': {'type': 'string', 'maxLength': 80},
            'idempotency_key': {'type': 'string'}},
        'required': ['action', 'code', 'idempotency_key']}).validate(request)
    assert request['code'].startswith('CPR-')
    assert 'Rescan' in instructions and '原生订阅' in instructions
    assert '不要换成旧任务协议' in instructions
    for extra in ({'invite_code': request['code']}, {'capabilities': ['execute']}, {'room_id': 'other'}):
        with pytest.raises(DevError):
            resolve('collaboration', {**request, **extra})


@pytest.mark.parametrize('first_new', [False, True])
def test_old_and_new_actions_share_room_proof_and_idempotency(rooms, first_new):
    s, owner, worker, _ = rooms
    request, _ = invitation(s, owner, add(s, owner, create(s, owner)))
    first, second = (room_join_canonical(request), request) if first_new else (request, room_join_canonical(request))
    joined = s.invoke('collaboration', first, worker)
    replay = s.invoke('collaboration', second, worker)
    assert replay['connection_proof'] == joined['connection_proof']
    assert replay['member_id'] == joined['member_id']
    legacy = s.invoke('collaboration_join', {k: v for k, v in request.items() if k != 'action'}, worker)
    assert legacy['connection_proof'] == joined['connection_proof']
    assert s.store.one('SELECT COUNT(*) n FROM room_members')['n'] == 1
    assert joined['member']['state'] == 'waiting_host'
    assert joined['grants_authority'] is False
    assert not s.store.one('SELECT 1 FROM mcp_event_subscriptions')
    assert not s.store.one('SELECT 1 FROM dot_consumer_sessions')
    assert 'room_probe_read' in joined['required_host_actions']['collaboration']
    assert 'Rescan' in joined['next_step']
    assert '不伪造 callback' in joined['next_step']
    for raw in (request, room_join_canonical(request)):
        with pytest.raises(DevError) as caught:
            s.invoke('collaboration', {**raw, 'idempotency_key': key()}, worker)
        assert caught.value.code == 'ROOM_INVITE_REDEEMED'


@pytest.mark.parametrize('condition', ['expired', 'revoked', 'other_owner'])
def test_compatibility_route_preserves_canonical_denials(rooms, condition):
    s, owner, worker, clock = rooms
    created = add(s, owner, create(s, owner))
    request, _ = invitation(s, owner, created)
    if condition == 'expired':
        clock[0] += 1801
    elif condition == 'revoked':
        s.store.execute('UPDATE room_connect_flows SET revoked_at=? WHERE id=?',
                        (clock[0], created['flow']['id']))
    else:
        worker = replace(worker, user_id='different-owner')
    codes = []
    for raw in (request, room_join_canonical(request)):
        with pytest.raises(DevError) as caught:
            s.invoke('collaboration', raw, worker)
        codes.append(caught.value.code)
    assert codes[0] == codes[1]
    assert not s.store.one('SELECT 1 FROM room_members WHERE grant_id != ?', ('',))


@pytest.mark.asyncio
async def test_compatibility_join_still_requires_real_synthetic_subscription_and_probe(rooms):
    s, owner, worker, _ = rooms
    request, _ = invitation(s, owner, add(s, owner, create(s, owner)))
    joined = s.invoke('collaboration', request, worker)
    assert joined['member']['state'] == 'waiting_host'
    await subscribe(s, worker, joined)
    nonce = s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'],
        'idempotency_key': key()}, worker)['probe_nonce']
    result = s.rooms.probe_reply({**identity(joined), 'probe_nonce': nonce,
        'idempotency_key': key()}, worker)
    assert result['member']['state'] == 'ready_chat'
    replay = s.invoke('collaboration', request, worker)
    assert replay['member']['state'] == 'ready_chat'
    assert replay['connection_proof'] == joined['connection_proof']



def test_empty_room_idempotency_and_current_visibility(rooms):
    s, owner, worker, _ = rooms
    raw = {'title': '空房间', 'projects': [], 'idempotency_key': key()}
    room = s.rooms.create(raw, owner)['room']
    assert room['projects'] == []
    assert s.rooms.create(raw, owner)['room']['id'] == room['id']
    assert any(r['id'] == room['id'] for r in s.rooms.listing({}, owner)['items'])
    with pytest.raises(DevError):
        s.rooms.create({**raw, 'title': '不同内容'}, owner)
    with pytest.raises(DevError):
        s.rooms.read({'conversation_id': room['id']}, worker)


def test_remove_last_project_survives_repeated_migration(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner, [{'project': 'otherproj', 'environment_id': 'production'}])
    partition = room['projects'][0]['room_id']
    updated = save_room_permissions(s, owner, room['id'], [])
    assert not updated['scope']['project_scopes']
    from hub.collaboration.conversations import migrate as migrate_conversations
    with s.store.transaction():
        migrate_conversations(s.store.db)
        migrate_conversations(s.store.db)
    assert not s.rooms.read({'conversation_id': room['id']}, owner)['room']['projects']
    assert not s.store.one('SELECT 1 FROM conversation_rooms WHERE id=?', (partition,))
    assert s.store.one('SELECT 1 FROM projects WHERE id=?', ('otherproj',))


def test_invite_expiry_refresh_preserves_identity_and_disallows_old_code(rooms):
    s, owner, worker, clock = rooms
    room = create(s, owner)
    created = add(s, owner, room)
    old = s.store.one('SELECT * FROM room_connect_flows WHERE id=?', (created['flow']['id'],))
    old_code = s.store.decrypt(old['invite_secret'])
    clock[0] += 1801
    with pytest.raises(DevError) as caught:
        s.rooms.join({'invite_code': old_code, 'idempotency_key': key()}, worker)
    assert caught.value.code == 'ROOM_INVITE_EXPIRED'
    renewed = s.rooms.connect_action({'flow_id': old['id'], 'action': 'refresh_invite',
        'expected_version': old['version'], 'idempotency_key': key()}, owner)
    assert renewed['member']['id'] == created['member']['id']
    assert renewed['flow']['id'] == old['id']
    with pytest.raises(DevError):
        s.rooms.join({'invite_code': old_code, 'idempotency_key': key()}, worker)
    joined, raw = join(s, worker, renewed)
    assert s.rooms.join(raw, worker)['connection_proof'] == joined['connection_proof']
    assert joined['connection_proof'] not in canonical(s.store.one('SELECT * FROM room_members'))
    assert joined['connection_proof'] not in canonical(s.rooms.connect_read({'flow_id': old['id']}, owner))


@pytest.mark.asyncio
async def test_ready_requires_subscription_current_nonce_read_and_reply(rooms):
    s, owner, worker, _ = rooms
    created = add(s, owner, create(s, owner))
    joined, _ = join(s, worker, created)
    assert joined['member']['state'] == 'waiting_host'
    with pytest.raises(DevError):
        s.rooms.probe_read({**identity(joined), 'wake_ticket': 'x' * 40, 'idempotency_key': key()}, worker)
    sub = await subscribe(s, worker, joined)
    assert s.rooms.member_view(s.rooms.member(joined['member_id']))['state'] == 'verifying'
    with pytest.raises(DevError):
        s.rooms.probe_reply({**identity(joined), 'probe_nonce': 'x' * 32, 'idempotency_key': key()}, worker)
    nonce = s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key()}, worker)['probe_nonce']
    result = s.rooms.probe_reply({**identity(joined), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)
    assert result['member']['state'] == 'ready_chat'
    assert not result['business_task_created']
    assert not result['consumer_lease_created']
    from shared.public_collaboration import resolve
    request = result['next_request']
    target, arguments = resolve(request['tool'], request['arguments'])
    assert target == 'collaboration_room_inbox'
    assert {name: arguments[name] for name in identity(joined)} == identity(joined)
    assert not s.store.one('SELECT 1 FROM dot_consumer_sessions')
    assert s.store.one('SELECT COUNT(*) n FROM collaboration_jobs')['n'] == 0
    s.store.execute("UPDATE mcp_event_subscriptions SET state='unsubscribed' WHERE id=?", (sub['id'],))
    assert s.rooms.member_view(s.rooms.member(joined['member_id']))['state'] == 'degraded'


@pytest.mark.asyncio
async def test_shared_grant_members_cannot_use_each_others_proof_or_route(rooms):
    s, owner, worker, _ = rooms
    room = create(s, owner)
    first, _ = join(s, worker, add(s, owner, room))
    second, _ = join(s, worker, add(s, owner, room))
    await subscribe(s, worker, first)
    with pytest.raises(DevError) as caught:
        await subscribe(s, worker, second)
    assert caught.value.code == 'ROOM_ROUTE_SHARED'
    await subscribe(s, worker, second, 'b')
    with pytest.raises(DevError) as caught:
        s.rooms.probe_read({**identity(second), 'connection_proof': first['connection_proof'], 'wake_ticket': second['wake_ticket'], 'idempotency_key': key()}, worker)
    assert caught.value.code == 'ROOM_MEMBER_PROOF_REQUIRED'


@pytest.mark.asyncio
async def test_shared_grant_route_registration_without_member_proof_never_becomes_ready(rooms):
    s, owner, worker, _ = rooms
    legitimate, _ = join(s, worker, add(s, owner, create(s, owner)))
    # The native event API authenticates a grant, so public IDs permit a caller
    # sharing that grant to claim a route before the intended host registers it.
    # This is a recoverable setup denial, never a member or work capability.
    await subscribe(s, worker, legitimate, 'unexpected-host')
    guessed = {**identity(legitimate), 'connection_proof': 'unrelated-member-proof-' + 'x' * 32}
    with pytest.raises(DevError) as read:
        s.rooms.probe_read({**guessed, 'wake_ticket': legitimate['wake_ticket'], 'idempotency_key': key()}, worker)
    assert read.value.code == 'ROOM_MEMBER_PROOF_REQUIRED'
    with pytest.raises(DevError) as reply:
        s.rooms.probe_reply({**guessed, 'probe_nonce': 'x' * 40, 'idempotency_key': key()}, worker)
    assert reply.value.code == 'ROOM_MEMBER_PROOF_REQUIRED'
    with pytest.raises(DevError) as consume:
        s.room_consumers.begin({**guessed, 'wake_ticket': legitimate['wake_ticket'],
            'consumer_run_id': key(), 'idempotency_key': key()}, worker)
    assert consume.value.code == 'ROOM_MEMBER_PROOF_REQUIRED'
    assert s.rooms.state(s.rooms.member(legitimate['member_id'])) == 'verifying'
    assert not s.store.one('SELECT 1 FROM dot_consumer_sessions WHERE member_id=?', (legitimate['member_id'],))
    assert not s.store.one('SELECT 1 FROM coordination_goals')
    with pytest.raises(DevError) as host_change:
        await subscribe(s, worker, legitimate, 'intended-host')
    assert host_change.value.code == 'ROOM_HOST_CHANGED'


@pytest.mark.asyncio
async def test_paused_member_keeps_its_reserved_host_route_until_explicit_rebind_or_removal(rooms):
    s, owner, worker, _ = rooms
    room = create(s, owner)
    created = add(s, owner, room)
    original, _ = join(s, worker, created)
    other, _ = join(s, worker, add(s, owner, room, 'other'))
    await subscribe(s, worker, original, 'reserved')
    flow = s.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['flow']
    s.rooms.connect_action({'flow_id': flow['id'], 'action': 'pause', 'expected_version': flow['version'],
        'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as caught:
        await subscribe(s, worker, other, 'reserved')
    assert caught.value.code == 'ROOM_ROUTE_SHARED'


@pytest.mark.asyncio
async def test_rebind_fences_old_proof_and_preserves_member(rooms):
    s, owner, worker, _ = rooms
    created = add(s, owner, create(s, owner))
    joined, old_join = join(s, worker, created)
    old_sub = await subscribe(s, worker, joined)
    nonce = s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key()}, worker)['probe_nonce']
    s.rooms.probe_reply({**identity(joined), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)
    lease = s.room_consumers.begin({**identity(joined), 'wake_ticket': joined['wake_ticket'],
        'consumer_run_id': key(), 'idempotency_key': key()}, worker)
    current = s.rooms.connect_read({'flow_id': created['flow']['id']}, owner)
    replaced = s.rooms.connect_action({'flow_id': current['flow']['id'], 'action': 'rebind',
        'expected_version': current['flow']['version'], 'idempotency_key': key()}, owner)
    assert replaced['member']['id'] == joined['member_id']
    assert replaced['member']['binding_generation'] == joined['binding_generation']
    assert replaced['member']['state'] == 'ready_chat'
    assert replaced['flow']['pending_binding']
    assert replaced['flow']['candidate_binding_generation'] == joined['binding_generation'] + 1
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'active'
    assert s.store.one('SELECT state FROM dot_consumer_sessions WHERE id=?', (lease['consumer_session_id'],))['state'] == 'active'
    candidate, candidate_join = join(s, worker, replaced)
    candidate_sub = await subscribe(s, worker, candidate, 'replacement')
    with pytest.raises(DevError):
        s.room_consumers.begin({**identity(candidate), 'wake_ticket': candidate['wake_ticket'],
            'consumer_run_id': key(), 'idempotency_key': key()}, worker)
    with pytest.raises(DevError):
        s.rooms.probe_reply({**identity(candidate), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)
    assert s.rooms.member(joined['member_id'])['binding_generation'] == joined['binding_generation']
    assert s.rooms.member_scope(worker, **identity(joined), require_proof=True)[2]['subscription_id'] == old_sub['id']
    candidate_nonce = s.rooms.probe_read({**identity(candidate), 'wake_ticket': candidate['wake_ticket'], 'idempotency_key': key()}, worker)['probe_nonce']
    result = s.rooms.probe_reply({**identity(candidate), 'probe_nonce': candidate_nonce, 'idempotency_key': key()}, worker)
    assert result['binding_activated']
    assert result['member']['binding_generation'] == candidate['binding_generation']
    assert result['member']['state'] == 'ready_chat'
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'unsubscribed'
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (candidate_sub['id'],))['state'] == 'active'
    assert s.store.one('SELECT state FROM dot_consumer_sessions WHERE id=?', (lease['consumer_session_id'],))['state'] == 'expired'
    assert s.rooms.join(candidate_join, worker)['connection_proof'] == candidate['connection_proof']
    with pytest.raises(DevError):
        s.rooms.join(old_join, worker)
    with pytest.raises(DevError):
        s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key()}, worker)


@pytest.mark.asyncio
async def test_rebind_failure_rolls_back_cutover_and_keeps_live_route(rooms, monkeypatch):
    s, owner, worker, _ = rooms
    created = add(s, owner, create(s, owner))
    joined, _ = join(s, worker, created)
    old_sub = await subscribe(s, worker, joined)
    old = s.rooms.member(joined['member_id'])
    flow = s.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['flow']
    replacement = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    candidate, _ = join(s, worker, replacement)
    await subscribe(s, worker, candidate, 'candidate')
    nonce = s.rooms.probe_read({**identity(candidate), 'wake_ticket': candidate['wake_ticket'], 'idempotency_key': key()}, worker)['probe_nonce']
    real_ensure = s.room_work_bridge.ensure
    def unavailable(*args):
        raise DevError('TEST_BRIDGE_UNAVAILABLE', 'synthetic failure before commit', 409)
    monkeypatch.setattr(s.room_work_bridge, 'ensure', unavailable)
    with pytest.raises(DevError) as caught:
        s.rooms.probe_reply({**identity(candidate), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)
    assert caught.value.code == 'TEST_BRIDGE_UNAVAILABLE'
    current = s.rooms.member(joined['member_id'])
    for name in ('binding_generation', 'grant_id', 'connection_proof_hash', 'subscription_id', 'probe_nonce_hash'):
        assert current[name] == old[name]
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'active'
    assert s.rooms.member_scope(worker, **identity(joined), require_proof=True)[2]['id'] == old['id']
    monkeypatch.setattr(s.room_work_bridge, 'ensure', real_ensure)
    assert s.rooms.probe_reply({**identity(candidate), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)['binding_activated']


@pytest.mark.asyncio
async def test_expired_or_superseded_candidate_never_revokes_live_binding(rooms):
    s, owner, worker, clock = rooms
    created = add(s, owner, create(s, owner))
    joined, _ = join(s, worker, created)
    old_sub = await subscribe(s, worker, joined)
    flow = s.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['flow']
    replacement = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    candidate, candidate_join = join(s, worker, replacement)
    clock[0] += 3601
    with pytest.raises(DevError):
        s.rooms.join(candidate_join, worker)
    assert s.rooms.member_scope(worker, **identity(joined), require_proof=True)[2]['binding_generation'] == joined['binding_generation']
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'active'
    flow = s.rooms.connect_read({'flow_id': flow['id']}, owner)['flow']
    replacement = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    new_candidate, _ = join(s, worker, replacement)
    assert new_candidate['binding_generation'] > candidate['binding_generation']
    assert s.rooms.member(joined['member_id'])['binding_generation'] == joined['binding_generation']
    with pytest.raises(DevError):
        s.rooms.member_scope(worker, **identity(candidate), pending=True, require_proof=True)


@pytest.mark.asyncio
async def test_full_room_allows_one_verification_candidate_without_displacing_other_members(rooms):
    s, owner, worker, clock = rooms
    room = create(s, owner)
    current = []
    for index in range(32):
        # Respect production per-owner challenge and send budgets. Advancing
        # this deterministic regression clock is not the real-time load test.
        clock[0] += 61
        created = add(s, owner, room, 'member-' + str(index))
        joined, _ = join(s, worker, created)
        subscription = await subscribe(s, worker, joined, 'full-' + str(index))
        probe = s.rooms.probe_read({**identity(joined), 'wake_ticket': joined['wake_ticket'], 'idempotency_key': key()}, worker)
        s.rooms.probe_reply({**identity(joined), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, worker)
        current.append((created, joined, subscription))
    assert s.store.one("SELECT COUNT(*) n FROM mcp_event_subscriptions WHERE conversation_id=? AND state='active'", (room['id'],))['n'] == 32
    created, original, old_sub = current[0]
    flow = s.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['flow']
    replacement = s.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    candidate, _ = join(s, worker, replacement)
    clock[0] += 61
    await subscribe(s, worker, candidate, 'full-candidate')
    assert s.store.one("SELECT COUNT(*) n FROM mcp_event_subscriptions WHERE conversation_id=? AND state='active'", (room['id'],))['n'] == 33
    assert s.rooms.member(original['member_id'])['binding_generation'] == original['binding_generation']
    probe = s.rooms.probe_read({**identity(candidate), 'wake_ticket': candidate['wake_ticket'], 'idempotency_key': key()}, worker)
    s.rooms.probe_reply({**identity(candidate), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, worker)
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'unsubscribed'
    for _, joined, subscription in current[1:]:
        assert s.rooms.member(joined['member_id'])['binding_generation'] == joined['binding_generation']
        assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (subscription['id'],))['state'] == 'active'
        assert s.rooms.state(s.rooms.member(joined['member_id'])) == 'ready_chat'
    assert s.store.one("SELECT COUNT(*) n FROM mcp_event_subscriptions WHERE conversation_id=? AND state='active'", (room['id'],))['n'] == 32


def test_project_association_never_approves_member_scope(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner)
    created = add(s, owner, room)
    current = s.rooms.read({'conversation_id': room['id']}, owner)['room']
    with pytest.raises(DevError) as denied:
        s.rooms.projects({'conversation_id': room['id'], 'expected_version': current['version'],
            'projects': [{'project': 'proj', 'environment_id': 'production'}], 'idempotency_key': key()}, owner)
    assert denied.value.code == 'ROOM_PERMISSION_SETTINGS_REQUIRED'
    assert s.rooms.scopes(s.rooms.member(created['member']['id'])) == []


def test_removed_member_never_resumes_or_renews_invite(rooms):
    s, owner, _, _ = rooms
    created = add(s, owner, create(s, owner))
    result = s.rooms.connect_action({'flow_id': created['flow']['id'], 'action': 'remove',
        'expected_version': created['flow']['version'], 'idempotency_key': key()}, owner)
    assert result['member']['state'] == 'removed'
    assert 'join_instructions' not in result
    with pytest.raises(DevError) as caught:
        s.rooms.connect_action({'flow_id': result['flow']['id'], 'action': 'resume',
            'expected_version': result['flow']['version'], 'idempotency_key': key()}, owner)
    assert caught.value.code == 'ROOM_MEMBER_REMOVED'


def test_project_removal_requires_explicit_canonical_reapproval(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner, [{'project': 'proj', 'environment_id': 'production'}])
    created = s.rooms.create_member({'conversation_id': room['id'], 'name': '研究助手',
        'idempotency_key': key()}, owner)
    member_id = created['member']['id']
    assert s.rooms.scopes(s.rooms.member(member_id))[0]['capabilities'] == ['read']
    save_room_permissions(s, owner, room['id'], [])
    assert not s.rooms.scopes(s.rooms.member(member_id))[0]['capabilities']
    with s.store.transaction():
        migrate(s.store.db)
    assert not s.rooms.scopes(s.rooms.member(member_id))[0]['capabilities']
    save_room_permissions(s, owner, room['id'], [{'project_id': 'proj', 'capabilities': ['read']}])
    assert s.rooms.scopes(s.rooms.member(member_id))[0]['capabilities'] == ['read']
    assert s.store.one('SELECT COUNT(*) n FROM room_member_scope_revisions')['n'] == 3


def test_resource_change_only_invalidates_affected_approval_and_never_recreates_member(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner, [{'project': 'proj', 'environment_id': 'production'}])
    created = s.rooms.create_member({'conversation_id': room['id'], 'name': '研究助手',
        'idempotency_key': key()}, owner)
    member_id = created['member']['id']
    before = s.store.one('SELECT COUNT(*) n FROM room_members')['n']
    s.store.execute("UPDATE projects SET root='/tmp/changed-project' WHERE id='proj'")
    assert s.rooms.scopes(s.rooms.member(member_id))[0]['state'] == 'unavailable'
    with s.store.transaction():
        migrate(s.store.db)
    assert s.store.one('SELECT COUNT(*) n FROM room_members')['n'] == before
    assert s.rooms.member(member_id)['id'] == member_id


def test_room_hint_is_read_only_and_content_free(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner)
    before = s.store.one('SELECT total_changes() n')['n']
    hint = s.rooms.hint_snapshot(room['id'], owner)
    assert s.store.one('SELECT total_changes() n')['n'] == before
    assert set(hint) == {'revision', 'access', 'owner'}
    assert '工作房间' not in canonical(hint)


@pytest.mark.asyncio
async def test_room_hint_rechecks_session_and_releases_bounded_subscriber(rooms):
    s, owner, _, _ = rooms
    room = create(s, owner)
    initial = s.rooms.hint_snapshot(room['id'], owner)
    queue = s.relay_hints.subscribe(initial['owner'])
    async def connected():
        return False
    actor = [owner]
    auth = SimpleNamespace(panel=lambda request: actor[0])
    stream = s.rooms.hint_stream(SimpleNamespace(is_disconnected=connected), room['id'], auth, queue, initial)
    first = await anext(stream)
    assert 'event: change' in first and '工作房间' not in first
    actor[0] = replace(owner, user_id='other')
    queue.put_nowait(None)
    assert await anext(stream) == 'event: access_changed\ndata: {}\n\n'
    await stream.aclose()
    assert queue not in s.relay_hints._subscribers
    assert queue not in s.runtime.watchers
