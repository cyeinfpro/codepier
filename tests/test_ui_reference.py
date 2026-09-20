"""Black/white/yellow workspace regression checks on isolated Hub/Agent data."""
from __future__ import annotations

from pathlib import Path
from scripts.check_release import check_web_assets
from shared.util import VERSION

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_ui_unification import (
    _layout,
    _login,
    _native_management_round_trip,
    _prepare_native_fixture,
    _set_scheme,
)

ROOT = Path(__file__).resolve().parents[1]


def _uncovered(locator) -> None:
    locator.click(trial=True)
    assert locator.evaluate("""el => {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return !!top && (top === el || el.contains(top));
    }""")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(320, 568), (390, 844), (768, 1024), (1440, 1000)])
def test_reference_navigation_filter_and_reachability(stack, engine, scheme, width, height):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, stack)
        _set_scheme(page, scheme)
        _layout(page, f"{engine}-{scheme}-{width}-overview")
        dock = page.locator(".mobile-dock")
        if width <= 900:
            expect(dock).to_be_visible()
            expect(dock.locator("button")).to_have_count(5)
            for button in dock.locator("button").all():
                _uncovered(button)
                rect = button.bounding_box()
                assert rect["width"] >= 40 and rect["height"] >= 44, rect
            dock.locator('[data-nav="projects"]').click()
            expect(page.locator("#page h1")).to_have_text("项目映射")
            expect(dock.locator('[data-nav="projects"]')).to_have_attribute("aria-current", "page")
            more = dock.locator('[data-action="toggle-menu"]')
            more.click()
            expect(page.locator(".mobile-close")).to_be_focused()
            assert page.locator(".main").evaluate("el => el.inert")
            page.keyboard.press("Escape")
            expect(more).to_be_focused()
            assert not page.locator(".main").evaluate("el => el.inert")
        else:
            expect(dock).to_be_hidden()
            page.locator('.nav [data-nav="projects"]').click()
            expect(page.locator("#page h1")).to_have_text("项目映射")
        search = page.locator("#project-query")
        search.fill("__no_matching_reference_project__")
        expect(page.locator("#project-no-match")).to_be_visible()
        expect(page.locator('[data-project-row]:visible')).to_have_count(0)
        search.fill("")
        expect(page.locator("#project-no-match")).to_be_hidden()
        assert page.locator('[data-project-row]:visible').count() > 0
        _layout(page, f"{engine}-{scheme}-{width}-projects")
        page.locator("#page h1").click()
        help_summary = page.locator(".page-help > summary").last
        help_summary.scroll_into_view_if_needed()
        _uncovered(help_summary)
        help_summary.click()
        expect(page.locator(".page-help").last).to_have_attribute("open", "")
        if width <= 900:
            page.set_viewport_size({"width": 1440, "height": 1000})
            expect(dock).to_be_hidden()
            expect(page.locator(".sidebar")).to_be_visible()
            assert not page.locator(".main").evaluate("el => el.inert")
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width,height", [(390, 844), (1440, 1000)])
def test_reference_chat_stays_focused_and_keeps_draft(stack, engine, width, height):
    _prepare_native_fixture(stack)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        submitted = []
        page.on("request", lambda request: submitted.append(request.url)
                if request.method == "POST" and "/api/native/sessions" in request.url else None)
        _login(page, stack, "native")
        expect(page.locator("#chat-root")).to_be_visible()
        expect(page.locator(".mobile-dock")).to_be_hidden()
        draft = "保留这份草稿，先不要发送。"
        _native_management_round_trip(page, draft)
        expect(page.locator("#chat-compose")).to_have_value(draft)
        expect(page.locator(".mobile-dock")).to_be_hidden()
        _set_scheme(page, "dark")
        _layout(page, f"{engine}-{width}-native")
        assert not submitted, submitted
        browser.close()


def test_reference_workspace_asset_order_and_native_terminal_absence():
    html = (ROOT / "web/index.html").read_text()
    assert html.index("tokens.css") < html.index("styles.css") < html.index("workspace.css") < html.index("chat.css")
    assert {"tokens.css", "styles.css", "workspace.css", "chat.css"} <= check_web_assets(ROOT, VERSION).keys()
    assert "xterm" not in html
    css = (ROOT / "web/workspace.css").read_text()
    assert ".mobile-dock" in css and "safe-area-inset-bottom" in css
    assert "chat-mode" in css
