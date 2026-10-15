"""Source/version/channel isolation for original-channel task clarification."""
import json
from dataclasses import replace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, send_dot, inbox, no_work
from tests.test_collaboration_dots import call
from tests.test_owner_task_context import ask, item
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet
from tests.test_dot_weixin_owner_authority import approve
from tests.test_dot_telegram_channel import setup as telegram_setup, update, ingest as telegram_ingest


def target(c, task):
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (task['delegation_id'],))
    goal = c.s.coordination.object(c.room, link['goal_id'])
    return c.s.delegation.target(c.actor, c.room, goal, 'read', {'path': 'README.md'})


@pytest.mark.parametrize('decline', [False, True])
def test_pending_or_declined_input_does_not_consume_a_work_attempt(collab, decline):
    c = duplex(collab)
    source = owner_message(c, '先验证测试文件。')[0]['message']
    task = call(c, item(c, source['id'])['task_request'])
    before = call(c, c.connected['work_inbox_request'])['items'][0]
    assert before['category'] == 'claimable'
    ask(c, source['id'])
    if decline:
        owner_message(c, '取消')
    expected = 'TASK_RESPONSE_DECLINED' if decline else 'TASK_INPUT_REQUIRED'
    current = call(c, c.connected['work_inbox_request'])['items'][0]
    assert current['category'] == 'blocked' and current['reason_code'] == expected
    assert current['claim_request'] is None
    with pytest.raises(DevError) as err:
        call(c, before['claim_request'])
    assert err.value.code == expected
    assert c.s.store.one('SELECT attempt FROM coordination_work')['attempt'] == 0
    assert call(c, task['read_request'])['delegation']['message_id'] == source['id']
    assert not c.s.store.all('SELECT * FROM operations')


def test_pending_question_stops_new_steps_but_can_still_read_original_work(collab):
    c = duplex(collab)
    source = owner_message(c, '验证测试文件。')[0]['message']
    task = call(c, item(c, source['id'])['task_request'])
    ask(c, source['id'])
    assert call(c, task['read_request'])['delegation']['message_id'] == source['id']
    with pytest.raises(DevError) as err:
        target(c, task)
    assert err.value.code == 'TASK_INPUT_REQUIRED'
    assert not c.s.store.all('SELECT * FROM operations')


def test_repeat_yes_does_not_create_a_second_task_and_cancel_stops_new_steps(collab):
    c = duplex(collab)
    source = owner_message(c, '验证测试文件。')[0]['message']
    question = ask(c, source['id'])
    response = owner_message(c, '可以')[0]['message']
    task = call(c, item(c, response['id'])['task_request'])
    again = owner_message(c, '可以')[0]['message']
    assert item(c, again['id'])['task_blocked_reason'] == 'TASK_RESPONSE_ALREADY_ANSWERED'
    assert item(c, again['id'])['task_request'] is None
    cancel = owner_message(c, '取消 ' + question['task_continuation']['code'])[0]['message']
    assert item(c, cancel['id'])['task_blocked_reason'] == 'TASK_RESPONSE_DECLINED'
    with pytest.raises(DevError) as err:
        target(c, task)
    assert err.value.code == 'TASK_RESPONSE_DECLINED'
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert not c.s.store.all('SELECT * FROM operations')
    assert call(c, task['read_request'])['delegation']['message_id'] == source['id']


@pytest.mark.parametrize('change', ['source', 'response', 'scope'])
def test_changed_evidence_cannot_be_converted_through_original_request(collab, change):
    c = duplex(collab)
    source = owner_message(c, '只验证测试文件。')[0]['message']
    original = item(c, source['id'])['task_request']
    ask(c, source['id'])
    response = owner_message(c, '可以')[0]['message']
    if change in {'source', 'response'}:
        mid = source['id'] if change == 'source' else response['id']
        row = c.s.object('collaboration_messages', c.room, mid)
        body = json.loads(row['body'])
        body['body_text'] = '先不要执行。'
        c.s.store.execute('UPDATE collaboration_messages SET body=?,version=version+1 WHERE id=?', (json.dumps(body), mid))
    else:
        c.s.store.execute("UPDATE projects SET root='/tmp/changed-fixture-root' WHERE id='proj'")
    with pytest.raises(DevError):
        call(c, original)
    no_work(c)


@pytest.mark.parametrize('change', ['expiry', 'source', 'scope'])
def test_old_question_code_never_approves_changed_scope(collab, change):
    c = duplex(collab)
    source = owner_message(c, '先确认验证范围。')[0]['message']
    question = ask(c, source['id'])
    if change == 'expiry':
        c.clock[0] += 1801
    elif change == 'source':
        c.s.store.execute('UPDATE collaboration_messages SET version=version+1 WHERE id=?', (source['id'],))
    else:
        c.s.store.execute("UPDATE projects SET root='/tmp/changed-fixture-root' WHERE id='proj'")
    response = owner_message(c, '确认 ' + question['task_continuation']['code'])[0]['message']
    current = item(c, response['id'])
    assert current['task_request'] is None
    assert current['task_continuation']['resolution'] == 'stale'
    no_work(c)


