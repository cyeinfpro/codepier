"""Synthetic enrollment and callback tests; no real credentials, chats or grants are created."""
import asyncio
import base64
import json
import secrets
from dataclasses import replace

import pytest

from hub.collaboration.common import STATUS_EVENT, TASK_EVENT, canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.schema import migrate
from hub.principal import Principal
from shared.collaboration_contracts import tool_definitions
from shared.util import DevError
from tests.legacy_iam_fixture import seed_grant
from tests.collaboration_support import collab, key


def slot(c, kind='dot', **extra):
    s, owner, _, _, _, _, _, scope = c
    return s.joining.create({**scope, 'label': '同名 <b>位置</b>', 'kind': kind,
                            'idempotency_key': key(), **extra}, owner)['slot']


def join(c, item, principal=None, **extra):
    return c[0].invoke('collaboration_join', {'code': item['join_code'],
                       'idempotency_key': key(), **extra}, principal or c[2])


def view(c, item):
    return c[0].joining.view(c[4], c[0].joining.slot(c[4], item['id']), c[1])


def control(c, item, action, **extra):
    current = view(c, item)
    return c[0].joining.control({**c[-1], 'slot_id': item['id'], 'expected_version': current['version'],
                                'action': action, 'idempotency_key': key(), **extra}, c[1])


class Receiver:
    def __init__(self):
        self.requests = []
        self.interrupt = None

    async def __call__(self, url, body, headers):
        data = json.loads(body)
        self.requests.append((url, data, headers))
        if data.get('type') == 'verification':
            if self.interrupt:
                await self.interrupt()
            return Reply(200, canonical({'challenge': data['challenge']}).encode())
        return Reply(204)


def events(c):
    s = c[0]
    s.config = replace(s.config, events_enabled=True, analysis_dispatch_enabled=False)
    receiver = Receiver()
    s.events = EventService(s, receiver)
    return s.events, receiver


