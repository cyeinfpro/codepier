"""Context/diagnostic UI contracts with explicitly synthetic fact projections.

Owner login, rooms and source messages use the real isolated HTTP Hub. Facts,
their correction receipt and selected diagnostic records are labelled fixtures;
native summary CAS/source authorization and delivery journals have separate
service regressions. This does not represent a live model or WeChat account.
"""
from copy import deepcopy
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import sign_in, create_room


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_context_correction_preserves_room_draft_and_diagnostics_never_add_authority(
    collaboration_stack, chat_browser_pool, engine
):
    stack = collaboration_stack
    browser_context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1000})
    page = browser_context.new_page()
    errors, writes, reads = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        sign_in(page, stack)
        create_room(page, '上下文验收房间')
        room = next(item for item in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
            if item['title'] == '上下文验收房间')
        room_id = room['id']
        source = stack.must(stack.client.post(f'/api/collaboration/rooms/{room_id}/messages', json={
            'body_text': '原始约束：页面使用蓝色。', 'client_message_id': uuid4().hex,
            'idempotency_key': uuid4().hex}))['message']
        facts = {'conversation_id': room_id, 'version': 1, 'visibility_revision': 'fixture-context-scope',
            'facts': [{'id': 'fixture-fact-one', 'kind': 'constraint', 'key': 'color',
                'text': '<img src=x onerror=alert(1)> 使用蓝色', 'review_state': 'current',
                'sources': [{'message_id': source['id'], 'version': source['version'], 'digest': 'fixture'}],
                'artifact_refs': [], 'project_ids': []}], 'active_projects': [],
            'next_cursor': 'opaque/+context-page', 'incomplete': True,
            'incomplete_reasons': ['more_facts'], 'review_state': 'current',
            'read_only': True, 'grants_authority': False}
        def room_projection(route):
            response = route.fetch()
            result = response.json()
            result['room']['context'] = deepcopy(facts)
            route.fulfill(status=response.status, json=result)
        def context_projection(route):
            reads.append(route.request.url)
            value = deepcopy(facts)
            if parse_qs(urlsplit(route.request.url).query).get('cursor'):
                value['facts'] = [{'id': 'fixture-fact-two', 'kind': 'decision', 'key': 'layout',
                    'text': '第二页的已确认布局', 'sources': [], 'artifact_refs': [], 'project_ids': []}]
                value.update(next_cursor=None, incomplete=False, incomplete_reasons=[])
            route.fulfill(status=200, json=value)
        def message_write(route):
            if route.request.method != 'POST':
                route.continue_()
                return
            body = route.request.post_data_json
            writes.append(body)
            if body.get('supersedes_context_ids'):
                # Synthetic summary/correction receipt only; do not suggest
                # this substitutes for the native CAS/transaction tests.
                facts.update(version=facts['version'] + 1, facts=[], next_cursor=None,
                    incomplete=True, incomplete_reasons=['unreviewed_input'], review_state='needs_review')
                route.fulfill(status=200, json={'message': {'id': 'fixture-corrected-message',
                    'body_text': body['body_text'], 'author_kind': 'owner', 'version': 1,
                    'client_message_id': body['client_message_id'], 'created_at': 2}})
            else:
                route.continue_()
        def diagnostic_projection(route):
            reads.append(route.request.url)
            response = route.fetch()
            value = response.json()
            value.update(queues=[{'kind': 'waiting_input', 'label': '等待原输入核对',
                'pending_count': 2, 'oldest_age_seconds': None}],
                budgets=[{'goal_id': 'fixture-goal', 'title': '合成额度投影',
                    'counters': [{'label': '已准入步骤', 'used': 0, 'limit': 2}]}],
                traces=[{'kind': 'operation', 'id': 'fixture-operation', 'state': 'unknown',
                    'goal_id': 'fixture-goal', 'work_item_id': 'fixture-step',
                    'display': {'title': '原操作待核对', 'reason': '<b>服务端原原因</b>'},
                    'raw_body': 'synthetic-private-not-for-ui', 'callback_url': 'https://example.invalid/private'}],
                truncated={'members': False, 'traces': True, 'budgets': False, 'pending': True, 'goals': False})
            route.fulfill(status=200, json=value)
        page.route(re.compile(r'/api/collaboration/rooms/' + room_id + r'(?:\?.*)?$'), room_projection)
        page.route(f'**/api/collaboration/rooms/{room_id}/context?*', context_projection)
        page.route(re.compile(r'/api/collaboration/rooms/' + room_id + r'/messages(?:\?.*)?$'), message_write)
        page.route(f'**/api/collaboration/rooms/{room_id}/diagnostics*', diagnostic_projection)

        page.locator('.cr-room-actions [data-cr-action="settings"]').click()
        page.locator('[data-cr-action="context"]').click()
        expect(page.locator('[data-context-id="fixture-fact-one"]')).to_contain_text('<img src=x onerror=alert(1)>')
        expect(page.locator('[data-cr-context-view] img')).to_have_count(0)
        page.locator('[data-cr-action="more-context"]').click()
        expect(page.locator('[data-context-id]')).to_have_count(2)
        assert any('cursor=opaque%2F%2Bcontext-page' in url for url in reads)
        page.locator('[data-cr-action="context-source"]').click()
        expect(page.locator('[data-cr-context-source]')).to_contain_text('原始约束：页面使用蓝色。')
        page.locator('[data-cr-action="context"]').click()
        page.locator('[data-context-id="fixture-fact-one"] [data-cr-action="correct-context"]').click()
        page.locator('#cr-message').fill('明确纠正：改用绿色。')
        expect(page.locator('[data-cr-correction]')).to_contain_text('版本 1')
        create_room(page, '另一个上下文房间')
        page.locator(f'[data-cr-action="select"][data-id="{room_id}"]').click()
        expect(page.locator('#cr-message')).to_have_value('明确纠正：改用绿色。')
        expect(page.locator('[data-cr-correction]')).to_contain_text('版本 1')

        facts['version'] = 2
        facts['facts'][0].update(id='fixture-fact-current', text='待重新核对的原约束', review_state='needs_review')
        facts.update(review_state='needs_review', incomplete_reasons=['unreviewed_input'], next_cursor=None)
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-correction]')).to_contain_text('上下文已变化')
        expect(page.locator('.cr-send')).to_be_disabled()
        page.locator('[data-cr-correction] [data-cr-action="context"]').click()
        expect(page.locator('[data-context-id="fixture-fact-current"]')).to_contain_text('待重新核对')
        page.locator('[data-cr-action="correct-context"]').click()
        page.locator('.cr-send').click()
        expect(page.locator('#cr-message')).to_have_value('')
        expect(page.locator('[data-cr-correction]')).not_to_be_visible()
        assert writes[0]['supersedes_context_ids'] == ['fixture-fact-current']
        assert writes[0]['expected_context_version'] == 2
        page.locator('#cr-message').fill('普通讨论无需附带纠正权限。')
        page.locator('.cr-send').click()
        expect(page.locator('#cr-message')).to_have_value('')
        assert len(writes) == 2
        assert 'supersedes_context_ids' not in writes[1] and 'expected_context_version' not in writes[1]

        page.locator('.cr-room-actions [data-cr-action="settings"]').click()
        page.locator('[data-cr-action="diagnostics"]').click()
        expect(page.locator('[data-cr-diagnostics-view]')).to_contain_text('这些记录和额度不授予工作权限')
        expect(page.locator('[data-cr-diagnostics-incomplete]')).to_contain_text('原记录、待处理输入')
        expect(page.locator('[data-cr-diagnostic-queues]')).to_contain_text('未提供')
        page.locator('[data-cr-diagnostic-budgets] summary').click()
        expect(page.locator('[data-cr-diagnostic-budgets]')).to_contain_text('0 / 2')
        expect(page.locator('[data-cr-diagnostic-budgets] .cr-facts > div').filter(
            has_text='下一可用时间').locator('dd')).to_have_text('未提供')
        page.locator('[data-trace-id="fixture-operation"] summary').click()
        expect(page.locator('[data-trace-id="fixture-operation"]')).to_contain_text('<b>服务端原原因</b>')
        expect(page.locator('[data-trace-id="fixture-operation"] b')).to_have_count(0)
        expect(page.locator('[data-cr-diagnostics-view]')).not_to_contain_text('synthetic-private-not-for-ui')
        expect(page.locator('[data-cr-diagnostics-view]')).not_to_contain_text('https://example.invalid/private')
        page.locator('[data-cr-action="refresh-diagnostics"]').click()
        expect(page.locator('[data-cr-diagnostics-incomplete]')).to_be_visible()
        assert len(writes) == 2 and not errors
    finally:
        try:
            # Drain synthetic fetch/fulfill callbacks before disposing responses.
            page.unroute_all(behavior='wait')
        finally:
            browser_context.close()
