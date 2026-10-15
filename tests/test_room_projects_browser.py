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
    network = []
    page.on('requestfailed', lambda request: network.append({'url': request.url, 'failure': request.failure}))
    page.on('response', lambda response: network.append({'url': response.url, 'status': response.status})
            if 'kind=coordination_options' in response.url else None)
    page.on('pageerror', lambda error: errors.append({'message': str(error), 'stack': error.stack}))
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
        evidence_sizes(page, tmp_path, 'room-project-add-review', engine)
        form.locator('button[type="submit"]').evaluate('(b) => {b.click(); b.click();}')
        expect(page.locator('.cc-project-count')).to_have_text('2 个项目')
        assert len(applies) == 1
        assert applies[0]['expected_scope_version'] == 0
        assert applies[0]['dot_id'] == joined['slot']['id']
        expect(page.locator('.cc-room-subline > span')).to_have_text('2 个项目')
        evidence_sizes(page, tmp_path, 'room-project-added', engine)
        expect(area).to_have_value('管理期间的本地草稿')
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        connection = overview(stack)['conversation']['connection']
        assert connection['dot_id'] == joined['slot']['id']
        page.locator('.cc-project-summary [data-cc-action="manage-projects"]').click()
        expect(page.locator(f'#cc-room-projects [name="room_projects"][value="{stack.projects[1]["id"]}"]')).to_be_checked()
        page.keyboard.press('Escape')
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
        evidence_sizes(page, tmp_path, 'room-project-remove-review', engine)
        targets.evaluate_all('(nodes, originals) => nodes.forEach((n, i) => n.textContent = originals[i])', original_targets)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert form.evaluate('(n) => n.scrollWidth <= n.clientWidth')
        save(form)
        expect(page.locator('.cc-project-count')).to_have_text('1 个项目')
        open_add_project(page)
        expect(project_checkbox(page.locator('#cc-room-projects'), stack.projects[1])).to_be_checked()
        page.keyboard.press('Escape')
        current = overview(stack)['conversation']
        expect(page.locator('.cc-room-title h2')).to_have_text(stack.project['alias'])
        assert current['connection'] == connection and current['id'] == before['id']
        expect(page.locator('.cc-room-subline > span')).to_have_text('1 个项目')
        expect(page.locator('.cc-compose-controls > small')).to_have_text('来源 · ' + stack.project['alias'])
        expect(page.locator('.cc-compose-help > span').first).to_have_text('发送到 ' + stack.project['alias'] + ' · production')
        evidence_sizes(page, tmp_path, 'room-project-removed-anchor', engine)
        expect(area).to_have_value('管理期间的本地草稿')

        # Removing the last task project leaves the same room and chat connection usable.
        open_add_project(page)
        project_checkbox(form, stack.projects[1]).uncheck()
        expect(form.locator('.cc-project-chat-only')).to_contain_text('全部取消后仍可聊天')
        save(form)
        expect(page.locator('.cc-project-chip')).to_have_count(0)
        expect(page.locator('.cc-project-empty')).to_contain_text('纯聊天')
        expect(page.locator('.cc-project-empty')).to_be_visible()
        expect(area).to_have_value('管理期间的本地草稿')
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        current = overview(stack)['conversation']
        assert current['projects'] == [] and current['connection'] == connection
        area.fill('纯聊天消息仍使用原连接')
        with page.expect_request('**/api/collaboration/message') as sent:
            page.locator('#cc-command button[type="submit"]').click()
        assert sent.value.post_data_json['project'] == connection['project_id']
        assert sent.value.post_data_json['environment_id'] == connection['environment_id']
        expect(area).to_have_value('')
        expect(page.locator('.cc-feed')).to_contain_text('纯聊天消息仍使用原连接')
        page.reload()
        expect(page.locator('.cc-project-empty')).to_contain_text('纯聊天')
        expect(page.locator('.cc-project-empty')).to_be_visible()
        expect(page.locator('.cc-feed')).to_contain_text('保留的房间历史')
        expect(page.locator('.cc-feed')).to_contain_text('纯聊天消息仍使用原连接')
        expect(area).to_be_enabled()
        page.set_viewport_size({'width': 1440, 'height': 900})
        evidence_sizes(page, tmp_path, 'room-project-chat-only', engine)
        assert not errors, {'errors': errors, 'network': network}
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
        expect(page.locator('#cc-drawer')).to_be_visible()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('已重新检查并保留你的选择')
        expect(project_checkbox(form, stack.projects[1])).to_be_checked()
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
        expect(page.locator('.cc-project-count')).to_have_text('1 个项目')
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


