"""Real isolated browsers verify room-only scopes and honest blocking."""
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
        form = page.locator('#cc-room-projects')
        form.locator(f'[name="room_projects"][value="{stack.projects[1]["id"]}"]').check()
        expect(form.locator('[data-room-project-summary]')).to_contain_text('消息连接')
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(form.locator('[name="room_projects"]')).to_have_count(len(stack.projects))
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
        original_dot_id = joined['slot']['id']
        open_add_project(page)
        form = page.locator('#cc-room-projects')
        added = form.locator(f'[name="room_projects"][value="{stack.projects[1]["id"]}"]')
        added.check()
        summary = form.locator('[data-room-project-summary]')
        for text in (stack.projects[1]['alias'], 'production', '范围验收助手', '读取', '修改', '执行',
                     '执行目标', '当前助手', '有效期', '并非目录沙箱'):
            expect(summary).to_contain_text(text)
        expect(form.locator('[name="confirm"]')).to_have_count(0)
        expect(form.locator('select[name="dot_id"]')).to_have_count(0)
        expect(form.locator('button[type="submit"]')).to_have_text('保存')
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('房间项目已保存')
        room = overview(stack)['conversation']
        assert room['working_scope']['dot_id'] == original_dot_id
        assert len(room['working_scope']['project_ids']) == 2
        original_connection = room['connection']
        open_add_project(page)
        added.uncheck()
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('房间项目已保存')
        room = overview(stack)['conversation']
        assert room['working_scope']['project_ids'] == [stack.project['id']]
        assert room['connection'] == original_connection
        assert room['working_scope']['dot_id'] == original_dot_id
        assert len(room['projects']) == 1
        assert not errors
    finally:
        context.close()
