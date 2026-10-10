"""No panel mode: ordinary answer, interim reply, real Agent work, then discussion.

The dot decision is an explicit test consumer, not proof of native-model autonomy.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_collaboration_delivery_flow import login
from tests.test_collaboration_dot_chat_flow import refresh
from tests.test_collaboration_dots_flow import task_stack as task_stack, mcp, call, join_dot, complete_real_task
from tests.test_collaboration_join_browser import fixture_host

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('webkit', 390)])
def test_one_composer_interleaves_explanation_real_work_and_followup(task_stack, chat_browser_pool, engine, width):
    stack = task_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    errors, submissions = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: submissions.append(request.post_data_json)
        if request.method == 'POST' and request.url.endswith('/api/collaboration/message') else None)
    try:
        login(page, stack)
        page.locator('[data-cc-open-joins]').click()
        form = page.locator('#cc-dot-create')
        expect(form.locator('[name="access"]')).not_to_be_visible()
        expect(form.locator('[name="confirm_tasks"]')).to_have_count(0)
        expect(form.locator('.cc-dot-default-scope')).to_contain_text('读取、修改文件、运行命令')
        expect(form.locator('.cc-dot-consent')).to_contain_text('点击添加，即允许')
        form.locator('[name="label"]').fill('对话助手')
        form.locator('button[type="submit"]').click()
        card = page.locator('#cc-drawer [data-dot-card]')
        expect(card).to_be_visible()
        joined = join_dot(stack, {'join_code': card.locator('.cc-join-code').inner_text()})
        assert joined['slot']['capabilities'] == ['read', 'write', 'execute']
        assert joined['consumer_configuration']['panel_mode_switch_required'] is False
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        refresh(page)
        page.locator('.cc-dot-card [data-cc-action="dot-select"]').click()
        area = page.locator('#cc-message-input')
        expect(area).to_be_enabled()
        expect(page.locator('#cc-command [name="delegation_policy"]')).to_have_count(0)
        expect(page.locator('#cc-task-compose-tools')).not_to_be_visible()
        expect(page.locator('#cc-command [name="delegation_acceptance"]')).to_have_count(0)

        area.fill('能说说你准备怎么验证文件吗？先不要动代码。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        first = call(stack, joined['inbox_request'])['items'][0]
        mcp(stack, 'collaboration', {'action': 'dot_message', **first['reply_arguments'],
            'body_text': '先写测试文件，再读回，最后运行命令核对。'})
        refresh(page)
        answer = page.locator('.cc-message').filter(has_text='先写测试文件，再读回，最后运行命令核对。')
        expect(answer.locator('[data-cc-action="convert"]')).to_have_count(0)
        answer.locator('[data-cc-action="reply"]').click()
        area.fill('可以，按刚才的步骤做，先告诉我再执行。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        requested = call(stack, joined['inbox_request'])['items'][0]
        assert requested['message']['thread_root_id'] == first['message_id']
        history = call(stack, requested['thread_request'])
        assert history['items']  # The consumer reads the actual topic to resolve "刚才".
        interim = {'action': 'dot_message', **requested['interim_reply_arguments'],
            'body_text': '收到，我按刚才的三个步骤检查。'}
        posted = mcp(stack, 'collaboration', interim)
        assert mcp(stack, 'collaboration', interim)['message']['id'] == posted['message']['id']
        pending = call(stack, joined['inbox_request'])['items'][0]
        assert pending['message_id'] == requested['message_id'] and pending['state'] == 'read'
        task = call(stack, pending['task_request'])
        assert call(stack, pending['task_request'])['delegation_id'] == task['delegation_id']
        operations = complete_real_task(stack, joined, task['delegation_id'], 1)
        assert len(set(operations)) == 3
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('第 1 项已完成，文件创建、读取和命令回执一致。')
        expect(page.locator('#cc-command [name="delegation_policy"]')).to_have_count(0)
        expect(page.locator('#cc-task-compose-tools')).not_to_be_visible()
        result = page.locator('.cc-message').filter(has_text='第 1 项已完成，文件创建、读取和命令回执一致。')
        result.locator('[data-cc-action="reply"]').click()
        area.fill('谢谢，解释一下刚才的检查结果就好，不用再执行。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)
        final = call(stack, joined['inbox_request'])['items'][0]
        mcp(stack, 'collaboration', {'action': 'dot_message', **final['reply_arguments'],
            'body_text': '文件内容、读回内容和命令输出一致，这次只作解释。'})
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('这次只作解释。')
        store = Store(stack.hubdir)
        try:
            assert store.one('SELECT COUNT(*) AS n FROM delegation_requests')['n'] == 1
            assert store.one('SELECT COUNT(*) AS n FROM coordination_operations')['n'] == 3
        finally:
            store.close()
        assert len(submissions) == 3
        assert all(not payload.get('delegation') and not payload.get('dispatch_mode') and not payload.get('automatic_policy_version') for payload in submissions)
        assert not call(stack, joined['inbox_request'])['items']
        assert not errors, errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        screenshots = Path('.work/dot-refactor/screenshots')
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / f'seamless-{engine}-{width}.png'), full_page=True)
    finally:
        context.close()
