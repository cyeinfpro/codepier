"""Role policy editing, profile binding and explicit live consent in real browsers."""
import base64
import hashlib
import os
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.role_contracts import ROLE_SCOPE
from tests.test_roles import role, profile, update_role, must
from tests.test_access_profiles import call, data


@pytest.fixture(scope='module',params=['chromium','webkit'])
def role_browser(request):
    with sync_playwright() as pw:
        executable=os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param=='chromium' else None
        browser=getattr(pw,request.param).launch(**({'executable_path':executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width':1280,'height':900},{'width':390,'height':844}])
def role_page(role_browser,request,stack):
    page=role_browser.new_page(viewport=request.param)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(stack.url+'/#roles');page.fill('#password',stack.password);page.click('#login-form button')
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
    page.locator('.modal [data-action="close-modal"]').click()
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
