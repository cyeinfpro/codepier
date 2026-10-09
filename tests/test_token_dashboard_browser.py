"""Real overview card at desktop/mobile; no fabricated zero or stale navigation."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
def test_overview_token_filters_missing_data_and_navigation(chat_browser_pool, stack, viewport):
    context = chat_browser_pool('chromium').new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(stack.url + '/#overview')
        page.fill('#username', 'admin')
        page.fill('#password', stack.password)
        page.click('#login-form button')
        card = page.locator('.overview-summary [data-token-dashboard]')
        expect(card).to_contain_text('今日工具 Token')
        expect(card).to_contain_text('实际模型用量')
        expect(card).to_contain_text('未提供')
        expect(card.locator('.token-dashboard-metric')).to_have_count(2)
        expect(card.locator('.token-usage-help > p').first).to_be_hidden()
        card.locator('[data-token-filters] > summary').focus()
        page.keyboard.press('Enter')
        card.locator('.token-usage-help > summary').click()
        expect(card.locator('.token-usage-help')).to_have_attribute('open', '')
        expect(card.locator('.token-usage-help > p').first).to_be_visible()
        card.locator('[data-token-filter=period]').select_option('7d')
        expect(card).to_contain_text('近 7 天工具 Token')
        expect(card.locator('[data-token-filters]')).to_have_attribute('open', '')
        page.evaluate('renderPage(false)')
        expect(card).to_contain_text('近 7 天工具 Token')
        expect(card.locator('[data-token-filter=period]')).to_have_value('7d')
        expect(card.locator('[data-token-filters]')).to_have_attribute('open', '')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        pending = []
        page.route('**/api/token-usage?*', lambda route: pending.append(route))
        card.locator('[data-token-filter=period]').select_option('30d')
        page.wait_for_timeout(100)
        assert pending
        page.evaluate("navigate('projects')")
        expect(page.locator('[data-token-dashboard]')).to_have_count(0)
        for route in pending:
            try:
                route.fulfill(json={})
            except Exception:
                pass
        expect(page.locator('[data-token-dashboard]')).to_have_count(0)
        page.go_back()
        expect(page.locator('[data-token-dashboard]')).to_be_visible()
        expect(page.locator('[data-token-filter=period]')).to_have_value('today')
        assert not errors
    finally:
        context.close()
