"""M3 cutover through the default room UI and real isolated Hub HTTP.

The old room, messages, approval, safe work control, candidate connection and
activation are real. No external native host or provider is used. The unverified
old member is explicitly removed, never falsely promoted to a verified dot.
"""
import re

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_room_center_browser import create_room, sign_in


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def legacy_room(stack):
    scope = {'project': stack.project['id'], 'environment_id': 'production'}
    conversation = stack.must(stack.client.post('/api/collaboration/conversation', json={
        'title': '原房间迁移验收', 'projects': [scope], 'idempotency_key': key(),
    }))['conversation']
    identifier = conversation['id']
    partition = conversation['projects'][0]['room_id']
    message = stack.must(stack.client.post('/api/collaboration/message', json={
        **scope, 'room_id': partition, 'conversation_id': identifier,
        'body_text': '迁移前保存的原消息', 'mentions': [],
        'client_message_id': key(), 'idempotency_key': key(),
    }))['message']
    goal = stack.must(stack.client.post('/api/collaboration/goal-create', json={
        **scope, 'conversation_id': identifier, 'objective': '保留原工作并安全停止',
        'acceptance': '核对原操作，不重复启动', 'project_ids': [stack.project['id']],
        'participant_grant_ids': [stack.grant], 'capabilities': ['read'],
        'idempotency_key': key(),
    }))['goal']
    stack.must(stack.client.post('/api/collaboration/goal-approve', json={
        **scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
        'digest': goal['digest'], 'idempotency_key': key(),
    }))
    stack.must(stack.client.post('/api/collaboration/dot', json={
        **scope, 'conversation_id': identifier, 'label': '待迁移的原 dot',
        'duplex': True, 'capabilities': ['read'], 'confirm_tasks': True,
        'idempotency_key': key(),
    }))
    room = stack.must(stack.client.get(f'/api/collaboration/rooms/{identifier}'))['room']
    assert room['legacy'] and room['read_only'] and not room['migration']['eligible']
    assert len(room['members']) == 1 and room['members'][0]['legacy']
    return room, message, goal


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('lost_before_commit', [False, True], ids=['saved-receipt', 'confirmed-not-saved'])
def test_legacy_room_read_only_safe_stop_explicit_cutover_and_original_receipt(
    collaboration_stack, chat_browser_pool, engine, lost_before_commit
):
    stack = collaboration_stack
    room, original_message, goal = legacy_room(stack)
    room_id = room['id']
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1050})
    page = context.new_page()
    page.add_init_script("""(() => {
      window.panelTrace=[];
      window.roomRefreshEvidence={started:0,completed:0,active:0,succeeded:[],cancelled:[],failed:[]};
      let roomStore;
      Object.defineProperty(window,'CodePierRoomStore',{configurable:true,
        get(){return roomStore;},set(value){
          roomStore=value;
          const create=value.create;
          value.create=function(...args){
            const instance=create.apply(this,args),refresh=instance.refresh;
            instance.refresh=function(...refreshArgs){
              const evidence=window.roomRefreshEvidence;
              const refreshId=++evidence.started;evidence.active++;
              const pending=refresh.apply(this,refreshArgs);
              const finished=()=>{evidence.completed++;evidence.active--;};
              Promise.resolve(pending).then(()=>{
                evidence.succeeded.push(refreshId);finished();
              },error=>{
                if(error?.name==='AbortError')evidence.cancelled.push(refreshId);
                else evidence.failed.push(String(error));
                finished();
              });
              return pending;
            };
            return instance;
          };
        }});
      for(const method of ['showModal','close']) {
        const original=HTMLDialogElement.prototype[method];
        HTMLDialogElement.prototype[method]=function(...args){
          panelTrace.push({method,at:performance.now(),open:this.open,stack:new Error().stack});
          return original.apply(this,args);
        };
      }
      document.addEventListener('close',event=>panelTrace.push({event:'close',at:performance.now(),open:event.target.open}),true);
      document.addEventListener('cancel',event=>panelTrace.push({event:'cancel',at:performance.now(),open:event.target.open}),true);
      document.addEventListener('click',event=>{const button=event.target.closest('[data-cr-action]');
        if(button)panelTrace.push({event:'click',action:button.dataset.crAction,at:performance.now()});},true);
    })()""")
    errors, writes, activations = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url)
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}
            and '/api/collaboration/' in request.url else None)
    try:
        sign_in(page, stack)
        page.locator(f'[data-cr-action="select"][data-id="{room_id}"]').click()
        expect(page.locator('[data-cr-title]')).to_have_text('原房间迁移验收')
        expect(page.locator('[data-cr-legacy]')).to_contain_text('原房间当前只读')
        expect(page.locator(f'[data-message-id="{original_message["id"]}"]')).to_contain_text('迁移前保存的原消息')
        editor = page.locator('#cr-message')
        expect(editor).to_be_disabled()
        expect(page.locator('.cr-send')).to_be_disabled()
        expect(page.locator('#cr-file')).to_be_disabled()
        expect(page.locator('[data-cr-action="attach"]')).to_be_disabled()
        expect(page.locator('[data-cr-action="reply"]').first).to_be_disabled()

        # Seed only a local pre-existing draft, not a server projection or a
        # ready claim. Refresh, keyboard submission and room switching must
        # retain it without sending it while the real server room is legacy.
        editor.evaluate("""node => { node.value='迁移前未发送的草稿';
          node.dispatchEvent(new Event('input',{bubbles:true})); }""")
        before = len(writes)
        editor.dispatch_event('keydown', {'key': 'Enter', 'code': 'Enter'})
        page.locator('#cr-composer').evaluate('(form) => form.requestSubmit()')
        # Observe the actual background read before checking its effects and
        # starting the separate project-panel action. The event only schedules
        # this work; the unchanged draft is not evidence that it has finished.
        refresh_started = page.evaluate('roomRefreshEvidence.started')
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        # Selecting a room legitimately aborts stale reads. Require a new
        # successful refresh, not an older completion or a cancelled request.
        page.wait_for_function('before => roomRefreshEvidence.succeeded.some(id => id > before) && '
            'roomRefreshEvidence.active === 0', arg=refresh_started, timeout=5000)
        assert not page.evaluate('roomRefreshEvidence.failed')
        expect(editor).to_have_value('迁移前未发送的草稿')
        assert len(writes) == before

        page.locator('.cr-room-actions [data-cr-action="projects"]').click()
        expect(page.locator('[data-cr-form="projects"] button[type="submit"]')).to_be_disabled()
        expect(page.locator('[data-cr-form="projects"] input').first).to_be_disabled()
        page.keyboard.press('Escape')
        page.locator('.cr-room-actions [data-cr-action="weixin"]').click()
        expect(page.locator('[data-cr-channel-view]')).to_contain_text('不会创建新的微信接入')
        assert len(writes) == before
        page.keyboard.press('Escape')
        page.locator('.cr-room-actions [data-cr-action="settings"]').click()
        page.locator('[data-cr-action="diagnostics"]').click()
        expect(page.locator('[data-cr-diagnostics-view]')).to_contain_text('只读')
        assert len(writes) == before
        page.keyboard.press('Escape')

        # Hub independently refuses fresh new-surface writes, even if a stale
        # or scripted client bypasses disabled DOM controls.
        for endpoint, payload in [
            (f'/rooms/{room_id}/messages', {'body_text': '不能误发', 'client_message_id': key()}),
            (f'/rooms/{room_id}/members', {'name': '不能新建', 'project_scopes': []}),
            (f'/rooms/{room_id}/channels/weixin/connect', {}),
        ]:
            response = stack.client.post('/api/collaboration' + endpoint,
                json={**payload, 'idempotency_key': key()})
            assert response.status_code == 409, response.text
            assert response.json()['error']['code'] == 'ROOM_MIGRATION_REQUIRED'
        response = stack.client.post(f'/api/collaboration/rooms/{room_id}/attachments',
            content=b'not uploaded', headers={'Content-Type': 'application/octet-stream',
                'X-File-Name': 'blocked.txt', 'X-Upload-Key': key()})
        assert response.status_code == 409, response.text

        # This is the actual old approval, not a synthetic work projection.
        page.locator(f'[data-cr-action="work-detail"][data-id="{goal["id"]}"]').click()
        expect(page.locator('[data-cr-work-view]')).to_contain_text('核对原操作，不重复启动')
        expect(page.locator('[data-work-action="pause"]')).to_be_enabled()
        expect(page.locator('[data-cr-form="work-recovery"]')).to_have_count(0)
        expect(page.locator('[data-cr-action="supplement"]')).to_have_count(0)
        page.locator('[data-work-action="pause"]').click()
        expect(page.locator('[data-work-action="resume"]')).to_have_count(0)
        expect(page.locator('[data-cr-work-view]')).to_contain_text('工作已暂停')
        paused = stack.must(stack.client.get(f'/api/collaboration/work/{goal["id"]}'))['work']
        denied = stack.client.post(f'/api/collaboration/work/{goal["id"]}/control', json={
            'action': 'resume', 'expected_version': paused['version'], 'idempotency_key': key(),
        })
        assert denied.status_code == 409 and denied.json()['error']['code'] == 'ROOM_MIGRATION_REQUIRED'
        page.locator('[data-work-action="cancel"]').click()
        expect(page.locator('[data-cr-work-view]')).to_contain_text('工作已取消')
        assert stack.must(stack.client.get(f'/api/collaboration/work/{goal["id"]}'))['work']['id'] == goal['id']
        page.keyboard.press('Escape')

        page.locator('[data-cr-migration-open]').click()
        expect(page.locator('[data-cr-form="migration"] button')).to_be_disabled()
        expect(page.locator('[name="confirm_migration"]')).to_be_disabled()
        page.locator('[data-migration-blocker="MEMBER_MIGRATION_REQUIRED"] [data-cr-action="member-detail"]').click()
        page.locator('[data-flow-action="migrate"]').first.click()
        expect(page.locator('.cr-instructions')).to_have_value(re.compile('room_connect_join'))
        still_legacy = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
        assert still_legacy['legacy'] and still_legacy['read_only']
        assert not still_legacy['migration']['eligible']
        page.locator('[data-cr-action="member-scope"]').click()
        expect(page.locator('[data-cr-form="member-scope"] button[type="submit"]')).to_be_disabled()
        page.keyboard.press('Escape')
        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        expect(page.locator('dialog [data-cr-action="add-member"]')).to_be_disabled()
        page.locator('[data-cr-action="member-detail"]').click()
        page.locator('[data-cr-action="remove-member"]').click()
        page.locator('[data-cr-action="confirm-remove-member"]').click()
        # The confirmation panel has no member rows, so wait for the refreshed
        # members panel itself before asserting the member was removed.
        expect(page.locator('dialog [data-cr-action="add-member"]')).to_be_disabled()
        expect(page.locator('.cr-member')).to_have_count(0)
        page.keyboard.press('Escape')
        page.locator('[data-cr-migration-open]').click()
        form = page.locator('[data-cr-form="migration"]')
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        expect(form.locator('[name="confirm_migration"]')).not_to_be_checked()
        assert stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['legacy']

        # Server eligibility enables review, never automatic activation. Any
        # changed room version clears the original owner confirmation.
        form.locator('[name="confirm_migration"]').check()
        current = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
        stack.must(stack.client.patch(f'/api/collaboration/rooms/{room_id}', json={
            'title': '核对后的原房间', 'expected_version': current['version'], 'idempotency_key': key(),
        }))
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('[data-cr-title]')).to_have_text('核对后的原房间')
        expect(form.locator('[name="confirm_migration"]')).not_to_be_checked()
        expect(page.locator('[data-cr-panel-error]')).to_contain_text('重新核对并确认')
        form.locator('button[type="submit"]').click()
        assert stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['legacy']

        committed = []
        def lose_activation_response(route):
            activations.append(route.request.post_data_json)
            if len(activations) == 1:
                if not lost_before_commit:
                    response = route.fetch()
                    assert response.ok, response.text()
                    committed.append(response.json()['room'])
                route.abort('failed')
            else:
                response = route.fetch()
                if response.ok:
                    committed.append(response.json()['room'])
                elif lost_before_commit:
                    assert response.status == 409
                    assert response.json()['error']['not_saved'] is True
                route.fulfill(response=response)
        page.route(f'**/api/collaboration/rooms/{room_id}/migration', lose_activation_response)
        form.locator('[name="confirm_migration"]').check()
        form.locator('button[type="submit"]').evaluate('(button) => {button.click();button.click();}')
        expect(page.locator('[data-cr-panel-recovery] [data-cr-action="recover-action"]')).to_be_visible()
        page.locator('[data-cr-action="close"]').click()
        if lost_before_commit:
            current = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
            assert current['legacy'] and not committed
            stack.must(stack.client.patch(f'/api/collaboration/rooms/{room_id}', json={
                'title': '版本变化后重新核对', 'expected_version': current['version'], 'idempotency_key': key(),
            }))
        page.locator('[data-cr-pending] [data-cr-action="recover-action"]').click()
        expect(page.locator('[data-cr-pending] [data-cr-action="recover-action"]')).to_have_count(0)
        assert len(activations) == 2 and activations[0] == activations[1]
        if lost_before_commit:
            expect(page.locator('[data-cr-pending] [data-mutation-state="not_saved"]')).to_contain_text('原请求未保存')
            assert not committed
            assert stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']['legacy']
            page.locator('[data-cr-pending] [data-cr-action="migration"]').click()
            expect(form.locator('[name="confirm_migration"]')).not_to_be_checked()
            form.locator('button[type="submit"]').click()
            assert len(activations) == 2  # a newer version is not consent
            form.locator('[name="confirm_migration"]').check()
            form.locator('button[type="submit"]').click()
            expect(page.locator('dialog.cr-panel')).not_to_be_visible()
            assert len(activations) == 3
            assert activations[2]['idempotency_key'] != activations[0]['idempotency_key']
            assert activations[2]['expected_version'] > activations[0]['expected_version']
        current = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
        assert not current['legacy'] and not current['read_only']
        assert current['migration']['status'] == 'complete'
        assert current['version'] == committed[0]['version']
        expect(page.locator('[data-cr-legacy]')).not_to_be_visible()
        if lost_before_commit:
            # A successful explicit activation resolves this obsolete prompt.
            # The earlier same-key failure remains true; it must not invite
            # another confirmation after the server has completed migration.
            expect(page.locator('[data-cr-pending] [data-mutation-state="not_saved"]')).to_have_count(0)
            expect(page.locator('[data-cr-panel-recovery] [data-mutation-state="not_saved"]')).to_have_count(0)
            expect(page.locator('[data-cr-pending] [data-cr-action="migration"]')).to_have_count(0)
        expect(editor).to_be_enabled()
        expect(editor).to_have_value('迁移前未发送的草稿')
        assert len(stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}/messages'))['messages']) == 1
        page.locator('.cr-send').click()
        expect(editor).to_have_value('')
        expect(page.locator('.cr-message')).to_have_count(2)
        assert not errors, errors
    finally:
        print('PANEL_TRACE', page.evaluate('window.panelTrace || []'))
        print('ROOM_REFRESH_TRACE', page.evaluate('window.roomRefreshEvidence'))
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_saved_business_receipt_remains_queryable_after_room_becomes_read_only(
    collaboration_stack, chat_browser_pool, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 1050})
    page = context.new_page()
    attempts, paused_versions = [], []
    try:
        sign_in(page, stack)
        create_room(page, '原回执只读核对')
        room = next(room for room in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
            if room['title'] == '原回执只读核对')
        room_id = room['id']
        page.locator('#cr-message').fill('在暂停前写好的独立草稿')
        def lose_project_response(route):
            attempts.append(route.request.post_data_json)
            if len(attempts) == 1:
                response = route.fetch()
                assert response.ok, response.text()
                saved = response.json()['room']
                paused = stack.must(stack.client.patch(f'/api/collaboration/rooms/{room_id}', json={
                    'state': 'paused', 'expected_version': saved['version'], 'idempotency_key': key(),
                }))['room']
                paused_versions.append(paused['version'])
                route.abort('failed')
            else:
                route.continue_()
        page.route(f'**/api/collaboration/rooms/{room_id}/projects', lose_project_response)
        page.locator('.cr-room-actions [data-cr-action="projects"]').click()
        page.locator(f'[data-cr-form="projects"] [value="{stack.project["id"]}"]').check()
        page.locator('[data-cr-form="projects"] button[type="submit"]').click()
        # Pausing changes the access projection. SSE intentionally clears the
        # stale dialog before reauthorizing; the original receipt survives in
        # the main room, and reopening projects must remain read-only.
        expect(page.locator('[data-cr-pending] [data-cr-action="recover-action"]')).to_be_visible()
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(page.locator('#cr-message')).to_be_disabled()
        expect(page.locator('#cr-message')).to_have_value('在暂停前写好的独立草稿')
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        page.locator('.cr-room-actions [data-cr-action="projects"]').click()
        expect(page.locator('[data-cr-form="projects"] button[type="submit"]')).to_be_disabled()
        recovery = page.locator('[data-cr-panel-recovery] [data-cr-action="recover-action"]')
        expect(recovery).to_be_enabled()
        expect(page.locator('[data-cr-panel-recovery]')).to_contain_text('仅按原请求键核对')
        recovery.click()
        expect(page.locator('dialog.cr-panel')).not_to_be_visible()
        expect(page.locator('[data-cr-pending] [data-cr-action="recover-action"]')).to_have_count(0)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        current = stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}'))['room']
        assert current['state'] == 'paused' and current['read_only']
        assert current['version'] == paused_versions[0]
        assert [project['project_id'] for project in current['projects']] == [stack.project['id']]
        expect(page.locator('#cr-message')).to_have_value('在暂停前写好的独立草稿')
        assert stack.must(stack.client.get(f'/api/collaboration/rooms/{room_id}/messages'))['messages'] == []
    finally:
        context.close()
