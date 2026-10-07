"""Management design acceptance with isolated Hub data and explicit UI-only adapters.

No production accounts, external MCP servers, real model turns or extension
permissions are used. The existing gateway/IAM suites retain backend authority
and consent assertions; these cases check the new presentation contracts.
"""
import json
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture

ROUTES = (
    'overview','devices','projects','vps','workbench','workflows','audit','connect',
    'profiles','roles','mcp-gateway','diagnostics','integrations','artifacts',
    'identity','members','identity-admin','settings','native',
)


@pytest.mark.parametrize('scheme', ['light','dark'])
@pytest.mark.parametrize('width,height', [(1440,1000),(390,844)])
def test_all_nineteen_routes_reflow_in_each_theme(stack, chat_browser_pool, tmp_path, scheme, width, height):
    _prepare_native_fixture(stack)
    context = chat_browser_pool('chromium').new_context(
        viewport={'width':width,'height':height},color_scheme=scheme,reduced_motion='reduce')
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        _login(page,stack)
        page.evaluate('scheme=>CodePierAppearance.setPreference(scheme)',scheme)
        for route in ROUTES:
            _navigate(page,route)
            if route == 'native':
                expect(page.locator('#chat-root')).to_be_visible()
                expect(page.locator('#chat-compose')).to_be_visible()
            else:
                title=page.evaluate('route=>nav.find(item=>item[0]===route)[2]',route)
                expect(page.locator('#page h1')).to_have_text(title)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),route
            assert page.evaluate('document.documentElement.dataset.appearance')==scheme
            page.screenshot(path=str(tmp_path/f'{route}-{width}-{scheme}.png'),full_page=True)
        _navigate(page,'identity')
        panels=page.locator('.management-grid > .panel')
        expect(panels.locator('.panel-head h2')).to_have_text([
            '空间', '登录身份', '我的密码', '登录会话',
        ])
        expect(page.locator('#password-form')).to_be_visible()
        rects=panels.evaluate_all('(nodes)=>nodes.map(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})')
        if width>1000:
            assert rects[0]['x']<rects[2]['x']
            assert all(rect['w']<1000 for rect in rects)
            for index, first in enumerate(rects):
                for second in rects[index+1:]:
                    assert (first['x']+first['w']<=second['x']
                            or second['x']+second['w']<=first['x']
                            or first['y']+first['h']<=second['y']
                            or second['y']+second['h']<=first['y'])
        else:
            assert all(current['y']>=previous['y']+previous['h']+15
                       for previous, current in zip(rects, rects[1:]))
        assert not errors,errors
    finally:
        context.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_editor_back_after_resize_keeps_dirty_input_until_confirmed(stack,chat_browser_pool,engine):
    page=chat_browser_pool(engine).new_page(viewport={'width':1440,'height':1000})
    try:
        _login(page,stack,'roles')
        page.click('#role-create')
        page.fill('#role-form [name=label]','Unsaved isolated role')
        page.set_viewport_size({'width':390,'height':844})
        expect(page.get_by_role('button',name='返回上一页')).to_be_visible()
        assert page.locator('.editor-back-label').evaluate('el=>el.getBoundingClientRect().height') < 30
        page.once('dialog',lambda dialog:dialog.dismiss())
        page.go_back()
        expect(page.locator('#role-form [name=label]')).to_have_value('Unsaved isolated role')
        assert page.evaluate("history.state?.codepierEditor != null")
        page.once('dialog',lambda dialog:dialog.accept())
        page.go_back()
        expect(page.locator('#role-form')).to_have_count(0)
        assert not page.evaluate("document.querySelector('#app').inert")
        expect(page.locator('#page h1')).to_have_text('访问角色')
    finally:
        page.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_policy_rule_switch_is_one_click_after_textarea_blur(stack,chat_browser_pool,engine):
    page=chat_browser_pool(engine).new_page(viewport={'width':1440,'height':1000})
    try:
        _login(page,stack,'roles');page.click('#role-create')
        page.click('#role-add-project-rule');page.click('#role-add-device-rule')
        page.fill('[data-device-rule] [name=root_prefixes]','/fixture/allowed')
        first=page.locator('.policy-rule-list button').first
        first.click()
        expect(page.locator('[data-project-rule]')).to_be_visible()
        expect(page.locator('[data-device-rule]')).to_be_hidden()
        page.locator('.policy-rule-list button').nth(1).click()
        expect(page.locator('[data-device-rule] [name=root_prefixes]')).to_have_value('/fixture/allowed')
        assert page.locator('#role-form [name=root_prefixes]').count()==1
        assert page.evaluate("document.querySelector('[name=root_prefixes]').form.id")=='role-form'
        expect(page.locator('[data-project-rule] [name=action][value=read]')).to_be_checked()
    finally:
        page.close()


