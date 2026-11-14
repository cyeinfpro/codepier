"""Real loopback Hub/Agent plus isolated Chromium/WebKit onboarding checks."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.integration]


def poll_original(stack, value):
    if not value.get('pending'):
        return value
    identifier = value['operation_id']
    for _ in range(15):
        response = stack.mcp('task_query', {'operation':'wait','operation_ids':[identifier],
                                           'wait_seconds':1})
        operation = response['structuredContent']['operations'][0]
        if not operation.get('pending'):
            assert operation['state']=='succeeded', operation
            return {'operation_id':identifier, **operation['result']['data']}
    raise AssertionError('Original read did not finish in the bounded fixture')


def test_real_batch_read_evidence_and_independent_errors(stack):
    stack.rpc('initialize', {'protocolVersion':'2025-11-25'}).raise_for_status()
    stack.rpc('tools/list').raise_for_status()
    response = stack.mcp('read', {'operation':'batch','project':'ProjectAlpha',
        'options':{'items':[{'path':'README.md'},{'path':'.env'},{'path':'missing.txt'}]}})
    data = poll_original(stack, response['structuredContent'])
    assert data['batch'] and data['files'][0]['ok']
    assert data['files'][1]['error']['code']=='PROTECTED_PATH'
    assert data['files'][2]['ok'] is False
    status = stack.must(stack.client.get('/api/grants/'+stack.grant+'/connection-status',
                                        params={'project_id':stack.project['id']}))
    layers = {x['id']:x for x in status['layers']}
    assert layers['readonly']['status']=='observed', status
    assert layers['readonly']['evidence']['operation_id']==data['operation_id']
    assert layers['agent']['status']=='online'
    assert status['catalog']['client_cache_status']=='unknown'


@pytest.mark.parametrize('browser,viewport', [
    ('chromium', {'width':1280,'height':900}),
    ('webkit', {'width':390,'height':844}),
])
def test_connection_ui_preview_and_late_response_do_not_cross_navigation(stack, chat_browser_pool, browser, viewport):
    page = chat_browser_pool(browser).new_page(viewport=viewport)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(stack.url+'/#connect')
        page.fill('#username','admin')
        page.fill('#password',stack.password)
        page.click('#login-form button')
        expect(page.locator('#connection-setup')).to_be_visible()
        form = page.locator('#connection-check-form')
        form.locator('[name=grant_id]').select_option(stack.grant)
        # Project and catalog live under the folded 「排查选项」.
        form.locator('details > summary').click()
        form.locator('[name=project_id]').select_option(stack.project['id'])
        form.locator('button[type=submit]').click()
        output = page.locator('#connection-check-result')
        expect(output).to_contain_text('ChatGPT 工具扫描 / 缓存')
        # Host-side scanning and caching stay explicitly unconfirmed.
        expect(output).to_contain_text('尚未确认')
        expect(output).to_contain_text('不能观测 ChatGPT 是否扫描完成')
        # The tunnel preview sits under 「其他客户端」 on the address card.
        page.locator('#other-clients > summary').click()
        page.locator('#other-clients details > summary').click()
        tunnel = page.locator('#connection-tunnel-form')
        tunnel.locator('[name=tunnel_id]').fill('tunnel_'+'a'*32)
        tunnel.locator('[name=install_dir]').fill('/opt/codepier')
        tunnel.locator('button').click()
        preview = page.locator('#connection-tunnel-result')
        expect(preview).to_contain_text('配置文本校验通过，尚未运行')
        expect(preview).to_contain_text('PAT 实际范围：未验证')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        screenshots = Path('.work/connection-ui')
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots/('connection-setup-'+browser+'.png')), full_page=True)
        # Editing invalidates the prior preview; no stale configuration survives.
        tunnel.locator('[name=install_dir]').fill('/opt/changed')
        expect(preview).to_be_empty()
        held = []
        page.route('**/api/connection/tunnel-preview', lambda route: held.append(route))
        with page.expect_request('**/api/connection/tunnel-preview'):
            tunnel.locator('button').click()
        page.wait_for_function('document.querySelector("#connection-tunnel-form button").disabled')
        expect(tunnel.locator('button')).to_be_disabled()
        assert len(held) == 1, 'Exactly one preview request must be pending'
        if viewport['width'] < 700:
            page.get_by_role('button', name='更多页面', exact=True).click()
        page.locator('#sidebar [data-nav=overview]').click()
        expect(page.locator('#connection-setup')).to_have_count(0)
        if held:
            try:
                held[0].fulfill(status=200, json={'configuration_valid':True,'errors':[],'warnings':[],
                    'artifacts':[],'commands':[],'manual_steps':[]})
            except Exception as exc:
                # An aborted request is expected; any other browser failure remains visible.
                assert 'closed' in str(exc).lower() or 'intercept' in str(exc).lower(), str(exc)
        page.go_back()
        expect(page.locator('#connection-setup')).to_be_visible()
        expect(page.locator('#connection-tunnel-result')).to_be_empty()
        assert not errors, errors
    finally:
        page.close()
