"""Deterministic hint scheduling and access recovery in the real isolated panel."""
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_chatroom_browser import login, open_add_project, overview
from tests.test_collaboration_dots_flow import create_dot, join_dot

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]

# Only hint delivery is controlled; reads/actions/previews use the real Hub.
HINTS = r"""
(() => {
  window.testHintSources = [];
  window.EventSource = class {
    constructor(url) { this.url=url; this.handlers={}; this.closed=false;
      testHintSources.push(this); }
    addEventListener(name, fn) { this.handlers[name]=fn; }
    close() { this.closed=true; }
    emit(name, data) { this.handlers[name]?.({data:JSON.stringify(data)}); }
  };
  window.testHint = (name, data) => {
    const source=testHintSources.filter(s=>!s.closed).at(-1);
    if(!source)throw Error('ordinary room has no live hint transport');
    source.emit(name,data);
  };
})();
"""


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_ordinary_hints_defer_busy_action_and_coalesce_one_trailing_refresh(
        collaboration_stack, chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    page.add_init_script(HINTS)
    try:
        login(page, collaboration_stack)
        page.locator('#cc-message-input').fill('保留的普通房间消息')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('保留的普通房间消息')
        expect(page.locator('#cc-command button[type="submit"]')).to_be_enabled()
        threads, timelines, kinds, relay_reads = [], [], [], []
        page.on('request', lambda request: relay_reads.append(request.url)
                if '/api/collaboration/relay-sync?' in request.url else None)

        def route_reads(route):
            kind = parse_qs(urlsplit(route.request.url).query).get('kind', ['overview'])[0]
            kinds.append(kind)
            if kind == 'thread' and not threads:
                threads.append((route, route.fetch()))
                page.locator('html').evaluate('(n)=>n.dataset.hintThreadHeld="yes"')
            elif kind == 'timeline':
                timelines.append(route)
                page.locator('html').evaluate('(n,count)=>n.dataset.hintTimelineCount=count', str(len(timelines)))
                if len(timelines) > 1:
                    route.continue_()
            else:
                route.continue_()

        page.route('**/api/collaboration?*', route_reads)
        page.locator('.cc-feed [data-cc-action="thread"]').first.click()
        expect(page.locator('html')).to_have_attribute('data-hint-thread-held', 'yes')
        page.evaluate('()=>{for(let i=0;i<8;i++)testHint("change",{revision:"busy-"+i});}')
        page.wait_for_timeout(125)  # Flush the documented 60 ms coalescer.
        assert not timelines and not relay_reads
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        threads[0][0].fulfill(response=threads[0][1])
        expect(page.locator('html')).to_have_attribute('data-hint-timeline-count', '1')
        page.evaluate('()=>{for(let i=0;i<8;i++)testHint("change",{revision:"inflight-"+i});}')
        page.wait_for_timeout(125)
        assert len(timelines) == 1
        timelines[0].continue_()
        expect(page.locator('html')).to_have_attribute('data-hint-timeline-count', '2')
        page.wait_for_timeout(125)
        assert len(timelines) == 2 and not relay_reads
        assert {'timeline', 'overview', 'members', 'coordination_options'} <= set(kinds)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('outcome', ['expanded', 'removed', 'navigation', 'focus'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_hint_access_change_clears_before_recheck_and_only_restores_authorized_intent(
        collaboration_stack, chat_browser_pool, engine, outcome):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    page.add_init_script(HINTS)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '权限恢复测试连接'))
        page.locator('#cc-message-input').fill('必须先清除的旧权限记录')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('必须先清除的旧权限记录')
        page.locator('#cc-message-input').fill('仅仍获权时恢复的本地草稿')
        current = overview(stack)
        conversation = current['conversation']
        other = stack.projects[1]
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        form.locator(f'[name="room_projects"][value="{other["id"]}"]').check()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        stack.must(stack.client.post('/api/collaboration/conversation-project', json={
            'conversation_id': conversation['id'], 'expected_version': conversation['version'],
            'project': other['id'], 'environment_id': 'production', 'idempotency_key': key()}))
        if outcome == 'removed':
            store = Store(stack.hubdir)
            try:
                with store.transaction():
                    store.execute('DELETE FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                                  (conversation['id'], current['room']['id']))
            finally:
                store.close()
        held = []

        def directory(route):
            if not held:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n)=>n.dataset.hintDirectoryHeld="yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration/conversations', directory)
        page.evaluate('()=>testHint("access_changed",{})')
        expect(page.locator('html')).to_have_attribute('data-hint-directory-held', 'yes')
        expect(page.locator('.cc-feed')).not_to_contain_text('必须先清除的旧权限记录')
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('#cc-message-input')).to_be_disabled()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-drawer')).to_be_empty()
        if outcome == 'navigation':
            page.get_by_role('button', name='项目映射', exact=True).click()
            expect(page.locator('#page h1')).to_have_text('项目映射')
        external = page.get_by_role('button', name='项目映射', exact=True)
        if outcome == 'focus':
            external.focus()
            expect(external).to_be_focused()
        held[0][0].fulfill(response=held[0][1])
        if outcome == 'expanded':
            expect(page.locator('#cc-message-input')).to_have_value('仅仍获权时恢复的本地草稿')
            expect(form).to_be_visible()
            expect(form.locator(f'[name="room_projects"][value="{other["id"]}"]')).to_be_checked()
            expect(form.locator('[data-room-project-summary]')).to_contain_text('已重新检查并保留你的选择')
            expect(form.locator('button[type="submit"]')).to_be_enabled()
        elif outcome == 'focus':
            expect(page.locator('#cc-message-input')).to_have_value('仅仍获权时恢复的本地草稿')
            expect(form).to_have_count(0)
            expect(external).to_be_focused()
        elif outcome == 'removed':
            expect(page.locator('#cc-feedback')).to_contain_text('请重新选择仍获权的项目')
            expect(page.locator('#cc-message-input')).to_have_value('')
            expect(page.locator('#cc-message-input')).to_be_disabled()
            expect(form).to_have_count(0)
        else:
            expect(page.locator('#page h1')).to_have_text('项目映射')
            expect(page.locator('.collaboration')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('kind', ['projects', 'archive'])
@pytest.mark.parametrize('outcome', ['applied', 'conflict', 'navigation'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_hint_waits_for_original_mutation_receipt_without_replay(
        collaboration_stack, chat_browser_pool, engine, outcome, kind):
    from tests.test_room_archive_browser import archive_preview

    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    page.add_init_script(HINTS)
    try:
        login(page, stack)
        if kind == 'projects':
            join_dot(stack, create_dot(stack, '在途回执测试连接'))
        page.locator('#cc-message-input').fill('权限变化必须立即清除的记录')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('权限变化必须立即清除的记录')
        page.locator('#cc-message-input').fill('仍获权时保留的本地输入')
        before = overview(stack)['conversation']
        if kind == 'projects':
            open_add_project(page)
            form = page.locator('#cc-room-projects')
            form.locator(f'[name="room_projects"][value="{stack.projects[1]["id"]}"]').check()
        else:
            form = archive_preview(page)
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        held, writes = [], []

        def hold_receipt(route):
            writes.append(route.request.post_data_json)
            if outcome == 'conflict':
                stack.must(stack.client.post('/api/collaboration/conversation-project', json={
                    'conversation_id': before['id'], 'expected_version': before['version'],
                    'project': stack.projects[1]['id'], 'environment_id': 'production',
                    'idempotency_key': key()}))
            response = route.fetch()
            assert response.status == (409 if outcome == 'conflict' else 200)
            held.append((route, response))
            page.locator('html').evaluate('(node)=>node.dataset.mutationReceiptHeld="yes"')

        operation = 'conversation-projects' if kind == 'projects' else 'conversation-archive'
        page.route('**/api/collaboration/' + operation, hold_receipt)
        form.locator('button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-mutation-receipt-held', 'yes')
        page.evaluate('()=>testHint("access_changed",{})')
        # No acknowledgement has reached the UI yet. Security clearing must not
        # wait for it, and a possibly committed mutation must never be replayed.
        expect(page.locator('.cc-feed')).not_to_contain_text('权限变化必须立即清除的记录')
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('#cc-message-input')).to_be_disabled()
        expect(page.locator('#cc-drawer')).to_be_empty()
        if outcome == 'navigation':
            page.get_by_role('button', name='项目映射', exact=True).click()
            expect(page.locator('#page h1')).to_have_text('项目映射')
        held[0][0].fulfill(response=held[0][1])
        if outcome == 'navigation':
            expect(page.locator('#page h1')).to_have_text('项目映射')
            expect(page.locator('.collaboration')).to_have_count(0)
        elif outcome == 'applied' and kind == 'archive':
            expect(page.locator('.cc-archived-empty')).to_be_visible()
        elif outcome == 'applied':
            expect(page.locator('#cc-feedback')).to_contain_text('房间项目与权限已保存')
            expect(page.locator('#cc-drawer')).not_to_be_visible()
            expect(page.locator('#cc-message-input')).to_have_value('仍获权时保留的本地输入')
        elif kind == 'projects':
            expect(page.locator('#cc-room-projects')).to_be_visible()
            expect(page.locator('[data-room-project-summary]')).to_contain_text('已重新检查并保留你的选择')
            expect(page.locator('#cc-feedback')).not_to_contain_text('已保存')
        else:
            expect(page.locator('#cc-message-input')).to_be_enabled()
            expect(page.locator('.cc-archived-empty')).to_have_count(0)
        assert len(writes) == 1
    finally:
        context.close()
