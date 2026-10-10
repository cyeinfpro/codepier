"""Project target selection stays explicit and survives cancelled/repeated UI flows."""
import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack, key  # noqa: F401
from tests.test_collaboration_delivery_flow import login, join_slot, refresh, scope

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_policy_choices_use_exact_project_snapshot_without_vps_api_or_auto_expansion(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    errors, vps_reads = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        slot = join_slot(stack)
        room = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['room']
        def save(name, projects, enabled=True):
            return stack.must(stack.client.post('/api/vps', json={
                'name': name, 'host': name.lower().replace(' ', '-') + '.example.invalid',
                'username': 'fixture', 'password': 'synthetic-browser-only',
                'project_ids': projects, 'enabled': enabled}))
        selected = save('Bound VPS', [stack.project['id']])
        unavailable = save('Disabled VPS', [stack.project['id']], False)
        outside = save('Unbound VPS', [])
        policy = stack.must(stack.client.post('/api/collaboration/delegation-policy', json={
            **scope(stack), 'conversation_id': room['id'], 'slot_id': slot['id'], 'expected_version': 0,
            'purpose': 'Read the fixture project only', 'capabilities': ['read'],
            'execution_targets': ['project_agent'], 'idempotency_key': key()}))['policy']
        # An old extra API fetch would fail; exact choices must come with the policy snapshot.
        def reject_extra(route):
            vps_reads.append(route.request.url)
            route.abort()
        page.route('**/api/vps?*', reject_extra)
        refresh(page)
        area = page.locator('#cc-message-input')
        area.fill('Keep this draft while checking project targets')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="mention-connect"]').click()
        drawer = page.locator('#cc-drawer')
        drawer.locator('.cc-delegation-policy .cc-connection-settings > summary').click()
        drawer.locator('[data-cc-action="delegation-setup"]').click()
        form = page.locator('#cc-delegation-policy')
        expect(form).to_be_visible()
        expect(form.locator('[name="execution_targets"][value="project_agent"]')).to_be_checked()
        target = form.locator('[name="execution_targets"][value="' + selected['target'] + '"]')
        expect(target).to_be_enabled()
        expect(target).not_to_be_checked()
        expect(form.locator('[value="' + unavailable['target'] + '"]')).to_be_disabled()
        expect(form.locator('[value="' + outside['target'] + '"]')).to_have_count(0)
        expect(form).to_contain_text('已有策略不会自动扩大')
        assert not vps_reads
        target.check()
        drawer.locator('[data-cc-action="close-drawer"]').click()
        expect(drawer).not_to_be_visible()
        expect(area).to_have_value('Keep this draft while checking project targets')
        current = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items'][0]
        assert current['version'] == policy['version'] and current['execution_targets'] == ['project_agent']
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="mention-connect"]').click()
        drawer.locator('.cc-delegation-policy .cc-connection-settings > summary').click()
        drawer.locator('[data-cc-action="delegation-setup"]').click()
        expect(target).not_to_be_checked()
        page.keyboard.press('Escape')
        expect(drawer).not_to_be_visible()
        expect(area).to_have_value('Keep this draft while checking project targets')
        assert not errors and not vps_reads
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [390, 1440])
def test_automatic_delegation_panel_preserves_recipient_and_real_id_without_dispatching_replies(
        collaboration_stack, chat_browser_pool, engine, width, tmp_path):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    errors, sent = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: sent.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/collaboration/message') else None)
    try:
        login(page, stack)
        join_slot(stack)
        refresh(page)
        area = page.locator('#cc-message-input')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-mention"]').click()
        page.keyboard.press('Escape')
        page.locator('#cc-send-mode [data-cc-action="delegation-setup"]').click()
        form = page.locator('#cc-delegation-policy')
        expect(form.locator('[name="automatic_delegation"]')).not_to_be_checked()
        form.locator('[name="purpose"]').fill('Read fixture files and report evidence')
        form.locator('[name="execution_targets"][value="project_agent"]').check()
        form.locator('[name="automatic_delegation"]').check()
        form.locator('[name="automatic_acceptance"]').fill('Report actual content and limits')
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('自动委托已开启')
        policy = stack.must(stack.client.get('/api/collaboration', params={
            **scope(stack), 'kind': 'delegation_policies'}))['items'][0]
        assert policy['automatic_delegation'] is True
        page.keyboard.press('Escape')
        # Saving a rule does not change a discussion draft into an execution request.
        expect(page.locator('[name="delegation_policy"]')).to_have_value('')
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-delegation"]').click()
        mode = page.locator('[name="delegation_policy"]')
        expect(mode).to_have_value('auto:' + policy['id'])
        expect(page.locator('.cc-delegation-context')).to_contain_text('发送后保留助手')
        expect(page.locator('[data-cc-action="delegation-scope"]')).to_have_count(0)
        for text in ['first automatic fixture task', 'second automatic fixture task']:
            area.fill(text)
            page.locator('#cc-command button[type="submit"]').click()
            expect(area).to_have_value('')
            expect(mode).to_have_value('auto:' + policy['id'])
            expect(page.locator('.cc-mention-chip')).to_contain_text('协作 dot')
        rows = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['messages']
        tasks = [row for row in rows if row['body'].get('delegation')]
        assert len(tasks) == 2
        ids = {row['body']['delegation']['delegation_id'] for row in tasks}
        assert len(ids) == 2
        assert all(row['body']['delegation']['automatic'] for row in tasks)
        assert sent[0]['dispatch_mode'] == 'automatic' and 'delegation' not in sent[0]
        retry = stack.must(stack.client.post('/api/collaboration/message', json=sent[0]))
        assert retry['delegation_id'] in ids
        assert len(stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['messages']) == 2
        # Clipboard unavailable: expose the real saved ID in a selectable field.
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})")
        first = page.locator('.cc-message').filter(has_text='first automatic fixture task')
        first.locator('[data-cc-action="delegation-id"]').click()
        assert page.locator('.cc-delegation-id').input_value() in ids
        page.keyboard.press('Escape')
        first.locator('[data-cc-action="reply"]').click()
        expect(mode).to_have_value('')
        area.fill('ordinary reply stays a discussion')
        page.locator('#cc-command button[type="submit"]').click()
        expect(area).to_have_value('')
        assert sent[-1]['reply_to_id'] and 'dispatch_mode' not in sent[-1]
        rows = stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['messages']
        assert len(rows) == 3 and len([row for row in rows if row['body'].get('delegation')]) == 2
        # A changed rule cannot silently apply to an already selected task.
        page.locator('[data-cc-action="mentions"]').click()
        page.locator('[data-cc-action="pick-delegation"]').click()
        area.fill('retain after policy pause')
        stack.must(stack.client.post('/api/collaboration/delegation-policy-control', json={
            **scope(stack), 'policy_id': policy['id'], 'expected_version': policy['version'],
            'action': 'pause', 'idempotency_key': key()}))
        refresh(page)
        expect(page.locator('.cc-delegation-context')).to_contain_text('已改变、到期或不可用')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('正文已保留')
        expect(area).to_have_value('retain after policy pause')
        assert len(stack.must(stack.client.get('/api/collaboration', params=scope(stack)))['messages']) == 3
        assert not errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'automatic-{engine}-{width}.png'), full_page=True)
    finally:
        context.close()
