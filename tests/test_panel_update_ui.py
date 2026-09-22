"""Real Chromium/WebKit settings flows; host deploy responses are simulated."""
import copy
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.util import VERSION


@pytest.fixture(scope='module',params=['chromium','webkit'])
def update_browser(request):
    with sync_playwright() as pw:
        browser=getattr(pw,request.param).launch()
        yield browser,request.param
        browser.close()


def ready():
    return {'enabled':True,'repository':'cyeinfpro/codepier','running_version':VERSION,
            'current_version':VERSION,'busy':False,'recovery_required':False,'update_available':True,
            'candidate':{'version':'1.11.0','release_id':21,'sha256':'a'*64,
                         'notes':'<img src=x onerror=alert(1)>\nRelease notes','checked_at':time.time()},
            'operation':None,'request_found':None}


def open_settings(browser,stack,state,width=1440,post_handler=None):
    page=browser.new_page(viewport={'width':width,'height':960 if width>500 else 844})
    calls=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def route(r):
        path=urlsplit(r.request.url).path
        if r.request.method=='POST':
            calls.append({'path':path,'body':r.request.post_data_json})
            if post_handler:return post_handler(r,calls)
            r.fulfill(status=202,json={'operation':state.get('operation')})
        else:r.fulfill(json=copy.deepcopy(state))
    page.route('**/api/panel-update/**',route)
    page.goto(stack.url+'/#settings')
    page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('系统设置')
    expect(page.locator('#panel-update')).to_be_visible()
    return page,calls,errors


@pytest.mark.parametrize('width',[1440,390])
def test_disabled_setup_and_mobile_layout(update_browser,stack,width):
    browser,kind=update_browser
    state={'enabled':False,'running_version':VERSION,'reason':'宿主机尚未启用面板更新服务','code':'UPDATER_NOT_CONFIGURED'}
    page,calls,errors=open_settings(browser,stack,state,width)
    try:
        expect(page.locator('#panel-update-state')).to_contain_text('尚未启用')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        expect(page.locator('#panel-update-check')).to_be_disabled()
        page.click('#panel-update-setup summary')
        expect(page.locator('#panel-update-setup')).to_contain_text('scripts/panel_updater.py install')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        path=Path('.work/panel-update/screenshots');path.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(path/f'{kind}-{width}-setup.png'),full_page=True)
        assert not calls and not errors,errors
    finally:page.close()


@pytest.mark.parametrize('width',[1440,390])
def test_update_confirmation_single_submission_restart_recovery_and_escaping(update_browser,stack,width):
    browser,kind=update_browser;state=ready()
    def post(r,calls):
        body=calls[-1]['body']
        state.update(busy=True,request_found=True,operation={'id':'c'*32,'kind':'apply','state':'running',
            'phase':'building','message':'正在构建候选版本','target_version':'1.11.0',
            'events':[{'at':time.time(),'message':'正在构建候选版本'}]})
        r.fulfill(status=202,json={'operation':state['operation']})
    page,calls,errors=open_settings(browser,stack,state,width,post)
    try:
        expect(page.locator('#panel-update-apply')).to_be_enabled()
        page.click('#panel-update-release summary')
        expect(page.locator('#panel-update-notes')).to_have_text('<img src=x onerror=alert(1)>\nRelease notes')
        expect(page.locator('#panel-update-notes img')).to_have_count(0)
        page.once('dialog',lambda dialog:dialog.dismiss());page.click('#panel-update-apply')
        assert calls==[]
        page.once('dialog',lambda dialog:dialog.accept());page.click('#panel-update-apply',delay=150)
        expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        assert len(calls)==1 and calls[0]['body']['confirmation']=='1.11.0'
        page.reload()
        expect(page.locator('#page h1')).to_have_text('系统设置')
        expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
        assert len(calls)==1
        state.update(busy=False,current_version='1.11.0',running_version='1.11.0',update_available=False)
        state['operation'].update(state='succeeded',phase='done',message='面板及 Agent 文件更新成功')
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-reload')).to_be_visible()
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        expect(page.locator('#panel-update-check')).to_be_enabled()
        assert len(calls)==1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        path=Path('.work/panel-update/screenshots');path.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(path/f'{kind}-{width}-complete.png'),full_page=True)
        assert not errors,errors
    finally:page.close()


def test_lost_submission_response_queries_receipt_without_second_post(update_browser,stack):
    browser,_=update_browser;state=ready()
    def post(r,calls):
        state.update(busy=True,request_found=True,operation={'id':'d'*32,'kind':'check','state':'running',
                     'phase':'checking','message':'正在检查 GitHub 正式 Release','events':[]})
        r.abort('failed')
    page,calls,errors=open_settings(browser,stack,state,post_handler=post)
    try:
        expect(page.locator('#panel-update-check')).to_be_enabled();page.click('#panel-update-check')
        expect(page.locator('#panel-update-state')).to_contain_text('正在检查 GitHub')
        expect(page.locator('#panel-update-check')).to_be_disabled()
        assert len(calls)==1
        state.update(busy=False,request_found=True)
        state['operation'].update(state='failed',message='GitHub 请求限流，请稍后重新检查')
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-state')).to_contain_text('GitHub 请求限流')
        expect(page.locator('#panel-update-check')).to_be_enabled()
        assert len(calls)==1 and not errors,errors
    finally:page.close()


def test_missing_receipt_retries_only_original_key_and_recovery_locks_controls(update_browser,stack):
    browser,_=update_browser;state=ready();state['request_found']=False
    def post(r,calls):
        if len(calls)==1:r.abort('failed')
        else:
            state.update(busy=False,request_found=True,recovery_required=True,
                operation={'id':'e'*32,'kind':'check','state':'recovery_required','message':'需宿主机检查，不可重复更新','events':[]})
            r.fulfill(status=202,json={'operation':state['operation']})
    page,calls,errors=open_settings(browser,stack,state,post_handler=post)
    try:
        expect(page.locator('#panel-update-check')).to_be_enabled();page.click('#panel-update-check')
        expect(page.locator('#panel-update-retry')).to_be_visible()
        first=calls[0]['body']['idempotency_key']
        page.click('#panel-update-retry')
        expect(page.locator('#panel-update-state')).to_contain_text('需宿主机检查')
        assert len(calls)==2 and calls[1]['body']['idempotency_key']==first
        expect(page.locator('#panel-update-check')).to_be_disabled()
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        expect(page.locator('#panel-update-retry')).to_be_hidden()
        assert not errors,errors
    finally:page.close()
