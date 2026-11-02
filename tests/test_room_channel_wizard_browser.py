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
        expect(revoke).to_have_text('仅撤销工作许可')
        expect(page.locator('[data-cr-channel-view]')).to_contain_text('保留聊天连接')
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
    finally:
        context.close()
