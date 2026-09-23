"""Real Chromium/WebKit panel flows against an isolated Hub and fixture Agent."""
from pathlib import Path
import json
import re
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.util import atomic_json
from tests.test_ssh import PASSWORD, fake_transport


@pytest.fixture(scope='module')
def ui_stack(stack):
    stack.stop_agent()
    stack.config['shell'] = {'enabled':True,'projects':['Imago','Nexus','Lumen'],
                             'command':['/bin/sh','-c'],'env':{'PATH':fake_transport(stack.directory/'vps-ui-bin')}}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    return stack


@pytest.fixture(params=['chromium','webkit'])
def browser(request):
    with sync_playwright() as pw:
        value=getattr(pw,request.param).launch()
        yield value, request.param
        value.close()


def open_page(browser, stack, width=1440):
    page=browser.new_page(viewport={'width':width,'height':960 if width>500 else 844})
    page.goto(stack.url+'/#overview')
    page.fill('#password',stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('控制总览')
    if width <= 500:
        page.locator('.mobile-menu').click()
    entry=page.locator('.sidebar [data-nav="vps"]')
    expect(entry).to_be_visible()
    entry.click()
    expect(page.locator('#page h1')).to_have_text('VPS 管理')
    expect(entry).to_have_attribute('aria-current','page')
    if width <= 500:
        expect(page.locator('.sidebar')).not_to_have_class(re.compile(r'\bopen\b'))
    return page


def create_vps(stack, **changes):
    return stack.must(stack.client.post('/api/vps',json={
        'name':'VPS-'+uuid.uuid4().hex[:8], 'host':'vps.example.invalid',
        'port':10000+int(uuid.uuid4().hex[:4],16)%40000,'password':PASSWORD,
        'project_ids':[stack.project['id']],**changes}))


def update_data(v, **changes):
    keys=('name','host','port','username','host_key_policy','provider','region','system','notes','enabled','project_ids')
    return {**{k:v[k] for k in keys},'expected_version':v['version'],**changes}


@pytest.mark.parametrize('width',[1440,390])
def test_create_edit_search_and_responsive_layout(browser,ui_stack,width):
    engine,kind=browser;s=ui_stack;page=open_page(engine,s,width)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.locator('.page-head [data-vps-action="add"]').click()
        form=page.locator('#vps-form')
        name='面板测试-'+uuid.uuid4().hex[:8]
        form.locator('[name="name"]').fill(name)
        form.locator('[name="host"]').fill('vps.example.invalid')
        form.locator('[name="port"]').fill(str(10000+int(uuid.uuid4().hex[:4],16)%40000))
        form.locator('[name="password"]').fill(PASSWORD)
        for project in s.projects[:2]:form.locator(f'[name="project_ids"][value="{project["id"]}"]').check()
        form.locator('[name="host_key_policy"]').select_option('accept-new')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.click('#vps-save')
        expect(form).to_have_count(0)
        page.fill('#vps-query',name)
        card=page.locator('[data-vps-card]:visible')
        expect(card).to_have_count(1)
        expect(card).to_contain_text('Imago')
        expect(card).to_contain_text('Nexus')
        identifier=card.get_attribute('data-vps-card')
        saved=s.must(s.client.get('/api/vps/'+identifier))
        assert len(saved['project_ids'])==2
        # A normal held press must survive focus leaving the search field.
        card.locator('[data-vps-action="edit"]').click(delay=200)
        expect(page.locator('#vps-form [name="password"]')).to_have_value('')
        page.locator('#vps-form .vps-extra summary').click()
        page.locator('#vps-form [name="notes"]').fill('已保存配置，无需再次输入密码')
        page.click('#vps-save')
        expect(page.locator('#vps-form')).to_have_count(0)
        expect(page.locator('[data-vps-card]:visible')).to_contain_text('已保存配置')
        assert PASSWORD not in page.content()
        assert PASSWORD not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        directory=Path('.work/vps-feature/screenshots');directory.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(directory/f'{kind}-{width}.png'),full_page=True)
        page.fill('#vps-query','no-match-'+uuid.uuid4().hex)
        expect(page.locator('#vps-empty')).to_be_visible()
        assert not errors,errors
    finally:page.close()


def test_project_and_vps_assignment_both_directions(browser,ui_stack):
    engine,_=browser;s=ui_stack
    v=create_vps(s,project_ids=[p['id'] for p in s.projects[:2]])
    page=open_page(engine,s)
    try:
        page.fill('#vps-query',v['name'])
        page.locator('[data-vps-card]:visible [data-vps-action="assign"]').click()
        page.locator(f'.modal [name="project_ids"][value="{s.projects[1]["id"]}"]').uncheck()
        page.locator(f'.modal [name="project_ids"][value="{s.projects[2]["id"]}"]').check()
        page.click('#vps-assign-save')
        expect(page.locator('#vps-assign-save')).to_have_count(0)
        current=s.must(s.client.get('/api/vps/'+v['id']))
        assert set(current['project_ids'])=={s.projects[0]['id'],s.projects[2]['id']}
        page.locator('[data-nav="projects"]').first.click()
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.locator(f'#page [data-vps-action="project"][data-project="{s.projects[0]["id"]}"]').click()
        page.locator(f'.modal [name="vps_ids"][value="{v["id"]}"]').uncheck()
        page.click('#project-vps-save')
        expect(page.locator('#project-vps-save')).to_have_count(0)
        assert s.must(s.client.get('/api/vps/'+v['id']))['project_ids']==[s.projects[2]['id']]
    finally:page.close()


