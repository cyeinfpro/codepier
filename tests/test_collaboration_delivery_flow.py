"""Delivery UX against an isolated Hub and deterministic event-status projections.

The status projection tests do not contact a native host or prove model execution.
"""
import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from shared.mcp_protocol import MODERN, PREFIX, request_headers
from shared.public_collaboration import request as public_collaboration_request
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.service import CollaborationService
from hub.runtime import Runtime
from hub.store import Store
from tests.collaboration_support import collaboration_stack, key  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def screenshot(page, name, tmp_path):
    directory = Path(os.getenv('CODEPIER_COLLABORATION_DELIVERY_SCREENSHOTS', str(tmp_path / 'screenshots')))
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), animations='disabled', full_page=True)


def scope(stack):
    return {'project': stack.project['id'], 'environment_id': 'production'}


def login(page, stack):
    page.goto(stack.url + '/#projects')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('项目映射')
    page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
    page.locator('[data-cc-action="create-room"]').click()
    expect(page.locator('#cc-message-input')).to_be_enabled()


def refresh(page):
    if not page.locator('[data-cc-action="refresh"]').is_visible():
        page.locator('[data-cc-more]').click()
    page.locator('[data-cc-action="refresh"]').click()


def join_slot(stack):
    slot = stack.must(stack.client.post('/api/collaboration/join-slot', json={
        **scope(stack), 'label': '协作 dot', 'kind': 'dot', 'idempotency_key': key()}))['slot']
    params = {'name': 'collaboration_join', 'arguments': {
        'code': slot['join_code'], 'idempotency_key': key()},
        '_meta': {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}}
    body = {'jsonrpc': '2.0', 'id': key(), 'method': 'tools/call', 'params': params}
    result = stack.must(stack.client.post('/mcp', json=body, headers={
        'Authorization': 'Bearer ' + stack.pat, 'Accept': 'application/json, text/event-stream',
        **request_headers(body)}))['result']
    assert not result.get('isError'), result
    return slot


