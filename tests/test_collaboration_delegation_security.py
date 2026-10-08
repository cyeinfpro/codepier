"""Isolated owner-delegation boundaries, durable receipts and deterministic recovery."""
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import DELEGATION_EVENT, canonical
from hub.collaboration.schema import migrate
from hub.vps import VPSInput
from shared.util import DevError
from shared.public_collaboration import resolve
from tests.collaboration_support import collab, key  # noqa: F401


def deny(code, call, *args):
    with pytest.raises(DevError) as caught:
        call(*args)
    assert caught.value.code == code


def policy_fixture(fixture, *, caps=None, **options):
    s, owner, worker, _, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    actor = replace(worker, grant_id='broad', actor='mcp:broad:delegation')
    slot = s.joining.create({**scope, 'label': 'Selected dot', 'kind': 'dot', 'idempotency_key': key()}, owner)['slot']
    s.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, actor)
    request = {**scope, 'conversation_id': room['id'], 'slot_id': slot['id'], 'expected_version': 0,
               'purpose': 'Inspect this selected project and report actual evidence.',
               'capabilities': caps or ['read', 'execute'], 'acknowledge_unsandboxed_exec': True,
               'idempotency_key': key(), **options}
    policy = s.delegation.set_policy(request, owner)['policy']
    return s, owner, actor, room, scope, policy, request


def send(context, **extra):
    s, owner, _, room, scope, policy, _ = context
    raw = {**scope, 'room_id': room['id'], 'conversation_id': room['id'], 'body_text': 'Please inspect the selected target.',
           'client_message_id': key(), 'idempotency_key': key(), 'mentions': [{'slot_id': policy['slot_id']}],
           'delegation': {'policy_id': policy['id'], 'policy_version': policy['version'], 'acceptance': 'Report actual checks and limits.'},
           **extra}
    return s.chatroom.create(raw, owner), raw


def claim(context, sent):
    s, _, actor, room, scope, _, _ = context
    goal = s.coordination.object(room, sent['goal_id'])
    item = s.coordination.work(goal, sent['work_item_id'])
    return s.invoke('collaboration_work', {'action': 'claim', **scope, 'goal_id': goal['id'], 'work_item_id': item['id'],
                                     'expected_version': item['version'], 'idempotency_key': key()}, actor)['work_item']


