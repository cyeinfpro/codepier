"""New wizard UI with an explicitly synthetic channel projection.

The owner session and room are real HTTP resources. Only the provider-facing
channel flow is a labelled fixture; no real login, consent or message is sent.
"""
from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import sign_in, create_room


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_ready_chat_can_approve_tasks_but_scope_drift_clears_old_checked_consent(
    collaboration_stack, chat_browser_pool, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()
    writes = []
    fields = [{'name': name, 'type': 'checkbox', 'label': label, 'required': True} for name, label in (
        ('confirm_until_revoked', '连接持续至我解除'),
        ('confirm_owner_identity', '发送配对码的是我本人'),
        ('confirm_future_tasks', '允许所列项目的后续工作'),
        ('confirm_result_delivery', '允许工作结果回到此私聊'),
        ('confirm_pairing_notices', '允许必要的配对和阻塞提示'),
    )]
    flow = {
        'id': 'synthetic-wizard-only', 'kind': 'weixin', 'stage': 'ready', 'version': 1,
        'account': {'id': 'synthetic-account', 'label': '合成微信', 'credential_version': 1},
        'binding': {'id': 'synthetic-binding', 'version': 1, 'generation': 1, 'peer_id': 'synthetic-owner'},
        'work_authorized': False, 'primary_action': 'approve_tasks',
        'display': {'title': '微信已连接', 'reason': '本用例使用合成渠道投影'},
        'task_scope': {'scope_digest': 'scope-before', 'summary': '核对当前范围', 'projects': [
            {'project_id': stack.projects[0]['id'], 'capabilities': ['read'], 'execution_targets': []}]},
        'actions': [{'action': 'approve_tasks', 'label': '允许本人交办工作'}],
        'next_request': {'method': 'POST', 'path': '/api/collaboration/channel-connect-flows/synthetic-wizard-only/actions',
            'body': {'action': 'approve_tasks', 'expected_version': 1}, 'form_fields': fields},
    }
    def channel_projection(route):
        if route.request.method == 'POST' and route.request.url.endswith('/actions'):
            submitted = route.request.post_data_json
            writes.append(submitted)
            flow['version'] += 1
            if submitted['action'] == 'approve_tasks':
                flow.update(work_authorized=True, primary_action='refresh')
                flow['next_request'] = {'body': {'action': 'refresh'}, 'form_fields': []}
                flow['actions'] = [{'action': 'revoke_tasks', 'label': '撤销工作许可'}]
                flow['allowed_actions'] = ['revoke_tasks']
            elif submitted['action'] == 'revoke_tasks':
                flow.update(work_authorized=False, primary_action='approve_tasks')
                flow['next_request'] = {'body': {'action': 'approve_tasks', 'expected_version': flow['version']},
                    'form_fields': fields}
                flow['actions'] = [{'action': 'approve_tasks', 'label': '允许本人交办工作'}]
                flow['allowed_actions'] = ['approve_tasks']
            else:
                raise AssertionError('Unexpected channel action: ' + submitted['action'])
        route.fulfill(status=200, json={'flow': deepcopy(flow)})
    try:
        sign_in(page, stack)
        create_room(page, '微信向导范围核对')
        page.route('**/api/collaboration/rooms/*/channels/weixin/connect', channel_projection)
        page.route('**/api/collaboration/channel-connect-flows/**', channel_projection)
        page.locator('.cr-room-actions [data-cr-action="weixin"]').click()
        # A close event queued by the previous dialog may arrive after this
        # dialog is reopened; it must not invalidate the new panel.
        page.locator('dialog.cr-panel').evaluate("dialog => dialog.dispatchEvent(new Event('close'))")
        expect(page.locator('[data-cr-form="weixin"]')).to_be_visible()
        expect(page.locator('[data-cr-channel-view]')).to_contain_text('已连接房间聊天')
        for checkbox in page.locator('[data-cr-form="weixin"] input[type="checkbox"]').all():
            checkbox.check()

        flow['version'] = 2
        flow['next_request']['body']['expected_version'] = 2
        flow['task_scope']['scope_digest'] = 'scope-after'
        flow['task_scope']['projects'][0]['capabilities'] = ['read', 'write']
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('参与范围已变化')
        expect(page.locator('[data-cr-form="weixin"] input:checked')).to_have_count(0)
        page.locator('[data-cr-form="weixin"] button[type="submit"]').click()
        assert writes == []
        for checkbox in page.locator('[data-cr-form="weixin"] input[type="checkbox"]').all():
            checkbox.check()
        page.locator('[data-cr-form="weixin"] button[type="submit"]').click()
        expect(page.locator('[data-cr-channel-view]')).to_contain_text('已允许本人交办工作')
        assert len(writes) == 1
        assert writes[0]['expected_scope_digest'] == 'scope-after'
        assert writes[0]['expected_version'] == 2
        assert all(writes[0][item['name']] is True for item in fields)
        original_account = deepcopy(flow['account'])
        original_binding = deepcopy(flow['binding'])
        revoke = page.locator('[data-channel-action="revoke_tasks"]')
        expect(revoke).to_have_accessible_name('关闭微信交办工作')
        expect(revoke).to_have_attribute('title', '保留聊天连接')
        revoke.click()
        expect(revoke).to_have_count(0)
        expect(page.locator('[data-cr-channel-view]')).to_contain_text('已连接房间聊天')
        expect(page.locator('[data-cr-form="weixin"]')).to_be_visible()
        expect(page.locator('[data-cr-form="weixin"] input:checked')).to_have_count(0)
        assert len(writes) == 2
        assert writes[1]['action'] == 'revoke_tasks'
        assert writes[1]['expected_version'] == 3
        assert writes[1]['idempotency_key'] != writes[0]['idempotency_key']
        assert not any(writes[1].get(item['name']) for item in fields)
        assert flow['account'] == original_account
        assert flow['binding'] == original_binding
        assert flow['stage'] == 'ready' and flow['work_authorized'] is False

        # A writable native room may still contain a historical flow whose
        # only allowed operation is cancellation. Account presence is not
        # permission to bind that old flow or replace its credentials.
        flow.update(stage='choose_account', version=5, primary_action='cancel',
            accounts=[{'id': 'retained-account', 'label': '原账号'}],
            actions=[{'action': 'cancel', 'label': '取消原流程'}], allowed_actions=['cancel'],
            next_request={'body': {'action': 'cancel', 'expected_version': 5}, 'form_fields': []})
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-form="weixin"]')).to_have_attribute('data-channel-action', 'cancel')
        expect(page.locator('[name="existing_account_id"]')).to_have_count(0)
        expect(page.locator('[data-cr-action="channel-existing"]')).to_have_count(0)
        assert len(writes) == 2

        flow['version'] = 6
        flow['actions'].append({'action': 'select_account', 'label': '使用已有微信'})
        flow['allowed_actions'].append('select_account')
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[name="existing_account_id"]')).to_be_visible()
        expect(page.locator('[data-cr-action="channel-existing"]')).to_be_visible()
        assert len(writes) == 2
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_unified_room_permissions_bulk_channel_modes_and_return(
    collaboration_stack, chat_browser_pool, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()
    channel_writes = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        sign_in(page, stack)
        create_room(page, '统一房间权限')
        room = next(item for item in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
                    if item['title'] == '统一房间权限')
        room_path = f'/api/collaboration/rooms/{room["id"]}'
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(page.locator('[data-cr-form="permissions"]')).to_be_visible()
        expect(page.locator('.cr-panel-content')).to_contain_text('保存后生效。')
        initial = stack.must(stack.client.get(room_path + '/permissions'))
        assert initial['configured'] and initial['version'] == 0
        assert not initial['scope']['project_scopes']
        room_section = page.locator('[data-permission-section="room"]')
        room_section.locator('[data-permission-select-all]').check()
        room_section.locator('[data-permission-bulk-level]').select_option('execute')
        # "All" respects each project's upper bound, including read-only rows.
        for project in initial['available_projects']:
            row = room_section.locator(
                f'[data-permission-row][data-project-id="{project["project_id"]}"]'
                f'[data-environment-id="{project.get("environment_id", "production")}"]')
            for capability in ('read', 'write', 'execute'):
                assert row.locator(f'[data-permission-cap][value="{capability}"]').is_checked() == (
                    capability in project['capabilities'])
            if 'execute' in project['capabilities']:
                chosen_targets = row.locator('[data-permission-target-option]:checked').evaluate_all(
                    '(inputs) => inputs.map(input => input.value)')
                assert chosen_targets == project['execution_targets']
        page.keyboard.press('Escape')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        unchanged = stack.must(stack.client.get(room_path + '/permissions'))
        assert unchanged['version'] == initial['version']
        assert unchanged['scope'] == initial['scope']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(page.locator('[data-cr-form="permissions"]')).to_be_visible()
        first_id, second_id = [project['id'] for project in stack.projects[:2]]
        first = room_section.locator(f'[data-permission-row][data-project-id="{first_id}"]')
        second = room_section.locator(f'[data-permission-row][data-project-id="{second_id}"]')
        first.locator('[data-permission-project]').check()
        expect(first.locator('[data-permission-level]')).to_have_value('read')
        expect(first.locator('[data-permission-level]')).to_have_value('read')
        expect(second.locator('[data-permission-level]')).to_have_value('')
        expect(page.locator('[data-permission-mode="weixin"]')).to_be_checked()
        expect(page.locator('[data-permission-mode="telegram"]')).to_have_count(0)
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        # Adding the first room project invalidates the old visibility stream.
        # The confirmed save must survive that safe dismissal, without reopening it.
        expect(page.locator('[data-cr-feedback]')).to_contain_text('权限设置已保存')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        policy = stack.must(stack.client.get(room_path + '/permissions'))
        assert policy['configured'] and policy['version'] == initial['version'] + 1
        assert len(policy['scope']['project_scopes']) == 1
        assert policy['scope']['project_scopes'][0]['project_id'] == first_id
        assert policy['scope']['project_scopes'][0]['capabilities'] == ['read']
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(first.locator('[data-permission-level]')).to_have_value('read')
        expect(second.locator('[data-permission-level]')).to_have_value('')
        page.locator('[data-cr-action="close"]').click()
        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        page.locator('dialog.cr-panel [data-cr-action="add-member"]').click()
        expect(page.locator('[data-cr-form="member"]')).to_be_visible()
        expect(page.locator('[data-cr-form="member"] [name="capability"]')).to_have_count(0)
        expect(page.locator('[data-cr-form="member"] [name="lifetime"]')).to_have_count(0)
        page.locator('[data-cr-form="member"] [name="name"]').fill('继承房间成员')
        with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith(room_path + '/members')) as added:
            page.locator('[data-cr-form="member"] button[type="submit"]').click()
        assert 'project_scopes' not in added.value.post_data_json
        expect(page.locator('[data-cr-flow-view]')).to_be_visible()
        page.locator('[data-cr-action="close"]').click()
        flow = {
            'id': 'synthetic-unified-permissions', 'kind': 'weixin', 'stage': 'ready', 'version': 1,
            'work_authorized': True, 'primary_action': 'refresh', 'actions': [], 'allowed_actions': [],
            'next_request': {'body': {'action': 'refresh'}, 'form_fields': []},
        }
        def channel_projection(route):
            if route.request.method == 'POST' and route.request.url.endswith('/actions'):
                channel_writes.append(route.request.post_data_json)
            route.fulfill(status=200, json={'flow': deepcopy(flow)})
        page.route('**/api/collaboration/rooms/*/channels/weixin/connect', channel_projection)
        page.route('**/api/collaboration/channel-connect-flows/**', channel_projection)
        page.locator('.cr-room-actions [data-cr-action="weixin"]').click()
        for _ in range(2):
            page.locator('[data-cr-action="channel-project-permissions"]').click()
            expect(page.locator('[data-cr-form="permissions"]')).to_be_visible()
            expect(first.locator('[data-permission-level]')).to_have_value('read')
            expect(first.locator('[data-permission-level]')).to_have_value('read')
            expect(second.locator('[data-permission-level]')).to_have_value('')
            first.locator('[data-permission-level]').select_option('write')
            page.locator('[data-cr-action="channel-scope-back"]').click()
            expect(page.locator('[data-cr-channel-view]')).to_be_visible()
        page.locator('[data-cr-action="channel-project-permissions"]').click()
        expect(first.locator('[data-permission-level]')).to_have_value('read')
        first.locator('[data-permission-level]').select_option('write')
        page.locator('[data-permission-mode="weixin"]').uncheck()
        wx_section = page.locator('[data-permission-section="weixin"]')
        wx_section.locator(f'[data-project-id="{first_id}"] [data-permission-level]').select_option('read')
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-channel-view]')).to_be_visible()
        page.locator('[data-cr-action="channel-project-permissions"]').click()
        expect(first.locator('[data-permission-level]')).to_have_value('write')
        expect(page.locator('[data-permission-mode="weixin"]')).not_to_be_checked()
        expect(wx_section.locator(f'[data-project-id="{first_id}"] [data-permission-level]')).to_have_value('read')
        expect(second.locator('[data-permission-level]')).to_have_value('')
        # A conflict must not reuse the room version or replay the same selection.
        conflicts = []
        permission_pattern = '**' + room_path + '/permissions'
        def stale_permission(route):
            if route.request.method == 'PUT':
                conflicts.append(route.request.post_data_json)
                route.fulfill(status=409, json={'detail': 'synthetic stale policy version'})
            else:
                route.continue_()
        page.route(permission_pattern, stale_permission)
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('重新载入')
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        assert len(conflicts) == 1
        page.unroute(permission_pattern, stale_permission)
        page.locator('[data-cr-action="reload-permissions"]').click()
        expect(page.locator('[data-cr-form="permissions"]')).to_be_visible()
        # A late read after dismissal must not resurrect this page or another dialog.
        pending = []
        policy = stack.must(stack.client.get(room_path + '/permissions'))
        page.route(permission_pattern, lambda route: pending.append(route))
        page.locator('[data-cr-action="reload-permissions"]').click()
        expect(page.locator('[data-cr-permissions-view]')).to_contain_text('正在读取')
        page.locator('[data-cr-action="close"]').click()
        assert len(pending) == 1
        pending[0].fulfill(status=200, json=policy)
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        assert channel_writes == []
        assert errors == []
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('renewal_second', [0, 17])
def test_permission_lifetime_renewal_and_custom_inheritance(
    collaboration_stack, chat_browser_pool, engine, renewal_second
):
    """Expired retained projection is synthetic; policy writes use the real Hub."""
    from datetime import datetime, timezone
    import time

    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(
        viewport={'width': 1280, 'height': 1000}, timezone_id='UTC')
    page = context.new_page()
    writes = []
    try:
        sign_in(page, stack)
        create_room(page, '权限有效期')
        room = next(item for item in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
                    if item['title'] == '权限有效期')
        path = f'/api/collaboration/rooms/{room["id"]}/permissions'
        deadline = (int(time.time()) // 60 + 120) * 60 + 0.375
        projects = [{'project_id': item['id'], 'environment_id': 'production',
                     'capabilities': ['read'], 'execution_targets': [],
                     'lifetime': {'mode': 'timed', 'expires_at': deadline}}
                    for item in stack.projects[:2]]
        policy = stack.must(stack.client.put(path, json={
            'expected_version': 0, 'scope': {'project_scopes': projects},
            'channels': {'weixin': {'mode': 'follow_room'}, 'telegram': {'mode': 'follow_room'}},
        }))
        # The out-of-band setup changes the room SSE access snapshot. Start a
        # fresh page at that committed scope before testing lifetime edits;
        # a late access_changed must otherwise dismiss the old editor.
        page.reload()
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 2')
        retained = deepcopy(policy)
        deadline = next(rule for rule in policy['scope']['project_scopes'] if rule['project_id'] == stack.projects[1]['id'])['lifetime']['expires_at']
        next(rule for rule in retained['scope']['project_scopes'] if rule['project_id'] == stack.projects[0]['id'])['lifetime']['expires_at'] = int(time.time()) - 60
        def policy_projection(route):
            if route.request.method == 'GET':
                route.fulfill(status=200, json=deepcopy(retained))
            else:
                writes.append(route.request.post_data_json)
                route.continue_()
        pattern = '**' + path
        page.route(pattern, policy_projection)
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        section = page.locator('[data-permission-section="room"]')
        first = section.locator(f'[data-project-id="{stack.projects[0]["id"]}"]')
        second = section.locator(f'[data-project-id="{stack.projects[1]["id"]}"]')
        expect(first.locator('.cr-permission-expiry summary')).to_contain_text('已过期')
        first.locator('.cr-permission-expiry summary').click()
        expect(first.locator('[data-permission-expired]')).to_be_visible()
        expect(first.locator('[data-permission-lifetime-mode]')).to_have_value('timed')
        second.locator('[data-permission-level]').select_option('write')
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('请更新“ProjectAlpha”的有效期')
        assert writes == []
        first.locator('[data-permission-lifetime-mode]').select_option('until_revoked')
        expect(first.locator('[data-permission-expired]')).not_to_be_visible()
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('已保存')
        assert len(writes) == 1
        sent = {rule['project_id']: rule for rule in writes[0]['scope']['project_scopes']}
        assert sent[stack.projects[0]['id']]['lifetime'] == {'mode': 'until_revoked', 'expires_at': None}
        assert sent[stack.projects[1]['id']]['lifetime']['expires_at'] == deadline
        # No destructive remove/save/re-add workaround was used for renewal.
        assert len(sent) == 2
        page.unroute(pattern, policy_projection)
        def track_write(route):
            if route.request.method == 'PUT':
                writes.append(route.request.post_data_json)
            route.continue_()
        page.route(pattern, track_write)
        page.locator('[data-permission-mode="weixin"]').uncheck()
        custom = page.locator('[data-permission-section="weixin"]')
        custom_second = custom.locator(f'[data-project-id="{stack.projects[1]["id"]}"]')
        custom_second.locator('[data-permission-level]').select_option('read')
        expect(custom_second.locator('[data-permission-lifetime-mode]')).to_have_value('timed')
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('已保存')
        assert len(writes) == 2
        assert writes[1]['channels']['weixin']['scope']['project_scopes'][0]['lifetime']['expires_at'] == deadline
        # Intentional deadline editing replaces only the chosen row's timestamp.
        renewal = (int(time.time()) // 60 + 60) * 60 + renewal_second
        if not first.locator('[data-permission-lifetime-mode]').is_visible():
            first.locator('.cr-permission-expiry summary').click()
        first.locator('[data-permission-lifetime-mode]').select_option('timed')
        # datetime-local normalizes zero seconds away. Playwright fill requires
        # that canonical value; cover both minute and second precision.
        first.locator('[data-permission-lifetime-until]').fill(
            datetime.fromtimestamp(renewal, timezone.utc).isoformat(
                timespec='seconds' if renewal_second else 'minutes')
            .removesuffix('+00:00'))
        page.locator('[data-cr-form="permissions"] button[type="submit"]').click()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('已保存')
        assert len(writes) == 3
        final_rules = {rule['project_id']: rule for rule in writes[2]['scope']['project_scopes']}
        assert final_rules[stack.projects[0]['id']]['lifetime']['expires_at'] == renewal
        assert final_rules[stack.projects[1]['id']]['lifetime']['expires_at'] == deadline
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_external_permission_setup_invalidates_open_editor(
    collaboration_stack, chat_browser_pool, engine
):
    """A real scope-change hint must clear an old unsaved permission editor."""
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = context.new_page()
    page.add_init_script("""(() => {
        const Native = window.EventSource;
        window.permissionHints = {ready: 0, access: 0};
        window.EventSource = class extends Native {
            constructor(url, ...args) {
                super(url, ...args);
                if (!String(url).includes('/api/collaboration/rooms/')) return;
                this.addEventListener('change', () => window.permissionHints.ready++);
                this.addEventListener('access_changed', () => window.permissionHints.access++);
            }
        };
    })();""")
    writes = []
    try:
        sign_in(page, stack)
        create_room(page, '外部权限准备失效')
        # Establish the empty-room access snapshot before changing its projects.
        page.wait_for_function('() => window.permissionHints.ready > 0')
        room = next(item for item in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
                    if item['title'] == '外部权限准备失效')
        path = f'/api/collaboration/rooms/{room["id"]}/permissions'
        def track_write(route):
            if route.request.method == 'PUT':
                writes.append(route.request.post_data_json)
            route.continue_()
        page.route('**' + path, track_write)
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        section = page.locator('[data-permission-section="room"]')
        row = section.locator(f'[data-project-id="{stack.projects[0]["id"]}"]')
        old_selector = row.locator('[data-permission-level]')
        old_selector.select_option('write')
        old_control = old_selector.element_handle()
        assert old_control.input_value() == 'write'
        policy = stack.must(stack.client.put(path, json={
            'expected_version': 0,
            'scope': {'project_scopes': [
                {'project_id': item['id'], 'environment_id': 'production',
                 'capabilities': ['read'], 'execution_targets': [],
                 'lifetime': {'mode': 'until_revoked'}}
                for item in stack.projects[:2]
            ]},
            'channels': {'weixin': {'mode': 'follow_room'}, 'telegram': {'mode': 'follow_room'}},
        }))
        page.wait_for_function('() => window.permissionHints.access === 1')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        expect(page.locator('[data-cr-form="permissions"]')).to_have_count(0)
        assert not old_control.evaluate('(element) => element.isConnected')
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 2')
        assert writes == []
        saved = stack.must(stack.client.get(path))
        assert saved['version'] == policy['version']
        assert all(rule['capabilities'] == ['read'] for rule in saved['scope']['project_scopes'])
    finally:
        context.close()
