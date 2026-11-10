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
    extra_remotes = [stack.must(stack.client.post('/api/vps', json={
        'name': f'Extra synthetic VPS {index}', 'host': f'extra-{index}.example.invalid',
        'username': 'fixture', 'password': 'synthetic-not-real', 'host_key_policy': 'strict',
        'project_ids': [stack.project['id']]}))['target'] for index in range(16)]
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        path = open_permissions(page, stack, 'VPS bulk permission fixture')
        section = page.locator('[data-permission-section="room"]')
        first = section.locator(f'[data-project-id="{stack.project["id"]}"]')
        section.locator('[data-permission-select-all]').check()
        section.locator('[data-permission-bulk-level]').select_option('execute')
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        expect(first).to_contain_text('Synthetic room VPS')
        expect(first.locator(f'[data-permission-target-option][value="{other_remote}"]')).to_be_checked()
        expect(page.locator('[data-permission-mode="telegram"]')).to_have_count(0)
        for channel in ('weixin',):
            page.locator(f'[data-permission-mode="{channel}"]').uncheck()
            custom = page.locator(f'[data-permission-section="{channel}"]')
            custom.locator('[data-permission-select-all]').check()
            custom.locator('[data-permission-bulk-level]').select_option('execute')
            expect(custom.locator(f'[data-project-id="{stack.project["id"]}"] '
                                  f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        payload = submit_permissions(page)
        for scope in [payload['scope'], payload['channels']['weixin']['scope']]:
            rule = next(r for r in scope['project_scopes'] if r['project_id'] == stack.project['id'])
            assert set(rule['execution_targets']) == {'project_agent', remote, other_remote, *extra_remotes}
        saved = stack.must(stack.client.get(path))
        assert saved['channels']['weixin']['scope'] == saved['scope']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        first.locator('[data-permission-targets] > summary').click()
        first.locator(f'[data-permission-target-option][value="{remote}"]').uncheck()
        second = section.locator(f'[data-project-id="{stack.projects[1]["id"]}"]')
        second.locator('[data-permission-level]').select_option('')
        expect(page.locator('[data-permission-mode="telegram"]')).to_have_count(0)
        for channel in ('weixin',):
            custom = page.locator(f'[data-permission-section="{channel}"]')
            expect(custom.locator(f'[data-project-id="{stack.project["id"]}"] '
                                  f'[data-permission-target-option][value="{remote}"]')).to_be_disabled()
            expect(custom.locator(f'[data-project-id="{stack.projects[1]["id"]}"] '
                                  '[data-permission-project]')).to_be_disabled()
        payload = submit_permissions(page)
        for scope in [payload['scope'], payload['channels']['weixin']['scope']]:
            assert stack.projects[1]['id'] not in scope['project_ids']
            rule = next(r for r in scope['project_scopes'] if r['project_id'] == stack.project['id'])
            assert set(rule['execution_targets']) == {'project_agent', other_remote, *extra_remotes}
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        section.locator('[data-permission-select-all]').check()
        section.locator('[data-permission-select-all]').uncheck()
        expect(section.locator('[data-permission-targets]:visible')).to_have_count(0)
        section.locator('[data-permission-select-all]').check()
        section.locator('[data-permission-bulk-level]').select_option('execute')
        expect(first.locator(f'[data-permission-target-option][value="{remote}"]')).to_be_checked()
        # Merely closing never commits the newly expanded selection.
        page.keyboard.press('Escape')
        assert stack.must(stack.client.get(path))['version'] == saved['version'] + 1
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_bulk_projects_saves_above_old_limit_without_contract_error(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    for index in range(3, 129):
        directory = stack.root / f'PermissionProject{index}'
        directory.mkdir()
        stack.projects.append(stack.create_project({
            'alias': directory.name, 'root': str(directory), 'device_id': stack.device,
            'mode': 'read', 'allow_tasks': False}))
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    if engine == 'chromium':
        def projection(route):
            if route.request.method != 'GET':
                route.continue_()
                return
            response = route.fetch()
            policy = response.json()
            policy.pop('limits', None)
            route.fulfill(response=response, json=policy)
        page.route('**/api/collaboration/rooms/*/permissions', projection)

    try:
        path = open_permissions(page, stack, 'Unlimited project permissions')
        section = page.locator('[data-permission-section="room"]')
        select_all = section.locator('[data-permission-select-all]')
        first = section.locator('[data-permission-project]').first
        first.check()
        expect(select_all).to_have_js_property('indeterminate', True)
        expect(section.locator('[data-permission-count]')).to_have_text('已选 1 / 129')
        section.locator('[data-permission-bulk-level]').select_option('read')
        expect(section.locator('[data-permission-project]:checked')).to_have_count(1)
        select_all.check()
        expect(select_all).to_have_js_property('indeterminate', False)
        expect(section.locator('[data-permission-project]:checked')).to_have_count(129)
        payload = submit_permissions(page)
        assert len(payload['scope']['project_scopes']) == 129
        assert all(rule['capabilities'] == ['read'] for rule in payload['scope']['project_scopes'])
        saved = stack.must(stack.client.get(path))
        assert len(saved['scope']['project_scopes']) == 129
        assert saved['channels']['weixin']['effective_scope'] == saved['effective_scope']
        assert saved['channels']['telegram']['effective_scope'] == saved['effective_scope']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(section.locator('[data-permission-project]:checked')).to_have_count(129)
        submit_permissions(page, closes=False)
        assert stack.must(stack.client.get(path))['version'] == saved['version'] + 1
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



def test_permission_continuation_reviews_and_submits_exact_selection(
        collaboration_stack, chat_browser_pool):
    stack = collaboration_stack
    context = chat_browser_pool('chromium').new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        path = open_permissions(page, stack, 'Continuation permissions fixture')
        current = stack.must(stack.client.get(path))
        preview_requests = []
        saved_requests = []

        def preview(route):
            preview_requests.append(route.request.post_data_json)
            route.fulfill(json={
                'policy_preview_digest': 'draft-digest',
                'continuation_preview_id': 'draft-preview',
                'candidates': [
                    {'task_id': 'work-a', 'label': '房间读取工作', 'selected_by_default': True,
                     'remaining_operations': [{'label': '读取原文件'}]},
                    {'task_id': 'source-b', 'label': '微信原消息', 'selected_by_default': True,
                     'remaining_operations': [{'label': '保存项目结果'}]},
                    {'task_id': 'source-c', 'label': '授权已撤销的微信消息', 'eligible': False,
                     'reason_code': 'CHANNEL_OWNER_NOT_APPROVED'},
                ],
            })

        def permissions(route):
            if route.request.method != 'PUT':
                route.continue_()
                return
            saved_requests.append(route.request.post_data_json)
            route.fulfill(json=current | {
                'version': current['version'] + 1,
                'continuation': {'batch_id': 'receipt-one', 'state': 'complete',
                                 'counts': {'continued': 1, 'waiting_operations': 0,
                                            'skipped': 0, 'pending': 0},
                                 'results': [{'task_id': 'source-b', 'state': 'continued'}]},
            })

        page.route('**' + path + '/continuation-preview', preview)
        page.route('**' + path, permissions)
        form = page.locator('[data-cr-form="permissions"]')
        opt_in = form.locator('[data-cr-continue-selected]')
        all_tasks = form.locator('[data-cr-continuation-all]')
        submit = form.locator('button[type="submit"]')
        expect(opt_in).not_to_be_checked()
        expect(submit).to_have_text('保存权限')
        opt_in.check()
        expect(form.locator('[data-cr-continuation-count]')).to_have_text('已选 2 / 2')
        expect(form).to_contain_text('保存项目结果')
        expect(form.locator('[data-cr-continuation-task][value="source-c"]')).to_be_disabled()
        expect(form).to_contain_text('需要先在微信连接里重新允许本人交办')
        all_tasks.uncheck()
        expect(form.locator('[data-cr-continuation-task]:checked')).to_have_count(0)
        all_tasks.check()
        form.locator('[data-cr-continuation-task][value="work-a"]').uncheck()
        expect(form.locator('[data-cr-continuation-count]')).to_have_text('已选 1 / 2')
        expect(submit).to_have_text('保存并继续')
        submit.click()
        expect(page.locator('[data-cr-continuation-result]')).to_contain_text('1 项已继续')
        expect(page.locator('[data-cr-continuation-result]')).to_contain_text('微信原消息')
        assert len(saved_requests) == len(preview_requests) == 1
        assert saved_requests[0]['continue_selected'] is True
        assert saved_requests[0]['selected_task_ids'] == ['source-b']
        assert saved_requests[0]['policy_preview_digest'] == 'draft-digest'
        assert saved_requests[0]['continuation_preview_id'] == 'draft-preview'
        assert saved_requests[0]['idempotency_key']
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('code,status', [('JOIN_CODE_EXPIRED', 409), ('INVALID_SCOPE', 422),
                                        ('IDEMPOTENCY_CONFLICT', 409), ('IDEMPOTENCY_RESPONSE_EXPIRED', 409),
                                        ('STALE_VERSION', 409), ('NETWORK_UNCERTAIN', 0)])
def test_permission_save_errors_preserve_identity_and_real_conflicts(
        collaboration_stack, chat_browser_pool, engine, code, status):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    attempts = []
    try:
        path = open_permissions(page, stack, 'Permission error recovery')
        original = stack.must(stack.client.get(path))

        def save(route):
            if route.request.method != 'PUT':
                route.continue_()
                return
            attempts.append(route.request.post_data_json)
            if len(attempts) == 1 or (code in {'IDEMPOTENCY_CONFLICT', 'IDEMPOTENCY_RESPONSE_EXPIRED'} and len(attempts) == 2):
                if code == 'NETWORK_UNCERTAIN':
                    response = route.fetch()
                    assert response.ok, response.text()
                    route.abort('failed')
                else:
                    route.fulfill(status=status, json={'error': {
                        'code': code, 'message': 'Synthetic permission rejection'}})
            else:
                route.continue_()

        page.route('**' + path, save)
        form = page.locator('[data-cr-form="permissions"]')
        submit = form.locator('button[type="submit"]')
        submit.click()
        error = page.locator('[data-cr-panel-error]')
        if code == 'NETWORK_UNCERTAIN':
            recovery = page.locator('[data-cr-panel-recovery] [data-cr-action="recover-action"]')
            expect(recovery).to_be_visible()
            # Changed draft must not replace the uncertain serialized request.
            form.locator('[data-permission-select-all]').first.check()
            recovery.click()
            expect(page.locator('dialog.cr-panel')).not_to_be_visible()
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            assert stack.must(stack.client.get(path))['version'] == original['version'] + 1
        elif code in {'IDEMPOTENCY_CONFLICT', 'IDEMPOTENCY_RESPONSE_EXPIRED'}:
            expect(error).to_contain_text('Synthetic permission rejection')
            expect(error).not_to_contain_text('权限已在其他位置更新')
            with page.expect_response(lambda r: r.request.method == 'PUT' and r.url.endswith('/permissions')):
                submit.click()
            assert len(attempts) == 2 and attempts[0] == attempts[1]
        else:
            if code == 'STALE_VERSION':
                expect(error).to_contain_text('权限已在其他位置更新')
                submit.click()
                assert len(attempts) == 1
                page.locator('[data-cr-action="reload-permissions"]').click()
                expect(form).to_be_visible()
            else:
                expect(error).to_contain_text('Synthetic permission rejection')
                expect(error).not_to_contain_text('权限已在其他位置更新')
                form.locator('[data-permission-select-all]').first.check()
            submit_permissions(page, closes=code != 'STALE_VERSION')
            assert len(attempts) == 2
            assert attempts[0]['idempotency_key'] != attempts[1]['idempotency_key']
    finally:
        context.close()