def connections(page):
    page.locator('[data-cc-view="agents"]').click()
    expect(page.locator('.cc-channel-project-entry')).to_be_visible()


def manage_from_connections(page):
    connections(page)
    page.locator('.cc-channel-project-entry [data-cc-action="manage-projects"]').click()
    return page.locator('#cc-room-projects')


def advanced_connections(page):
    connections(page)
    page.locator('.cc-channel-settings [data-cc-action="dot-channel-open"]').click()
    page.locator('[data-cc-action="dot-channel-view-advanced"]').click()
    return page.locator('.cc-advanced-settings')


def evidence(page, tmp_path, name):
    import os
    from pathlib import Path
    destination = Path(os.environ.get('ROOM_UI_EVIDENCE_DIR', str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / name), full_page=True, animations='disabled')


def evidence_sizes(page, tmp_path, state, engine):
    viewport = page.viewport_size
    try:
        for width, height in ((390, 844), (1440, 900)):
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            evidence(page, tmp_path, f'{state}-{engine}-{width}.png')
    finally:
        page.set_viewport_size(viewport)



@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_simple_connection_picker_search_bulk_empty_and_reopen(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    page.set_default_timeout(10000)
    writes = []
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/conversation-projects') else None)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成助手'))
        form = manage_from_connections(page)
        second = stack.projects[1]
        project_checkbox(form, stack.project).uncheck()
        project_checkbox(form, second).check()
        expect(form.locator('button[type="submit"]')).to_have_text('保存项目与权限')
        expect(form).to_contain_text('新项目不会自动加入')
        expect(form.locator('[data-cc-action="room-project-preview"]')).to_have_count(0)
        expect(form.locator('.cc-project-permission-details')).not_to_have_attribute('open', '')
        evidence(page, tmp_path, f'room-project-simple-{engine}-390.png')
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-room-subline')).to_contain_text('1 个项目')
        anchor = overview(stack)['conversation']['connection']

        form = manage_from_connections(page)
        search = form.locator('[data-project-search]')
        search.fill(stack.project['alias'])
        expect(project_checkbox(form, second)).not_to_be_visible()
        form.locator('[data-project-bulk="all"]').click()
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(project_checkbox(form, second)).to_be_checked()
        form.locator('[data-project-bulk="none"]').click()
        expect(project_checkbox(form, stack.project)).not_to_be_checked()
        expect(project_checkbox(form, second)).to_be_checked()
        expect(form.locator('[data-project-selection-count]')).to_contain_text('1 项在搜索结果外')
        search.fill('没有匹配的合成项目')
        expect(form.locator('[data-project-bulk="all"]')).to_be_disabled()
        expect(form.locator('[data-project-search-empty]')).to_be_visible()
        search.fill('')
        form.locator('[data-project-bulk="all"]').click()
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(project_checkbox(form, second)).to_be_checked()
        save(form)
        expect(page.locator('.cc-project-count')).to_have_text(f'{len(stack.projects)} 个项目')
        expect(page.locator('.cc-project-chip')).to_have_count(0)
        assert page.locator('.cc-project-summary').evaluate('(node) => node.scrollWidth <= node.clientWidth')
        page.locator('.cc-project-summary [data-cc-action="manage-projects"]').click()
        expect(page.locator('#cc-room-projects [name="room_projects"]:checked')).to_have_count(len(stack.projects))
        page.keyboard.press('Escape')
        assert overview(stack)['conversation']['connection'] == anchor

        form = manage_from_connections(page)
        form.locator('[data-project-bulk="none"]').click()
        expect(form.locator('[name="room_projects"]:checked')).to_have_count(0)
        page.keyboard.press('Escape')
        form = manage_from_connections(page)
        expect(form.locator('[name="room_projects"]:checked')).to_have_count(len(stack.projects))
        form.locator('[data-project-bulk="none"]').click()
        save(form)
        expect(page.locator('.cc-room-subline')).to_contain_text('仅聊天')
        expect(page.locator('.cc-project-chip')).to_have_count(0)
        form = manage_from_connections(page)
        expect(form.locator('[name="room_projects"]:checked')).to_have_count(0)
        page.keyboard.press('Escape')
        page.locator('[data-cc-workspace-view="discussion"]').first.click()
        area = page.locator('#cc-message-input')
        area.fill('清空项目后仍由原连接保存的消息')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('.cc-feed')).to_contain_text('清空项目后仍由原连接保存的消息')
        assert overview(stack)['conversation']['connection'] == anchor
        assert len(writes) == 3
        page.set_viewport_size({'width': 1440, 'height': 900})
        evidence(page, tmp_path, f'room-project-empty-{engine}-1440.png')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_link_permissions_only_current_projects_and_bulk_all_capabilities(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    shared_writes, writes = [], []
    page.on('request', lambda r: shared_writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/room-scope') else None)
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/conversation-projects') else None)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成原助手'))
        form = manage_from_connections(page)
        project_checkbox(form, stack.projects[1]).check()
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        before = overview(stack)['conversation']
        group = advanced_connections(page)
        expect(group.locator('[data-channel-form="scope"]')).to_have_count(0)
        group.locator('[data-cc-action="manage-room-permissions"]').click()
        form = page.locator('#cc-room-projects')
        expect(form).to_have_attribute('data-permissions-only', 'true')
        expect(form.locator('[name="room_projects"]')).to_have_count(2)
        for project in stack.projects[2:]:
            expect(project_checkbox(form, project)).to_have_count(0)
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('[data-project-bulk="none"]').click()
        expect(form.locator('[data-project-capability]:checked')).to_have_count(0)
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert len(overview(stack)['conversation']['projects']) == 2
        assert all(p['capabilities'] == [] for p in writes[-1]['projects'])
        group = advanced_connections(page)
        group.locator('[data-cc-action="manage-room-permissions"]').click()
        form = page.locator('#cc-room-projects')
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('[data-project-bulk="all"]').click()
        expect(form.locator('[data-project-capability]:checked')).to_have_count(6)
        expect(form.locator('[data-room-project-summary]')).to_contain_text('执行')
        evidence(page, tmp_path, f'room-project-permissions-{engine}-390.png')
        form.locator('[data-room-project-summary]').scroll_into_view_if_needed()
        evidence(page, tmp_path, f'room-project-permission-summary-{engine}-390.png')
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert writes[-1]['permissions_only'] is True
        assert all(p['capabilities'] == ['read', 'write', 'execute'] for p in writes[-1]['projects'])
        assert overview(stack)['conversation']['connection'] == before['connection']
        assert not shared_writes
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_upstream_limit_is_explicit_and_never_expands_shared_connection(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key
    from tests.test_collaboration_delivery_flow import scope
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    shared_writes = []
    page.on('request', lambda r: shared_writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/room-scope') else None)
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack, '合成受限助手'))
        conversation = overview(stack)['conversation']
        intent = {'conversation_id': conversation['id'], 'dot_id': joined['slot']['id'],
                  'projects': [{'project': stack.project['id'], 'environment_id': 'production'}]}
        preview = stack.must(stack.client.post('/api/collaboration/conversation-projects-preview', json=intent))
        grant = preview['connection']['grant_id']
        stack.must(stack.client.post('/api/collaboration/room-scope', json={
            **scope(stack), 'conversation_id': conversation['id'], 'grant_id': grant,
            'expected_version': 0, 'project_rules': [{'project_id': stack.project['id'],
                'capabilities': ['read', 'write', 'execute'], 'execution_targets': ['agent']}],
            'execution_mode': 'account_permissions_acknowledged', 'acknowledge_unsandboxed_exec': True,
            'confirm_connection_wide_ceiling': True, 'expires_seconds': 3600, 'idempotency_key': key()}))
        baseline = stack.must(stack.client.get('/api/collaboration/room-scopes', params={**scope(stack), 'conversation_id': conversation['id']}))
        form = manage_from_connections(page)
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('尚未保存')
        expect(form.locator('[data-room-project-summary]')).to_contain_text('共享凭据')
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(project_checkbox(form, stack.projects[1])).to_be_checked()
        expect(form.locator('[data-cc-action="room-project-connection-review"]')).to_have_count(0)
        page.keyboard.press('Escape')
        form = manage_from_connections(page)
        expect(project_checkbox(form, stack.projects[1])).not_to_be_checked()
        page.keyboard.press('Escape')
        after = stack.must(stack.client.get('/api/collaboration/room-scopes', params={**scope(stack), 'conversation_id': conversation['id']}))
        assert after['items'] == baseline['items']
        assert overview(stack)['conversation']['version'] == conversation['version']
        assert not shared_writes
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_preview_retry_keeps_selection_and_new_room_reuses_bulk_picker(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    page.set_default_timeout(10000)
    calls = []
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack))
        def flaky(route):
            calls.append(route.request.post_data_json)
            if len(calls) <= 2:
                route.fulfill(status=503, json={'error': {'code': 'TEMPORARY', 'message': '合成短暂离线'}})
            else:
                route.continue_()
        page.route('**/api/collaboration/conversation-projects-preview', flaky)
        form = manage_from_connections(page)
        expect(form).to_have_attribute('data-preview-state', 'error')
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('选择已保留')
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert len(calls) == 3
        assert calls[0]['projects'] == calls[1]['projects'] == calls[2]['projects']
        page.locator('[data-cc-more]').click()
        page.locator('.cc-room-menu [data-cc-action="new-conversation"]').click()
        form = page.locator('#cc-new-conversation')
        form.locator('[data-project-bulk="all"]').click()
        expect(form.locator('[name="projects"]:checked')).to_have_count(len(stack.projects))
        form.locator('[data-project-search]').fill(stack.project['alias'])
        form.locator('[data-project-bulk="none"]').click()
        expect(form.locator(f'[name="projects"][value="{stack.projects[1]["id"]}"]')).to_be_checked()
        expect(form.locator(f'[name="projects"][value="{stack.project["id"]}"]')).not_to_be_checked()
        page.keyboard.press('Escape')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_shared_vps_target_is_read_only_from_room_connection_entry(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key
    from tests.test_collaboration_delivery_flow import scope
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    writes = []
    page.on('request', lambda r: writes.append(r.url)
            if r.url.endswith('/api/collaboration/room-scope') else None)
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack, '合成目标保留助手'))
        conversation = overview(stack)['conversation']
        target = stack.must(stack.client.post('/api/vps', json={
            'name': 'Synthetic target only', 'host': 'target.example.invalid', 'username': 'fixture',
            'password': 'synthetic-not-real', 'project_ids': [p['id'] for p in stack.projects]}))['target']
        preview = stack.must(stack.client.post('/api/collaboration/conversation-projects-preview', json={
            'conversation_id': conversation['id'], 'dot_id': joined['slot']['id'],
            'projects': [{'project': stack.project['id'], 'environment_id': 'production'}]}))
        grant = preview['connection']['grant_id']
        stack.must(stack.client.post('/api/collaboration/room-scope', json={
            **scope(stack), 'conversation_id': conversation['id'], 'grant_id': grant,
            'expected_version': 0, 'project_rules': [{'project_id': stack.project['id'],
                'capabilities': ['read', 'execute'], 'execution_targets': [target]}],
            'execution_mode': 'account_permissions_acknowledged', 'acknowledge_unsandboxed_exec': True,
            'confirm_connection_wide_ceiling': True, 'expires_seconds': 3600, 'idempotency_key': key()}))
        group = advanced_connections(page)
        expect(group).to_contain_text('只读')
        expect(group.locator('[name="grant_id"], [name="hours"], [data-channel-form="scope"]')).to_have_count(0)
        page.keyboard.press('Escape')
        saved = stack.must(stack.client.get('/api/collaboration/room-scopes', params={
            **scope(stack), 'conversation_id': conversation['id']}))
        assert saved['items'][0]['manifest']['project_rules'][0]['execution_targets'] == [target]
        assert not writes
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_apply_concurrent_connection_limit_refreshes_remedy_without_resubmitting(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key
    from tests.test_collaboration_delivery_flow import scope
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    page.set_default_timeout(10000)
    writes = []
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack))
        form = manage_from_connections(page)
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        preview = form.evaluate('(form) => form.projectAuthorization')
        def change_ceiling(route):
            writes.append(route.request.post_data_json)
            stack.must(stack.client.post('/api/collaboration/room-scope', json={
                **scope(stack), 'conversation_id': joined['slot']['conversation_id'],
                'grant_id': preview['connection']['grant_id'], 'expected_version': 0,
                'project_rules': [{'project_id': stack.project['id'], 'capabilities': ['read','write','execute'],
                                   'execution_targets': ['agent']}],
                'execution_mode': 'account_permissions_acknowledged', 'acknowledge_unsandboxed_exec': True,
                'confirm_connection_wide_ceiling': True, 'expires_seconds': 3600, 'idempotency_key': key()}))
            route.continue_()
        page.route('**/api/collaboration/conversation-projects', change_ceiling)
        save(form)
        expect(form.locator('[data-room-project-summary]')).to_contain_text('上游权限')
        expect(form.locator('[data-cc-action="room-project-connection-review"]')).to_have_count(0)
        expect(project_checkbox(form, stack.projects[1])).to_be_checked()
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        assert len(writes) == 1
        assert overview(stack)['conversation'].get('working_scope', {}).get('version', 0) == 0
        assert writes[0]['dot_id'] == joined['slot']['id']
    finally:
        context.close()


