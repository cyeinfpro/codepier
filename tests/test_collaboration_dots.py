"""One-code task dot lifecycle in isolated fixtures; not a claim of a live dot host."""
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hub.collaboration.common import DELEGATION_EVENT
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


def setup(fixture, *, join=True, **changes):
    s, owner, worker, _, room, _, clock, scope = fixture
    s.runtime.collaboration = s
    actor = replace(worker, grant_id='broad', actor='mcp:broad:dot')
    raw = {**scope, 'conversation_id': room['id'], 'label': '任务 dot',
           'capabilities': ['read', 'write', 'execute'], 'acknowledge_unsandboxed_exec': True,
           'confirm_tasks': True, 'idempotency_key': key(), **changes}
    created = s.dots.create(raw, owner)
    c = SimpleNamespace(s=s, owner=owner, actor=actor, room=room, clock=clock, scope=scope,
                        raw=raw, created=created, slot=created['dot'])
    c.join_raw = {'code': c.slot['join_code'], 'idempotency_key': key()}
    if join:
        c.connected = s.invoke('collaboration', {'action': 'join', **c.join_raw}, actor)
        c.slot = c.connected['slot']
    return c


def call(c, request, actor=None):
    return c.s.invoke(request['tool'], request['arguments'], actor or c.actor)


def send(c, **changes):
    raw = {**c.scope, 'room_id': c.room['id'], 'conversation_id': c.slot['conversation_id'],
           'body_text': '检查 README 并回报实际结果', 'mentions': [{'slot_id': c.slot['id']}],
           'dispatch_mode': 'automatic', 'automatic_policy_version': c.slot['policy_version'],
           'client_message_id': key(), 'idempotency_key': key(), **changes}
    return c.s.chatroom.create(raw, c.owner), raw


def lease(c, item):
    return {**c.scope, 'goal_id': item['goal_id'], 'work_item_id': item['id'],
            'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'idempotency_key': key()}


def test_one_panel_create_one_join_one_subscription_without_grant_changes(collab):
    c = setup(collab, join=False)
    before = c.s.store.all('SELECT * FROM grants ORDER BY id')
    assert c.slot['join_code'].startswith('CPD-')
    assert not c.s.store.all('SELECT * FROM delegation_policies')
    assert not c.s.store.all('SELECT * FROM coordination_work')
    assert c.s.dots.create(c.raw, c.owner)['dot']['id'] == c.slot['id']
    joined = c.s.joining.join(c.join_raw, c.actor)
    assert joined['registered'] and joined['managed_scope_activated']
    assert not joined['permissions_changed'] and not joined['subscription_created']
    assert not joined['chat_identity_verified']
    assert joined['subscription_requests'] == [joined['subscription_request']]
    assert joined['subscription_request']['name'] == DELEGATION_EVENT
    assert len(c.s.store.all('SELECT * FROM delegation_policies')) == 1
    assert before == c.s.store.all('SELECT * FROM grants ORDER BY id')
    assert not c.s.store.all('SELECT * FROM coordination_work')
    assert not c.s.store.all('SELECT * FROM mcp_event_subscriptions')
    assert joined['consumer_configuration']['checkpoint_storage'] == 'server_persisted_once'
    assert joined['consumer_configuration']['mode_evidence'] == 'requested_only'
    assert joined['slot']['expected_subscription_count'] == 1


def test_rejoin_and_restart_contract_preserve_initial_checkpoint_and_all_new_tasks(collab):
    c = setup(collab)
    baseline = c.s.dots.find(c.slot['id'])['checkpoint']
    first, _ = send(c)
    for _ in range(3):
        repeated = c.s.joining.join({**c.join_raw, 'idempotency_key': key()}, c.actor)
        assert repeated['inbox_request'] == c.connected['inbox_request']
        assert c.s.dots.find(c.slot['id'])['checkpoint'] == baseline
    second, _ = send(c)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    resumed = c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)
    page = call(c, resumed['inbox_request'])
    assert {i['delegation_id'] for i in page['items'] if i['category'] == 'claimable'} == {first['delegation_id'], second['delegation_id']}
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert page['checkpoint'] == baseline and page['resume_request'] == {**resumed['inbox_request'], 'arguments': {**resumed['inbox_request']['arguments'], 'limit': 40}}
    assert len(c.s.store.all('SELECT * FROM delegation_policies')) == 1


