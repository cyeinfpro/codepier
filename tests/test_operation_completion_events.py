"""Real Store / Runtime integration with an injected receiver; no external I/O."""
import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import OPERATION_EVENT, canonical, validate
from hub.collaboration.event_contracts import OperationFilters, OperationPayload, definitions
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.operation_events import COMPLETED
from shared.util import DevError
from tests.collaboration_support import collab  # noqa: F401


WORKSPACE = 'a' * 32


class Receiver:
    def __init__(self):
        self.requests = []
        self.status = 204
        self.on_challenge = None

    async def __call__(self, url, body, headers):
        payload = json.loads(body)
        self.requests.append(payload)
        if payload.get('type') == 'verification':
            if self.on_challenge:
                self.on_challenge()
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        return Reply(self.status)


@pytest.fixture
def harness(collab):
    service, _, worker, _, room, _, clock, _ = collab
    service.config = replace(service.config, events_enabled=True)
    receiver = Receiver()
    events = EventService(service, receiver)
    service.events = events
    service.runtime.collaboration = service
    service.store.collaboration = service
    service.store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('different','fixture',?,?)",
                          (service.store.encrypt('fixture-only'), service.clock()))
    service.store.execute("""INSERT INTO access_profiles
        (id,user_id,label,label_key,scopes,projects,enabled,version,created,updated,create_key,create_fingerprint)
        VALUES ('different','owner','fixture','fixture','["read"]','["proj"]',1,1,1,1,'fixture','fixture')""")
    service.store.execute("""INSERT INTO access_roles
        (id,user_id,label,label_key,policy,enabled,version,created,updated,create_key,create_fingerprint)
        VALUES ('different','owner','fixture','fixture','{}',1,1,1,1,'fixture','fixture')""")
    return service, events, receiver, worker, room, clock


def seed(h, identifier='operation1', *, grant='worker', state='running', project='proj', tool='read'):
    s = h[0]
    s.store.execute('''INSERT INTO operations
        (id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,
         payload,created,updated,space_id,owner_user_id,output_seq)
        VALUES (?,'dev',?,?,?, ?,?,'fixture',?,?,?,?,'legacy','owner',3)''',
        (identifier, project, 'mcp:' + grant + ':fixture', grant, tool,
         canonical({'workspace_id': WORKSPACE, 'path': '/private/path', 'command': 'private command'}),
         state, s.store.encrypt('private request payload'), s.clock(), s.clock()))
    return identifier


def arguments(*identifiers, environment='production'):
    return {'name': OPERATION_EVENT, 'arguments': {'project_id': 'proj',
            'environment_id': environment, 'operation_ids': list(identifiers)},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/fixture',
                         'secret': 'whsec_' + base64.b64encode(b'f' * 32).decode()}}


def finish(h, identifier='operation1', state='succeeded', *, seq=4):
    s, events = h[:2]
    with s.store.transaction():
        s.store.execute('UPDATE operations SET state=?,output_seq=?,payload=NULL WHERE id=?', (state, seq, identifier))
        events.operation_completed(identifier)


def outbox(h):
    return h[0].store.all('SELECT * FROM mcp_event_outbox WHERE name=? ORDER BY seq', (OPERATION_EVENT,))


@pytest.mark.asyncio
async def test_completion_wake_is_after_commit_and_never_on_rollback(harness, monkeypatch):
    s, events, _, worker, _, _ = harness
    seed(harness)
    await events.subscribe(arguments('operation1'), worker)
    wakes = []
    from hub.collaboration.lifecycle import CollaborationLoops
    monkeypatch.setattr(s, 'loops', CollaborationLoops(s), raising=False)
    monkeypatch.setattr(s.loops, 'wake_events', lambda: wakes.append('committed'))
    with pytest.raises(RuntimeError):
        with s.store.transaction():
            s.store.execute("UPDATE operations SET state='succeeded' WHERE id='operation1'")
            events.operation_completed('operation1')
            assert wakes == []
            raise RuntimeError('rollback fixture')
    assert wakes == [] and outbox(harness) == []
    finish(harness)
    assert wakes == ['committed']
    finish(harness)
    assert wakes == ['committed']


def test_catalog_and_closed_bounded_filters():
    definition = next(item for item in definitions() if item['name'] == OPERATION_EVENT)
    assert definition['delivery'] == ['webhook']
    assert definition['inputSchema']['additionalProperties'] is False
    for ids in ([], ['same', 'same'], [str(i) for i in range(17)], ['*']):
        with pytest.raises(DevError):
            validate(OperationFilters, {'project_id': 'proj', 'environment_id': 'production', 'operation_ids': ids})
    with pytest.raises(DevError):
        validate(OperationFilters, {'project_id': 'proj', 'environment_id': 'production',
                                   'operation_ids': ['original'], 'command': 'forbidden'})


