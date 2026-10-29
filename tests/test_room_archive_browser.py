"""Recoverable room deletion in real browsers and disposable Hub fixtures."""
import pytest
from playwright.sync_api import expect
from tests.collaboration_support import collaboration_stack  # noqa: F401
from tests.test_collaboration_chatroom_browser import login, overview, click_room_refresh
from tests.test_collaboration_dots_flow import create_dot, join_dot
from tests.test_room_projects_browser import evidence_sizes, manage_from_connections, project_checkbox, save
pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def archive_preview(page):
    page.locator('[data-cc-more]').click()
    page.locator('.cc-room-menu [data-cc-action="archive-room"]').click()
    form = page.locator('#cc-archive-room')
    expect(form).to_be_visible()
    return form


def _mark_archive_route_count(page, pending_routes):
    page.locator('html').evaluate(
        '(node, count) => node.dataset.archiveRoutesPending = String(count)', len(pending_routes))


def _drain_restored_history_routes(page, pending_routes):
    # unroute_all(wait) removes interception before waiting for callbacks. A
    # live refresh can then auto-continue a request whose fixture still needs
    # to fulfill it. Leave the view normally and drain while routes still own
    # their requests; preserve callback errors instead of ignoring them.
    _mark_archive_route_count(page, pending_routes)
    page.evaluate("navigate('projects')")
    expect(page.locator('html')).to_have_attribute('data-archive-routes-pending', '0', timeout=5000)
    assert not pending_routes


