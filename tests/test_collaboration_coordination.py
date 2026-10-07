"""Goal authority, bounded coordination and real durable admission in isolated Stores.

No personal grants, live subscriptions or native model calls are used.
"""
import asyncio
import json
from dataclasses import replace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401


def setup_goal(collab, *, capabilities=None, participants=None, approve=True, budget=None):
    service, owner, worker, dot, room, agents, clock, scope = collab
    service.runtime.collaboration = service
    coordinator = service.coordination
    raw = {**scope, 'conversation_id': room['id'], 'objective': 'Inspect, discuss, edit and review the selected project.',
           'acceptance': 'Record actual operations and review limitations.', 'project_ids': ['proj'],
           'participant_grant_ids': participants or ['worker', 'broad'], 'capabilities': capabilities or ['read', 'write', 'execute'],
           'idempotency_key': key()}
    if budget:
        raw['budget'] = budget
    goal = coordinator.create(raw, owner)['goal']
    if approve:
        goal = coordinator.approve({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                                   'digest': goal['digest'], 'idempotency_key': key()}, owner)['goal']
    broad = replace(worker, grant_id='broad', actor='mcp:broad:fixture')
    return coordinator, service, owner, worker, broad, goal, scope, raw


def make_work(c, scope, goal, actor, *, assignee='broad', caps=None, **extra):
    return c.work_create({**scope, 'goal_id': goal['id'], 'objective': 'Flexible custom objective',
        'acceptance': 'Demonstrate the requested outcome', 'assignee_grant_id': assignee,
        'target_project_id': 'proj', 'required_capabilities': caps or ['read', 'execute'],
        'idempotency_key': key(), **extra}, actor)['work_item']


def claim(c, scope, goal, item, actor):
    return c.work_claim({**scope, 'goal_id': goal['id'], 'work_item_id': item['id'],
                        'expected_version': item['version'], 'idempotency_key': key()}, actor)['work_item']


