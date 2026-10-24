"""Real Chromium/WebKit settings flows; host deploy responses are simulated."""
import copy
import os
import re
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.util import VERSION


@pytest.fixture(scope='module',params=['chromium','webkit'])
def update_browser(request):
    with sync_playwright() as pw:
        executable=os.environ.get('CODEPIER_TEST_'+request.param.upper()+'_EXECUTABLE')
        options={'executable_path':executable} if executable else {}
        browser=getattr(pw,request.param).launch(**options)
        yield browser,request.param
        browser.close()


def ready():
    return {'enabled':True,'repository':'cyeinfpro/codepier','running_version':VERSION,
            'current_version':VERSION,'busy':False,'recovery_required':False,'update_available':True,
            'candidate':{'version':'1.11.0','release_id':21,'sha256':'a'*64,
                         'notes':'<img src=x onerror=alert(1)>\nRelease notes','checked_at':time.time()},
            'operation':None,'request_found':None}


def show_update_controls(page):
    """Reach maintenance through the same settings navigation as a user."""
    page.locator('.settings-groups [data-settings-group="advanced"]').click()
    maintenance = page.locator('.settings-maintenance')
    expect(maintenance).to_be_visible()
    if not maintenance.evaluate('el => el.open'):
        maintenance.locator('summary').first.click()
    expect(page.locator('#panel-update')).to_be_visible()


def open_settings(browser,stack,state,width=1440,post_handler=None,init_script=None,get_handler=None,clock=False,has_touch=False):
    page=browser.new_page(viewport={'width':width,'height':960 if width>500 else 844},has_touch=has_touch)
    if clock:
        page.clock.install()
    if init_script:
        page.add_init_script(init_script)
    calls=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def route(r):
        path=urlsplit(r.request.url).path
        if r.request.method=='POST':
            calls.append({'path':path,'body':r.request.post_data_json})
            if post_handler:return post_handler(r,calls)
            r.fulfill(status=202,json={'operation':state.get('operation')})
        elif get_handler:get_handler(r)
        else:r.fulfill(json=copy.deepcopy(state))
    page.route('**/api/panel-update/**',route)
    page.goto(stack.url+'/#settings')
    page.fill('#username', 'admin');page.fill('#password',stack.password);page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('设置中心')
    show_update_controls(page)
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
        expect(page.locator('#page h1')).to_have_text('设置中心')
        show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
        assert len(calls)==1
        state.update(busy=False,current_version='1.11.0',running_version='1.11.0',update_available=False)
        state['operation'].update(state='succeeded',phase='done',message='面板及 Agent 文件更新成功')
        # The existing page must discover completion and navigate without a user refresh.
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'), timeout=15000)
        show_update_controls(page)
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


@pytest.mark.parametrize('width',[1440,390,320])
@pytest.mark.parametrize('theme',['light','dark'])
def test_global_update_entry_and_failure_banner_removal(update_browser,stack,width,theme):
    browser,kind=update_browser
    state=ready()
    state['update_available']=False
    state['agent_rollout']={'state':'running','target_version':VERSION,'total':2,'completed':0,'message':'',
        'nodes':[{'device_id':'synthetic-offline','name':'Synthetic offline <node>','state':'offline',
                  'message':'离线待更新，重连后继续','version':'1.0.0','operation_id':''},
                 {'device_id':'synthetic-busy','name':'Synthetic busy','state':'waiting_idle',
                  'message':'等待现有写任务结束','version':'1.0.0','operation_id':''}]}
    page,calls,errors=open_settings(browser,stack,state,width,
        init_script=f"localStorage.setItem('codepier-appearance','{theme}')")
    try:
        def overview(route):
            response=route.fetch()
            value=response.json()
            value['today_failed']=507
            value['recent_operations']=[{'id':'synthetic-failure','tool':'fs_write','state':'failed',
                'created':time.time(),'actor':'panel:admin','alias':'Synthetic project'}]
            route.fulfill(response=response,json=value)
        page.route('**/api/overview',overview)
        page.evaluate("navigate('overview')")
        expect(page.locator('#page h1')).to_have_text('控制总览')
        expect(page.locator('.attention-strip')).to_have_count(0)
        expect(page.locator('[data-cp-key="operation:synthetic-failure"] .badge.failed')).to_be_visible()
        entry=page.locator('[data-panel-update-open]')
        expect(entry).to_be_visible()
        expect(entry).to_contain_text(VERSION)
        box=entry.bounding_box()
        assert box and box['y']>=0 and box['y']+box['height']<=150
        topbar=page.locator('.topbar').bounding_box()
        heading=page.locator('#page h1').bounding_box()
        assert topbar and heading
        assert box['y']+box['height']<=topbar['y']+topbar['height']+1
        assert heading['y']>=box['y']+box['height']+8
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        path=Path('.work/panel-agent-update/screenshots');path.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(path/f'{kind}-{theme}-{width}-entry.png'),animations='disabled')
        entry.click()
        expect(page.locator('#page h1')).to_have_text('控制总览')
        expect(page.locator('#panel-update-popover')).to_be_visible()
        assert not calls
        page.locator('[data-panel-update-close]').click()
        page.evaluate("navigate('settings')")
        show_update_controls(page)
        expect(page.locator('#panel-update')).to_be_visible()
        expect(page.locator('#panel-update-agents')).to_be_checked()
        expect(page.locator('#panel-agent-rollout')).to_be_visible()
        expect(page.locator('[data-agent-update-state="offline"]')).to_contain_text('Synthetic offline <node>')
        expect(page.locator('[data-agent-update-state="waiting_idle"]')).to_contain_text('等待现有写任务结束')
        assert page.locator('#panel-agent-rollout node').count()==0
        page.screenshot(path=str(path/f'{kind}-{theme}-{width}-progress.png'),full_page=True,animations='disabled')
        assert not errors,errors
        assert not calls
    finally:page.close()