def test_inbox_pages_resume_from_server_baseline_not_model_checkpoint(collab):
    c = setup(collab)
    sent = [send(c)[0] for _ in range(3)]
    request = {**c.connected['inbox_request'], 'arguments': {**c.connected['inbox_request']['arguments'], 'limit': 1}}
    rows = []
    while request:
        page = call(c, request)
        rows += page['items']
        request = page['next_page_request']
        if request:
            assert request['arguments']['action'] == 'dot_inbox'
    assert {r['delegation_id'] for r in rows} == {s['delegation_id'] for s in sent}
    with pytest.raises(DevError):
        call(c, {**c.connected['inbox_request'], 'arguments': {**c.connected['inbox_request']['arguments'], 'checkpoint': 'model-chosen'}})


def test_progress_and_final_result_are_idempotent_named_replies_to_original_message(collab):
    c = setup(collab)
    source, raw = send(c)
    assert c.s.chatroom.create(raw, c.owner)['message']['id'] == source['message']['id']
    pending = call(c, c.connected['inbox_request'])['items'][0]
    assert call(c, pending['read_request'])['trusted_author']['authenticated']
    work = call(c, pending['claim_request'])['work_item']
    call(c, pending['claim_request'])
    progress = {'action': 'progress', **lease(c, work), 'summary': '正在读取并核对 README。'}
    first = c.s.invoke('collaboration_work', progress, c.actor)
    repeated = c.s.invoke('collaboration_work', progress, c.actor)
    assert repeated['message_id'] == first['message_id']
    command = {**lease(c, work), 'tool': 'exec', 'arguments': {'command': 'printf synthetic-dot-receipt'}}
    operation = c.s.coordination.admit(command, c.actor)
    assert c.s.coordination.admit(command, c.actor)['operation_id'] == operation['operation_id']
    with pytest.raises(DevError):
        c.s.coordination.result({**lease(c, work), 'outcome': 'succeeded', 'summary': 'Not finished', 'operation_ids': [operation['operation_id']]}, c.actor)
    row = c.s.store.one('SELECT * FROM operations WHERE id=?', (operation['operation_id'],))
    c.s.runtime.complete(row, {'ok': True, 'data': {'exit_code': 0, 'output': 'synthetic-dot-receipt'}})
    result = {**lease(c, work), 'outcome': 'succeeded', 'summary': '实际回执已核对。', 'operation_ids': [operation['operation_id']]}
    c.s.coordination.result(result, c.actor)
    c.s.coordination.result(result, c.actor)
    replies = c.s.store.all('SELECT * FROM collaboration_messages WHERE reply_to_id=?', (source['message']['id'],))
    assert len(replies) == 3  # claim receipt, one progress, one final result
    assert sum(r['kind'] == 'delegation_result' for r in replies) == 1
    assert all(r['conversation_id'] == c.slot['conversation_id'] for r in replies)
    assert all(c.s.chatroom.view(r)['display_name'] == '任务 dot' for r in replies)
    assert all(not json.loads(r['body'])['notifications'] for r in replies)
    assert len(c.s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (DELEGATION_EVENT,))) == 1
    assert len(c.s.store.all('SELECT * FROM operations')) == 1


@pytest.mark.parametrize('field,value', [('attempt', 2), ('fencing_token', 100)])
def test_progress_requires_exact_live_fence(collab, field, value):
    c = setup(collab)
    send(c)
    work = call(c, call(c, c.connected['inbox_request'])['items'][0]['claim_request'])['work_item']
    with pytest.raises(DevError):
        c.s.coordination.progress({**lease(c, work), field: value, 'summary': 'must reject'}, c.actor)
    assert not c.s.store.all("SELECT * FROM collaboration_messages WHERE id LIKE 'delegation-progress:%'")


@pytest.mark.parametrize('target', ['join', 'inbox', 'connection'])
def test_other_grant_cannot_take_registered_dot(collab, target):
    c = setup(collab)
    other = replace(c.actor, grant_id='second', actor='mcp:second:fixture')
    with pytest.raises(DevError):
        if target == 'join':
            c.s.joining.join({**c.join_raw, 'idempotency_key': key()}, other)
        elif target == 'inbox':
            call(c, c.connected['inbox_request'], other)
        else:
            c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, other)


def test_insufficient_grant_cannot_activate_preapproved_scope_or_consume_code(collab):
    c = setup(collab, join=False)
    with pytest.raises(DevError):
        c.s.joining.join(c.join_raw, collab[2])
    assert c.s.joining.slot(c.room, c.slot['id'])['state'] == 'invited'
    assert not c.s.store.all('SELECT * FROM delegation_policies')
    assert c.s.joining.join(c.join_raw, c.actor)['registered']


