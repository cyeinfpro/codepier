"""Panel navigation and retained drafts, exercised in both browser engines."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_chat_browser import chat_page
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture


OUT = Path(__file__).resolve().parents[1] / 'docs/evidence/cli-workspace-20260922'


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height', [(1440,900), (1024,768), (390,844), (320,568), (667,375)])
def test_panel_return_is_visible_with_history_hidden_or_open(chat_page, width, height):
    page = chat_page
    page.set_viewport_size({'width': width, 'height': height})
    page.emulate_media(reduced_motion='reduce')
    page.fill('#chat-compose', 'Keep this draft when returning to the panel')
    back = page.get_by_role('button', name='返回面板', exact=True)
    expect(back).to_be_visible()
    box = back.bounding_box()
    assert box['width'] >= 100 and box['height'] >= 44
    assert 0 <= box['y'] < 30
    page.click('#chat-history-toggle')
    back.click(trial=True)
    if width > 760:
        page.click('#chat-focus')
        back.click(trial=True)
    page.locator('#chat-back').focus()
    page.keyboard.press('Enter')
    assert page.evaluate('S.page') == 'overview'
    assert page.evaluate('chatView().draft') == 'Keep this draft when returning to the panel'
    assert not page.evaluate(r"requests.some(r => /\/(start|stop|chat_prompt)$/.test(r.path))")


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('scheme,width,height', [('light',1440,900), ('dark',390,844), ('light',320,568)])
def test_real_panel_roundtrip_retains_session_draft_and_theme(stack, engine, scheme, width, height):
    _prepare_native_fixture(stack)
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={'width':width, 'height':height}, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            _login(page, stack, 'overview')
            page.evaluate('scheme => CodePierAppearance.setPreference(scheme)', scheme)
            page.screenshot(path=str(OUT / f'{engine}-{scheme}-{width}-panel.png'))
            _navigate(page, 'native')
            expect(page.locator('#chat-root')).to_be_visible()
            expect(page.locator('#chat-effort-select')).to_be_enabled(timeout=20_000)
            assert page.locator('#chat-scroll').evaluate('(el) => el.scrollTop') == 0
            expect(page.locator('#chat-empty h2')).to_be_in_viewport()
            page.fill('#chat-compose', '保留草稿与模型选择，返回面板后继续')
            page.screenshot(path=str(OUT / f'{engine}-{scheme}-{width}-chat.png'))
            page.locator('#chat-back').click()
            expect(page.locator('.editorial-board')).to_be_visible()
            assert page.evaluate("document.body.classList.contains('chat-mode')") is False
            _navigate(page, 'native')
            expect(page.locator('#chat-compose')).to_have_value('保留草稿与模型选择，返回面板后继续')
            expect(page.locator('#chat-root')).to_have_attribute('data-appearance', scheme)
            for selector in ['#chat-back', '#chat-compose', '#chat-send', '#chat-model-picker']:
                bounds = page.locator(selector).bounding_box()
                assert bounds and bounds['x'] >= 0 and bounds['y'] >= 0, (selector, bounds)
                assert bounds['x'] + bounds['width'] <= width + 1, (selector, bounds)
                assert bounds['y'] + bounds['height'] <= height + 1, (selector, bounds)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
        finally:
            browser.close()


def test_failed_chat_load_cannot_overwrite_a_new_panel_page(stack):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            _login(page, stack, 'overview')
            page.evaluate('''() => {
              window.savedChatPage=chatPage;
              chatPage=()=>new Promise((resolve,reject)=>window.failChatLoad=reject);
              window.loadingChat=navigate('native');
            }''')
            page.wait_for_function('typeof failChatLoad === "function"')
            _navigate(page, 'projects')
            page.evaluate('''async () => {
              failChatLoad(new Error('A late failed chat load'));
              await loadingChat;
              chatPage=savedChatPage;
            }''')
            expect(page.locator('.workspace-project-row').first).to_be_visible()
            expect(page.get_by_text('A late failed chat load')).to_have_count(0)
            assert page.evaluate('S.page') == 'projects'
        finally:
            browser.close()
