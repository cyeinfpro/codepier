"""Real owner/consent UI; optional explicit Chromium executable for local checks."""
import base64
import hashlib
import os
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope='module', params=['chromium', 'webkit'])
def profiles_browser(request):
    with sync_playwright() as pw:
        executable = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = getattr(pw, request.param).launch(**({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}])
def profiles_page(profiles_browser, request, stack):
    page = profiles_browser.new_page(viewport=request.param)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(stack.url + '/#profiles')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#profiles-page')).to_be_visible()
    yield page, errors
    page.close()


def test_create_rename_and_concurrent_edit_keep_identity(profiles_page, stack):
    page, errors = profiles_page
    label = 'Review ' + uuid.uuid4().hex[:10]
    page.click('#profile-create')
    form = page.locator('#profile-form')
    expect(form.locator('[name="scope"][value="read"]')).to_be_checked()
    expect(form.locator('[name="scope"][value="execute"]')).not_to_be_checked()
    expect(form.locator('[name="scope"][value="computer"]')).not_to_be_checked()
    expect(form.locator('[name="all_projects"]')).not_to_be_checked()
    form.locator('[name="label"]').fill(label)
    form.locator('[name="project"][value="' + stack.project['id'] + '"]').check()
    page.locator('button[form="profile-form"]').click()
    expect(form).to_have_count(0)
    row = page.locator('.grant-row').filter(has_text=label)
    expect(row).to_be_visible()
    identifier = row.locator('[data-profile-edit]').get_attribute('data-profile-edit')
    row.locator('[data-profile-edit]').click()
    form = page.locator('#profile-form')
    form.locator('[name="label"]').fill(label + ' renamed')
    page.locator('button[form="profile-form"]').click()
    expect(form).to_have_count(0)
    row = page.locator('.grant-row').filter(has_text=label + ' renamed')
    expect(row).to_be_visible()
    assert row.locator('[data-profile-edit]').get_attribute('data-profile-edit') == identifier
    row.locator('[data-profile-edit]').click()
    expect(page.locator('#profile-form')).to_be_visible()
    profile = stack.must(stack.client.get('/api/access-profiles/' + identifier))
    stack.must(stack.client.put('/api/access-profiles/' + identifier, json={
        'label': profile['label'], 'scopes': profile['scopes'], 'projects': profile['projects'],
        'enabled': False, 'expected_version': profile['version']}))
    page.locator('#profile-form [name="label"]').fill('Stale overwrite')
    page.locator('button[form="profile-form"]').click()
    expect(page.locator('#profile-save-status')).to_contain_text('其他窗口修改')
    assert not stack.must(stack.client.get('/api/access-profiles/' + identifier))['enabled']
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert not errors, errors


def test_oauth_profile_filters_scope_and_project_then_binds_real_token(profiles_page, stack):
    page, errors = profiles_page
    label = 'OAuth ' + uuid.uuid4().hex[:10]
    profile = stack.must(stack.client.post('/api/access-profiles', json={
        'label': label, 'scopes': ['read', 'write', 'computer'],
        'projects': [stack.project['id']], 'idempotency_key': uuid.uuid4().hex}))
    registration = stack.must(stack.client.post('/oauth/register', json={
        'redirect_uris': ['http://localhost:12345/callback']}))
    verifier = 'v' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response = stack.client.get('/oauth/authorize', params={
        'response_type': 'code', 'client_id': registration['client_id'],
        'redirect_uri': 'http://localhost:12345/callback', 'scope': 'read write execute computer',
        'code_challenge_method': 'S256', 'code_challenge': challenge, 'resource': stack.url + '/mcp'})
    request_id = parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]
    page.route('http://localhost:12345/callback*', lambda route: route.fulfill(body='Authorized test callback'))
    page.evaluate('(id)=>consentModal(id)', request_id)
    expect(page.locator('#access-profile-selector')).to_be_visible()
    page.select_option('#access-profile-selector', profile['id'])
    dialog = page.locator('.modal')
    expect(dialog.locator('[name="scope"][value="execute"]')).to_have_count(0)
    expect(dialog.locator('[name="scope"][value="computer"]')).not_to_be_checked()
    expect(dialog.locator('[name="all_projects"]')).to_be_disabled()
    expect(dialog.locator('[name="project"]')).to_have_count(1)
    expect(dialog.locator('[name="project"]')).to_be_checked()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    with page.expect_response(lambda r: '/api/oauth/requests/' in r.url and r.url.endswith('/decide')) as received:
        dialog.locator('#allow-consent').click()
    result = received.value
    assert result.status == 200, result.text()
    sent = result.request.post_data_json
    assert sent['profile_id'] == profile['id'] and sent['profile_version'] == profile['version']
    assert sent['scopes'] == ['read', 'write'] and sent['projects'] == [stack.project['id']]
    code = parse_qs(urlsplit(result.json()['redirect']).query)['code'][0]
    token = stack.must(stack.client.post('/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code, 'client_id': registration['client_id'],
        'redirect_uri': 'http://localhost:12345/callback', 'code_verifier': verifier}))['access_token']
    identity = stack.rpc('tools/call', {'name': 'get_profile', 'arguments': {}}, token_value=token).json()
    assert identity['result']['structuredContent']['id'] == profile['id']
    assert not errors, errors
