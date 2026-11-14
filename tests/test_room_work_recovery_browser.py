"""Owner work controls with a labelled synthetic managed-work projection.

The Hub, owner session and room are real. The work projection isolates browser
consent/fencing behavior; native recovery transactions are tested separately.
"""
from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import sign_in, create_room


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_owner_restores_original_participant_and_selects_original_goal_successor(
    collaboration_stack, chat_browser_pool, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1100})
    page = context.new_page()
    writes = []
    project = stack.projects[0]['id']
    candidate = {'member_id': 'approved-worker', 'name': '原已批准助手', 'state': 'ready_work',
        'binding_generation': 2, 'projects': [{'project_id': project, 'environment_id': 'production',
            'capabilities': ['read'], 'execution_targets': []}]}
    work = {'id': 'original-goal', 'version': 5, 'intent_revision': 2, 'state': 'blocked',
        'title': '继续原检查工作', 'acceptance': '核对原操作回执后完成检查',
        'coordinator_member_id': 'original-coordinator', 'coordinator_epoch': 3,
        'members': [{'id': 'original-coordinator', 'name': '原主责'},
                    {'id': candidate['member_id'], 'name': candidate['name']}],
        'project_ids': [project],
        'items': [{'id': 'original-step', 'title': '原检查步骤', 'state': 'blocked',
                   'member_id': candidate['member_id'], 'reason_code': 'connection_changed'}],
        'operations': [{'id': 'original-operation', 'state': 'failed', 'cancel_requested': False,
                        'display': {'title': '原操作已结束', 'reason': '保留原失败回执供核对'}}],
        'allowed_actions': ['restore_binding', 'takeover', 'cancel'],
        'display': {'title': '原工作等待恢复', 'reason': '连接已更换，请核对后继续'},
        'recovery': {'coordinator_member_id': 'original-coordinator', 'coordinator_name': '原主责',
            'coordinator_epoch': 3, 'intent_revision': 2, 'state': 'available', 'reason_code': 'binding_changed',
            'restore_candidates': [deepcopy(candidate)], 'takeover_candidates': [deepcopy(candidate)]},
    }
    def projection(route):
        if route.request.method != 'GET':
            route.continue_()
            return
        response = route.fetch()
        result = response.json()
        result['works'] = [deepcopy(work)]
        if work.get('channel_deliveries'):
            result['messages'].append({'id': 'original-final', 'version': 1, 'author_kind': 'dot',
                'created_at': 1, 'body_text': '原结果已保存',
                'channel_deliveries': deepcopy(work['channel_deliveries'])})
        route.fulfill(status=response.status, json=result)
    def control(route):
        submitted = route.request.post_data_json
        writes.append(submitted)
        work['version'] += 1
        work['coordinator_epoch'] += 1
        work['intent_revision'] += 1
        work['recovery'].update(coordinator_epoch=work['coordinator_epoch'],
            intent_revision=work['intent_revision'])
        if submitted['action'] == 'restore_binding':
            work['recovery']['restore_candidates'] = []
        elif submitted['action'] == 'takeover':
            work['coordinator_member_id'] = submitted['target_member_id']
            work['recovery'].update(coordinator_member_id=submitted['target_member_id'],
                coordinator_name=candidate['name'], takeover_candidates=[])
            work['allowed_actions'] = ['cancel']
        route.fulfill(status=200, json={'work': deepcopy(work), 'operations': deepcopy(work['operations']), 'revision': 1})
    try:
        sign_in(page, stack)
        create_room(page, '原工作恢复验收')
        page.locator('#cr-message').fill('先检查问题，再确认结果')
        page.locator('.cr-send').click()
        origin = page.locator('.cr-message').filter(has_text='先检查问题，再确认结果')
        expect(origin).to_be_visible()
        source_id = origin.get_attribute('data-message-id')
        work['original_source_message_id'] = source_id
        work['source_message_id'] = 'later-instruction'
        page.route('**/api/collaboration/rooms/*/messages?*', projection)
        page.route('**/api/collaboration/work/original-goal',
            lambda route: route.fulfill(status=200, json={'work': deepcopy(work), 'revision': 1, 'read_only': True}))
        page.route('**/api/collaboration/work/original-goal/control', control)
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        timeline_work = origin.locator('[data-cr-source-works] [data-id="original-goal"]')
        expect(timeline_work).to_have_attribute('data-work-state', 'blocked')
        expect(timeline_work).to_contain_text('原工作等待恢复')
        page.locator('[data-cr-action="work-list"]').click()
        card = page.locator('[data-cr-work-list-view] [data-work-id="original-goal"]')
        expect(card).to_be_visible()
        card.locator('[data-cr-action="work-detail"]').click()
        expect(page.locator('[data-cr-work-view]')).to_contain_text('核对原操作回执后完成检查')
        expect(page.locator('[data-cr-work-view]')).to_contain_text('原检查步骤')
        expect(page.locator('[data-operation-id="original-operation"]')).to_contain_text('原操作已结束')
        restore = page.locator('[data-cr-form="work-recovery"][data-work-action="restore_binding"]')
        takeover = page.locator('[data-cr-form="work-recovery"][data-work-action="takeover"]')
        assert restore.locator('option:not([value=""])').count() == 1
        assert takeover.locator('option:not([value=""])').count() == 1
        restore.locator('button[type="submit"]').click()
        assert writes == []
        restore.locator('select').select_option(candidate['member_id'])
        expect(restore.locator('[data-cr-recovery-scope]')).to_contain_text('读取')

        # A late change of coordinator/intent/generation invalidates the old
        # choice. Polling must not silently submit it with newer credentials.
        work['version'] = 6
        work['intent_revision'] = 3
        work['coordinator_epoch'] = 4
        work['recovery'].update(coordinator_epoch=4, intent_revision=3)
        work['recovery']['restore_candidates'][0]['binding_generation'] = 3
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('重新选择成员')
        expect(restore.locator('select')).to_have_value('')
        restore.locator('button[type="submit"]').click()
        assert writes == []
        restore.locator('select').select_option(candidate['member_id'])
        restore.locator('button[type="submit"]').click()
        expect(restore).to_have_count(0)
        assert writes[0]['action'] == 'restore_binding'
        assert writes[0]['target_member_id'] == candidate['member_id']
        assert writes[0]['expected_version'] == 6
        assert writes[0]['expected_coordinator_epoch'] == 4
        assert writes[0]['expected_intent_revision'] == 3
        expect(page.locator('[data-cr-work-coordinator]')).to_contain_text('原主责')

        takeover.locator('select').select_option(candidate['member_id'])
        takeover.locator('button[type="submit"]').click()
        expect(takeover).to_have_count(0)
        assert len(writes) == 2
        assert writes[1]['action'] == 'takeover' and writes[1]['expected_version'] == 7
        assert writes[1]['target_member_id'] == candidate['member_id']
        assert writes[1]['expected_coordinator_epoch'] == 5
        assert writes[1]['expected_intent_revision'] == 4
        assert writes[0]['idempotency_key'] != writes[1]['idempotency_key']
        expect(page.locator('[data-cr-work-coordinator]')).to_contain_text(candidate['name'])
        expect(page.locator('[data-operation-id="original-operation"]')).to_have_count(1)
        expect(page.locator('[data-cr-work-view]')).to_contain_text('核对原操作回执后完成检查')

        # Explicitly synthetic transport projection, independent of business
        # completion. Inspecting either receipt never sends or reruns anything.
        checked_parts = []

        def preview_delivery(route):
            selection = route.request.post_data_json
            assert selection == {'action': 'confirm_received', 'part_ids': ['unknown-part']}
            route.fulfill(status=200, json={'delivery_id': 'original-delivery', 'delivery_version': 3,
                'ordering_revision': 2, 'preview_digest': 'original-scope-digest', 'affected_count': 1,
                'affected_parts': [{'delivery_id': 'original-delivery', 'part_id': 'unknown-part',
                                    'ordinal': 1, 'state': 'unknown'}]})

        def confirm_delivery(route):
            checked_parts.append(route.request.post_data_json)
            delivery = work['channel_deliveries'][0]
            delivery['state'] = 'accepted'
            delivery['parts'][1]['state'] = 'owner_confirmed'
            route.fulfill(status=200, json={'delivery_id': 'original-delivery', 'affected_count': 1})

        page.route('**/weixin-deliveries/original-delivery/control-preview', preview_delivery)
        page.route('**/weixin-deliveries/original-delivery/control', confirm_delivery)
        work['version'] += 1
        work.update(state='completed', allowed_actions=[],
            display={'title': '工作已完成', 'reason': '原结果已经保存'},
            channel_deliveries=[{'id': 'original-delivery', 'message_id': 'original-final',
                'channel': 'weixin', 'state': 'needs_attention', 'version': 3, 'ordering_revision': 2,
                'provider_delivery_verified': False,
                'read_verified': False, 'parts': [
                    {'id': 'accepted-part', 'ordinal': 0, 'kind': 'text', 'state': 'accepted', 'attempts': 1},
                    {'id': 'unknown-part', 'ordinal': 1, 'kind': 'image', 'state': 'unknown',
                     'reason_code': 'provider_response_unknown', 'attempts': 1}]}],
            delivery_display={'state': 'unknown', 'title': '已完成，微信回复发送待核对',
                'reason': '核对原分项前不会自动重发或重跑工作。'})
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-work-view] .cr-flow-state h3')).to_contain_text('完成')
        details = page.locator('[data-cr-work-view] [data-delivery-id="original-delivery"]')
        expect(details).to_contain_text('微信接口已受理 1 段，1 段待核对')
        details.locator('summary').click()
        expect(details.locator('[data-delivery-part="unknown-part"]')).to_contain_text('发送结果待确认')
        expect(details.locator('[data-delivery-part="accepted-part"]')).to_contain_text('已受理')
        expect(details).not_to_contain_text('已送达')
        expect(page.locator('[data-cr-work-view] [data-cr-action="work-control"]')).to_have_count(0)
        expect(page.locator('[data-message-id="original-final"] [data-delivery-id="original-delivery"]')).to_have_count(1)
        expect(timeline_work).to_have_attribute('data-work-state', 'completed')
        expect(details.locator('[data-delivery-part="accepted-part"] [data-cr-action="preview-delivery-control"]')).to_have_count(0)
        details.get_by_role('button', name='核对这一段').click()
        expect(details.locator('[data-cr-delivery-control-preview]')).to_contain_text('原回复 · 第 2 段')
        assert checked_parts == []
        details.get_by_role('button', name='确认已收到所选分项').click()
        expect(page.locator('[data-message-id="original-final"]')).to_contain_text('本人已核对收到 1 段')
        assert len(checked_parts) == 1
        assert checked_parts[0]['part_ids'] == ['unknown-part']
        assert checked_parts[0]['expected_version'] == 3
        assert checked_parts[0]['expected_ordering_revision'] == 2
        assert checked_parts[0]['preview_digest'] == 'original-scope-digest'
        page.locator('[data-cr-action="reply-work"]').click()
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        expect(page.locator('[data-cr-reply]')).to_contain_text('先检查问题，再确认结果')
        page.locator('#cr-message').fill('最后为什么这样处理？')
        with page.expect_request(
            lambda request: request.method == 'POST' and '/messages' in request.url
        ) as submitted:
            page.locator('.cr-send').click()
        payload = submitted.value.post_data_json
        assert payload['reply_to_id'] == source_id
        assert 'work_id' not in payload and 'action' not in payload
        expect(page.locator('.cr-message').filter(has_text='最后为什么这样处理？')).to_be_visible()
        assert len(writes) == 2
    finally:
        # Drain projection route.fetch()/response.json() before closing their
        # request context. Otherwise a poll can race teardown in WebKit and
        # surface Response has been disposed from the event listener.
        page.unroute_all(behavior="wait")
        context.close()
