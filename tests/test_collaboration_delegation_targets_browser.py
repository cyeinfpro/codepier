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