@pytest.mark.asyncio
async def test_opt_in_exact_id_secret_minimization_and_accepted_only(harness):
    s, events, receiver, worker, room, _ = harness
    seed(harness)
    seed(harness, 'unwatched')
    sub = await events.subscribe(arguments('operation1'), worker)
    finish(harness, 'unwatched')
    assert outbox(harness) == []
    finish(harness)
    finish(harness)
    assert len(outbox(harness)) == 1
    payload = json.loads(outbox(harness)[0]['data'])
    assert validate(OperationPayload, payload) == payload
    assert payload['workspace_id'] == WORKSPACE
    assert payload['operation_id'] == 'operation1' and payload['output_seq'] == 4
    assert payload['next_call']['arguments']['operation_ids'] == ['operation1']
    serialized = canonical(payload)
    for value in ('private', 'command', 'password', 'payload', '/tmp', '/private', 'whsec'):
        assert value not in serialized
    await events.tick()
    row = s.store.one('SELECT * FROM mcp_event_deliveries')
    assert row['subscription_id'] == sub['id'] and row['state'] == 'accepted'
    assert len(receiver.requests) == 2
    assert s.store.one('SELECT COUNT(*) AS n FROM operations')['n'] == 2
    assert s.store.one('SELECT payload FROM operations WHERE id=?', ('operation1',))['payload'] is None


@pytest.mark.asyncio
async def test_completion_during_challenge_catches_up_and_renewal_deduplicates(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    receiver.on_challenge = lambda: finish(harness)
    await events.subscribe(arguments('operation1'), worker)
    assert len(outbox(harness)) == 1
    await events.tick()
    await events.subscribe(arguments('operation1'), worker)
    await events.tick()
    assert len(outbox(harness)) == 1 and len(receiver.requests) == 2
    assert s.store.one('SELECT COUNT(*) AS n FROM mcp_event_deliveries')['n'] == 1


@pytest.mark.asyncio
async def test_bounded_catchup_and_distinct_environment_routes(harness):
    seed(harness, state='succeeded')
    seed(harness, 'second', state='failed')
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1', 'second'), worker)
    await events.subscribe(arguments('operation1', environment='review'), worker)
    assert len(outbox(harness)) == 3
    for _ in range(3):
        await events.tick()
    notifications = [r for r in receiver.requests if 'eventId' in r]
    assert len(notifications) == 3
    assert {r['data']['environment_id'] for r in notifications} == {'production', 'review'}
    assert all(r['data']['workspace_id'] == WORKSPACE for r in notifications)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    "UPDATE operations SET grant_id='dot',visibility='space'",
    "UPDATE operations SET owner_user_id='other',visibility='space'",
    "UPDATE operations SET project_id='otherproj',visibility='space'",
    "UPDATE operations SET device_id='different'",
])
async def test_foreign_operation_denied_before_callback(harness, change):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    s.store.execute(change)
    with pytest.raises(DevError):
        await events.subscribe(arguments('operation1'), worker)
    assert receiver.requests == []
    assert s.store.all('SELECT * FROM mcp_event_subscriptions') == []


@pytest.mark.asyncio
async def test_tool_specific_access_and_read_write_grant(harness):
    seed(harness, tool='browser_action')
    s, events, receiver, worker, _, _ = harness
    with pytest.raises(DevError):
        await events.subscribe(arguments('operation1'), worker)
    assert not receiver.requests
    seed(harness, 'broad-operation', tool='exec', grant='broad')
    broad = replace(worker, actor='mcp:broad:fixture', grant_id='broad', scopes={'read', 'write', 'execute'})
    await events.subscribe(arguments('broad-operation'), broad)
    finish(harness, 'broad-operation')
    await events.tick()
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'accepted'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    "UPDATE grants SET revoked=1 WHERE id='worker'",
    "UPDATE projects SET root='/different' WHERE id='proj'",
    "UPDATE projects SET alias='different' WHERE id='proj'",
    "UPDATE projects SET device_id='different' WHERE id='proj'",
    "UPDATE grants SET role_id='different' WHERE id='worker'",
    "UPDATE grants SET profile_id='different' WHERE id='worker'",
    "UPDATE grants SET scopes='[]' WHERE id='worker'",
    "UPDATE operations SET owner_user_id='other'",
    "UPDATE operations SET args_summary='{}'",
])
async def test_live_reauthorization_blocks_reserved_delivery(harness, change):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness)
    item = events.reserve()[0]
    s.store.execute(change)
    await events.deliver(item)
    assert len(receiver.requests) == 1
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'abandoned'