def gateway_adapter(page, stack, *, uncertain=False):
    owner=stack.client.get('/api/iam/me').json()['id']
    state={'enabled':True,'instance_admin':True,'space_admin':True,'user_id':owner,
        'connectors':[{'id':'fixture-connector','label':'Synthetic service','endpoint':'https://example.invalid/mcp','protocol':'auto','enabled':True,'version':1,'allow_http':False}],
        'accounts':[{'id':'fixture-account','connector_id':'fixture-connector','label':'Synthetic account','sharing':'private','owner_user_id':owner,'enabled':True,'version':1,'catalog_hash':'a'*64}],
        'bindings':[],'grants':[],'calls':[]}
    requests=[]
    def route(r):
        path=r.request.url.split('?',1)[0]
        if r.request.method=='PATCH':
            requests.append(r.request.post_data_json)
            if uncertain:r.abort();return
        if path.endswith('/discover'):
            r.fulfill(json={'catalog_hash':'a'*64,'tools':[{'name':f'tool_{i}','description':f'Synthetic tool {i}','inputSchema':{'type':'object','properties':{'query':{'type':'string'}}}} for i in range(24)]});return
        r.fulfill(json=state)
    page.route('**/api/mcp-gateway**',route)
    return requests


@pytest.mark.parametrize('width',[1440,390])
def test_gateway_search_never_changes_selected_tools(stack,chat_browser_pool,width):
    page=chat_browser_pool('chromium').new_page(viewport={'width':width,'height':900})
    try:
        gateway_adapter(page,stack);_login(page,stack,'mcp-gateway')
        page.click('[data-gw-discover]')
        expect(page.locator('#gw-form [name=tool]:checked')).to_have_count(0)
        page.check('#gw-form [name=tool][value=tool_5]')
        page.fill('#gw-tool-search','no matching tool')
        expect(page.locator('.gw-tool-row:visible')).to_have_count(0)
        expect(page.locator('#gw-form [name=tool]:checked')).to_have_count(1)
        expect(page.locator('#gw-selection-count')).to_contain_text('已选 1')
        page.fill('#gw-tool-search','')
        page.check('#gw-selected-only')
        expect(page.locator('.gw-tool-row:visible')).to_have_count(1)
        page.click('#gw-clear-selection')
        expect(page.locator('#gw-form [name=tool]:checked')).to_have_count(0)
        expect(page.locator('#gw-form [name=confirmed]')).not_to_be_checked()
        assert page.evaluate("getComputedStyle(document.querySelector('.gw-tools')).overflowY")=='visible'
        page.fill('#gw-form [name=alias]','fixture_tools')
        page.check('#gw-form [name=confirmed]')
        page.click('button[form=gw-form]')
        expect(page.locator('#gw-status')).to_contain_text('至少选择一个工具')
        expect(page.locator('button[form=gw-form]')).to_be_enabled()
        expect(page.locator('#gw-form')).to_have_attribute('data-submission','rejected')
    finally:
        page.close()