def subscribe_args(c, item, *, url='https://callback.example.invalid/joined', name=STATUS_EVENT, secret=None):
    args = {'name': name, 'arguments': {'project_id': c[4]['project_id'], 'environment_id': 'production',
            'slot_id': item['id']}, 'delivery': {'mode': 'webhook', 'url': url,
            'secret': secret or 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}
    if name == TASK_EVENT:
        args['arguments']['queue'] = 'dot-coordination' if item['kind'] == 'dot' else 'work-analysis'
    return args


def test_code_is_identifier_not_new_authority_or_worker_binding(collab):
    s = collab[0]
    before = {table: len(s.store.all('SELECT * FROM ' + table))
              for table in ('grants', 'collaboration_agents', 'collaboration_jobs', 'collaboration_secrets')}
    broad = Principal('mcp:broad:fixture', 'owner', {'read', 'write', 'execute'}, ['proj'], grant_id='broad')
    first, second = slot(collab), slot(collab)
    assert first['id'] != second['id'] and first['join_code'] != second['join_code']
    assert first['join_instruction'].startswith('@CodePier')
    for item in (first, second):
        result = join(collab, item, broad)
        assert result['registered'] and not result['permissions_changed'] and not result['worker_authorized']
        assert result['slot']['status'] == 'waiting_subscription'
        assert result['slot']['join_code'] is None and not result['chat_identity_verified']
        assert all(request['arguments']['slot_id'] == item['id'] for request in result['subscription_requests'])
    assert before == {table: len(s.store.all('SELECT * FROM ' + table)) for table in before}
    assert not s.config.analysis_dispatch_enabled and not s.config.collector_enabled
    with pytest.raises(DevError):
        s.bound_worker(broad, collab[4])
    definition = next(item for item in tool_definitions() if item['name'] == 'collaboration_join')
    assert definition['annotations']['readOnlyHint'] is False
    schema = definition['outputSchema']
    assert 'subscription_requests' in schema['required']
    assert schema['properties']['registered']['const'] is True
    assert schema['properties']['worker_authorized']['const'] is False
    assert schema['$defs']['JoinSubscriptionArguments']['required'] == ['project_id', 'environment_id', 'slot_id']
    from shared.collaboration_contracts import JoinResult
    JoinResult.model_validate(result)


def test_repeat_join_does_not_claim_a_new_chat_and_reauthorizes_before_replay(collab):
    item = slot(collab)
    stable = key()
    first = join(collab, item, idempotency_key=stable)
    second = join(collab, {**item, 'join_code': ' ' + item['join_code'].lower() + ' '}, idempotency_key=stable)
    assert first == second
    assert view(collab, item)['version'] == 2
    with pytest.raises(DevError) as error:
        join(collab, item, collab[3])
    assert error.value.code == 'JOIN_CONNECTION_MISMATCH'
    collab[0].store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    with pytest.raises(DevError):
        join(collab, item, idempotency_key=stable)


@pytest.mark.asyncio
async def test_registered_slot_can_resume_missing_subscriptions_after_code_expiry(collab):
    adapter, _ = events(collab)
    item = slot(collab, kind='work_cloud')
    result = join(collab, item)
    collab[6][0] += 1801
    current = view(collab, item)
    assert current['join_code'] is None and current['join_instruction'] is None
    instruction = current['subscription_instruction']
    assert item['id'] in instruction and item['join_code'] not in instruction
    assert 'Cloud' in instruction and 'connector_id' in instruction
    await adapter.subscribe(subscribe_args(collab, item, name=TASK_EVENT), collab[2])
    instruction = view(collab, item)['subscription_instruction']
    assert STATUS_EVENT in instruction and TASK_EVENT not in instruction
    await adapter.subscribe(subscribe_args(collab, item, name=STATUS_EVENT), collab[2])
    assert view(collab, item)['subscription_instruction'] is None
    assert len(result['subscription_requests']) == 2


@pytest.mark.parametrize('blocked', ['room_paused', 'slot_revoked', 'slot_expired', 'grant_revoked', 'events_disabled', 'subscription_paused'])
@pytest.mark.asyncio
async def test_resume_instruction_does_not_bypass_current_controls(collab, blocked):
    adapter, _ = events(collab)
    item = slot(collab)
    join(collab, item)
    if blocked == 'room_paused':
        collab[0].store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (collab[4]['id'],))
        collab[4]['state'] = 'paused'
    elif blocked == 'slot_revoked':
        control(collab, item, 'revoke')
    elif blocked == 'slot_expired':
        collab[6][0] += 8 * 86400
    elif blocked == 'grant_revoked':
        collab[0].store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    elif blocked == 'events_disabled':
        collab[0].config = replace(collab[0].config, events_enabled=False)
    else:
        subscribed = await adapter.subscribe(subscribe_args(collab, item), collab[2])
        collab[0].store.execute("UPDATE mcp_event_subscriptions SET state='paused' WHERE id=?", (subscribed['id'],))
    assert view(collab, item)['subscription_instruction'] is None


@pytest.mark.parametrize('mode', ['other_user', 'other_project', 'no_read', 'panel'])
def test_join_requires_existing_principal_space_and_project_authority(collab, mode):
    s = collab[0]
    item = slot(collab)
    if mode == 'panel':
        principal = collab[1]
    else:
        user = 'other' if mode == 'other_user' else 'owner'
        scopes = ('write',) if mode == 'no_read' else ('read',)
        projects = ('otherproj',) if mode == 'other_project' else ('proj',)
        seed_grant(s.store, 'outsider', user, scopes=scopes, projects=projects)
        principal = Principal('mcp:outsider:fixture', user, set(scopes), list(projects), grant_id='outsider')
    with pytest.raises(DevError):
        join(collab, item, principal)
    assert view(collab, item)['state'] == 'invited'
    with pytest.raises(DevError):
        s.joining.create({**collab[-1], 'label': 'cannot create', 'kind': 'dot', 'idempotency_key': key()}, collab[2])


