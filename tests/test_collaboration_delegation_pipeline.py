"""Panel-to-consumer recovery for an old tool catalog, with no live subscriptions."""
import json
from dataclasses import replace

import pytest

from hub.vps import VPSInput
from shared.collaboration_contracts import Read
from shared.public_collaboration import resolve
from shared.util import DevError
from tests.collaboration_support import collab, collaboration_stack, key  # noqa: F401
from tests.test_collaboration_delegation_consumer import setup, send, connection, lease, complete


def old_call(c, request):
    if request['tool'] == 'collaboration_read':
        Read.model_validate(request['arguments'])  # The pre-rescan schema already accepts every field.
    return c.s.invoke(request['tool'], request['arguments'], c.actor)


def old_connection(c, mode='managed_execution'):
    return old_call(c, c.policy['consumer_contracts'][mode]['legacy_read_request'])


def test_legacy_static_wake_recovers_new_work_without_event_payload_and_returns_exact_thread(collab):
    c = setup(collab)
    historical = send(c)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    connected = old_connection(c)
    saved = json.loads(json.dumps(connected['consumer_configuration']))
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert saved['must_persist_before_subscription'] and not saved['grants_authority']
    assert json.dumps(saved['legacy_inbox_request'], ensure_ascii=False, separators=(',', ':')) in saved['wake_instructions']
    first, second = send(c), send(c)
    request = {**saved['legacy_inbox_request'], 'arguments': {**saved['legacy_inbox_request']['arguments'], 'limit': 1}}
    items = []
    while request:
        page = old_call(c, request)
        items.extend(page['items'])
        request = page['legacy_next_page_request']
    assert {item['delegation_id'] for item in items if item['category'] == 'claimable'} == {first['delegation_id'], second['delegation_id']}
    assert next(item for item in items if item['delegation_id'] == historical['delegation_id'])['historical_report_only']
    for item in items:
        if item['category'] != 'claimable':
            continue
        fresh = old_call(c, item['legacy_read_request'])
        assert fresh['trusted_author']['kind'] == 'panel_owner' and fresh['trusted_author']['authenticated']
        work = old_call(c, item['legacy_claim_request'])['work_item']
        assert old_call(c, item['legacy_claim_request'])['work_item']['attempt'] == work['attempt']
        # The primary tool uses exactly the same claim key and business receipt.
        assert old_call(c, item['claim_request'])['work_item']['attempt'] == work['attempt']
        raw = {**lease(c, fresh['delegation'], work), 'tool': 'exec',
               'arguments': {'target': 'agent', 'command': 'printf pipeline-fixture'}}
        admitted = c.s.coordination.admit(raw, c.actor)
        assert c.s.coordination.admit(raw, c.actor)['operation_id'] == admitted['operation_id']
        assert any(row['category'] == 'operation_review' for row in old_call(c, saved['legacy_inbox_request'])['items'])
        with pytest.raises(DevError):
            c.s.coordination.result({**lease(c, fresh['delegation'], work), 'outcome': 'succeeded',
                'summary': 'Cannot complete pending operations', 'operation_ids': [admitted['operation_id']]}, c.actor)
        complete(c, admitted)
        result = {**lease(c, fresh['delegation'], work), 'outcome': 'succeeded', 'summary': 'Verified fixture result',
                  'operation_ids': [admitted['operation_id']]}
        c.s.invoke('collaboration_work_result', result, c.actor)
        c.s.invoke('collaboration_work_result', result, c.actor)
        replies = c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='delegation_result' AND reply_to_id=?",
                               (fresh['delegation']['message_id'],))
        assert len(replies) == 1 and replies[0]['conversation_id'] == c.room['id']
        assert json.loads(replies[0]['body'])['execution_verified']
    assert len(c.s.store.all('SELECT * FROM operations')) == 2
    assert old_call(c, page['legacy_resume_request'])['checkpoint'] == saved['checkpoint']