@pytest.mark.parametrize('change', ["UPDATE projects SET root='/changed' WHERE id='proj'", "UPDATE projects SET mode='read' WHERE id='proj'"])
def test_owner_approved_project_change_blocks_enrollment(collab, change):
    c = setup(collab, join=False)
    c.s.store.execute(change)
    with pytest.raises(DevError):
        c.s.joining.join(c.join_raw, c.actor)
    assert not c.s.store.all('SELECT * FROM delegation_policies')


def test_revoke_stops_pending_work_and_original_checkpoint_cannot_reactivate(collab):
    c = setup(collab)
    send(c)
    current = c.s.joining.slot(c.room, c.slot['id'])
    c.s.joining.control({**c.scope, 'slot_id': current['id'], 'expected_version': current['version'],
                        'action': 'revoke', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        call(c, c.connected['inbox_request'])
    with pytest.raises(DevError):
        c.s.joining.join(c.join_raw, c.actor)
    assert all(r['state'] != 'active' for r in c.s.store.all('SELECT * FROM coordination_goals'))
    assert not c.s.store.all('SELECT * FROM operations')


def test_expired_code_can_recover_existing_dot_without_new_enrollment(collab):
    c = setup(collab)
    c.clock[0] += 1801
    with pytest.raises(DevError) as exc:
        c.s.joining.join({**c.join_raw, 'idempotency_key': key()}, c.actor)
    assert exc.value.code == 'JOIN_CODE_EXPIRED'
    assert c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)['inbox_request'] == c.connected['inbox_request']


def test_refresh_code_keeps_cpd_intent_and_invalidates_old_code(collab):
    c = setup(collab, join=False)
    refreshed = c.s.joining.control({**c.scope, 'slot_id': c.slot['id'], 'expected_version': c.slot['version'],
        'action': 'refresh_code', 'idempotency_key': key()}, c.owner)['slot']
    assert refreshed['join_code'].startswith('CPD-') and refreshed['join_code'] != c.slot['join_code']
    with pytest.raises(DevError):
        c.s.joining.join(c.join_raw, c.actor)
    assert c.s.joining.join({'code': refreshed['join_code'], 'idempotency_key': key()}, c.actor)['registered']


def test_legacy_cpj_remains_notification_only(collab):
    s, owner, _, dot, *_ = collab
    slot = s.joining.create({'project': 'proj', 'label': '旧通知', 'kind': 'dot', 'idempotency_key': key()}, owner)['slot']
    response = s.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, dot)
    assert response['worker_authorized'] is False and len(response['subscription_requests']) == 4
    assert not s.store.all('SELECT * FROM delegation_policies')
    assert not s.store.all('SELECT * FROM collaboration_dots')


@pytest.mark.parametrize('changes', [{'confirm_tasks': False}, {'confirm_tasks': None}, {'capabilities': ['execute']}, {'acknowledge_unsandboxed_exec': False}])
def test_create_requires_explicit_bounded_owner_intent(collab, changes):
    with pytest.raises((DevError, ValidationError)):
        setup(collab, join=False, **changes)
    assert not collab[0].store.all('SELECT * FROM collaboration_dots')


def test_mcp_cannot_create_panel_dot_or_rewrite_enrollment(collab):
    c = setup(collab)
    with pytest.raises(DevError):
        c.s.dots.create({**c.raw, 'idempotency_key': key()}, c.actor)
    with pytest.raises(sqlite3.IntegrityError):
        c.s.store.execute('UPDATE collaboration_dots SET checkpoint=? WHERE id=?', ('replacement', c.slot['id']))
    with pytest.raises(sqlite3.IntegrityError):
        c.s.store.execute('UPDATE collaboration_dots SET setup=? WHERE id=?', ('{}', c.slot['id']))


def test_new_dot_is_scoped_to_its_conversation_not_all_project_chats(collab):
    c = setup(collab)
    another = c.s.conversations.create({'title': '另一房间', 'projects': [c.scope], 'idempotency_key': key()}, c.owner)['conversation']
    members = c.s.read({**c.scope, 'kind': 'members', 'conversation_id': another['id']}, c.owner)['items']
    assert c.slot['id'] not in {m['id'] for m in members}
    slots = c.s.read({**c.scope, 'kind': 'join_slots', 'conversation_id': another['id']}, c.owner)['items']
    assert c.slot['id'] not in {m['id'] for m in slots}


