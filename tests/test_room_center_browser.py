"""Room-first journeys against a real isolated Hub and real browser engines.

No native dot, real subscription, WeChat account or model call is used. Member
readiness is intentionally left pending: an invitation is not a working dot.
"""
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack  # noqa: F401


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def sign_in(page, stack):
    page.goto(stack.url + '/#collaboration')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('.cr-shell')).to_be_visible()


def create_room(page, title, *, lose_first_response=False):
    attempts = []
    route_pattern = '**/api/collaboration/rooms'
    def lost_creation(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            # Commit the actual HTTP request, then lose only its response.
            # Recovery must use the exact original body after dialog closure.
            response = route.fetch()
            assert response.ok, response.text()
            route.abort('failed')
        else:
            route.continue_()
    if lose_first_response:
        page.route(route_pattern, lost_creation)
    page.locator('[data-cr-action="create"]').first.click()
    page.locator('[data-cr-form="create"] [name="title"]').fill(title)
    page.locator('[data-cr-form="create"] button[type="submit"]').evaluate(
        '(button) => {button.click();button.click();}'
    )
    if lose_first_response:
        expect(page.locator('[data-cr-panel-recovery] [data-cr-action="recover-action"]')).to_be_visible()
        page.locator('[data-cr-action="close"]').click()
        page.locator('[data-cr-action="create"]').first.click()
        page.locator('[data-cr-panel-recovery] [data-cr-action="recover-action"]').evaluate(
            '(button) => {button.click();button.click();}'
        )
    expect(page.locator('dialog.cr-panel')).not_to_be_visible()
    expect(page.locator('[data-cr-title]')).to_have_text(title)
    if lose_first_response:
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        page.unroute(route_pattern, lost_creation)


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_room_empty_projects_member_recovery_and_owner_scope(
    collaboration_stack, chat_browser_pool, tmp_path, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 1050})
    page = context.new_page()
    page.add_init_script("""(() => {
      const NativeEventSource=window.EventSource;
      window.roomChangeHints=0;
      window.EventSource=class extends NativeEventSource {
        constructor(...args){super(...args);this.addEventListener('change',()=>window.roomChangeHints++);}
      };
    })();""")
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        sign_in(page, stack)
        create_room(page, '房间中心验收', lose_first_response=True)
        rooms = stack.must(stack.client.get('/api/collaboration/rooms'))['items']
        selected = [room for room in rooms if room['title'] == '房间中心验收']
        assert len(selected) == 1
        room_id = selected[0]['id']
        assert selected[0]['projects'] == []

        # Empty-room input is real persisted data, not a fake hidden project.
        payload = '先记录我们的目标 <img src=x onerror="window.roomXss=true">'
        page.locator('#cr-message').fill(payload)
        page.locator('.cr-send').evaluate('(button) => {button.click();button.click();}')
        expect(page.locator('#cr-message')).to_have_value('')
        expect(page.locator('.cr-message')).to_have_count(1)
        assert not page.evaluate('Boolean(window.roomXss)')
        assert page.locator('.cr-message img').count() == 0
        saved = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}/messages'))
        assert len(saved['messages']) == 1
        assert saved['messages'][0]['body_text'] == payload
        assert saved['works'] == []

        # A real raw-file HTTP upload can be sent without text. Only the
        # canonical server manifest provides the authenticated download path.
        contents = '浏览器上传的原始说明。\n保留中文与换行。'.encode()
        attachment_path = tmp_path / '房间说明.txt'
        attachment_path.write_bytes(contents)
        page.locator('#cr-file').set_input_files(str(attachment_path))
        expect(page.locator('[data-cr-files] .cr-file[data-state="ready"]')).to_have_count(1)
        expect(page.locator('[data-cr-files]')).to_contain_text('已上传，待发送')
        expect(page.locator('.cr-send')).to_be_enabled()
        page.locator('.cr-send').click()
        expect(page.locator('.cr-message')).to_have_count(2)
        expect(page.locator('[data-cr-files]')).not_to_be_visible()
        download = page.locator('.cr-message a.cr-message-file')
        expect(download).to_contain_text('房间说明.txt')
        saved = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}/messages'))
        file_message = next(message for message in saved['messages'] if message.get('attachments'))
        assert file_message['body_text'] == '' and len(file_message['attachments']) == 1
        assert download.get_attribute('href') == file_message['attachments'][0]['download_path']
        assert stack.client.get(download.get_attribute('href')).content == contents
        with page.expect_download() as downloaded:
            download.click()
        assert Path(downloaded.value.path()).read_bytes() == contents

        # Seed a timed canonical room approval, then extend it through the UI.
        deadline = time.time() + 3600
        stack.must(stack.client.put(f'/api/collaboration/rooms/{room_id}/permissions', json={
            'expected_version': 0, 'scope': {'project_scopes': [{
                'project_id': stack.projects[0]['id'], 'environment_id': 'production',
                'capabilities': ['read'], 'execution_targets': [],
                'lifetime': {'mode': 'timed', 'expires_at': deadline},
            }]}, 'channels': {name: {'mode': 'follow_room', 'scope': {'project_scopes': []}}
                              for name in ('weixin', 'telegram')}, 'idempotency_key': uuid4().hex,
        }))
        page.reload()
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 1')

        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        page.locator('dialog [data-cr-action="add-member"]').click()
        page.locator('[name="connection_mode"][value="existing"]').check()
        expect(page.locator('[data-cr-connection-status]')).to_contain_text('其他房间还没有连好的 dot')
        page.locator('[name="connection_mode"][value="new"]').check()
        page.locator('[data-cr-form="member"] [name="name"]').fill('产品 dot')
        expect(page.locator('[data-cr-form="member"] [data-project-id]')).to_have_count(0)
        expect(page.locator('[data-cr-form="member"] [name="lifetime"]')).to_have_count(0)
        page.locator('[data-cr-form="member"] button[type="submit"]').click()
        expect(page.locator('[data-cr-flow-view] h3').first).to_have_text('等待 dot 接入')
        assert '"action":"join"' in page.locator('.cr-instructions').input_value()
        assert '"code":"CPR-' in page.locator('.cr-instructions').input_value()
        record = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
        assert len(record['members']) == 1
        member = record['members'][0]
        assert member['state'] == 'invited'
        assert member['evidence']['round_trip_verified'] is False
        member_id, flow_id = member['id'], member['flow_id']

        # Refresh resumes this member/flow. It never makes a ready claim.
        page.reload()
        expect(page.locator('[data-cr-title]')).to_have_text('房间中心验收')
        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        page.locator(f'[data-cr-action="member-detail"][data-id="{member_id}"]').click()
        expect(page.locator('[data-cr-flow-view] h3').first).to_have_text('等待 dot 接入')
        page.locator('[data-flow-action="pause"]').click()
        expect(page.locator('[data-cr-flow-view] h3').first).to_have_text('已暂停')
        page.locator('[data-flow-action="resume"]').first.click()
        expect(page.locator('[data-cr-flow-view] h3').first).to_have_text('等待 dot 接入')
        same = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['members'][0]
        assert same['id'] == member_id and same['flow_id'] == flow_id

        # The member entry opens the same room-wide permission page. Changing
        # capabilities keeps its explicit timed deadline, with no second editor.
        page.locator('dialog [data-cr-action="permissions"]').click()
        form = page.locator('[data-cr-form="permissions"]')
        first_project = form.locator(f'[data-permission-section="room"] [data-project-id="{stack.projects[0]["id"]}"]')
        first_project.locator('[data-permission-level]').select_option('write')
        with page.expect_response(lambda response: response.request.method == 'PUT'
                                  and response.url.endswith(f'/rooms/{room_id}/permissions')) as first_save:
            form.locator('button[type="submit"]').click()
        assert first_save.value.ok
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        updated = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['members'][0]
        first_scope = next(scope for scope in updated['project_scopes'] if scope['project_id'] == stack.projects[0]['id'])
        assert 'write' in first_scope['capabilities']
        assert first_scope['lifetime'] == {'mode': 'timed', 'expires_at': deadline}

        # Adding another project preserves the existing approval's exact expiry
        # and version. It cannot implicitly renew old work authority.
        second_project = form.locator(f'[data-permission-section="room"] [data-project-id="{stack.projects[1]["id"]}"]')
        expect(second_project.locator('[data-permission-level]')).to_have_value('')
        second_project.locator('[data-permission-level]').select_option('read')
        with page.expect_response(lambda response: response.request.method == 'PUT'
                                  and response.url.endswith(f'/rooms/{room_id}/permissions')) as scope_save:
            form.locator('button[type="submit"]').click()
        assert scope_save.value.ok
        payload = scope_save.value.request.post_data_json
        assert 'member_approvals' not in payload
        assert next(rule for rule in payload['scope']['project_scopes']
                    if rule['project_id'] == stack.projects[0]['id'])['lifetime'] == first_scope['lifetime']
        updated = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['members'][0]
        preserved = next(scope for scope in updated['project_scopes'] if scope['project_id'] == stack.projects[0]['id'])
        added = next(scope for scope in updated['project_scopes'] if scope['project_id'] == stack.projects[1]['id'])
        assert preserved['lifetime'] == first_scope['lifetime']
        assert preserved['version'] == first_scope['version']
        assert preserved['capabilities'] == first_scope['capabilities']
        assert added['lifetime']['mode'] == 'until_revoked' and added['lifetime']['expires_at'] is None
        assert updated['id'] == member_id and updated['flow_id'] == flow_id
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 2')
        saved_policy = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}/permissions'))
        assert saved_policy['version'] == payload['expected_version'] + 1
        assert saved_policy['scope'] == scope_save.value.json()['scope']
        assert {rule['project_id'] for rule in saved_policy['scope']['project_scopes']} == {
            project['id'] for project in stack.projects[:2]}
        # Project membership changes invalidate the old access projection. The
        # stale dialog must close; explicitly load the canonical policy again.
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        expect(page.locator('[data-cr-feedback]')).to_contain_text('权限设置已保存')
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        expect(form).to_be_visible()
        for project in stack.projects[:2]:
            form.locator(f'[data-permission-section="room"] [data-project-id="{project["id"]}"] [data-permission-level]').select_option('')
        form.locator('button[type="submit"]').click()
        expect(page.locator('[data-cr-project-count]')).to_have_text('项目 0')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        assert (stack.projectalpha / 'README.md').is_file()
        expect(page.locator('.cr-message')).to_have_count(2)

        # Observe the actual SSE channel while reading older content. A new
        # canonical message must reveal the jump control without moving either
        # the scroll position or the original message's DOM node.
        for index in range(14):
            stack.must(stack.client.post(f'/api/collaboration/rooms/{room_id}/messages', json={
                'body_text': f'回看消息 {index}\n第一段背景\n第二段约束\n第三段细节',
                'client_message_id': uuid4().hex, 'idempotency_key': uuid4().hex,
            }))
        expect(page.locator('.cr-message')).to_have_count(16, timeout=15000)
        page.evaluate("""() => {
          const feed=document.querySelector('.cr-feed');feed.scrollTop=80;
          window.roomScrollTop=feed.scrollTop;
          window.roomScrollNode=document.querySelector('.cr-message');
          window.roomScrollRect=roomScrollNode.getBoundingClientRect().top;
        }""")
        assert page.evaluate('document.querySelector(".cr-feed").scrollHeight > document.querySelector(".cr-feed").clientHeight + 150')
        before_hint = page.evaluate('window.roomChangeHints')
        stack.must(stack.client.post(f'/api/collaboration/rooms/{room_id}/messages', json={
            'body_text': '正在回看时抵达的新消息', 'client_message_id': uuid4().hex,
            'idempotency_key': uuid4().hex,
        }))
        page.wait_for_function('(before) => window.roomChangeHints > before', arg=before_hint)
        expect(page.locator('.cr-message')).to_have_count(17)
        expect(page.locator('[data-cr-latest]')).to_be_visible()
        assert page.evaluate('Math.abs(document.querySelector(".cr-feed").scrollTop-roomScrollTop)<2')
        assert page.evaluate('roomScrollNode===document.querySelector(".cr-message") && Math.abs(roomScrollNode.getBoundingClientRect().top-roomScrollRect)<2')
        page.locator('[data-cr-latest]').focus()
        page.keyboard.press('Enter')
        expect(page.locator('[data-cr-latest]')).not_to_be_visible()

        screenshot_dir = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_dir / f'{engine}-room-center-desktop.png'), full_page=True)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_deferred_panel_autofocus_does_not_steal_user_focus(collaboration_stack, chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.add_init_script("""(() => {
      const original=window.requestAnimationFrame;
      window.heldPanelFocus=[];
      window.requestAnimationFrame=(callback) => {
        if (String(callback).includes('[autofocus]')) {
          window.heldPanelFocus.push(callback);
          return 0;
        }
        return original.call(window, callback);
      };
    })();""")
    try:
        sign_in(page, collaboration_stack)
        page.locator('.cr-empty [data-cr-action="create"]').click()
        checkbox = page.locator('[data-cr-form="create"] button[type="submit"]')
        expect(checkbox).to_be_visible()
        checkbox.focus()
        assert page.evaluate('window.heldPanelFocus.length') == 1
        page.evaluate('window.heldPanelFocus.splice(0).forEach((callback) => callback(performance.now()))')
        expect(checkbox).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_mobile_room_switch_preserves_ime_draft_and_stable_message_node(
    collaboration_stack, chat_browser_pool, tmp_path, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(
        viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True,
        reduced_motion='reduce',
    )
    page = context.new_page()
    try:
        sign_in(page, stack)
        # The empty-state create button remains reachable with the sidebar closed.
        page.locator('.cr-empty [data-cr-action="create"]').focus()
        page.keyboard.press('Enter')
        expect(page.locator('[data-cr-form="create"] [name="title"]')).to_be_focused()
        page.keyboard.insert_text('移动房间 A')
        # macOS WebKit follows Safari's native full-control traversal shortcut;
        # plain Tab can intentionally skip checkboxes/buttons on this platform.
        tab = 'Alt+Tab' if engine == 'webkit' and sys.platform == 'darwin' else 'Tab'
        projects = page.locator('[data-cr-form="create"] [name="project"]')
        expect(projects).to_have_count(len(stack.projects))
        expect(page.locator('[data-cr-form="create"] [name="project"]:checked')).to_have_count(0)
        for project in projects.all():
            page.keyboard.press(tab)
            expect(project).to_be_focused()
        page.keyboard.press(tab)
        expect(page.locator('[data-cr-form="create"] button[type="submit"]')).to_be_focused()
        assert page.evaluate('matchMedia("(prefers-reduced-motion: reduce)").matches')
        assert page.locator('dialog.cr-panel').evaluate('(node)=>getComputedStyle(node).animationName') == 'none'
        assert page.locator('.cr-feed').evaluate('(node)=>getComputedStyle(node).scrollBehavior') == 'auto'
        page.keyboard.press('Enter')
        expect(page.locator('[data-cr-title]')).to_have_text('移动房间 A')
        touch_targets = page.locator('.cr-topbar button:visible, .cr-send, .cr-attach').evaluate_all(
            '(nodes)=>nodes.map(node=>({name:node.getAttribute("aria-label")||node.textContent,width:node.getBoundingClientRect().width,height:node.getBoundingClientRect().height}))'
        )
        assert all(target['width'] >= 44 and target['height'] >= 44 for target in touch_targets), touch_targets
        page.locator('#cr-message').fill('移动端第一条消息')
        page.locator('.cr-send').click()
        expect(page.locator('.cr-message')).to_have_count(1)
        page.locator('#cr-message').fill('尚未发送的中文草稿')
        page.evaluate("""() => {
          window.roomEditor=document.querySelector('#cr-message');
          window.roomMessage=document.querySelector('.cr-message');
          roomEditor.focus();roomEditor.setSelectionRange(2,5);
          roomEditor.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'输入'}));
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_timeout(500)
        assert page.evaluate("roomEditor===document.querySelector('#cr-message') && roomMessage===document.querySelector('.cr-message')")
        assert page.locator('#cr-message').input_value() == '尚未发送的中文草稿'
        assert page.evaluate('[roomEditor.selectionStart,roomEditor.selectionEnd]') == [2, 5]
        # The composition itself is a labelled synthetic event. A trusted Enter
        # without a native IME would insert a newline into the selected text;
        # dispatch the composing key event instead, then verify no submission or
        # editor/selection replacement occurred.
        page.evaluate("roomEditor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}))")
        expect(page.locator('.cr-message')).to_have_count(1)
        expect(page.locator('#cr-message')).to_have_value('尚未发送的中文草稿')
        assert page.evaluate('[roomEditor.selectionStart,roomEditor.selectionEnd]') == [2, 5]
        page.evaluate("roomEditor.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'输入'}))")

        room_id = next(room['id'] for room in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
                       if room['title'] == '移动房间 A')
        for index in range(12):
            stack.must(stack.client.post(f'/api/collaboration/rooms/{room_id}/messages', json={
                'body_text': f'手机历史 {index}\n待回看的背景信息\n保留这段阅读位置',
                'client_message_id': uuid4().hex, 'idempotency_key': uuid4().hex,
            }))
        expect(page.locator('.cr-message')).to_have_count(13, timeout=15000)
        page.evaluate("""() => {
          const feed=document.querySelector('.cr-feed');feed.scrollTop=85;
          window.savedMobileScroll=feed.scrollTop;
          const edge=feed.getBoundingClientRect().top;
          const anchor=[...document.querySelectorAll('[data-message-id]')].find(node=>node.getBoundingClientRect().bottom>edge+8);
          window.savedMobileAnchor={id:anchor.dataset.messageId,offset:anchor.getBoundingClientRect().top-edge};
        }""")

        page.locator('[data-cr-action="rooms"]').click()
        page.locator('.cr-sidebar [data-cr-action="create"]').click()
        page.locator('[data-cr-form="create"] [name="title"]').fill('移动房间 B')
        page.locator('[data-cr-form="create"] button[type="submit"]').click()
        expect(page.locator('[data-cr-title]')).to_have_text('移动房间 B')
        expect(page.locator('#cr-message')).to_have_value('')
        page.locator('[data-cr-action="rooms"]').click()
        page.locator('.cr-room', has_text='移动房间 A').click()
        expect(page.locator('#cr-message')).to_have_value('尚未发送的中文草稿')
        expect(page.locator('.cr-message')).to_have_count(13)
        page.wait_for_function("""() => {
          const feed=document.querySelector('.cr-feed'),anchor=document.querySelector('[data-message-id="'+savedMobileAnchor.id+'"]');
          return anchor && Math.abs(anchor.getBoundingClientRect().top-feed.getBoundingClientRect().top-savedMobileAnchor.offset)<2;
        }""")
        assert page.evaluate('Math.abs(document.querySelector(".cr-feed").scrollTop-savedMobileScroll)<2')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        expect(page.locator('dialog.cr-panel')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        assert page.locator('.cr-room-actions [data-cr-action="members"]').evaluate('(node)=>document.activeElement===node')
        screenshots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / f'{engine}-room-center-mobile.png'), full_page=True)
    finally:
        context.close()