@pytest.mark.asyncio
async def test_mapping_change_during_challenge_is_not_saved(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    receiver.on_challenge = lambda: s.store.execute("UPDATE projects SET root='/moved' WHERE id='proj'")
    with pytest.raises(DevError, match='身份发生变化'):
        await events.subscribe(arguments('operation1'), worker)
    assert s.store.all('SELECT * FROM mcp_event_subscriptions') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['paused', 'expired', 'unsubscribed', 'room_paused', 'disabled', 'revoked'])
async def test_no_notifications_for_inactive_or_revoked_opt_in(harness, stop):
    seed(harness)
    s, events, _, worker, _, clock = harness
    await events.subscribe(arguments('operation1'), worker)
    if stop == 'expired':
        clock[0] += 86401
    elif stop == 'room_paused':
        s.store.execute("UPDATE collaboration_rooms SET state='paused'")
    elif stop == 'disabled':
        s.config = replace(s.config, events_enabled=False)
    elif stop == 'revoked':
        s.store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    else:
        s.store.execute('UPDATE mcp_event_subscriptions SET state=?', (stop,))
    finish(harness)
    assert outbox(harness) == []


@pytest.mark.asyncio
async def test_disabled_after_reserve_and_paused_renewal(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness)
    item = events.reserve()[0]
    s.config = replace(s.config, events_enabled=False)
    await events.deliver(item)
    assert len(receiver.requests) == 1
    s.config = replace(s.config, events_enabled=True)
    s.store.execute("UPDATE mcp_event_subscriptions SET state='paused'")
    with pytest.raises(DevError, match='暂停'):
        await events.subscribe(arguments('operation1'), worker)