def test_agent_auto_update_opt_out_is_preserved_in_original_request(update_browser,stack):
    browser,_=update_browser
    state=ready()
    def post(route,calls):
        state.update(busy=True,request_found=True,operation={'id':'d'*32,'kind':'apply','state':'running',
            'target_version':'1.11.0','message':'正在准备','events':[]})
        route.fulfill(status=202,json={'operation':state['operation']})
    page,calls,errors=open_settings(browser,stack,state,post_handler=post)
    try:
        page.uncheck('#panel-update-agents')
        page.once('dialog',lambda dialog:dialog.accept())
        page.click('#panel-update-apply')
        expect(page.locator('#panel-update-agents')).to_be_disabled()
        assert len(calls)==1 and calls[0]['body']['update_agents'] is False
        page.reload();show_update_controls(page)
        expect(page.locator('#panel-update-agents')).not_to_be_checked()
        assert len(calls)==1
        assert not errors,errors
    finally:page.close()


def test_top_update_entry_keeps_same_settings_draft(update_browser,stack):
    browser,_=update_browser
    page,calls,errors=open_settings(browser,stack,ready())
    try:
        field=page.locator('#settings-form [name="public_url"]')
        field.evaluate("(el,value)=>{el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));}",stack.url+'/unsaved')
        page.locator('[data-panel-update-open]').click()
        expect(page.locator('#panel-update-popover')).to_be_visible()
        expect(page.locator('#panel-update')).to_be_visible()
        expect(field).to_have_value(stack.url+'/unsaved')
        assert len(calls)==1 and calls[0]['body']['update_agents'] is False
        assert not errors,errors
    finally:page.close()


def test_top_entry_direct_apply_double_click_and_original_receipt(update_browser,stack):
    browser,_=update_browser;state=ready();queries=[];dialogs=[]
    def get(route):
        queries.append(route.request.url)
        route.fulfill(json=copy.deepcopy(state))
    def post(route,calls):
        state.update(busy=True,request_found=True,operation={
            'id':'f'*32,'kind':'apply','state':'running','phase':'building',
            'message':'正在构建候选版本','target_version':'1.11.0','events':[]})
        route.fulfill(status=202,json={'operation':state['operation']})
    page,calls,errors=open_settings(browser,stack,state,post_handler=post,get_handler=get)
    try:
        page.evaluate("navigate('overview')")
        expect(page.locator('#page h1')).to_have_text('控制总览')
        entry=page.locator('[data-panel-update-open]')
        page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.dismiss()))
        entry.dblclick()
        expect(page.locator('#panel-update-float-state')).to_contain_text('正在构建')
        assert len(calls)==1 and not dialogs
        original=copy.deepcopy(calls[0]['body'])
        assert calls[0]['path']=='/api/panel-update/apply'
        assert original['confirmation']=='1.11.0' and original['update_agents'] is False
        page.locator('[data-panel-update-close]').click()
        page.evaluate("navigate('settings')")
        show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
        page.reload();show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('正在构建')
        assert len(calls)==1
        assert any('request_key='+original['idempotency_key'] in url for url in queries)
        # A terminal receipt alone must never claim the target version is running.
        state.update(busy=False)
        state['operation'].update(state='succeeded',message='面板更新成功')
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-note')).to_contain_text('等待目标版本')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        expect(page.locator('[data-panel-update-open]')).to_have_attribute('data-state','verifying')
        expect(page.locator('#panel-update-reload')).to_be_hidden()
        expect(page.locator('[data-panel-update-version]')).to_have_text('v'+VERSION)
        state.update(running_version='1.11.0',current_version='1.11.0',update_available=False)
        page.click('#panel-update-refresh')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'))
        expect(page.locator('[data-panel-update-version]')).to_have_text('v1.11.0')
        assert len(calls)==1 and not errors,errors
    finally:page.close()