def lease(scope, goal, item):
    return {**scope, 'goal_id': goal['id'], 'work_item_id': item['id'],
            'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'idempotency_key': key()}


def expect(code, function, *args, **kwargs):
    with pytest.raises(DevError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_exact_owner_approval_and_readonly_participant(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, approve=False)
    approval = {**scope, 'goal_id': goal['id'], 'expected_version': goal['version'], 'digest': goal['digest'], 'idempotency_key': key()}
    expect('OWNER_REQUIRED', c.approve, approval, broad)
    expect('GOAL_APPROVAL_CHANGED', c.approve, {**approval, 'digest': '0' * 64}, owner)
    approved = c.approve(approval, owner)['goal']
    assert approved['state'] == 'active'
    assert c.approve(approval, owner) == {'goal': approved}
    assert approved['execution_boundary']['shell'] == 'execution_account_not_project_sandbox'
    reviewer = make_work(c, scope, approved, worker, assignee='worker', caps=['read'])
    assert claim(c, scope, approved, reviewer, worker)['attempt'] == 1
    expect('INSUFFICIENT_SCOPE', make_work, c, scope, approved, worker, assignee='worker')
    assert service.store.one("SELECT scopes FROM grants WHERE id='worker'")['scopes'] == '["read"]'


def test_mcp_cannot_activate_or_fake_approved_boolean(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, approve=False)
    expect('INVALID_ARGUMENTS', c.create, {**raw, 'approved': True}, broad)
    expect('UNKNOWN_TOOL', service.invoke, 'collaboration_goal_approve', {}, broad)
    expect('GOAL_INACTIVE', make_work, c, scope, goal, broad)
    assert service.store.one('SELECT COUNT(*) AS n FROM coordination_approvals')['n'] == 0


def test_work_dependencies_handoff_and_budget(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, budget={'max_work_items': 3})
    first = make_work(c, scope, goal, worker, caps=['read'], assignee='worker')
    second = make_work(c, scope, goal, worker, dependencies=[first['id']])
    expect('WORK_DEPENDENCY_PENDING', claim, c, scope, goal, second, broad)
    expect('GOAL_BUDGET_EXCEEDED', make_work, c, scope, goal, broad)
    first = claim(c, scope, goal, first, worker)
    c.result({**lease(scope, goal, first), 'outcome': 'succeeded', 'summary': 'Planning complete'}, worker)
    second = claim(c, scope, goal, second, broad)
    assert second['state'] == 'leased'
    expect('WORK_NOT_ASSIGNABLE', c.work_assign, {**scope, 'goal_id': goal['id'], 'work_item_id': second['id'],
        'expected_version': second['version'], 'assignee_grant_id': 'broad', 'idempotency_key': key()}, broad)


def test_actual_durable_operation_and_fabricated_result_rejected(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, worker), broad)
    command = {**lease(scope, goal, item), 'tool': 'exec',
               'arguments': {'command': 'printf coordination-test', 'timeout_seconds': 10}}
    receipt = asyncio.run(service.runtime.invoke('collaboration_work_execute', command, broad))
    operation_id = receipt['operation_id']
    assert receipt['pending'] and receipt['state'] == 'queued'
    assert service.store.one('SELECT grant_id,project_id FROM operations WHERE id=?', (operation_id,)) == {'grant_id': 'broad', 'project_id': 'proj'}
    assert asyncio.run(service.runtime.invoke('collaboration_work_execute', command, broad))['operation_id'] == operation_id
    assert service.store.one('SELECT steps FROM coordination_goals WHERE id=?', (goal['id'],))['steps'] == 1
    result = {**lease(scope, goal, item), 'outcome': 'succeeded', 'summary': 'Fixture command'}
    expect('WORK_OPERATION_MISMATCH', c.result, {**result, 'operation_ids': ['fabricated']}, broad)
    expect('WORK_OPERATION_PENDING', c.result, {**result, 'operation_ids': [operation_id]}, broad)
    operation = service.store.one('SELECT * FROM operations WHERE id=?', (operation_id,))
    service.runtime.complete(operation, {'ok': True, 'data': {'exit_code': 0, 'output': 'fixture executor'}})
    completed = c.result({**result, 'operation_ids': [operation_id]}, broad)['work_item']
    assert completed['state'] == 'succeeded'
    assert completed['result']['execution_verified'] is True
    assert completed['result']['provenance_project_ids'] == ['proj']


def test_capability_target_and_static_policy_guards(collab, monkeypatch):
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', 'true')
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, worker, assignee='worker', caps=['read']), worker)
    request = {**lease(scope, goal, item), 'tool': 'exec', 'arguments': {'command': 'printf forbidden'}}
    expect('GOAL_CAPABILITY_DENIED', c.admit, request, worker)
    item2 = claim(c, scope, goal, make_work(c, scope, goal, broad), broad)
    base = {**lease(scope, goal, item2), 'tool': 'exec'}
    expect('WORK_PROJECT_MISMATCH', c.admit, {**base, 'arguments': {'project': 'otherproj', 'command': 'pwd'}}, broad)
    expect('GOAL_TARGET_NOT_APPROVED', c.admit, {**base, 'arguments': {'workspace_id': 'a' * 32, 'command': 'pwd'}}, broad)
    expect('INVALID_ARGUMENTS', c.admit, {**base, 'arguments': {'command': 'printf invalid', 'timeout_seconds': 'ten'}}, broad)
    expect('CODEX_REMOTE_DISABLED', c.admit, {**base, 'arguments': {'command': 'codex exec hello'}}, broad)
    assert service.store.one('SELECT COUNT(*) AS n FROM coordination_operations')['n'] == 0


def test_expired_lease_fencing_and_honest_reclaim(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, worker), broad)
    old_lease = lease(scope, goal, item)
    collab[6][0] += 301
    expect('WORK_LEASE_EXPIRED', c.heartbeat, old_lease, broad)
    c.reconcile()
    current = c.work(c.object(collab[4], goal['id']), item['id'])
    assert current['state'] == 'queued'
    current = claim(c, scope, goal, current, broad)
    assert current['attempt'] == 2 and current['fencing_token'] > item['fencing_token']
    expect('WORK_LEASE_EXPIRED', c.result, {**old_lease, 'outcome': 'succeeded', 'summary': 'Late'}, broad)


