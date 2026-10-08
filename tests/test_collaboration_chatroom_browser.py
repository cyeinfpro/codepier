"""Chat-first UX against the real isolated Hub; no real native host is contacted."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def register_notification_fixture(stack, label):
    slot = stack.must(stack.client.post('/api/collaboration/join-slot', json={
        **scope(stack), 'label': label, 'kind': 'work_cloud', 'idempotency_key': key()}))['slot']
    joined = stack.mcp('collaboration_join', {'code': slot['join_code'], 'idempotency_key': key()})
    assert not joined.get('isError'), joined
    return slot


def click_room_refresh(page):
    if not page.locator('[data-cc-action="refresh"]').is_visible():
        page.locator('[data-cc-more]').click()
    page.locator('[data-cc-action="refresh"]').click()


def hold_drawer_frames(page):
    page.evaluate("""() => {
      window.__ccFrameOriginal = window.requestAnimationFrame;
      window.__ccCloseFrames = [];
      delete document.documentElement.dataset.closeFrameHeld;
      window.requestAnimationFrame = callback => {
        window.__ccCloseFrames.push(callback);
        document.documentElement.dataset.closeFrameHeld = 'yes';
        return 0;
      };
    }""")


def release_drawer_frames(page):
    page.evaluate("""() => {
      window.requestAnimationFrame = window.__ccFrameOriginal;
      window.__ccCloseFrames.splice(0).forEach(callback => callback(performance.now()));
    }""")


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_delayed_drawer_close_focus_does_not_steal_composer_input(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    sent = []
    page.on('request', lambda request: sent.append(request.post_data_json)
            if request.method == 'POST' and urlsplit(request.url).path == '/api/collaboration/message' else None)
    try:
        login(page, stack)
        slot = register_notification_fixture(stack, '关闭菜单后的连接')
        click_room_refresh(page)
        page.locator('[data-cc-action="mentions"]').click()
        option = page.locator('[data-cc-action="pick-mention"]')
        option.click()
        expect(option).to_have_attribute('aria-pressed', 'true')
        # Flush opening focus before controlling just the native close frame.
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        hold_drawer_frames(page)
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-close-frame-held', 'yes')
        area = page.locator('#cc-message-input')
        # WebKit fill and ordinary typing both focus first, then insert text.
        # Deliver delayed close restoration between those two real input steps.
        area.focus()
        release_drawer_frames(page)
        focused = area.evaluate('(node) => document.activeElement === node')
        page.keyboard.insert_text('关闭菜单后输入并发送')
        page.locator('#cc-command button[type="submit"]').click()
        if not focused:
            print('CLOSE_FOCUS_EVIDENCE', {'focused': focused, 'draft': area.input_value(),
                                         'send_requests': len(sent), 'messages': len(overview(stack)['messages']),
                                         'mentions': page.locator('.cc-mention-chip').count()})
        assert focused, 'A delayed closed drawer must not steal the newer composer focus.'
        expect(page.locator('#cc-feedback')).to_contain_text('部分提醒未送达')
        expect(page.locator('.cc-feed')).to_contain_text('关闭菜单后输入并发送')
        expect(area).to_have_value('')
        expect(page.locator('.cc-mention-chip')).to_have_count(0)
        messages = overview(stack)['messages']
        assert len(sent) == len(messages) == 1
        assert messages[0]['mentions'] == [{'slot_id': slot['id'], 'display_snapshot': '关闭菜单后的连接'}]
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_drawer_focus_lifecycle_respects_reopen_navigation_and_new_focus(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        register_notification_fixture(stack, '焦点生命周期连接')
        click_room_refresh(page)
        mentions = page.locator('[data-cc-action="mentions"]')
        page.locator('#cc-message-input').fill('打开菜单前的草稿')
        mentions.click()
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(mentions).to_be_focused()  # Normal keyboard dismissal remains accessible.
        expect(page.locator('#cc-message-input')).to_have_value('打开菜单前的草稿')

        hold_drawer_frames(page)
        mentions.click()
        expect(page.locator('html')).to_have_attribute('data-close-frame-held', 'yes')
        option = page.locator('[data-cc-action="pick-mention"]')
        option.focus()
        release_drawer_frames(page)
        expect(option).to_be_focused()  # Opening RAF cannot undo a newer focus choice.

        hold_drawer_frames(page)
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-close-frame-held', 'yes')
        mentions.click()  # Reopen before the old close restoration runs.
        option.focus()
        release_drawer_frames(page)
        expect(page.locator('#cc-drawer')).to_be_visible()
        expect(option).to_be_focused()

        hold_drawer_frames(page)
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-close-frame-held', 'yes')
        tab = page.locator('[data-cc-view="agents"]')
        tab.click()
        expect(page.locator('[data-speaking-slot]')).to_have_count(1)
        tab.focus()
        release_drawer_frames(page)
        expect(tab).to_be_focused()
        expect(page.locator('#cc-drawer')).not_to_be_visible()

        # Delay only the completion callback of an already committed real render.
        # Returning to the same view later does not give the old callback focus.
        page.evaluate("""() => {
          const render = renderPage;
          let first = true;
          renderPage = async (...args) => {
            const result = await render(...args);
            if (first) {
              first = false;
              document.documentElement.dataset.tabRenderHeld = 'yes';
              await new Promise(resolve => { window.__ccReleaseTabRender = resolve; });
            }
            return result;
          };
        }""")
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('html')).to_have_attribute('data-tab-render-held', 'yes')
        tab.click()
        expect(page.locator('[data-speaking-slot]')).to_have_count(1)
        page.locator('[data-cc-view="discussion"]').click()
        area = page.locator('#cc-message-input')
        area.fill('新视图里继续输入')
        page.evaluate("""() => {
          window.__ccReleaseTabRender();
          return new Promise(resolve => requestAnimationFrame(resolve));
        }""")
        expect(area).to_be_focused()
        expect(area).to_have_value('新视图里继续输入')
        assert overview(stack)['messages'] == []
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_foreground_refresh_exposes_busy_actions_and_preserves_ime_draft(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.add_init_script("""(() => {
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => {
        const id = schedule(fn, delay, ...args);
        if (delay === 5000 && typeof fn === 'function' && fn.name === 'poll')
          window.__ccScheduledPoll = id;
        return id;
      };
    })();""")
    try:
        login(page, stack)
        page.evaluate('() => clearTimeout(window.__ccScheduledPoll)')
        slot = register_notification_fixture(stack, '慢刷新后的通知连接')
        held = []

        def hold_overview(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['overview'] and not held:
                held.append([route, None])  # Reserve before route.fetch can dispatch another request.
                held[0][1] = route.fetch()
                page.locator('html').evaluate('(n) => n.dataset.chatRefreshHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', hold_overview)
        click_room_refresh(page)
        expect(page.locator('html')).to_have_attribute('data-chat-refresh-held', 'yes')
        # Waiting is a product state: a fresh action must never look ready while
        # its click would be silently dropped by the request lock.
        mentions = page.locator('[data-cc-action="mentions"]')
        expect(mentions).to_be_disabled()
        expect(page.locator('[data-cc-view="jobs"]')).to_be_enabled()
        expect(page.locator('.collaboration')).to_have_attribute('aria-busy', 'true')
        area = page.locator('#cc-message-input')
        expect(area).to_be_enabled()
        area.fill('刷新期间继续输入中文')
        area.dispatch_event('compositionstart')
        area.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        assert overview(stack)['messages'] == []
        area.dispatch_event('compositionend')
        held[0][0].fulfill(response=held[0][1])
        # Keep interception alive through the following options/goals requests.
        # Removing it at response release can strand a newly paused Chromium request.
        expect(mentions).to_be_enabled()
        page.unroute('**/api/collaboration?*', hold_overview)
        mentions.click()
        option = page.locator('[data-cc-action="pick-mention"]')
        expect(option).to_have_attribute('aria-pressed', 'false')
        expect(option).to_contain_text(slot['id'][-6:])
        option.click()
        expect(option).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('Escape')
        expect(area).to_have_value('刷新期间继续输入中文')
        expect(page.locator('.cc-mention-chip')).to_have_text('@慢刷新后的通知连接 ×')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_older_background_poll_cannot_replace_new_foreground_snapshot(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    # Invoke the real registered poll deterministically instead of sleeping five
    # seconds. No API is mocked: only the first real overview response is held.
    page.add_init_script("""(() => {
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => {
        const id = schedule(fn, delay, ...args);
        if (delay === 5000 && typeof fn === 'function' && fn.name === 'poll') {
          window.__ccRunRegisteredPoll = () => {
            clearTimeout(id);
            window.__ccHeldPoll = Promise.resolve(fn());
          };
        }
        return id;
      };
    })();""")
    try:
        login(page, stack)
        held = []

        def hold_first_overview(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['overview'] and not held:
                held.append([route, None])  # Reserve before route.fetch can dispatch another request.
                held[0][1] = route.fetch()
                page.locator('html').evaluate('(n) => n.dataset.oldPollHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', hold_first_overview)
        page.evaluate('() => { window.__ccRunRegisteredPoll(); }')
        expect(page.locator('html')).to_have_attribute('data-old-poll-held', 'yes')
        register_notification_fixture(stack, '新快照里的通知连接')
        click_room_refresh(page)
        expect(page.locator('.cc-member-strip')).to_contain_text('1 个通知位置')
        expect(page.locator('[data-cc-action="refresh"]')).to_be_enabled()
        page.locator('[data-cc-action="mentions"]').click()
        expect(page.locator('[data-cc-action="pick-mention"]')).to_contain_text('新快照里的通知连接')
        held[0][0].fulfill(response=held[0][1])
        page.evaluate('() => window.__ccHeldPoll')
        page.unroute('**/api/collaboration?*', hold_first_overview)
        expect(page.locator('.cc-member-strip')).to_contain_text('1 个通知位置')
        expect(page.locator('#cc-drawer')).to_be_visible()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        expect(page.locator('.cc-mention-chip')).to_have_text('@新快照里的通知连接 ×')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_pending_refresh_allows_navigation_and_failed_refresh_releases_actions(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        page.locator('#cc-message-input').fill('导航与失败刷新期间保留的草稿')
        held = []

        def delay_overview(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['overview'] and not held:
                held.append([route, None])
                held[0][1] = route.fetch()
                page.locator('html').evaluate('(n) => n.dataset.navigationRefreshHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', delay_overview)
        click_room_refresh(page)
        expect(page.locator('html')).to_have_attribute('data-navigation-refresh-held', 'yes')
        page.locator('[data-cc-view="jobs"]').click()
        expect(page.locator('.cc-coordination-list')).to_be_visible()
        held[0][0].fulfill(response=held[0][1])
        page.unroute('**/api/collaboration?*', delay_overview)
        expect(page.locator('[data-cc-view="jobs"]')).to_have_attribute('aria-pressed', 'true')
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('导航与失败刷新期间保留的草稿')

        def fail_overview(route):
            if parse_qs(urlsplit(route.request.url).query).get('kind') == ['overview']:
                route.fulfill(status=422, json={'error': {'code': 'TEST_REFRESH_FAILED', 'message': '合成刷新失败'}})
            else:
                route.continue_()

        page.route('**/api/collaboration?*', fail_overview)
        click_room_refresh(page)
        expect(page.locator('#cc-feedback')).to_contain_text('合成刷新失败')
        expect(page.locator('[data-cc-action="mentions"]')).to_be_enabled()
        expect(page.locator('#cc-message-input')).to_have_value('导航与失败刷新期间保留的草稿')
        page.unroute('**/api/collaboration?*', fail_overview)
        page.locator('[data-cc-action="mentions"]').click()
        expect(page.locator('#cc-drawer')).to_be_visible()
        page.locator('#cc-drawer [data-cc-action="close-drawer"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_ordinary_drawer_close_suppresses_late_thread_response(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        body = '普通话题关闭后不得重新出现的响应'
        page.locator('#cc-message-input').fill(body)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text(body)
        held = []

        def delay_thread(route):
            if parse_qs(urlsplit(route.request.url).query).get('kind') == ['thread'] and not held:
                held.append([route, None])
                held[0][1] = route.fetch()
                page.locator('html').evaluate('(n) => n.dataset.ordinaryThreadHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', delay_thread)
        trigger = page.locator('.cc-feed [data-cc-action="thread"]').first
        trigger.click()
        expect(page.locator('html')).to_have_attribute('data-ordinary-thread-held', 'yes')
        expect(page.locator('#cc-drawer')).to_be_visible()
        close = page.locator('#cc-drawer [data-cc-action="close-drawer"]')
        expect(close).to_be_enabled()
        close.click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        held[0][0].fulfill(response=held[0][1])
        page.unroute('**/api/collaboration?*', delay_thread)
        expect(trigger).to_be_enabled()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-drawer')).not_to_contain_text(body)
    finally:
        context.close()


@pytest.mark.parametrize('failure_kind', ['timeline', 'overview', 'coordination_options'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_permission_loss_at_each_refresh_stage_clears_and_rebinds_remaining_project(
        collaboration_stack, chat_browser_pool, engine, failure_kind):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.add_init_script("""(() => {
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => {
        const id = schedule(fn, delay, ...args);
        if (delay === 5000 && typeof fn === 'function' && fn.name === 'poll')
          window.__ccPermissionPoll = () => {clearTimeout(id);void fn();};
        return id;
      };
    })();""")
    try:
        login(page, stack)
        original = read_scope = scope(stack)
        current = overview(stack)
        conversation = current['conversation']
        other = stack.projects[1]
        stack.must(stack.client.post('/api/collaboration/conversation-project', json={
            'conversation_id': conversation['id'], 'expected_version': conversation['version'],
            'project': other['id'], 'environment_id': 'production', 'idempotency_key': key()}))
        add_message(stack, current['room']['id'], '撤权后必须清除的旧项目记录')
        other_scope = {'project': other['id'], 'environment_id': 'production',
                       'conversation_id': conversation['id']}
        other_room = stack.must(stack.client.get('/api/collaboration', params=other_scope))['room']
        stack.must(stack.client.post('/api/collaboration/message', json={
            **other_scope, 'room_id': other_room['id'], 'body_text': '仍获权项目的安全记录',
            'client_message_id': key(), 'idempotency_key': key(), 'mentions': []}))
        click_room_refresh(page)
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        expect(page.locator('.cc-feed')).to_contain_text('撤权后必须清除的旧项目记录')
        page.locator('#cc-message-input').fill('必须清除的旧项目草稿')
        page.locator('.cc-feed [data-cc-action="thread"]').first.click()
        expect(page.locator('#cc-drawer .cc-message')).not_to_have_count(0)

        directory_attempts = []
        def directory(route):
            directory_attempts.append(True)
            if failure_kind == 'coordination_options' and len(directory_attempts) == 1:
                route.fulfill(status=422, json={'error': {'code': 'TEST_DIRECTORY_FAILED', 'message': '合成目录错误'}})
                return
            response = route.fetch()
            body = response.json()
            body['items'] = [room for room in body['items'] if room['id'] == conversation['id']]
            for room in body['items']:
                room['projects'] = [p for p in room['projects'] if p['project_id'] == other['id']]
                room['visibility_token'] = 'fixture-remaining-project'
            route.fulfill(response=response, json=body)

        def current_authority(route):
            query = parse_qs(urlsplit(route.request.url).query)
            kind = query.get('kind', ['overview'])[0]
            project = query.get('project', [''])[0]
            if project == read_scope['project'] and kind == failure_kind:
                route.fulfill(status=403, json={'error': {'code': 'PERMISSION_DENIED',
                    'message': '原项目访问已撤回'}})
                return
            response = route.fetch()
            body = response.json()
            if project == other['id']:
                if kind == 'timeline':
                    body['items'] = [m for m in body['items'] if m['project_id'] == other['id']]
                    body['visibility_token'] = 'fixture-remaining-project'
                if kind == 'overview' and body.get('conversation'):
                    body['conversation']['projects'] = [p for p in body['conversation']['projects']
                                                        if p['project_id'] == other['id']]
                if kind == 'coordination_options':
                    for participant in body['participants']:
                        participant['project_ids'] = [other['id']]
                        participant['project_capabilities'] = {
                            other['id']: participant.get('project_capabilities', {}).get(other['id'], ['read'])}
            route.fulfill(response=response, json=body)

        page.route('**/api/collaboration/conversations', directory)
        page.route('**/api/collaboration?*', current_authority)
        page.evaluate('() => window.__ccPermissionPoll()')
        expect(page.locator('#cc-feedback')).to_contain_text('旧记录已清除')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-drawer')).to_be_empty()
        expect(page.locator('.cc-feed')).not_to_contain_text('撤权后必须清除')
        expect(page.locator('#cc-message-input')).to_have_value('')
        expect(page.locator('#cc-message-input')).to_be_disabled()
        expect(page.locator('#cc-command button[type="submit"]')).to_be_disabled()
        if failure_kind == 'coordination_options':
            expect(page.locator('#cc-feedback')).to_contain_text('房间目录读取失败')
            retry = page.locator('[data-cc-action="retry-directory"]')
            expect(retry).to_be_enabled()
            retry.click()
            expect(page.locator('#cc-feedback')).to_contain_text('请重新选择仍获权的项目')
        remaining = page.locator(f'[data-cc-partition="{other["id"]}"]')
        expect(remaining).to_be_enabled()
        remaining.click()
        expect(page.locator('.collaboration')).to_have_attribute('data-project', other['id'])
        expect(page.locator('#cc-message-input')).to_be_enabled()
        expect(page.locator('.cc-feed')).to_contain_text('仍获权项目的安全记录')
        expect(page.locator('.cc-feed')).not_to_contain_text('撤权后必须清除')
        assert original['project'] != other['id']
    finally:
        context.close()


@pytest.mark.parametrize('pending_stage', ['visibility', 'directory'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_refresh_recovery_clears_before_waiting_and_respects_new_navigation(
        collaboration_stack, chat_browser_pool, engine, pending_stage):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.add_init_script("""(() => {
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => {
        const id = schedule(fn, delay, ...args);
        if (delay === 5000 && typeof fn === 'function' && fn.name === 'poll')
          window.__ccRunRecoveryPoll = () => {
            clearTimeout(id); window.__ccRecoveryPoll = Promise.resolve(fn());
          };
        return id;
      };
    })();""")
    try:
        login(page, stack)
        body = '发现权限投影变化后立即移除的旧内容'
        page.locator('#cc-message-input').fill(body)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text(body)
        expect(page.locator('#cc-command button[type="submit"]')).to_be_enabled()
        held = []

        def hold(route):
            held.append([route, None])
            held[0][1] = route.fetch()
            page.locator('html').evaluate('(n) => n.dataset.recoveryStageHeld = "yes"')

        def route_scope(route):
            kind = parse_qs(urlsplit(route.request.url).query).get('kind', [''])[0]
            if pending_stage == 'directory' and kind == 'overview':
                route.fulfill(status=403, json={'error': {'code': 'PERMISSION_DENIED', 'message': '范围已撤回'}})
            elif pending_stage == 'visibility' and kind == 'timeline':
                response = route.fetch()
                value = response.json()
                value['visibility_token'] = 'fixture-changed-visible-records'
                value['items'] = []
                route.fulfill(response=response, json=value)
            elif pending_stage == 'visibility' and kind == 'coordination_options' and not held:
                hold(route)
            else:
                route.continue_()

        def route_directory(route):
            if pending_stage == 'directory' and not held:
                hold(route)
            else:
                route.continue_()

        page.route('**/api/collaboration?*', route_scope)
        page.route('**/api/collaboration/conversations', route_directory)
        page.evaluate('() => { window.__ccRunRecoveryPoll(); }')
        expect(page.locator('html')).to_have_attribute('data-recovery-stage-held', 'yes')
        expect(page.locator('.cc-feed')).not_to_contain_text(body)
        expect(page.locator('#cc-message-input')).to_be_disabled()
        page.get_by_role('button', name='项目映射', exact=True).click()
        expect(page.locator('#page h1')).to_have_text('项目映射')
        held[0][0].fulfill(response=held[0][1])
        page.evaluate('() => window.__ccRecoveryPoll')
        page.unroute('**/api/collaboration?*', route_scope)
        page.unroute('**/api/collaboration/conversations', route_directory)
        expect(page.locator('#page h1')).to_have_text('项目映射')
        expect(page.locator('.collaboration')).to_have_count(0)
    finally:
        context.close()
