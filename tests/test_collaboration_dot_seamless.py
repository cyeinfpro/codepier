"""Conversation/work interleaving without a panel-selected mode or new authority."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab
from tests.test_collaboration_dot_chat import duplex, owner_message, inbox, no_work
from tests.test_collaboration_dots import call


def test_interim_reply_then_work_does_not_require_repeating_owner_message(collab):
    c = duplex(collab)
    original, _ = owner_message(c, '先说说你准备怎么检查，然后帮我读 README。')
    item = inbox(c)['items'][0]
    interim = {'action': 'dot_message', **item['interim_reply_arguments'], 'body_text': '我先读取说明，再核对示例。'}
    first = c.s.invoke('collaboration', interim, c.actor)
    assert first['complete'] is False and not first['scheduled']
    assert c.s.invoke('collaboration', interim, c.actor)['message']['id'] == first['message']['id']
    no_work(c)
    pending = inbox(c)['items'][0]
    assert pending['message_id'] == original['message']['id'] and pending['state'] == 'read'
    task = call(c, pending['task_request'])
    assert call(c, pending['task_request'])['delegation_id'] == task['delegation_id']
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1
    assert c.s.store.one('SELECT message_id FROM delegation_requests')['message_id'] == original['message']['id']
    # A chat message never becomes a forged execution receipt.
    c.s.invoke('collaboration', {'action': 'dot_message', **item['reply_arguments'], 'body_text': '现在已经接好原请求，正在继续处理。'}, c.actor)
    assert not c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='delegation_result'")
    assert c.s.store.one('SELECT state FROM coordination_work')['state'] == 'queued'


def test_interim_and_final_answer_without_work_are_idempotent_and_monotonic(collab):
    c = duplex(collab)
    owner_message(c, '请先解释思路，再给出建议，不改文件。')
    item = inbox(c)['items'][0]
    interim = {'action': 'dot_message', **item['interim_reply_arguments'], 'body_text': '可以先从操作路径讨论。'}
    final = {'action': 'dot_message', **item['reply_arguments'], 'body_text': '建议保留一个输入框，不区分对话模式。'}
    assert interim['idempotency_key'] != final['idempotency_key']
    c.s.invoke('collaboration', interim, c.actor)
    assert inbox(c)['items'][0]['state'] == 'read'
    answer = c.s.invoke('collaboration', final, c.actor)
    assert answer['complete'] is True
    assert c.s.invoke('collaboration', final, c.actor)['message']['id'] == answer['message']['id']
    # Delayed delivery/retry of the earlier interim reply cannot reopen completion.
    c.s.invoke('collaboration', interim, c.actor)
    assert not inbox(c)['items']
    receipts = c.s.dot_chat.receipts(item['message_id'])
    assert receipts[0]['state'] == 'handled' and receipts[0]['handled_at'] is not None
    no_work(c)


def test_same_reply_key_cannot_change_completion_semantics(collab):
    c = duplex(collab)
    owner_message(c)
    item = inbox(c)['items'][0]
    request = {'action': 'dot_message', **item['interim_reply_arguments'], 'body_text': '先讨论。'}
    c.s.invoke('collaboration', request, c.actor)
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {**request, 'complete': True}, c.actor)
    assert inbox(c)['items'][0]['state'] == 'read'
    assert len(c.s.store.all('SELECT * FROM collaboration_messages WHERE reply_to_id=?', (item['message_id'],))) == 1


def test_finished_discussion_cannot_be_reopened_as_unrequested_work(collab):
    c = duplex(collab)
    owner_message(c)
    item = inbox(c)['items'][0]
    c.s.invoke('collaboration', {'action': 'dot_message', **item['reply_arguments'], 'body_text': '这部分我们已经讨论完。'}, c.actor)
    with pytest.raises(DevError) as failure:
        call(c, item['task_request'])
    assert failure.value.code == 'DOT_MESSAGE_HANDLED'
    no_work(c)


@pytest.mark.parametrize('mode,allow_tasks,expected', [
    ('write', 1, ['read', 'write', 'execute']),
    ('read', 0, ['read']),
    ('write', 0, ['read', 'write']),
])
def test_onboarding_defaults_follow_current_project_without_changing_permissions(collab, mode, allow_tasks, expected):
    service, owner, _, _, room, *_ = collab
    service.store.execute('UPDATE projects SET mode=?,allow_tasks=? WHERE id=?', (mode, allow_tasks, room['project_id']))
    before = service.store.one('SELECT total_changes() AS n')['n']
    grants = service.store.all('SELECT * FROM grants ORDER BY id')
    result = service.dots.setup_defaults(owner, room)
    assert result['capabilities'] == expected
    assert result['project_id'] == room['project_id'] and result['changes_authority'] is False
    assert service.store.one('SELECT total_changes() AS n')['n'] == before
    assert service.store.all('SELECT * FROM grants ORDER BY id') == grants
    assert not service.store.all('SELECT * FROM collaboration_dots')


def test_consumer_uses_context_and_interim_reply_instead_of_panel_modes(collab):
    c = duplex(collab)
    config = c.connected['consumer_configuration']
    assert config['panel_mode_switch_required'] is False
    assert config['next_action_decided_by'] == 'dot_with_conversation_context'
    assert 'thread_request' in config['wake_instructions']
    assert 'interim_reply_arguments' in config['wake_instructions']
    assert '不按问号或关键词硬分模式' in config['wake_instructions']
    assert '需要新授权或宿主确认' in config['wake_instructions']
    assert json.loads(c.s.dots.find(c.slot['id'])['setup'])['capabilities'] == ['read', 'write', 'execute']
