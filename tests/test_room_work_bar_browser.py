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