def lease(context, sent, item):
    return {**context[4], 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
            'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'idempotency_key': key()}


def admit(context, sent, item, **arguments):
    _, raw = resolve('collaboration_work', {'action': 'execute', **lease(context, sent, item), 'tool': 'exec',
                                          'arguments': {'command': 'printf isolated-delegation', **arguments}})
    return context[0].coordination.admit(raw, context[2])


def test_migration_and_ordinary_mentions_never_create_policy_or_goal(collab):
    s, owner, _, _, room, _, _, scope = collab
    with s.store.transaction():
        migrate(s.store.db)
    assert not s.store.all('SELECT * FROM delegation_policies')
    result = s.chatroom.create({**scope, 'room_id': room['id'], 'body_text': '@dot execute this',
                               'client_message_id': key(), 'idempotency_key': key()}, owner)
    assert result['scheduled'] is False
    assert not s.store.all('SELECT * FROM coordination_goals')


def test_atomic_owner_source_and_idempotent_replay_after_pause(collab):
    c = policy_fixture(collab)
    s, owner, actor, room, scope, policy, _ = c
    sent, raw = send(c)
    fresh = s.delegation.read({**scope, 'delegation_id': sent['delegation_id']}, actor)
    assert fresh['trusted_author'] == {'kind': 'panel_owner', 'user_id': owner.user_id, 'authenticated': True}
    assert fresh['source_message']['author'] == owner.user_id
    assert fresh['source_message']['origin'] == 'panel_owner'
    assert fresh['policy']['execution_targets'] == ['project_agent']
    assert len(s.store.all('SELECT * FROM coordination_work')) == 1
    assert not s.store.all('SELECT * FROM conversation_writers')
    s.delegation.control({**scope, 'policy_id': policy['id'], 'expected_version': policy['version'],
                          'action': 'pause', 'idempotency_key': key()}, owner)
    assert s.chatroom.create(raw, owner)['goal_id'] == sent['goal_id']
    replay = s.chatroom.create({**raw, 'idempotency_key': key()}, owner)
    assert replay['goal_id'] == sent['goal_id']
    assert replay['message']['body']['delegation']['delivery_status'] == 'blocked'
    assert len(s.store.all('SELECT * FROM coordination_goals')) == 1
    deny('SOURCE_MESSAGE_CONFLICT', s.chatroom.create, {**raw, 'body_text': 'Different instruction', 'idempotency_key': key()}, owner)
    deny('DELEGATION_POLICY_INACTIVE', send, c)
    assert len(s.store.all('SELECT * FROM collaboration_messages')) == 1


def test_stale_policy_rolls_back_message_goal_and_outbox(collab):
    c = policy_fixture(collab)
    s, owner, _, _, _, policy, _ = c
    with pytest.raises(DevError):
        send(c, delegation={'policy_id': policy['id'], 'policy_version': policy['version'] + 1, 'acceptance': 'Inspect'})
    assert not s.store.all('SELECT * FROM coordination_goals')
    assert not s.store.all('SELECT * FROM collaboration_messages')
    assert not s.store.all('SELECT * FROM mcp_event_outbox')


def test_source_edit_and_connector_impersonation_are_rejected(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, policy, _ = c
    s.chatroom.access({**scope, 'room_id': room['id'], 'conversation_id': room['id'], 'grant_id': actor.grant_id,
                       'expected_version': 0, 'enabled': True, 'idempotency_key': key()}, owner)
    sent, raw = send(c)
    deny('OWNER_REQUIRED', s.chatroom.create, {**raw, 'client_message_id': key(), 'idempotency_key': key()}, actor)
    s.store.execute('UPDATE collaboration_messages SET version=version+1 WHERE id=?', (sent['message']['id'],))
    deny('DELEGATION_MESSAGE_CHANGED', claim, c, sent)
    deny('DELEGATION_MESSAGE_CHANGED', s.delegation.read, {**scope, 'delegation_id': sent['delegation_id']}, actor)


def test_fixed_project_new_room_membership_cannot_expand_goal(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    s.conversations.add_project({'conversation_id': room['id'], 'expected_version': 1,
                                'project': 'otherproj', 'idempotency_key': key()}, owner)
    deny('GOAL_PROJECT_DENIED', s.coordination.work_create, {**scope, 'goal_id': sent['goal_id'],
        'objective': 'other', 'acceptance': 'other', 'target_project_id': 'otherproj',
        'assignee_grant_id': actor.grant_id, 'required_capabilities': ['read'], 'idempotency_key': key()}, actor)
    assert json.loads(s.coordination.object(room, sent['goal_id'])['spec'])['project_ids'] == ['proj']


def test_policy_budget_does_not_count_message_replays(collab):
    c = policy_fixture(collab, max_delegations=1)
    sent, raw = send(c)
    assert c[0].chatroom.create(raw, c[1])['delegation_id'] == sent['delegation_id']
    deny('DELEGATION_BUDGET_EXCEEDED', send, c)
    assert c[0].store.one('SELECT uses FROM delegation_policies')['uses'] == 1


def test_exec_result_needs_real_complete_operation_and_projects_to_thread(collab):
    c = policy_fixture(collab)
    s, _, actor, room, scope, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    request = {**lease(c, sent, item), 'outcome': 'succeeded', 'summary': 'Actual inspection'}
    deny('WORK_OPERATION_REQUIRED', s.coordination.result, request, actor)
    operation = admit(c, sent, item)
    request.update(operation_ids=[operation['operation_id']], idempotency_key=key())
    deny('WORK_OPERATION_PENDING', s.coordination.result, request, actor)
    row = s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],))
    s.runtime.complete(row, {'ok': True, 'data': {'exit_code': 0, 'output': 'isolated-delegation'}})
    result = s.coordination.result(request, actor)
    assert result['work_item']['result']['execution_verified'] is True
    assert result['work_item']['result']['acceptance_verified_by_owner'] is False
    assert s.coordination.result(request, actor)['work_item']['id'] == item['id']
    timeline = s.read({**scope, 'kind': 'thread', 'conversation_id': room['id'], 'id': sent['message']['id']}, actor)
    replies = [m for m in timeline['items'] if m['kind'] == 'delegation_result']
    assert len(replies) == 1
    assert replies[0]['reply_to_id'] == sent['message']['id']
    assert replies[0]['body']['operation_ids'] == [operation['operation_id']]
    assert replies[0]['body']['notifications'] == []
    assert replies[0]['body']['provenance_project_ids'] == ['proj']


