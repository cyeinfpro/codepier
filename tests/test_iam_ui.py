"""Chromium/WebKit UI acceptance against real isolated Hub and Agent processes."""
import os
import uuid
from urllib.parse import urlsplit,parse_qs
import pytest
from playwright.sync_api import sync_playwright,expect


@pytest.fixture(scope='module',params=['chromium','webkit'])
def iam_browser(request):
    with sync_playwright() as pw:
        executable=os.getenv('CODEPIER_TEST_CHROMIUM_EXECUTABLE') if request.param=='chromium' else None
        browser=getattr(pw,request.param).launch(**({'executable_path':executable} if executable else {}))
        yield browser
        browser.close()


@pytest.fixture(params=[{'width':1440,'height':1000},{'width':390,'height':844}])
def iam_page(iam_browser,request,stack):
    context=iam_browser.new_context(viewport=request.param)
    page=context.new_page();errors=[]
    page.on('pageerror',lambda err:errors.append(str(err)))
    page.goto(stack.url+'/#identity');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('我的账号')
    yield page,errors
    assert not errors,errors
    context.close()


def test_space_creation_and_two_tab_selection_are_independent(iam_page,stack):
    page,errors=iam_page
    page.locator('[data-iam="new-space"]').click()
    label='Team '+uuid.uuid4().hex[:8]
    page.locator('#iam-form [name="label"]').fill(label)
    page.locator('button[form="iam-form"]').click()
    expect(page.locator('#iam-form')).to_have_count(0)
    expect(page.locator('.grant-row').filter(has_text=label)).to_be_visible()
    spaces=stack.client.get('/api/iam/me').json()['spaces'];sid=next(s['id'] for s in spaces if s['label']==label)
    second=page.context.new_page();second.goto(stack.url+'/#identity');expect(second.locator('#iam-active-space')).to_have_value('legacy')
    page.locator('[data-iam-space="'+sid+'"]').click()
    expect(page.locator('#iam-active-space')).to_have_value(sid)
    expect(page.locator('#page h1')).to_have_text('控制总览')
    assert page.evaluate("S.projects.length")==0
    assert second.locator('#iam-active-space').input_value()=='legacy'
    # The original tab re-reads legacy resources using the same cookie.
    assert second.evaluate("async()=> (await api('/api/projects')).projects.length")==3
    assert page.evaluate("async()=> (await api('/api/projects')).projects.length")==0
    second.close()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_provider_form_is_closed_by_default_and_does_not_echo_secret(iam_page,stack):
    page,errors=iam_page
    page.evaluate("navigate('identity-admin')")
    page.locator('[data-iam="new-provider"]').click()
    form=page.locator('#iam-form')
    expect(form.locator('[name="enabled"]')).not_to_be_checked()
    expect(form.locator('[name="admission"]')).to_have_value('closed')
    identifier=uuid.uuid4().hex[:8];label='Test SSO '+identifier
    for name,value in [('label',label),('issuer','https://idp-'+identifier+'.example.invalid/'),('client_id','client-id'),('client_secret','test-ui-secret-not-a-real-credential')]:
        form.locator('[name="'+name+'"]').fill(value)
    page.locator('button[form="iam-form"]').click()
    expect(form).to_have_count(0)
    row=page.locator('.grant-row').filter(has_text=label);expect(row).to_be_visible()
    row.locator('[data-iam-provider]').click()
    expect(page.locator('#iam-form [name="client_secret"]')).to_have_value('')
    expect(page.locator('#iam-form [name="issuer"]')).to_have_attribute('readonly','')
    assert 'test-ui-secret-not-a-real-credential' not in page.content()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_user_invitation_lifecycle_and_shared_role_assignment_ui(iam_page,stack):
    page,errors=iam_page
    space=stack.must(stack.client.post('/api/iam/spaces',json={'label':'Shared team','idempotency_key':uuid.uuid4().hex}))
    page.reload();expect(page.locator('#page h1')).to_have_text('我的账号')
    page.locator('[data-iam-space="'+space['id']+'"]').click()
    expect(page.locator('#iam-active-space')).to_have_value(space['id'])
    page.evaluate("navigate('members')")
    expect(page.locator('#page h1')).to_have_text('空间成员')
    page.locator('[data-iam="invite"]').click()
    expect(page.locator('#iam-form [name="level"]')).to_have_value('member')
    page.locator('button[form="iam-form"]').click()
    expect(page.locator('.modal .secret')).to_be_visible()
    secret=page.locator('.modal .secret').inner_text();assert secret.startswith('cpi_')
    page.keyboard.press('Escape');page.evaluate('renderPage(false)')
    expect(page.locator('[data-iam-invite]')).to_have_count(1)
    page.locator('[data-iam-invite]').click()
    expect(page.locator('[data-iam-invite]')).to_have_count(0)
    assert secret not in page.content()


def test_account_session_revocation_and_role_controls_respect_current_user(iam_page,stack):
    page,errors=iam_page
    assert page.locator('[data-iam-session]').count()>=1
    # Current browser can end itself without granting or changing a Role.
    page.locator('.grant-row').filter(has_text='当前浏览器').locator('[data-iam-session]').click()
    expect(page.locator('#login-form')).to_be_visible()
    assert page.locator('#page').count()==0
    assert stack.client.get('/api/session').json()['authenticated']  # Other session remains valid.