def test_automatic_check_never_silently_applies(update_browser,stack):
    browser,_=update_browser;state=ready();discovery=[]
    state.update(candidate=None,update_available=False)
    def get(route):
        if urlsplit(route.request.url).path.endswith('/available'):
            discovery.append(route)
        else:route.fulfill(json=copy.deepcopy(state))
    page,calls,errors=open_settings(browser,stack,state,get_handler=get)
    try:
        expect(page.locator('[data-panel-update-open]')).to_have_attribute('data-state','checking')
        assert len(discovery)==1 and not calls
        result=copy.deepcopy(state)
        result.update(discovery_candidate=ready()['candidate'],discovery_available=True,
                      discovery_checked_at=time.time(),discovery_error=None)
        discovery[0].fulfill(json=result)
        expect(page.locator('[data-panel-update-status]')).to_contain_text('更新面板至 v1.11.0')
        page.evaluate("navigate('overview')")
        assert not calls
        expect(page.locator('#page h1')).to_have_text('控制总览')
        assert not errors,errors
    finally:page.close()


def test_automatic_check_ttl_uses_fresh_candidate_and_rechecks_expiry(update_browser,stack):
    browser,_=update_browser;state=ready();discovery=[]
    state.update(update_available=False)
    def get(route):
        result=copy.deepcopy(state)
        if urlsplit(route.request.url).path.endswith('/available'):
            discovery.append(route.request.url)
            result.update(discovery_candidate=copy.deepcopy(state['candidate']),discovery_available=False,
                          discovery_checked_at=page.evaluate('Date.now()/1000'),discovery_error=None)
        route.fulfill(json=result)
    page,calls,errors=open_settings(browser,stack,state,get_handler=get,clock=True)
    try:
        entry=page.locator('[data-panel-update-open]')
        expect(entry).to_have_attribute('data-state','latest')
        assert not calls and not discovery
        page.clock.fast_forward(240000)
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','latest')
        assert not calls and not discovery
        page.clock.fast_forward(61000)
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','latest')
        assert len(discovery)==1 and not calls
        page.clock.fast_forward(240000)
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','latest')
        assert len(discovery)==1 and not calls
        assert not errors,errors
    finally:page.close()


@pytest.mark.parametrize('blocked',['busy','recovery'])
def test_automatic_check_never_displaces_existing_work(update_browser,stack,blocked):
    browser,_=update_browser;state=ready()
    state.update(candidate=None,update_available=False)
    state['operation']={'id':'c'*32,'kind':'apply','state':'running',
                        'message':'原更新处理中','events':[]}
    if blocked=='busy':state['busy']=True
    else:
        state.update(recovery_required=True)
        state['operation'].update(state='recovery_required')
    page,calls,errors=open_settings(browser,stack,state,clock=True)
    try:
        page.clock.fast_forward(360000)
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-state')).to_contain_text('原更新处理中')
        assert not calls and not errors,errors
    finally:page.close()


