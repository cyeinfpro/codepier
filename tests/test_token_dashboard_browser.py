"""Real overview card at desktop/mobile; no fabricated zero or stale navigation."""
import pytest
from pathlib import Path

from shared.token_estimate import summarize, usage
from playwright.sync_api import expect
from tests.test_ui_unification import _login, _navigate


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
        expect(card).to_contain_text('暂无保留调用，暂无法估算。')
        expect(card.locator('.is-total strong')).to_have_text('未记录')
        expect(card.locator('.token-dashboard-metric')).to_have_count(2)
        expect(card.locator('[data-token-panel], [data-token-export]')).to_have_count(0)
        card.locator('[data-token-filters] > summary').focus()
        page.keyboard.press('Enter')
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



def priced_dashboard(percent=95, authority='test-authority', model='gpt-6-astra'):
    def metric(n):
        return dict(state='available', estimated_tokens=n, low=n, high=n,
                    characters=n, utf8_bytes=n, source_truncated=False)
    summary = summarize([{'id': 1, 'token_usage': usage(metric(10_976), metric(41_463))}], cache_read_percent=percent, reference_model=model)
    return dict(summary=summary, authority_key=authority,
                period=dict(key='today', start=0, end=3600, timezone='UTC', bucket='hour'),
                filters=dict(project='', connection='', session=''),
                options=dict(projects=[], connections=[], sessions=[]),
                freshness=dict(as_of=3600, max_cache_age_seconds=30),
                coverage=dict(activity_row_limit=10000, estimate_retention_days=30), trend=[])


