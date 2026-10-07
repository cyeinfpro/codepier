"""Real panel/Hub user flow with isolated Chromium and WebKit contexts.

All records are synthetic fixtures. No real subscription, collector, model CLI
or production project is enabled by this test.
"""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.collaboration_support import collaboration_stack  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_collaboration_panel_complete_readonly_flow(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    grant = stack.must(stack.client.post('/api/grants', json={
        'label': 'Browser fixture read-only', 'scopes': ['read'],
        'projects': [stack.project['id']], 'days': 1}))
    browser = chat_browser_pool(engine)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    screenshots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(stack.url + '/#projects')
        page.fill('#username', 'admin')
        page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
        expect(page.locator('#page h1')).to_have_text('协作中心')
        expect(page.locator('#cc-scope [name="project"]')).to_have_value(stack.project['id'])
        page.locator('[data-cc-action="create-room"]').click()
        expect(page.locator('#cc-command')).to_be_visible()
        page.locator('[data-cc-view="agents"]').click()
        page.locator('.cc-advanced > summary').click()
        page.get_by_text('登记智能体用途', exact=True).click()
        page.locator('#cc-agent [name="label"]').fill('巡检 Work')
        page.locator('#cc-agent [name="grant_id"]').select_option(grant['grant_id'])
        page.locator('#cc-agent button[type="submit"]').click()
        expect(page.locator('.cc-main')).to_contain_text('用途绑定有效')
        page.locator('[data-cc-view="discussion"]').click()
        payload = '检查这次超时，保留事实与假设。<img src=x onerror="window.collaborationXss=true">'
        page.locator('#cc-command [name="request"]').fill(payload)
        # Navigation preserves only this in-memory draft, not private browser data.
        page.locator('[data-cc-view="jobs"]').click()
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('#cc-command [name="request"]')).to_have_value(payload)
        page.locator('#cc-command button[type="submit"]').evaluate('(button) => {button.click();button.click();}')
        expect(page.locator('.cc-feed')).to_contain_text(payload)
        expect(page.locator('#cc-command [name="request"]')).to_have_value('')
        assert not page.evaluate('Boolean(window.collaborationXss)')
        assert page.locator('.cc-feed img').count() == 0
        overview = stack.must(stack.client.get('/api/collaboration', params={
            'project': stack.project['id'], 'environment_id': 'production'}))
        assert len(overview['jobs']) == 0  # Ordinary discussion never creates a task.
        page.locator('.cc-feed [data-cc-action="convert"]').first.click()
        expect(page.locator('#cc-task-review')).to_be_visible()
        page.locator('#cc-task-review [name="target_project"]').select_option(stack.project['id'])
        option = page.locator('#cc-task-review [name="assignee"] option').nth(1)
        agent_id = option.get_attribute('value')
        page.locator('#cc-task-review [name="assignee"]').select_option(agent_id)
        page.locator('#cc-task-review button[type="submit"]').click()
        assert len(stack.must(stack.client.get('/api/collaboration', params={
            'project': stack.project['id'], 'environment_id': 'production'}))['jobs']) == 0
        page.locator('#cc-task-review [name="confirm"]').check()
        page.locator('#cc-task-review button[type="submit"]').evaluate('(button) => {button.click();button.click();}')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        overview = stack.must(stack.client.get('/api/collaboration', params={
            'project': stack.project['id'], 'environment_id': 'production'}))
        assert len(overview['jobs']) == 1
        assert overview['jobs'][0]['delivery_status'] == 'manual_claim_required'
        page.screenshot(path=str(screenshots / f'{engine}-discussion-desktop.png'), full_page=True)
        page.locator('[data-cc-view="jobs"]').click()
        expect(page.locator('.cc-main')).to_contain_text('待人工领取')
        page.locator('[data-cc-detail="job"] summary').click()
        expect(page.locator('.cc-detail')).to_contain_text('fencing_token')
        page.locator('[data-cc-view="monitor"]').click()
        page.get_by_text('登记固定只读探针', exact=True).click()
        page.locator('#cc-probe [name="label"]').fill('只读健康入口')
        page.locator('#cc-probe [name="url"]').fill('https://fixture.example.invalid/ready')
        page.locator('#cc-probe input[required][type="checkbox"]').check()
        page.locator('#cc-probe button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('操作已保存')
        page.get_by_text('新建或修订监控草稿', exact=True).click()
        probe_id = page.locator('#cc-plan [name="probe"] option').nth(1).get_attribute('value')
        page.locator('#cc-plan [name="probe"]').select_option(probe_id)
        page.locator('[data-cc-action="plan-template"]').click()
        candidate = json.loads(page.locator('#cc-plan [name="plan_json"]').input_value())
        assert candidate['rules'][0]['probe_id'] == probe_id
        page.locator('[data-cc-action="validate-plan"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('计划校验通过')
        page.locator('#cc-plan button[type="submit"]').click()
        expect(page.locator('#page')).to_contain_text('最新草稿 1')
        # Activation cannot silently inherit the prior draft save consent.
        page.locator('[data-cc-action="activate-plan"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('请先审阅')
        page.get_by_text('审阅最新计划与固定目标', exact=True).click()
        expect(page.locator('.cc-targets')).to_contain_text('https://fixture.example.invalid/ready')
        page.locator('#cc-approval').check()
        page.locator('[data-cc-action="activate-plan"]').click()
        expect(page.locator('#page')).to_contain_text('采集开关未启用')
        assert stack.must(stack.client.get('/api/collaboration', params={
            'project': stack.project['id'], 'environment_id': 'production'}))['plan']['last_collected'] is None
        page.screenshot(path=str(screenshots / f'{engine}-monitor-desktop.png'), full_page=True)
        for width in (1024, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            for view in ('discussion', 'jobs', 'monitor', 'agents'):
                page.locator(f'[data-cc-view="{view}"]').click()
                expect(page.locator(f'[data-cc-view="{view}"]')).to_have_attribute('aria-pressed', 'true')
                expect(page.locator('.cc-main')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (engine, width, view)
            page.evaluate('window.scrollTo(0, 0)')
            page.wait_for_function('window.scrollY === 0')
            page.screenshot(path=str(screenshots / f'{engine}-agents-{width}.png'), full_page=True)
        # Changing the login while client-side hashing is awaiting must not
        # issue the old instruction under the replacement session's CSRF.
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('[data-cc-view="discussion"]')).to_have_attribute('aria-pressed', 'true')
        page.locator('#cc-command [name="request"]').fill('Must not be sent after session replacement')
        command_requests = []
        page.on('request', lambda request: command_requests.append(request.url)
                if request.url.endswith('/api/collaboration/message') else None)
        page.evaluate('''() => {
            window.ccOriginalSession = S.session;
            window.ccOriginalDigest = crypto.subtle.digest.bind(crypto.subtle);
            crypto.subtle.digest = async (...args) => {
                const result = await window.ccOriginalDigest(...args);
                await new Promise(resolve => { window.ccFinishDigest = resolve; });
                return result;
            };
        }''')
        page.locator('#cc-command button[type="submit"]').click()
        page.wait_for_function('typeof window.ccFinishDigest === "function"')
        page.evaluate('() => { S.session = {...S.session}; window.ccFinishDigest(); }')
        expect(page.locator('#cc-command button[type="submit"]')).to_be_enabled()
        assert command_requests == []
        page.evaluate('() => { S.session = window.ccOriginalSession; crypto.subtle.digest = window.ccOriginalDigest; }')
        assert not errors, errors
    finally:
        context.close()