def test_unknown_gateway_credential_save_cannot_resubmit_an_empty_token(stack,chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    try:
        requests=gateway_adapter(page,stack,uncertain=True);_login(page,stack,'mcp-gateway')
        page.click('[data-gw-account]');page.fill('#gw-form [name=token]','SYNTHETIC_UI_ONLY')
        page.click('button[form=gw-form]')
        expect(page.locator('#gw-form')).to_have_attribute('data-submission','uncertain')
        expect(page.locator('#gw-form [name=token]')).to_have_value('')
        expect(page.locator('button[form=gw-form]')).to_be_disabled()
        page.evaluate("document.querySelector('#gw-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
        assert len(requests)==1
        assert requests[0]['token']=='SYNTHETIC_UI_ONLY'
    finally:
        page.close()


def test_management_filter_and_focus_survive_refresh(stack,chat_browser_pool):
    marker='UI Filter '+uuid.uuid4().hex[:7]
    for i in range(5):
        stack.must(stack.client.post('/api/access-roles',json={'label':marker+str(i),'enabled':True,'project_rules':[],'device_rules':[],'connector_rules':[],'idempotency_key':uuid.uuid4().hex}))
    page=chat_browser_pool('chromium').new_page()
    try:
        _login(page,stack,'roles')
        search=page.locator('.entity-filter input');search.fill(marker+'2')
        expect(page.locator('#roles-page .grant-row:visible')).to_have_count(1)
        page.evaluate('renderPage(false)')
        expect(page.locator('.entity-filter input')).to_have_value(marker+'2')
        expect(page.locator('.entity-filter input')).to_be_focused()
        expect(page.locator('#roles-page .grant-row:visible')).to_have_count(1)
    finally:page.close()


@pytest.mark.parametrize('scheme',['light','dark'])
def test_extension_small_pages_reflow_and_failed_status_has_retry(stack,chat_browser_pool,scheme):
    context=chat_browser_pool('chromium').new_context(viewport={'width':390,'height':844},color_scheme=scheme)
    page=context.new_page()
    page.add_init_script("""window.chrome ||= {};window.fixtureStatusCalls=0;
      chrome.runtime={sendMessage:async()=>{fixtureStatusCalls++;
        if(fixtureStatusCalls===1)return {ok:false,message:'Synthetic disconnected state'};
        return {ok:true,data:{extension_id:'fixture-extension',profile_id:'fixture-profile',pool_size:4,available:4,leased:0,origins:[],connection:'Fixture connected'}};}};
    """)
    try:
        page.goto(stack.url+'/static/browser-extension/popup.html')
        expect(page.locator('#retry')).to_be_visible();expect(page.locator('#copy')).to_be_disabled()
        page.click('#retry')
        expect(page.locator('#copy')).to_be_enabled();expect(page.locator('#retry')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.goto(stack.url+'/static/browser-extension/idle.html')
        expect(page.locator('.idle-status')).to_contain_text('等待授权操作')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    finally:context.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_large_reader_is_full_page_and_keyboard_back_is_safe(stack,chat_browser_pool,engine):
    page=chat_browser_pool(engine).new_page(viewport={'width':390,'height':844})
    try:
        _login(page,stack,'workbench')
        prompts=[];page.on('dialog',lambda dialog:(prompts.append(dialog.message),dialog.dismiss()))
        page.evaluate("""modal('长只读详情',
            '<pre>'+'Synthetic read-only detail\\n'.repeat(100)+'</pre>','',true)""")
        expect(page.locator('.modal')).to_have_attribute('data-presentation','reader')
        rect=page.locator('.modal').bounding_box()
        assert rect and rect['x']==0 and rect['y']==0 and rect['height']==844
        expect(page.get_by_role('button',name='返回上一页')).to_be_visible()
        page.get_by_role('button',name='返回上一页').focus()
        page.keyboard.press('Tab')
        expect(page.locator('.modal-body')).to_be_focused()
        page.keyboard.press('PageDown')
        page.wait_for_function("() => document.querySelector('.modal-body').scrollTop>0")
        page.go_back()
        expect(page.locator('.modal')).to_have_count(0)
        expect(page.locator('#page h1')).to_have_text('远程工作台')
        assert not page.evaluate("document.querySelector('#app').inert")
        assert prompts==[]
    finally:page.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_remove_only_rule_and_node_name_both_guard_unsaved_leave(stack,chat_browser_pool,engine):
    role=stack.must(stack.client.post('/api/access-roles',json={
        'label':'Discard guard '+uuid.uuid4().hex[:8],'enabled':True,
        'project_rules':[{'actions':['read'],'projects':[stack.project['id']],'all_projects':False,'created_projects':False,'excluded_projects':[]}],
        'device_rules':[],'connector_rules':[],'idempotency_key':str(uuid.uuid4())}))
    page=chat_browser_pool(engine).new_page(viewport={'width':390,'height':844})
    try:
        _login(page,stack,'roles')
        page.locator('[data-role-edit="'+role['id']+'"]').click()
        expect(page.locator('[data-project-rule]')).to_have_count(1)
        page.locator('[data-remove-rule]').click()
        expect(page.locator('[data-project-rule]')).to_have_count(0)
        expect(page.locator('#role-add-project-rule')).to_be_focused()
        page.once('dialog',lambda dialog:dialog.dismiss());page.go_back()
        expect(page.locator('#role-form')).to_be_visible()
        expect(page.locator('[data-project-rule]')).to_have_count(0)
        page.once('dialog',lambda dialog:dialog.accept());page.go_back()
        expect(page.locator('#role-form')).to_have_count(0)
        saved=stack.must(stack.client.get('/api/access-roles/'+role['id']))
        assert len(saved['project_rules'])==1
        _navigate(page,'devices')
        page.evaluate('deviceDetail(S.devices[0].id)')
        expect(page.locator('.modal')).to_have_attribute('data-presentation','editor')
        page.fill('#device-rename','Unsaved synthetic node')
        page.once('dialog',lambda dialog:dialog.dismiss());page.go_back()
        expect(page.locator('#device-rename')).to_have_value('Unsaved synthetic node')
        page.once('dialog',lambda dialog:dialog.accept());page.go_back()
        expect(page.locator('.modal')).to_have_count(0)
        assert not page.evaluate("document.querySelector('#app').inert")
        page.evaluate('showAgentMaintenanceCommands(S.devices[0])')
        expect(page.locator('.modal')).to_have_attribute('data-presentation','editor')
        page.fill('#agent-command-form [name=install_dir]','/tmp/synthetic-agent-draft')
        page.once('dialog',lambda dialog:dialog.dismiss());page.go_back()
        expect(page.locator('#agent-command-form [name=install_dir]')).to_have_value('/tmp/synthetic-agent-draft')
        page.once('dialog',lambda dialog:dialog.accept());page.go_back()
        expect(page.locator('.modal')).to_have_count(0)
        page.evaluate('computerPanel(S.projects[0].id)')
        expect(page.locator('.modal')).to_have_attribute('data-presentation','tool')
        page.go_back()
        expect(page.locator('.modal')).to_have_count(0)
    finally:page.close()
