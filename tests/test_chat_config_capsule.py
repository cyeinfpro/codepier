"""Configuration capsule and shared command dispatch regression checks."""
from pathlib import Path
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page

@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width', [320, 390, 768, 1024, 1440])
def test_capsule_closes_and_restores_focus_without_changing_draft(chat_page, width):
    p = chat_page
    p.set_viewport_size({'width': width, 'height': 900})
    p.fill('#chat-compose', 'Keep this draft')
    expect(p.locator('#chat-options')).to_be_hidden()
    capsule = p.locator('#chat-options-toggle')
    expect(capsule).to_have_attribute('aria-haspopup', 'dialog')
    assert '工作目录：' in capsule.get_attribute('title')
    assert '思考：' in capsule.get_attribute('title')
    for _ in range(2):
        capsule.click()
        expect(p.locator('#chat-options')).to_be_visible()
        p.keyboard.press('Escape')
        expect(capsule).to_be_focused()
        expect(p.locator('#chat-options')).to_be_hidden()
        assert p.locator('#chat-options').evaluate('(el) => el.inert')
    expect(p.locator('#chat-compose')).to_have_value('Keep this draft')
    assert p.evaluate('document.documentElement.scrollWidth <= innerWidth')

@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_palette_toggle_and_panel_owned_shortcut(chat_page):
    p = chat_page
    p.focus('#chat-compose')
    p.keyboard.press('Control+k')
    expect(p.locator('.chat-palette')).to_have_count(1)
    p.keyboard.press('Control+k')
    expect(p.locator('.chat-palette')).to_have_count(0)
    # When the panel dispatcher is installed the chat root must not swallow K.
    p.evaluate("window.uiCommand=()=>{};window.bubbled=0;document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='k')window.bubbled++})")
    p.focus('#chat-compose')
    p.keyboard.press('Control+k')
    assert p.evaluate('bubbled') == 1
    expect(p.locator('.chat-palette')).to_have_count(0)

def test_panel_and_chat_buttons_use_single_command_entry():
    source = (Path(__file__).resolve().parents[1] / 'web/ui.js').read_text()
    assert "if ($('#chat-root') && typeof chatPalette === 'function') return chatPalette();" in source
    assert "if ($('#chat-root')) chatPalette();" not in source

@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width', [390, 1440])
def test_slash_model_has_visible_anchor_and_escape_focus(chat_page, width):
    p = chat_page
    p.set_viewport_size({'width': width, 'height': 900})
    expect(p.locator('#chat-options')).to_be_hidden()
    p.fill('#chat-compose', '/model ')
    p.press('#chat-compose', 'Enter')
    expect(p.locator('#chat-model-search')).to_be_visible()
    expect(p.locator('#chat-model-picker')).to_be_visible()
    p.keyboard.press('Escape')
    expect(p.locator('#chat-model-picker')).to_be_focused()
    assert not p.evaluate("requests.some(r => r.path.endsWith('/start'))")

@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_delayed_breakpoint_event_preserves_open_model_anchor(chat_page):
    p = chat_page
    p.set_viewport_size({'width': 390, 'height': 900})
    p.evaluate('chatModelPopover()')
    expect(p.locator('#chat-model-search')).to_be_visible()
    # The browser may deliver this queued event after the user opened /model.
    p.evaluate("ChatUI.mobileMedia.dispatchEvent(new Event('change'))")
    expect(p.locator('#chat-model-picker')).to_be_visible()
    expect(p.locator('#chat-model-search')).to_be_focused()
    p.keyboard.press('Escape')
    expect(p.locator('#chat-model-picker')).to_be_focused()
    assert not p.evaluate("requests.some(r => r.path.endsWith('/start'))")
