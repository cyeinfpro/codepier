"""Real browser DOM + real Hub/IAM through an ASGI HTTP adapter (not a mock API)."""
import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.test_mcp_gateway import gw, team
from tests.test_roles import role, profile, credential, must


@pytest.fixture(params=['chromium', 'webkit'])
def gateway_browser(request):
    with sync_playwright() as pw:
        executable = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = getattr(pw, request.param).launch(**({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()


@pytest.mark.parametrize('width', [1280, 390])
def test_gateway_panel_end_to_end(gw, gateway_browser, width, tmp_path):
    app, b, backend = gw
    r = role(b['owner'], label='UI secretary', project_rules=[])
    page = gateway_browser.new_page(viewport={'width': width, 'height': 900})
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("""
        sessionStorage.setItem('codepier-space:owner','team');
        // This adapter does not exercise SSE transport. An empty fulfilled
        // stream races WebKit's reload teardown and produces a CORS pageerror.
        // Keep real browser security and every HTTP/IAM request unchanged.
        window.fixtureEventStreams = [];
        window.EventSource = class extends EventTarget {
            static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
            constructor(url) {
                super();
                const parsed = new URL(url, location.href);
                if (parsed.origin !== location.origin || parsed.pathname !== '/api/events')
                    throw new Error('Unexpected fixture EventSource destination');
                this.url = parsed.href; this.readyState = 1;
                window.fixtureEventStreams.push(this);
            }
            close() { this.readyState = 2; }
        };
    """)
    def route(request_route):
        req = request_route.request; parts = urlsplit(req.url)
        if parts.hostname != '127.0.0.1':
            request_route.abort(); return
        # SSE is intentionally outside this DOM test; the HTTP/IAM calls and
        # static files are served by the real app, not canned JSON responses.
        if parts.path.endswith('/events'):
            request_route.fulfill(status=200, content_type='text/event-stream', body=''); return
        response = b['owner'].client.request(req.method, req.url,
                    headers=req.all_headers(), content=req.post_data_buffer, follow_redirects=False)
        headers = {k: v for k, v in response.headers.items() if k not in ('content-length', 'content-encoding')}
        request_route.fulfill(status=response.status_code, headers=headers, body=response.content)
    page.route('**/*', route)
    try:
        page.goto('http://127.0.0.1:8765/#mcp-gateway')
        expect(page.locator('#login-form')).to_be_visible()
        page.fill('#username', 'owner')
        page.fill('#password', 'fixture-password-only')
        page.click('#login-form button')
        expect(page.locator('#gateway-page')).to_be_visible()
        page.click('[data-gw="connector"]')
        page.fill('#gw-form [name="label"]', 'UI MCP <not markup>')
        page.fill('#gw-form [name="endpoint"]', 'https://mcp.example/mcp')
        page.click('button[form="gw-form"]'); expect(page.locator('#gw-form')).to_have_count(0)
        page.click('[data-gw="account"]')
        page.fill('#gw-form [name="label"]', 'UI account')
        page.fill('#gw-form [name="token"]', 'PRIVATE_UI_TOKEN')
        page.click('button[form="gw-form"]'); expect(page.locator('#gw-form')).to_have_count(0)
        page.click('[data-gw-discover]')
        page.fill('#gw-form [name="alias"]', 'ui_mcp')
        page.check('#gw-form [name="tool"][value="echo"]')
        page.check('#gw-form [name="confirmed"]')
        page.click('button[form="gw-form"]'); expect(page.locator('#gw-form')).to_have_count(0)
        page.click('[data-gw-role]')
        page.check('#gw-form [name="tool"][value="echo"]')
        page.click('button[form="gw-form"]'); expect(page.locator('#gw-form')).to_have_count(0)
        page.click('[data-gw-role]')
        expect(page.locator('#gw-form [name="tool"][value="echo"]')).to_be_checked()
        page.get_by_role('button', name='取消', exact=True).click()
        r = must(b['owner'].get('/api/access-roles/' + r['id']))
        assert r['connector_rules'][0]['tools'] == ['echo']
        p = profile(b['owner'], r, 'UI profile'); g = must(credential(b['owner'], r, p))
        page.reload(); expect(page.locator('#gateway-page')).to_be_visible()
        page.get_by_role('tab', name='我的委派', exact=True).click()
        expect(page.locator('[data-gw-consent]')).to_be_visible()
        page.click('[data-gw-consent]')
        expect(page.locator('#gw-form [name="confirmed"]')).not_to_be_checked()
        page.check('#gw-form [name="confirmed"]')
        page.click('button[form="gw-form"]'); expect(page.locator('#gw-form')).to_have_count(0)
        assert app.state.store.one('SELECT consent_version FROM gateway_consents WHERE grant_id=?', (g['grant_id'],))['consent_version'] == 1
        assert 'PRIVATE_UI_TOKEN' not in page.locator('body').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path / 'gateway-panel.png'), full_page=True)
        assert page.evaluate("fixtureEventStreams.length > 0 && fixtureEventStreams.every(s => new URL(s.url).searchParams.get('space_id') === 'team')")
        assert not errors, errors
    finally:
        page.close()
