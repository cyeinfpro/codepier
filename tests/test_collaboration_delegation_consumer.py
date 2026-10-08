"""Consumer contracts and reconciliation use isolated fixtures, never a real host/model."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import DELEGATION_EVENT, digest
from hub.vps import VPSInput
from shared.collaboration_contracts import tool_definitions
from shared.public_collaboration import resolve
from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401


def setup(fixture, **extra):
    s, owner, worker, _, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    actor = replace(worker, grant_id='broad', actor='mcp:broad:consumer')
    slot = s.joining.create({**scope, 'label': 'Consumer', 'kind': 'dot', 'idempotency_key': key()}, owner)['slot']
    s.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, actor)
    raw = {**scope, 'conversation_id': room['id'], 'slot_id': slot['id'], 'expected_version': 0,
        'purpose': 'Inspect only the selected scope', 'capabilities': ['read', 'write', 'execute'],
        'acknowledge_unsandboxed_exec': True, 'idempotency_key': key(), **extra}
    policy = s.delegation.set_policy(raw, owner)['policy']
    return SimpleNamespace(s=s, owner=owner, actor=actor, room=room, clock=clock, scope=scope,
                           policy=policy, policy_raw=raw, consumer=s.delegation.consumer)


def request(c, **subset):
    return {**c.scope, 'room_id': c.room['id'], 'conversation_id': c.room['id'],
        'body_text': 'Inspect and report actual evidence', 'client_message_id': key(), 'idempotency_key': key(),
        'mentions': [{'slot_id': c.policy['slot_id']}],
        'delegation': {'policy_id': c.policy['id'], 'policy_version': c.policy['version'],
                       'acceptance': 'Actual checks and limits', **subset}}


def send(c, **subset):
    raw = request(c, **subset)
    return c.s.chatroom.create(raw, c.owner)


def connection(c, mode='managed_execution'):
    return c.consumer.connection({**c.scope, 'policy_id': c.policy['id'],
        'policy_version': c.policy['version'], 'mode': mode}, c.actor)


def public_inbox(c, arguments, actor=None):
    name, raw = resolve('collaboration_query', arguments)
    assert name == 'collaboration_delegation_inbox'
    return c.consumer.inbox(raw, actor or c.actor)


def inbox(c, connected, **extra):
    return public_inbox(c, {**connected['inbox_request']['arguments'], **extra})


def claim(c, sent):
    goal = c.s.coordination.object(c.room, sent['goal_id'])
    item = c.s.coordination.work(goal, sent['work_item_id'])
    raw = {**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
           'expected_version': item['version'], 'idempotency_key': key()}
    return c.s.coordination.work_claim(raw, c.actor)['work_item'], raw


def lease(c, sent, item):
    return {**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
            'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'idempotency_key': key()}


def admit(c, sent, item, **arguments):
    raw = {**lease(c, sent, item), 'tool': 'exec',
           'arguments': {'command': 'printf consumer-fixture', **arguments}}
    return c.s.coordination.admit(raw, c.actor), raw


def complete(c, operation):
    row = c.s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],))
    c.s.runtime.complete(row, {'ok': True, 'data': {'exit_code': 0, 'output': 'fixture'}})


def assert_denied(code, call, *args, **kwargs):
    with pytest.raises(DevError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code


def test_connection_is_read_only_modes_are_separate_and_do_not_claim_online(collab, monkeypatch):
    c = setup(collab)
    monkeypatch.setattr(c.s.delegation, 'notification_status',
        lambda *_: {'notification_state': 'active', 'notification_expires_at': c.policy['expires_at']})
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    notification = connection(c, 'notification_only')
    managed = connection(c)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert not notification['permissions_changed'] and not notification['subscription_created']
    assert notification['subscription_request'] == managed['subscription_request']
    assert set(notification['subscription_request']['arguments']) == {
        'project_id', 'environment_id', 'conversation_id', 'slot_id', 'policy_id', 'policy_version'}
    for result in (notification, managed):
        assert result['status']['notification_state'] == 'active'
        assert result['status']['consumer_mode'] == result['status']['model_online'] == 'unknown'
        assert result['consumer_contract']['mode_evidence'] == 'requested_only'
        assert result['checkpoint_grants_authority'] is False
    read = notification['consumer_contract']
    active = managed['consumer_contract']
    assert 'collaboration_work' not in read['required_tools']
    assert 'collaboration_work' not in read['required_tools']
    assert '禁止 claim' in read['instructions']
    assert 'collaboration_work' in active['required_tools']
    assert '明确同意' in active['instructions'] and '原operation_id' in active['instructions']
    assert '不能重复调用 connection_read' in active['instructions']
    definitions = {tool['name']: tool for tool in tool_definitions()}
    for name in ('collaboration_query',):
        assert definitions[name]['annotations']['readOnlyHint']
        assert not definitions[name]['annotations']['destructiveHint']
    assert not c.s.store.all('SELECT * FROM coordination_goals')


def test_subset_is_immutable_and_enforced_for_goal_work_and_every_execute(collab):
    c = setup(collab)
    sent = send(c, execution_targets=['project_agent'], capabilities=['read'])
    fresh = c.s.delegation.read({**c.scope, 'delegation_id': sent['delegation_id']}, c.actor)
    assert fresh['request_scope'] == {'execution_targets': ['project_agent'], 'capabilities': ['read']}
    assert fresh['goal']['capabilities'] == ['read']
    assert fresh['goal']['delegation']['request_scope'] == fresh['request_scope']
    item, _ = claim(c, sent)
    assert item['required_capabilities'] == ['read']
    assert_denied('GOAL_CAPABILITY_DENIED', admit, c, sent, item)
    assert_denied('GOAL_CAPABILITY_DENIED', c.s.coordination.work_create, {**c.scope, 'goal_id': sent['goal_id'],
        'objective': 'Expand', 'acceptance': 'Expand', 'target_project_id': 'proj', 'assignee_grant_id': c.actor.grant_id,
        'required_capabilities': ['read', 'execute'], 'idempotency_key': key()}, c.actor)
    assert not c.s.store.all('SELECT * FROM operations')


def test_scope_escape_empty_duplicates_and_same_key_change_are_rejected(collab):
    c = setup(collab, capabilities=['read'])
    for subset in ({'capabilities': ['read', 'execute']}, {'execution_targets': ['vps:outside']}):
        assert_denied('DELEGATION_SCOPE_DENIED', send, c, **subset)
    for subset in ({'capabilities': []}, {'execution_targets': []}, {'capabilities': ['read', 'read']}):
        with pytest.raises(DevError):
            send(c, **subset)
    raw = request(c, capabilities=['read'], execution_targets=['project_agent'])
    first = c.s.chatroom.create(raw, c.owner)
    assert c.s.chatroom.create(raw, c.owner)['delegation_id'] == first['delegation_id']
    changed = {**raw, 'delegation': {**raw['delegation'], 'capabilities': ['read', 'execute']}}
    assert_denied('IDEMPOTENCY_CONFLICT', c.s.chatroom.create, changed, c.owner)
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1


def test_multi_target_send_requires_choice_and_execution_cannot_use_other_policy_target(collab):
    s, owner = collab[:2]
    vps = s.runtime.vps.save(VPSInput(name='Selected VPS', host='selected.example.invalid', username='fixture',
        password='synthetic-fixture-only', project_ids=['proj']), owner)
    c = setup(collab, execution_targets=['project_agent', vps['target']])
    assert_denied('DELEGATION_TARGET_REQUIRED', send, c)
    sent = send(c, execution_targets=['project_agent'], capabilities=['read', 'execute'])
    item, _ = claim(c, sent)
    with pytest.raises(DevError) as caught:
        admit(c, sent, item, target=vps['target'])
    assert caught.value.code == 'GOAL_TARGET_NOT_APPROVED'
    operation, _ = admit(c, sent, item, target='agent')
    assert operation['operation_id']
    assert len(c.s.store.all('SELECT * FROM operations')) == 1


def test_capability_shrink_pause_and_revoke_are_live_rechecked(collab):
    c = setup(collab)
    connected = connection(c)
    send(c, capabilities=['read'])
    c.s.store.execute("UPDATE grants SET scopes=? WHERE id='broad'", (json.dumps(['read']),))
    page = inbox(c, connected)
    assert page['items'][0]['category'] == 'blocked'
    assert page['items'][0]['reason_code']
    c.s.delegation.control({**c.scope, 'policy_id': c.policy['id'], 'expected_version': c.policy['version'],
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    assert_denied('DELEGATION_POLICY_CHANGED', inbox, c, connected)
    c.s.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    with pytest.raises(DevError):
        connection(c)


def test_initial_history_is_report_only_and_notification_checkpoint_cannot_upgrade(collab):
    c = setup(collab)
    old = send(c)
    notify = connection(c, 'notification_only')
    later = send(c)
    notify_page = inbox(c, notify)
    assert all(item['next_action'] == 'report' for item in notify_page['items'])
    assert_denied('INVALID_CURSOR', public_inbox, c,
        {**notify['inbox_request']['arguments'], 'mode': 'managed_execution'})
    managed = connection(c)
    page = inbox(c, managed)
    assert {item['delegation_id'] for item in page['items']} == {old['delegation_id'], later['delegation_id']}
    assert all(item['category'] == 'report_only' for item in page['items'])
    assert all(item['historical_report_only'] for item in page['items'])
    future = send(c)
    resumed = public_inbox(c, page['resume_request']['arguments'])
    assert [item['delegation_id'] for item in resumed['items'] if item['category'] == 'claimable'] == [future['delegation_id']]
    assert resumed['checkpoint'] == managed['checkpoint']


def test_initial_history_running_is_not_resumed_but_pending_original_operation_can_be_polled(collab):
    c = setup(collab)
    sent = send(c)
    item, _ = claim(c, sent)
    connected = connection(c)
    assert connected['next_action']['code'] != 'continue_leased_work'
    assert connection(c, 'notification_only')['next_action']['code'] != 'continue_leased_work'
    page = inbox(c, connected)
    assert page['items'][0]['category'] == 'report_only'
    assert page['items'][0]['next_action'] == 'report'
    operation, _ = admit(c, sent, item)
    pending = inbox(c, connected)['items'][0]
    assert pending['next_action'] == 'poll_original_operations'
    assert pending['operations'][0]['operation_id'] == operation['operation_id']
    complete(c, operation)
    settled = inbox(c, connected)['items'][0]
    assert settled['next_action'] == 'report'


def test_current_inbox_pagination_reordering_duplicate_events_and_owner_redispatch(collab):
    c = setup(collab)
    historical = send(c)
    connected = connection(c)
    first, second = send(c), send(c)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = inbox(c, connected, limit=1)
    rows = list(page['items'])
    while page['next_cursor']:
        page = inbox(c, connected, limit=1, cursor=page['next_cursor'])
        rows.extend(page['items'])
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert len(rows) == 3 and len({r['work_item_id'] for r in rows}) == 3
    assert {r['delegation_id'] for r in rows if r['category'] == 'claimable'} == {first['delegation_id'], second['delegation_id']}
    assert 'cursor' not in page['resume_request']['arguments']
    initial = inbox(c, connected, limit=1)
    goal = c.s.coordination.object(c.room, first['goal_id'])
    work = c.s.coordination.work(goal, first['work_item_id'])
    count = c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (DELEGATION_EVENT,))['n']
    c.s.delegation.emit_work(c.room, goal, work)
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?', (DELEGATION_EVENT,))['n'] == count
    c.s.delegation.remind({**c.scope, 'delegation_id': historical['delegation_id'], 'idempotency_key': key()}, c.owner)
    after = inbox(c, connected, limit=1, cursor=initial['next_cursor'])
    assert after['reconcile_required'] is True
    resumed = public_inbox(c, after['resume_request']['arguments'])
    # resume uses limit=1, so consume all temporary pages before asserting.
    found = list(resumed['items'])
    while resumed['next_cursor']:
        resumed = inbox(c, connected, limit=1, cursor=resumed['next_cursor'])
        found.extend(resumed['items'])
    assert next(row for row in found if row['delegation_id'] == historical['delegation_id'])['category'] == 'claimable'


def test_two_tasks_one_wake_claim_conflicts_continue_and_pending_never_resubmits(collab):
    c = setup(collab)
    connected = connection(c)
    first, second = send(c), send(c)
    assert len([row for row in inbox(c, connected)['items'] if row['category'] == 'claimable']) == 2
    one, claim_raw = claim(c, first)
    assert c.s.coordination.work_claim(claim_raw, c.actor)['work_item']['attempt'] == one['attempt']
    with pytest.raises(DevError):
        claim(c, first)
    two, _ = claim(c, second)
    assert {row['category'] for row in inbox(c, connected)['items']} == {'own_running'}
    for sent, item in ((first, one), (second, two)):
        heartbeat = c.s.coordination.heartbeat(lease(c, sent, item), c.actor)['work_item']
        assert heartbeat['attempt'] == item['attempt'] and heartbeat['fencing_token'] == item['fencing_token']
        operation, raw = admit(c, sent, item)
        assert c.s.coordination.admit(raw, c.actor)['operation_id'] == operation['operation_id']
        page = inbox(c, connected)
        pending = next(row for row in page['items'] if row['delegation_id'] == sent['delegation_id'])
        assert pending['category'] == 'operation_review' and pending['next_action'] == 'poll_original_operations'
        complete(c, operation)
        c.s.coordination.result({**lease(c, sent, item), 'outcome': 'succeeded',
            'summary': 'Actual fixture check', 'operation_ids': [operation['operation_id']]}, c.actor)
    final = inbox(c, connected)
    assert all(row['category'] == 'terminal' for row in final['items'])
    assert len(c.s.store.all('SELECT * FROM operations')) == 2
    replies = c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='delegation_result'")
    assert {row['reply_to_id'] for row in replies} == {first['message']['id'], second['message']['id']}
    status = connection(c)['status']
    assert status['result']['count'] == 2 and status['operation']['pending_count'] == 0
    assert status['model_online'] == status['consumer_mode'] == 'unknown'


def test_unknown_operation_expired_lease_and_owner_retry_do_not_duplicate_admission(collab):
    c = setup(collab)
    connected = connection(c)
    sent = send(c)
    item, _ = claim(c, sent)
    operation, raw = admit(c, sent, item)
    c.s.store.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation['operation_id'],))
    pending = inbox(c, connected)['items'][0]
    assert pending['category'] == 'operation_review'
    assert connection(c)['status']['operation']['unknown_count'] == 1
    c.clock[0] = item['lease_until'] + 1
    c.s.coordination.reconcile()
    with pytest.raises(DevError):
        claim(c, sent)
    reminded = c.s.delegation.remind({**c.scope, 'delegation_id': sent['delegation_id'],
        'retry_blocked': True, 'idempotency_key': key()}, c.owner)
    assert reminded['work_items_requeued'] == 0
    assert len(c.s.store.all('SELECT * FROM operations')) == 1
    assert inbox(c, connected)['items'][0]['operations'][0]['operation_id'] == operation['operation_id']


def test_cursor_policy_mode_and_owner_source_are_rechecked(collab):
    c = setup(collab)
    connected = connection(c)
    sent = send(c)
    send(c)
    first = inbox(c, connected, limit=1)
    assert first['next_cursor']
    assert_denied('INVALID_CURSOR', inbox, c, connected, cursor=first['next_cursor'] + 'x')
    other = setup(collab)
    assert_denied('INVALID_CURSOR', inbox, other, {**connected, 'inbox_request': {
        'arguments': {**connected['inbox_request']['arguments'], 'policy_id': other.policy['id']}}})
    c.s.store.execute('UPDATE collaboration_messages SET version=version+1 WHERE id=?', (sent['message']['id'],))
    page = inbox(c, connected)
    altered = next(row for row in page['items'] if row['delegation_id'] == sent['delegation_id'])
    assert altered['category'] == 'blocked' and altered['reason_code'] == 'DELEGATION_MESSAGE_CHANGED'
    # Supplying a valid reference never grants an unrelated connection access.
    outsider = replace(c.actor, grant_id='second')
    with pytest.raises(DevError):
        public_inbox(c, connected['inbox_request']['arguments'], outsider)


def test_omitted_subset_keeps_existing_message_replay_fingerprint(collab):
    c = setup(collab, capabilities=['read'])
    raw = request(c)
    sent = c.s.chatroom.create(raw, c.owner)
    stored = c.s.store.one('SELECT body FROM collaboration_messages WHERE id=?', (sent['message']['id'],))
    old_normalized = {**raw, 'reply_to_id': ''}
    assert json.loads(stored['body'])['request_digest'] == digest({k: v for k, v in old_normalized.items() if k != 'idempotency_key'})
    assert c.s.chatroom.create({**raw, 'idempotency_key': key()}, c.owner)['delegation_id'] == sent['delegation_id']


def test_every_public_query_action_is_read_only_in_real_business_tables(collab):
    import json
    from datetime import datetime, timezone
    from hub.collaboration.common import canonical, digest
    from hub.collaboration.monitor import MonitorService
    from shared.public_collaboration import ACTIONS

    c = setup(collab)
    s = c.s
    monitor = MonitorService(s)
    s.monitor = monitor
    probe = monitor.register_probe({**c.scope, 'label': 'query fixture',
        'url': 'https://probe.example.invalid/query', 'idempotency_key': key()}, c.owner)['id']
    candidate = {'intent': 'Validate only', 'valid_until': datetime.fromtimestamp(
        c.clock[0] + 3600, timezone.utc).isoformat(), 'rules': [{
        'rule_id': 'available', 'probe_id': probe, 'metric': 'availability',
        'open_when': {'operator': 'lt', 'value': 0.5},
        'close_when': {'operator': 'gt', 'value': 0.9}, 'require_recovery_probe': probe}]}
    connected = connection(c)
    delegated = send(c)
    message_key = key()
    message = s.chatroom.create({**c.scope, 'room_id': c.room['id'], 'conversation_id': c.room['id'],
        'body_text': 'Query fixture discussion', 'client_message_id': message_key,
        'idempotency_key': key()}, c.owner)['message']
    legacy = s.command({**c.scope, 'room_id': c.room['id'], 'request': 'Read fixture',
        'structured_mentions': [{'agent_id': collab[5][0]['id']}],
        'source_message_id': key(), 'idempotency_key': key()}, c.owner, from_panel=True)
    leased = s.claim({**c.scope, 'job_id': legacy['job_id'], 'expected_version': 1,
                      'idempotency_key': key()}, collab[2])
    evidence_id = 'query-evidence'
    evidence = {'observed': 'fixture'}
    s.store.execute('INSERT INTO monitor_evidence VALUES (?,?,?,?,?,?,?,?,?)',
        (evidence_id, c.room['id'], None, canonical(evidence), digest(evidence), c.clock[0],
         c.clock[0] + 3600, 1, 'aggregate_probe'))
    job = s.object('collaboration_jobs', c.room, leased['job_id'])
    context = json.loads(job['context'])
    context['evidence_refs'] = [evidence_id]
    s.store.execute('UPDATE collaboration_jobs SET context=? WHERE id=?', (canonical(context), job['id']))
    result = s.submit({**c.scope, 'job_id': job['id'], 'attempt': leased['attempt'],
        'fencing_token': leased['fencing_token'], 'idempotency_key': key(),
        'result': {'outcome': 'explained', 'summary': 'Synthetic fixture evidence',
                   'observations': [{'claim': 'Fixture', 'evidence_refs': [evidence_id]}]}}, collab[2])
    queries = {action: {} for action in ('overview', 'rooms', 'jobs', 'messages', 'goals',
        'incidents', 'agents', 'subscriptions', 'join_slots', 'plan', 'timeline', 'members',
        'changes', 'coordination_goals', 'coordination_options', 'delegation_policies')}
    queries.update({
        'job': {'id': job['id']}, 'result': {'id': result['result_id']},
        'thread': {'id': message['id']}, 'search': {'query': 'fixture'},
        'message_status': {'client_message_id': message_key}, 'message_by_id': {'id': message['id']},
        'coordination_goal': {'id': delegated['goal_id']},
        'result_evidence': {'id': evidence_id, 'result_id': result['result_id']},
        'goal': {'goal_id': delegated['goal_id']},
        'delegation': {'delegation_id': delegated['delegation_id']},
        'connection': {'policy_id': c.policy['id'], 'policy_version': c.policy['version'], 'mode': 'managed_execution'},
        'inbox': {k: v for k, v in connected['inbox_request']['arguments'].items() if k not in {'action', 'project', 'environment_id'}},
        'plan_validate': {'candidate': candidate},
    })
    assert set(queries) == set(ACTIONS['collaboration_query'])
    tables = [r['name'] for r in s.store.all(
        "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'collaboration_%' "
        "OR name LIKE 'coordination_%' OR name LIKE 'delegation_%' OR name LIKE 'monitor_%' "
        "OR name LIKE 'mcp_event_%' OR name LIKE 'conversation_%' OR name='operations')")]
    def snapshot():
        return {table: s.store.all('SELECT * FROM "' + table + '" ORDER BY rowid') for table in tables}
    before = snapshot()
    for action, arguments in queries.items():
        actor = c.actor if action in {'goal', 'delegation', 'connection', 'inbox'} else c.owner
        assert s.invoke('collaboration_query', {'action': action, **c.scope, **arguments}, actor) is not None
        assert snapshot() == before, action