@pytest.mark.parametrize('resume_during_press',[False,True])
def test_top_entry_lost_response_recovers_without_post_replay(update_browser,stack,resume_during_press):
    browser,_=update_browser;state=ready();queries=[];offline=False
    def get(route):
        queries.append(route.request.url)
        if offline:route.abort('failed')
        else:route.fulfill(json=copy.deepcopy(state))
    def post(route,calls):
        state.update(busy=True,request_found=True,operation={
            'id':'e'*32,'kind':'apply','state':'running','message':'原更新处理中',
            'target_version':'1.11.0','events':[]})
        route.abort('failed')
    page,calls,errors=open_settings(browser,stack,state,post_handler=post,get_handler=get,clock=True)
    try:
        page.evaluate("navigate('overview')")
        page.locator('[data-panel-update-open]').click()
        expect(page.locator('#panel-update-float-state')).to_contain_text('原更新处理中')
        key=calls[0]['body']['idempotency_key']
        offline=True
        page.click('#panel-update-float-refresh')
        expect(page.locator('#panel-update-float-note')).to_contain_text('连接')
        page.clock.fast_forward(8000)
        assert len(calls)==1
        offline=False
        page.clock.fast_forward(4000)
        expect(page.locator('#panel-update-float-state')).to_contain_text('原更新处理中')
        if resume_during_press:
            # Hold the boot snapshot so the real resume runs between pointerdown
            # and pointerup. WebKit drops click when its text hit target changes.
            page.add_init_script("""(() => {
              const fetch = window.fetch;
              let held = false;
              window.fetch = async function(...args) {
                const response = await fetch.apply(this, args);
                if (!held && String(args[0]).includes('/api/overview')) {
                  held = true;
                  await new Promise(resolve => {
                    window.__releaseUpdateOverview = resolve;
                    document.documentElement.dataset.updateOverviewHeld = 'true';
                  });
                }
                return response;
              };
            })();""")
        page.reload()
        entry=page.locator('[data-panel-update-open]')
        if resume_during_press:
            expect(page.locator('html')).to_have_attribute('data-update-overview-held','true')
            expect(entry).to_contain_text('检查更新')
            box=entry.bounding_box()
            assert box
            page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
            page.mouse.down()
            try:
                page.evaluate('window.__releaseUpdateOverview()')
                expect(entry).to_have_attribute('data-state','updating')
            finally:page.mouse.up()
        else:entry.click()
        expect(page.locator('#panel-update-float-state')).to_contain_text('原更新处理中')
        assert len(calls)==1 and all('request_key='+key in url for url in queries if 'request_key=' in url)
        assert not errors,errors
    finally:page.close()


@pytest.mark.parametrize('activation',['Enter','Space','touch'])
def test_top_entry_native_activation_survives_repeated_status_refresh(update_browser,stack,activation):
    browser,_=update_browser;state=ready()
    state.update(busy=True,operation={'id':'d'*32,'kind':'apply','state':'running',
                                    'message':'原更新处理中','events':[]})
    page,calls,errors=open_settings(browser,stack,state,
        width=390 if activation=='touch' else 1440,has_touch=activation=='touch')
    try:
        page.evaluate("navigate('overview')")
        expect(page.locator('#page h1')).to_have_text('控制总览')
        entry=page.locator('[data-panel-update-open]')
        for _ in range(2):
            if activation=='touch':entry.tap()
            else:
                entry.focus()
                page.keyboard.press(activation)
            expect(page.locator('#panel-update-float-state')).to_contain_text('原更新处理中')
            page.click('#panel-update-float-refresh')
            expect(entry).to_have_attribute('data-state','updating')
            page.keyboard.press('Escape')
            expect(page.locator('#panel-update-popover')).to_have_count(0)
            expect(entry).to_be_focused()
        assert not calls and not errors,errors
    finally:page.close()


@pytest.mark.parametrize('status',[403,404])
@pytest.mark.parametrize('endpoint',['status','available'])
def test_permission_or_unsupported_status_stops_background_checks(update_browser,stack,status,endpoint):
    browser,_=update_browser;state=ready();queries=[]
    if endpoint=='available':state.update(candidate=None,update_available=False)
    def get(route):
        queries.append(route.request.url)
        if urlsplit(route.request.url).path.endswith('/'+endpoint):
            route.fulfill(status=status,json={'error':'更新服务不可用','code':'FORBIDDEN' if status==403 else 'NOT_FOUND'})
        else:route.fulfill(json=copy.deepcopy(state))
    page,calls,errors=open_settings(browser,stack,state,get_handler=get,clock=True)
    try:
        expect(page.locator('#panel-update-note')).to_be_visible()
        expect(page.locator('[data-panel-update-open]')).to_have_attribute('data-state','unknown')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        count=len(queries)
        page.clock.fast_forward(360000)
        assert len(queries)==count and not calls
        page.locator('[data-panel-update-open]').click()
        expect(page.locator('#page h1')).to_have_text('设置中心')
        assert not calls and not errors,errors
    finally:page.close()


