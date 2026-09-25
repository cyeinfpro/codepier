"""Check browser-parsed conversation/approval styles, not source whitespace."""
from pathlib import Path
import re
import pytest


ROOT = Path(__file__).parents[1]
@pytest.fixture
def styles(chat_browser_pool):
    page = chat_browser_pool('chromium').new_page()
    try:
        page.set_content('<main></main>')
        for name in ('chat.css', 'computer.css'):
            page.add_style_tag(path=str(ROOT / 'web' / name))
        # CSSOM preserves the effective declaration/token values while normalizing
        # legal source quotation and spacing differences introduced by formatters.
        yield page.evaluate("[...document.styleSheets].map(sheet=>[...sheet.cssRules].map(rule=>rule.cssText).join('\\n'))")
    finally:
        page.context.close()


def compact(value):
    return re.sub(r'\s+', '', value).replace("'", '"')


def test_chat_aliases_every_shared_design_token_with_standalone_fallbacks(styles):
    CHAT = styles[0]
    css = compact(CHAT)
    light = {'bg': ('panel', '#ffffff'),
 'side': ('side', '#fafafa'),
 'surface': ('surface', '#f0f0f0'),
 'raised': ('raised', '#ffffff'),
 'hover': ('hover', '#e8e8e8'),
 'selected': ('selected', '#e3e3e3'),
 'line': ('line', '#e3e3e3'),
 'border': ('border', '#cccccc'),
 'fg': ('text', '#272727'),
 'muted': ('muted', '#606060'),
 'soft': ('soft', '#626262'),
 'accent': ('accent', '#746018'),
 'accent-soft': ('accent-soft', '#f2eee0'),
 'primary': ('primary', '#f3d970'),
 'primary-text': ('primary-text', '#29261c'),
 'danger': ('danger', '#a13c35'),
 'danger-bg': ('danger-bg', '#f8efed'),
 'warning': ('warning', '#785b22'),
 'warning-bg': ('warning-bg', '#f7f3e9'),
 'success': ('success', '#34684e'),
 'success-bg': ('success-bg', '#edf3ef')}
    for chat_name, (ui_name, fallback) in light.items():
        assert f'--chat-{chat_name}:var(--ui-{ui_name},{fallback})' in css
    assert '--chat-font:var(--ui-font,' in css
    assert '--chat-mono:var(--ui-mono,' in css
    assert '--chat-shadow:var(--ui-shadow,' in css
    assert '--chat-scrim:var(--ui-scrim,' in css


def test_chat_dark_fallbacks_match_the_shared_contract(styles):
    CHAT = styles[0]
    css = compact(CHAT)
    selector = '.chat-workspace[data-appearance="dark"],html[data-appearance="dark"].chat-workspace'
    assert selector in css
    dark = {'bg': ('panel', '#222222'),
 'side': ('side', '#1e1e1e'),
 'surface': ('surface', '#2b2b2b'),
 'raised': ('raised', '#2b2b2b'),
 'hover': ('hover', '#343434'),
 'selected': ('selected', '#393939'),
 'line': ('line', '#3a3a3a'),
 'border': ('border', '#515151'),
 'fg': ('text', '#e5e5e5'),
 'accent': ('accent', '#d1bd75'),
 'primary': ('primary', '#d1bd75'),
 'primary-text': ('primary-text', '#27241b'),
 'danger': ('danger', '#e3a39b'),
 'warning': ('warning', '#d5b77e'),
 'success': ('success', '#9ec4ae')}
    dark_block = css.split(selector, 1)[1].split('}', 1)[0]
    for chat_name, (ui_name, fallback) in dark.items():
        assert f'--chat-{chat_name}:var(--ui-{ui_name},{fallback})' in dark_block


def test_conversation_keeps_focus_responsive_and_layer_contracts(styles):
    CHAT = styles[0]
    css = compact(CHAT)
    assert ':focus-visible' in CHAT
    assert '@media(max-width:760px)' in css
    assert '@media(max-width:360px)' in css
    assert '@media(max-height:520px)' in css
    assert '@media(prefers-reduced-motion:reduce)' in css
    assert 'max-width:calc(100%-20px)' in css
    assert 'var(--chat-scrim)' in CHAT
    assert 'pointer-events:none' in css


def test_computer_surfaces_use_shared_tokens_and_semantic_states(styles):
    COMPUTER = styles[1]
    css = compact(COMPUTER)
    for token in (
        'ui-panel', 'ui-raised', 'ui-surface', 'ui-line', 'ui-border',
        'ui-text', 'ui-muted', 'ui-focus', 'ui-primary',
        'ui-primary-text', 'ui-danger', 'ui-danger-bg',
        'ui-warning', 'ui-warning-bg', 'ui-font', 'ui-mono',
        'ui-control-radius', 'ui-panel-radius', 'ui-shadow',
    ):
        assert f'var(--{token},' in css
    for state in ('pending', 'expired', 'unavailable'):
        assert f'[data-state="{state}"]' in css
    assert ':focus-visible' in COMPUTER
    assert '@media(max-width:680px)' in css
    assert 'max-height:calc(100dvh-16px)' in css
    assert '#54e7cc' not in COMPUTER.lower()
    assert 'border:2px' not in css
