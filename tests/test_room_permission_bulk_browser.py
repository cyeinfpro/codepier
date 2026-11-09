"""Room permission bulk selection against a real isolated Hub and browsers."""
import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack, key  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def open_permissions(page, stack, title):
    room = stack.must(stack.client.post('/api/collaboration/rooms', json={
        'title': title, 'idempotency_key': key()}))['room']
    page.goto(stack.url + '/#collaboration')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('[data-cr-title]')).to_have_text(title)
    page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
    expect(page.locator('[data-cr-form="permissions"]')).to_be_visible()
    return f'/api/collaboration/rooms/{room["id"]}/permissions'


def submit_permissions(page, *, closes=True):
    with page.expect_response(lambda r: r.request.method == 'PUT'
                              and r.url.endswith('/permissions')) as response:
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
    assert response.value.ok, response.value.text()
    expect(page.locator('[data-cr-feedback]')).to_contain_text('权限设置已保存')
    if closes:
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
    else:
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('权限设置已保存')
        expect(page.locator('dialog.cr-panel')).to_be_visible()
    return response.value.request.post_data_json


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_bulk_permissions_select_vps_and_constrain_custom_channels(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    remote = stack.must(stack.client.post('/api/vps', json={
        'name': 'Synthetic room VPS', 'host': 'room-vps.example.invalid',
        'username': 'fixture', 'password': 'synthetic-not-real', 'host_key_policy': 'strict',
        'project_ids': [stack.project['id']]}))['target']
    other_remote = stack.must(stack.client.post('/api/vps', json={
        'name': 'Second synthetic room VPS', 'host': 'second-room-vps.example.invalid',
        'username': 'fixture', 'password': 'synthetic-not-real', 'host_key_policy': 'strict',
        'project_ids': [stack.project['id']]}))['target']
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        path = open_permissions(page, stack, 'VPS bulk permission fixture')
        section = page.locator('[data-permission-section="room"]')
        first = section.locator(f'[data-project-id="{stack.project["id"]}"]')
        section.locator('[data-permission-bulk="all"]').click()
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        expect(first).to_contain_text('Synthetic room VPS')
        expect(first.locator(f'[data-permission-target-option][value="{other_remote}"]')).to_be_checked()
        for channel in ('weixin', 'telegram'):
            page.locator(f'[data-permission-mode="{channel}"]').select_option('custom')
            custom = page.locator(f'[data-permission-section="{channel}"]')
            custom.locator('[data-permission-bulk="all"]').click()
            expect(custom.locator(f'[data-project-id="{stack.project["id"]}"] '
                                  f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        payload = submit_permissions(page)
        for scope in [payload['scope'], *(c['scope'] for c in payload['channels'].values())]:
            rule = next(r for r in scope['project_scopes'] if r['project_id'] == stack.project['id'])
            assert set(rule['execution_targets']) == {'project_agent', remote, other_remote}
        saved = stack.must(stack.client.get(path))
        assert saved['channels']['weixin']['scope'] == saved['scope']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        first.locator(f'[data-permission-target-option][value="{remote}"]').uncheck()
        second = section.locator(f'[data-project-id="{stack.projects[1]["id"]}"]')
        second.locator('[data-permission-project]').uncheck()
        for channel in ('weixin', 'telegram'):
            custom = page.locator(f'[data-permission-section="{channel}"]')
            expect(custom.locator(f'[data-project-id="{stack.project["id"]}"] '
                                  f'[data-permission-target-option][value="{remote}"]')).to_be_disabled()
            expect(custom.locator(f'[data-project-id="{stack.projects[1]["id"]}"] '
                                  '[data-permission-project]')).to_be_disabled()
        payload = submit_permissions(page)
        for scope in [payload['scope'], *(c['scope'] for c in payload['channels'].values())]:
            assert stack.projects[1]['id'] not in scope['project_ids']
            rule = next(r for r in scope['project_scopes'] if r['project_id'] == stack.project['id'])
            assert set(rule['execution_targets']) == {'project_agent', other_remote}
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        section.locator('[data-permission-bulk="none"]').click()
        expect(section.locator('[data-permission-target-option]:checked')).to_have_count(0)
        section.locator('[data-permission-bulk="execute"]').click()
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        # Merely closing never commits the newly expanded selection.
        page.keyboard.press('Escape')
        assert stack.must(stack.client.get(path))['version'] == saved['version'] + 1
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_bulk_projects_saves_twenty_eight_without_contract_error(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    for index in range(3, 28):
        directory = stack.root / f'PermissionProject{index}'
        directory.mkdir()
        stack.projects.append(stack.create_project({
            'alias': directory.name, 'root': str(directory), 'device_id': stack.device,
            'mode': 'read', 'allow_tasks': False}))
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        path = open_permissions(page, stack, 'Twenty eight project permissions')
        section = page.locator('[data-permission-section="room"]')
        section.locator('[data-permission-bulk="projects"]').click()
        expect(section.locator('[data-permission-project]:checked')).to_have_count(28)
        payload = submit_permissions(page)
        assert len(payload['scope']['project_scopes']) == 28
        assert all(rule['capabilities'] == ['read'] for rule in payload['scope']['project_scopes'])
        saved = stack.must(stack.client.get(path))
        assert len(saved['scope']['project_scopes']) == 28
        assert saved['channels']['weixin']['effective_scope'] == saved['effective_scope']
        assert saved['channels']['telegram']['effective_scope'] == saved['effective_scope']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(section.locator('[data-permission-project]:checked')).to_have_count(28)
        submit_permissions(page, closes=False)
        assert stack.must(stack.client.get(path))['version'] == saved['version'] + 1
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('limit_kind', ['projects', 'targets'])
def test_bulk_selection_limit_is_visible_before_any_write(
        collaboration_stack, chat_browser_pool, engine, limit_kind):
    """Only the GET projection is synthetic; no excessive scope reaches the Hub."""
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    writes = []
    page.on('request', lambda request: writes.append(request.url)
            if request.method == 'PUT' and request.url.endswith('/permissions') else None)

    def projection(route):
        response = route.fetch()
        policy = response.json()
        template = policy['available_projects'][0]
        policy['limits'] = {'project_scopes': 128, 'execution_targets': 16}
        if limit_kind == 'projects':
            policy['available_projects'] = [
                {**template, 'project_id': f'synthetic-project-{index}', 'name': f'Synthetic {index}',
                 'capabilities': ['read'], 'execution_targets': []}
                for index in range(129)]
        else:
            policy['available_projects'] = [{
                **template, 'capabilities': ['read', 'execute'],
                'execution_targets': [f'vps:{index:032x}' for index in range(17)]}]
        route.fulfill(response=response, json=policy)

    page.route('**/api/collaboration/rooms/*/permissions', projection)
    try:
        path = open_permissions(page, stack, 'Synthetic bounded permission projection')
        section = page.locator('[data-permission-section="room"]')
        section.locator('[data-permission-bulk="all"]').click()
        for _ in range(2):
            page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
            message = '最多选择 128 个项目与环境' if limit_kind == 'projects' else '最多选择 16 个执行位置'
            expect(page.locator('[data-cr-panel-error]')).to_contain_text(message)
            expect(page.locator('[data-cr-panel-error]')).to_be_in_viewport()
        assert writes == []
        count = 129 if limit_kind == 'projects' else 1
        expect(section.locator('[data-permission-project]:checked')).to_have_count(count)
        assert not stack.must(stack.client.get(path))['configured']
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_project_environment_identity_does_not_collapse_colons(
        collaboration_stack, chat_browser_pool, engine):
    """Distinct accepted project/environment tuples must render distinct scopes."""
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()

    def projection(route):
        response = route.fetch()
        policy = response.json()
        rules = [
            {'project_id': 'a:b', 'environment_id': 'c', 'capabilities': ['read'],
             'execution_targets': [], 'lifetime': {'mode': 'until_revoked', 'expires_at': None}},
            {'project_id': 'a', 'environment_id': 'b:c', 'capabilities': ['read', 'write'],
             'execution_targets': [], 'lifetime': {'mode': 'until_revoked', 'expires_at': None}},
        ]
        policy['scope'] = {'project_ids': ['a:b', 'a'], 'project_scopes': rules}
        policy['available_projects'] = [
            {**rule, 'name': rule['project_id'], 'capabilities': ['read', 'write']}
            for rule in rules]
        route.fulfill(response=response, json=policy)

    page.route('**/api/collaboration/rooms/*/permissions', projection)
    try:
        open_permissions(page, stack, 'Synthetic project environment identity')
        section = page.locator('[data-permission-section="room"]')
        first = section.locator('[data-project-id="a:b"][data-environment-id="c"]')
        second = section.locator('[data-project-id="a"][data-environment-id="b:c"]')
        expect(first.locator('[data-permission-cap][value="write"]')).not_to_be_checked()
        expect(second.locator('[data-permission-cap][value="write"]')).to_be_checked()
    finally:
        context.close()