def messages(stack):
    return stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['messages']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_saved_message_survives_following_read_failure(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    broken = [False]
    try:
        login(page, stack)

        def reads(route):
            kind = parse_qs(urlsplit(route.request.url).query).get('kind', [''])[0]
            if broken[0] and kind in {'timeline', 'overview'}:
                route.fulfill(status=503, content_type='application/json',
                              body=json.dumps({'error': {'code': 'UNAVAILABLE', 'message': 'fixture read failed'}}))
            else:
                route.continue_()

        def saved(route):
            response = route.fetch()
            assert response.ok
            broken[0] = True
            route.fulfill(response=response)

        page.route('**/api/collaboration?*', reads)
        page.route('**/api/collaboration/message', saved)
        page.locator('#cc-message-input').fill('成功保存后读取中断')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-message-list')).to_contain_text('成功保存后读取中断')
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('#cc-feedback')).to_contain_text('消息已保存')
        expect(page.locator('#cc-feedback')).to_contain_text('请勿重发正文')
        assert len(messages(stack)) == 1
        page.locator('#cc-message-input').fill('下一条仍保留的草稿')
        broken[0] = False
        refresh(page)
        expect(page.locator('#cc-message-input')).to_have_value('下一条仍保留的草稿')
        expect(page.locator('.cc-message-list .cc-message')).to_have_count(1)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_member_routes_refresh_without_losing_picker_or_draft(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    connected = [False]
    try:
        login(page, stack)
        slot = join_slot(stack)
        refresh(page)
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')

        def projection(route):
            response = route.fetch()
            value = response.json()
            if connected[0]:
                for member in value.get('items', []):
                    member['message_notification_state'] = 'active'
                    member['can_speak'] = True
            route.fulfill(response=response, json=value)

        page.route('**/api/collaboration?*kind=members*', projection)
        area = page.locator('#cc-message-input')
        area.fill('接通时保留正文')
        page.locator('[data-cc-action="mentions"]').click()
        option = page.locator('[data-cc-action="pick-mention"]')
        expect(option).to_contain_text('尚未订阅此房间')
        option.click()
        option.evaluate('(n) => {n.dataset.retained = "yes"; n.focus();}')
        connected[0] = True
        expect(option).to_contain_text('房间提醒可投递', timeout=12000)
        expect(option).to_contain_text('已允许回帖')
        expect(option).to_have_attribute('data-retained', 'yes')
        expect(option).to_have_attribute('aria-pressed', 'true')
        expect(option).to_be_focused()
        page.keyboard.press('Escape')
        expect(area).to_have_value('接通时保留正文')
        expect(page.locator('.cc-mention-chip')).to_contain_text('协作 dot')
        assert messages(stack) == []
        assert slot['id']
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_failed_mentions_persist_and_open_exact_repair_without_resending(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 360, 'height': 800})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('#cc-message-input').fill('只保存一次的提醒')
        page.locator('#cc-command button[type="submit"]').click()
        receipt = page.locator('.cc-message-deliveries li')
        expect(receipt).to_have_attribute('data-delivery-state', 'not_subscribed')
        expect(receipt).to_contain_text('未送达')
        page.set_viewport_size({'width': 1440, 'height': 900})
        screenshot(page, engine + '-mention-unconnected-desktop.png', tmp_path)
        page.set_viewport_size({'width': 360, 'height': 800})
        page.locator('#cc-message-input').fill('接通后继续写的草稿')
        receipt.locator('[data-cc-action="mention-connect"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('接通 协作 dot')
        page.locator('#cc-drawer summary').filter(has_text='普通讨论提醒与回帖').click()
        expect(page.locator('.cc-mention-instruction')).to_be_visible()
        expect(page.locator('.cc-mention-instruction')).to_contain_text(slot['id'])
        expect(page.locator('.cc-mention-instruction')).to_contain_text('message_mentioned.v1')
        page.keyboard.press('Escape')
        expect(page.locator('#cc-message-input')).to_have_value('接通后继续写的草稿')
        page.reload()
        expect(receipt).to_have_attribute('data-delivery-state', 'not_subscribed')
        assert len(messages(stack)) == 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_explicit_policy_and_delegation_keep_plain_chat_inert(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('#cc-message-input').fill('委托表单期间仍保留的正文')
        page.locator('#cc-send-mode [data-cc-action="delegation-setup"]').click()
        form = page.locator('#cc-delegation-policy')
        expect(form).to_be_visible()
        expect(form.locator('[name="execution_targets"]:checked')).to_have_count(0)
        expect(form.locator('[name="capabilities"][value="read"]')).to_be_checked()
        expect(form.locator('[name="capabilities"][value="execute"]')).not_to_be_checked()
        expect(form.locator('.cc-exec-boundary')).not_to_be_visible()
        form.locator('[name="capabilities"][value="execute"]').check()
        expect(form.locator('.cc-exec-boundary')).to_be_visible()
        expect(form.locator('[name="acknowledge_unsandboxed_exec"]')).to_have_attribute('required', '')
        form.locator('[name="capabilities"][value="execute"]').uncheck()
        expect(form.locator('.cc-exec-boundary')).not_to_be_visible()
        expect(form.locator('[name="confirm"]')).not_to_be_checked()
        form.locator('[name="purpose"]').fill('读取此项目的 README 并报告现状')
        form.locator('[name="execution_targets"][value="project_agent"]').check()
        submit_box = form.locator('button[type="submit"]').bounding_box()
        assert submit_box and submit_box['y'] + submit_box['height'] <= page.viewport_size['height']
        screenshot(page, engine + '-delegation-first-approval.png', tmp_path)
        form.locator('button[type="submit"]').click()
        policies = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items']
        assert policies == []
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('[data-cc-action="delegation-copy"]')).to_be_visible()
        expect(page.locator('.cc-delegation-instruction')).not_to_be_visible()
        saved_policy = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items'][0]
        expect(page.locator('.cc-delegation-instruction')).to_have_value(
            saved_policy['consumer_contracts']['managed_execution']['instructions'])
        expect(page.locator('.cc-connection-instruction')).to_have_attribute('data-consumer-mode', 'managed_execution')
        page.keyboard.press('Escape')
        expect(page.locator('#cc-message-input')).to_have_value('委托表单期间仍保留的正文')
        expect(page.locator('[name="delegation_policy"]')).to_have_value('')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        assert len(messages(stack)) == 1
        assert not messages(stack)[0]['body'].get('delegation')

        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        policy = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items'][0]
        page.locator('[name="delegation_policy"]').select_option(policy['id'])
        expect(page.locator('.cc-delegation-context')).to_contain_text('本次发送将创建委托')
        page.locator('#cc-message-input').fill('请读取 README 并报告实际内容')
        page.locator('[data-cc-action="delegation-scope"]').click()
        page.locator('#cc-delegation-scope summary').click()
        page.locator('[name="delegation_acceptance"]').fill('总结实际读取内容与限制')
        page.locator('#cc-delegation-scope button[type="submit"]').click()
        page.locator('#cc-command button[type="submit"]').click()
        card = page.locator('.cc-delegation-message')
        expect(card).to_contain_text('任务已保存，尚未接通')
        source = [m for m in messages(stack) if m['body'].get('delegation')][0]
        goal_id = source['body']['delegation']['goal_id']
        detail = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'coordination_goal', 'id': goal_id}))
        assert detail['goal']['state'] == 'active'
        assert len(detail['work_items']) == 1
        assert detail['work_items'][0]['state'] == 'queued'
        card.locator('[data-cc-action="delegation-remind"]').click()
        expect(card).to_contain_text('任务已保存')
        latest = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'coordination_goal', 'id': goal_id}))
        assert len(latest['work_items']) == 1
        assert len(messages(stack)) == 2
        page.set_viewport_size({'width': 390, 'height': 844})
        screenshot(page, engine + '-delegation-saved-mobile.png', tmp_path)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        card.locator('[data-cc-action="delegation-result"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('步骤与结果')
        page.keyboard.press('Escape')
        page.locator('#cc-message-input').fill('后续草稿')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('[name="delegation_policy"]').select_option(policy['id'])
        stack.must(stack.client.post('/api/collaboration/delegation-policy-control', json={
            **scope(stack), 'policy_id': policy['id'], 'expected_version': policy['version'],
            'action': 'pause', 'idempotency_key': key()}))
        refresh(page)
        expect(page.locator('.cc-delegation-context')).to_contain_text('已改变、到期或不可用')
        expect(page.locator('#cc-message-input')).to_have_value('后续草稿')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('正文已保留')
        assert len(messages(stack)) == 2
        assert slot['id']
    finally:
        context.close()


def register_fixture_subscription(stack, request):
    """Create a real fixture route using deterministic challenge transport only."""
    def create():
        store = Store(stack.hubdir)
        try:
            runtime = Runtime(store)
            service = CollaborationService(runtime, CollaborationConfig(enabled=True, events_enabled=True))
            room = store.one('SELECT * FROM collaboration_rooms WHERE project_id=? AND environment_id=?',
                             (stack.project['id'], 'production'))
            principal = service.grant_reader(room, stack.grant)

            async def receive(url, body, headers):
                assert url == 'https://example.invalid/browser-fixture'
                value = json.loads(body)
                return Reply(200, json.dumps({'challenge': value['challenge']}).encode())

            service.events = EventService(service, receive)
            return asyncio.run(service.events.subscribe({
                **request, 'delivery': {
                    'mode': 'webhook', 'url': 'https://example.invalid/browser-fixture',
                    'secret': 'whsec_' + base64.b64encode(b'browser-fixture-signing-material!').decode(),
                },
            }, principal))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(create).result(timeout=20)


def message_event_count(stack, event_name, message_id):
    store = Store(stack.hubdir)
    try:
        return store.one("SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=? "
                         "AND json_extract(data,'$.message_id')=?", (event_name, message_id))['n']
    finally:
        store.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('mode', ['message', 'delegation'])
def test_retry_after_subscription_uses_new_intent_without_new_content(
        collaboration_stack, chat_browser_pool, engine, mode):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        policy = None
        if mode == 'delegation':
            policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
                **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
                'expected_version': 0, 'purpose': 'Read and summarize the isolated project.',
                'capabilities': ['read'], 'duration_seconds': 3600, 'goal_duration_seconds': 900,
                'max_delegations': 5, 'budget': {}, 'execution_targets': ['project_agent'],
                'idempotency_key': key()}))['policy']
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        if policy:
            page.locator('[name="delegation_policy"]').select_option(policy['id'])
        page.locator('#cc-message-input').fill('接通后继续派发，原文只保存一次')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        source = messages(stack)[0]
        action = 'delegation-remind' if policy else 'message-remind'
        reminder = page.locator('[data-cc-action="' + action + '"]')
        expect(reminder).to_be_visible()
        sent_keys = []
        page.on('request', lambda request: sent_keys.append(request.post_data_json['idempotency_key'])
                if request.method == 'POST' and urlsplit(request.url).path.endswith('/' + action) else None)
        reminder.click()
        expect(page.locator('#cc-feedback')).to_contain_text('已核对')
        if policy:
            request = policy['subscription_request']
        else:
            members = stack.must(stack.client.get('/api/collaboration', params={
                **scope(stack), 'kind': 'members'}))['items']
            request = next(member for member in members if member['slot_id'] == slot['id'])['message_subscription_request']
        before = message_event_count(stack, request['name'], source['id'])
        registered = register_fixture_subscription(stack, request)
        assert registered['id']
        refresh(page)
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        reminder.click()
        expect(page.locator('#cc-feedback')).to_contain_text('已核对')
        assert len(sent_keys) == 2 and sent_keys[0] != sent_keys[1]
        assert message_event_count(stack, request['name'], source['id']) > before
        assert len(messages(stack)) == 1
        if policy:
            goals = stack.must(stack.client.get('/api/collaboration', params={
                **scope(stack), 'kind': 'coordination_goals'}))['items']
            assert len(goals) == 1
            detail = stack.must(stack.client.get('/api/collaboration', params={
                **scope(stack), 'kind': 'coordination_goal', 'id': goals[0]['id']}))
            assert len(detail['work_items']) == 1
    finally:
        context.close()