def test_logout_stops_update_watch_and_cleans_floating_controls(update_browser,stack):
    browser,_=update_browser;state=ready();queries=[];held=[];hold=False
    state.update(busy=True,operation={'id':'d'*32,'kind':'apply','state':'running',
                                    'message':'原更新处理中','events':[]})
    def get(route):
        queries.append(route.request.url)
        if hold:held.append(route)
        else:route.fulfill(json=copy.deepcopy(state))
    page,calls,errors=open_settings(browser,stack,state,get_handler=get,clock=True)
    try:
        page.locator('[data-panel-update-open]').click()
        expect(page.locator('#panel-update-popover')).to_be_visible()
        hold=True
        with page.expect_request(lambda request:'/api/panel-update/status' in request.url):
            page.click('#panel-update-float-refresh')
        with page.expect_event('requestfailed',predicate=lambda request:'/api/panel-update/status' in request.url):
            page.click('[data-action="logout"]')
        expect(page.locator('#login-form')).to_be_visible()
        expect(page.locator('#panel-update-popover')).to_have_count(0)
        assert held
        # A response arriving after logout cannot recreate user-specific UI or timers.
        for route in held:route.fulfill(json=copy.deepcopy(state))
        count=len(queries)
        page.clock.fast_forward(360000)
        assert len(queries)==count and not calls
        expect(page.locator('#panel-update-popover')).to_have_count(0)
        expect(page.locator('[data-panel-update-open]')).to_have_count(0)
        assert not errors,errors
    finally:page.close()


def test_stale_status_never_advertises_or_applies_cached_candidate(update_browser,stack):
    browser,_=update_browser;state=ready();offline=False
    def get(route):
        if offline:route.abort('failed')
        else:route.fulfill(json=copy.deepcopy(state))
    page,calls,errors=open_settings(browser,stack,state,get_handler=get)
    try:
        entry=page.locator('[data-panel-update-open]')
        expect(entry).to_have_attribute('data-state','available')
        offline=True
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','unknown')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        entry.click()
        expect(page.locator('#panel-update-float-apply')).to_be_disabled()
        assert not calls
        offline=False
        page.click('#panel-update-float-refresh')
        expect(entry).to_have_attribute('data-state','available')
        assert not calls and not errors,errors
    finally:page.close()


def test_automatic_discovery_failure_never_creates_durable_jobs(update_browser,stack):
    browser,_=update_browser;state=ready();discovery=[]
    state.update(candidate=None,update_available=False)
    def get(route):
        result=copy.deepcopy(state)
        if urlsplit(route.request.url).path.endswith('/available'):
            discovery.append(route.request.url)
            result.update(discovery_candidate=None,discovery_available=False,
                          discovery_checked_at=time.time(),
                          discovery_error={'code':'RELEASE_UNAVAILABLE','message':'GitHub 请求限流，请稍后检查'})
        route.fulfill(json=result)
    page,calls,errors=open_settings(browser,stack,state,get_handler=get,clock=True)
    try:
        entry=page.locator('[data-panel-update-open]')
        expect(entry).to_have_attribute('data-state','failed')
        assert len(discovery)==1 and not calls
        page.clock.fast_forward(30000)
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','failed')
        assert len(discovery)==1 and not calls
        page.clock.fast_forward(300000)
        page.click('#panel-update-refresh')
        expect(entry).to_have_attribute('data-state','failed')
        assert len(discovery)>=2 and not calls
        assert not errors,errors
    finally:page.close()