def test_terminal_operation_without_host_result_never_auto_reexecutes(collab):
    c = policy_fixture(collab)
    s, _, actor, room, scope, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    operation = admit(c, sent, item)
    row = s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],))
    s.runtime.complete(row, {'ok': True, 'data': {'exit_code': 0}})
    collab[6][0] = item['lease_until'] + 1
    s.coordination.reconcile()
    current = s.coordination.work(s.coordination.object(room, sent['goal_id']), item['id'])
    assert current['state'] == 'blocked'
    deny('WORK_NOT_CLAIMABLE', s.coordination.work_claim, {**scope, 'goal_id': sent['goal_id'],
        'work_item_id': item['id'], 'expected_version': current['version'], 'idempotency_key': key()}, actor)
    assert len(s.store.all('SELECT * FROM operations')) == 1


def test_repeated_claim_and_policy_pause_cannot_admit_a_new_step(collab):
    c = policy_fixture(collab)
    s, owner, actor, room, scope, policy, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    deny('WORK_NOT_CLAIMABLE', claim, c, sent)
    operation = admit(c, sent, item)
    s.delegation.control({**scope, 'policy_id': policy['id'], 'expected_version': policy['version'],
                          'action': 'pause', 'idempotency_key': key()}, owner)
    deny('DELEGATION_POLICY_INACTIVE', admit, c, sent, item)
    assert len(s.store.all('SELECT * FROM operations')) == 1
    assert s.store.one('SELECT cancel_note FROM coordination_operations WHERE operation_id=?', (operation['operation_id'],))['cancel_note']


def saved_vps(s, owner, name):
    return s.runtime.vps.save(VPSInput(name=name, host=name + '.example.invalid', username='fixture',
                                      password='synthetic-fixture-only', project_ids=['proj']), owner)


def test_multiple_saved_vps_require_explicit_target_and_exact_snapshot(collab):
    s, owner = collab[:2]
    first, second = saved_vps(s, owner, 'first'), saved_vps(s, owner, 'second')
    c = policy_fixture(collab, execution_targets=[first['target'], second['target']])
    deny('DELEGATION_TARGET_REQUIRED', send, c)
    sent, _ = send(c, delegation={'policy_id': c[5]['id'], 'policy_version': c[5]['version'],
        'acceptance': 'Check both selected targets.', 'execution_targets': [first['target'], second['target']]})
    item = claim(c, sent)
    deny('DELEGATION_TARGET_REQUIRED', admit, c, sent, item)
    deny('GOAL_TARGET_NOT_APPROVED', lambda: admit(c, sent, item, target='agent'))
    op = admit(c, sent, item, target=second['target'])
    record = s.store.one('SELECT * FROM operations WHERE id=?', (op['operation_id'],))
    payload = json.loads(s.store.decrypt(record['payload']))
    assert payload['args']['target'] == second['target']
    assert payload['vps_ref']['id'] == second['id']
    assert 'synthetic-fixture-only' not in canonical(payload)
    assert 'synthetic-fixture-only' not in canonical(c[5])
    s.store.execute('UPDATE vps_connections SET connection_revision=connection_revision+1 WHERE id=?', (first['id'],))
    deny('DELEGATION_TARGET_CHANGED', lambda: admit(c, sent, item, target=second['target']))