def test_expiry_refresh_pause_revocation_and_stale_controls(collab):
    s = collab[0]
    item = slot(collab, code_ttl_minutes=5)
    collab[6][0] += 301
    assert view(collab, item)['status'] == 'code_expired'
    with pytest.raises(DevError) as expired:
        join(collab, item)
    assert expired.value.code == 'JOIN_CODE_EXPIRED'
    fresh = control(collab, item, 'refresh_code')['slot']
    assert fresh['join_code'] != item['join_code']
    with pytest.raises(DevError):
        join(collab, item)
    s.store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (collab[4]['id'],))
    with pytest.raises(DevError):
        join(collab, fresh)
    s.store.execute("UPDATE collaboration_rooms SET state='active' WHERE id=?", (collab[4]['id'],))
    join(collab, fresh)
    with pytest.raises(DevError):
        control(collab, fresh, 'refresh_code')
    with pytest.raises(DevError):
        s.joining.control({**collab[-1], 'slot_id': fresh['id'], 'expected_version': 1,
                           'action': 'revoke', 'idempotency_key': key()}, collab[1])
    control(collab, fresh, 'revoke')
    with pytest.raises(DevError):
        join(collab, fresh)
    assert view(collab, fresh)['state'] == 'revoked'


@pytest.mark.asyncio
async def test_two_concurrent_connections_cannot_take_over_one_slot(collab):
    s = collab[0]
    item = slot(collab)
    outcomes = await asyncio.gather(*(s.store.run(
        s.joining.join, {'code': item['join_code'], 'idempotency_key': key()}, principal)
        for principal in (collab[2], collab[3])), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, DevError) for value in outcomes) == 1
    assert view(collab, item)['version'] == 2


@pytest.mark.asyncio
async def test_subscription_test_and_explicit_owner_confirmation_are_separate(collab):
    s = collab[0]
    ev, receiver = events(collab)
    item = slot(collab)
    join(collab, item)
    request = subscribe_args(collab, item)
    subscribed = await ev.subscribe(request, collab[2])
    assert view(collab, item)['status'] == 'partial_subscription'
    assert len(receiver.requests) == 1
    control(collab, item, 'test')
    assert view(collab, item)['status'] == 'partial_subscription'
    await ev.tick()
    state = view(collab, item)
    assert state['status'] == 'test_delivered'
    assert state['routes'][0]['id'] == subscribed['id']
    assert not state['routes'][0]['chat_receipt_confirmed']
    assert not state['chat_identity_verified'] and not state['worker_authorized']
    with pytest.raises(DevError):
        control(collab, item, 'confirm_chat', test_event_ids=state['confirmation_event_ids'])
    control(collab, item, 'confirm_chat', test_event_ids=state['confirmation_event_ids'], confirmed_received=True)
    confirmed = view(collab, item)
    assert confirmed['status'] == 'partial_confirmed'
    assert not confirmed['connection_complete'] and len(confirmed['missing_events']) == 3
    assert confirmed['routes'][0]['confirmation_source'] == 'owner_attested'
    assert not confirmed['chat_identity_verified']
    assert s.store.all('SELECT * FROM collaboration_jobs') == []
    assert request['delivery']['secret'] not in json.dumps(confirmed)
    assert request['delivery']['url'] not in json.dumps(confirmed)