def test_legacy_notification_mode_missing_baseline_and_scope_tampering_fail_closed(collab):
    c = setup(collab)
    connected = old_connection(c, 'notification_only')
    send(c)
    request = connected['legacy_inbox_request']
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = old_call(c, request)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert page['items'][0]['next_action'] == 'report'
    assert page['items'][0]['legacy_claim_request'] is None
    mutations = [
        {'query': request['arguments']['query'].replace('notification_only', 'managed_execution')},
        {'after': ''}, {'after': request['arguments']['after'] + 'x'},
        {'query': request['arguments']['query'].replace(':inbox:', ':execute:')},
        {'query': request['arguments']['query'].rsplit(':', 1)[0] + ':2'},
        {'conversation_id': 'another-room'}, {'job_id': 'unrelated'},
    ]
    for changes in mutations:
        with pytest.raises(DevError):
            old_call(c, {**request, 'arguments': {**request['arguments'], **changes}})
    with pytest.raises(DevError):
        c.s.invoke(request['tool'], request['arguments'], replace(c.actor, grant_id='second'))
    other = setup(collab)
    with pytest.raises(DevError):
        old_call(c, {**request, 'arguments': {**request['arguments'], 'id': other.policy['id']}})
    assert not c.s.store.all('SELECT * FROM operations')
    assert all(item['attempt'] == 0 for item in c.s.store.all('SELECT * FROM coordination_work'))


def test_legacy_reconnect_does_not_replay_history_and_revocation_stops_reads(collab):
    c = setup(collab)
    initial = old_connection(c)
    sent = send(c)
    assert old_call(c, initial['legacy_inbox_request'])['items'][0]['category'] == 'claimable'
    restarted = old_connection(c)
    assert old_call(c, restarted['legacy_inbox_request'])['items'][0]['category'] == 'report_only'
    c.s.delegation.remind({**c.scope, 'delegation_id': sent['delegation_id'], 'idempotency_key': key()}, c.owner)
    assert old_call(c, restarted['legacy_inbox_request'])['items'][0]['category'] == 'claimable'
    c.s.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    with pytest.raises(DevError):
        old_call(c, initial['legacy_inbox_request'])


def test_project_target_candidates_are_exact_complete_and_never_expand_old_policy(collab):
    c = setup(collab)
    # More than the old UI's one API page, including a disabled connection and an unbound server.
    for i in range(202):
        c.s.runtime.vps.save(VPSInput(name=f'Fixture {i:03}', host=f'fixture-{i}.example.invalid',
            username='fixture', password='synthetic-only', project_ids=['proj'], enabled=i != 0), c.owner)
    other = c.s.runtime.vps.save(VPSInput(name='Not this project', host='other.example.invalid',
        username='fixture', password='synthetic-only', project_ids=[]), c.owner)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    records = c.s.read({**c.scope, 'kind': 'delegation_policies'}, c.owner)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    targets = records['execution_target_candidates']
    assert len(targets) == 203 and all(row['project_id'] == 'proj' for row in targets)
    assert other['target'] not in {row['id'] for row in targets}
    assert next(row for row in targets if row['label'] == 'VPS · Fixture 000')['reason_code'] == 'VPS_DISABLED'
    assert records['items'][0]['execution_targets'] == ['project_agent']
    assert records['target_discovery_changes_policy'] is False
    assert c.s.read({**c.scope, 'kind': 'delegation_policies'}, c.actor)['execution_target_candidates'] == []
    # The public query still supports its original no-filter list, and can route the explicit v1 read.
    name, raw = resolve('collaboration_query', {'action': 'delegation_policies', **c.scope})
    assert name == 'collaboration_read' and c.s.read(raw, c.owner)['items']
    legacy = c.policy['consumer_contracts']['managed_execution']['legacy_read_request']['arguments']
    name, raw = resolve('collaboration_query', {'action': 'delegation_policies', **{k:v for k,v in legacy.items() if k != 'kind'}})
    assert c.s.read(raw, c.actor)['checkpoint']