def test_question_replay_is_one_record_and_does_not_change_permissions(collab):
    c = duplex(collab)
    source = owner_message(c, '讨论测试范围。')[0]['message']
    option = item(c, source['id'])['response_request_options']
    raw = {'action': 'dot_message', **c.scope, 'dot_id': c.slot['id'], 'body_text': '按测试文件范围继续吗？',
           'reply_to_id': source['id'], 'complete': False, 'idempotency_key': key(),
           'response_request': {**option, 'reason': 'plan_confirmation'}}
    grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    tokens = c.s.store.all('SELECT * FROM tokens ORDER BY id')
    first = c.s.invoke('collaboration', raw, c.actor)
    again = c.s.invoke('collaboration', raw, c.actor)
    assert first['message']['id'] == again['message']['id']
    assert len(c.s.store.all('SELECT * FROM dot_task_questions')) == 1
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == grants
    assert c.s.store.all('SELECT * FROM tokens ORDER BY id') == tokens
    assert first['task_continuation']['host_approval_satisfied'] is False
    altered = {**raw, 'response_request': {**raw['response_request'], 'reason': 'clarification'}}
    with pytest.raises(DevError):
        c.s.invoke('collaboration', altered, c.actor)
    no_work(c)


@pytest.mark.parametrize('change', ['host_reason', 'source_digest', 'scope_digest', 'final', 'other_connector'])
def test_question_contract_cannot_create_host_approval_or_impersonate_source(collab, change):
    c = duplex(collab)
    source = owner_message(c, '讨论范围。')[0]['message']
    option = {**item(c, source['id'])['response_request_options'], 'reason': 'plan_confirmation'}
    raw = {'action': 'dot_message', **c.scope, 'dot_id': c.slot['id'], 'body_text': '方案',
           'reply_to_id': source['id'], 'complete': False, 'idempotency_key': key(), 'response_request': option}
    actor = c.actor
    if change == 'host_reason':
        option['reason'] = 'host_approval'
    elif change in {'source_digest', 'scope_digest'}:
        option[change] = '0' * 64
    elif change == 'final':
        raw['complete'] = True
    else:
        actor = replace(c.actor, grant_id='second', actor='mcp:second:fixture')
    with pytest.raises(DevError):
        c.s.invoke('collaboration', raw, actor)
    assert not c.s.store.all('SELECT * FROM dot_task_questions')
    no_work(c)


def test_approved_telegram_owner_can_reply_without_leaving_private_chat(collab):
    c, t, account, binding, scope = telegram_setup(duplex(collab))
    current = t.binding(binding['binding_id'], c.owner, c.room)
    option = c.s.channel_owner.options(current, channel='telegram')
    c.s.channel_owner.control({**scope, 'binding_id': current['id'], 'expected_binding_version': current['version'],
        'action': 'approve', 'confirm_peer_id': current['user_id'], 'confirm_owner_tasks': True,
        'confirm_until_revoked': True, 'expected_scope_digest': option['scope_digest'], 'idempotency_key': key()},
        c.owner, channel='telegram')
    c.clock[0] += 2
    source = telegram_ingest(c, t, account, [update(2, '只验证测试文件。', date=int(c.clock[0]))])['message_ids'][0]
    question = ask(c, source)
    c.clock[0] += 2
    response = telegram_ingest(c, t, account, [update(3, '可以', date=int(c.clock[0]))])['message_ids'][0]
    current = item(c, response)
    assert current['message']['reply_to_id'] == question['message']['id']
    assert current['task_continuation']['resolution'] == 'confirmed'
    assert current['trusted_author']['channel'] == 'telegram'
    task = call(c, current['task_request'])
    assert call(c, task['read_request'])['delegation']['message_id'] == source
    assert not inbox(c)['items']


