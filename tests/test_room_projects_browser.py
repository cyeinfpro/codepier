"""Room-only project revisions in disposable Hub/Agent fixtures and real browsers."""

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack  # noqa: F401
from tests.test_collaboration_chatroom_browser import login, overview, open_add_project
from tests.test_collaboration_dots_flow import create_dot, join_dot

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def project_checkbox(form, project):
    return form.locator(f'[name="room_projects"][value="{project["id"]}"]')


def save(form):
    expect(form.locator('button[type="submit"]')).to_be_enabled()
    form.locator('button[type="submit"]').click()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_projects_add_remove_anchor_and_chat_only_preserve_connection(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    errors, applies = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: applies.append(request.post_data_json)
            if request.url.endswith('/api/collaboration/conversation-projects') else None)
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack, '合成房间原连接'))
        area = page.locator('#cc-message-input')
        area.fill('保留的房间历史')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        before = overview(stack)['conversation']
        area.fill('管理期间的本地草稿')
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(project_checkbox(form, stack.project)).to_be_enabled()
        expect(project_checkbox(form, stack.projects[1])).not_to_be_checked()
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('合成房间原连接')
        page.keyboard.press('Escape')
        expect(area).to_have_value('管理期间的本地草稿')
        assert not applies
        assert overview(stack)['conversation']['version'] == before['version']

        open_add_project(page)
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('button[type="submit"]').evaluate('(b) => {b.click(); b.click();}')
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        assert len(applies) == 1
        assert applies[0]['expected_scope_version'] == 0
        assert applies[0]['dot_id'] == joined['slot']['id']
        expect(area).to_have_value('管理期间的本地草稿')
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        connection = overview(stack)['conversation']['connection']
        assert connection['dot_id'] == joined['slot']['id']
        page.locator(f'[data-room-project="{stack.projects[1]["id"]}"]').click()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', connection['project_id'])
        expect(page.locator('[data-cc-partition]')).to_have_count(0)
        expect(area).to_have_value('管理期间的本地草稿')

        # The initial project is a regular removable task project, never a locked anchor.
        open_add_project(page)
        expect(project_checkbox(form, stack.project)).to_be_enabled()
        project_checkbox(form, stack.project).uncheck()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('移除')
        targets = form.locator('.cc-project-review span').filter(has_text='执行目标')
        original_targets = targets.all_text_contents()
        targets.evaluate_all("nodes => nodes.forEach(n => n.textContent = '执行目标：合成 Agent · /synthetic/project')")
        confirm = form.locator('button[type="submit"]')
        box = confirm.bounding_box()
        assert box and box['y'] + box['height'] <= page.viewport_size['height']
        page.screenshot(path=str(tmp_path / f'room-projects-review-{engine}-390.png'), full_page=True)
        targets.evaluate_all('(nodes, originals) => nodes.forEach((n, i) => n.textContent = originals[i])', original_targets)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert form.evaluate('(n) => n.scrollWidth <= n.clientWidth')
        save(form)
        expect(page.locator('.cc-project-chip')).to_have_count(1)
        expect(page.locator('.cc-project-chip')).to_have_attribute('data-room-project', stack.projects[1]['id'])
        current = overview(stack)['conversation']
        expect(page.locator('.cc-room-title h2')).to_have_text(stack.project['alias'])
        assert current['connection'] == connection and current['id'] == before['id']
        expect(area).to_have_value('管理期间的本地草稿')

        # Removing the last task project leaves the same room and chat connection usable.
        open_add_project(page)
        project_checkbox(form, stack.projects[1]).uncheck()
        expect(form.locator('.cc-project-chat-only')).to_contain_text('切换为纯聊天')
        save(form)
        expect(page.locator('.cc-project-chip')).to_have_count(0)
        expect(page.locator('.cc-project-empty')).to_contain_text('纯聊天')
        expect(page.locator('.cc-project-empty')).to_be_visible()
        expect(area).to_have_value('管理期间的本地草稿')
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        current = overview(stack)['conversation']
        assert current['projects'] == [] and current['connection'] == connection
        area.fill('纯聊天消息仍使用原连接')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('.cc-feed')).to_contain_text('纯聊天消息仍使用原连接')
        page.reload()
        expect(page.locator('.cc-project-empty')).to_contain_text('纯聊天')
        expect(page.locator('.cc-project-empty')).to_be_visible()
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        expect(page.locator('.cc-feed')).to_contain_text('纯聊天消息仍使用原连接')
        expect(area).to_be_enabled()
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.screenshot(path=str(tmp_path / f'room-projects-chat-only-{engine}-1440.png'), full_page=True)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_projects_late_preview_close_and_scope_conflict(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    held, writes = [], []
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack))
        page.locator('#cc-message-input').fill('迟到摘要不能覆盖的草稿')

        def delayed(route):
            if len(route.request.post_data_json['projects']) == 2:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n) => n.dataset.heldPreview = "ready"')
            else:
                route.continue_()

        def conflict(route):
            writes.append(route.request.post_data_json)
            route.fulfill(status=409, json={'error': {
                'code': 'STALE_VERSION', 'message': '房间范围已经改变'}})

        page.route('**/api/collaboration/conversation-projects-preview', delayed)
        page.route('**/api/collaboration/conversation-projects', conflict)
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(page.locator('html')).to_have_attribute('data-held-preview', 'ready')
        assert held
        # A newer selection wins even if the old network response arrives last.
        project_checkbox(form, stack.projects[1]).uncheck()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        current_summary = form.locator('[data-room-project-summary]').inner_text()
        route, response = held.pop()
        page.locator('html').evaluate('(n) => delete n.dataset.heldPreview')
        route.fulfill(response=response)
        expect(form.locator('[data-room-project-summary]')).to_have_text(current_summary, use_inner_text=True)
        assert not writes

        # Dismissal invalidates another in-flight preview and never sends an apply.
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(page.locator('html')).to_have_attribute('data-held-preview', 'ready')
        page.keyboard.press('Escape')
        route, response = held.pop()
        page.locator('html').evaluate('(n) => delete n.dataset.heldPreview')
        route.fulfill(response=response)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-message-input')).to_have_value('迟到摘要不能覆盖的草稿')
        assert not writes

        page.unroute('**/api/collaboration/conversation-projects-preview', delayed)
        open_add_project(page)
        project_checkbox(form, stack.projects[1]).check()
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-feedback')).to_contain_text('重新审阅')
        expect(page.locator('#cc-message-input')).to_have_value('迟到摘要不能覆盖的草稿')
        assert len(writes) == 1
        assert writes[0]['expected_scope_version'] == 0
        assert writes[0]['selection_digest']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        for route, _ in held:
            try:
                route.abort()
            except Exception:
                pass
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_projects_multiple_connections_require_explicit_choice(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '候选助手甲'))
        second = join_dot(stack, create_dot(stack, '候选助手乙'))
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        expect(form.locator('select[name="dot_id"]')).to_have_value('')
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(project_checkbox(form, stack.projects[1])).not_to_be_checked()
        form.locator('select[name="dot_id"]').select_option(second['slot']['id'])
        expect(form.locator('[data-room-project-summary]')).to_contain_text('候选助手乙')
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        page.locator('[data-cc-action="close-drawer"]').last.click()
        assert overview(stack)['conversation'].get('working_scope', {}).get('version', 0) == 0
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_removed_historical_project_thread_reply_and_convert_keep_connection(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key

    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    sends, task_creates = [], []
    page.on('request', lambda r: sends.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/message') else None)
    page.on('request', lambda r: task_creates.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/message-to-task') else None)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack))
        room = overview(stack)['conversation']
        other = stack.projects[1]
        stack.must(stack.client.post('/api/collaboration/conversation-project', json={
            'conversation_id': room['id'], 'expected_version': room['version'],
            'project': other['id'], 'environment_id': 'production', 'idempotency_key': key()}))
        historical_room = next(p['room_id'] for p in overview(stack)['conversation']['projects']
                               if p['project_id'] == other['id'])
        historical = stack.must(stack.client.post('/api/collaboration/message', json={
            'room_id': historical_room,
            'project': other['id'], 'environment_id': 'production',
            'conversation_id': room['id'], 'body_text': '旧项目中保留的来源消息',
            'client_message_id': key(), 'idempotency_key': key(), 'mentions': []}))
        page.reload()
        expect(page.locator('.cc-feed')).to_contain_text('旧项目中保留的来源消息')
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        project_checkbox(form, other).uncheck()
        save(form)
        expect(page.locator('.cc-project-chip')).to_have_count(1)
        assert overview(stack)['conversation']['working_scope']['project_ids'] == [stack.project['id']]
        message = page.locator(f'[data-message-id="{historical["message"]["id"]}"]')
        message.locator('[data-cc-action="thread"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('旧项目中保留的来源消息')
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', stack.project['id'])
        page.keyboard.press('Escape')
        page.locator('[data-cc-action="search"]').click()
        page.locator('#cc-search [name="query"]').fill('旧项目中保留的来源消息')
        page.locator('#cc-search button[type="submit"]').click()
        page.locator('.cc-search-results [data-cc-action="source"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', stack.project['id'])
        message.locator('[data-cc-action="reply"]').click()
        expect(page.locator('.cc-reply-preview')).to_contain_text('旧项目中保留的来源消息')
        page.locator('#cc-message-input').fill('在原连接回复旧项目历史')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('.cc-feed')).to_contain_text('在原连接回复旧项目历史')
        assert sends[-1]['project'] == stack.project['id']
        assert sends[-1]['reply_to_id'] == historical['message']['id']
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', stack.project['id'])
        message.locator('[data-cc-action="convert"]').click()
        expect(page.locator('#cc-task-review button[type="submit"]')).to_be_disabled()
        expect(page.locator('.cc-task-blocker')).to_contain_text('该项目已移出房间，请用当前项目创建新任务')
        assert not task_creates
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', stack.project['id'])
    finally:
        context.close()
