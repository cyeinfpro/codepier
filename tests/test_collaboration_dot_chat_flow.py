"""Real Hub, MCP and panel duplex conversations; the external model is a fixture."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_delivery_flow import login, refresh as request_refresh, scope
from tests.test_collaboration_dots_flow import call, mcp, join_dot
from tests.test_collaboration_join_browser import fixture_host

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def refresh(page):
    request_refresh(page)
    expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('#cc-feedback')).to_have_text('状态已刷新。')


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_panel_chat_proactive_dot_thread_reply_restart_and_no_task(
        collaboration_stack, chat_browser_pool, engine, width):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    errors, sent = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: sent.append(request.post_data_json)
        if request.method == 'POST' and request.url.endswith('/api/collaboration/message') else None)
    try:
        login(page, stack)
        dot = stack.must(stack.client.post('/api/collaboration/dot', json={**scope(stack),
            'label': '沟通 dot', 'duplex': True, 'capabilities': ['read'], 'confirm_tasks': True,
            'idempotency_key': key()}))['dot']
        joined = join_dot(stack, dot)
        assert joined['consumer_configuration']['mode'] == 'bidirectional_chat'
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        refresh(page)
        area = page.locator('#cc-message-input')
        expect(area).to_be_enabled()
        area.fill('@沟通')
        expect(page.locator('#cc-dot-suggestions')).to_be_visible()
        area.press('Enter')
        expect(page.locator('#cc-send-mode')).to_contain_text('发给 @沟通 dot')
        area.fill('你好，我们先聊一下方案。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        expect(page.locator('#cc-send-mode')).to_contain_text('发给 @沟通 dot')
        first = call(stack, joined['inbox_request'])['items'][0]
        assert first['message']['body_text'] == '你好，我们先聊一下方案。'
        assert len(sent) == 1 and not sent[0].get('delegation') and not sent[0].get('dispatch_mode')
        call(stack, first['ack_request'])
        refresh(page)
        expect(page.locator('.cc-dot-receipt[data-relay-state="received"]')).to_have_text('已送达 dot')
        response = {'action': 'dot_message', **first['reply_arguments'], 'body_text': '你更重视速度，还是界面？'}
        replied = mcp(stack, 'collaboration', response)
        assert mcp(stack, 'collaboration', response)['message']['id'] == replied['message']['id']
        refresh(page)
        reply_card = page.locator('.cc-message').filter(has_text='你更重视速度，还是界面？')
        expect(reply_card.locator('.cc-message-meta strong')).to_have_text('沟通 dot')
        expect(reply_card).to_have_count(1)
        reply_card.locator('[data-cc-action="reply"]').click()
        area.fill('先讨论界面，暂时不要修改。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        second = call(stack, joined['inbox_request'])['items'][0]
        assert second['message']['thread_root_id'] == first['message_id']
        mcp(stack, 'collaboration', {'action': 'dot_message', **second['reply_arguments'],
            'body_text': '好的，先比较两种布局，不动代码。'})
        assert not call(stack, joined['inbox_request'])['items']
        # A proactive topic does not require any owner task or prior inbound message.
        proactive = mcp(stack, 'collaboration', {'action': 'dot_message', **scope(stack),
            'dot_id': dot['id'], 'body_text': '我还有一个问题：手机端需要优先吗？', 'idempotency_key': key()})
        assert proactive['proactive'] and not proactive['scheduled']
        page.locator('[data-cc-action="dot-clear"]').click()
        refresh(page)
        proactive_card = page.locator('.cc-message').filter(has_text='我还有一个问题：手机端需要优先吗？')
        expect(proactive_card.locator('.cc-message-meta strong')).to_have_text('沟通 dot')
        proactive_card.locator('[data-cc-action="reply"]').click()
        expect(page.locator('#cc-send-mode')).to_contain_text('发给 @沟通 dot')
        area.fill('是的，优先手机端。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        before_restart = call(stack, joined['inbox_request'])['items'][0]
        assert before_restart['message']['thread_root_id'] == proactive['message']['id']
        stack.hub.terminate()
        stack.hub.wait(timeout=12)
        stack.start_hub()
        restored = mcp(stack, 'collaboration_query', {'action': 'dot_connection', **scope(stack), 'dot_id': dot['id']})
        assert restored['inbox_request'] == joined['inbox_request']
        pending = call(stack, restored['inbox_request'])['items'][0]
        assert pending['message_id'] == before_restart['message_id']
        assert pending['reply_arguments'] == before_restart['reply_arguments']
        mcp(stack, 'collaboration', {'action': 'dot_message', **pending['reply_arguments'],
            'body_text': '收到，后续讨论以手机端为先。'})
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('收到，后续讨论以手机端为先。')
        assert not call(stack, restored['inbox_request'])['items']
        store = Store(stack.hubdir)
        try:
            assert store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == 0
            assert store.one('SELECT COUNT(*) AS n FROM delegation_requests')['n'] == 0
            assert store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE kind IN ('delegation_progress','delegation_result')")['n'] == 0
        finally:
            store.close()
        assert all(not record.get('dispatch_mode') and not record.get('delegation') for record in sent)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        screenshots = Path('.work/dot-refactor/screenshots')
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / f'duplex-chat-{engine}-{width}.png'), full_page=True)
        assert not errors, errors
    finally:
        context.close()