def open_scope_form(page):
    form = page.locator('[data-channel-form="scope"]')
    if not form.is_visible():
        form.locator('..').locator(':scope > summary').click()
    return form


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_early_bulk_intent_survives_late_initial_permission_preview(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    held, seen = [], []
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack))
        form = manage_from_connections(page)
        project_checkbox(form, stack.projects[1]).check()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        first = project_checkbox(form, stack.project).locator('..').locator('..')
        first.locator('[data-project-capability][value="write"]').uncheck()
        first.locator('[data-project-capability][value="execute"]').uncheck()
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        def delay_initial(route):
            seen.append(route.request.post_data_json)
            if len(seen) == 1:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n) => n.dataset.heldFirstPermission = "ready"')
            else:
                route.continue_()
        page.route('**/api/collaboration/conversation-projects-preview', delay_initial)
        group = advanced_connections(page)
        group.locator('[data-cc-action="manage-room-permissions"]').click()
        expect(page.locator('html')).to_have_attribute('data-held-first-permission', 'ready')
        form = page.locator('#cc-room-projects')
        form.locator('[data-project-bulk="all"]').click()
        expect(form.locator('[data-project-capability]:checked')).to_have_count(6)
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        route, response = held.pop()
        route.fulfill(response=response)
        expect(form.locator('[data-project-capability]:checked')).to_have_count(6)
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert len(seen) >= 3
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_global_connection_scope_is_separate_confirmed_and_unblocks_archive_dependency(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    from tests.collaboration_support import key
    from tests.test_collaboration_delivery_flow import scope
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    shared_writes, room_writes = [], []
    page.on('request', lambda r: shared_writes.append((r.url.rsplit('/', 1)[-1], r.post_data_json))
            if r.method == 'POST' and r.url.rsplit('/', 1)[-1] in {'room-scope', 'room-scope-control'} else None)
    page.on('request', lambda r: room_writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/conversation-projects') else None)
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack, '合成范围核对助手'))
        before = overview(stack)['conversation']
        preview = stack.must(stack.client.post('/api/collaboration/conversation-projects-preview', json={
            'conversation_id': before['id'], 'dot_id': joined['slot']['id'],
            'projects': [{'project': stack.project['id'], 'environment_id': 'production'}]}))
        grant = preview['connection']['grant_id']
        rules = [{'project_id': stack.project['id'], 'capabilities': ['read', 'write', 'execute'],
                  'execution_targets': ['agent']}]
        stack.must(stack.client.post('/api/collaboration/room-scope', json={
            **scope(stack), 'conversation_id': before['id'], 'grant_id': grant,
            'expected_version': 0, 'project_rules': rules,
            'execution_mode': 'account_permissions_acknowledged', 'acknowledge_unsandboxed_exec': True,
            'confirm_connection_wide_ceiling': True, 'expires_seconds': 3600, 'idempotency_key': key()}))
        initial = stack.must(stack.client.get('/api/collaboration/room-scopes', params={
            **scope(stack), 'conversation_id': before['id']}))['items'][0]
        blocked = stack.must(stack.client.post('/api/collaboration/conversation-archive-preview',
                                              json={'conversation_id': before['id']}))
        assert blocked['ready'] is False and blocked['dependent_connections'][0]['grant_id'] == grant
        assert '整条连接限制' in blocked['message'] and '所有使用同一凭据的会话' in blocked['message']

        page.locator('[data-cc-more]').click()
        page.locator('[data-cc-action="archive-room"]').click()
        expect(page.locator('#cc-drawer h2')).to_have_text('暂时不能删除此房间')
        expect(page.locator('#cc-drawer')).to_contain_text('查看整条连接限制')
        evidence_sizes(page, tmp_path, 'connection-archive-dependency-guide', engine)
        page.locator('#cc-drawer').get_by_role('button', name='返回', exact=True).click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        group = advanced_connections(page)
        expect(group.locator('[data-channel-form="scope"]')).to_have_count(0)
        expect(group.locator('[data-cc-action="manage-room-permissions"]')).to_be_visible()
        evidence_sizes(page, tmp_path, 'connection-separated-entry', engine)
        group.locator('[data-cc-action="dot-channel-view-connection-scope"]').click()
        expect(page.locator('[data-channel-view]')).to_have_attribute('data-channel-view', 'connection-scope')
        expect(page.locator('#cc-drawer h2')).to_have_text('整条 MCP 凭据限制')
        expect(page.locator('.cc-connection-warning').first).to_contain_text('所有使用同一凭据的会话')
        form = open_scope_form(page)
        expect(form.locator('[name="grant_id"]')).to_have_value(grant)
        expect(form.locator('[name="confirm_scope"]')).not_to_be_checked()
        expect(form.locator('[name="unsandboxed"]')).not_to_be_checked()
        assert not shared_writes and not room_writes
        form.scroll_into_view_if_needed()
        evidence_sizes(page, tmp_path, 'connection-global-scope-top', engine)
        # Merely returning or reopening neither changes the scope nor retains consent.
        form.locator('[name="confirm_scope"]').check()
        page.locator('[data-cc-action="dot-channel-view-advanced"]').click()
        expect(page.locator('[data-channel-form="scope"]')).to_have_count(0)
        evidence_sizes(page, tmp_path, 'connection-back-to-room-permissions', engine)
        page.locator('[data-cc-action="dot-channel-view-connection-scope"]').click()
        form = open_scope_form(page)
        expect(form.locator('[name="confirm_scope"]')).not_to_be_checked()
        form.locator('[name="hours"]').fill('2')
        form.locator('[data-cc-action="dot-channel-scope-save"]').click()
        assert not shared_writes
        form.locator('[name="confirm_scope"]').check()
        form.locator('[name="unsandboxed"]').check()
        form.locator('[data-cc-action="dot-channel-scope-save"]').scroll_into_view_if_needed()
        evidence_sizes(page, tmp_path, 'connection-global-explicit-confirmation', engine)
        form.locator('[data-cc-action="dot-channel-scope-save"]').click()
        expect(page.locator('.cc-channel-binding').first).to_contain_text('版本 2')
        assert len(shared_writes) == 1 and shared_writes[0][0] == 'room-scope'
        sent = shared_writes[0][1]
        assert sent['grant_id'] == grant and sent['expected_version'] == initial['version']
        assert sent['project_rules'] == rules and sent['expires_seconds'] == 7200
        assert sent['confirm_connection_wide_ceiling'] is True
        assert sent['conversation_id'] == before['id']

        page.once('dialog', lambda dialog: dialog.dismiss())
        page.locator('[data-cc-action="dot-channel-scope-revoke"]').click()
        assert len(shared_writes) == 1
        expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy', 'true')
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response('**/api/collaboration/room-scope-control') as revoked:
            page.locator('[data-cc-action="dot-channel-scope-revoke"]').click()
        assert revoked.value.ok, revoked.value.text()
        expect(page.locator('.cc-channel-binding').first).to_contain_text('已撤销')
        assert len(shared_writes) == 2 and shared_writes[-1][0] == 'room-scope-control'
        assert shared_writes[-1][1]['grant_id'] == grant
        assert shared_writes[-1][1]['expected_version'] == 2
        current = stack.must(stack.client.get('/api/collaboration/room-scopes', params={
            **scope(stack), 'conversation_id': before['id']}))['items'][0]
        assert current['state'] == 'revoked'
        ready = stack.must(stack.client.post('/api/collaboration/conversation-archive-preview',
                                            json={'conversation_id': before['id']}))
        assert ready['ready'] is True and ready['dependent_connections'] == []
        assert overview(stack)['conversation'] == before
        assert not room_writes
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_permission_preview_root_overlap_blocks_apply_without_adding_project(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    applies, previews = [], []
    page.on('request', lambda r: applies.append(r.url)
            if r.method == 'POST' and r.url.rsplit('/', 1)[-1] in
            {'conversation-projects', 'room-scope', 'room-scope-control'} else None)
    page.on('response', lambda response: previews.append(response.json())
            if response.url.endswith('/api/collaboration/conversation-projects-preview') else None)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成目录核对助手'))
        before = overview(stack)['conversation']
        nested = stack.create_project({
            'alias': 'SyntheticNestedProject', 'root': str(stack.projectalpha / 'src'),
            'device_id': stack.device, 'description': 'Disposable overlapping mapping',
            'mode': 'write', 'allow_tasks': True,
        })
        page.reload()
        group = advanced_connections(page)
        group.locator('[data-cc-action="manage-room-permissions"]').click()
        form = page.locator('#cc-room-projects')
        expect(form).to_have_attribute('data-permissions-only', 'true')
        expect(form).to_have_attribute('data-preview-state', 'blocked')
        expect(form.locator('[data-room-project-summary]')).to_contain_text('包含未选择的项目目录')
        expect(form.locator('[data-room-project-summary]')).to_contain_text(stack.project['alias'])
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(form.locator('[name="room_projects"]')).to_have_count(1)
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(project_checkbox(form, nested)).to_have_count(0)
        assert any(item.get('reason_code') == 'ROOM_PROJECT_ROOT_OVERLAP' for item in previews)
        evidence_sizes(page, tmp_path, 'room-root-overlap-blocked', engine)
        # A second explicit read preserves the same selection and cannot apply.
        page.keyboard.press('Escape')
        group = advanced_connections(page)
        group.locator('[data-cc-action="manage-room-permissions"]').click()
        expect(form).to_have_attribute('data-preview-state', 'blocked')
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        assert not applies
        after = overview(stack)['conversation']
        assert after['version'] == before['version'] and after['projects'] == before['projects']
        assert after == before
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [390, 1440])
def test_project_picker_preview_response_during_pointer_preserves_click(
        collaboration_stack, chat_browser_pool, tmp_path, engine, width):
    """A real preview may finish between pointerdown and pointerup."""
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 844})
    page = context.new_page()
    held, holding = [], [True]

    def hold_preview(route):
        if holding[0]:
            held.append((route, route.fetch()))
            page.locator("#cc-room-projects").evaluate("form => form.dataset.testPreviewHeld = 'true'")
        else:
            route.continue_()

    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成预览时序助手'))
        form = manage_from_connections(page)
        second = stack.projects[1]
        project_checkbox(form, stack.project).uncheck()
        project_checkbox(form, second).check()
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        form = manage_from_connections(page)
        form.locator('[data-project-search]').fill(stack.project['alias'])
        expect(form).to_have_attribute('data-preview-state', 'ready')
        page.route('**/api/collaboration/conversation-projects-preview', hold_preview)
        form.locator('[data-project-bulk="all"]').click()
        expect(project_checkbox(form, stack.project)).to_be_checked()
        expect(form).to_have_attribute('data-preview-state', 'checking')
        expect(form).to_have_attribute('data-test-preview-held', 'true')
        assert held, 'The current all-selection preview must be held.'
        # Exercise the scroll container before returning to the operation toolbar.
        form.locator('[data-cc-action="close-drawer"]').scroll_into_view_if_needed()
        button = form.locator('[data-project-bulk="none"]')
        button.click(trial=True)
        box = button.bounding_box()
        assert box
        page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.mouse.down()
        holding[0] = False
        for route, response in list(held):
            route.fulfill(response=response)
        expect(form).to_have_attribute('data-preview-state', 'ready')
        after = button.bounding_box()
        assert after and abs(after['y'] - box['y']) < 2, (box, after)
        page.mouse.up()
        expect(project_checkbox(form, stack.project)).not_to_be_checked()
        expect(project_checkbox(form, second)).to_be_checked()
        # Keep native keyboard semantics as well as pointer release semantics.
        form.locator('[data-project-bulk="all"]').focus()
        page.keyboard.press('Enter')
        expect(project_checkbox(form, stack.project)).to_be_checked()
        button.focus()
        page.keyboard.press('Space')
        expect(project_checkbox(form, stack.project)).not_to_be_checked()
        expect(project_checkbox(form, second)).to_be_checked()
        expect(form.locator('[data-project-selection-count]')).to_contain_text('1 项在搜索结果外')
        expect(form).to_have_attribute('data-preview-state', 'ready')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        evidence(page, tmp_path, f'room-project-stable-preview-{engine}-{width}.png')
    finally:
        context.close()
