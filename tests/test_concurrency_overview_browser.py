"""Live overview renders while an independent statistics request is held."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
def test_overview_does_not_wait_for_statistics_or_revive_after_navigation(
        chat_browser_pool, stack, browser_kind):
    context = chat_browser_pool(browser_kind).new_context()
    page = context.new_page()
    pending, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/token-usage*", lambda route: pending.append(route))
    try:
        page.goto(stack.url + "/#overview")
        page.fill("#username", "admin")
        page.fill("#password", stack.password)
        with page.expect_request("**/api/token-usage*"):
            page.click("#login-form button")
        expect(page.locator(".workspace-stats")).to_be_visible()
        expect(page.locator("[data-token-dashboard]")).to_contain_text("正在读取统计")
        expect(page.locator("#page")).not_to_have_attribute("aria-busy", "true")
        assert pending
        pending.pop(0).continue_()
        expect(page.locator(".token-dashboard-metric")).to_have_count(2)
        with page.expect_request("**/api/token-usage*"):
            page.evaluate("renderPage(false)")
        expect(page.locator(".workspace-stats")).to_be_visible()
        page.evaluate("navigate('projects')")
        expect(page.locator("[data-token-dashboard]")).to_have_count(0)
        for route in pending:
            route.continue_()
        expect(page.locator("[data-token-dashboard]")).to_have_count(0)
        assert not errors
    finally:
        context.close()