@pytest.mark.asyncio
async def test_task_dot_single_event_challenge_signed_retry_and_duplicate_wakeup(collab):
    import base64
    import hashlib
    import hmac
    import secrets
    from hub.collaboration.events import EventService, signing_key
    from tests.test_collaboration_events import Receiver

    c = setup(collab)
    c.s.config = replace(c.s.config, events_enabled=True)
    receiver = Receiver()
    c.s.events = events = EventService(c.s, receiver)
    secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
    subscription = {**c.connected['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://task-dot.example.invalid/events', 'secret': secret}}
    subscribed = await events.subscribe(subscription, c.actor)
    assert json.loads(receiver.requests[0][1])['type'] == 'verification'
    assert len(c.s.store.all('SELECT * FROM mcp_event_subscriptions')) == 1
    source, raw = send(c)
    c.s.chatroom.create(raw, c.owner)
    receiver.status = 503
    await events.tick()
    failed = receiver.requests[-1]
    assert json.loads(failed[1])['data']['delegation_id'] == source['delegation_id']
    receiver.status = 204
    c.clock[0] += 31
    await events.tick()
    accepted = receiver.requests[-1]
    assert failed[1] == accepted[1]
    assert failed[2]['webhook-signature'] != accepted[2]['webhook-signature']
    body, headers = accepted[1:]
    expected = base64.b64encode(hmac.new(signing_key(secret), headers['webhook-id'].encode()
        + b'.' + headers['webhook-timestamp'].encode() + b'.' + body, hashlib.sha256).digest()).decode()
    assert headers['webhook-signature'] == 'v1,' + expected
    assert headers['X-MCP-Subscription-Id'] == subscribed['id']
    assert json.loads(body)['data']['recipient_slot_id'] == c.slot['id']
    status = c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)['slot']['task_status']
    assert status['notification_state'] == 'active'
    assert status['notification']['accepted_count'] == 1
    assert status['model_online'] == 'unknown' and status['claim']['observed_count'] == 0
    page1 = call(c, c.connected['inbox_request'])
    page2 = call(c, c.connected['inbox_request'])
    assert page1['items'][0]['claim_request'] == page2['items'][0]['claim_request']
    first = call(c, page1['items'][0]['claim_request'])
    assert call(c, page2['items'][0]['claim_request'])['work_item']['attempt'] == first['work_item']['attempt']
    assert len(c.s.store.all('SELECT * FROM coordination_attempts')) == 1
    assert not c.s.store.all('SELECT * FROM operations')


def test_native_event_catalog_guides_cpd_to_persisted_inbox_without_upgrading_legacy():
    from hub.collaboration.event_contracts import definitions
    event = next(row for row in definitions() if row['name'] == DELEGATION_EVENT)
    assert 'CPD' in event['description'] and 'dot_inbox' in event['description']
    assert 'dot_connection' in event['description'] and 'notification_only' in event['description']
    assert 'explicitly user-approved managed_execution' in event['description']


@pytest.mark.parametrize('change', ['grant_revoked', 'slot_revoked', 'lease_expired', 'message_budget'])
def test_progress_rechecks_revocation_lease_and_budget_without_side_effects(collab, change):
    c = setup(collab)
    send(c)
    work = call(c, call(c, c.connected['inbox_request'])['items'][0]['claim_request'])['work_item']
    if change == 'grant_revoked':
        c.s.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (c.actor.grant_id,))
    elif change == 'slot_revoked':
        slot = c.s.joining.slot(c.room, c.slot['id'])
        c.s.joining.control({**c.scope, 'slot_id': slot['id'], 'expected_version': slot['version'],
                            'action': 'revoke', 'idempotency_key': key()}, c.owner)
    elif change == 'lease_expired':
        row = c.s.store.one('SELECT lease_until FROM coordination_work WHERE id=?', (work['id'],))
        c.clock[0] = row['lease_until'] + 1
    else:
        goal = c.s.store.one('SELECT spec FROM coordination_goals WHERE id=?', (work['goal_id'],))
        maximum = json.loads(goal['spec'])['budget']['max_messages']
        c.s.store.execute('UPDATE coordination_goals SET messages=? WHERE id=?', (maximum, work['goal_id']))
    before_messages = c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id')
    before_work = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (work['id'],))
    before_goal = c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (work['goal_id'],))
    with pytest.raises(DevError):
        c.s.invoke('collaboration_work', {'action': 'progress', **lease(c, work), 'summary': 'must reject'}, c.actor)
    assert c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id') == before_messages
    assert c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (work['id'],)) == before_work
    assert c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (work['goal_id'],)) == before_goal