@pytest.mark.integration
@pytest.mark.parametrize('automatic_dispatch', [False, True])
def test_old_catalog_real_agent_read_finishes_with_one_original_topic_result(collaboration_stack, automatic_dispatch):
    stack = collaboration_stack
    scope = {'project': stack.project['id'], 'environment_id': 'production'}
    def mcp(request):
        result = stack.mcp(request['tool'], request['arguments'])
        assert not result.get('isError'), result
        return result.get('structuredContent') or json.loads(result['content'][0]['text'])
    room = stack.must(stack.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))
    # Room setup is supplied by the shared isolated stack in some configurations.
    room = stack.must(stack.client.get('/api/collaboration', params=scope))['room']
    slot = stack.must(stack.client.post('/api/collaboration/join-slot', json={
        **scope, 'label': 'Pipeline consumer', 'kind': 'dot', 'idempotency_key': key()}))['slot']
    mcp({'tool': 'collaboration_join', 'arguments': {'code': slot['join_code'], 'idempotency_key': key()}})
    policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
        **scope, 'conversation_id': room['id'], 'slot_id': slot['id'], 'expected_version': 0,
        'purpose': 'Read the isolated README', 'capabilities': ['read'], 'execution_targets': ['project_agent'],
        'automatic_delegation': automatic_dispatch, 'idempotency_key': key()}))['policy']
    connected = json.loads(json.dumps(mcp(policy['consumer_contracts']['managed_execution']['legacy_read_request'])))
    # Restart only the fixture Hub; saved configuration must remain usable.
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.start_hub()
    assert mcp(connected['legacy_inbox_request'])['checkpoint'] == connected['checkpoint']
    dispatch = {'dispatch_mode': 'automatic', 'automatic_policy_version': policy['version']} if automatic_dispatch else {
        'delegation': {'policy_id': policy['id'], 'policy_version': policy['version'], 'acceptance': 'Actual content receipt'}}
    source_args = {**scope, 'room_id': room['id'], 'conversation_id': room['id'], 'body_text': 'Read README and report here',
        'mentions': [{'slot_id': slot['id']}], 'client_message_id': key(), 'idempotency_key': key(), **dispatch}
    source = stack.must(stack.client.post('/api/collaboration/message', json=source_args))
    repeat = stack.must(stack.client.post('/api/collaboration/message', json=source_args))
    assert repeat['delegation_id'] == source['delegation_id']
    assert source['message']['body']['delegation']['automatic'] is automatic_dispatch
    discovered = mcp({'tool': 'collaboration_query', 'arguments': {
        'action': 'delegations', **scope, 'conversation_id': room['id']}})
    assert len(discovered['items']) == 1 and discovered['items'][0]['delegation_id'] == source['delegation_id']
    assert mcp(discovered['items'][0]['read_request'])['delegation']['id'] == source['delegation_id']
    inbox = mcp(connected['legacy_inbox_request'])
    item = inbox['items'][0]
    assert item['category'] == 'claimable'
    fresh = mcp(item['legacy_read_request'])
    assert fresh['trusted_author']['authenticated']
    work = mcp(item['legacy_claim_request'])['work_item']
    lease_args = {**scope, 'goal_id': work['goal_id'], 'work_item_id': work['id'],
                  'attempt': work['attempt'], 'fencing_token': work['fencing_token']}
    op = mcp({'tool': 'collaboration_work_execute', 'arguments': {**lease_args,
        'tool': 'read', 'arguments': {'path': 'README.md'}, 'idempotency_key': key()}})
    receipt = stack.poll(op['operation_id'], timeout=20)
    assert receipt['state'] == 'succeeded'
    assert 'Integration fixture' in receipt['result']['data']['content']
    result = {'tool': 'collaboration_work_result', 'arguments': {**lease_args, 'outcome': 'succeeded',
        'summary': 'Read isolated fixture README', 'operation_ids': [op['operation_id']], 'idempotency_key': key()}}
    mcp(result)
    mcp(result)
    messages = stack.must(stack.client.get('/api/collaboration', params=scope))['messages']
    replies = [message for message in messages if message['kind'] == 'delegation_result']
    assert len(replies) == 1 and replies[0]['reply_to_id'] == source['message']['id']
    assert replies[0]['body']['execution_verified']
    assert mcp(inbox['legacy_resume_request'])['items'][0]['category'] == 'terminal'