def test_vps_file_tools_and_rebound_target_are_denied(collab):
    s, owner = collab[:2]
    vps = saved_vps(s, owner, 'selected')
    c = policy_fixture(collab, execution_target=vps['target'])
    sent, _ = send(c)
    item = claim(c, sent)
    deny('GOAL_TARGET_NOT_APPROVED', s.coordination.admit, {**lease(c, sent, item), 'tool': 'read',
         'arguments': {'path': 'fixture.txt'}}, c[2])
    s.store.execute('UPDATE vps_projects SET binding_id=? WHERE vps_id=?', ('new-binding', vps['id']))
    deny('DELEGATION_TARGET_CHANGED', admit, c, sent, item)


def test_room_remind_reuses_original_goal_and_work_with_new_fence_version(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    assert sent['message']['body']['delegation']['delivery_status'] == 'notifications_disabled'
    request = {**scope, 'delegation_id': sent['delegation_id'], 'idempotency_key': key()}
    response = s.delegation.remind(request, owner)
    assert response['work_items_reminded'] == 1
    assert s.delegation.remind(request, owner)['work_items_reminded'] == 1
    assert len(s.store.all('SELECT * FROM coordination_goals')) == 1
    assert len(s.store.all('SELECT * FROM coordination_work')) == 1
    assert len(s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (DELEGATION_EVENT,))) == 2
    item = claim(c, sent)
    assert s.delegation.remind({**request, 'idempotency_key': key()}, owner)['work_items_reminded'] == 0
    assert item['version'] == 3


def test_result_message_provenance_is_rechecked_in_every_read_surface(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    s.coordination.result({**lease(c, sent, item), 'outcome': 'succeeded', 'summary': 'Mixed synthetic provenance'}, actor)
    row = s.store.one("SELECT * FROM collaboration_messages WHERE kind='delegation_result'")
    body = json.loads(row['body'])
    body['provenance_project_ids'] = ['proj', 'otherproj']
    s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), row['id']))
    for kind in ('timeline', 'messages', 'thread', 'search'):
        args = {**scope, 'kind': kind, 'conversation_id': room['id']}
        if kind == 'thread':
            args['id'] = sent['message']['id']
        if kind == 'search':
            args['query'] = 'Mixed'
        assert row['id'] not in [m['id'] for m in s.read(args, actor)['items']]
    deny('NOT_FOUND', s.read, {**scope, 'kind': 'message_status', 'conversation_id': room['id'], 'id': row['id']}, actor)
    changes = s.read({**scope, 'kind': 'changes', 'conversation_id': room['id']}, actor)
    assert row['id'] not in [m['object_id'] for m in changes['items']]


def test_initial_work_success_does_not_complete_followup_work(collab):
    c = policy_fixture(collab, caps=['read'])
    s, _, actor, room, scope, _, _ = c
    sent, _ = send(c)
    first = claim(c, sent)
    s.coordination.result({**lease(c, sent, first), 'outcome': 'succeeded', 'summary': 'Planning finished'}, actor)
    following = s.coordination.work_create({**scope, 'goal_id': sent['goal_id'], 'objective': 'Inspect the plan',
        'acceptance': 'Return evidence', 'target_project_id': 'proj', 'assignee_grant_id': actor.grant_id,
        'required_capabilities': ['read'], 'idempotency_key': key()}, actor)['work_item']
    assert s.delegation.progress(sent['delegation_id'])['state'] == 'queued'
    following = s.coordination.work_claim({**scope, 'goal_id': sent['goal_id'], 'work_item_id': following['id'],
        'expected_version': following['version'], 'idempotency_key': key()}, actor)['work_item']
    receipt = s.coordination.admit({**lease(c, sent, following), 'tool': 'read', 'arguments': {'path': 'fixture.txt'}}, actor)
    state = s.chatroom.view(s.object('collaboration_messages', room, sent['message']['id']))
    assert state['body']['delegation']['delivery_status'] == 'running'
    assert state['body']['delegation']['progress']['counts'] == {'succeeded': 1, 'running': 1}
    assert receipt['operation_id']