@pytest.mark.asyncio
async def test_same_grant_slots_need_independent_host_material_and_tests_never_cross(collab):
    ev, receiver = events(collab)
    first, second = slot(collab), slot(collab)
    for item in (first, second):
        join(collab, item)
    req1 = subscribe_args(collab, first)
    sub1 = await ev.subscribe(req1, collab[2])
    req2 = {**req1, 'arguments': {**req1['arguments'], 'slot_id': second['id']}}
    with pytest.raises(DevError) as shared:
        await ev.subscribe(req2, collab[2])
    assert shared.value.code == 'JOIN_ROUTE_SHARED'
    req2 = subscribe_args(collab, second, url='https://callback.example.invalid/second')
    sub2 = await ev.subscribe(req2, collab[2])
    control(collab, first, 'test')
    await ev.tick()
    payloads = [entry[1] for entry in receiver.requests if 'eventId' in entry[1]]
    assert len(payloads) == 1 and payloads[0]['data']['test_subscription_id'] == sub1['id']
    assert view(collab, second)['status'] == 'partial_subscription'
    assert sub1['id'] != sub2['id']
    with pytest.raises(DevError):
        await ev.subscribe(req1, collab[3])
    wrong_queue = subscribe_args(collab, first, name=TASK_EVENT)
    wrong_queue['arguments']['queue'] = 'work-analysis'
    with pytest.raises(DevError) as mismatch:
        await ev.subscribe(wrong_queue, collab[2])
    assert mismatch.value.code == 'JOIN_QUEUE_MISMATCH'


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['slot', 'grant', 'room'])
async def test_revocation_during_host_challenge_wins_before_subscription_commit(collab, stop):
    s = collab[0]
    ev, receiver = events(collab)
    item = slot(collab)
    join(collab, item)
    async def interrupt():
        if stop == 'slot':
            await s.store.run(control, collab, item, 'revoke')
        elif stop == 'grant':
            await s.store.run(s.store.execute, "UPDATE grants SET revoked=1 WHERE id='worker'")
        else:
            await s.store.run(s.store.execute, "UPDATE collaboration_rooms SET state='paused' WHERE id=?", (collab[4]['id'],))
    receiver.interrupt = interrupt
    with pytest.raises(DevError):
        await ev.subscribe(subscribe_args(collab, item), collab[2])
    assert s.store.all('SELECT * FROM mcp_event_subscriptions') == []
    assert s.store.all('SELECT * FROM collaboration_join_routes') == []


