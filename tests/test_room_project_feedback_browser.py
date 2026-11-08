"""Project assignment feedback in real browsers with an isolated Hub."""
import json

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack, key  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def open_project_editor(page, stack):
    # Room creation is not the behavior under test. Seed it before mounting
    # the browser so its asynchronous creation/selection cannot overlap the
    # project editor; the save, SSE, rejection and interruption paths stay real.
    room = stack.must(stack.client.post('/api/collaboration/rooms', json={
        'title': 'Project feedback fixture', 'idempotency_key': key(),
    }))['room']
    stack.must(stack.client.put(f'/api/collaboration/rooms/{room["id"]}/permissions', json={
        'expected_version': 0,
        'scope': {'project_scopes': [{'project_id': stack.projects[0]['id']}]},
        'channels': {name: {'mode': 'follow_room', 'scope': {'project_scopes': []}}
                     for name in ('weixin', 'telegram')}, 'idempotency_key': key(),
    }))
    page.goto(stack.url + '/#collaboration')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('.cr-shell')).to_be_visible()
    expect(page.locator('[data-cr-title]')).to_have_text('Project feedback fixture')
    expect(page.locator('[data-cr-project-count]')).to_have_text('项目 1')
    page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
    form = page.locator('[data-cr-form="permissions"]')
    try:
        expect(form).to_be_visible()
        form.locator(f'[data-permission-section="room"] [data-project-id="{stack.projects[1]["id"]}"] [data-permission-project]').check()
    except Exception:
        # Failure-only shape diagnostics, never request bodies or credentials.
        try:
            print('PROJECT_EDITOR_SETUP', json.dumps(page.evaluate("""() => ({
                shellCount: document.querySelectorAll('.cr-shell').length,
                dialogOpen: document.querySelector('dialog.cr-panel')?.open ?? false,
                formKinds: [...document.querySelectorAll('[data-cr-form]')].map(n => n.dataset.crForm),
                projectButtons: [...document.querySelectorAll('.cr-room-actions [data-cr-action="permissions"]')]
                    .map(n => ({disabled: n.disabled, connected: n.isConnected})),
                fixtureSelected: document.querySelector('[data-cr-title]')?.textContent === 'Project feedback fixture',
                collaborationPage: typeof S !== 'undefined' && S.page === 'collaboration'
            })"""), sort_keys=True))
        except Exception:
            print('PROJECT_EDITOR_SETUP unavailable')
        raise
    expect(page.locator('[data-cr-permissions-view]')).to_contain_text('本房间唯一的项目权限设置')
    expect(form).to_contain_text('项目 / 宿主的安全上限不能在这里突破')
    return form


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_project_rejection_visible_and_selection_retained(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 400})
    page = context.new_page()
    page.set_default_timeout(10000)
    writes = []
    try:
        form = open_project_editor(page, stack)
        def reject(route):
            if route.request.method != 'PUT':
                route.continue_()
                return
            writes.append(route.request.post_data_json)
            route.fulfill(status=403, content_type='application/json', body=json.dumps({
                'error': {'code': 'OWNER_REQUIRED', 'message': '需要空间管理员确认'}}))
        page.route('**/api/collaboration/rooms/*/permissions', reject)
        form.locator('button[type="submit"]').click()
        error = page.locator('[data-cr-panel-error]')
        expect(error).to_contain_text('需要空间管理员确认')
        expect(error).to_be_in_viewport()
        expect(form.locator(f'[data-permission-section="room"] [data-project-id="{stack.projects[1]["id"]}"] [data-permission-project]')).to_be_checked()
        assert len(writes) == 1
        page.unroute('**/api/collaboration/rooms/*/permissions', reject)
        with page.expect_request(lambda request: request.method == 'PUT' and request.url.endswith('/permissions')) as success:
            form.locator('button[type="submit"]').click()
        payload = success.value.post_data_json
        assert {item['project_id'] for item in payload['scope']['project_scopes']} == {p['id'] for p in stack.projects[:2]}
        assert 'member_approvals' not in payload
        expect(page.locator('[data-cr-feedback]')).to_contain_text('权限设置已保存')
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 2')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        from urllib.parse import urlsplit
        saved = stack.must(stack.client.get(urlsplit(success.value.url).path))
        assert saved['version'] == payload['expected_version'] + 1
        assert {item['project_id'] for item in saved['scope']['project_scopes']} == {
            project['id'] for project in stack.projects[:2]}
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_project_preflight_error_is_visible_without_request(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    writes = []
    try:
        form = open_project_editor(page, stack)
        page.on('request', lambda request: writes.append(request.url)
                if request.method == 'PUT' and request.url.endswith('/permissions') else None)
        # Isolate a client-side form serialization failure before mutation()
        # has installed its HTTP error handler.
        page.evaluate("""() => {
            const Native = window.FormData;
            window.FormData = class extends Native {
                constructor(form, ...args) {
                    if (form?.dataset.crForm === 'permissions') throw new Error('无法读取项目选择');
                    super(form, ...args);
                }
            };
        }""")
        form.locator('button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('无法读取项目选择')
        assert writes == []
        expect(form.locator(f'[data-permission-section="room"] [data-project-id="{stack.projects[1]["id"]}"] [data-permission-project]')).to_be_checked()
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('interruption', ['none', 'new_panel', 'new_room'])
def test_project_saved_receipt_survives_access_invalidation(
        collaboration_stack, chat_browser_pool, engine, interruption):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1100, 'height': 800})
    page = context.new_page()
    page.set_default_timeout(10000)
    writes, held, saved_room_path, errors = [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
        const Native = window.EventSource;
        window.accessChanges = 0;
        window.EventSource = class extends Native {
            constructor(...args) {
                super(...args);
                this.addEventListener('access_changed', () => window.accessChanges++);
            }
        };
    })();""")
    try:
        form = open_project_editor(page, stack)
        access_before = page.evaluate('window.accessChanges')
        def hold_save(route):
            from urllib.parse import urlsplit
            path = urlsplit(route.request.url).path
            if route.request.method == 'PUT' and path.endswith('/permissions'):
                writes.append(route.request.post_data_json)
                saved_room_path.append(path.removesuffix('/permissions'))
                response = route.fetch()
                assert response.ok, response.text()
                # Keep the successful response separate from the real SSE.
                # Drive UI actions below, outside this route callback.
                held.append((route, response))
            elif route.request.method == 'GET' and path in saved_room_path:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({
                    'error': {'code': 'TEST_REFRESH_FAILED', 'message': '暂时无法刷新房间'}}))
            else:
                route.continue_()
        page.route('**/api/collaboration/rooms/**', hold_save)
        form.locator('button[type="submit"]').evaluate('(button) => {button.click(); button.click();}')
        page.wait_for_function('(before) => window.accessChanges > before', arg=access_before)
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        assert len(held) == len(writes) == 1
        if interruption != 'none':
            page.locator('[data-cr-action="create"]:visible').first.click()
            expect(page.locator('[data-cr-form="create"]')).to_be_visible()
            if interruption == 'new_panel':
                page.locator('[data-cr-action="close"]').click()
            else:
                page.locator('[data-cr-form="create"] [name="title"]').fill('Other room fixture')
                page.locator('[data-cr-form="create"] button[type="submit"]').click()
                expect(page.locator('[data-cr-title]')).to_have_text('Other room fixture')
        with page.expect_response(lambda response: response.request.method == 'PUT'
                                  and response.url.endswith('/permissions')):
            held[0][0].fulfill(response=held[0][1])
        page.evaluate('() => new Promise(requestAnimationFrame)')
        if interruption == 'none':
            expect(page.locator('[data-cr-feedback]')).to_contain_text('权限设置已保存')
        else:
            expect(page.locator('[data-cr-feedback]')).not_to_contain_text('权限设置已保存')
        assert len(writes) == 1
        stored = stack.must(stack.client.get(saved_room_path[0]))
        assert len(stored['room']['projects']) == 2
        # The PUT success fact must not restore the cleared, unverified room.
        expect(page.locator('[data-cr-project-count]')).not_to_have_text('项目 2')
        page.unroute('**/api/collaboration/rooms/**', hold_save)
        page.reload()
        expect(page.locator('.cr-shell')).to_be_visible()
        if interruption == 'new_room':
            expect(page.locator('[data-cr-title]')).to_have_text('Other room fixture')
        else:
            expect(page.locator('[data-cr-project-count]')).to_have_text('项目 2')
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_project_saved_receipt_reports_read_failure_without_access_hint(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1100, 'height': 800})
    page = context.new_page()
    # Model a delayed/lost room permission hint, without breaking unrelated
    # application EventSource connections during sign-in.
    page.add_init_script("""(() => {
        const Native = window.EventSource;
        window.EventSource = class extends Native {
            constructor(url, ...args) {
                super(url, ...args);
                this.isFixtureRoom = String(url).includes('/api/collaboration/rooms/');
            }
            addEventListener(type, ...args) {
                if (this.isFixtureRoom && type === 'access_changed') return;
                return super.addEventListener(type, ...args);
            }
        };
    })();""")
    writes, saved_room_path, read_attempts = [], [], []
    try:
        form = open_project_editor(page, stack)
        def fail_refresh(route):
            from urllib.parse import urlsplit
            path = urlsplit(route.request.url).path
            if route.request.method == 'PUT' and path.endswith('/permissions'):
                writes.append(route.request.post_data_json)
                saved_room_path.append(path.removesuffix('/permissions'))
                response = route.fetch()
                assert response.ok, response.text()
                route.fulfill(response=response)
            elif route.request.method == 'GET' and path in saved_room_path:
                read_attempts.append(path)
                route.fulfill(status=503, content_type='application/json', body=json.dumps({
                    'error': {'code': 'TEST_REFRESH_FAILED', 'message': '暂时无法刷新房间'}}))
            else:
                route.continue_()
        page.route('**/api/collaboration/rooms/**', fail_refresh)
        form.locator('button[type="submit"]').evaluate('(button) => {button.click(); button.click();}')
        expect(page.locator('dialog.cr-panel')).to_be_visible()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('权限设置已保存')
        # api() retains four safe-read delays: 500 + 1000 + 2000 + 4000 ms.
        # Await that real bounded retry budget instead of shortening timers.
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('刷新失败', timeout=12000)
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('权限设置已保存')
        assert len(read_attempts) >= 5
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 1')
        assert len(writes) == 1
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('interruption', ['close', 'new_panel', 'new_room'])
def test_late_project_rejection_does_not_replace_new_view_feedback(
        collaboration_stack, chat_browser_pool, engine, interruption):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1100, 'height': 800})
    page = context.new_page()
    page.set_default_timeout(10000)
    held, writes, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        form = open_project_editor(page, stack)
        def hold_rejection(route):
            if route.request.method == 'PUT':
                writes.append(route.request.post_data_json)
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/collaboration/rooms/*/permissions', hold_rejection)
        with page.expect_request(lambda request: request.method == 'PUT'
                                 and request.url.endswith('/permissions')):
            form.locator('button[type="submit"]').click()
        expect(form).to_have_attribute('aria-busy', 'true')
        page.locator('[data-cr-action="close"]').click()
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        if interruption != 'close':
            page.locator('[data-cr-action="create"]:visible').first.click()
            expect(page.locator('[data-cr-form="create"]')).to_be_visible()
            page.locator('[data-cr-form="create"] [name="title"]').fill('Keep this newer draft')
            if interruption == 'new_room':
                page.locator('[data-cr-form="create"] button[type="submit"]').click()
                expect(page.locator('[data-cr-title]')).to_have_text('Keep this newer draft')
        assert len(held) == len(writes) == 1
        with page.expect_response(lambda response: response.request.method == 'PUT'
                                  and response.url.endswith('/permissions')):
            held[0].fulfill(status=403, content_type='application/json', body=json.dumps({
                'error': {'code': 'OWNER_REQUIRED', 'message': 'OLD_PROJECT_REJECTION'}}))
        page.evaluate('() => new Promise(requestAnimationFrame)')
        expect(page.locator('[data-cr-feedback]')).not_to_contain_text('OLD_PROJECT_REJECTION')
        expect(page.locator('[data-cr-panel-error]')).not_to_contain_text('OLD_PROJECT_REJECTION')
        if interruption == 'new_panel':
            expect(page.locator('[data-cr-form="create"] [name="title"]')).to_have_value(
                'Keep this newer draft')
        assert len(writes) == 1
        assert errors == []
    finally:
        context.close()
