"""Real overview card at desktop/mobile; no fabricated zero or stale navigation."""
import pytest
from pathlib import Path

from shared.token_estimate import summarize, usage
from playwright.sync_api import expect


@pytest.mark.parametrize('browser_kind', ['chromium', 'webkit'])
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
def test_overview_token_filters_missing_data_and_navigation(chat_browser_pool, stack, viewport, browser_kind):
    context = chat_browser_pool(browser_kind).new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(stack.url + '/#overview')
        page.fill('#username', 'admin')
        page.fill('#password', stack.password)
        page.click('#login-form button')
        card = page.locator('.overview-summary [data-token-dashboard]')
        expect(card).to_contain_text('今日工具用量')
        expect(card).to_contain_text('实际模型用量')
        expect(card).to_contain_text('未提供')
        expect(card.locator('.token-dashboard-metric')).to_have_count(2)
        expect(card.locator('.token-usage-help > p').first).to_be_hidden()
        card.locator('[data-token-filters] > summary').focus()
        page.keyboard.press('Enter')
        card.locator('.token-usage-help > summary').click()
        expect(card.locator('.token-usage-help')).to_have_attribute('open', '')
        expect(card.locator('.token-usage-help > p').first).to_be_visible()
        card.locator('[data-token-panel=settings] > summary').click()
        card.locator('[data-token-filter=period]').select_option('7d')
        expect(card).to_contain_text('近 7 天工具用量')
        expect(card.locator('[data-token-filters]')).to_have_attribute('open', '')
        page.evaluate('renderPage(false)')
        expect(card).to_contain_text('近 7 天工具用量')
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



def priced_dashboard(percent=90, authority='test-authority', model='gpt-6-astra'):
    def metric(n):
        return dict(state='available', estimated_tokens=n, low=n, high=n,
                    characters=n, utf8_bytes=n, source_truncated=False)
    summary = summarize([{'id': 1, 'token_usage': usage(metric(10_976), metric(41_463))}], cache_read_percent=percent, reference_model=model)
    return dict(summary=summary, authority_key=authority,
                period=dict(key='today', start=0, end=3600, timezone='UTC', bucket='hour'),
                filters=dict(project='', connection='', session=''),
                options=dict(projects=[], connections=[], sessions=[]),
                coverage=dict(activity_row_limit=10000, estimate_retention_days=30), trend=[])