def all_rooms(stack):
    return stack.must(stack.client.get('/api/collaboration/conversations',
                                      params={'include_archived': 'true'}))['items']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_archive_cancel_conflict_double_click_and_restore(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    writes, errors = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/conversation-archive') else None)
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成归档助手'))
        projects_form = manage_from_connections(page)
        project_checkbox(projects_form, stack.projects[1]).check()
        save(projects_form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        page.locator('[data-cc-workspace-view="discussion"]').first.click()
        area = page.locator('#cc-message-input')
        area.fill('删除后仍保留的历史')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        area.fill('取消删除必须保留的草稿')
        before = overview(stack)['conversation']
        archive_preview(page)
        expect(page.locator('#cc-drawer')).to_contain_text('共享账号凭据')
        expect(page.locator('#cc-drawer')).to_contain_text('审计')
        for label in ('进行中的操作', '监控计划', '旧版用途绑定', '待批准提案'):
            expect(page.locator('.cc-archive-impact')).to_contain_text(label)
        evidence_sizes(page, tmp_path, 'room-archive-impact', engine)
        page.keyboard.press('Escape')
        expect(area).to_have_value('取消删除必须保留的草稿')
        assert not writes
        assert overview(stack)['conversation']['version'] == before['version']

        def conflict(route):
            route.fulfill(status=409, json={'error': {'code': 'STALE_VERSION', 'message': '合成并发更新'}})
        page.route('**/api/collaboration/conversation-archive', conflict)
        form = archive_preview(page)
        form.locator('button[type="submit"]').click()
        expect(form.locator('[data-archive-error]')).to_contain_text('重新核对')
        assert not next(r for r in all_rooms(stack) if r['id'] == before['id']).get('archived_at')
        page.keyboard.press('Escape')
        page.unroute('**/api/collaboration/conversation-archive', conflict)

        form = archive_preview(page)
        form.locator('button[type="submit"]').evaluate('(b) => { b.click(); b.click(); }')
        expect(page.locator('.cc-archived-empty')).to_be_visible()
        evidence_sizes(page, tmp_path, 'room-archive-complete', engine)
        assert len(writes) == 2
        assert next(r for r in all_rooms(stack) if r['id'] == before['id'])['archived_at']
        projects = stack.must(stack.client.get('/api/projects'))['projects']
        assert {p['id'] for p in stack.projects} <= {p['id'] for p in projects}
        page.reload()
        expect(page.locator('.cc-archived-empty')).to_be_visible()
        page.locator('[data-cc-action="archived-rooms"]').click()
        card = page.locator('.cc-archived-room').filter(has_text=stack.project['alias'])
        expect(card).to_be_visible()
        card.locator('[data-cc-action="restore-room"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('旧连接、权限、订阅和任务保持关闭')
        page.locator('#cc-restore-room [data-cc-action="archived-rooms"]').click()
        assert next(r for r in all_rooms(stack) if r['id'] == before['id'])['archived_at']
        card.locator('[data-cc-action="restore-room"]').click()
        page.locator('#cc-restore-room button[type="submit"]').click()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-conversation', before['id'])
        expect(page.locator('.cc-feed')).to_contain_text('删除后仍保留的历史')
        restored = next(r for r in all_rooms(stack) if r['id'] == before['id'])
        assert not restored.get('archived_at')
        assert restored['restored_from_archive']
        assert restored['projects'] == []
        assert len(restored['history_projects']) == 2
        expect(page.locator('.cc-restored-notice')).to_contain_text('执行权限未恢复')
        expect(page.locator('.cc-project-count')).to_have_text('2 个历史项目')
        expect(page.locator('[data-cc-view="agents"]')).not_to_be_visible()
        expect(page.locator('[data-cc-action="convert"]')).not_to_be_visible()
        page.locator('[data-cc-action="view-projects"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('历史关联，无执行授权')
        expect(page.locator('#cc-drawer .cc-project-review li')).to_have_count(2)
        for project in stack.projects[:2]:
            expect(page.locator('#cc-drawer')).to_contain_text(project['alias'])
        page.keyboard.press('Escape')
        page.set_viewport_size({'width': 1440, 'height': 900})
        evidence_sizes(page, tmp_path, 'room-history-restored', engine)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_archive_late_preview_and_list_do_not_undo_navigation(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    held = []
    try:
        login(page, stack)
        join_dot(stack, create_dot(stack))
        original = overview(stack)['conversation']['id']
        def delayed_preview(route):
            held.append((route, route.fetch()))
            page.locator('html').evaluate('(n) => n.dataset.heldArchive = "ready"')
        page.route('**/api/collaboration/conversation-archive-preview', delayed_preview)
        page.locator('[data-cc-more]').click()
        page.locator('.cc-room-menu [data-cc-action="archive-room"]').click()
        expect(page.locator('html')).to_have_attribute('data-held-archive', 'ready')
        page.keyboard.press('Escape')
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert not next(r for r in all_rooms(stack) if r['id'] == original).get('archived_at')
        page.unroute('**/api/collaboration/conversation-archive-preview', delayed_preview)
        form = archive_preview(page)
        def delayed_list(route):
            held.append((route, route.fetch()))
            page.locator('html').evaluate('(n) => n.dataset.heldRoomList = "ready"')
        page.route('**/api/collaboration/conversations', delayed_list)
        form.locator('button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-held-room-list', 'ready')
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        route, response = held.pop()
        route.fulfill(response=response)
        expect(page.locator('#page h1')).to_have_text('项目映射')
        assert next(r for r in all_rooms(stack) if r['id'] == original)['archived_at']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_archive_dependency_lists_exact_connection_and_never_submits(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key
    from tests.test_collaboration_delivery_flow import scope
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    writes = []
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.url.endswith('/api/collaboration/conversation-archive') else None)
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack))
        room = overview(stack)['conversation']
        preview = stack.must(stack.client.post('/api/collaboration/conversation-projects-preview', json={
            'conversation_id': room['id'], 'dot_id': joined['slot']['id'],
            'projects': [{'project': stack.project['id'], 'environment_id': 'production'}]}))
        grant_id = preview['connection']['grant_id']
        stack.must(stack.client.post('/api/collaboration/room-scope', json={
            **scope(stack), 'conversation_id': room['id'], 'grant_id': grant_id,
            'expected_version': 0, 'project_rules': [{'project_id': stack.project['id'],
                'capabilities': ['read'], 'execution_targets': ['agent']}],
            'execution_mode': 'file_tools_only', 'acknowledge_unsandboxed_exec': False,
            'confirm_connection_wide_ceiling': True, 'expires_seconds': 3600, 'idempotency_key': key()}))
        blocked = stack.must(stack.client.post('/api/collaboration/conversation-archive-preview',
                                               json={'conversation_id': room['id']}))
        assert blocked['ready'] is False and blocked['dependent_connections']
        page.locator('[data-cc-more]').click()
        page.locator('.cc-room-menu [data-cc-action="archive-room"]').click()
        expect(page.locator('.cc-archive-dependencies')).to_contain_text(grant_id)
        for item in blocked['dependent_connections']:
            expect(page.locator('.cc-archive-dependencies')).to_contain_text(item['label'])
        expect(page.locator('#cc-archive-room button[type="submit"]')).to_have_count(0)
        page.keyboard.press('Escape')
        assert not writes
        assert not next(r for r in all_rooms(stack) if r['id'] == room['id']).get('archived_at')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_late_archived_overview_does_not_clear_new_room_selection(
        collaboration_stack, chat_browser_pool, engine):
    from urllib.parse import parse_qs, urlsplit
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    held = []
    try:
        login(page, stack)
        def delayed(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind', ['overview']) == ['overview'] and query.get('project') == [stack.project['id']]:
                held.append(route)
                page.locator('html').evaluate('(n) => n.dataset.heldArchivedOverview = "ready"')
            else:
                route.continue_()
        page.route('**/api/collaboration?*', delayed)
        click_room_refresh(page)
        expect(page.locator('html')).to_have_attribute('data-held-archived-overview', 'ready')
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        other = stack.projects[1]
        page.locator(f'[data-action="project-collaboration"][data-id="{other["id"]}"]').click()
        page.locator('[data-cc-action="create-room"]').click()
        expect(page.locator('#cc-message-input')).to_be_enabled()
        selected = page.locator('.cc-chat-shell').get_attribute('data-conversation')
        assert selected
        for route in held:
            route.fulfill(status=410, json={'error': {'code': 'CONVERSATION_ARCHIVED', 'message': '旧房间已删除'}})
        held.clear()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-conversation', selected)
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', other['id'])
        expect(page.locator('#cc-message-input')).to_be_enabled()
        saved = page.evaluate("JSON.parse(sessionStorage.getItem('codepier-collaboration-selection'))")
        assert saved['conversation'] == selected and saved['project'] == other['id']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('empty_history', [False, True])
def test_restore_uses_only_visible_history_project_for_navigation(
        collaboration_stack, chat_browser_pool, tmp_path, engine, empty_history, hide_directory=False):
    from urllib.parse import parse_qs, urlsplit
    from tests.collaboration_support import key
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    other = stack.projects[1]
    pending_routes = set()

    def tracked(handler):
        def handle(route):
            pending_routes.add(route)
            _mark_archive_route_count(page, pending_routes)
            try:
                return handler(route)
            finally:
                pending_routes.remove(route)
                _mark_archive_route_count(page, pending_routes)
        return handle

    try:
        login(page, stack)
        join_dot(stack, create_dot(stack, '合成历史助手'))
        form = manage_from_connections(page)
        project_checkbox(form, other).check()
        save(form)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        conversation = overview(stack)['conversation']
        other_scope = {'project': other['id'], 'environment_id': 'production',
                       'conversation_id': conversation['id']}
        other_room = stack.must(stack.client.get('/api/collaboration', params=other_scope))['room']
        if not empty_history:
            stack.must(stack.client.post('/api/collaboration/message', json={
                **other_scope, 'room_id': other_room['id'], 'body_text': '仍可见项目的恢复历史',
                'client_message_id': key(), 'idempotency_key': key(), 'mentions': []}))
        archive_preview(page).locator('button[type="submit"]').click()
        expect(page.locator('.cc-archived-empty')).to_be_visible()

        def restrict_room(room):
            if room.get('id') != conversation['id']:
                return room
            room['projects'] = [p for p in room.get('projects', []) if p['project_id'] == other['id']]
            if 'history_projects' in room:
                room['history_projects'] = [p for p in room['history_projects'] if p['project_id'] == other['id']]
            room.pop('connection', None)
            if room.get('working_scope'):
                room['working_scope'].pop('dot_id', None)
            return room

        def restricted_directory(route):
            response = route.fetch()
            body = response.json()
            body['items'] = [restrict_room(room) for room in body['items']
                             if room['id'] == conversation['id']]
            if hide_directory and parse_qs(urlsplit(route.request.url).query).get('include_archived') != ['true']:
                body['items'] = []
            route.fulfill(response=response, json=body)

        def restricted_restore(route):
            response = route.fetch()
            body = response.json()
            # The apply response is older than the directory read: it still has A/B.
            assert body['conversation']['projects'] == []
            assert len(body['conversation']['history_projects']) == 2
            route.fulfill(response=response, json=body)

        historical = []
        goal = {'id': 'd' * 32, 'version': 1, 'state': 'active',
                'objective': '恢复前的合成目标', 'acceptance': '仅保留历史证据',
                'project_ids': [other['id']], 'participant_grant_ids': [],
                'conversation_id': conversation['id'], 'anchor_project_id': other['id'],
                'environment_id': 'production', 'can_approve': True}
        def restricted_history(route):
            query = parse_qs(urlsplit(route.request.url).query)
            kind = query.get('kind', ['overview'])[0]
            if kind == 'coordination_goals' and not empty_history:
                route.fulfill(json={'items': [goal], 'next_cursor': None})
                return
            if kind == 'coordination_goal' and query.get('id') == [goal['id']]:
                route.fulfill(json={'goal': goal, 'work_items': [], 'operations': [], 'messages': []})
                return
            if kind == 'message_status' and historical:
                item = next((m for m in historical if m['id'] == query.get('id', [''])[0]), None)
                if item:
                    route.fulfill(json={'message': item})
                    return
            if query.get('project') == [stack.project['id']]:
                route.fulfill(status=403, json={'error': {'code': 'PERMISSION_DENIED', 'message': '原项目访问已撤回'}})
                return
            response = route.fetch()
            body = response.json()
            if body.get('conversation'):
                body['conversation'] = restrict_room(body['conversation'])
                if kind == 'overview' and not empty_history:
                    body['capabilities']['coordination_goals'] = True
                    body['can_manage'] = True
                    body['plan'] = {'id': 'f' * 32, 'version': 1, 'status': 'active', 'state': 'active',
                        'active_version': 1, 'latest_version': 2, 'rules': [],
                        'latest': {'version': 2, 'digest': 'f' * 64, 'config': {'rules': []}}}
            if kind == 'timeline':
                body['items'] = [m for m in body['items'] if m['project_id'] == other['id']]
                if body['items'] and not empty_history:
                    from copy import deepcopy
                    original = body['items'][0]
                    historical.clear()
                    for index, status in enumerate(['plain', 'blocked', 'dispatch_required', 'relay']):
                        item = deepcopy(original)
                        item['id'] = str(index + 1) * 32
                        item['server_sequence'] = index + 1
                        item['body'] = {'text': original.get('body_text', '仍可见项目的恢复历史')}
                        if status == 'plain':
                            item['state'] = 'awaiting_approval'
                            item['mentions'] = [{'slot_id': 'e' * 32, 'display_snapshot': '合成历史助手'}]
                            item['body']['notifications'] = [{'slot_id': 'e' * 32, 'state': 'not_subscribed'}]
                        elif status in ('blocked', 'dispatch_required'):
                            item['body']['delegation'] = {'goal_id': goal['id'], 'delegation_id': str(index + 4) * 32,
                                'delivery_status': status, 'retry_blocked_eligible': True}
                        else:
                            item['sender_dot_id'] = 'e' * 32
                            item['relay_receipts'] = [{'dot_id': 'e' * 32, 'state': 'waiting_authorization',
                                'experience': {'read_only': True, 'primary_state': 'waiting_authorization',
                                    'next_action': {'kind': 'open_host_confirmation', 'mutates': False}}}]
                        historical.append(item)
                    body['items'] = historical[:]
            route.fulfill(response=response, json=body)

        page.route('**/api/collaboration/conversations*', tracked(restricted_directory))
        page.route('**/api/collaboration/conversation-restore', tracked(restricted_restore))
        page.route('**/api/collaboration?*', tracked(restricted_history))
        page.locator('[data-cc-action="archived-rooms"]').click()
        page.locator(f'[data-cc-action="restore-room"][data-id="{conversation["id"]}"]').click()
        page.locator('#cc-restore-room button[type="submit"]').click()
        if hide_directory:
            expect(page.locator('.cc-history-unavailable')).to_be_visible()
            expect(page.locator('#cc-message-input')).to_have_count(0)
            expect(page.locator('.collaboration')).not_to_contain_text(stack.project['alias'])
            expect(page.locator('.collaboration')).not_to_contain_text(other['alias'])
            saved = page.evaluate("JSON.parse(sessionStorage.getItem('codepier-collaboration-selection'))")
            assert saved['conversation'] == '' and saved['project'] == '' and saved['environment'] == ''
            evidence_sizes(page, tmp_path, 'room-history-no-visible-project', engine)
            hide_directory = False
            page.locator('[data-cc-action="available-rooms"]').click()
            available = page.locator(f'#cc-drawer [data-cc-room="{conversation["id"]}"]')
            expect(available).to_have_attribute('data-project', other['id'])
            available.click()
            expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', other['id'])
            expect(page.locator('.cc-project-count')).to_have_text('1 个历史项目')
            return
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', other['id'])
        expect(page.locator('.cc-room-title h2')).to_have_text('聊天室')
        expect(page.locator('.cc-room-subline > span')).to_have_text('1 个历史项目')
        expect(page.locator('.cc-project-count')).to_have_text('1 个历史项目')
        expect(page.locator('.cc-feed')).to_contain_text('当前可访问的项目范围内暂无历史消息' if empty_history else '仍可见项目的恢复历史')
        expect(page.locator('.cc-room-body')).not_to_contain_text(stack.project['alias'])
        expect(page.locator('#cc-command')).not_to_be_visible()
        expect(page.locator('#cc-workspace-panel')).not_to_be_visible()
        expect(page.locator('[data-cc-panel="agents"]')).not_to_be_visible()
        expect(page.locator('[data-cc-action="open-members"]:visible')).to_have_count(0)
        expect(page.locator('[data-cc-action="dot-connect"]:visible')).to_have_count(0)
        expect(page.locator('.cc-feed')).not_to_contain_text('添加第一个 dot')
        evidence_sizes(page, tmp_path, 'room-history-empty' if empty_history else 'room-history-partial-access', engine)
        page.locator('[data-cc-action="view-projects"]').click()
        expect(page.locator('#cc-drawer .cc-project-review li')).to_have_count(1)
        expect(page.locator('#cc-drawer')).to_contain_text(other['alias'])
        expect(page.locator('#cc-drawer')).not_to_contain_text(stack.project['alias'])
        if not empty_history:
            evidence_sizes(page, tmp_path, 'room-history-visible-list', engine)
        page.keyboard.press('Escape')
        forbidden = ['goal-new', 'goal-review', 'goal-edit', 'goal-pause', 'goal-cancel-review',
                     'goal-copy-subscription', 'reply', 'convert', 'accept_proposal', 'reject_proposal',
                     'mention-connect', 'message-remind', 'delegation-remind', 'delegation-retry-blocked',
                     'dot-connect', 'open-members', 'activate-plan', 'plan_pause', 'pause', 'resume']
        def no_historical_mutations():
            for action in forbidden:
                expect(page.locator(f'[data-cc-action="{action}"]')).to_have_count(0)
            expect(page.locator('#cc-probe, #cc-plan, #cc-goal-draft, #cc-goal-approve')).to_have_count(0)
        no_historical_mutations()
        page.locator('.cc-workspace-nav [data-cc-view="agents"]').evaluate('(node) => node.click()')
        expect(page.locator('#cc-workspace-panel')).not_to_be_visible()
        if not empty_history:
            expect(page.locator('[data-cc-action="delegation-id"]')).to_have_count(2)
            expect(page.locator('[data-cc-action="relay-inspect"]')).to_have_count(1)
            page.locator('[data-cc-action="relay-inspect"]').click()
            expect(page.locator('.cc-receipt-detail')).to_have_attribute('open', '')
            expect(page.locator('.cc-receipt-detail')).to_contain_text('历史回执')
            page.locator('[data-cc-view="jobs"]').click()
            page.locator(f'[data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
            expect(page.locator('#cc-drawer')).to_contain_text('历史状态与证据')
            no_historical_mutations()
            evidence_sizes(page, tmp_path, 'room-history-goal-evidence', engine)
            page.keyboard.press('Escape')
            page.locator('[data-cc-view="monitor"]').click()
            expect(page.locator('[data-cc-panel="monitor"]')).to_contain_text('监控计划')
            no_historical_mutations()
            evidence_sizes(page, tmp_path, 'room-history-monitor-evidence', engine)
            page.locator('[data-cc-workspace-view="discussion"]').first.click()
        rail = page.locator(f'[data-cc-room="{conversation["id"]}"]')
        expect(rail).to_have_attribute('data-project', other['id'])
        rail.click()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-project', other['id'])
        page.reload()
        expect(page.locator('.cc-feed')).to_contain_text('当前可访问的项目范围内暂无历史消息' if empty_history else '仍可见项目的恢复历史')
        expect(page.locator('.cc-project-count')).to_have_text('1 个历史项目')
        assert next(r for r in all_rooms(stack) if r['id'] == conversation['id'])['projects'] == []
    finally:
        try:
            _drain_restored_history_routes(page, pending_routes)
            page.unroute_all(behavior='wait')
        finally:
            context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_explicit_title_matching_project_text_is_preserved(
        collaboration_stack, chat_browser_pool, engine):
    from tests.collaboration_support import key
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        for title in (stack.project['alias'] + ' 团队讨论', stack.project['id'] + ' · production'):
            room = stack.must(stack.client.post('/api/collaboration/conversation', json={
                'title': title, 'projects': [{'project': stack.projects[1]['id'], 'environment_id': 'production'}],
                'idempotency_key': key()}))['conversation']
            assert room['title_is_generated'] is False
            page.reload()
            page.locator(f'[data-cc-room="{room["id"]}"]').click()
            expect(page.locator('.cc-room-title h2')).to_have_text(title)
            archive_preview(page)
            expect(page.locator('#cc-drawer')).to_contain_text(title)
            page.keyboard.press('Escape')
            expect(page.locator('.cc-room-title h2')).to_have_text(title)
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_restore_latest_directory_without_room_does_not_reopen_old_project(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    test_restore_uses_only_visible_history_project_for_navigation(
        collaboration_stack, chat_browser_pool, tmp_path, engine, empty_history=False, hide_directory=True)


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_restore_route_teardown_drains_pending_refresh_before_unroute(
        collaboration_stack, chat_browser_pool, tmp_path, engine, monkeypatch):
    import re
    import sys
    from playwright.sync_api import Route
    original_fetch = Route.fetch
    original_drain = _drain_restored_history_routes
    holding = False
    overlap_seen = []

    def gated_fetch(route, *args, **kwargs):
        response = original_fetch(route, *args, **kwargs)
        if holding:
            # Deterministically park the original callback, without a sleep or
            # replay, until cleanup performs the normal navigation lifecycle.
            expect(route.request.frame.page.locator('#page h1')).to_have_text('项目映射', timeout=5000)
        return response

    def overlapping_cleanup(page, pending_routes):
        nonlocal holding
        _mark_archive_route_count(page, pending_routes)
        if not page.locator('[data-cc-action="refresh"]').is_visible():
            page.locator('[data-cc-more]').click()
        holding = True
        page.locator('[data-cc-action="refresh"]').click()
        expect(page.locator('html')).to_have_attribute(
            'data-archive-routes-pending', re.compile(r'[1-9]\d*'), timeout=5000)
        assert pending_routes, 'The original refresh must overlap teardown'
        overlap_seen.append(len(pending_routes))
        original_drain(page, pending_routes)
        assert not pending_routes
        expect(page.locator('#page h1')).to_have_text('项目映射')

    monkeypatch.setattr(Route, 'fetch', gated_fetch)
    monkeypatch.setattr(sys.modules[__name__], '_drain_restored_history_routes', overlapping_cleanup)
    test_restore_uses_only_visible_history_project_for_navigation(
        collaboration_stack, chat_browser_pool, tmp_path, engine, empty_history=False, hide_directory=True)
    assert len(overlap_seen) == 1


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('replayed_active', [False, True])
def test_archive_outbox_cancel_and_confirm_only_clear_target_conversation(
        collaboration_stack, chat_browser_pool, engine, replayed_active):
    from tests.collaboration_support import key
    from tests.test_dots_ecosystem_integrated_browser import HOOK
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    context.add_init_script(HOOK)
    page = context.new_page()
    archive_writes = []
    try:
        login(page, stack)
        original = overview(stack)['conversation']
        other = stack.must(stack.client.post('/api/collaboration/conversation', json={
            'title': '另一房间待发记录', 'projects': [{'project': stack.project['id'], 'environment_id': 'production'}],
            'idempotency_key': key()}))['conversation']
        page.evaluate("""([original,other]) => {
          const ctx=ecosystemRelayContext, current=ctx.chat(), otherChat={...current,messages:[],
            visibility:'',initialized:false,relayOutbox:[],relayEpoch:0};
          window.archivedChat=current;window.otherPendingChat=otherChat;
          ctx.state.chats.set(JSON.stringify([S.space_id,other.id]),otherChat);
          const make=(id,room,conversation)=>({id,chat:room,epoch:room.relayEpoch||0,session:S.session,
            space:S.space_id,dotId:'synthetic-dot',phase:'error',attempts:0,due:0,
            payload:{...ctx.chatScope(),room_id:ctx.state.snapshot.room.id,conversation_id:conversation,
              mentions:[{slot_id:'synthetic-dot'}],body_text:id}});
          current.relayOutbox=[make('target-pending',current,original.id)];
          otherChat.relayOutbox=[make('other-pending',otherChat,other.id)];
          const store=CodePierCollaborationRelay.pendingStore(localStorage,JSON.stringify([S.session.user_id,S.space_id]));
          store.enable();
          for(const item of [...current.relayOutbox,...otherChat.relayOutbox])
            store.save({...item,scope:item.payload.conversation_id,created:Date.now(),
              authorization_revision:'a'.repeat(64)});
          window.savedPending=store;
        }""", [original, other])
        archive_preview(page)
        page.keyboard.press('Escape')
        assert page.evaluate('archivedChat.relayOutbox.length') == 1
        assert page.evaluate('otherPendingChat.relayOutbox.length') == 1
        assert page.evaluate('(id)=>savedPending.recover(id).length', original['id']) == 1

        def archive_request(route):
            archive_writes.append(route.request.post_data_json)
            if replayed_active:
                route.fulfill(json={'archived': False, 'replayed': True, 'can_restore': False,
                                    'conversation': {**original, 'archived_at': None}})
            else:
                route.continue_()
        page.route('**/api/collaboration/conversation-archive', archive_request)
        form = archive_preview(page)
        form.locator('button[type="submit"]').evaluate('(node)=>{node.click();node.click()}')
        expected = original['id'] if replayed_active else other['id']
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-conversation', expected)
        expect(page.locator('#cc-command button[type="submit"]')).to_be_enabled()
        assert len(archive_writes) == 1
        assert page.evaluate('archivedChat.relayOutbox.length') == (1 if replayed_active else 0)
        assert page.evaluate('(id)=>savedPending.recover(id).length', original['id']) == (1 if replayed_active else 0)
        assert page.evaluate('otherPendingChat.relayOutbox[0].id') == 'other-pending'
        assert page.evaluate('(id)=>savedPending.recover(id).length', other['id']) == 1
        if replayed_active:
            expect(page.locator('#cc-feedback')).to_contain_text('原删除操作的回执')
            assert not next(room for room in all_rooms(stack) if room['id'] == original['id']).get('archived_at')
        else:
            assert next(room for room in all_rooms(stack) if room['id'] == original['id'])['archived_at']
    finally:
        page.unroute_all(behavior='wait')
        context.close()