def test_unapproved_weixin_contact_cannot_answer_panel_question(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    source = owner_message(c, '先明确范围。')[0]['message']
    question = ask(c, source['id'])
    response = ingest(c, w, account, packet(301, '确认 ' + question['task_continuation']['code'],
        create_time_ms=int(c.clock[0] * 1000) + 1))['message_ids'][0]
    current = item(c, response)
    assert current['trusted_author']['authenticated'] is False
    assert current['task_request'] is None and current['task_continuation'] is None
    assert c.s.store.one('SELECT state FROM dot_task_questions')['state'] == 'pending'
    no_work(c)


def test_explicit_verified_cross_channel_reply_binds_exact_panel_question(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = owner_message(c, '明确测试文件范围。')[0]['message']
    question = ask(c, source['id'])
    response = ingest(c, w, account, packet(302, '确认 ' + question['task_continuation']['code'],
        create_time_ms=int(c.clock[0] * 1000) + 1))['message_ids'][0]
    current = item(c, response)
    assert current['task_continuation']['resolution'] == 'confirmed'
    assert current['message']['thread_root_id'] == source['id']
    assert current['task_continuation']['host_approval_satisfied'] is False
    no_work(c)


@pytest.mark.parametrize('adopted', [False, True])
def test_only_an_adopted_proposal_not_ambient_progress_fences_execution(collab, adopted):
    c = duplex(collab)
    earlier = owner_message(c, '先说明如何验证文件。')[0]['message']
    phase = send_dot(c, '使用独立测试文件，不修改业务代码。', earlier['id'], complete=False)
    source = owner_message(c, '按这个方案做。' if adopted else '另建一个独立测试文件验证读取。',
        **({'reply_to_id': phase['message']['id']} if adopted else {}))[0]['message']
    task = call(c, item(c, source['id'])['task_request'])
    c.s.invoke('collaboration', {'action': 'dot_update', **phase['update_arguments'],
        'body_text': '阶段说明已更新。', 'complete': False}, c.actor)
    if adopted:
        with pytest.raises(DevError) as err:
            target(c, task)
        assert err.value.code == 'TASK_CONTEXT_CHANGED'
    else:
        target(c, task)
    assert not c.s.store.all('SELECT * FROM operations')


def test_context_budget_preserves_read_routes_instead_of_truncating_text(collab):
    c = duplex(collab)
    for i in range(8):
        owner_message(c, f'{i}：' + '测试上下文。' * 500)
    page = inbox(c)
    contexts = [i['execution_context'] for i in page['items']]
    entries = [e for bundle in contexts for e in bundle['references']]
    assert sum(len(e.get('body_text', '').encode('utf-8')) for e in entries) <= 24000
    missing = [e for e in entries if not e['included']]
    assert missing
    for e in missing[:2]:
        assert 'body_text' not in e and e['read_request']
        assert call(c, e['read_request'])
    assert all(not bundle['history_complete'] for bundle in contexts)
    no_work(c)


def test_new_freeform_request_after_answer_is_not_folded_into_old_work(collab):
    c = duplex(collab)
    source = owner_message(c, '验证第一个测试文件。')[0]['message']
    question = ask(c, source['id'])
    response = owner_message(c, '可以')[0]['message']
    first = call(c, item(c, response['id'])['task_request'])
    followup = owner_message(c, '另外检查第二个独立测试文件。', reply_to_id=question['message']['id'])[0]['message']
    current = item(c, followup['id'])
    assert current['task_continuation'] is None
    second = call(c, current['task_request'])
    assert second['delegation_id'] != first['delegation_id']
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 2
    assert not c.s.store.all('SELECT * FROM operations')


def test_polite_reply_cannot_undo_a_cancelled_plan(collab):
    c = duplex(collab)
    source = owner_message(c, '验证测试文件。')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    question = ask(c, source['id'])
    owner_message(c, '取消')
    thanks = owner_message(c, '谢谢，先这样。', reply_to_id=question['message']['id'])[0]['message']
    assert item(c, thanks['id'])['task_continuation'] is None
    with pytest.raises(DevError) as err:
        target(c, original)
    assert err.value.code == 'TASK_RESPONSE_DECLINED'
    assert not c.s.store.all('SELECT * FROM operations')


def test_bare_yes_after_intervening_topic_does_not_approve_old_question(collab):
    c = duplex(collab)
    source = owner_message(c, '讨论原测试方案。')[0]['message']
    ask(c, source['id'])
    owner_message(c, '先聊一下另一个项目的安排，不执行。')
    response = owner_message(c, '可以')[0]['message']
    current = item(c, response['id'])
    assert current['task_blocked_reason'] == 'TASK_RESPONSE_AMBIGUOUS'
    assert current['task_request'] is None
    assert c.s.store.one('SELECT state FROM dot_task_questions')['state'] == 'pending'
    no_work(c)


def test_delayed_weixin_ack_created_before_question_cannot_approve_it(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = ingest(c, w, account, packet(401, '先讨论验证范围。',
        create_time_ms=int(c.clock[0] * 1000) + 1))['message_ids'][0]
    before_question = int(c.clock[0] * 1000) + 2
    c.clock[0] += 10
    ask(c, source)
    response = ingest(c, w, account, packet(402, '可以', create_time_ms=before_question))['message_ids'][0]
    current = item(c, response)
    assert current['task_request'] is None
    assert current['task_blocked_reason'] == 'TASK_RESPONSE_STALE'
    no_work(c)
