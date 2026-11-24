"""Real Chromium/WebKit panel flows against an isolated Hub and fixture Agent."""
import asyncio
from concurrent.futures import Future
from pathlib import Path
from threading import Thread
import json
import re
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.util import atomic_json
from tests.browser_launch import launch_browser
from tests.test_ssh import PASSWORD
from tests.test_vps_ssh_v2_exec import _make_exec_server


@pytest.fixture(scope='module')
def ui_stack(stack):
    stack.stop_agent()
    stack.config['shell'] = {'enabled':True,'projects':['*','ProjectAlpha','ProjectGamma','ProjectBeta'],
                             'command':['/bin/sh','-c']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    return stack


@pytest.fixture
def managed_probe_peer():
    ssh = pytest.importorskip("asyncssh")
    started = Future()
    commands = []

    async def handle(channel, _username, command):
        commands.append(command)
        if not command.startswith("printf 'CODEPIER_VPS_OK"):
            channel.exit(127)
            return
        channel.write(f"CODEPIER_VPS_OK\nfixture-node\nroot\n{PASSWORD}\n".encode())
        channel.exit(0)

    async def serve():
        stop = asyncio.Event()
        key = ssh.generate_private_key("ssh-ed25519")
        peer = _make_exec_server(ssh, users={"root": PASSWORD}, handler=handle)
        server = await ssh.create_server(
            peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
        )
        started.set_result((server.get_port(), asyncio.get_running_loop(), stop))
        try:
            await stop.wait()
        finally:
            server.close()
            await server.wait_closed()

    def run():
        try:
            asyncio.run(serve())
        except BaseException as error:
            if started.done():
                raise
            started.set_exception(error)

    worker = Thread(target=run, daemon=True)
    worker.start()
    port, loop, stop = started.result(timeout=15)
    try:
        yield {"host": "127.0.0.1", "port": port}, commands
    finally:
        loop.call_soon_threadsafe(stop.set)
        worker.join(15)
        assert not worker.is_alive()


@pytest.fixture(params=['chromium','webkit'])
def browser(request):
    with sync_playwright() as pw:
        value=launch_browser(getattr(pw,request.param))
        yield value, request.param
        value.close()


def open_page(browser, stack, width=1440):
    page=browser.new_page(viewport={'width':width,'height':960 if width>500 else 844})
    page.goto(stack.url+'/#overview')
    page.fill('#username', 'admin');page.fill('#password',stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('总览',timeout=15000)
    if width <= 500:
        page.locator('.mobile-menu').click()
    entry=page.locator('.sidebar [data-nav="vps"]')
    expect(entry).to_be_visible()
    entry.click()
    expect(page.locator('#page h1')).to_have_text('VPS')
    expect(entry).to_have_attribute('aria-current','page')
    if width <= 500:
        expect(page.locator('.sidebar')).not_to_have_class(re.compile(r'\bopen\b'))
    return page


def create_vps(stack, **changes):
    return stack.must(stack.client.post('/api/vps',json={
        'name':'VPS-'+uuid.uuid4().hex[:8], 'host':'vps.example.invalid',
        'port':10000+int(uuid.uuid4().hex[:4],16)%40000,'password':PASSWORD,
        'project_ids':[stack.project['id']],**changes}))


def open_details(page):
    more=page.locator('[data-vps-card]:visible .vps-card-more')
    if more.get_attribute('open') is None:
        more.locator('summary').click()


def update_data(v, **changes):
    keys=('name','host','port','username','provider','region','system','notes','enabled','project_ids')
    return {**{k:v[k] for k in keys if k in v},'expected_version':v['version'],**changes}


@pytest.mark.parametrize('width',[1440,390])
def test_create_edit_search_and_responsive_layout(browser,ui_stack,width,managed_probe_peer):
    endpoint, commands = managed_probe_peer
    engine,kind=browser;s=ui_stack;page=open_page(engine,s,width)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.locator('.page-head [data-vps-action="add"]').click()
        form=page.locator('#vps-form')
        name='面板测试-'+uuid.uuid4().hex[:8]
        form.locator('[name="name"]').fill(name)
        form.locator('[name="host"]').fill(endpoint['host'])
        form.locator('[name="port"]').fill(str(endpoint['port']))
        form.locator('[name="password"]').fill(PASSWORD)
        for project in s.projects[:2]:form.locator(f'[name="project_ids"][value="{project["id"]}"]').check()
        expect(form.locator('[name="host_key_policy"], [name="host_key_fingerprint"]')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.click('#vps-save')
        expect(form).to_have_count(0)
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功', timeout=30000)
        page.locator('.modal-footer [data-action="close-modal"]').click()
        page.fill('#vps-query',name)
        card=page.locator('[data-vps-card]:visible')
        expect(card).to_have_count(1)
        expect(card).to_contain_text('ProjectAlpha')
        expect(card).to_contain_text('ProjectGamma')
        identifier=card.get_attribute('data-vps-card')
        saved=s.must(s.client.get('/api/vps/'+identifier))
        assert len(saved['project_ids'])==2
        # A normal held press must survive focus leaving the search field.
        open_details(page)
        card.locator('[data-vps-action="edit"]').click(delay=200)
        expect(page.locator('#vps-form [name="password"]')).to_have_value('')
        page.locator('#vps-form .vps-extra summary').click()
        page.locator('#vps-form [name="notes"]').fill('已保存配置，无需再次输入密码')
        page.click('#vps-save')
        expect(page.locator('#vps-form')).to_have_count(0)
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功', timeout=30000)
        page.locator('.modal-footer [data-action="close-modal"]').click()
        expect(page.locator('[data-vps-card]:visible .vps-notes')).to_have_text('已保存配置，无需再次输入密码')
        assert PASSWORD not in page.content()
        assert PASSWORD not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        directory=Path('.work/vps-feature/screenshots');directory.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(directory/f'{kind}-{width}.png'),full_page=True)
        page.fill('#vps-query','no-match-'+uuid.uuid4().hex)
        expect(page.locator('#vps-empty')).to_be_visible()
        assert not errors,errors
        assert len(commands) == 2
    finally:page.close()


def test_project_and_vps_assignment_both_directions(browser,ui_stack):
    engine,_=browser;s=ui_stack
    v=create_vps(s,project_ids=[p['id'] for p in s.projects[:2]])
    page=open_page(engine,s)
    try:
        page.fill('#vps-query',v['name'])
        expect(page.locator('[data-vps-card]:visible [data-vps-action="assign"]')).to_have_count(0)
        open_details(page)
        page.locator('[data-vps-card]:visible [data-vps-action="edit"]').click()
        expect(page.locator('#vps-form [name="host_key_policy"], #vps-form [name="host_key_fingerprint"]')).to_have_count(0)
        page.locator(f'.modal [name="project_ids"][value="{s.projects[1]["id"]}"]').uncheck()
        page.locator(f'.modal [name="project_ids"][value="{s.projects[2]["id"]}"]').check()
        page.click('#vps-save')
        expect(page.locator('#vps-check-state')).to_be_visible()
        page.locator('.modal-footer [data-action="close-modal"]').click()
        expect(page.locator('#vps-check-state')).to_have_count(0)
        current=s.must(s.client.get('/api/vps/'+v['id']))
        assert set(current['project_ids'])=={s.projects[0]['id'],s.projects[2]['id']}
        page.locator('[data-nav="projects"]').first.click()
        expect(page.locator('#page h1')).to_have_text('项目')
        assignment = page.locator(f'#page [data-vps-action="project"][data-project="{s.projects[0]["id"]}"]')
        assignment.locator('xpath=ancestor::details').locator('summary').click()
        assignment.click()
        page.locator(f'.modal [name="vps_ids"][value="{v["id"]}"]').uncheck()
        page.click('#project-vps-save')
        expect(page.locator('#project-vps-save')).to_have_count(0)
        assert s.must(s.client.get('/api/vps/'+v['id']))['project_ids']==[s.projects[2]['id']]
    finally:page.close()


def test_check_connection_real_operation_once_and_no_secret(browser,ui_stack,managed_probe_peer):
    endpoint, commands = managed_probe_peer
    engine,_=browser;s=ui_stack;v=create_vps(s, **endpoint)
    page=open_page(engine,s)
    submissions=[]
    def capture(request):
        if request.method=='POST' and request.url.endswith('/api/vps/'+v['id']+'/check'):
            submissions.append(request.post_data_json)
    page.on('request',capture)
    try:
        page.fill('#vps-query',v['name'])
        open_details(page)
        page.locator('[data-vps-card]:visible [data-vps-action="check"]').click()
        page.click('#vps-check-run')
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功',timeout=30000)
        expect(page.locator('#vps-check-output')).to_contain_text('CODEPIER_VPS_OK')
        # Completion unlocks an explicit new check; it must not automatically
        # replay the previous command or reuse its idempotency key (audit F20).
        button=page.locator('#vps-check-run')
        expect(button).to_be_enabled()
        expect(button).to_have_text('重新检查')
        expect(page.locator('#vps-check-project')).to_have_count(0)
        assert len(submissions)==1
        assert len(commands) == 1
        assert not re.search(r'[a-f0-9]{32}', page.locator('#vps-check-state').inner_text())
        button.click()
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功',timeout=30000)
        expect(button).to_be_enabled()
        expect(button).to_have_text('重新检查')
        assert len(submissions)==2
        assert len(commands) == 2
        assert submissions[0]['idempotency_key']!=submissions[1]['idempotency_key']
        assert all('password' not in submission for submission in submissions)
        assert PASSWORD not in json.dumps(submissions) and PASSWORD not in page.content()
    finally:page.close()


def test_layout_toggle_copy_ssh_and_unassigned_vps_check(browser,ui_stack):
    engine,_=browser;s=ui_stack
    system_tag='Debian13-'+uuid.uuid4().hex[:6]
    v=create_vps(s,project_ids=[],username='ubuntu',region='东京',system=system_tag)
    page=open_page(engine,s,1440)
    direct_checks=[]
    def handle_direct_check(route):
        direct_checks.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type='application/json',
            body=json.dumps({
                'state':'succeeded',
                'pending':False,
                'output':'CODEPIER_VPS_OK\nunassigned-vps\n',
                'error':None,
            }),
        )
    page.route('**/api/vps/'+v['id']+'/check',handle_direct_check)
    try:
        expect(page.locator('.vps-summary [data-vps-stat="total"]')).to_be_visible()
        expect(page.locator('.vps-summary [data-vps-stat="ready"]')).to_be_visible()
        expect(page.locator('.vps-summary [data-vps-stat="sessions"]')).to_be_visible()
        expect(page.locator('.vps-summary [data-vps-stat="projects"]')).to_be_visible()
        # Full-field search matches system/username/region as well as name.
        page.fill('#vps-query',system_tag)
        card=page.locator('[data-vps-card]:visible')
        expect(card).to_have_count(1)
        open_details(page)
        expect(card.locator('.vps-address code')).to_have_text(f"{v['username']}@{v['host']}:{v['port']}")
        expect(card).to_contain_text('未关联（仅人工终端）')
        expect(card.locator('[data-vps-action="assign"]')).to_have_count(0)
        expect(card.locator('[data-vps-action="check"]')).to_be_enabled()
        expect(card.locator('[data-vps-action="terminal"]')).to_be_enabled()

        # Cards / compact list view toggle + localStorage persistence.
        list_tab=page.locator('[data-vps-layout="list"]')
        cards_tab=page.locator('[data-vps-layout="cards"]')
        list_tab.click()
        expect(page.locator('.vps-grid')).to_have_attribute('data-layout','list')
        expect(list_tab).to_have_attribute('aria-pressed','true')
        assert page.evaluate("localStorage.getItem('codepier-vps-layout')")=='list'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        cards_tab.click()
        expect(page.locator('.vps-grid')).to_have_attribute('data-layout','cards')
        expect(cards_tab).to_have_attribute('aria-pressed','true')

        # Copy full SSH command action and feedback toast.
        page.evaluate('''() => {
          window.__copiedSsh = null;
          const prev = window.copy;
          window.copy = async (text) => {
            window.__copiedSsh = text;
            try { return await prev(text); } catch {}
          };
        }''')
        card.locator('[data-vps-action="copy-ssh"]').click()
        assert page.evaluate('window.__copiedSsh')==f"ssh -p {v['port']} {v['username']}@{v['host']}"
        expect(page.get_by_text('已复制 SSH 命令').first).to_be_visible()

        # Unassigned VPS connection check without project picker.
        open_details(page)
        card.locator('[data-vps-action="check"]').click()
        expect(page.locator('#vps-check-project')).to_have_count(0)
        expect(page.locator('[data-vps-action="check-terminal"]')).to_be_enabled()
        run_btn=page.locator('#vps-check-run')
        expect(run_btn).to_be_enabled()
        run_btn.click()
        expect(page.locator('#vps-check-state')).to_contain_text('连接检查成功')
        expect(page.locator('#vps-check-output')).to_contain_text('CODEPIER_VPS_OK')
        expect(run_btn).to_have_text('重新检查')
        assert len(direct_checks)==1
        assert direct_checks[0]['timeout_seconds']==30
        assert direct_checks[0]['idempotency_key'].startswith('vps-check-')
        page.locator('.modal-footer [data-action="close-modal"]').click()

        # Command palette exposes loaded VPS entries searchable by name/region.
        page.evaluate('uiCommand()')
        expect(page.locator('#command-query')).to_be_visible()
        page.fill('#command-query',v['name'])
        expect(page.locator('#command-results')).to_contain_text(v['name'])
        page.keyboard.press('Escape')
    finally:
        page.unroute_all(behavior='wait')
        page.close()


def test_stale_edit_is_not_overwritten_and_text_is_escaped(browser,ui_stack):
    engine,_=browser;s=ui_stack
    v=create_vps(s,notes='<img src=x onerror=alert(1)>')
    page=open_page(engine,s)
    try:
        page.fill('#vps-query',v['name'])
        open_details(page)
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
        open_details(page)
        button=page.locator('[data-vps-card]:visible [data-vps-action="edit"]')
        page.evaluate('''async () => {
          const first=await api('/api/vps?limit=200&offset=0');
          document.addEventListener('pointerdown',event=>{
          const button=event.target.closest('[data-vps-action="edit"]');
          if(!button)return;
          window.heldVpsButton=button;
          window.heldVpsDetachCount=0;
          const card=button.closest('[data-vps-card]');
          new MutationObserver(records=>{
            if(records.some(record=>[...record.removedNodes].some(node=>node===card||node.contains?.(card))))
              window.heldVpsDetachCount++;
          }).observe(document.querySelector('#page'),{childList:true,subtree:true});
          vpsInventory=async(observeFirstPage)=>{
            observeFirstPage?.(first);
            return S.vps;
          };
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
        assert page.evaluate('heldVpsDetachCount')==0
        expect(page.locator('#vps-form [name="name"]')).to_have_value(v['name'])
    finally:page.close()


def test_returning_mobile_dock_preserves_held_page_click(browser,ui_stack):
    engine,_=browser;s=ui_stack;v=create_vps(s)
    page=open_page(engine,s,390)
    try:
        page.fill('#vps-query',v['name'])
        open_details(page)
        page.evaluate('''() => {
          const dock=document.querySelector('.mobile-dock');
          dock.style.cssText='visibility:hidden;opacity:0;pointer-events:none;transform:translateY(100px)';
          const button=document.querySelector('[data-vps-card]:not([hidden]) [data-vps-action="edit"]');
          const spacer=document.createElement('div');
          spacer.style.height='400px';
          button.closest('[data-vps-card]').before(spacer);
          button.style.scrollMarginBlock='0';
          button.scrollIntoView({block:'end'});
          button.addEventListener('pointerdown',()=>{
            dock.removeAttribute('style');
          },{once:true});
        }''')
        expect(page.locator('.mobile-dock')).to_have_css('opacity','0')
        open_details(page)
        button=page.locator('[data-vps-card]:visible [data-vps-action="edit"]')
        bounds=button.bounding_box()
        assert bounds and bounds['y']+bounds['height']/2>800
        page.mouse.move(bounds['x']+bounds['width']/2,bounds['y']+bounds['height']/2)
        page.mouse.down()
        expect(page.locator('.mobile-dock')).to_have_css('transform','none')
        page.mouse.up()
        expect(page.locator('#vps-form')).to_be_visible()
        expect(page.locator('#vps-form [name="name"]')).to_have_value(v['name'])
        expect(page.locator('body')).not_to_have_class(re.compile(r'\bui-page-press\b'))
    finally:page.close()


def test_background_refresh_does_not_discard_explicit_edit(browser,ui_stack):
    engine,_=browser;s=ui_stack;v=create_vps(s)
    page=open_page(engine,s);held=[]
    try:
        page.fill('#vps-query',v['name'])
        page.route('**/api/vps/'+v['id'],lambda route:held.append(route))
        open_details(page)
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


def test_pin_reorders_compact_vps_without_losing_filter_or_opening_ssh(browser,ui_stack):
    engine,_=browser;s=ui_stack
    token='Pin'+uuid.uuid4().hex[:8]
    a=create_vps(s,name=token+' Alpha',system='Hidden OS')
    b=create_vps(s,name=token+' Beta',system='Hidden OS')
    page=open_page(engine,s,320)
    try:
        page.fill('#vps-query',token)
        cards=page.locator('[data-vps-card]:visible')
        expect(cards).to_have_count(2)
        target=cards.last
        pinned_id=target.get_attribute('data-vps-card')
        assert pinned_id in (a['id'],b['id'])
        pin=target.locator('.vps-pin')
        expect(target.get_by_role('group',name='关联项目')).to_contain_text('ProjectAlpha')
        expect(target.locator('.vps-address')).not_to_be_visible()
        assert 'Hidden OS' not in target.inner_text()
        assert '密钥策略' not in target.inner_text()
        assert '连接通道' not in target.inner_text()
        pin.focus()
        pin.press('Space')
        expect(cards.first).to_have_attribute('data-vps-card',pinned_id)
        expect(cards.first.locator('.vps-pin')).to_have_attribute('aria-pressed','true')
        expect(cards.first.locator('.vps-pin')).to_be_focused()
        expect(page.locator('#vps-query')).to_have_value(token)
        expect(page.locator('[data-vps-terminal-inline]:visible')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.reload()
        expect(page.locator('#vps-query')).to_be_visible()
        page.fill('#vps-query',token)
        expect(cards.first).to_have_attribute('data-vps-card',pinned_id)
        cards.first.locator('.vps-pin').click()
        expect(cards.last).to_have_attribute('data-vps-card',pinned_id)
        expect(cards.last.locator('.vps-pin')).to_have_attribute('aria-pressed','false')
    finally:page.close()
