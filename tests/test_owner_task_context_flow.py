"""Real browser + temporary Hub/Agent; the Dot is a labelled test consumer.

No production channel or native-host approval is exercised or inferred here.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_delivery_flow import login
from tests.test_collaboration_dot_chat_flow import refresh
from tests.test_collaboration_dots_flow import task_stack as task_stack, mcp, call, join_dot, complete_real_task
from tests.test_collaboration_join_browser import fixture_host

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_plan_reply_resumes_original_work_and_clear_next_request_needs_no_question(
        task_stack, chat_browser_pool, engine, width):
    stack = task_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        page.locator('[data-cc-open-joins]').click()
        form = page.locator('#cc-dot-create')
        form.locator('[name="label"]').fill('原渠道任务助手')
        form.locator('button[type="submit"]').click()
        card = page.locator('#cc-drawer [data-dot-card]')
        expect(card).to_be_visible()
        joined = join_dot(stack, {'join_code': card.locator('.cc-join-code').inner_text()})
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        refresh(page)
        page.locator('.cc-workspace-nav [data-cc-view="agents"]').click()
        page.locator('#cc-workspace-panel [data-dot-card] .cc-dot-primary [data-cc-action="dot-select"]').click()
        area = page.locator('#cc-message-input')
        expect(area).to_be_visible()
        expect(area).to_be_enabled()
        area.fill('验证读写和只读命令，先说明要创建的独立测试文件。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        first = call(stack, joined['inbox_request'])['items'][0]
        assert first['execution_route']['desktop_access_required'] is False
        # Already queued before a genuine clarification. Confirmation must not
        # manufacture a second goal, work item or execution attempt.
        original = call(stack, first['task_request'])
        detail = call(stack, original['read_request'])
        question = mcp(stack, 'collaboration', {'action': 'dot_message',
            **first['interim_reply_arguments'], 'idempotency_key': key(),
            'body_text': '仅创建 dot-proof-1.txt，读回后运行 cat 校验，保留文件，不修改业务代码。按此范围继续吗？',
            'response_request': {**detail['response_request_options'], 'reason': 'plan_confirmation'}})
        refresh(page)
        question_card = page.locator(f'[data-message-id="{question["message"]["id"]}"] .cc-task-context-card')
        expect(question_card).to_contain_text('等待你确认这份方案')
        expect(question_card).to_contain_text('直接在这里回复即可')
        expect(question_card.locator('[data-cc-action="reply"]')).to_be_visible()
        assert '请在原 dot 界面查看真实请求' not in page.locator('.cc-message-list').inner_text()
        shots = Path('.work/owner-task-context/screenshots')
        shots.mkdir(parents=True, exist_ok=True)
        question_card.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / f'question-{engine}-{width}.png'), full_page=True, animations='disabled')
        # Restart only this disposable test Hub. The plan, original task and
        # response binding must survive without new enrollment or permissions.
        stack.hub.terminate()
        stack.hub.wait(timeout=12)
        stack.start_hub()
        recovered = call(stack, original['read_request'])
        assert recovered['delegation']['id'] == original['delegation_id']
        question_card.locator('[data-cc-action="reply"]').click()
        area.fill('可以')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        reply = next(i for i in call(stack, joined['inbox_request'])['items'] if i['message']['body_text'] == '可以')
        assert reply['message']['reply_to_id'] == question['message']['id']
        assert reply['task_continuation']['host_approval_satisfied'] is False
        resumed = call(stack, reply['task_request'])
        assert resumed['delegation_id'] == original['delegation_id']
        assert call(stack, reply['task_request'])['delegation_id'] == original['delegation_id']
        assert not call(stack, joined['inbox_request'])['items']
        # The answer must update the existing card without a manual page refresh.
        expect(question_card).to_contain_text('已确认这份方案', timeout=15000)
        operations = complete_real_task(stack, joined, original['delegation_id'], 1)
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('第 1 项已完成，文件创建、读取和命令回执一致。')
        expect(question_card).to_contain_text('已确认这份方案')
        expect(question_card.locator('[data-cc-action="reply"]')).to_have_count(0)
        page.screenshot(path=str(shots / f'completed-{engine}-{width}.png'), full_page=True, animations='disabled')
        # A fully specified follow-up runs directly through the existing policy;
        # the new clarification capability is not a mandatory confirmation step.
        area.fill('另建 dot-proof-2.txt，写入 real-agent-proof-2 后读回并运行 cat，只处理这个测试文件。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        second = call(stack, joined['inbox_request'])['items'][0]
        assert second['task_continuation'] is None
        next_task = call(stack, second['task_request'])
        operations += complete_real_task(stack, joined, next_task['delegation_id'], 2)
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('第 2 项已完成')
        store = Store(stack.hubdir)
        try:
            assert store.one('SELECT COUNT(*) AS n FROM delegation_requests')['n'] == 2
            assert store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == 2
            assert store.one('SELECT COUNT(*) AS n FROM coordination_operations')['n'] == 6
            assert store.one('SELECT COUNT(*) AS n FROM dot_task_questions')['n'] == 1
        finally:
            store.close()
        assert len(set(operations)) == 6
        assert not errors, errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    except BaseException:
        shots = Path('.work/owner-task-context/screenshots')
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / f'failure-{engine}-{width}.png'), full_page=True)
        raise
    finally:
        context.close()


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_continue_conversation_is_not_swallowed_by_pending_thread_read(
        task_stack, chat_browser_pool, engine, width):
    import time
    from urllib.parse import parse_qs, urlparse
    stack = task_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    held, released, seen = [], False, []
    def delay_thread(route):
        request = route.request
        url = urlparse(request.url)
        try:
            body = request.post_data_json or {}
        except Exception:
            body = {}
        seen.append({'method': request.method, 'path': url.path, 'query': url.query, 'body': body})
        if ('thread' in url.path.split('/') or parse_qs(url.query).get('kind') == ['thread']
                or body.get('kind') == 'thread'):
            held.append(route)
            return
        route.continue_()
    try:
        login(page, stack)
        page.locator('[data-cc-open-joins]').click()
        form = page.locator('#cc-dot-create')
        form.locator('[name="label"]').fill('导航测试助手')
        form.locator('button[type="submit"]').click()
        card = page.locator('#cc-drawer [data-dot-card]')
        expect(card).to_be_visible()
        joined = join_dot(stack, {'join_code': card.locator('.cc-join-code').inner_text()})
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        scope = joined['inbox_request']['arguments']
        publication = mcp(stack, 'collaboration', {'action': 'dot_message',
            'project': scope['project'], 'environment_id': scope['environment_id'],
            'dot_id': scope['dot_id'], 'body_text': '这是一条合成导航测试说明，不执行工作。',
            'idempotency_key': key()})
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        refresh(page)
        page.locator('.cc-workspace-nav [data-cc-view="discussion"]').click()
        expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy', 'true')
        page.route('**/api/**', delay_thread)
        page.locator(f'[data-message-id="{publication["message"]["id"]}"] [data-cc-action="thread"]').click()
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert held, ('The test must hold the actual thread read, not merely simulate a busy flag', seen)
        expect(page.locator('.collaboration')).to_have_attribute('aria-busy', 'true')
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        page.locator('.cc-workspace-nav [data-cc-view="agents"]').click()
        select = page.locator('#cc-workspace-panel [data-dot-card] .cc-dot-primary [data-cc-action="dot-select"]')
        expect(select).not_to_have_attribute('aria-disabled', 'true')
        select.click()
        expect(page.locator('#cc-message-input')).to_be_visible()
        expect(page.locator('.collaboration')).to_have_attribute('data-workspace-view', 'discussion')
        assert held, 'Navigation is verified before the old read is released'
        for route in held:
            route.continue_()
        released = True
        expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy', 'true')
        expect(page.locator('#cc-message-input')).to_be_visible()
        store = Store(stack.hubdir)
        try:
            assert store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == 0
        finally:
            store.close()
    finally:
        if not released:
            for route in held:
                try:
                    route.abort()
                except Exception:
                    pass
        context.close()