def test_write_delegation_cannot_claim_success_without_durable_receipt(collab):
    c = policy_fixture(collab, caps=['read', 'write'])
    s, _, actor, _, _, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    result = {**lease(c, sent, item), 'outcome': 'succeeded', 'summary': 'No change needed'}
    deny('WORK_OPERATION_REQUIRED', s.coordination.result, result, actor)
    receipt = s.coordination.admit({**lease(c, sent, item), 'tool': 'read', 'arguments': {'path': 'fixture.txt'}}, actor)
    operation = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    s.runtime.complete(operation, {'ok': True, 'data': {'content': 'Already correct'}})
    saved = s.coordination.result({**result, 'idempotency_key': key(), 'operation_ids': [receipt['operation_id']]}, actor)['work_item']['result']
    assert saved['execution_verified'] is True
    assert saved['acceptance_verified_by_owner'] is False


@pytest.mark.asyncio
async def test_policy_renewals_retire_routes_without_losing_audit_or_quota(collab):
    import base64
    from hub.collaboration.common import MESSAGE_EVENT
    from hub.collaboration.events import EventService
    from hub.collaboration.network import Reply

    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, policy, raw = c
    s.config = replace(s.config, events_enabled=True)
    async def receiver(url, body, headers):
        value = json.loads(body)
        if value.get('type') == 'verification':
            return Reply(200, canonical({'challenge': value['challenge']}).encode())
        return Reply(204)
    s.events = EventService(s, receiver)
    delivery = {'mode': 'webhook', 'url': 'https://callback.example.invalid/quota',
                'secret': 'whsec_' + base64.b64encode(b'delegation-quota-synthetic-key-32!').decode()}
    slot = s.joining.slot(room, policy['slot_id'])
    for request in s.joining.subscription_requests(room, slot):
        await s.events.subscribe({**request, 'delivery': delivery}, actor)
    await s.events.subscribe({'name': MESSAGE_EVENT, 'arguments': {'project_id': 'proj',
        'environment_id': 'production', 'conversation_id': room['id'], 'slot_id': slot['id']}, 'delivery': delivery}, actor)
    past = []
    for index in range(5):
        if index:
            policy = s.delegation.set_policy({**raw, 'expected_version': policy['version'],
                                              'idempotency_key': key()}, owner)['policy']
        subscribed = await s.events.subscribe({**policy['subscription_request'], 'delivery': delivery}, actor)
        for old in past:
            stored = s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (old,))
            assert stored['state'] == 'revoked'
            with pytest.raises(DevError):
                s.events.authorize(stored, room)
        past.append(subscribed['id'])
    assert s.store.one('SELECT COUNT(*) AS n FROM collaboration_join_routes')['n'] == 10
    assert s.store.one("SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE state='active'")['n'] == 6


def test_legacy_monitor_flags_do_not_describe_delegation_authority(collab):
    c = policy_fixture(collab, caps=['read'])
    s, _, actor, _, scope, policy, _ = c
    overview = s.read(scope, actor)
    assert overview['production_actions_enabled'] is False
    assert overview['delegation']['legacy_monitor_flags_apply'] is False
    assert overview['delegation']['creates_grant'] is False
    assert overview['delegation']['default_enabled'] is False
    sent, _ = send(c)
    fresh = s.delegation.read({**scope, 'delegation_id': sent['delegation_id']}, actor)
    assert fresh['policy']['effective_active'] is True
    assert fresh['approved_policy']['version'] == policy['version']
    assert fresh['authority']['authorization_scope'] == 'managed_delegation_only'