def host_tool(stack, name, arguments):
    public = public_collaboration_request(name, arguments)
    assert public['tool'] in {'collaboration_query', 'collaboration', 'collaboration_work'}
    params = {'name': public['tool'], 'arguments': public['arguments'],
              '_meta': {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}}
    body = {'jsonrpc': '2.0', 'id': key(), 'method': 'tools/call', 'params': params}
    reply = stack.must(stack.client.post('/mcp', json=body, headers={
        'Authorization': 'Bearer ' + stack.pat, 'Accept': 'application/json, text/event-stream',
        **request_headers(body)}))['result']
    assert not reply.get('isError'), reply
    return reply.get('structuredContent', reply)


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_host_reads_real_project_and_returns_one_reply_to_original_thread(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
            'expected_version': 0, 'purpose': 'Read README.md and report evidence.',
            'capabilities': ['read'], 'execution_targets': ['project_agent'],
            'idempotency_key': key()}))['policy']
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('[name="delegation_policy"]').select_option(policy['id'])
        page.locator('#cc-message-input').fill('读取 README.md，完成后在本话题报告')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-delegation-message')).to_be_visible()
        source = messages(stack)[0]
        delegated = source['body']['delegation']
        source_article = page.locator('.cc-feed [data-message-id="' + source['id'] + '"]')
        expect(source_article.locator('[data-cc-action="convert"]')).to_have_count(0)
        expect(source_article.locator('.cc-message-deliveries')).to_have_count(0)
        expect(source_article.locator('[data-cc-action="message-remind"]')).to_have_count(0)
        fresh = host_tool(stack, 'collaboration_delegation_read', {
            **scope(stack), 'delegation_id': delegated['delegation_id']})
        work = next(item for item in fresh['work_items'] if item['id'] == delegated['work_item_id'])
        work = host_tool(stack, 'collaboration_work_claim', {
            **scope(stack), 'goal_id': delegated['goal_id'], 'work_item_id': work['id'],
            'expected_version': work['version'], 'idempotency_key': key()})['work_item']
        lease = {**scope(stack), 'goal_id': delegated['goal_id'], 'work_item_id': work['id'],
                 'attempt': work['attempt'], 'fencing_token': work['fencing_token']}
        operated = host_tool(stack, 'collaboration_work_execute', {
            **lease, 'tool': 'read', 'arguments': {'path': 'README.md'}, 'idempotency_key': key()})
        receipt = stack.poll(operated['operation_id'], timeout=20)
        assert receipt['state'] == 'succeeded', receipt
        assert 'Integration fixture' in receipt['result']['data']['content']
        result_args = {
            **lease, 'outcome': 'succeeded', 'summary': '已实际读取 README.md：这是 ProjectAlpha 隔离测试项目。',
            'operation_ids': [operated['operation_id']], 'limitations': ['仅验证本地测试项目。'],
            'idempotency_key': key()}
        finished = host_tool(stack, 'collaboration_work_result', result_args)['work_item']
        host_tool(stack, 'collaboration_work_result', result_args)
        assert finished['result']['execution_verified']
        result_card = page.locator('.cc-feed .cc-delegation-result')
        expect(result_card).to_have_count(1, timeout=12000)
        expect(page.locator('.cc-feed')).to_contain_text(result_args['summary'])
        expect(page.locator('.cc-delegation-message')).to_have_attribute('data-delegation-state', 'succeeded')
        result_article = result_card.locator('xpath=ancestor::article[1]')
        expect(result_article.locator('[data-cc-action="convert"]')).to_have_count(0)
        result_card.locator('[data-cc-action="delegation-result"]').click()
        drawer = page.locator('#cc-drawer')
        expect(drawer.locator('[data-cc-action="goal-edit"]')).to_have_count(0)
        expect(drawer.locator('[data-cc-action="goal-review"]')).to_have_count(0)
        expect(drawer.locator('#cc-goal-message')).to_have_count(0)
        drawer.locator('.cc-goal-subscription [data-cc-action="mention-connect"]').click()
        expect(drawer.locator('.cc-delegation-policy')).to_be_visible()
        drawer.locator('[data-mode="managed_execution"]').click()
        expect(drawer.locator('.cc-delegation-instruction')).to_have_value(
            policy['consumer_contracts']['managed_execution']['instructions'])
        assert 'work_available.v1' not in drawer.locator('.cc-delegation-instruction').input_value()
        page.keyboard.press('Escape')
        result_card.locator('[data-cc-action="delegation-result"]').click()
        drawer.locator('.cc-work-operations details summary').click()
        expect(drawer).to_contain_text(operated['operation_id'])
        drawer.locator('[data-cc-action="goal-operation"]').click()
        expect(drawer).to_contain_text('Integration fixture')
        page.keyboard.press('Escape')
        page.set_viewport_size({'width': 390, 'height': 844})
        result_card.scroll_into_view_if_needed()
        screenshot(page, engine + '-delegation-completed-mobile.png', tmp_path)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        result_article.locator('[data-cc-action="reply"]').click()
        page.locator('#cc-message-input').fill('已看到结果，继续核对')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('已看到结果，继续核对')
        all_messages = messages(stack)
        results = [item for item in all_messages if item['kind'] == 'delegation_result']
        assert len(results) == 1
        response = next(item for item in all_messages if item['body_text'] == '已看到结果，继续核对')
        assert response['reply_to_id'] == results[0]['id']
        assert response['thread_root_id'] == source['thread_root_id']
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_blocked_steps_require_explicit_safe_retry(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
            'expected_version': 0, 'purpose': 'Read README after confirming prerequisites.',
            'capabilities': ['read'], 'execution_targets': ['project_agent'],
            'idempotency_key': key()}))['policy']
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('[name="delegation_policy"]').select_option(policy['id'])
        page.locator('#cc-message-input').fill('核对前置条件后读取 README')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-delegation-message')).to_be_visible()
        source = messages(stack)[0]
        delegated = source['body']['delegation']
        source_article = page.locator('.cc-feed [data-message-id="' + source['id'] + '"]')
        expect(source_article.locator('[data-cc-action="convert"]')).to_have_count(0)
        expect(source_article.locator('.cc-message-deliveries')).to_have_count(0)
        expect(source_article.locator('[data-cc-action="message-remind"]')).to_have_count(0)
        fresh = host_tool(stack, 'collaboration_delegation_read', {
            **scope(stack), 'delegation_id': delegated['delegation_id']})
        work = fresh['work_items'][0]
        work = host_tool(stack, 'collaboration_work_claim', {
            **scope(stack), 'goal_id': delegated['goal_id'], 'work_item_id': work['id'],
            'expected_version': work['version'], 'idempotency_key': key()})['work_item']
        host_tool(stack, 'collaboration_work_result', {
            **scope(stack), 'goal_id': delegated['goal_id'], 'work_item_id': work['id'],
            'attempt': work['attempt'], 'fencing_token': work['fencing_token'], 'outcome': 'blocked',
            'summary': '前置资料缺失，尚未开始实际操作。', 'operation_ids': [], 'idempotency_key': key()})
        retry = page.locator('[data-cc-action="delegation-retry-blocked"]')
        expect(retry).to_be_visible(timeout=12000)
        source_card = page.locator('.cc-delegation-message')
        expect(source_card.locator('[data-cc-action="delegation-remind"]')).to_have_count(0)
        with page.expect_request('**/api/collaboration/delegation-remind') as requested:
            retry.click()
        assert requested.value.post_data_json['retry_blocked'] is True
        expect(page.locator('#cc-feedback')).to_contain_text('重新排队')
        latest = host_tool(stack, 'collaboration_delegation_read', {
            **scope(stack), 'delegation_id': delegated['delegation_id']})
        assert len(latest['work_items']) == 1
        assert latest['work_items'][0]['id'] == work['id']
        assert latest['work_items'][0]['state'] == 'queued'
        assert latest['operations'] == []
        assert len([m for m in messages(stack) if m['kind'] == 'delegation_result']) == 1
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_mention_primary_delegates_in_one_choice_and_secondary_only_discusses(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
            'expected_version': 0, 'purpose': 'Read project facts and report evidence.',
            'capabilities': ['read'], 'execution_targets': ['project_agent'],
            'idempotency_key': key()}))['policy']
        refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        primary = page.locator('[data-cc-action="pick-delegation"]')
        secondary = page.locator('[data-cc-action="pick-mention"]')
        expect(primary).to_contain_text('交给 协作 dot 处理')
        expect(secondary).to_contain_text('仅讨论提醒')
        screenshot(page, engine + '-mention-primary-actions.png', tmp_path)
        primary.click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('[name="delegation_policy"]')).to_have_value(policy['id'])
        expect(page.locator('.cc-delegation-context')).to_contain_text('本次发送将创建委托')
        expect(page.locator('.cc-mention-chip')).to_have_count(1)
        page.locator('#cc-message-input').fill('本次明确交给 dot 处理')
        with page.expect_request('**/api/collaboration/message') as sent:
            page.locator('#cc-command button[type="submit"]').click()
        posted = sent.value.post_data_json
        assert posted['mentions'] == [{'slot_id': slot['id']}]
        assert posted['delegation']['policy_id'] == policy['id']
        assert posted['delegation']['policy_version'] == policy['version']
        expect(page.locator('#cc-message-input')).to_have_value('')
        page.locator('[data-cc-action="mentions"]').click()
        secondary.click()
        page.keyboard.press('Escape')
        expect(page.locator('[name="delegation_policy"]')).to_have_value('')
        page.locator('#cc-message-input').fill('本次只讨论，不创建另一个任务')
        with page.expect_request('**/api/collaboration/message') as discussed:
            page.locator('#cc-command button[type="submit"]').click()
        assert 'delegation' not in discussed.value.post_data_json
        expect(page.locator('#cc-message-input')).to_have_value('')
        all_messages = messages(stack)
        assert len(all_messages) == 2
        assert sum(bool(m['body'].get('delegation')) for m in all_messages) == 1
        goals = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'coordination_goals'}))['items']
        assert len(goals) == 1

        page.locator('#cc-message-input').fill('旧菜单的草稿需要保留')
        page.locator('[data-cc-action="mentions"]').click()
        secondary.click()
        page.keyboard.press('Escape')
        option = page.locator('[name="delegation_policy"] option[value="' + policy['id'] + '"]')
        expect(option).to_have_attribute('data-policy-version', str(policy['version']))
        page.locator('[data-cc-action="mentions"]').click()
        expect(primary).to_have_attribute('data-policy-version', str(policy['version']))
        updated = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
            'expected_version': policy['version'], 'purpose': 'A changed scope that requires a new choice.',
            'capabilities': ['read'], 'execution_targets': ['project_agent'],
            'idempotency_key': key()}))['policy']
        assert updated['version'] > policy['version']
        # This option is patched only after the complete refresh commits. HTTP
        # response headers alone are not proof that the new catalog was applied.
        expect(option).to_have_attribute('data-policy-version', str(updated['version']), timeout=12000)
        expect(primary).to_have_attribute('data-policy-version', str(policy['version']))
        primary.click()
        expect(page.locator('#cc-feedback')).to_contain_text('委托范围已改变')
        page.keyboard.press('Escape')
        expect(page.locator('#cc-message-input')).to_have_value('旧菜单的草稿需要保留')
        expect(page.locator('[name="delegation_policy"]')).to_have_value('')
        assert len(messages(stack)) == 2
    finally:
        context.close()


