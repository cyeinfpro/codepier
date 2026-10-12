"""Conversation-first hierarchy against an isolated Hub and real browsers.

The dot is a deterministic protocol fixture, not a native-model execution proof.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_collaboration_dot_relay_flow import no_work, prepare, send
from tests.test_collaboration_dots_flow import call, mcp

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]
SCREENSHOTS = Path('.work/collaboration-redesign/screenshots')


def capture(page, name):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / name), animations='disabled', full_page=True)


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('chromium', 768), ('webkit', 390)])
def test_three_destinations_secondary_monitor_and_preserved_conversation(
    collaboration_stack, chat_browser_pool, engine, width
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        joined = prepare(stack, page)
        nav = page.locator('.cc-workspace-nav')
        expect(nav.locator('button')).to_have_count(3)
        expect(nav).to_contain_text('对话')
        expect(nav).to_contain_text('任务与结果')
        expect(nav).to_contain_text('连接')
        expect(nav.locator('[data-cc-view="monitor"]')).to_have_count(0)
        expect(page.locator('.cc-workspace-pulse')).not_to_be_visible()
        nav.locator('[data-cc-view="jobs"]').click()
        expect(page.locator('.cc-message-receipts')).not_to_have_attribute('open', '')
        page.locator('[data-cc-workspace-view="discussion"]').first.click()

        send(page, '请先讨论协作中心的方案，不要执行。')
        incoming = call(stack, joined['inbox_request'])['items'][0]
        expect(page.locator('.cc-workspace-pulse')).to_be_visible(timeout=15000)
        page.locator('[data-cc-progress-open]').click()
        expect(page.locator('.cc-message-receipts')).to_have_attribute('open', '')
        expect(page.locator('.cc-progress-card')).to_be_visible()
        expect(page.locator('.cc-message-receipts > summary')).to_be_focused()
        page.locator('[data-cc-workspace-view="discussion"]').first.click()

        reply = mcp(stack, 'collaboration', {
            'action': 'dot_message', **incoming['reply_arguments'],
            'body_text': '方案讨论，没有执行操作。\n\n' + '\n\n'.join(
                f'{i}. 对话是主要入口，任务结果可以返回原消息，连接只设置一次。'
                for i in range(1, 19)
            ),
        })
        expect(page.locator(f'[data-message-id="{reply["message"]["id"]}"]')).to_contain_text(
            '没有执行操作', timeout=15000
        )
        expect(page.locator('.cc-workspace-pulse')).not_to_be_visible(timeout=15000)
        area = page.locator('#cc-message-input')
        area.fill('保留的中文草稿和选区')
        area.evaluate('(n) => { n.setSelectionRange(2, 7); n.dataset.identity = "same"; }')
        original = area.element_handle()
        feed = page.locator('.cc-feed')
        original_feed = feed.element_handle()
        feed.evaluate('(n) => { n.scrollTop = 80; }')
        scroll_before = feed.evaluate('(n) => n.scrollTop')
        assert scroll_before > 0

        nav.locator('[data-cc-view="jobs"]').click()
        receipts = page.locator('.cc-message-receipts')
        receipts.locator('summary').click()
        expect(receipts).not_to_have_attribute('open', '')
        nav.locator('[data-cc-view="agents"]').click()
        nav.locator('[data-cc-view="jobs"]').click()
        expect(receipts).not_to_have_attribute('open', '')
        assert page.locator('.cc-workspace-records').bounding_box()['y'] < receipts.bounding_box()['y']
        expect(page.locator('.cc-task-toolbar [data-cc-view="monitor"]')).to_be_visible()
        capture(page, f'{engine}-{width}-tasks.png')

        page.locator('.cc-task-toolbar [data-cc-view="monitor"]').click()
        expect(page.locator('#cc-panel-title')).to_have_text('监控与异常')
        expect(nav.locator('[data-cc-view="jobs"]')).to_have_attribute('aria-pressed', 'true')
        page.locator('summary', has_text='登记固定只读探针').click()
        probe = page.locator('#cc-probe [name="label"]')
        probe.fill('不提交的探针草稿')
        page.locator('summary', has_text='新建或修订监控草稿').click()
        plan = page.locator('#cc-plan [name="plan_json"]')
        plan.fill('未提交配置，返回后保留。')
        original_probe = probe.element_handle()
        scroller = page.locator('.cc-panel-scroll')
        scroller.evaluate('(n) => { n.scrollTop = 140; }')
        monitor_scroll = scroller.evaluate('(n) => n.scrollTop')
        assert monitor_scroll > 0
        page.locator('.cc-panel-back').click()
        expect(receipts).not_to_have_attribute('open', '')
        page.locator('.cc-task-toolbar [data-cc-view="monitor"]').click()
        expect(probe).to_have_value('不提交的探针草稿')
        expect(plan).to_have_value('未提交配置，返回后保留。')
        assert original_probe.evaluate('(n) => n.isConnected')
        assert abs(scroller.evaluate('(n) => n.scrollTop') - monitor_scroll) <= 2
        capture(page, f'{engine}-{width}-monitor.png')

        page.keyboard.press('Escape')
        expect(page.locator('#cc-workspace-panel')).not_to_be_visible()
        expect(area).to_be_focused()
        expect(area).to_have_value('保留的中文草稿和选区')
        assert area.evaluate('(n) => [n.selectionStart, n.selectionEnd, n.dataset.identity]') == [2, 7, 'same']
        assert original.evaluate('(n) => n.isConnected')
        assert original_feed.evaluate('(n) => n.isConnected')
        assert abs(feed.evaluate('(n) => n.scrollTop') - scroll_before) <= 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        capture(page, f'{engine}-{width}-conversation.png')
        for narrow in [320, 390, 768, 1024, 1440]:
            page.set_viewport_size({'width': narrow, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            button = page.locator('#cc-command button[type="submit"]').bounding_box()
            assert button and button['y'] + button['height'] <= 900, (narrow, button)
        no_work(stack)
        assert not errors, errors
    finally:
        context.close()