def test_delegated_goal_metadata_and_reapproval_are_explicit(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, policy, _ = c
    sent, _ = send(c)
    detail = s.coordination.read({**scope, 'goal_id': sent['goal_id']}, owner)
    goal = detail['goal']
    assert not goal['can_approve'] and not detail['can_approve']
    assert goal['delegation']['source_message_id'] == sent['message']['id']
    assert goal['delegation']['subscription_request']['name'] == DELEGATION_EVENT
    assert goal['delegation']['subscription_request']['arguments']['policy_id'] == policy['id']
    deny('DELEGATION_IMMUTABLE', s.coordination.approve, {**scope, 'goal_id': goal['id'],
        'expected_version': goal['version'], 'digest': goal['digest'], 'idempotency_key': key()}, owner)
    paused = s.coordination.control({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
                                   'action': 'pause', 'idempotency_key': key()}, owner)['goal']
    assert paused['delegation']['subscription_request'] is None
    assert paused['delegation']['resume_requires_new_delegation'] is True
    assert paused['approval_id'] == goal['approval_id']


def block_without_operation(context, sent, item):
    return context[0].coordination.result({**lease(context, sent, item), 'outcome': 'blocked',
        'summary': 'Host requires an explicit decision before the first operation.'}, context[2])


def test_blocked_retry_requires_explicit_owner_action_and_preserves_report(collab):
    c = policy_fixture(collab)
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    first = claim(c, sent)
    block_without_operation(c, sent, first)
    source = s.chatroom.view(s.object('collaboration_messages', room, sent['message']['id']))
    assert source['body']['delegation']['retry_blocked_eligible'] is True
    plain = {**scope, 'delegation_id': sent['delegation_id'], 'idempotency_key': key()}
    assert s.delegation.remind(plain, owner)['work_items_requeued'] == 0
    assert s.delegation.progress(sent['delegation_id'])['state'] == 'blocked'
    explicit = {**plain, 'retry_blocked': True, 'idempotency_key': key()}
    deny('OWNER_REQUIRED', s.delegation.remind, explicit, actor)
    result = s.delegation.remind(explicit, owner)
    assert result['work_items_requeued'] == 1 and result['blocked_retries'] == []
    assert s.delegation.remind(explicit, owner)['work_items_requeued'] == 1
    queued = s.coordination.work(s.coordination.object(room, sent['goal_id']), first['id'])
    assert queued['attempt'] == first['attempt']
    assert queued['fencing_token'] > first['fencing_token'] and queued['result'] is None
    assert s.store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE kind='delegation_result'")['n'] == 1
    second = claim(c, sent)
    assert second['attempt'] == first['attempt'] + 1 and second['fencing_token'] > queued['fencing_token']
    deny('WORK_LEASE_EXPIRED', s.coordination.admit, {**lease(c, sent, first), 'tool': 'exec',
        'arguments': {'command': 'printf stale'}}, actor)
    receipt = admit(c, sent, second)
    assert receipt['operation_id']
    assert s.store.one("SELECT COUNT(*) AS n FROM collaboration_audit WHERE action='delegation.retry_unstarted'")['n'] == 1


@pytest.mark.parametrize('operation_state', ['unknown', 'succeeded', 'failed'])
def test_any_historical_admission_blocks_owner_retry(collab, operation_state):
    c = policy_fixture(collab)
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    receipt = admit(c, sent, item)
    operation = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    if operation_state == 'unknown':
        s.store.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation['id'],))
        collab[6][0] = item['lease_until'] + 1
        s.coordination.reconcile()
    else:
        completion = {'ok': True, 'data': {'exit_code': 0}} if operation_state == 'succeeded' else {
            'ok': False, 'error': {'code': 'FIXTURE_FAILURE', 'message': 'Isolated test failure'}}
        s.runtime.complete(operation, completion)
        s.coordination.result({**lease(c, sent, item), 'outcome': 'blocked', 'summary': 'Review the existing receipt.',
                               'operation_ids': [operation['id']]}, actor)
    response = s.delegation.remind({**scope, 'delegation_id': sent['delegation_id'], 'retry_blocked': True,
                                  'idempotency_key': key()}, owner)
    assert response['work_items_requeued'] == 0
    assert response['blocked_retries'][0]['reason'] == 'operation_admitted_requires_review'
    assert response['blocked_retries'][0]['operation_ids'] == [operation['id']]
    assert response['blocked_retries'][0]['operations'][0]['state'] == operation_state
    assert response['message']['body']['delegation']['retry_blocked_eligible'] is False
    assert len(s.store.all('SELECT * FROM operations')) == 1
    assert s.coordination.work(s.coordination.object(room, sent['goal_id']), item['id'])['state'] == 'blocked'