def test_pause_cancel_tracks_inflight_and_blocks_new_admission(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, worker), broad)
    request = {**lease(scope, goal, item), 'tool': 'exec', 'arguments': {'command': 'printf test'}}
    receipt = c.admit(request, broad)
    service.store.execute("UPDATE operations SET attempts=1,state='running',accepted_at=? WHERE id=?", (collab[6][0], receipt['operation_id']))
    paused = c.control({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                        'action': 'pause', 'idempotency_key': key()}, owner)
    assert paused['goal']['state'] == 'paused'
    assert paused['operations'][0]['state'] == 'cancelling'
    assert 'not_proven_stopped' in paused['operations'][0]['cancel_note']
    expect('GOAL_INACTIVE', c.admit, {**request, 'idempotency_key': key()}, broad)
    assert c.operation_denial(service.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],)))
    assert service.store.one("SELECT revoked FROM grants WHERE id='broad'")['revoked'] == 0


def test_live_grant_revocation_and_goal_revision(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = make_work(c, scope, goal, broad)
    updated = c.update({**raw, 'goal_id': goal['id'], 'expected_version': goal['version'], 'idempotency_key': key()}, broad)['goal']
    assert updated['state'] == 'proposed' and not updated['approval_id']
    assert c.work(c.object(collab[4], goal['id']), item['id'])['state'] == 'blocked'
    expect('GOAL_INACTIVE', claim, c, scope, goal, item, broad)
    service.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    with pytest.raises(DevError):
        c.read({**scope, 'goal_id': goal['id']}, broad)


def test_peer_messages_bounded_and_require_explicit_participants(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, budget={'max_messages': 1})
    message = {**scope, 'goal_id': goal['id'], 'body_text': 'Please review the plan, not a shell command.',
               'mention_grant_ids': ['worker'], 'idempotency_key': key()}
    saved = c.message(message, broad)
    assert c.message(message, broad) == saved
    outbox = service.store.one('SELECT * FROM mcp_event_outbox WHERE object_id=?', (saved['message_id'],))
    assert outbox['name'] == 'codepier.collaboration.work_available.v1'
    assert outbox['target_grant_id'] == 'worker'
    expect('GOAL_BUDGET_EXCEEDED', c.message, {**message, 'idempotency_key': key()}, broad)
    expect('GOAL_PARTICIPANT_REQUIRED', c.message, {**message, 'mention_grant_ids': ['dot'], 'idempotency_key': key()}, broad)


def test_room_project_add_does_not_widen_approval(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    service.conversations.add_project({'conversation_id': goal['conversation_id'], 'expected_version': 1,
        'project': 'otherproj', 'environment_id': 'production', 'idempotency_key': key()}, owner)
    expect('GOAL_PROJECT_DENIED', make_work, c, scope, goal, owner, target_project_id='otherproj')
    assert c.read({**scope, 'goal_id': goal['id']}, owner)['goal']['project_ids'] == ['proj']


def test_owner_project_change_invalidates_approval(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    service.store.execute("UPDATE projects SET root='/tmp/changed' WHERE id='proj'")
    expect('GOAL_PROJECT_CHANGED', make_work, c, scope, goal, broad)
    c.reconcile()
    assert c.read({**scope, 'goal_id': goal['id']}, owner)['goal']['state'] == 'paused'


def test_current_goal_event_guard_and_distinct_delivery(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = make_work(c, scope, goal, broad)
    event = service.store.one('SELECT data FROM mcp_event_outbox WHERE object_id=?', (item['id'],))
    payload = json.loads(event['data'])
    filters = {'project_id': 'proj', 'environment_id': 'production',
               'conversation_id': goal['conversation_id'], 'goal_id': goal['id'], 'approval_id': goal['approval_id']}
    assert c.authorize_event(broad, filters, payload)['id'] == goal['id']
    expect('GOAL_EVENT_OBSOLETE', c.authorize_event, worker, filters, payload)
    claim(c, scope, goal, item, broad)
    expect('GOAL_EVENT_OBSOLETE', c.authorize_event, broad, filters, payload)


def test_mixed_project_provenance_and_reduced_proposal_never_leak_history(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, approve=False)
    service.conversations.add_project({'conversation_id': goal['conversation_id'], 'expected_version': 1,
        'project': 'otherproj', 'environment_id': 'production', 'idempotency_key': key()}, owner)
    for grant_id in ('broad', 'worker'):
        service.store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['proj', 'otherproj']), grant_id))
    proposed = {**raw, 'project_ids': ['proj', 'otherproj'], 'goal_id': goal['id'],
                'expected_version': goal['version'], 'idempotency_key': key()}
    goal = c.update(proposed, owner)['goal']
    goal = c.approve({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                     'digest': goal['digest'], 'idempotency_key': key()}, owner)['goal']
    first = claim(c, scope, goal, make_work(c, scope, goal, worker, assignee='worker', caps=['read']), worker)
    c.result({**lease(scope, goal, first), 'outcome': 'succeeded', 'summary': 'First project inspection'}, worker)
    other = make_work(c, scope, goal, worker, assignee='worker', caps=['read'],
                      target_project_id='otherproj', dependencies=[first['id']])
    other = claim(c, scope, goal, other, worker)
    result = c.result({**lease(scope, goal, other), 'outcome': 'succeeded', 'summary': 'Combined inspection'}, worker)['work_item']
    assert result['provenance_project_ids'] == ['otherproj', 'proj']
    narrowed = {**raw, 'goal_id': goal['id'], 'expected_version': goal['version'], 'idempotency_key': key()}
    c.update(narrowed, owner)
    service.store.execute("UPDATE grants SET projects='[\"proj\"]' WHERE id='worker'")
    with pytest.raises(DevError):
        c.read({**scope, 'goal_id': goal['id']}, worker)
    assert c.read({**scope, 'goal_id': goal['id']}, owner)['work_items']


def test_dispatch_rechecks_pause_before_first_delivery(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, broad), broad)
    receipt = c.admit({**lease(scope, goal, item), 'tool': 'exec',
                       'arguments': {'command': 'printf approved-once'}}, broad)
    service.store.execute("UPDATE coordination_goals SET state='paused' WHERE id=?", (goal['id'],))
    assert service.runtime._prepare_delivery(receipt['operation_id']) is None
    operation = service.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    assert operation['state'] == 'failed' and operation['attempts'] == 0
    assert 'AUTHORIZATION_CHANGED' in operation['result']