@pytest.mark.parametrize('outcome',['match','reload','changed_release'])
def test_top_discovery_click_checks_exact_release_then_applies_once(update_browser,stack,outcome):
    browser,_=update_browser;state=ready();candidate=copy.deepcopy(state['candidate']);queries=[]
    state.update(candidate=None,update_available=False)
    def get(route):
        queries.append(route.request.url)
        result=copy.deepcopy(state)
        if urlsplit(route.request.url).path.endswith('/available'):
            result.update(discovery_candidate=candidate,discovery_available=True,
                          discovery_checked_at=time.time(),discovery_error=None)
        route.fulfill(json=result)
    def post(route,calls):
        if calls[-1]['path'].endswith('/check'):
            state.update(busy=True,request_found=True,operation={
                'id':'1'*32,'kind':'check','state':'running','message':'核验所选发布','events':[]})
        else:
            state.update(busy=True,request_found=True,operation={
                'id':'2'*32,'kind':'apply','state':'running','message':'安装已核验发布',
                'target_version':candidate['version'],'events':[]})
        route.fulfill(status=202,json={'operation':state['operation']})
    page,calls,errors=open_settings(browser,stack,state,post_handler=post,get_handler=get,clock=True)
    try:
        entry=page.locator('[data-panel-update-open]')
        expect(entry).to_have_attribute('data-state','available')
        assert not calls
        page.evaluate("navigate('overview')")
        entry.dblclick()
        expect(page.locator('#panel-update-float-state')).to_contain_text('核验所选发布')
        assert len(calls)==1 and calls[0]['path'].endswith('/check')
        check_key=calls[0]['body']['idempotency_key']
        if outcome=='reload':
            page.reload()
            page.locator('[data-panel-update-open]').click()
            expect(page.locator('#panel-update-float-state')).to_contain_text('核验所选发布')
            assert len(calls)==1
        verified=copy.deepcopy(candidate)
        if outcome=='changed_release':verified['sha256']='b'*64
        state.update(busy=False,request_found=True,candidate=verified,update_available=True)
        state['operation'].update(state='succeeded',message='所选发布核验完成')
        page.click('#panel-update-float-refresh')
        if outcome=='changed_release':
            expect(page.locator('#panel-update-float-state')).to_contain_text('核验完成')
            page.clock.fast_forward(10000)
            assert len(calls)==1
        else:
            expect(page.locator('#panel-update-float-state')).to_contain_text('安装已核验发布')
            assert len(calls)==2 and calls[1]['path'].endswith('/apply')
            body=calls[1]['body']
            assert body['idempotency_key']!=check_key
            assert (body['version'],body['release_id'],body['sha256'])==(
                candidate['version'],candidate['release_id'],candidate['sha256'])
            assert body['update_agents'] is False
            page.reload()
            page.clock.fast_forward(5000)
            assert len(calls)==2
        assert any('request_key='+check_key in url for url in queries)
        assert not errors,errors
    finally:page.close()


def test_manual_check_reload_has_no_implicit_install_continuation(update_browser,stack):
    browser,_=update_browser;state=ready();queries=[]
    def get(route):
        queries.append(route.request.url)
        route.fulfill(json=copy.deepcopy(state))
    def post(route,calls):
        state.update(busy=True,request_found=True,operation={
            'id':'3'*32,'kind':'check','state':'running','message':'检查原请求','events':[]})
        route.abort('failed')
    page,calls,errors=open_settings(browser,stack,state,post_handler=post,get_handler=get,clock=True)
    try:
        page.click('#panel-update-check')
        expect(page.locator('#panel-update-state')).to_contain_text('检查原请求')
        assert len(calls)==1
        key=calls[0]['body']['idempotency_key']
        page.reload();show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('检查原请求')
        state.update(busy=False)
        state['operation'].update(state='succeeded',message='发现可用发布')
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-state')).to_contain_text('发现可用发布')
        expect(page.locator('#panel-update-apply')).to_be_enabled()
        page.clock.fast_forward(10000)
        assert len(calls)==1 and calls[0]['path'].endswith('/check')
        assert any('request_key='+key in url for url in queries)
        assert not errors,errors
    finally:page.close()


def test_legacy_host_notice_and_engine_activation_are_not_hidden(update_browser, stack):
    browser, kind = update_browser
    state = ready()
    state.update(host_setup_required=True, host_update_notice='旧宿主可以更新 Hub，但宿主引导需管理员初始化。')
    page, calls, errors = open_settings(browser, stack, state)
    try:
        expect(page.locator('#panel-update-host-notice')).to_be_visible()
        expect(page.locator('#panel-update-host-notice')).to_contain_text('管理员初始化')
        expect(page.locator('#panel-update-setup')).to_be_visible()
        expect(page.locator('#panel-update-apply')).to_be_enabled()
        state.update(host_setup_required=False, host_update_notice='', busy=True,
                     host_engine={'active': VERSION, 'ready': False, 'activating': True})
        page.click('#panel-update-refresh')
        expect(page.locator('#panel-update-state')).to_contain_text('请等待就绪回执')
        expect(page.locator('#panel-update-apply')).to_be_disabled()
        assert not calls and not errors
    finally:
        page.close()
