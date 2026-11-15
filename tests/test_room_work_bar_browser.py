"""Exercise actual room CSS with a deterministic active-work capsule."""
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [320, 390])
def test_work_bar_has_touch_sized_actions_and_truncates_long_title(chat_browser_pool, engine, width):
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 844})
    page = context.new_page()
    root = Path(__file__).resolve().parents[1] / 'web'
    styles = root / 'collaboration-room/styles'
    css = (root / 'tokens.css').read_text() + '\n' + '\n'.join(
        (styles / name).read_text() for name in json.loads((styles / 'manifest.json').read_text())
    )
    try:
        page.set_content('<div class="cr-work-bar"><span class="cr-work-bar-summary"><strong>'
                         + 'long-unbreakable-title-' * 30 + '</strong><small>执行中</small></span>'
                         '<span><button class="cr-button">查看</button><button class="cr-button">全部</button></span></div>')
        page.add_style_tag(content=css)
        bounds = page.locator('.cr-work-bar .cr-button').evaluate_all(
            '(items) => items.map(item => ({width:item.getBoundingClientRect().width,height:item.getBoundingClientRect().height}))')
        assert all(item['height'] >= 44 and item['width'] >= 44 for item in bounds), bounds
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [320, 390, 768, 1280])
@pytest.mark.parametrize('kind,text', [('blocking', '请完成附件上传后发送'), ('blocking', '此消息正在确认发送结果，请勿重复发送'), ('context', '发送后更新原交办')])
def test_current_room_composer_keeps_action_explanations_visible(chat_browser_pool, engine, width, kind, text):
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': 844})
    page = context.new_page()
    styles = Path(__file__).resolve().parents[1] / 'web/collaboration-room/styles'
    css = '\n'.join((styles / name).read_text() for name in json.loads((styles / 'manifest.json').read_text()))
    try:
        page.set_content(f'<form class="cr-composer"><footer><div class="cr-composer-tools">发送给主责</div><span data-cr-compose-status data-kind="{kind}" role="status">{text}</span></footer></form>')
        page.add_style_tag(content=css)
        assert page.locator('[data-cr-compose-status]').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


from tests.collaboration_support import collaboration_stack  # noqa: E402,F401


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_current_room_mobile_failed_upload_and_archive_explanations(collaboration_stack, chat_browser_pool, tmp_path, engine):
    from playwright.sync_api import expect
    from tests.test_room_center_browser import sign_in, create_room
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        sign_in(page, collaboration_stack)
        page.locator('[data-cr-action="rooms"]').click()
        create_room(page, '当前房间手机提示验收')
        expect(page.locator('[data-cr-message-window]')).to_be_hidden()
        page.locator('#cr-message').fill('归档前草稿保留')
        path = tmp_path / 'upload.txt'
        path.write_text('synthetic attachment')
        page.route('**/api/collaboration/rooms/*/attachments*', lambda route: route.fulfill(status=400, json={'error': {'message': 'synthetic upload failure'}}) if route.request.method == 'POST' else route.continue_())
        page.locator('#cr-file').set_input_files(str(path))
        expect(page.locator('[data-cr-files] .cr-file')).to_have_attribute('data-state', 'failed')
        expect(page.locator('[data-cr-compose-status]')).to_be_visible()
        expect(page.locator('[data-cr-compose-status]')).to_contain_text('请完成附件上传')
        expect(page.locator('.cr-send')).to_be_disabled()
        expect(page.locator('#cr-message')).to_have_attribute('aria-describedby', 'cr-compose-status')
        page.locator('[data-cr-action="remove-file"]').click()
        expect(page.locator('.cr-send')).to_be_enabled()
        page.locator('[data-cr-action="settings"]').click()
        page.locator('[data-cr-action="archive-room"]').click()
        page.locator('[data-cr-action="room-state"][data-state="archived"]').click()
        expect(page.locator('#cr-message')).to_be_disabled()
        expect(page.locator('#cr-message')).to_have_value('归档前草稿保留')
        expect(page.locator('[data-cr-compose-status]')).to_be_visible()
        expect(page.locator('[data-cr-compose-status]')).to_contain_text('房间只读')
        expect(page.locator('.cr-send')).to_be_disabled()
        evidence = Path(__file__).resolve().parents[1] / 'docs/evidence/ui-redesign-20261115/room'
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f'{engine}-current-room-mobile-archive.png'), full_page=True)
    finally:
        context.close()
