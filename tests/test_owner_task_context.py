"""Original-channel context is evidence, never a replacement for host approval."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, send_dot, inbox, no_work
from tests.test_collaboration_dots import call
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet
from tests.test_dot_weixin_owner_authority import approve


def item(c, mid):
    return next(x for x in inbox(c)['items'] if x['message_id'] == mid)


def ask(c, mid, text='只在临时测试文件中验证，不修改业务代码。按这个范围继续吗？'):
    pending = next((x for x in inbox(c)['items'] if x['message_id'] == mid), None)
    if pending:
        request = pending['response_request_options']
    else:
        link = c.s.store.one('SELECT * FROM delegation_requests WHERE message_id=?', (mid,))
        request = call(c, c.s.delegation.read_request(c.room, link['id']))['response_request_options']
    return send_dot(c, text, mid, complete=False,
                    response_request={**request, 'reason': 'plan_confirmation'})


def test_authenticated_request_has_attributed_context_without_new_confirmation(collab):
    c = duplex(collab)
    first = owner_message(c, '看看项目有哪些权限，不修改文件。')[0]['message']
    send_dot(c, '当前项目支持读取、写入和执行。', first['id'])
    source = owner_message(c, '用独立测试文件验证读写，执行一次只读命令。')[0]['message']
    current = item(c, source['id'])
    assert current['execution_context']['source_message_id'] == source['id']
    assert first['id'] in [r['message_id'] for r in current['execution_context']['references']]
    assert current['execution_route']['desktop_access_required'] is False
    assert current['execution_route']['host_approval']['state'] == 'not_evaluated'
    assert current['task_request']
    no_work(c)
    task = call(c, current['task_request'])
    detail = call(c, task['read_request'])
    assert detail['execution_context']['references']
    assert detail['execution_context']['grants_authority'] is False


def test_panel_confirmation_is_linked_and_resumes_exact_existing_task(collab):
    c = duplex(collab)
    source = owner_message(c, '测试读写执行。')[0]['message']
    task = call(c, item(c, source['id'])['task_request'])
    question = ask(c, source['id'])
    c.clock[0] += 1
    response = owner_message(c, '可以')[0]['message']
    assert response['thread_root_id'] == source['id']
    assert response['reply_to_id'] == question['message']['id']
    current = item(c, response['id'])
    assert current['task_continuation']['resolution'] == 'confirmed'
    assert current['task_continuation']['host_approval_satisfied'] is False
    resumed = call(c, current['task_request'])
    assert resumed['delegation_id'] == task['delegation_id']
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert not c.s.store.all('SELECT * FROM operations')
    assert call(c, current['task_request'])['delegation_id'] == task['delegation_id']
    context = call(c, task['read_request'])['execution_context']
    assert response['id'] in [r['message_id'] for r in context['references']]


def test_weixin_fresh_owner_yes_stays_in_original_thread(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = ingest(c, w, account, packet(101, '验证项目文件读写。',
        create_time_ms=int(c.clock[0] * 1000) + 1))['message_ids'][0]
    question = ask(c, source)
    c.clock[0] += 2
    response = ingest(c, w, account, packet(102, '可以',
        create_time_ms=int(c.clock[0] * 1000)))['message_ids'][0]
    current = item(c, response)
    assert current['trusted_author']['kind'] == 'verified_external_owner'
    assert current['message']['thread_root_id'] == source
    assert current['message']['reply_to_id'] == question['message']['id']
    context_ids = [r['message_id'] for r in current['execution_context']['references']]
    assert source in context_ids and question['message']['id'] in context_ids
    original_request = item(c, source)['task_request']
    task = call(c, current['task_request'])
    assert call(c, original_request)['delegation_id'] == task['delegation_id']
    detail = call(c, task['read_request'])
    assert detail['delegation']['message_id'] == source
    assert '验证项目文件读写' in detail['goal']['objective']
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1
    assert not inbox(c)['items']


def test_two_pending_plans_do_not_guess_what_yes_approves(collab):
    c = duplex(collab)
    a = owner_message(c, '测试方案一。')[0]['message']
    b = owner_message(c, '测试方案二。')[0]['message']
    qa, qb = ask(c, a['id']), ask(c, b['id'])
    c.clock[0] += 1
    response = owner_message(c, '可以')[0]['message']
    current = item(c, response['id'])
    assert current['task_request'] is None
    assert current['task_blocked_reason'] == 'TASK_RESPONSE_AMBIGUOUS'
    assert current['task_continuation']['candidate_count'] == 2
    no_work(c)
    response2 = owner_message(c, '确认 ' + qa['task_continuation']['code'])[0]['message']
    assert response2['reply_to_id'] == qa['message']['id']
    assert response2['reply_to_id'] != qb['message']['id']


@pytest.mark.parametrize('reply', ['不可以', '取消', '先不要做'])
def test_decline_is_not_an_execution_request(collab, reply):
    c = duplex(collab)
    source = owner_message(c, '先讨论验证范围。')[0]['message']
    ask(c, source['id'])
    c.clock[0] += 1
    response = owner_message(c, reply)[0]['message']
    current = item(c, response['id'])
    assert current['task_request'] is None
    assert current['task_blocked_reason'] == 'TASK_RESPONSE_DECLINED'
    no_work(c)


@pytest.mark.parametrize('reply', ['可以删除所有文件吗？', '他说“可以”', '确认 AAAAAAAA'])
def test_question_quote_or_wrong_code_never_confirms_pending_plan(collab, reply):
    c = duplex(collab)
    source = owner_message(c, '先讨论。')[0]['message']
    question = ask(c, source['id'])
    c.clock[0] += 1
    response = owner_message(c, reply)[0]['message']
    assert response['reply_to_id'] != question['message']['id']
    pending = c.s.store.one('SELECT state FROM dot_task_questions WHERE message_id=?', (question['message']['id'],))
    assert pending['state'] == 'pending'
    no_work(c)


def test_plan_edit_invalidates_old_confirmation_without_host_approval_claim(collab):
    c = duplex(collab)
    source = owner_message(c, '验证文件。')[0]['message']
    question = ask(c, source['id'])
    c.s.invoke('collaboration', {'action': 'dot_update', **question['update_arguments'],
        'body_text': '方案已改变，请重新明确范围。', 'complete': False}, c.actor)
    c.clock[0] += 1
    response = owner_message(c, '确认 ' + question['task_continuation']['code'])[0]['message']
    current = item(c, response['id'])
    assert current['task_request'] is None
    assert current['task_blocked_reason'] == 'TASK_RESPONSE_STALE'
    no_work(c)


def test_legacy_interim_and_reads_do_not_create_questions(collab):
    c = duplex(collab)
    source = owner_message(c, '请检查 README。')[0]['message']
    send_dot(c, '收到，先检查。', source['id'], complete=False)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    for _ in range(2):
        assert item(c, source['id'])['task_request']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert not c.s.store.all('SELECT * FROM dot_task_questions')
    no_work(c)


def test_original_channel_guide_is_in_public_bundle_without_private_evidence():
    from pathlib import Path
    from scripts.build_source_bundle import PUBLIC_DOCS, include
    guide = Path('docs/OWNER_TASK_CONTEXT.md')
    assert guide.as_posix() in PUBLIC_DOCS
    assert include(guide, public=True)
    assert not include(Path('.work/owner-task-context/environment.json'), public=True)
    assert not include(Path('.work/owner-task-context/screenshots/question-webkit-390.png'), public=True)
