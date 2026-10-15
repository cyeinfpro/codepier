"""Explicit proposals and multi-round clarification must fence the original work."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab
from tests.test_collaboration_dot_chat import duplex, inbox, owner_message, send_dot
from tests.test_collaboration_dots import call
from tests.test_owner_task_context import ask, item
from tests.test_owner_task_context_boundaries import target


def rewrite(c, mid, text):
    row = c.s.object('collaboration_messages', c.room, mid)
    body = json.loads(row['body'])
    body['body_text'] = text
    c.s.store.execute('UPDATE collaboration_messages SET body=?,version=version+1 WHERE id=?',
                      (json.dumps(body), mid))


@pytest.mark.parametrize('paused', [False, True])
def test_existing_consumer_reads_current_protocol_without_rejoining_or_expanding_authority(collab, paused):
    c = duplex(collab)
    dot = c.s.dots.find(c.slot['id'])
    if paused:
        c.s.store.execute("UPDATE delegation_policies SET state='paused' WHERE id=?", (dot['policy_id'],))
    tables = ('grants', 'collaboration_dots', 'delegation_policies', 'collaboration_join_slots')
    before = {table: c.s.store.all('SELECT * FROM ' + table) for table in tables}
    page = inbox(c)
    protocol = page['task_context_protocol']
    assert protocol['question_field'] == 'response_request'
    assert protocol['requires_rejoin'] is False
    assert protocol['preserves_checkpoint'] is True
    assert protocol['grants_authority'] is False
    assert protocol['host_approval_evaluated'] is False
    assert 'response_request_options' in protocol['instructions']
    assert len(json.dumps(protocol, ensure_ascii=False).encode()) < 2400
    assert protocol == c.s.dot_chat.configuration(c.room, dot, c.actor)['task_context_protocol']
    if paused:
        assert page['work_inbox_request'] is None
        assert page['task_blocked_reason']
    assert before == {table: c.s.store.all('SELECT * FROM ' + table) for table in tables}
    assert not c.s.store.all('SELECT * FROM dot_task_questions')
    assert not c.s.store.all('SELECT * FROM coordination_work')


def test_historical_context_read_route_keeps_its_original_project_permission(collab):
    from tests.test_conversation_project_revision_history import context, cross_reply, revoke_source, SECRET

    c, _, historical = context(collab)
    source = cross_reply(c, historical)
    current = item(c, source['id'])
    ref = next(row for row in current['execution_context']['references'] if row['message_id'] == historical['id'])
    assert ref['read_request']['arguments']['project'] == 'otherproj'
    assert ref['is_execution_authority'] is False
    assert call(c, ref['read_request'])['message']['id'] == historical['id']
    revoke_source(c)
    with pytest.raises(DevError) as error:
        call(c, ref['read_request'])
    assert SECRET not in str(error.value)
    assert not c.s.store.all('SELECT * FROM operations')


def test_superseded_parent_cannot_leave_a_live_child_confirmation(collab):
    c = duplex(collab)
    source = owner_message(c, '讨论原测试方案。')[0]['message']
    call(c, item(c, source['id'])['task_request'])
    ask(c, source['id'])
    first_reply = owner_message(c, '可以')[0]['message']
    child = ask(c, first_reply['id'], '在原方案上只补充一次读取，这样继续吗？')
    ask(c, source['id'], '原方案替换为另一份待讨论方案。按新方案继续吗？')
    answer = owner_message(c, '确认 ' + child['task_continuation']['code'])[0]['message']
    current = item(c, answer['id'])
    assert current['task_continuation']['resolution'] == 'stale'
    assert current['task_request'] is None
    assert not c.s.store.all('SELECT * FROM operations')


def test_unrelated_pending_question_never_blocks_another_original_goal(collab):
    c = duplex(collab)
    source = owner_message(c, '只读取第一个测试文件。')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    other = owner_message(c, '另外讨论第二个测试文件。')[0]['message']
    ask(c, other['id'])
    target(c, original)
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert not c.s.store.all('SELECT * FROM operations')


def test_complete_sixteen_round_chain_is_bounded_without_dropping_evidence(collab):
    c = duplex(collab)
    source = owner_message(c, '多轮核对独立测试文件范围。')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    current = source
    required = {source['id']}
    for n in range(16):
        c.clock[0] += 2
        question = ask(c, current['id'], f'第 {n + 1} 项补充仅限测试文件，是否继续？')
        current = owner_message(c, '可以')[0]['message']
        required.update([question['message']['id'], current['id']])
    with pytest.raises(DevError) as error:
        ask(c, current['id'], '不能省略前面的限制再附加第十七轮。')
    assert error.value.code == 'TASK_CONTEXT_LIMIT'
    latest = item(c, current['id'])
    assert required <= {ref['message_id'] for ref in latest['execution_context']['references']}
    assert len(latest['execution_context']['references']) <= 40
    assert call(c, latest['task_request'])['delegation_id'] == original['delegation_id']
    assert len(c.s.store.all('SELECT * FROM dot_task_questions')) == 16
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert not c.s.store.all('SELECT * FROM operations')


def test_explicit_old_proposal_is_not_lost_outside_ambient_preview(collab):
    c = duplex(collab)
    earlier = owner_message(c, '先说明如何只验证独立测试文件。')[0]['message']
    proposal = send_dot(c, '仅处理 proof.txt，不修改业务文件。', earlier['id'], complete=False)
    for n in range(6):
        other = owner_message(c, f'旁支讨论 {n}，只聊天。')[0]['message']
        send_dot(c, f'旁支回复 {n}。', other['id'])
    source = owner_message(c, '按这份方案执行。', reply_to_id=proposal['message']['id'])[0]['message']
    current = item(c, source['id'])
    refs = {r['message_id']: r for r in current['execution_context']['references']}
    assert proposal['message']['id'] in refs
    assert refs[proposal['message']['id']]['role'] == 'assistant_context'
    assert refs[proposal['message']['id']]['is_execution_authority'] is False
    task = call(c, current['task_request'])
    rewrite(c, proposal['message']['id'], '方案内容发生了变化。')
    with pytest.raises(DevError) as error:
        target(c, task)
    assert error.value.code == 'TASK_CONTEXT_CHANGED'
    assert not c.s.store.all('SELECT * FROM operations')


@pytest.mark.parametrize('state', ['pending', 'declined', 'changed'])
def test_nested_clarification_fences_the_original_goal(collab, state):
    c = duplex(collab)
    source = owner_message(c, '验证独立测试文件，先明确范围。')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    ask(c, source['id'])
    first_reply = owner_message(c, '可以')[0]['message']
    second_question = ask(c, first_reply['id'], '只读回 proof.txt，不运行其他命令，这样继续吗？')
    if state != 'pending':
        second_reply = owner_message(c, '取消' if state == 'declined' else '可以')[0]['message']
        if state == 'changed':
            rewrite(c, second_reply['id'], '不再同意，先不要执行。')
    expected = {'pending': 'TASK_INPUT_REQUIRED', 'declined': 'TASK_RESPONSE_DECLINED',
                'changed': 'TASK_RESPONSE_STALE'}[state]
    with pytest.raises(DevError) as error:
        target(c, original)
    assert error.value.code == expected
    assert c.s.store.one('SELECT attempt FROM coordination_work')['attempt'] == 0
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert second_question['message']['id']
    assert not c.s.store.all('SELECT * FROM operations')


def test_nested_confirmation_keeps_every_exact_input_and_one_original_work(collab):
    c = duplex(collab)
    source = owner_message(c, '验证独立测试文件，先明确范围。')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    first_question = ask(c, source['id'])
    first_reply = owner_message(c, '可以')[0]['message']
    second_question = ask(c, first_reply['id'], '补充限制：只读回 proof.txt，不运行其他命令。这样继续吗？')
    second_reply = owner_message(c, '可以')[0]['message']
    resumed = call(c, item(c, second_reply['id'])['task_request'])
    assert resumed['delegation_id'] == original['delegation_id']
    context = call(c, original['read_request'])['execution_context']
    refs = {r['message_id'] for r in context['references']}
    assert {source['id'], first_question['message']['id'], first_reply['id'],
            second_question['message']['id'], second_reply['id']} <= refs
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1
    rewrite(c, second_question['message']['id'], '第二轮方案已改变。')
    with pytest.raises(DevError):
        target(c, original)
    assert not c.s.store.all('SELECT * FROM operations')