def test_idempotent_approval_replay_reports_current_paused_state(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, approve=False)
    approval = {**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                'digest': goal['digest'], 'idempotency_key': key()}
    goal = c.approve(approval, owner)['goal']
    c.control({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
               'action': 'pause', 'idempotency_key': key()}, owner)
    replay = c.approve(approval, owner)['goal']
    assert replay['state'] == 'paused'
    assert service.store.one('SELECT COUNT(*) AS n FROM coordination_approvals')['n'] == 1


def goal_events(collab, goal):
    import base64
    from hub.collaboration.events import EventService
    from hub.collaboration.network import Reply
    from hub.collaboration.common import canonical
    service = collab[0]
    service.config = replace(service.config, events_enabled=True)
    received = []

    async def receiver(url, body, headers):
        value = json.loads(body)
        received.append(value)
        if value.get('type') == 'verification':
            return Reply(200, canonical({'challenge': value['challenge']}).encode())
        return Reply(204)

    events = EventService(service, receiver)
    service.events = events
    args = {'name': 'codepier.collaboration.work_available.v1',
            'arguments': {'project_id': 'proj', 'environment_id': 'production',
                          'conversation_id': goal['conversation_id'], 'goal_id': goal['id'],
                          'approval_id': goal['approval_id']},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/goal-fixture',
                         'secret': 'whsec_' + base64.b64encode(b'goal-fixture-signing-key-32-bytes!').decode()}}
    return events, received, args