def test_check_connection_real_operation_once_and_no_secret(browser,ui_stack):
    engine,_=browser;s=ui_stack;v=create_vps(s)
    page=open_page(engine,s)
    submissions=[]
    def capture(request):
        if request.method=='POST' and request.url.endswith('/api/tools/call'):
            data=request.post_data_json
            if data.get('tool')=='vps_exec':submissions.append(data)
    page.on('request',capture)
    try:
        page.fill('#vps-query',v['name'])
        page.locator('[data-vps-card]:visible [data-vps-action="check"]').click()
        page.click('#vps-check-run')
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功',timeout=30000)
        expect(page.locator('#vps-check-output')).to_contain_text('CODEPIER_VPS_OK')
        # Completion unlocks an explicit new check; it must not automatically
        # replay the previous command or reuse its idempotency key (audit F20).
        button=page.locator('#vps-check-run')
        expect(button).to_be_enabled()
        expect(button).to_have_text('重新检查')
        expect(page.locator('#vps-check-project')).to_be_enabled()
        assert len(submissions)==1
        first_operation=re.findall(r'[a-f0-9]{32}',page.locator('#vps-check-state').inner_text())[-1]
        button.click()
        expect(page.locator('#vps-check-state')).not_to_contain_text(first_operation,timeout=30000)
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功',timeout=30000)
        expect(button).to_be_enabled()
        expect(button).to_have_text('重新检查')
        assert len(submissions)==2
        assert submissions[0]['arguments']['idempotency_key']!=submissions[1]['arguments']['idempotency_key']
        assert all('password' not in submission['arguments'] for submission in submissions)
        assert PASSWORD not in json.dumps(submissions) and PASSWORD not in page.content()
    finally:page.close()


def test_stale_edit_is_not_overwritten_and_text_is_escaped(browser,ui_stack):
    engine,_=browser;s=ui_stack
    v=create_vps(s,notes='<img src=x onerror=alert(1)>')
    page=open_page(engine,s)
    try:
        page.fill('#vps-query',v['name'])
        expect(page.locator('[data-vps-card]:visible .vps-notes')).to_have_text(v['notes'])
        expect(page.locator('[data-vps-card] img')).to_have_count(0)
        page.locator('[data-vps-card]:visible [data-vps-action="edit"]').click()
        current=s.must(s.client.get('/api/vps/'+v['id']))
        s.must(s.client.put('/api/vps/'+v['id'],json=update_data(current,notes='Concurrent update')))
        page.locator('#vps-form [name="notes"]').fill('Stale form')
        page.click('#vps-save')
        expect(page.locator('#vps-form-error')).to_contain_text('其他窗口')
        assert s.must(s.client.get('/api/vps/'+v['id']))['notes']=='Concurrent update'
    finally:page.close()


def test_background_refresh_preserves_held_click_target(browser,ui_stack):
    engine,_=browser;s=ui_stack;v=create_vps(s)
    page=open_page(engine,s,390)
    try:
        page.fill('#vps-query',v['name'])
        button=page.locator('[data-vps-card]:visible [data-vps-action="edit"]')
        page.evaluate('''() => {document.addEventListener('pointerdown',event=>{
          const button=event.target.closest('[data-vps-action="edit"]');
          if(!button)return;
          window.heldVpsButton=button;
          vpsInventory=async()=>S.vps;
          loadBasics=async()=>{};
          const original=uiWaitForPagePointer;
          uiWaitForPagePointer=()=>{
            document.body.dataset.refreshHeld=String(uiPagePointers.size);
            return original();
          };
          void renderPage(false).then(()=>{document.body.dataset.refreshFinished='true';});
        },{once:true,capture:true});}''')
        button.click(delay=200)
        expect(page.locator('body')).to_have_attribute('data-refresh-held','1')
        expect(page.locator('#vps-form')).to_be_visible()
        expect(page.locator('body')).to_have_attribute('data-refresh-finished','true')
        assert page.evaluate('heldVpsButton.isConnected')
        expect(page.locator('#vps-form [name="name"]')).to_have_value(v['name'])
    finally:page.close()


def test_background_refresh_does_not_discard_explicit_edit(browser,ui_stack):
    engine,_=browser;s=ui_stack;v=create_vps(s)
    page=open_page(engine,s);held=[]
    try:
        page.fill('#vps-query',v['name'])
        page.route('**/api/vps/'+v['id'],lambda route:held.append(route))
        page.locator('[data-vps-card]:visible [data-vps-action="edit"]').click()
        expect(page.locator('[data-vps-card]:visible')).to_have_count(1)
        page.wait_for_function('typeof vpsEdit === "function"')
        # Reproduce the list-render generation change while the user's edit read
        # is in flight; refreshing metadata must not cancel an explicit action.
        page.evaluate('renderPage(false)')
        assert held
        held.pop().fulfill(json=s.must(s.client.get('/api/vps/'+v['id'])))
        expect(page.locator('#vps-form')).to_be_visible()
        expect(page.locator('#vps-form [name="password"]')).to_have_value('')
    finally:
        for route in held:route.abort()
        page.unroute_all(behavior='wait');page.close()