def policy_choice_fixture(page, stack):
    login(page, stack)
    slot = join_slot(stack)
    room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
    policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
        **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
        'expected_version': 0, 'purpose': 'Original visible policy scope.',
        'capabilities': ['read'], 'execution_targets': ['project_agent'],
        'idempotency_key': key()}))['policy']
    refresh(page)
    page.locator('[data-cc-action="mentions"]').click()
    page.locator('[data-cc-action="pick-mention"]').click()
    page.keyboard.press('Escape')
    option = page.locator('[name="delegation_policy"] option[value="' + policy['id'] + '"]')
    expect(option).to_have_attribute('data-policy-version', str(policy['version']))
    return room, slot, policy


def revise_policy(stack, room, slot, policy):
    return stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
        **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
        'expected_version': policy['version'], 'purpose': 'New server policy scope requiring a new choice.',
        'capabilities': ['read'], 'execution_targets': ['project_agent'],
        'idempotency_key': key()}))['policy']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_uncommitted_policy_refresh_keeps_old_intent_and_server_rejects_it(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    held = []
    gate = {'seen_new_policy': False, 'held_options': False, 'freeze_following': False}
    try:
        room, slot, policy = policy_choice_fixture(page, stack)
        area = page.locator('#cc-message-input')
        area.fill('未提交刷新期间仍只选择原版本')
        page.locator('[data-cc-action="mentions"]').click()
        primary = page.locator('[data-cc-action="pick-delegation"]')
        expect(primary).to_have_attribute('data-policy-version', str(policy['version']))

        def policies(route):
            response = route.fetch()
            if gate['freeze_following']:
                held.append((route, response))
                return
            value = response.json()
            if any(item['id'] == policy['id'] and item['version'] > policy['version']
                   for item in value.get('items', [])):
                gate['seen_new_policy'] = True
                gate['freeze_following'] = True
            route.fulfill(response=response)

        def options(route):
            if gate['seen_new_policy'] and not gate['held_options']:
                response = route.fetch()
                held.append((route, response))
                gate['held_options'] = True
                page.evaluate('document.documentElement.dataset.fixturePolicyRefreshHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*kind=delegation_policies*', policies)
        page.route('**/api/collaboration?*kind=coordination_options*', options)
        updated = revise_policy(stack, room, slot, policy)
        assert updated['version'] > policy['version']
        expect(page.locator('html')).to_have_attribute('data-fixture-policy-refresh-held', 'yes', timeout=12000)
        option = page.locator('[name="delegation_policy"] option[value="' + policy['id'] + '"]')
        expect(option).to_have_attribute('data-policy-version', str(policy['version']))
        primary.click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-delegation-context')).to_contain_text(policy['purpose'])
        expect(option).to_have_attribute('data-policy-version', str(policy['version']))

        # Releasing the old refresh after the click cannot rebind this intent.
        route, response = held.pop(0)
        route.fulfill(response=response)
        with page.expect_request('**/api/collaboration/message') as sent:
            with page.expect_response('**/api/collaboration/message') as rejected:
                page.locator('#cc-command button[type="submit"]').click()
        assert sent.value.post_data_json['delegation']['policy_version'] == policy['version']
        assert rejected.value.status == 409
        assert rejected.value.json()['error']['code'] == 'DELEGATION_POLICY_CHANGED'
        expect(page.locator('#cc-feedback')).to_contain_text('委托版本')
        expect(area).to_have_value('未提交刷新期间仍只选择原版本')
        assert messages(stack) == []
        goals = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'coordination_goals'}))['items']
        assert goals == []
    finally:
        for route, response in held:
            route.fulfill(response=response)
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_focused_select_keeps_the_version_the_user_saw(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        room, slot, policy = policy_choice_fixture(page, stack)
        area = page.locator('#cc-message-input')
        area.fill('下拉框旧版本不得静默替换')
        picker = page.locator('[name="delegation_policy"]')
        option = picker.locator('option[value="' + policy['id'] + '"]')
        if not page.locator('[data-cc-action="refresh"]').is_visible():
            page.locator('[data-cc-more]').click()
        picker.focus()
        picker.evaluate('(node) => node.dataset.retained = "yes"')
        updated = revise_policy(stack, room, slot, policy)
        assert updated['version'] > policy['version']
        expect(page.locator('#cc-feedback')).to_have_text('')
        # Invoke the real refresh control without stealing the select's focus.
        # Its completion message is emitted only after refreshChat has committed.
        page.locator('[data-cc-action="refresh"]').evaluate('(button) => button.click()')
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        expect(picker).to_be_focused()
        expect(picker).to_have_attribute('data-retained', 'yes')
        expect(option).to_have_attribute('data-policy-version', str(policy['version']))
        picker.select_option(policy['id'])
        expect(page.locator('.cc-delegation-context')).to_contain_text('已改变、到期或不可用')
        sent = []
        page.on('request', lambda request: sent.append(request)
                if request.method == 'POST' and urlsplit(request.url).path.endswith('/collaboration/message') else None)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('正文已保留')
        expect(area).to_have_value('下拉框旧版本不得静默替换')
        assert sent == []
        assert messages(stack) == []
        goals = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'coordination_goals'}))['items']
        assert goals == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_connection_modes_reuse_scope_and_preserve_draft(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    posts = []
    try:
        room, slot, policy = policy_choice_fixture(page, stack)
        register_fixture_subscription(stack, policy['subscription_request'])
        refresh(page)
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        page.on('request', lambda request: posts.append(urlsplit(request.url).path)
                if request.method == 'POST' else None)
        area = page.locator('#cc-message-input')
        area.fill('接入前已经写好的草稿')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="mention-connect"]').click()
        drawer = page.locator('#cc-drawer')
        expect(drawer.locator('#cc-delegation-policy')).to_have_count(0)
        expect(drawer.locator('.cc-delegation-policy')).to_contain_text('当前范围已订阅')
        expect(drawer.locator('.cc-delegation-policy')).to_contain_text('尚无任务领取记录')
        expect(drawer.locator('.cc-join-card')).not_to_be_visible()
        drawer.locator('[data-mode="notification_only"]').click()
        instruction = drawer.locator('.cc-delegation-instruction')
        expect(drawer.locator('.cc-connection-instruction')).to_have_attribute('data-consumer-mode', 'notification_only')
        expect(instruction).to_have_value(policy['consumer_contracts']['notification_only']['instructions'])
        expect(instruction).not_to_be_visible()
        drawer.locator('.cc-instruction-details summary').click()
        instruction.focus()
        instruction.evaluate('(node) => node.dataset.retained = "yes"')
        # Refresh through the actual control without stealing focus.
        page.locator('[data-cc-action="refresh"]').evaluate('(node) => node.click()')
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        expect(instruction).to_be_focused()
        expect(instruction).to_have_attribute('data-retained', 'yes')
        drawer.locator('[data-mode="managed_execution"]').click()
        expect(drawer.locator('.cc-connection-instruction')).to_have_attribute('data-consumer-mode', 'managed_execution')
        expect(instruction).to_have_value(policy['consumer_contracts']['managed_execution']['instructions'])
        expect(instruction).not_to_be_visible()
        page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true, value: {writeText: async (value) => { window.fixtureCopied = value; }}
        })""")
        copy = drawer.locator('[data-cc-action="delegation-copy"]')
        copy.evaluate("""(node) => node.addEventListener('click', () => {
            window.fixtureCopyFocusAtClick = document.activeElement;
        }, {capture: true, once: true})""")
        copy.click()
        expect(page.locator('#cc-feedback')).to_contain_text('已复制')
        # WebKit does not focus buttons on pointer clicks. Verify that our
        # handler preserves the browser's actual focus at the click boundary.
        assert page.evaluate('document.activeElement === window.fixtureCopyFocusAtClick')
        expect(instruction).not_to_be_visible()
        assert page.evaluate('window.fixtureCopied') == policy['consumer_contracts']['managed_execution']['instructions']
        screenshot(page, engine + '-connection-managed-mobile.png', tmp_path)
        page.set_viewport_size({'width': 1440, 'height': 900})
        screenshot(page, engine + '-connection-managed-desktop.png', tmp_path)
        page.keyboard.press('Escape')
        expect(area).to_have_value('接入前已经写好的草稿')
        assert not any(path.endswith('/delegation-policy') or path.endswith('/delegation-policy-control') for path in posts)
        latest = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items'][0]
        assert latest['version'] == policy['version']
        assert latest['connection_status']['consumer_mode'] == 'unknown'
        assert messages(stack) == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_multiple_targets_choose_per_send_subset_and_double_click_creates_one(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        vps = stack.must(stack.client.post('/api/vps', json={
            'name': '合成检查服务器', 'host': 'delegation-browser.example.invalid',
            'username': 'fixture', 'password': 'synthetic-browser-fixture-only',
            'project_ids': [stack.project['id']]}))
        policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'],
            'expected_version': 0, 'purpose': '检查已选择的项目或服务器并报告。',
            'capabilities': ['read', 'write', 'execute'], 'acknowledge_unsandboxed_exec': True,
            'execution_targets': ['project_agent', vps['target']],
            'idempotency_key': key()}))['policy']
        refresh(page)
        page.locator('#cc-message-input').fill('请检查这台 VPS 的状态并报告')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-delegation"]').click()
        form = page.locator('#cc-delegation-scope')
        expect(form).to_be_visible()
        expect(form.locator('[name="execution_targets"]:checked')).to_have_count(0)
        expect(form).to_contain_text('合成检查服务器')
        form.locator('[data-cc-action="close-drawer"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('请检查这台 VPS 的状态并报告')
        expect(page.locator('.cc-send-scope')).to_contain_text('请选择本次目标')
        page.locator('#cc-command button[type="submit"]').click()
        expect(form).to_be_visible()
        assert messages(stack) == []
        form.locator('[name="execution_targets"][value="' + vps['target'] + '"]').check()
        form.locator('[name="capabilities"][value="write"]').uncheck()
        form.locator('button[type="submit"]').click()
        summary = page.locator('.cc-send-scope')
        expect(summary).to_contain_text('协作 dot')
        expect(summary).to_contain_text('合成检查服务器')
        expect(summary).to_contain_text('读取、运行命令')
        expect(summary).not_to_contain_text('项目 Agent')
        screenshot(page, engine + '-selected-scope-mobile.png', tmp_path)
        with page.expect_request('**/api/collaboration/message') as sent:
            page.locator('#cc-command button[type="submit"]').evaluate('(button) => {button.click(); button.click();}')
        payload = sent.value.post_data_json['delegation']
        assert payload['execution_targets'] == [vps['target']]
        assert payload['capabilities'] == ['read', 'execute']
        expect(page.locator('#cc-message-input')).to_have_value('')
        assert len(messages(stack)) == 1
        delegated = messages(stack)[0]['body']['delegation']
        fresh = host_tool(stack, 'collaboration_delegation_read', {
            **scope(stack), 'delegation_id': delegated['delegation_id']})
        assert fresh['policy']['execution_targets'] == ['project_agent', vps['target']]
        assert fresh['request_scope']['capabilities'] == ['read', 'execute']
        assert fresh['request_scope']['execution_targets'] == [vps['target']]
        assert len(fresh['work_items']) == 1
        assert fresh['work_items'][0]['required_capabilities'] == ['read', 'execute']
        assert policy['version'] == payload['policy_version']
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_one_target_is_visible_and_natural_language_does_not_change_authority(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        _, _, policy = policy_choice_fixture(page, stack)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-delegation"]').click()
        expect(page.locator('#cc-delegation-scope')).to_have_count(0)
        expect(page.locator('.cc-send-scope')).to_contain_text('项目 Agent')
        expect(page.locator('.cc-send-scope')).to_contain_text('读取')
        area = page.locator('#cc-message-input')
        area.fill('看看 VPS 上的服务，需要别的目标时告诉我')
        screenshot(page, engine + '-single-target-desktop.png', tmp_path)
        with page.expect_request('**/api/collaboration/message') as sent:
            page.locator('#cc-command button[type="submit"]').click()
        payload = sent.value.post_data_json
        assert payload['delegation']['execution_targets'] == ['project_agent']
        assert payload['delegation']['capabilities'] == ['read']
        assert payload['delegation']['policy_version'] == policy['version']
        assert payload['delegation']['acceptance'] == '完成请求，并报告实际检查、结果和限制'
        expect(area).to_have_value('')
        assert len(messages(stack)) == 1
        assert messages(stack)[0]['body']['body_text'] == payload['body_text']
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_unknown_operation_projection_never_claims_running(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        policy_choice_fixture(page, stack)

        def unknown_projection(route):
            response = route.fetch()
            value = response.json()
            for policy in value.get('items', []):
                policy['connection_status']['operation'] = {
                    'state': 'unknown', 'pending_count': 1, 'unknown_count': 1}
            route.fulfill(response=response, json=value)

        page.route('**/api/collaboration?*kind=delegation_policies*', unknown_projection)
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="mention-connect"]').click()
        card = page.locator('#cc-drawer .cc-delegation-policy')
        expect(card).to_contain_text('实际操作状态待核对')
        expect(card).not_to_contain_text('有实际操作正在运行')
        expect(card).not_to_contain_text('可自动执行')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_focused_connection_updates_evidence_and_rejects_stale_copy(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    projection = {'unknown': False}
    try:
        room, slot, policy = policy_choice_fixture(page, stack)

        def statuses(route):
            response = route.fetch()
            value = response.json()
            if projection['unknown']:
                for item in value.get('items', []):
                    item['connection_status']['operation'] = {
                        'state': 'unknown', 'pending_count': 1, 'unknown_count': 1}
            route.fulfill(response=response, json=value)

        page.route('**/api/collaboration?*kind=delegation_policies*', statuses)
        page.locator('#cc-message-input').fill('状态刷新期间保留的正文')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="mention-connect"]').click()
        drawer = page.locator('#cc-drawer')
        drawer.locator('[data-mode="managed_execution"]').click()
        copy = drawer.locator('[data-cc-action="delegation-copy"]')
        copy.focus()
        copy.evaluate('(node) => node.dataset.retained = "yes"')
        projection['unknown'] = True
        page.locator('[data-cc-action="refresh"]').evaluate('(node) => node.click()')
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        expect(drawer.locator('[data-connection-status="progress"]')).to_have_text('实际操作状态待核对')
        expect(copy).to_be_focused()
        expect(copy).to_have_attribute('data-retained', 'yes')
        updated = revise_policy(stack, room, slot, policy)
        assert updated['version'] > policy['version']
        page.locator('[data-cc-action="refresh"]').evaluate('(node) => node.click()')
        expect(page.locator('#cc-feedback')).to_contain_text('状态已刷新')
        expect(copy).to_be_focused()
        expect(drawer.locator('[data-policy-slot]')).to_have_attribute('data-policy-version', str(policy['version']))
        page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true, value: {writeText: async () => { window.staleCopyCalled = true; }}
        })""")
        copy.click()
        expect(drawer.locator('[data-policy-slot]')).to_have_attribute('data-policy-version', str(updated['version']))
        expect(drawer.locator('.cc-connection-instruction')).to_have_count(0)
        expect(page.locator('#cc-feedback')).to_contain_text('重新选择接入方式')
        assert page.evaluate('window.staleCopyCalled !== true')
        page.keyboard.press('Escape')
        expect(page.locator('#cc-message-input')).to_have_value('状态刷新期间保留的正文')
        assert messages(stack) == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_delegation_result_load_respects_drawer_dismissal_and_new_navigation(
        chat_browser_pool, engine):
    # Test the shipped module with a delayed authority refresh. The ordinary
    # goal-detail reader has its own guard, but this earlier load must not
    # reopen a dismissed thread or replace a newer drawer before reaching it.
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.add_script_tag(path=str(
            Path(__file__).resolve().parents[1] / 'web/collaboration-delegation.js'))
        result = page.evaluate("""async () => {
          const results = {};
          for (const change of ['none', 'close', 'replace', 'navigate']) {
            const state = {generation: 1, drawerEpoch: 5};
            const calls = [];
            let release;
            const ui = CodePierCollaborationDelegation.create({
              state,
              coordination: {
                load: () => new Promise(resolve => { release = resolve; }),
                act: (action, element) => {
                  calls.push({action, id: element.dataset.id});
                  return {local: true};
                }
              }
            });
            const pending = ui.act('delegation-result', {dataset: {id: 'fixture-goal'}});
            if (change === 'navigate') state.generation++;
            else if (change !== 'none') state.drawerEpoch++;
            release();
            await pending;
            results[change] = calls;
          }
          return results;
        }""")
        assert result == {
            'none': [{'action': 'goal-detail', 'id': 'fixture-goal'}],
            'close': [], 'replace': [], 'navigate': []}
    finally:
        context.close()