@pytest.mark.asyncio
async def test_rotated_key_requires_new_test_and_revoke_stops_reserved_delivery(collab):
    s = collab[0]
    ev, _ = events(collab)
    item = slot(collab)
    join(collab, item)
    request = subscribe_args(collab, item)
    original = await ev.subscribe(request, collab[2])
    control(collab, item, 'test')
    await ev.tick()
    old = view(collab, item)
    control(collab, item, 'confirm_chat', test_event_ids=old['confirmation_event_ids'], confirmed_received=True)
    rotated = {**request, 'delivery': {**request['delivery'],
               'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}
    assert (await ev.subscribe(rotated, collab[2]))['id'] == original['id']
    state = view(collab, item)
    assert state['status'] == 'partial_subscription' and state['confirmation_event_ids'] == []
    with pytest.raises(DevError):
        control(collab, item, 'confirm_chat', test_event_ids=old['confirmation_event_ids'], confirmed_received=True)
    control(collab, item, 'test')
    reserved = ev.reserve()
    assert len(reserved) == 1
    control(collab, item, 'revoke')
    assert ev.ready(reserved[0]) is None
    assert s.store.one('SELECT state FROM mcp_event_subscriptions')['state'] == 'revoked'


def test_additive_migration_and_panel_only_code_visibility(collab):
    s, owner, worker, _, room, _, _, scope = collab
    item = slot(collab)
    with s.store.transaction():
        migrate(s.store.db)
        migrate(s.store.db)
    assert view(collab, item)['join_code'] == item['join_code']
    assert s.read({**scope, 'kind': 'join_slots'}, worker)['items'][0]['join_code'] is None
    assert s.read(scope, owner)['join_slots'][0]['id'] == item['id']
    with pytest.raises(DevError):
        join(collab, {**item, 'join_code': 'CPJ-FFFF-FFFF-FFFF-FFFF'})


def test_expired_redeemed_code_allows_only_existing_idempotent_retry(collab):
    item = slot(collab, code_ttl_minutes=5)
    identifier = key()
    original = join(collab, item, idempotency_key=identifier)
    collab[6][0] += 301
    assert join(collab, item, idempotency_key=identifier)['slot']['id'] == original['slot']['id']
    with pytest.raises(DevError) as error:
        join(collab, item)
    assert error.value.code == 'JOIN_CODE_EXPIRED'
    assert view(collab, item)['state'] == 'registered'


@pytest.mark.asyncio
async def test_complete_status_requires_every_requested_event_and_owner_receipt(collab):
    ev, receiver = events(collab)
    item = slot(collab)
    result = join(collab, item)
    for template in result['subscription_requests']:
        request = subscribe_args(collab, item, name=template['name'])
        await ev.subscribe(request, collab[2])
    state = view(collab, item)
    assert state['status'] == 'subscription_verified'
    assert state['subscription_count'] == state['expected_subscription_count'] == 4
    assert state['missing_events'] == [] and not state['connection_complete']
    control(collab, item, 'test')
    for _ in range(4):
        await ev.tick()
    state = view(collab, item)
    assert len(state['confirmation_event_ids']) == 4
    with pytest.raises(DevError):
        control(collab, item, 'confirm_chat', test_event_ids=state['confirmation_event_ids'][:1], confirmed_received=True)
    control(collab, item, 'confirm_chat', test_event_ids=state['confirmation_event_ids'], confirmed_received=True)
    assert view(collab, item)['status'] == 'chat_confirmed'
    assert view(collab, item)['connection_complete']
    assert not view(collab, item)['chat_identity_verified']


@pytest.mark.asyncio
async def test_concurrent_callback_binding_cannot_alias_two_slots(collab):
    s = collab[0]
    ev, _ = events(collab)
    first, second = slot(collab), slot(collab)
    join(collab, first)
    join(collab, second)
    shared = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
    results = await asyncio.gather(*(ev.subscribe(subscribe_args(collab, item, secret=shared), collab[2])
                                    for item in (first, second)), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, DevError) for item in results) == 1
    assert len(s.store.all('SELECT * FROM collaboration_join_routes')) == 1
    assert len(s.store.all('SELECT * FROM mcp_event_subscriptions')) == 1


@pytest.mark.asyncio
async def test_slot_cannot_cross_environment_and_expiry_blocks_delivery(collab):
    s = collab[0]
    ev, _ = events(collab)
    item = slot(collab)
    join(collab, item)
    other = subscribe_args(collab, item)
    other['arguments']['environment_id'] = 'staging'
    with pytest.raises(DevError):
        await ev.subscribe(other, collab[2])
    assert len(s.store.all('SELECT * FROM collaboration_rooms')) == 1
    await ev.subscribe(subscribe_args(collab, item), collab[2])
    control(collab, item, 'test')
    collab[6][0] += 8 * 86400
    assert ev.reserve() == []
    assert view(collab, item)['state'] == 'expired'
    assert not view(collab, item)['connection_complete']


def test_slot_limit_reclaims_only_expired_or_revoked_positions(collab):
    rows = [slot(collab) for _ in range(16)]
    with pytest.raises(DevError) as limit:
        slot(collab)
    assert limit.value.code == 'JOIN_SLOT_LIMIT'
    control(collab, rows[0], 'revoke')
    assert slot(collab)['state'] == 'invited'


@pytest.mark.asyncio
async def test_route_limit_rolls_back_the_ninth_subscription_atomically(collab):
    s = collab[0]
    ev, _ = events(collab)
    item = slot(collab)
    join(collab, item)
    base = subscribe_args(collab, item)
    for index in range(8):
        request = {**base, 'arguments': {**base['arguments'], 'rule_ids': ['rule-' + str(index)]}}
        await ev.subscribe(request, collab[2])
    with pytest.raises(DevError) as limit:
        await ev.subscribe({**base, 'arguments': {**base['arguments'], 'rule_ids': ['rule-9']}}, collab[2])
    assert limit.value.code == 'JOIN_ROUTE_LIMIT'
    assert len(s.store.all('SELECT * FROM collaboration_join_routes')) == 8
    assert len(s.store.all('SELECT * FROM mcp_event_subscriptions')) == 8