@pytest.mark.parametrize('change', ['goal_expired', 'policy_expired', 'grant_revoked', 'target_changed'])
def test_blocked_retry_rechecks_original_authority(collab, change):
    c = policy_fixture(collab)
    s, owner, _, room, scope, policy, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    block_without_operation(c, sent, item)
    if change == 'goal_expired':
        s.store.execute('UPDATE coordination_goals SET expires_at=? WHERE id=?', (s.clock() - 1, sent['goal_id']))
    elif change == 'policy_expired':
        s.store.execute('UPDATE delegation_policies SET expires_at=? WHERE id=?', (s.clock() - 1, policy['id']))
    elif change == 'grant_revoked':
        s.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    else:
        s.store.execute("UPDATE projects SET root='/tmp/different-project' WHERE id='proj'")
    with pytest.raises(DevError):
        s.delegation.remind({**scope, 'delegation_id': sent['delegation_id'], 'retry_blocked': True,
                           'idempotency_key': key()}, owner)
    assert s.coordination.work(s.coordination.object(room, sent['goal_id']), item['id'])['state'] == 'blocked'
    assert s.delegation.retry_blocked_eligible(sent['delegation_id']) is False


def test_blocked_retry_never_resets_attempt_budget(collab):
    c = policy_fixture(collab, budget={'max_attempts': 1})
    s, owner, _, room, scope, _, _ = c
    sent, _ = send(c)
    item = claim(c, sent)
    block_without_operation(c, sent, item)
    response = s.delegation.remind({**scope, 'delegation_id': sent['delegation_id'], 'retry_blocked': True,
                                  'idempotency_key': key()}, owner)
    assert response['work_items_requeued'] == 0
    assert response['blocked_retries'][0]['reason'] == 'attempt_budget_exhausted'
    assert s.coordination.work(s.coordination.object(room, sent['goal_id']), item['id'])['attempt'] == 1


def test_blocked_retry_waits_for_dependencies_without_rewriting_them(collab):
    c = policy_fixture(collab, caps=['read'])
    s, owner, actor, room, scope, _, _ = c
    sent, _ = send(c)
    dependency = claim(c, sent)
    s.coordination.result({**lease(c, sent, dependency), 'outcome': 'succeeded', 'summary': 'Plan ready'}, actor)
    following = s.coordination.work_create({**scope, 'goal_id': sent['goal_id'], 'objective': 'Inspect approved plan',
        'acceptance': 'Evidence', 'target_project_id': 'proj', 'assignee_grant_id': actor.grant_id,
        'required_capabilities': ['read'], 'dependencies': [dependency['id']], 'idempotency_key': key()}, actor)['work_item']
    following = s.coordination.work_claim({**scope, 'goal_id': sent['goal_id'], 'work_item_id': following['id'],
        'expected_version': following['version'], 'idempotency_key': key()}, actor)['work_item']
    block_without_operation(c, sent, following)
    s.store.execute("UPDATE coordination_work SET state='failed' WHERE id=?", (dependency['id'],))
    response = s.delegation.remind({**scope, 'delegation_id': sent['delegation_id'], 'retry_blocked': True,
                                  'idempotency_key': key()}, owner)
    assert response['work_items_requeued'] == 0
    assert response['blocked_retries'][0]['reason'] == 'dependencies_not_succeeded'
