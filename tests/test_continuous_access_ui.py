"""Real settings, consent and existing-grant editing in Chromium and WebKit."""
import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope='module', params=['chromium', 'webkit'])
def access_browser(request):
    with sync_playwright() as pw:
        browser = getattr(pw, request.param).launch()
        yield browser, request.param
        browser.close()


@pytest.fixture
def access_page(access_browser, stack):
    browser, kind = access_browser
    stack.must(stack.client.put('/api/settings/access', json={'all_projects': False, 'developer_scopes': False}))
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(stack.url + '/#settings')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#access-settings-form')).to_be_visible()
    try:
        yield page, errors, kind
    finally:
        page.close()
        stack.must(stack.client.put('/api/settings/access', json={'all_projects': False, 'developer_scopes': False}))


def test_settings_preselect_new_grant_without_desktop_access_and_preserve_other_inputs(access_page, stack):
    page, errors, kind = access_page
    form = page.locator('#access-settings-form')
    expect(form.locator('[name="all_projects"]')).not_to_be_checked()
    expect(form.locator('[name="apply_to_existing"]')).to_be_disabled()
    page.fill('#password-form [name="current_password"]', 'unsaved-password')
    form.locator('[name="all_projects"]').check()
    form.locator('[name="developer_scopes"]').check()
    form.locator('button[type="submit"]').click()
    expect(page.locator('#access-settings-status')).to_contain_text('默认选项已保存')
    expect(page.locator('#password-form [name="current_password"]')).to_have_value('unsaved-password')
    assert stack.client.get('/api/settings').json()['access_defaults'] == {'all_projects': True, 'developer_scopes': True}
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    screenshots = Path('.work/robustness-20260923/screenshots'); screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f'{kind}-access-settings-mobile.png'), full_page=True)
    page.fill('#password-form [name="current_password"]', '')
    page.evaluate("navigate('connect')")
    page.click('[data-action="new-grant"]')
    expect(page.locator('#grant-form [name="all_projects"]')).to_be_checked()
    expect(page.locator('#grant-form [name="scope"][value="write"]')).to_be_checked()
    expect(page.locator('#grant-form [name="scope"][value="execute"]')).to_be_checked()
    expect(page.locator('#grant-form [name="scope"][value="computer"]')).not_to_be_checked()
    for field in page.locator('#grant-form [name="project"]').all():
        expect(field).to_be_disabled()
    page.locator('#grant-form [name="all_projects"]').uncheck()
    expect(page.locator('#grant-form [name="project"]').first).to_be_enabled()
    assert not errors, errors


def test_existing_connection_range_edit_without_reissuing_token_and_rejects_stale_dialog(access_page, stack):
    page, errors, _ = access_page
    created = stack.must(stack.client.post('/api/grants', json={
        'label': 'range UI fixture', 'scopes': ['read'], 'projects': [stack.project['id']]}))
    grant_id = created['grant_id']
    try:
        page.evaluate("navigate('connect')")
        page.locator('[data-action="edit-grant-projects"][data-id="' + grant_id + '"]').click()
        dialog = page.locator('.modal')
        expect(dialog.locator('[name="all_projects"]')).not_to_be_checked()
        dialog.locator('[name="all_projects"]').check()
        dialog.locator('button[type="submit"]').click()
        expect(dialog).to_have_count(0)
        snapshot = stack.must(stack.client.get('/api/grants/' + grant_id + '/projects'))
        assert snapshot['projects'] == ['*'] and snapshot['scopes'] == ['read']
        # The exact original credential is still accepted after editing.
        assert stack.rpc('ping', token_value=created['token']).status_code == 200
        page.locator('[data-action="edit-grant-projects"][data-id="' + grant_id + '"]').click()
        expect(page.locator('#grant-projects-form')).to_be_visible()
        changed = stack.must(stack.client.put('/api/grants/' + grant_id + '/projects', json={
            'projects': [stack.project['id']], 'all_projects': False,
            'expected_projects': snapshot['projects'], 'expected_revision': snapshot['project_revision']}))
        assert changed['projects'] == [stack.project['id']]
        page.locator('.modal button[type="submit"]').click()
        expect(page.locator('#grant-projects-status')).to_contain_text('其他窗口修改')
        assert stack.must(stack.client.get('/api/grants/' + grant_id + '/projects'))['projects'] == [stack.project['id']]
        assert not errors, errors
    finally:
        stack.must(stack.client.delete('/api/grants/' + grant_id))


def test_bulk_apply_requires_explicit_confirmation_and_does_not_change_pat(access_page, stack):
    page, errors, _ = access_page
    requests = []
    page.on('request', lambda request: requests.append(request.post_data_json)
            if request.method == 'PUT' and urlsplit(request.url).path == '/api/settings/access' else None)
    form = page.locator('#access-settings-form')
    form.locator('[name="all_projects"]').check()
    form.locator('[name="apply_to_existing"]').check()
    page.once('dialog', lambda dialog: dialog.dismiss())
    form.locator('button[type="submit"]').click()
    assert not requests
    page.once('dialog', lambda dialog: dialog.accept())
    form.locator('button[type="submit"]').click()
    expect(page.locator('#access-settings-status')).to_contain_text('无需重新连接')
    assert len(requests) == 1 and requests[0]['apply_to_existing'] is True
    expect(form.locator('[name="apply_to_existing"]')).not_to_be_checked()
    grant = stack.must(stack.client.get('/api/grants/' + stack.grant + '/projects'))
    assert grant['projects'] == [p['id'] for p in stack.projects] or set(grant['projects']) == {p['id'] for p in stack.projects}
    assert not errors, errors


def test_oauth_defaults_never_include_unrequested_scopes_or_submit_without_click(access_page, stack):
    page, errors, _ = access_page
    stack.must(stack.client.put('/api/settings/access', json={'all_projects': True, 'developer_scopes': True}))
    registration = stack.must(stack.client.post('/oauth/register', json={'redirect_uris': ['http://localhost:12345/callback']}))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(('v' * 64).encode()).digest()).rstrip(b'=').decode()
    response = stack.client.get('/oauth/authorize', params={
        'response_type': 'code', 'client_id': registration['client_id'], 'redirect_uri': 'http://localhost:12345/callback',
        'code_challenge_method': 'S256', 'code_challenge': challenge, 'scope': 'read', 'resource': stack.url + '/mcp'})
    request_id = parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]
    decisions = []
    page.on('request', lambda request: decisions.append(request.post_data_json)
            if request.method == 'POST' and '/decide' in request.url else None)
    page.evaluate('(id)=>consentModal(id)', request_id)
    expect(page.locator('.modal [name="all_projects"]')).to_be_checked()
    expect(page.locator('.modal [name="scope"]')).to_have_count(1)
    expect(page.locator('.modal [name="scope"][value="read"]')).to_be_checked()
    assert not decisions
    # Consent is pinned to the browser session that displayed it. Another
    # administrator session must not be allowed to take over that transaction.
    assert page.evaluate('(id)=>api("/api/oauth/requests/"+id).then(()=>200)', request_id) == 200
    assert stack.client.get('/api/oauth/requests/' + request_id).status_code == 403
    assert not errors, errors