@pytest.mark.asyncio
async def test_goal_event_separate_optin_readonly_peer_and_synthetic_test(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    events, received, args = goal_events(collab, goal)
    sub = await events.subscribe(args, worker)
    old = await events.subscribe({**args, 'name': 'codepier.collaboration.task_available.v1',
        'arguments': {'project_id': 'proj', 'environment_id': 'production', 'queue': 'work-analysis'}}, worker)
    assert sub['id'] != old['id']
    count = service.store.one('SELECT COUNT(*) AS n FROM coordination_work')['n']
    c.message({**scope, 'goal_id': goal['id'], 'body_text': 'Readonly peer review request',
               'mention_grant_ids': ['worker'], 'idempotency_key': key()}, broad)
    await events.tick()
    delivered = [row for row in received if row.get('name')]
    assert len(delivered) == 1 and delivered[0]['name'] == args['name']
    assert delivered[0]['data']['reason'] == 'peer_message'
    assert not service.store.one('SELECT 1 FROM mcp_event_deliveries WHERE subscription_id=?', (old['id'],))
    stored = service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],))
    test = events.queue_test(collab[4], stored)
    await events.tick()
    assert any(row.get('eventId') == test['test_event_id'] and row['data']['test'] for row in received)
    assert service.store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == count
    # Neither successful callback nor test result claims a model online or work.
    assert service.store.one("SELECT COUNT(*) AS n FROM coordination_work WHERE state='leased'")['n'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pause', 'revoke'])
async def test_goal_event_reserved_delivery_rechecks_pause_or_revoke(collab, change):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    events, received, args = goal_events(collab, goal)
    await events.subscribe(args, worker)
    c.message({**scope, 'goal_id': goal['id'], 'body_text': 'Do not deliver after authorization stops',
               'mention_grant_ids': ['worker'], 'idempotency_key': key()}, broad)
    reserved = events.reserve()
    assert len(reserved) == 1
    before = len(received)
    if change == 'pause':
        c.control({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                   'action': 'pause', 'idempotency_key': key()}, owner)
    else:
        service.store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    await events.deliver(reserved[0])
    assert len(received) == before
    assert service.store.one('SELECT state FROM mcp_event_deliveries WHERE id=?', (reserved[0]['delivery_id'],))['state'] == 'abandoned'


@pytest.mark.asyncio
async def test_goal_event_subscription_pins_exact_approval(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    events, received, args = goal_events(collab, goal)
    sub = await events.subscribe(args, worker)
    updated = c.update({**raw, 'goal_id': goal['id'], 'expected_version': goal['version'], 'idempotency_key': key()}, owner)['goal']
    current = c.approve({**scope, 'goal_id': goal['id'], 'expected_version': updated['version'],
                        'digest': updated['digest'], 'idempotency_key': key()}, owner)['goal']
    assert current['approval_id'] != goal['approval_id']
    with pytest.raises(DevError):
        await events.subscribe(args, worker)
    stored = service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],))
    kickoff = service.store.one('SELECT * FROM mcp_event_outbox WHERE name=? ORDER BY seq DESC LIMIT 1', (args['name'],))
    assert not events.matches(stored, args['arguments'], kickoff)
    fresh = await events.subscribe({**args, 'arguments': {**args['arguments'], 'approval_id': current['approval_id']}}, worker)
    assert fresh['id'] != sub['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pause', 'reapprove'])
async def test_goal_event_challenge_cannot_race_owner_pause(collab, change):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    events, received, args = goal_events(collab, goal)
    sender = events.sender

    async def pause_during_challenge(url, body, headers):
        reply = await sender(url, body, headers)
        if change == 'pause':
            c.control({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                       'action': 'pause', 'idempotency_key': key()}, owner)
        else:
            c.approve({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                       'digest': goal['digest'], 'idempotency_key': key()}, owner)
        return reply

    events.sender = pause_during_challenge
    with pytest.raises(DevError):
        await events.subscribe(args, worker)
    assert not service.store.one('SELECT 1 FROM mcp_event_subscriptions WHERE name=?', (args['name'],))


def test_operation_budget_can_submit_more_than_one_hundred_real_receipts(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab, budget={'max_steps': 101})
    item = claim(c, scope, goal, make_work(c, scope, goal, broad, caps=['read']), broad)
    identifiers = []
    for index in range(101):
        receipt = c.admit({**lease(scope, goal, item), 'tool': 'read', 'arguments': {'path': 'fixture.txt'}}, broad)
        identifiers.append(receipt['operation_id'])
        operation = service.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
        # Explicit fake executor; tests durable provenance, not native model execution.
        service.runtime.complete(operation, {'ok': True, 'data': {'content': 'synthetic fixture ' + str(index)}})
    completed = c.result({**lease(scope, goal, item), 'outcome': 'succeeded', 'summary': '101 isolated fixture receipts',
                         'operation_ids': identifiers}, broad)['work_item']
    assert len(completed['result']['operation_ids']) == 101
    expect('GOAL_BUDGET_EXCEEDED', c.admit,
           {**lease(scope, goal, claim(c, scope, goal, make_work(c, scope, goal, broad, caps=['read']), broad)),
            'tool': 'read', 'arguments': {'path': 'fixture.txt'}}, broad)


