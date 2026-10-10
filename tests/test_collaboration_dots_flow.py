"""Real panel / MCP / Hub / Agent loop; the external dot host is a fixture.

Browser clicks and file operations are real. Native-model autonomy is deliberately
not inferred from a verified webhook, a fixture receiver, or a synthetic consumer.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from shared.util import atomic_json
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_delivery_flow import login, refresh, scope
from tests.test_collaboration_join_browser import fixture_host
from tests.test_mcp_tasks_http import modern

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def task_stack(collaboration_stack):
    # Only the disposable test Agent enables shell, never a user or production Agent.
    stack = collaboration_stack
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': ['ProjectAlpha'], 'command': ['/bin/sh', '-c']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    return stack


def mcp(stack, tool, arguments):
    envelope = modern(stack, 'tools/call', {'name': tool, 'arguments': arguments}, token=stack.pat).json()
    assert 'error' not in envelope, envelope
    result = envelope['result']
    assert not result.get('isError'), result
    return result['structuredContent']


def call(stack, request):
    return mcp(stack, request['tool'], request['arguments'])


def overview(stack):
    return stack.must(stack.client.get('/api/collaboration', params=scope(stack)))


def create_dot(stack, label='真实文件任务 dot'):
    return stack.must(stack.client.post('/api/collaboration/dot', json={
        **scope(stack), 'label': label, 'confirm_tasks': True,
        'capabilities': ['read', 'write', 'execute'], 'acknowledge_unsandboxed_exec': True,
        'idempotency_key': key()}))['dot']


def join_dot(stack, slot):
    result = mcp(stack, 'collaboration', {'action': 'join', 'code': slot['join_code'], 'idempotency_key': key()})
    assert result['registered'] and result['subscription_requests'] == [result['subscription_request']]
    assert not result['subscription_created'] and not result['permissions_changed']
    return result


def send(stack, joined, text):
    slot = joined['slot']
    payload = {**scope(stack), 'conversation_id': slot['conversation_id'],
        'room_id': overview(stack)['room']['id'], 'body_text': text,
        'mentions': [{'slot_id': slot['id']}], 'dispatch_mode': 'automatic',
        'automatic_policy_version': slot['policy_version'], 'client_message_id': key(), 'idempotency_key': key()}
    result = stack.must(stack.client.post('/api/collaboration/message', json=payload))
    assert stack.must(stack.client.post('/api/collaboration/message', json=payload))['delegation_id'] == result['delegation_id']
    return result


def complete_real_task(stack, joined, delegation_id, number):
    page = call(stack, joined['inbox_request'])
    item = next(row for row in page['items'] if row['delegation_id'] == delegation_id)
    assert item['category'] == 'claimable'
    trusted = call(stack, item['read_request'])
    assert trusted['trusted_author']['authenticated']
    work = call(stack, item['claim_request'])['work_item']
    assert call(stack, item['claim_request'])['work_item']['attempt'] == work['attempt']
    lease = {**scope(stack), 'goal_id': work['goal_id'], 'work_item_id': work['id'],
             'attempt': work['attempt'], 'fencing_token': work['fencing_token']}
    progress = {'action': 'progress', **lease, 'summary': f'正在执行第 {number} 项文件检查。', 'idempotency_key': key()}
    assert mcp(stack, 'collaboration_work', progress)['message_id'] == mcp(stack, 'collaboration_work', progress)['message_id']
    filename, content = f'dot-proof-{number}.txt', f'real-agent-proof-{number}\n'
    operations = []
    steps = [('write', {'path': filename, 'content': content, 'expected_sha256': 'new'}),
             ('read', {'path': filename}), ('exec', {'command': f'cat {filename}', 'timeout_seconds': 15})]
    for tool, arguments in steps:
        request = {'action': 'execute', **lease, 'tool': tool, 'arguments': arguments, 'idempotency_key': key()}
        executed = mcp(stack, 'collaboration_work', request)
        operation = executed['operation_id']
        assert mcp(stack, 'collaboration_work', request)['operation_id'] == operation
        settled = stack.poll(operation, timeout=30)
        assert settled['state'] == 'succeeded', (settled.get('error'), settled.get('output'), settled.get('result'))
        if tool == 'exec':
            assert settled['result']['data']['exit_code'] == 0
            assert content.strip() in settled['output']
        operations.append(operation)
    assert (Path(stack.project['root']) / filename).read_text() == content
    result = {'action': 'result', **lease, 'outcome': 'succeeded',
        'summary': f'第 {number} 项已完成，文件创建、读取和命令回执一致。',
        'operation_ids': operations, 'idempotency_key': key()}
    mcp(stack, 'collaboration_work', result)
    mcp(stack, 'collaboration_work', result)
    return operations


def test_one_code_real_agent_two_tasks_restart_and_original_thread_results(task_stack):
    stack = task_stack
    dot = create_dot(stack)
    joined = join_dot(stack, dot)
    fixture_host(stack, joined['slot'], joined['subscription_requests'])
    first = send(stack, joined, '创建第一个验证文件并核对内容。')
    # Neither a new connection lookup nor restarting the actual Hub may reset
    # the server enrollment checkpoint and silently classify new work as history.
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.start_hub()
    restored = mcp(stack, 'collaboration_query', {'action': 'dot_connection', **scope(stack), 'dot_id': dot['id']})
    assert restored['inbox_request'] == joined['inbox_request']
    second = send(stack, restored, '再创建第二个验证文件并回报，不重复第一项。')
    first_ops = complete_real_task(stack, restored, first['delegation_id'], 1)
    second_ops = complete_real_task(stack, restored, second['delegation_id'], 2)
    assert len(set(first_ops + second_ops)) == 6
    messages = overview(stack)['messages']
    for source in (first, second):
        replies = [message for message in messages if message.get('reply_to_id') == source['message']['id']]
        assert len(replies) == 3
        finals = [row for row in replies if row['kind'] == 'delegation_result']
        assert len(finals) == 1 and finals[0]['display_name'] == dot['label']
        assert finals[0]['body']['execution_verified']
    final = call(stack, restored['inbox_request'])
    assert all(row['category'] != 'claimable' for row in final['items'])


@pytest.mark.browser
@pytest.mark.parametrize('engine,width', [('chromium', 1440), ('chromium', 390), ('webkit', 390)])
def test_panel_add_dot_type_mention_continue_thread_execute_and_report(
        task_stack, chat_browser_pool, engine, width, tmp_path):
    stack = task_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 920})
    page = context.new_page()
    errors, saved = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: saved.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/collaboration/message') else None)
    try:
        login(page, stack)
        page.locator('[data-cc-open-joins]').click()
        form = page.locator('#cc-dot-create')
        expect(form).to_be_visible()
        expect(page.locator('#cc-delegation-policy')).to_have_count(0)
        label = '项目 dot'
        form.locator('[name="label"]').fill(label)
        expect(form.locator('[name="confirm_tasks"]')).not_to_be_checked()
        form.locator('[name="confirm_tasks"]').check()
        form.locator('button[type="submit"]').click()
        card = page.locator('#cc-drawer [data-dot-card]')
        expect(card).to_be_visible()
        code = card.locator('.cc-join-code').inner_text()
        assert code.startswith('CPD-')
        instruction = card.locator('.cc-join-instruction').input_value()
        assert code in instruction and '任务通过插件执行' in instruction
        assert '加入不授权领取或执行任务' not in instruction
        page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {configurable: true,
            value: {writeText: async text => {window.dotCopied = text;}}})""")
        card.locator('[data-cc-action="join-copy"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('已复制')
        assert page.evaluate('window.dotCopied') == instruction
        directory = Path('.work/dot-refactor/screenshots')
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f'dot-join-{engine}-{width}.png'), full_page=True)
        joined = join_dot(stack, {'join_code': code})
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        refresh(page)
        page.locator('.cc-dot-card [data-cc-action="dot-select"]').click()
        expect(page.locator('#cc-send-mode')).to_contain_text('交给 @项目 dot')
        page.locator('[data-cc-action="dot-clear"]').click()
        area = page.locator('#cc-message-input')
        expect(area).to_be_enabled()
        area.fill('@项目')
        expect(page.locator('#cc-dot-suggestions')).to_be_visible()
        area.press('Enter')
        expect(page.locator('#cc-dot-suggestions')).not_to_be_visible()
        expect(page.locator('.cc-mention-chip')).to_contain_text('@项目 dot')
        expect(page.locator('#cc-send-mode')).to_contain_text('交给 @项目 dot')
        assert not saved  # Enter selected a dot; it did not submit an accidental task.
        area.fill('请创建文件并核对实际内容。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        expect(page.locator('.cc-mention-chip')).to_contain_text('@项目 dot')
        assert len(saved) == 1 and saved[0]['dispatch_mode'] == 'automatic'
        first = next(row for row in overview(stack)['messages'] if row.get('client_message_id') == saved[0]['client_message_id'])
        complete_real_task(stack, joined, first['body']['delegation']['delegation_id'], 1)
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('第 1 项已完成，文件创建、读取和命令回执一致。')
        expect(page.locator('.cc-message-list')).to_contain_text('正在执行第 1 项文件检查。')
        expect(page.locator('.cc-message').filter(has_text='第 1 项已完成，文件创建、读取和命令回执一致。').locator('.cc-message-meta strong')).to_have_text(label)
        # Reply in the original topic is an explicit follow-up task for CPD dots.
        page.locator(f'[data-cc-action="reply"][data-id="{first["id"]}"]').click()
        expect(page.locator('.cc-reply-preview')).to_be_visible()
        expect(page.locator('#cc-send-mode')).to_contain_text('交给 @项目 dot')
        area.fill('继续，在同一话题创建第二个文件。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        assert len(saved) == 2 and saved[1]['reply_to_id'] == first['id']
        assert saved[1]['delegation']['policy_id'] == joined['slot']['policy_id']
        followup = next(row for row in overview(stack)['messages'] if row.get('client_message_id') == saved[1]['client_message_id'])
        complete_real_task(stack, joined, followup['body']['delegation']['delegation_id'], 2)
        refresh(page)
        expect(page.locator('.cc-message-list')).to_contain_text('第 2 项已完成，文件创建、读取和命令回执一致。')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(directory / f'dot-results-{engine}-{width}.png'), full_page=True)
        page.locator('[data-cc-action="dot-clear"]').click()
        expect(page.locator('.cc-mention-chip')).to_have_count(0)
        area.fill('这是一条普通讨论，不要执行。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        assert len(saved) == 3 and not saved[2].get('delegation') and not saved[2].get('dispatch_mode')
        assert not errors, errors
    finally:
        context.close()