@pytest.mark.asyncio
async def test_cancel_ack_and_unknown_do_not_claim_completion(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    for state in ('queued', 'running', 'cancelling', 'unknown', 'reconnecting'):
        finish(harness, state=state)
        assert outbox(harness) == []
    finish(harness, state='cancelled')
    await events.tick()
    assert receiver.requests[-1]['data']['state'] == 'cancelled'
    assert s.store.one('SELECT state FROM operations')['state'] == 'cancelled'


@pytest.mark.asyncio
async def test_review_upgrades_and_stale_hint_is_abandoned(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness, state='needs_review')
    finish(harness, state='succeeded')
    assert len(outbox(harness)) == 2
    await events.tick()
    assert len(receiver.requests) == 1  # obsolete review hint is suppressed
    await events.tick()
    assert receiver.requests[-1]['data']['state'] == 'succeeded'
    assert [r['state'] for r in s.store.all('SELECT state FROM mcp_event_deliveries ORDER BY event_seq')] == ['abandoned', 'accepted']


@pytest.mark.asyncio
async def test_late_review_output_keeps_original_completion_hint(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness, state='needs_review')
    sequence = s.store.one('SELECT output_seq FROM operations')['output_seq']
    # Agent replay may carry a newer output snapshot after the result receipt.
    s.store.execute('UPDATE operations SET output_seq=output_seq+20')
    await events.tick()
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'accepted'
    payload = receiver.requests[-1]['data']
    assert payload['state'] == 'needs_review' and payload['output_seq'] == sequence
    assert payload['next_call']['arguments']['operation_ids'] == ['operation1']
    assert len(outbox(harness)) == 1


@pytest.mark.asyncio
async def test_future_output_sequence_cannot_deliver_completion_hint(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness, state='needs_review')
    s.store.execute('UPDATE operations SET output_seq=-1')
    await events.tick()
    assert len(receiver.requests) == 1
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'abandoned'


@pytest.mark.asyncio
async def test_retry_body_fence_and_unsubscribe_late_ack(harness):
    seed(harness)
    s, events, receiver, worker, _, clock = harness
    args = arguments('operation1')
    await events.subscribe(args, worker)
    finish(harness)
    receiver.status = 503
    await events.tick()
    first = receiver.requests[-1]
    clock[0] += 31
    item = events.reserve()[0]
    events.unsubscribe({**args, 'delivery': {k: v for k, v in args['delivery'].items() if k != 'secret'}}, worker)
    events.finish(item, 204)
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'abandoned'
    await events.subscribe(args, worker)
    await events.tick()
    assert receiver.requests[-1] == first  # no silent resend on re-enrollment


@pytest.mark.asyncio
async def test_panel_test_uses_original_context_without_work(harness):
    seed(harness)
    s, events, receiver, worker, room, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    sub = s.store.one('SELECT * FROM mcp_event_subscriptions')
    with s.store.transaction():
        receipt = events.queue_test(room, sub)
    await events.tick()
    payload = receiver.requests[-1]['data']
    assert payload['test'] and payload['operation_id'] == 'operation1'
    assert payload['next_call'] is None and receipt['chat_response_verified'] is False
    assert s.store.one('SELECT state FROM operations')['state'] == 'running'


@pytest.mark.asyncio
async def test_runtime_completion_and_outbox_are_one_transaction(harness, monkeypatch):
    seed(harness)
    s, events, _, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    op = s.store.one('SELECT * FROM operations')
    original = s.emit
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic outbox failure')
    monkeypatch.setattr(s, 'emit', broken)
    with pytest.raises(RuntimeError, match='synthetic'):
        s.runtime.complete(op, {'ok': True, 'data': {'output': 'private output'}})
    assert s.store.one('SELECT state,payload FROM operations')['state'] == 'running'
    assert s.store.one('SELECT payload FROM operations')['payload'] is not None
    assert outbox(harness) == []
    monkeypatch.setattr(s, 'emit', original)
    s.runtime.complete(op, {'ok': True, 'data': {'output': 'private output'}})
    assert len(outbox(harness)) == 1
    assert s.store.one('SELECT state,payload FROM operations') == {'state': 'succeeded', 'payload': None}
    s.runtime.complete(op, {'ok': True, 'data': {'output': 'private output'}})
    assert len(outbox(harness)) == 1


def test_transaction_required_and_no_optin_is_noop(harness):
    seed(harness, state='succeeded')
    with pytest.raises(RuntimeError, match='Store'):
        harness[1].operation_completed('operation1')
    with harness[0].store.transaction():
        harness[1].operation_completed('operation1')
    assert outbox(harness) == []
    assert 'unknown' not in COMPLETED and 'cancelling' not in COMPLETED


@pytest.mark.asyncio
async def test_cross_space_request_is_denied(harness):
    seed(harness)
    _, events, receiver, worker, _, _ = harness
    with pytest.raises(DevError):
        await events.subscribe(arguments('operation1'), replace(worker, space_id='different'))
    assert receiver.requests == []


@pytest.mark.asyncio
async def test_closed_resume_contract_and_payload_cannot_carry_output(harness):
    seed(harness)
    _, events, _, worker, _, _ = harness
    await events.subscribe(arguments('operation1'), worker)
    finish(harness)
    payload = json.loads(outbox(harness)[0]['data'])
    for extra in ({'output': 'secret'}, {'command': 'dangerous'}, {'next_call': None},
                  {'next_call': {'name': 'task_query', 'arguments': {'operation_ids': ['foreign']}}}):
        with pytest.raises(DevError):
            validate(OperationPayload, {**payload, **extra})


@pytest.mark.asyncio
async def test_slot_guard_is_preserved_and_not_silently_ignored(harness, monkeypatch):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    called = []
    def deny(room, principal, filters, delivery=None):
        called.append(filters['slot_id'])
        raise DevError('JOIN_SLOT_DENIED', 'fixture denial', 403)
    monkeypatch.setattr(s.joining, 'subscription_guard', deny)
    args = arguments('operation1')
    args['arguments']['slot_id'] = 'explicit-slot'
    with pytest.raises(DevError, match='fixture denial'):
        await events.subscribe(args, worker)
    assert called == ['explicit-slot'] and not receiver.requests


@pytest.mark.asyncio
async def test_profile_revision_stops_reserved_hint(harness):
    seed(harness)
    s, events, receiver, worker, _, _ = harness
    s.store.execute("UPDATE grants SET profile_id='different' WHERE id='worker'")
    worker = replace(worker, profile_id='different')
    await events.subscribe(arguments('operation1'), worker)
    finish(harness)
    item = events.reserve()[0]
    s.store.execute("UPDATE access_profiles SET version=version+1 WHERE id='different'")
    await events.deliver(item)
    assert len(receiver.requests) == 1


@pytest.mark.asyncio
async def test_exact_subscription_target_and_current_snapshot_guard(harness):
    seed(harness)
    s, events, _, worker, room, _ = harness
    one = await events.subscribe(arguments('operation1'), worker)
    second_args = arguments('operation1')
    second_args['delivery']['url'] = 'https://other.example.invalid/fixture'
    two = await events.subscribe(second_args, worker)
    finish(harness)
    event = outbox(harness)[0]
    data = json.loads(event['data'])
    for identifier in (one['id'], two['id']):
        sub = s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identifier,))
        filters = events.authorize(sub, room)
        matches = events.matches(sub, filters, event)
        assert matches == (event['object_id'] == events.operations.object_id(identifier, data))
        assert not events.matches(sub, filters, {**event, 'target_grant_id': ''})
