"""Real isolated browsers verify explicit add scopes and honest blocking."""
import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_collaboration_chatroom_browser import login, open_add_project
from tests.test_collaboration_dots_flow import create_dot, join_dot, overview

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_add_cannot_claim_execution_without_an_assistant(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        open_add_project(page)
        form = page.locator('#cc-add-project')
        form.locator('[name="project"]').select_option(stack.projects[1]['id'])
        expect(form.locator('[data-project-authorization]')).to_contain_text('尚无已接入')
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(form.locator('input[type="checkbox"]')).to_have_count(0)
        assert len(overview(stack)['conversation']['projects']) == 1
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_single_scope_summary_one_click_and_explicit_revoke(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1280, 'height': 980})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        joined = join_dot(stack, create_dot(stack, label='范围验收助手'))
        open_add_project(page)
        form = page.locator('#cc-add-project')
        form.locator('[name="project"]').select_option(stack.projects[1]['id'])
        summary = form.locator('[data-project-authorization]')
        for text in (stack.projects[1]['alias'], 'production', '范围验收助手', '读取', '修改', '执行',
                     '项目 Agent', '回传位置', '有效期至', '并非目录沙箱'):
            expect(summary).to_contain_text(text)
        expect(form.locator('input[type="checkbox"]')).to_have_count(0)
        expect(form.locator('[name="dot_id"]')).to_have_count(0)
        expect(form.locator('button[type="submit"]')).to_have_text('添加并授权')
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('项目已加入并授权')
        records = overview(stack)['conversation']['project_authorizations']
        assert len(records) == 1 and records[0]['dot_id'] == joined['slot']['id']
        page.locator('[data-cc-more]').click()
        page.locator('.cc-room-menu [data-cc-action="context"]').click()
        page.locator('[data-cc-action="project-scope-revoke"]').click()
        page.locator('#cc-project-scope-revoke button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('项目授权已撤销')
        assert not overview(stack)['conversation']['project_authorizations']
        assert len(overview(stack)['conversation']['projects']) == 2
        assert not errors
    finally:
        context.close()
