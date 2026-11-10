"""Real owner/consent UI; optional explicit Chromium executable for local checks."""
from tests.browser_launch import launch_browser
import base64
import hashlib
import os
import re
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope='module', params=['chromium', 'webkit'])
def profiles_browser(request):
    with sync_playwright() as pw:
        executable = os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param == 'chromium' else None
        browser = launch_browser(getattr(pw, request.param), **({'executable_path': executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width': 1280, 'height': 900}, {'width': 390, 'height': 844}])
def profiles_page(profiles_browser, request, stack):
    page = profiles_browser.new_page(viewport=request.param)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(stack.url + '/#profiles')
    page.fill('#username', 'admin')
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
    page.locator('#oauth-options > summary').click()
    expect(page.locator('#access-profile-selector')).to_be_visible()
    page.select_option('#access-profile-selector', profile['id'])
    expect(page.locator('#allow-read')).to_be_hidden()
    expect(page.locator('#allow-dev')).to_be_hidden()
    dialog = page.locator('.modal')
    expect(dialog.locator('[name="scope"][value="execute"]')).to_have_count(0)
    expect(dialog.locator('[name="scope"][value="computer"]')).not_to_be_checked()
    expect(dialog.locator('[name="scope"][value="write"]')).not_to_be_checked()
    dialog.locator('[name="scope"][value="write"]').check()
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
    # Chromium can discard the fetch body when consent navigates to the callback.
    # Verify the real redirect rather than rereading a detached response body.
    page.wait_for_url(re.compile(r'^http://localhost:12345/callback\?'))
    code = parse_qs(urlsplit(page.url).query)['code'][0]
    token = stack.must(stack.client.post('/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code, 'client_id': registration['client_id'],
        'redirect_uri': 'http://localhost:12345/callback', 'code_verifier': verifier}))['access_token']
    identity = stack.rpc('tools/call', {'name': 'get_profile', 'arguments': {}}, token_value=token).json()
    assert identity['result']['structuredContent']['id'] == profile['id']
    assert not errors, errors


@pytest.fixture(scope="module")
def pairing_stack(tmp_path_factory):
    # Separate synthetic stack keeps this matrix below the real OAuth throttle;
    # do not disable the limiter or reset its records to make tests pass.
    from tests.support import running_stack
    with running_stack(tmp_path_factory.mktemp("pairing-hub-agent")) as stack:
        yield stack


@pytest.fixture(params=[{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def pairing_page(profiles_browser, request, pairing_stack):
    page = profiles_browser.new_page(viewport=request.param)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(pairing_stack.url + "/#profiles")
    page.fill("#username", "admin")
    page.fill("#password", pairing_stack.password)
    page.click("#login-form button")
    expect(page.locator("#profiles-page")).to_be_visible()
    yield page, errors
    page.close()


@pytest.mark.parametrize("requested", [None, "read"])
def test_pairing_scope_choices_are_optional_and_reopen_safely(pairing_page, pairing_stack, requested):
    page, errors = pairing_page
    stack = pairing_stack
    # Even a saved developer convenience default must not check new OAuth powers.
    page.route("**/api/oauth/requests/*", lambda route: route.fulfill(
        response=(response := route.fetch()),
        json={**response.json(), "access_defaults": {"developer_scopes": True, "all_projects": False}},
    ) if not route.request.url.endswith("/decide") else route.continue_())
    registration = stack.must(stack.client.post("/oauth/register", json={
        "redirect_uris": ["http://localhost:12345/callback"]}))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).rstrip(b"=").decode()
    def new_request():
        response = stack.client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": registration["client_id"],
            "redirect_uri": "http://localhost:12345/callback",
            **({"scope": requested} if requested is not None else {}),
            "code_challenge_method": "S256", "code_challenge": challenge,
            "resource": stack.url + "/mcp",
        })
        return parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    page.route("http://localhost:12345/callback*", lambda route: route.fulfill(body="Denied test callback"))
    for _ in range(2):
        request_id = new_request()
        page.evaluate("(id)=>consentModal(id)", request_id)
        dialog = page.locator(".modal")
        expect(dialog.locator('[name="scope"][value="read"]')).to_be_checked()
        if requested != "read":
            write = dialog.locator('[name="scope"][value="write"]')
            write.locator('xpath=ancestor::details').locator(':scope > summary').click()
        for scope in ("write", "execute", "computer"):
            field = dialog.locator(f'[name="scope"][value="{scope}"]')
            if requested == "read":
                expect(field).to_have_count(0)
            else:
                expect(field).to_be_visible()
                expect(field).not_to_be_checked()
                field.check()
                expect(field).to_be_checked()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        dialog.locator("#deny-consent").click()
        page.wait_for_url(re.compile(r"^http://localhost:12345/callback\?"))
        page.goto(stack.url + "/#profiles")
        expect(page.locator("#profiles-page")).to_be_visible()
    assert not errors, errors


@pytest.mark.parametrize("failure", ["network", "server", "conflict", "expired"])
def test_uncertain_profile_creation_cannot_change_original_request(profiles_page, stack, failure):
    page, errors = profiles_page
    label = 'Recover profile ' + uuid.uuid4().hex[:8]
    page.click('#profile-create')
    form = page.locator('#profile-form')
    form.locator('[name="label"]').fill(label)
    form.locator('[name="project"][value="' + stack.project['id'] + '"]').check()
    requests = []
    def save_then_disconnect(route):
        if route.request.method != 'POST':
            return route.continue_()
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            response = route.fetch()
            assert response.ok
            if failure == 'network':
                route.abort('failed')
            elif failure == 'server':
                route.fulfill(status=503, content_type='application/json',
                              body='{"error": {"code": "SERVICE_UNAVAILABLE", "message": "响应未确认"}}')
            else:
                code, message = ('IDEMPOTENCY_CONFLICT', '原请求键已用于其他参数') if failure == 'conflict' else (
                    'IDEMPOTENCY_RESPONSE_EXPIRED', '原请求已执行，历史响应已清理')
                route.fulfill(status=409, content_type='application/json',
                              body='{"error": {"code": "' + code + '", "message": "' + message + '"}}')
        else:
            route.continue_()
    page.route('**/api/access-profiles', save_then_disconnect)
    page.locator('button[form="profile-form"]').click()
    first_message = {'conflict': '原请求键已用于其他参数', 'expired': '原请求已执行，历史响应已清理'}.get(failure, '未确认')
    expect(page.locator('#profile-save-status')).to_contain_text(first_message)
    form.locator('[name="label"]').fill(label + ' changed')
    page.locator('button[form="profile-form"]').click()
    expect(page.locator('#profile-save-status')).to_contain_text('恢复原输入')
    assert len(requests) == 1
    form.locator('[name="label"]').fill(label)
    page.locator('button[form="profile-form"]').click()
    expect(form).to_have_count(0)
    assert len(requests) == 2 and requests[0] == requests[1]
    profiles = stack.must(stack.client.get('/api/access-profiles'))['profiles']
    assert len([p for p in profiles if p['label'].startswith(label)]) == 1
    assert not errors, errors


@pytest.mark.parametrize("concurrent", [False, True])
def test_uncertain_profile_update_retries_original_without_overwriting_concurrent(profiles_page, stack, concurrent):
    page, errors = profiles_page
    label = 'Update profile ' + uuid.uuid4().hex[:8]
    saved = stack.must(stack.client.post('/api/access-profiles', json={'label': label, 'scopes': ['read'], 'projects': [stack.project['id']], 'idempotency_key': uuid.uuid4().hex}))
    page.reload()
    page.locator('[data-profile-edit="' + saved['id'] + '"]').click()
    form = page.locator('#profile-form')
    form.locator('[name="label"]').fill(label + ' saved')
    requests = []
    def update_then_disconnect(route):
        if route.request.method != 'PUT':
            return route.continue_()
        requests.append(route.request.post_data_json)
        response = route.fetch()
        if len(requests) == 1:
            assert response.ok
            route.abort('failed')
        else:
            route.fulfill(response=response)
    page.route('**/api/access-profiles/' + saved['id'], update_then_disconnect)
    page.locator('button[form="profile-form"]').click()
    expect(page.locator('#profile-save-status')).to_contain_text('未确认')
    if concurrent:
        current = stack.must(stack.client.get('/api/access-profiles/' + saved['id']))
        changed = dict(requests[0], label=label + ' concurrent', expected_version=current['version'])
        stack.must(stack.client.put('/api/access-profiles/' + saved['id'], json=changed))
    page.locator('button[form="profile-form"]').click()
    if concurrent:
        expect(page.locator('#profile-save-status')).to_contain_text('其他')
    else:
        expect(form).to_have_count(0)
    assert len(requests) == 2 and requests[0] == requests[1]
    current = stack.must(stack.client.get('/api/access-profiles/' + saved['id']))
    assert current['label'] == label + (' concurrent' if concurrent else ' saved')
    assert current['version'] == saved['version'] + (2 if concurrent else 1)
    assert not errors, errors