@pytest.mark.parametrize('browser_kind', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390, 320])
@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_compact_cost_cache_late_response_failure_and_mobile(chat_browser_pool, width, browser_kind, scheme):
    root = Path(__file__).resolve().parents[1]
    context = chat_browser_pool(browser_kind).new_context(viewport={'width': width, 'height': 900}, color_scheme=scheme)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<div id="fixture"></div>')
        page.add_style_tag(content='body{margin:16px;background:#f4f4f4;color:#232127;font-family:sans-serif} #fixture{padding:20px;border-radius:24px;background:#ccbafb} *{box-sizing:border-box} @media(prefers-color-scheme:dark){body{background:#171717;color:#eee} #fixture{background:#40364f}}')
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
        expect(card.locator('.is-cost')).to_contain_text('$0.6089')
        expect(card.locator('[data-token-panel], .token-pricing-details, .token-usage-help, [data-token-export]')).to_have_count(0)
        expect(card.locator('[data-token-filter]')).to_have_count(1)
        card.locator('[data-token-filters] > summary').click()
        period=card.locator('[data-token-filter=period]')
        expect(period).to_be_visible()
        expect(card.locator('.token-trend-section')).to_be_visible()
        period.select_option('7d')
        page.wait_for_function('pending.length===1')
        period.select_option('30d')
        page.wait_for_function('pending.length===2')
        result=priced_dashboard();result['period']['key']='30d'
        page.evaluate('data=>pending[1].resolve(data)',result)
        expect(period).to_have_value('30d')
        stale=priced_dashboard();stale['period']['key']='7d'
        page.evaluate('data=>pending[0].resolve(data)',stale)
        expect(period).to_have_value('30d')
        period.select_option('today');page.wait_for_function('pending.length===3')
        page.evaluate("pending[2].reject(new Error('fixture failure'))")
        expect(period).to_have_value('30d')
        expect(card.locator('[role=status]')).to_contain_text('上一次结果')
        period.select_option('7d');page.wait_for_function('pending.length===4')
        card.locator('[data-token-filters] > summary').click()
        result=priced_dashboard();result['period']['key']='7d'
        page.evaluate('data=>pending[3].resolve(data)',result)
        expect(card.locator('[data-token-filters]')).not_to_have_attribute('open','')
        card.locator('[data-token-filters] > summary').click()
        period.select_option('30d');page.wait_for_function('pending.length===5')
        page.evaluate('data=>pending[4].resolve(data)',priced_dashboard(authority='new-authority'))
        page.wait_for_function('pending.length===6')
        page.evaluate('data=>pending[5].resolve(data)',priced_dashboard(authority='new-authority'))
        expect(card.locator('[data-token-filters]')).not_to_have_attribute('open','')
        expect(period).to_have_value('today')
        expect(card.locator('[data-token-panel], [data-token-export]')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width', [320,390,900,901,1440])
@pytest.mark.parametrize('scheme', ['light','dark'])
def test_simplified_details_show_period_and_trend_immediately(stack, chat_browser_pool, width, scheme):
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':1000 if width==1440 else 844},color_scheme=scheme)
    page=context.new_page()
    try:
        def synthetic_paths(route):
            response=route.fetch()
            data=response.json()
            for i,project in enumerate(data.get('projects',[])):
                project['root']='/workspace/demo/project-'+str(i+1)
            route.fulfill(response=response,json=data)
        page.route('**/api/overview',synthetic_paths)
        from tests.context_usage_fixtures import synthetic_dashboard
        page.route('**/api/token-usage', lambda route: route.fulfill(json=synthetic_dashboard()))
        _login(page,stack)
        page.evaluate('s=>CodePierAppearance.setPreference(s)',scheme)
        _navigate(page,'overview')
        card=page.locator('[data-token-dashboard]')
        card.locator('[data-token-filters] > summary').click()
        expect(card.locator('[data-token-filter=period]')).to_be_visible()
        expect(card.locator('.token-trend-section')).to_be_visible()
        for key in ['project','connection','session','reference_model']:
            expect(card.locator('[data-token-filter='+key+']')).to_have_count(0)
        assert card.locator('[data-token-panel=trend]').count()==0
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        number_box=card.locator('.is-total strong')
        expect(number_box).to_have_text('206.49K')
        assert number_box.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
        assert number_box.evaluate("e=>getComputedStyle(e).whiteSpace==='nowrap'")
        if width == 390:
            total_rect = number_box.bounding_box()
            cost_rect = card.locator('.is-cost strong').bounding_box()
            assert abs(total_rect['y'] + total_rect['height'] - cost_rect['y'] - cost_rect['height']) <= 1
        page.evaluate("()=>{window.scrollTo(0,0);document.querySelectorAll('*').forEach(e=>{if(e.scrollTop)e.scrollTop=0})}")
        folder=Path(__import__('os').environ.get('TOKEN_USAGE_EVIDENCE_DIR', '.work/usage-three-tier'));folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'{scheme}-{width}-full.png'),full_page=True)
        page.screenshot(path=str(folder/f'{scheme}-{width}-full.jpg'),type='jpeg',quality=35,full_page=width<768)
        expect(card.locator('[data-token-panel], [data-token-export], .token-usage-help')).to_have_count(0)
        def core_boxes():
            page.evaluate("()=>{window.scrollTo(0,0);document.querySelectorAll('*').forEach(e=>{if(e.scrollTop)e.scrollTop=0})}")
            return card.locator('.token-dashboard-head, .token-dashboard-metrics').evaluate_all("nodes=>nodes.map(e=>{const r=e.getBoundingClientRect(),p=e.closest('[data-token-dashboard]').getBoundingClientRect();return [r.x-p.x,r.y-p.y,r.width,r.height]})")
        opened=core_boxes()
        card.locator('[data-token-filters] > summary').click()
        closed=core_boxes()
        for a,b in zip(opened,closed):
            assert all(abs(x-y)<=1 for x,y in zip(a,b)), (opened,closed)
        page.screenshot(path=str(folder/f'{scheme}-{width}-closed.jpg'),type='jpeg',quality=35,full_page=width<768)
        if scheme=='light' and width in (390,1440):
            page.set_viewport_size({'width':width,'height':1600})
            core_boxes()
            page.locator('.overview-summary').screenshot(path=str(folder/f'card-{width}-normal.jpg'),type='jpeg',quality=35)
        for variant,n,expected in [('missing',None,'未记录'),('zero',0,'0'),('long',9007199254740991,'9007199.25B')]:
            data=priced_dashboard()
            for direction in ('input','output'):
                for field in ('estimated_tokens','low','high'):
                    data['summary'][direction][field]=n if direction=='input' else (None if n is None else 0)
            data['summary']['reference_cost']['amount_nano_usd']=n
            page.evaluate("data=>document.querySelector('[data-token-dashboard]').outerHTML=CodePierTokenUsage.dashboard(data,false)",data)
            expect(card.locator('.is-total strong')).to_have_text(expected)
            expect(card.locator('.is-cost strong')).to_have_text('未记录' if n is None else '$0.00' if n==0 else '$9,007,199.25')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            for number_box in card.locator('.token-dashboard-metric strong').all():
                assert number_box.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
                assert number_box.evaluate("e=>getComputedStyle(e).whiteSpace==='nowrap'")
            if width in (390,1440):
                core_boxes()
                page.screenshot(path=str(folder/f'{scheme}-{width}-{variant}.jpg'),type='jpeg',quality=35,full_page=width<768)
                if scheme=='light' and (variant=='missing' or variant=='long' and width==390):
                    page.locator('.overview-summary').screenshot(path=str(folder/f'card-{width}-{variant}.jpg'),type='jpeg',quality=35)
    finally:context.close()
