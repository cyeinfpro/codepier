"""Chat-first UX against the real isolated Hub; no real native host is contacted."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack, key  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def scope(stack, environment='production'):
    return {'project': stack.project['id'], 'environment_id': environment}


def overview(stack):
    return stack.must(stack.client.get('/api/collaboration', params=scope(stack)))


def login(page, stack):
    page.goto(stack.url + '/#projects')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('项目映射')
    page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
    page.locator('[data-cc-action="create-room"]').click()
    expect(page.locator('#cc-message-input')).to_be_enabled()


def add_message(stack, room_id, body):
    return stack.must(stack.client.post('/api/collaboration/message', json={
        **scope(stack), 'room_id': room_id, 'body_text': body,
        'client_message_id': key(), 'idempotency_key': key(), 'mentions': []}))


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_chat_messages_drafts_ime_scroll_replies_and_task_gate(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    shots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
    shots.mkdir(parents=True, exist_ok=True)
    try:
        login(page, stack)
        area = page.locator('#cc-message-input')
        payload = '一起讨论 <img src=x onerror="window.chatroomXss=true"> @Work 文本本身不派发'
        area.fill(payload)
        page.locator('#cc-command button[type="submit"]').evaluate('(b) => {b.click();b.click();}')
        expect(page.locator('.cc-feed')).to_contain_text(payload)
        expect(area).to_have_value('')
        data = overview(stack)
        assert len(data['jobs']) == 0 and len(data['messages']) == 1
        assert page.locator('.cc-feed img').count() == 0
        assert not page.evaluate('Boolean(window.chatroomXss)')
        page.locator('.cc-feed [data-cc-action="convert"]').first.click()
        expect(page.locator('.cc-task-blocker')).to_contain_text('没有可用的只读任务执行者')
        expect(page.locator('#cc-task-review button[type="submit"]')).to_be_disabled()
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-feed [data-cc-action="convert"]').first).to_be_focused()
        assert overview(stack)['jobs'] == []

        page.locator('.cc-feed [data-cc-action="reply"]').first.click()
        expect(page.locator('.cc-reply-preview')).to_contain_text(payload)
        area.fill('这是一条明确回复')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('这是一条明确回复')
        expect(page.locator('.cc-reply-preview')).to_have_count(0)
        data = overview(stack)
        assert len(data['messages']) == 2 and data['jobs'] == []
        assert any(m.get('reply_to_id') for m in data['messages'])

        # Incoming messages are applied while Chinese IME and selection remain alive.
        area.fill('中文输入中的草稿')
        area.evaluate('(n) => {n.focus(); n.setSelectionRange(2, 5); n.dataset.retained = "yes"; n.dispatchEvent(new CompositionEvent("compositionstart", {bubbles:true}));}')
        room_id = data['room']['id']
        add_message(stack, room_id, '真实后台的新消息')
        expect(page.locator('.cc-feed')).to_contain_text('真实后台的新消息', timeout=12000)
        expect(area).to_have_value('中文输入中的草稿')
        expect(area).to_be_focused()
        assert area.evaluate('(n) => [n.selectionStart, n.selectionEnd, n.dataset.retained]') == [2, 5, 'yes']
        area.dispatch_event('keydown', {'key': 'Enter', 'code': 'Enter', 'isComposing': True})
        assert len(overview(stack)['messages']) == 3
        area.dispatch_event('compositionend')
        page.locator('[data-cc-view="jobs"]').click()
        page.locator('[data-cc-view="discussion"]').click()
        expect(area).to_have_value('中文输入中的草稿')

        # Real room switching preserves each in-memory draft without local storage prose.
        staging = stack.must(stack.client.post('/api/collaboration/room', json={
            **scope(stack, 'staging'), 'idempotency_key': key()}))['room']
        page.locator('[data-cc-view="jobs"]').click()
        page.locator('[data-cc-view="discussion"]').click()
        page.locator(f'[data-cc-room="{staging["id"]}"]').click()
        expect(area).to_have_value('')
        area.fill('staging 独立草稿')
        page.locator(f'[data-cc-room="{room_id}"]').click()
        expect(area).to_have_value('中文输入中的草稿')
        assert all('草稿' not in v for v in page.evaluate('Object.values(sessionStorage)'))

        # Reading older messages must never jump to the bottom on arrival.
        for index in range(14):
            add_message(stack, room_id, f'滚动证据 {index}，保留阅读位置。')
        if not page.locator('[data-cc-action="refresh"]').is_visible():
            page.locator('[data-cc-more]').click()
        page.locator('[data-cc-action="refresh"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('滚动证据 13')
        feed = page.locator('.cc-feed')
        feed.evaluate('(n) => n.scrollTop = 20')
        top = feed.evaluate('(n) => n.scrollTop')
        add_message(stack, room_id, '用户向上阅读时到达')
        expect(page.locator('.cc-new-messages')).to_be_visible(timeout=12000)
        assert abs(feed.evaluate('(n) => n.scrollTop') - top) < 3
        page.locator('.cc-new-messages').click()
        expect(page.locator('.cc-new-messages')).not_to_be_visible()
        page.screenshot(animations='disabled', path=str(shots / f'{engine}-chatroom-desktop.png'))

        for width in (390, 360):
            page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 800})
            area.fill('手机换行')
            area.press('Enter')
            expect(area).to_have_value('手机换行\n')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert area.evaluate('(n) => parseFloat(getComputedStyle(n).fontSize)') >= 16
            send = page.locator('#cc-command button[type="submit"]').bounding_box()
            assert send and send['y'] >= 0 and send['y'] + send['height'] <= page.viewport_size['height']
            assert page.locator('#cc-command button[type=\"submit\"]').evaluate('(n) => {const r=n.getBoundingClientRect(); return n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
            page.screenshot(animations='disabled', path=str(shots / f'{engine}-chatroom-{width}.png'))
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_chat_unknown_send_reload_reconciles_single_message(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        held = []

        def lose_response(route):
            held.append((route, route.fetch()))
            page.locator('html').evaluate('(n) => n.dataset.messageSaved = "yes"')

        page.route('**/api/collaboration/message', lose_response)
        body = '保存成功但响应中断的消息'
        page.locator('#cc-message-input').fill(body)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-message-saved', 'yes')
        assert len(overview(stack)['messages']) == 1
        held[0][0].abort()
        page.unroute('**/api/collaboration/message', lose_response)
        page.reload()
        expect(page.locator('.cc-feed')).to_contain_text(body)
        expect(page.locator('#cc-message-input')).to_have_value('')
        page.locator('#cc-message-input').fill(body)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        assert len(overview(stack)['messages']) == 1
        assert overview(stack)['jobs'] == []
        assert all(body not in v for v in page.evaluate('Object.values(sessionStorage)'))
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_structured_mentions_and_connection_access_review(collaboration_stack, chat_browser_pool, engine):
    from tests.test_mcp_tasks_http import modern

    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        slot = stack.must(stack.client.post('/api/collaboration/join-slot', json={
            **scope(stack), 'label': '通知位置 Work', 'kind': 'work_cloud', 'idempotency_key': key()}))['slot']
        joined = modern(stack, 'tools/call', {'name': 'collaboration_join', 'arguments': {
            'code': slot['join_code'], 'idempotency_key': key()}}, token=stack.pat).json()['result']
        assert not joined.get('isError')
        if not page.locator('[data-cc-action="refresh"]').is_visible():
            page.locator('[data-cc-more]').click()
        page.locator('[data-cc-action="refresh"]').click()
        page.locator('[data-cc-action="mentions"]').click()
        option = page.locator('[data-cc-action="pick-mention"]')
        expect(option).to_have_attribute('aria-pressed', 'false')
        expect(option).to_contain_text(slot['id'][-6:])
        option.click()
        expect(option).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('Escape')
        expect(page.locator('.cc-mention-chip')).to_have_text('@通知位置 Work ×')
        page.locator('#cc-message-input').fill('只提醒这个明确选择的位置')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('部分提醒未送达')
        data = overview(stack)
        assert data['jobs'] == [] and len(data['messages']) == 1
        assert data['messages'][0]['mentions'] == [{'slot_id': slot['id'], 'display_snapshot': '通知位置 Work'}]
        expect(page.locator('.cc-mention-chip')).to_have_count(0)
        expect(page.locator('.cc-feed .cc-message-mentions')).to_contain_text('@通知位置 Work')
        page.locator('[data-cc-view="agents"]').click()
        card = page.locator(f'[data-speaking-slot="{slot["id"]}"]')
        expect(card).to_contain_text('尚未获准')
        card.locator('[data-cc-action="message-access"]').click()
        form = page.locator('#cc-message-access')
        expect(form.locator('[name="confirm"]')).not_to_be_checked()
        expect(form).to_have_attribute('data-version', '0')
        form.locator('button[type="submit"]').click()
        members = stack.must(stack.client.get('/api/collaboration', params={**scope(stack), 'kind': 'members'}))['items']
        assert not members[0]['can_speak']
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(card).to_contain_text('已获准在本房间发言')
        members = stack.must(stack.client.get('/api/collaboration', params={**scope(stack), 'kind': 'members'}))['items']
        assert members[0]['can_speak'] and members[0]['speaking_version'] == 1
        card.locator('summary').click()
        expect(card.locator('.cc-mention-instruction')).to_contain_text('codepier.collaboration.message_mentioned.v1')
        assert overview(stack)['agents'] == []  # Speaking still grants no task eligibility.
        # A concurrent permission edit invalidates the visible confirmation.
        card.locator('[data-cc-action="message-access"]').click()
        expect(form).to_have_attribute('data-version', '1')
        member = members[0]
        stack.must(stack.client.post('/api/collaboration/message-access', json={
            **scope(stack), 'room_id': data['room']['id'], 'grant_id': member['grant_id'],
            'expected_version': 1, 'enabled': False, 'idempotency_key': key()}))
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('重新审阅')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        card.locator('[data-cc-action="message-access"]').click()
        expect(form).to_have_attribute('data-version', '2')
        expect(form.locator('[name="confirm"]')).not_to_be_checked()
    finally:
        context.close()



@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversation_add_project_preserves_history_draft_and_attribution(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        area = page.locator('#cc-message-input')
        area.fill('原项目已有讨论')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        data = overview(stack)
        conversation = data['conversation']['id']
        original_message = data['messages'][0]['id']
        assert conversation == data['room']['id']
        area.fill('添加项目期间保留的草稿')
        other = stack.projects[1]
        page.locator('[data-cc-action="add-project"]').click()
        form = page.locator('#cc-add-project')
        form.locator('[name="project"]').select_option(other['id'])
        page.keyboard.press('Escape')
        expect(area).to_have_value('添加项目期间保留的草稿')
        page.locator('[data-cc-action="add-project"]').click()
        form.locator('[name="project"]').select_option(other['id'])
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').evaluate('(button) => {button.click();button.click();}')
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-conversation', conversation)
        expect(area).to_have_value('添加项目期间保留的草稿')
        expect(page.locator('.cc-feed')).to_contain_text('原项目已有讨论')
        current = overview(stack)['conversation']
        assert current['id'] == conversation and len(current['projects']) == 2
        # Changing the source project changes execution context, not the room ID.
        page.locator(f'[data-cc-partition="{other["id"]}"]').click()
        expect(page.locator(f'[data-cc-partition="{other["id"]}"]')).to_have_attribute('aria-pressed', 'true')
        expect(area).to_have_value('添加项目期间保留的草稿')
        area.fill('第二个项目的消息')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('第二个项目的消息')
        records = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'timeline', 'conversation_id': conversation}))['items']
        assert {m['project_id'] for m in records} == {stack.project['id'], other['id']}
        assert all(m['conversation_id'] == conversation for m in records)
        second = next(m for m in records if m['project_id'] == other['id'])
        expect(page.locator(f'[data-message-id="{second["id"]}"] .cc-message-project')).to_contain_text(other['alias'])
        # A reply from another selected project follows its actual source.
        page.locator(f'[data-message-id="{original_message}"] [data-cc-action="reply"]').click()
        expect(page.locator(f'[data-cc-partition="{stack.project["id"]}"]')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('.cc-reply-preview')).to_contain_text('原项目已有讨论')
        area.fill('回复仍归属原项目')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        records = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'timeline', 'conversation_id': conversation}))['items']
        reply = next(m for m in records if m['body_text'] == '回复仍归属原项目')
        assert reply['project_id'] == stack.project['id'] and reply['reply_to_id'] == original_message
        assert overview(stack)['jobs'] == []
        shots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / f'{engine}-chatroom-multi-mobile.png'), animations='disabled')
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.screenshot(path=str(shots / f'{engine}-chatroom-multi-desktop.png'), animations='disabled')
        page.set_viewport_size({'width': 390, 'height': 844})
        # New room isolates history and survives reload by its opaque ID.
        page.locator('[data-cc-more]').click()
        page.locator('.cc-room-menu [data-cc-action="new-conversation"]').click()
        create = page.locator('#cc-new-conversation')
        create.locator('[name="title"]').fill('独立多项目讨论')
        for project in (stack.project, other):
            create.locator(f'[name="projects"][value="{project["id"]}"]').check()
        create.locator('[name="confirm"]').check()
        create.locator('button[type="submit"]').click()
        expect(page.locator('.cc-room-title h2')).to_have_text('独立多项目讨论')
        created = page.locator('.cc-chat-shell').get_attribute('data-conversation')
        assert created and created != conversation
        expect(page.locator('.cc-feed')).not_to_contain_text('原项目已有讨论')
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        page.reload()
        expect(page.locator('.cc-chat-shell')).to_have_attribute('data-conversation', created)
        expect(page.locator('.cc-room-title h2')).to_have_text('独立多项目讨论')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversation_stale_add_review_and_visible_permission_reset(collaboration_stack, chat_browser_pool, engine):
    """Real membership CAS; transport-only revoked projection verifies DOM cleanup.

    Server authorization itself is covered in test_collaboration_conversations.py.
    """
    from urllib.parse import parse_qs, urlsplit

    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        page.locator('#cc-message-input').fill('原项目敏感讨论记录')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        data = overview(stack)
        conversation = data['conversation']
        other = stack.projects[1]
        page.locator('[data-cc-action="add-project"]').click()
        form = page.locator('#cc-add-project')
        form.locator('[name="project"]').select_option(other['id'])
        form.locator('[name="confirm"]').check()
        stack.must(stack.client.post('/api/collaboration/conversation-project', json={
            'conversation_id': conversation['id'], 'expected_version': conversation['version'],
            'project': other['id'], 'environment_id': 'production', 'idempotency_key': key()}))
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('重新审阅')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        assert len(overview(stack)['conversation']['projects']) == 2
        page.locator('#cc-message-input').fill('被撤权来源的草稿')
        page.locator('[data-cc-more]').click()
        page.locator('[data-cc-action="search"]').click()
        page.locator('#cc-search [name="query"]').fill('敏感讨论')
        page.locator('#cc-search button[type="submit"]').click()
        expect(page.locator('.cc-search-results')).to_contain_text('原项目敏感讨论记录')

        def changed_projection(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['timeline'] and query.get('project') == [stack.project['id']]:
                route.fulfill(status=403, json={'error': {'code': 'PERMISSION_DENIED', 'message': '当前项目访问已撤回'}})
            else:
                route.continue_()

        def visible_rooms(route):
            response = route.fetch()
            body = response.json()
            body['items'] = [r for r in body['items'] if r['id'] == conversation['id']]
            for room in body['items']:
                room['projects'] = [p for p in room['projects'] if p['project_id'] == other['id']]
                room['visibility_token'] = 'browser-fixture-after-revocation'
            route.fulfill(response=response, json=body)

        page.route('**/api/collaboration/conversations', visible_rooms)
        page.route('**/api/collaboration?*', changed_projection)
        expect(page.locator('#cc-feedback')).to_contain_text('旧记录已清除', timeout=12000)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-drawer')).to_be_empty()
        expect(page.locator('.cc-feed')).not_to_contain_text('原项目敏感讨论记录')
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('#cc-message-input')).to_be_disabled()
        expect(page.locator('.cc-project-chip')).to_have_count(1)
        expect(page.locator('.cc-project-chip')).to_have_attribute('data-cc-partition', other['id'])
        expect(page.locator('.cc-context-rail')).to_be_empty()
    finally:
        context.close()