def test_legacy_claim_preserves_exact_saved_vps_target_and_fences_every_step(collab):
    s, owner = collab[:2]
    vps = s.runtime.vps.save(VPSInput(name='Pipeline selected target', host='pipeline-target.example.invalid',
        username='fixture', password='synthetic-only', project_ids=['proj']), owner)
    c = setup(collab, capabilities=['read', 'execute'], execution_targets=[vps['target']])
    connected = old_connection(c)
    sent = send(c)
    item = old_call(c, connected['legacy_inbox_request'])['items'][0]
    fresh = old_call(c, item['legacy_read_request'])
    assert fresh['request_scope']['execution_targets'] == [vps['target']]
    work = old_call(c, item['legacy_claim_request'])['work_item']
    raw = {**lease(c, sent, work), 'tool': 'exec',
           'arguments': {'target': vps['target'], 'command': 'printf synthetic-vps-route'}}
    admitted = c.s.coordination.admit(raw, c.actor)
    assert c.s.coordination.admit(raw, c.actor)['operation_id'] == admitted['operation_id']
    with pytest.raises(DevError):
        c.s.coordination.admit({**raw, 'idempotency_key': key(), 'fencing_token': work['fencing_token'] + 1}, c.actor)
    with pytest.raises(DevError):
        c.s.coordination.admit({**raw, 'idempotency_key': key(),
            'arguments': {**raw['arguments'], 'target': 'agent'}}, c.actor)
    assert len(c.s.store.all('SELECT * FROM operations')) == 1
    complete(c, admitted)
    c.s.coordination.result({**lease(c, sent, work), 'outcome': 'succeeded', 'summary': 'Synthetic VPS route verified',
        'operation_ids': [admitted['operation_id']]}, c.actor)
    assert old_call(c, connected['legacy_inbox_request'])['items'][0]['category'] == 'terminal'


def test_missing_room_has_exact_read_only_setup_diagnosis(collab):
    s, owner = collab[:2]
    before = s.store.one('SELECT total_changes() AS n')['n']
    response = s.read({'project': 'proj', 'environment_id': 'missing-room', 'kind': 'delegation_policies'}, owner)
    recovery = response['delegation_recovery']
    assert response['setup_required'] and response['room'] is None
    assert recovery['reason_code'] == 'COLLABORATION_ROOM_REQUIRED'
    assert recovery['project_id'] == 'proj' and recovery['environment_id'] == 'missing-room'
    assert recovery['visibility'] == 'current_connection_only'
    assert not recovery['permissions_changed'] and not recovery['grants_authority']
    assert s.invoke(recovery['read_request']['tool'], recovery['read_request']['arguments'], owner)['items']
    assert s.store.one('SELECT total_changes() AS n')['n'] == before


def test_missing_policy_and_missing_id_have_distinct_read_only_recovery(collab):
    s, owner, _, _, room, _, _, scope = collab
    before = s.store.one('SELECT total_changes() AS n')['n']
    empty = s.read({**scope, 'kind': 'delegation_policies'}, owner)
    assert empty['delegation_recovery']['reason_code'] == 'DELEGATION_POLICY_REQUIRED'
    assert empty['items'] == []
    assert s.store.one('SELECT total_changes() AS n')['n'] == before
    c = setup(collab)
    connected = old_connection(c)
    sent = send(c)
    before = s.store.one('SELECT total_changes() AS n')['n']
    visible = s.read({**scope, 'kind': 'delegation_policies'}, c.actor)
    recovery = visible['delegation_recovery']
    assert recovery['reason_code'] == 'SAVED_INBOX_REQUIRED'
    assert recovery['next_action'] == 'resume_saved_inbox' and recovery['read_request'] is None
    assert not recovery['grants_authority']
    page = old_call(c, connected['legacy_inbox_request'])
    assert page['items'][0]['delegation_id'] == sent['delegation_id']
    assert page['checkpoint'] == connected['checkpoint']
    assert s.store.one('SELECT total_changes() AS n')['n'] == before
    assert not s.store.all('SELECT * FROM operations')
    assert all(row['attempt'] == 0 for row in s.store.all('SELECT * FROM coordination_work'))
