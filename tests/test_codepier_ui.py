"""Real isolated browser pages: CodePier branding, legacy preference and reflow."""
import json
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright,expect
from tests.test_ui_unification import _login,_navigate,_prepare_native_fixture
OUT=Path(__file__).resolve().parents[1]/'docs/evidence/codepier-rename-20260918/screenshots'


@pytest.mark.parametrize('width,height,scheme',[(1440,900,'light'),(390,844,'dark')])
def test_branding_and_existing_preferences_survive_update(stack,width,height,scheme):
    _prepare_native_fixture(stack);OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':height},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.add_init_script("if(!localStorage.getItem('codepier-appearance'))localStorage.setItem('relay-appearance',"+json.dumps(scheme)+");")
        page.goto(stack.url);expect(page.locator('#login-form')).to_be_visible()
        assert page.title()=='登录 · CodePier'
        assert page.locator('.brand-name').first.inner_text()=='CodePier · 码头'
        assert page.evaluate('() => document.documentElement.dataset.appearance')==scheme
        assert page.evaluate("() => localStorage.getItem('codepier-appearance')") == scheme
        assert page.evaluate("() => localStorage.getItem('relay-appearance')") is None
        assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(OUT/f'login-{width}-{scheme}.png'),animations='disabled')
        _login(page,stack)
        for route in ['overview','devices','integrations','native']:
            _navigate(page,route)
            assert 'CodePier' in page.title()
            assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth+1'),route
            assert 'RELAY' not in page.locator('body').inner_text()
            page.screenshot(path=str(OUT/f'{route}-{width}-{scheme}.png'),animations='disabled')
        assert not errors,errors
        browser.close()
