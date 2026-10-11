"""Unified workspace contracts against an isolated Hub and real Chromium/WebKit.

Fixture dot replies are protocol consumers, not proof of a native model's autonomy.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_collaboration_delivery_flow import login
from tests.test_collaboration_dot_relay_flow import prepare, send, no_work
from tests.test_collaboration_dots_flow import call, mcp

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]
SCREENSHOTS = Path('.work/collaboration-ux/screenshots')


def capture(page, name):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / name), animations='disabled', full_page=True)


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_inspector_keeps_live_composer_selection_reply_and_source(collaboration_stack, chat_browser_pool, engine, width):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    errors, read_cursors = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: read_cursors.append(r.post_data_json)
            if r.method == 'POST' and r.url.endswith('/api/collaboration/read-cursor') else None)
    try:
        joined = prepare(stack, page)
        send(page, '请梳理协作中心的用户路径。先讨论方案，暂不修改代码。')
        incoming = call(stack, joined['inbox_request'])['items'][0]
        area = page.locator('#cc-message-input')
        area.fill('保留手机端的体验，也不要丢失原对话。')
        area.evaluate('(n) => { n.setSelectionRange(2, 7); n.dataset.identity = "original"; }')
        original = area.element_handle()
        feed = page.locator('.cc-feed').element_handle()
        page.locator('[data-cc-view="jobs"]').click()
        expect(page.locator('#cc-workspace-panel')).to_be_visible()
        expect(page.locator('.cc-progress-card')).to_have_count(1)
        # Keep a filter focused while the real remote receipt changes.
        page.locator('[data-cc-progress-filter="all"]').click()
        reply = mcp(stack, 'collaboration', {'action': 'dot_message', **incoming['reply_arguments'],
            'body_text': '## 建议的连续路径\n\n从房间开始，连接助手，然后持续对话。\n\n- 进展就地查看\n- 每个结果可回到原消息\n- 设置按需展开\n\n这里只提交方案，没有执行代码。'})
        expect(page.locator('.cc-progress-card')).to_contain_text('已回复', timeout=15000)
        expect(page.locator('[data-cc-progress-filter="all"]')).to_be_focused()
        assert original.evaluate('(n) => n.isConnected') and feed.evaluate('(n) => n.isConnected')
        expect(area).to_have_value('保留手机端的体验，也不要丢失原对话。')
        assert area.evaluate('(n) => [n.selectionStart, n.selectionEnd]') == [2, 7]
        if width > 1100:
            expect(area).to_be_visible()
        else:
            expect(page.locator('#cc-discussion-host')).to_have_attribute('inert', '')
            assert not any(row.get('last_seen_sequence', 0) >= reply['message']['server_sequence']
                           for row in read_cursors), read_cursors
        capture(page, f'{engine}-{width}-progress.png')
        page.locator('.cc-progress-card [data-cc-action="source"]').click()
        expect(page.locator('#cc-workspace-panel')).not_to_be_visible()
        expect(page.locator(f'.cc-feed [data-message-id="{incoming["message_id"]}"]')).to_be_focused()
        expect(page.locator(f'[data-message-id="{reply["message"]["id"]}"]')).to_contain_text('没有执行代码')
        assert original.evaluate('(n) => n.isConnected')
        area.focus()
        area.evaluate('(n) => n.setSelectionRange(2, 7)')
        page.locator('[data-cc-view="agents"]').click()
        form = page.locator('#cc-dot-create')
        expect(form).not_to_be_visible()
        page.locator('.cc-add-dot > summary').click()
        form.locator('[name="label"]').fill('保留中的新助手名称')
        page.locator('[data-cc-view="monitor"]').click()
        page.locator('summary', has_text='登记固定只读探针').click()
        probe = page.locator('#cc-probe [name="label"]')
        probe.fill('未提交的探针草稿')
        probe_node = probe.element_handle()
        page.locator('[data-cc-view="agents"]').click()
        expect(form.locator('[name="label"]')).to_have_value('保留中的新助手名称')
        page.locator('[data-cc-view="monitor"]').click()
        expect(probe).to_have_value('未提交的探针草稿')
        assert probe_node.evaluate('(n) => n.isConnected')
        page.locator('[data-cc-view="agents"]').click()
        capture(page, f'{engine}-{width}-connections.png')
        page.keyboard.press('Escape')
        expect(page.locator('#cc-workspace-panel')).not_to_be_visible()
        expect(area).to_be_focused()
        assert area.evaluate('(n) => [n.selectionStart, n.selectionEnd, n.dataset.identity]') == [2, 7, 'original']
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        capture(page, f'{engine}-{width}-conversation.png')
        no_work(stack)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_onboarding_prompts_are_drafts_and_room_search_restores_focus(collaboration_stack, chat_browser_pool, engine, width):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    try:
        login(page, stack)
        expect(page.locator('.cc-workspace-welcome')).to_contain_text('把你的 dot 带进来')
        page.locator('[data-cc-onboarding]').click()
        expect(page.locator('#cc-dot-create')).to_be_visible()
        page.locator('#cc-dot-create [name="label"]').fill('体验助手')
        page.locator('#cc-dot-create button[type="submit"]').click()
        expect(page.locator('#cc-drawer .cc-join-code')).to_be_visible()
        expect(page.locator('#cc-drawer .cc-join-instruction')).to_contain_text('加入')
        page.keyboard.press('Escape')
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('.cc-workspace-welcome')).to_contain_text('再连接一步')
        capture(page, f'{engine}-{width}-onboarding.png')
        area = page.locator('#cc-message-input')
        area.fill('切换房间入口时保留的草稿')
        original = area.element_handle()
        if width < 1000:
            page.locator('[data-cc-workspace="rooms"]').click()
            expect(page.locator('.cc-room-body')).to_have_attribute('inert', '')
        page.locator('#cc-room-search').fill('没有这个房间xyz')
        expect(page.locator('.cc-room-search-empty')).to_be_visible()
        expect(page.locator('.cc-room-list [data-cc-room]:visible')).to_have_count(0)
        page.locator('#cc-room-search').fill('')
        expect(page.locator('.cc-room-list [data-cc-room]:visible')).to_have_count(1)
        if width < 1000:
            page.keyboard.press('Escape')
            expect(page.locator('[data-cc-workspace="rooms"]')).to_be_focused()
            expect(page.locator('.cc-room-body')).not_to_have_attribute('inert', '')
        assert original.evaluate('(n) => n.isConnected')
        expect(area).to_have_value('切换房间入口时保留的草稿')
        assert all('保留的草稿' not in str(v) for v in page.evaluate('Object.values(localStorage).concat(Object.values(sessionStorage))'))
        no_work(stack)
    finally:
        context.close()


def test_suggestions_never_submit_or_overwrite_and_narrow_viewports(collaboration_stack, chat_browser_pool):
    stack = collaboration_stack
    context = chat_browser_pool('chromium').new_context(viewport={'width': 1440, 'height': 920})
    page = context.new_page()
    try:
        joined = prepare(stack, page)
        area = page.locator('#cc-message-input')
        prompt = page.locator('[data-cc-prompt]').first
        prompt.click()
        expect(area).to_have_value('梳理一下当前进展和下一步')
        assert call(stack, joined['inbox_request'])['items'] == []
        expect(prompt).to_be_disabled()
        area.fill('自己的草稿')
        page.locator('[data-cc-prompt]').last.evaluate('(n) => n.click()')
        expect(area).to_have_value('自己的草稿')
        for width in [320, 390, 768, 1024, 1440]:
            page.set_viewport_size({'width': width, 'height': 900})
            for view in ['jobs', 'agents', 'monitor', 'discussion']:
                page.locator(f'[data-cc-view="{view}"]').click()
                expect(page.locator(f'[data-cc-view="{view}"]')).to_have_attribute('aria-pressed', 'true')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, view)
            expect(area).to_have_value('自己的草稿')
            box = page.locator('#cc-command button[type="submit"]').bounding_box()
            assert box and box['y'] + box['height'] <= 900, (width, box)
        page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        capture(page, 'chromium-1440-dark.png')
        no_work(stack)
    finally:
        context.close()


def test_receipt_projection_is_scoped_conservative_and_nonmutating(chat_browser_pool):
    page = chat_browser_pool('chromium').new_page()
    try:
        page.add_script_tag(content=Path('web/collaboration-workspace.js').read_text())
        result = page.evaluate('''() => {
          const make = (id, state, extra = {}) => ({id, author_kind:'owner', project_id:'p', environment_id:'production', server_sequence:1, relay_receipts:[{state, dot_id:'d'}], ...extra});
          const rows = [make('private','working',{project_id:'other'}), make('stage','completed',{environment_id:'staging'}), make('reply','replied',{author_kind:'dot'}), make('unknown','new_server_state'), make('mix','completed'), make('active','replying')];
          rows[4].relay_receipts.push({state:'needs_attention',dot_id:'second'});
          const before = JSON.stringify(rows);
          const projection = window.CodePierCollaborationWorkspace.receiptRows(rows,'p','production');
          return {unchanged:before===JSON.stringify(rows), rows:projection.map(r=>[r.message.id,r.group,r.label])};
        }''')
        assert result['unchanged']
        assert result['rows'] == [['unknown', 'unknown', '状态待核对'], ['mix', 'attention', '需要关注'], ['active', 'active', '正在回复']]
    finally:
        page.close()
