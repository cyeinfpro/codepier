"""Mobile connection controls against a synthetic host, with a real owner session."""
import re

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import create_room, sign_in


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def test_host_batch_and_revoke_confirm_current_impact(collaboration_stack, chat_browser_pool):
    stack = collaboration_stack
    context = chat_browser_pool('chromium').new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    host = {
        'host_connection_id': 'synthetic-host',
        'label': '测试 dot',
        'state': 'active',
        'display_state': 'waiting_host_renewal',
        'room_count': 2,
        'connection_capabilities': {'host_reference': True, 'host_revoke': True, 'batch_rebind': True},
    }
    target = {
        **host, 'host_connection_id': 'target-host', 'room_count': 1,
        'display_state': 'connected', 'single_room_title': '工作室丙',
        'connection_capabilities': {'host_reference': True, 'verified_reuse': True},
    }
    impact = {
        'authority_version': 2,
        'impact_digest': 'first',
        'room_count': 2,
        'queued_work_count': 1,
        'dispatched_operation_count': 2,
        'items': [
            {'member_id': 'member-a', 'conversation_id': 'room-a', 'room_title': '工作室甲',
             'binding_generation': 4},
            {'member_id': 'member-b', 'conversation_id': 'room-b', 'room_title': '工作室乙',
             'binding_generation': 5},
        ],
    }
    revoke_bodies = []
    batch_bodies = []

    def connection_projection(route):
        request = route.request
        path = request.url.split('/api/collaboration/', 1)[1].split('?', 1)[0]
        if path == 'connections':
            route.fulfill(json={'items': [host, target], 'connection_capabilities': host['connection_capabilities']})
        elif path == 'connections/synthetic-host':
            route.fulfill(json=host)
        elif path == 'connections/synthetic-host/impact':
            route.fulfill(json=impact)
        elif path == 'connections/rebind-batch':
            batch_bodies.append(request.post_data_json)
            route.fulfill(json={'batch_id': 'synthetic-batch', 'complete': True, 'items': [
                {**impact['items'][0], 'status': 'activated'},
                {**impact['items'][1], 'status': 'failed', 'error_code': 'ROOM_MEMBER_INACTIVE'},
            ]})
        elif path == 'connections/synthetic-host/revoke':
            revoke_bodies.append(request.post_data_json)
            if len(revoke_bodies) == 1:
                impact.update(authority_version=3, impact_digest='second', room_count=1)
                impact['items'].pop(0)
                route.fulfill(status=409, json={'error': {
                    'code': 'ROOM_CONNECTION_IMPACT_CHANGED', 'message': '受影响的连接已变化',
                }})
            else:
                host.update(state='revoked', display_state='revoked', room_count=1)
                route.fulfill(json={'host_connection_id': host['host_connection_id'], 'state': 'revoked'})
        else:
            raise AssertionError('Unexpected host connection request: ' + path)

    try:
        sign_in(page, stack)
        create_room(page, '移动端连接管理')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.route(re.compile(r'/api/collaboration/connections(?:[/?]|$)'), connection_projection)
        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        page.locator('[data-cr-action="host-list"]').click()
        expect(page.get_by_text('旧房间可能各自登记了同名连接。', exact=False)).to_be_visible()
        expect(page.get_by_role('button', name='查看 测试 dot · 工作室丙 的连接详情')).to_be_visible()
        page.locator('[data-cr-action="host-detail"][data-id="synthetic-host"]').click()
        expect(page.locator('[data-cr-host-summary]')).to_contain_text('等待宿主续订')
        page.locator('[data-cr-action="host-impact"]').click()
        expect(page.locator('[data-cr-host-impact]')).to_contain_text('工作室甲')
        expect(page.locator('[data-cr-host-impact]')).to_contain_text('工作室乙')
        expect(page.get_by_role('button', name='撤销这条连接')).to_be_visible()
        expect(page.locator('[data-cr-host-impact]')).to_contain_text('同名的其他连接继续使用')
        assert revoke_bodies == []
        page.get_by_label('工作室甲').check()
        page.get_by_label('工作室乙').check()
        page.locator('[data-cr-action="load-batch-targets"]').click()
        page.locator('[name="batch_host_connection"]').select_option('target-host')
        page.locator('[data-cr-action="start-host-batch"]').click()
        expect(page.locator('[data-cr-batch-results]')).to_contain_text('已更换')
        expect(page.locator('[data-cr-batch-results]')).to_contain_text('更换失败')
        assert batch_bodies[0]['host_connection_id'] == 'target-host'
        assert batch_bodies[0]['members'] == [
            {'member_id': 'member-a', 'expected_generation': 4},
            {'member_id': 'member-b', 'expected_generation': 5},
        ]

        page.locator('[data-cr-action="host-detail"]').click()
        page.locator('[data-cr-action="host-impact"]').click()
        page.locator('[data-cr-action="revoke-host"]').click()
        expect(page.locator('[data-cr-host-impact]')).to_contain_text('移出 工作室甲')
        assert len(revoke_bodies) == 1
        assert revoke_bodies[0]['impact_digest'] == 'first'
        page.locator('[data-cr-action="revoke-host"]').click()
        expect(page.locator('[data-cr-host-summary]')).to_contain_text('已撤销')
        assert revoke_bodies[1]['impact_digest'] == 'second'
        assert revoke_bodies[1]['expected_authority_version'] == 3
        assert revoke_bodies[0]['idempotency_key'] != revoke_bodies[1]['idempotency_key']
    finally:
        context.close()