def test_existing_interactive_operation_cannot_be_retrofitted_as_goal_step(collab):
    from hub.collaboration.common import digest
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, broad, caps=['read']), broad)
    request = {**lease(scope, goal, item), 'tool': 'read', 'arguments': {'path': 'fixture.txt'}}
    interactive = {'project': 'proj', 'path': 'fixture.txt',
                   'idempotency_key': 'goal:' + digest([goal['id'], item['id'], item['attempt'], request['idempotency_key']])}
    prepared = service.runtime._invoke('read', interactive, broad)
    identifier, _ = service.runtime._admit_operation('read', prepared.arguments, prepared.project, broad, True)
    operation = service.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    service.runtime.complete(operation, {'ok': True, 'data': {'content': 'outside-goal fixture'}})
    expect('WORK_OPERATION_PROVENANCE', c.admit, request, broad)
    assert not service.store.one('SELECT 1 FROM coordination_operations WHERE operation_id=?', (identifier,))
    assert service.store.one('SELECT steps FROM coordination_goals WHERE id=?', (goal['id'],))['steps'] == 0


def test_dependent_work_is_not_notified_until_all_dependencies_finish(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    first = make_work(c, scope, goal, broad, caps=['read'])
    dependent = make_work(c, scope, goal, broad, caps=['read'], dependencies=[first['id']])
    assert not service.store.one('SELECT 1 FROM mcp_event_outbox WHERE object_id=?', (dependent['id'],))
    first = claim(c, scope, goal, first, broad)
    c.result({**lease(scope, goal, first), 'outcome': 'succeeded', 'summary': 'Dependency complete'}, broad)
    assert service.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE object_id=?', (dependent['id'],))['n'] == 1


def test_goal_pagination_and_reconcile_do_not_starve_later_rows(collab):
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    for index in range(101):
        candidate = c.create({**raw, 'objective': 'Goal ' + str(index), 'idempotency_key': key()}, owner)['goal']
        c.approve({**scope, 'goal_id': candidate['id'], 'expected_version': candidate['version'],
                   'digest': candidate['digest'], 'idempotency_key': key()}, owner)
    largest = service.store.one('SELECT id FROM coordination_goals ORDER BY id DESC LIMIT 1')['id']
    service.store.execute('UPDATE coordination_goals SET expires_at=0 WHERE id=?', (largest,))
    c.reconcile()
    assert service.store.one('SELECT state FROM coordination_goals WHERE id=?', (largest,))['state'] == 'active'
    c.reconcile()
    assert service.store.one('SELECT state FROM coordination_goals WHERE id=?', (largest,))['state'] == 'paused'
    seen, cursor = [], ''
    while True:
        page = service.read({**scope, 'kind': 'coordination_goals', 'conversation_id': goal['conversation_id'],
                             'limit': 40, 'cursor': cursor}, owner)
        seen.extend(row['id'] for row in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 102


def test_goal_options_report_per_project_capabilities_and_truncation(collab):
    from tests.legacy_iam_fixture import seed_grant
    c, service, owner, worker, broad, goal, scope, raw = setup_goal(collab)
    args = {**scope, 'conversation_id': goal['conversation_id'], 'kind': 'coordination_options'}
    options = service.read(args, owner)
    assert not options['truncated']
    available = {row['grant_id']: row for row in options['participants']}
    assert available['worker']['project_capabilities']['proj'] == ['read']
    assert set(available['broad']['project_capabilities']['proj']) == {'read', 'write', 'execute'}
    for index in range(130):
        seed_grant(service.store, 'fixture-many-' + str(index), 'owner', scopes=('read',), projects=('proj',))
    bounded = service.read(args, owner)
    assert bounded['truncated'] and bounded['scan_limit'] == 128
    assert len(bounded['participants']) == 128