@pytest.mark.parametrize('browser_kind', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390, 320])
def test_compact_cost_cache_late_response_failure_and_mobile(chat_browser_pool, width, browser_kind):
    root = Path(__file__).resolve().parents[1]
    context = chat_browser_pool(browser_kind).new_context(viewport={'width': width, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<div id="fixture"></div>')
        page.add_style_tag(content='body{margin:16px;background:#171717;color:#eee;font-family:sans-serif} #fixture{padding:20px;border-radius:24px;background:#40364f} *{box-sizing:border-box}')
        page.add_style_tag(path=str(root / 'web/token-usage.css'))
        page.add_script_tag(path=str(root / 'web/token-usage.js'))
        page.evaluate("""data => {
            window.pending = [];
            const api = window.CodePierTokenUsage, box = document.querySelector('#fixture');
            box.innerHTML = api.dashboard(data);
            api.bindDashboard(box, url => new Promise((resolve, reject) => pending.push({url, resolve, reject})), data);
        }""", priced_dashboard())
        card = page.locator('[data-token-dashboard]')
        expect(card.locator('.is-total')).to_contain_text('52.44K')
        expect(card.locator('.is-cost')).to_contain_text('$0.6276')
        expect(card.locator('.token-pricing-details')).to_be_hidden()
        expect(card).to_contain_text('GPT-6 Astra · 输入缓存 90% 假设')
        assert card.locator('.is-total strong').evaluate('(el) => el.getBoundingClientRect().height < parseFloat(getComputedStyle(el).fontSize) * 1.6')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        evidence = root / '.work/token-cost-preview'
        evidence.mkdir(parents=True, exist_ok=True)
        card.screenshot(path=str(evidence / f'compact-{browser_kind}-{width}.png'))
        expect(card.locator('.token-dashboard-limit')).to_be_visible()
        expect(card.locator('[data-token-filter=period]')).to_be_hidden()
        expect(card.locator('[data-token-export]')).to_be_hidden()
        card.locator('[data-token-filters] > summary').click()
        expect(card.locator('[data-token-filter=period]')).to_be_hidden()
        expect(card.locator('[data-token-panel=breakdown] > p').first).to_be_hidden()
        expect(card.locator('[data-token-panel=trend] > p').first).to_be_hidden()
        card.screenshot(path=str(evidence / f'details-{browser_kind}-{width}.png'))
        card.locator('[data-token-panel=settings] > summary').click()
        expect(card.locator('.token-pricing-notes > p').first).to_be_hidden()
        assumption = card.locator('[data-token-filter=cache_read_percent]')
        expect(assumption).to_have_value('90')
        assumption.fill('0')
        assumption.press('Tab')
        page.wait_for_function('pending.length === 1')
        assumption.fill('100')
        assumption.press('Tab')
        page.wait_for_function('pending.length === 2')
        assert 'cache_read_percent=0' in page.evaluate('pending[0].url')
        assert 'cache_read_percent=100' in page.evaluate('pending[1].url')
        page.evaluate('data => pending[1].resolve(data)', priced_dashboard(100))
        expect(assumption).to_have_value('100')
        expect(card.locator('[data-token-panel=settings]')).to_have_attribute('open', '')
        expect(card.locator('.token-pricing-notes > p').first).to_be_hidden()
        expect(card.locator('.is-cost')).to_contain_text('$0.5903')
        page.evaluate('data => pending[0].resolve(data)', priced_dashboard(0))
        expect(assumption).to_have_value('100')
        expect(card.locator('.is-cost')).to_contain_text('$0.5903')
        assumption.fill('101')
        assumption.press('Tab')
        expect(assumption).to_have_value('100')
        expect(card.locator('[role=status]')).to_contain_text('0–100')
        assert page.evaluate('pending.length') == 2
        assumption.fill('50')
        assumption.press('Tab')
        page.wait_for_function('pending.length === 3')
        page.evaluate("pending[2].reject(new Error('fixture failure'))")
        expect(assumption).to_have_value('100')
        expect(card.locator('[role=status]')).to_contain_text('上一次结果')
        model = card.locator('[data-token-filter=reference_model]')
        model.select_option('gpt-6-luna')
        page.wait_for_function('pending.length === 4')
        assert 'reference_model=gpt-6-luna' in page.evaluate('pending[3].url')
        page.evaluate('data => pending[3].resolve(data)', priced_dashboard(100, model='gpt-6-luna'))
        expect(model).to_have_value('gpt-6-luna')
        expect(card).to_contain_text('GPT-6 Luna')
        expect(assumption).to_have_value('100')
        expect(card.locator('.is-total')).to_contain_text('52.44K')
        expect(card).to_contain_text('估价拆分')
        card.locator('[data-token-panel=pricing] > summary').click()
        expect(card.locator('.token-pricing-notes > p').first).to_be_visible()
        model.select_option('gpt-6.1-sol')
        page.wait_for_function('pending.length === 5')
        page.evaluate("pending[4].reject(new Error('fixture failure'))")
        expect(model).to_have_value('gpt-6-luna')
        expect(card.locator('[data-token-panel=pricing]')).to_have_attribute('open', '')
        with page.expect_download() as downloaded:
            card.locator('[data-token-export]').click()
        assert downloaded.value.suggested_filename == 'codepier-mcp-tool-estimate.json'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assumption.fill('90')
        assumption.press('Tab')
        page.wait_for_function('pending.length === 6')
        card.locator('[data-token-filters] > summary').click()
        page.evaluate('data => pending[5].resolve(data)', priced_dashboard(90, model='gpt-6-luna'))
        expect(card.locator('[data-token-filters]')).not_to_have_attribute('open', '')
        expect(card.locator('.token-pricing-details')).to_be_hidden()
        expect(card.locator('.token-dashboard-limit')).to_be_visible()
        card.locator('[data-token-filters] > summary').click()
        expect(assumption).to_have_value('90')
        expect(card.locator('[data-token-panel=settings]')).to_have_attribute('open', '')
        expect(card.locator('[data-token-panel=pricing]')).to_have_attribute('open', '')
        # A new authorization scope resets both outer and advanced disclosures.
        assumption.fill('50')
        assumption.press('Tab')
        page.wait_for_function('pending.length === 7')
        page.evaluate('data => pending[6].resolve(data)', priced_dashboard(50, authority='new-authority'))
        page.wait_for_function('pending.length === 8')
        page.evaluate('data => pending[7].resolve(data)', priced_dashboard(90, authority='new-authority'))
        expect(card.locator('[data-token-filters]')).not_to_have_attribute('open', '')
        card.locator('[data-token-filters] > summary').click()
        expect(card.locator('[data-token-panel=settings]')).not_to_have_attribute('open', '')
        expect(assumption).to_be_hidden()
        assert not errors
    finally:
        context.close()
