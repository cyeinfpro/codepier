"""Role policy editing, profile binding and explicit live consent in real browsers."""
import base64
import hashlib
import os
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.role_contracts import ROLE_SCOPE
from tests.browser_launch import launch_browser
from tests.test_roles import role, profile, update_role, must
from tests.test_access_profiles import call, data


@pytest.fixture(scope='module',params=['chromium','webkit'])
def role_browser(request):
    with sync_playwright() as pw:
        executable=os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param=='chromium' else None
        browser=launch_browser(getattr(pw,request.param), **({'executable_path':executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width':1280,'height':900},{'width':390,'height':844}])
def role_page(role_browser,request,stack):
    page=role_browser.new_page(viewport=request.param)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(stack.url+'/#roles');page.fill('#username','admin');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#roles-page')).to_be_visible()
    yield page,errors
    page.close()


def test_role_editor_and_profile_binding_keep_stable_identity(role_page,stack):
    page,errors=role_page;label='Secretary '+uuid.uuid4().hex[:10]
    page.click('#role-create');form=page.locator('#role-form');form.locator('[name="label"]').fill(label)
    page.click('#role-add-project-rule')
    rule=page.locator('[data-project-rule]')
    rule.locator('[name="all_projects"]').check()
    rule.locator('[name="action"][value="write"]').check()
    page.locator('button[form="role-form"]').click();expect(form).to_have_count(0)
    row=page.locator('.grant-row').filter(has_text=label);expect(row).to_be_visible()
    identifier=row.locator('[data-role-edit]').get_attribute('data-role-edit')
    r=must(stack.client.get('/api/access-roles/'+identifier))
    assert r['project_rules'][0]['all_projects'] and r['project_rules'][0]['actions']==['read','write']
    row.locator('[data-role-edit]').click();expect(page.locator('#role-form')).to_be_visible()
    must(update_role(stack.client,r,enabled=False))
    page.locator('#role-form [name="label"]').fill(label+' stale')
    page.locator('button[form="role-form"]').click()
    expect(page.locator('#role-save-status')).to_contain_text('其他窗口修改')
    page.locator('.modal').get_by_role('button',name='取消',exact=True).click()
    page.locator('dialog.ui-sheet button[type="submit"]').click()
    page.evaluate("navigate('profiles')");expect(page.locator('#profile-create')).to_be_visible()
    page.click('#profile-create');profile_form=page.locator('#profile-form')
    profile_form.locator('[name="label"]').fill(label+' identity')
    page.select_option('#profile-role',identifier)
    expect(page.locator('[data-fixed-policy]')).to_be_hidden()
    page.locator('button[form="profile-form"]').click();expect(profile_form).to_have_count(0)
    records=stack.client.get('/api/access-profiles').json()['profiles']
    assert next(p for p in records if p['label']==label+' identity')['role_id']==identifier
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert not errors,errors


def test_explicit_role_oauth_and_live_new_project_without_reconnect(role_page,stack):
    page,errors=role_page;label='OAuthRole '+uuid.uuid4().hex[:10]
    r=role(stack.client,label=label,project_rules=[{'actions':['read'],'projects':[stack.project['id']]}])
    p=profile(stack.client,r,label)
    registration=must(stack.client.post('/oauth/register',json={'redirect_uris':['http://localhost:12345/callback']}),201)
    verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response=stack.client.get('/oauth/authorize',params={'response_type':'code','client_id':registration['client_id'],
        'redirect_uri':'http://localhost:12345/callback','scope':ROLE_SCOPE,'code_challenge_method':'S256',
        'code_challenge':challenge,'resource':stack.url+'/mcp'})
    request_id=parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]
    page.route('http://localhost:12345/callback*',lambda route:route.fulfill(body='Role test callback'))
    page.evaluate('(id)=>consentModal(id)',request_id)
    page.select_option('#access-profile-selector',p['id'])
    dialog=page.locator('.modal');confirm=dialog.locator('[name="confirm_dynamic_role"]')
    expect(confirm).not_to_be_checked();expect(dialog.locator('[name="project"]')).to_have_count(0)
    grants_before=len(stack.client.get('/api/grants').json()['grants'])
    dialog.locator('#allow-consent').click()
    expect(confirm).to_be_visible()
    assert len(stack.client.get('/api/grants').json()['grants'])==grants_before
    confirm.check()
    with page.expect_response(lambda response:'/api/oauth/requests/' in response.url and response.url.endswith('/decide')) as received:
        dialog.locator('#allow-consent').click()
    response=received.value;assert response.status==200
    payload=response.request.post_data_json
    assert payload['authorization_mode']=='role' and payload['scopes']==[ROLE_SCOPE] and payload['projects']==[]
    page.wait_for_url('http://localhost:12345/callback*')
    code=parse_qs(urlsplit(page.url).query)['code'][0]
    token=must(stack.client.post('/oauth/token',data={'grant_type':'authorization_code','code':code,'client_id':registration['client_id'],
            'redirect_uri':'http://localhost:12345/callback','code_verifier':verifier}))['access_token']
    must(update_role(stack.client,r,project_rules=[{'actions':['read'],'all_projects':True}]))
    context=data(call(stack.client,token,'get_access_context'))
    assert {item['id'] for item in context['projects']} >= {project['id'] for project in stack.projects}
    assert data(call(stack.client,token,'get_profile'))['id']==p['id']
    assert not errors,errors

@pytest.mark.parametrize("failure", ["network", "server", "conflict", "expired"])
def test_uncertain_role_creation_cannot_change_original_request(role_page, stack, failure):
    page, errors = role_page
    label = 'Recover role ' + uuid.uuid4().hex[:8]
    page.click('#role-create')
    form = page.locator('#role-form')
    form.locator('[name="label"]').fill(label)
    page.click('#role-add-project-rule')
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
    page.route('**/api/access-roles', save_then_disconnect)
    page.locator('button[form="role-form"]').click()
    first_message = {'conflict': '原请求键已用于其他参数', 'expired': '原请求已执行，历史响应已清理'}.get(failure, '未确认')
    expect(page.locator('#role-save-status')).to_contain_text(first_message)
    form.locator('[name="label"]').fill(label + ' changed')
    page.locator('button[form="role-form"]').click()
    expect(page.locator('#role-save-status')).to_contain_text('恢复原输入')
    assert len(requests) == 1
    form.locator('[name="label"]').fill(label)
    page.locator('button[form="role-form"]').click()
    expect(form).to_have_count(0)
    assert len(requests) == 2 and requests[0] == requests[1]
    roles = stack.must(stack.client.get('/api/access-roles'))['roles']
    assert len([p for p in roles if p['label'].startswith(label)]) == 1
    assert not errors, errors


@pytest.mark.parametrize("concurrent", [False, True])
def test_uncertain_role_update_retries_original_without_overwriting_concurrent(role_page, stack, concurrent):
    page, errors = role_page
    label = 'Update role ' + uuid.uuid4().hex[:8]
    saved = stack.must(stack.client.post('/api/access-roles', json={'label': label, 'project_rules': [{'actions': ['read'], 'projects': [stack.project['id']]}], 'device_rules': [], 'idempotency_key': uuid.uuid4().hex}))
    page.reload()
    page.locator('[data-role-edit="' + saved['id'] + '"]').click()
    form = page.locator('#role-form')
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
    page.route('**/api/access-roles/' + saved['id'], update_then_disconnect)
    page.locator('button[form="role-form"]').click()
    expect(page.locator('#role-save-status')).to_contain_text('未确认')
    if concurrent:
        current = stack.must(stack.client.get('/api/access-roles/' + saved['id']))
        changed = dict(requests[0], label=label + ' concurrent', expected_version=current['version'])
        stack.must(stack.client.put('/api/access-roles/' + saved['id'], json=changed))
    page.locator('button[form="role-form"]').click()
    if concurrent:
        expect(page.locator('#role-save-status')).to_contain_text('其他')
    else:
        expect(form).to_have_count(0)
    assert len(requests) == 2 and requests[0] == requests[1]
    current = stack.must(stack.client.get('/api/access-roles/' + saved['id']))
    assert current['label'] == label + (' concurrent' if concurrent else ' saved')
    assert current['version'] == saved['version'] + (2 if concurrent else 1)
    assert not errors, errors
