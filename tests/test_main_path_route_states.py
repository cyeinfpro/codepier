"""All registered routes against current assets and an isolated Hub.
The 200% case doubles each rendered text font, not OS/browser zoom or an
assistive-technology claim. Shared render faults are explicit UI injection;
they do not stand in for every domain's empty/permission state.
"""
import json
import pytest
from playwright.sync_api import expect
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture

ROUTES = ('overview','native','projects','workbench','workflows','collaboration',
          'artifacts','devices','vps','diagnostics','audit','connect','profiles',
          'roles','members','identity-admin','mcp-gateway','integrations','identity','settings')

@pytest.mark.parametrize("width", [320, 390, 1440])
def test_twenty_routes_text_200_percent_and_keyboard(stack, chat_browser_pool, width):
    _prepare_native_fixture(stack)
    context=chat_browser_pool("chromium").new_context(viewport={"width":width,"height":1000})
    page=context.new_page()
    errors=[]
    page.on("pageerror",lambda error: errors.append(str(error)))
    violations=[]
    try:
        _login(page,stack)
        for route in ROUTES:
            _navigate(page,route)
            page.evaluate("""() => {
              const nodes=[...document.querySelectorAll('#page *')];
              const sizes=nodes.map(el=>parseFloat(getComputedStyle(el).fontSize));
              nodes.forEach((el,i)=>{if(el.textContent.trim())el.style.fontSize=(sizes[i]*2)+'px'});
            }""")
            page.keyboard.press("Tab")
            focus=page.evaluate("""() => {
              const el=document.activeElement;return {tag:el.tagName,hidden:el.closest('[inert]')!==null}
            }""")
            assert not focus["hidden"],route
            overflow=page.evaluate("document.documentElement.scrollWidth-innerWidth")
            if overflow>1:
                offenders=page.evaluate("""() => [...document.querySelectorAll('#page *')].filter(el=>{
                  const r=el.getBoundingClientRect();return r.width>0&&r.right>innerWidth+1
                }).slice(0,12).map(el=>({tag:el.tagName,id:el.id,text:el.textContent,html:el.outerHTML,cls:el.className,width:el.getBoundingClientRect().width,right:el.getBoundingClientRect().right}))""")
                violations.append((route,overflow,offenders))
        assert not violations, json.dumps(violations,ensure_ascii=False)
        assert not errors, errors
    finally:
        context.close()

def test_current_route_shared_error_retry_and_late_completion(stack, chat_browser_pool):
    context=chat_browser_pool("chromium").new_context()
    page=context.new_page()
    try:
        _login(page,stack)
        # Domain-specific errors remain tested by their owners. This checks the
        # shared current-page error/retry and generation barrier on 19 shell routes.
        for route in ROUTES:
            if route == "native":
                continue  # Native owns its chat loader; separate chat suite covers it.
            _navigate(page,route)
            page.evaluate("""() => {
              window.originalAssetLoad=CodePierAssets;
              window.CodePierAssets={...originalAssetLoad,load:async()=>{throw new Error('isolated render fault')}};
              return renderPage();
            }""")
            expect(page.locator("#page")).to_contain_text("isolated render fault")
            page.evaluate("window.CodePierAssets=originalAssetLoad")
            page.locator('#page [data-action="refresh"]').click()
            expect(page.locator("#page")).not_to_contain_text("isolated render fault")
        page.evaluate("""() => {
          window.originalAssetLoad=CodePierAssets;
          window.CodePierAssets={...originalAssetLoad,load:()=>new Promise(resolve=>window.releaseOldPage=resolve)};
          window.pendingOldRender=navigate('projects');
        }""")
        expect(page.locator("#page")).to_have_attribute("aria-busy","true")
        page.evaluate("window.CodePierAssets=originalAssetLoad")
        _navigate(page,"overview")
        page.evaluate("releaseOldPage();pendingOldRender")
        expect(page.locator("#page")).to_have_attribute("data-page","overview")
    finally:
        context.close()

def test_two_tabs_share_browser_appearance_and_project_layout_preference(stack, chat_browser_pool):
    context=chat_browser_pool("chromium").new_context()
    first=context.new_page()
    second=context.new_page()
    try:
        _login(first,stack)
        second.goto(stack.url)
        expect(second.locator(".sidebar")).to_be_visible()
        first.evaluate("CodePierAppearance.setPreference('dark')")
        expect(second.locator("html")).to_have_attribute("data-appearance","dark")
        second.reload()
        expect(second.locator("html")).to_have_attribute("data-appearance","dark")
        _navigate(first,"projects")
        first.locator('[data-project-layout="list"]').click()
        first.reload()
        expect(first.locator(".workspace-project-collection")).to_have_attribute("data-layout","list")
        # The preference is browser-local, never a project/server write.
        assert first.evaluate("localStorage.getItem('codepier-project-layout')") == "list"
    finally:
        context.close()

def test_long_unicode_project_path_is_not_markup_and_copies_complete_value(stack, chat_browser_pool):
    context=chat_browser_pool("chromium").new_context(viewport={"width":320,"height":844})
    page=context.new_page()
    path="/" + ("中文-abc-é-🧪/" * 24) + "<safe>&folder"
    try:
        _login(page,stack)
        _navigate(page,"projects")
        page.evaluate("""path => {
          S.projects[0].root=path;
          document.querySelector('#page').innerHTML=projectsHTML();
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copiedPath=value}}});
        }""",path)
        button=page.locator(".workspace-copy-path").first
        expect(button).to_have_attribute("data-copy",path)
        expect(page.locator(".workspace-project-path safe")).to_have_count(0)
        button.click()
        page.wait_for_function("window.copiedPath !== undefined")
        assert page.evaluate("copiedPath") == path
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    finally:
        context.close()

from tests.test_panel_session_and_editor import app_page, expire, login

def test_return_context_does_not_survive_new_account_or_space(app_page):
    page=app_page
    page.evaluate("""() => {
      const context=mainPathContext();context.work=true;context.native=true;
      context.previous='workbench';context.pages.set('workbench',{scroll:123});
    }""")
    expire(page)
    page.evaluate("nextLogin={user_id:'different-user',username:'admin',csrf:'new-fixture'}")
    login(page)
    assert page.evaluate("mainPathContext().pages.size") == 0
    assert not page.evaluate("mainPathContext().work || mainPathContext().native")
    page.evaluate("""() => {
      mainPathContext().work=true;mainPathContext().pages.set('workbench',{scroll:123});
      S.space_id='different-space';
    }""")
    assert page.evaluate("mainPathContext().pages.size") == 0
    assert not page.evaluate("mainPathContext().work")


@pytest.mark.parametrize('width', [320, 390])
def test_mobile_topbar_space_and_overview_cli_permission(stack, chat_browser_pool, width):
    context = chat_browser_pool('chromium').new_context(viewport={'width':width,'height':844})
    page = context.new_page()
    try:
        _login(page, stack)
        _navigate(page, 'overview')
        label = page.evaluate("CodePierIdentity.current()?.label || '空间待核实'")
        expect(page.locator('.topbar-space')).to_be_visible()
        expect(page.locator('.topbar-space')).to_have_text(label)
        expect(page.locator('.topbar-space')).to_have_attribute('title', label)
        expect(page.locator('.topbar-space')).to_have_attribute('aria-label', '当前空间：' + label)
        page.evaluate("""() => {
          window.savedCanNavigate=CodePierIdentity.canNavigate;
          CodePierIdentity.canNavigate=page=>page==='native'?false:savedCanNavigate(page);
          document.querySelector('#page').innerHTML=overviewHTML();
        }""")
        expect(page.locator('.overview-continue [data-nav="native"]')).to_have_count(0)
        page.evaluate("""() => {
          CodePierIdentity.canNavigate=savedCanNavigate;
          document.querySelector('#page').innerHTML=overviewHTML();
        }""")
        expect(page.locator('.overview-continue')).to_have_count(0)
        page.evaluate("S.space_id='unavailable-space';renderShell()")
        expect(page.locator('.topbar-space')).to_be_visible()
        expect(page.locator('.topbar-space')).to_have_text('空间待核实')
        expect(page.locator('.topbar-space')).not_to_contain_text(label)
    finally:
        context.close()


def test_default_common_settings_are_real_visible_cards(stack, chat_browser_pool):
    context = chat_browser_pool('chromium').new_context(viewport={'width':390,'height':844})
    page = context.new_page()
    try:
        _login(page, stack)
        _navigate(page, 'settings')
        expect(page.locator('.settings-groups [data-settings-group=""]')).to_have_attribute('aria-pressed', 'true')
        for item in ('device', 'project_mapping', 'appearance', 'account_security'):
            expect(page.locator('[data-setting-id="' + item + '"]')).to_be_visible()
        assert page.locator('.settings-common [data-setting-id]:visible').count() >= 4
        page.locator('.settings-groups [data-settings-group="files"]').click()
        expect(page.locator('[data-setting-id="appearance"]')).to_be_hidden()
        page.locator('.settings-groups [data-settings-group=""]').click()
        expect(page.locator('[data-setting-id="appearance"]')).to_be_visible()
    finally:
        context.close()


def test_tree_states_and_late_project_readback(app_page):
    page = app_page
    result = page.evaluate("""async () => {
      const originalFetch=window.fetch;
      const response=value=>new Response(JSON.stringify(value),{status:200,headers:{"Content-Type":"application/json"}});
      const visible=()=>new DOMParser().parseFromString(treeHTML(),'text/html').body.textContent;
      const state={};
      try {
        S.work.tree=[];S.work.project='';state.unselected=visible();
        S.work.project='one';window.fetch=()=>new Promise(resolve=>window.resolveTree=value=>resolve(response(value)));
        const pending=loadTree();state.loading=visible();
        resolveTree({entries:[],next_offset:null});await pending;state.empty=visible();
        window.fetch=()=>Promise.reject(new Error('fixture offline'));
        await loadTree().catch(()=>{});state.error=visible();
        window.fetch=()=>new Promise(resolve=>window.resolveTree=value=>resolve(response(value)));
        const old=loadTree();S.work.project='two';S.work.tree=[{type:'file',path:'new.txt',name:'new.txt'}];
        resolveTree({entries:[{type:'file',path:'old.txt',name:'old.txt'}],next_offset:null});await old;
        state.paths=S.work.tree.map(x=>x.path);
        return state;
      } finally { window.fetch=originalFetch; }
    }""")
    assert '选择项目' in result['unselected']
    assert '正在读取' in result['loading']
    assert '此目录为空' in result['empty']
    assert '目录读取失败' in result['error']
    assert '网络暂时不可用' in result['error']
    assert result['paths'] == ['new.txt']


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width,height,scale', [(390,844,1),(1440,600,1),(390,600,2)])
def test_sidebar_native_select_alignment_and_reachability(stack, chat_browser_pool, width, height, scale, theme):
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':height})
    page=context.new_page()
    try:
        _login(page,stack)
        if width < 901:
            page.locator('.mobile-menu').click()
        page.evaluate('theme=>CodePierAppearance.setPreference(theme)',theme)
        space=page.locator('#iam-active-space')
        appearance=page.locator('.sidebar [data-ui-appearance]')
        expect(space).to_be_visible()
        expect(appearance).to_be_visible()
        assert '主理人' not in space.locator('option:checked').inner_text()
        expect(page.locator('.sidebar-account-role')).to_contain_text('空间角色')
        page.evaluate("""scale=>{
          document.querySelector('#iam-active-space option:checked').textContent='这是一个需要完整保留的非常长的空间名称 / Long workspace name';
          if(scale===2){const es=[...document.querySelectorAll('.sidebar *')];const sizes=es.map(e=>getComputedStyle(e).fontSize);es.forEach((e,i)=>e.style.fontSize=(parseFloat(sizes[i])*scale)+'px');}
        }""",scale)
        boxes=page.evaluate("""()=>['#iam-active-space','.sidebar [data-ui-appearance]'].map(s=>{const e=document.querySelector(s),r=e.getBoundingClientRect();return {x:r.x,width:r.width,height:r.height,radius:getComputedStyle(e).borderRadius}})""")
        assert abs(boxes[0]['x']-boxes[1]['x']) <= 1
        assert abs(boxes[0]['width']-boxes[1]['width']) <= 1
        assert abs(boxes[0]['height']-boxes[1]['height']) <= 1
        assert boxes[0]['height'] >= 44
        assert boxes[0]['radius'] == boxes[1]['radius']
        appearance.scroll_into_view_if_needed()
        assert appearance.locator('option').all_text_contents() == ['跟随系统','浅色','深色']
        appearance.click()
        # Browser page screenshots cannot promise capture of the OS picker.
        # Save the actual browser result without compositing a replacement menu.
        from pathlib import Path
        evidence=Path('.work/sidebar-arrows04-stress');evidence.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(evidence / f'{theme}-{width}-{height}-{scale}-picker.png'))
        page.keyboard.press('Escape')
        appearance.select_option('dark')
        expect(page.locator('html')).to_have_attribute('data-appearance','dark')
        appearance.focus()
        page.keyboard.press('ArrowUp')
        page.keyboard.press('ArrowUp')
        page.keyboard.press('Enter')
        keyboard_value=appearance.input_value()
        (evidence / f'{theme}-{width}-{height}-{scale}-keyboard.txt').write_text('ArrowUp twice + Enter observed value: '+keyboard_value+'; native popup keyboard selection is not claimed as passed.')
        appearance.select_option('auto')
        expect(appearance).to_have_value('auto')
        logout=page.locator('.sidebar [data-action="logout"]')
        logout.scroll_into_view_if_needed()
        box=logout.bounding_box()
        assert box and box['y'] >= 0 and box['y']+box['height'] <= height+1
        assert page.evaluate("document.querySelector('.sidebar').scrollWidth <= document.querySelector('.sidebar').clientWidth+1")
    finally:
        context.close()


@pytest.mark.parametrize('width,height,scale', [(1440,1000,1),(390,844,1),(390,600,2)])
def test_sidebar_controls03_delivery_views(stack, chat_browser_pool, width, height, scale):
    from pathlib import Path
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':height})
    page=context.new_page()
    try:
        _login(page,stack)
        page.evaluate("CodePierAppearance.setPreference('light')")
        if width < 901:
            page.locator('.mobile-menu').click()
        if scale == 2:
            page.evaluate("""()=>{const es=[...document.querySelectorAll('.sidebar *')];const sizes=es.map(e=>getComputedStyle(e).fontSize);es.forEach((e,i)=>e.style.fontSize=(parseFloat(sizes[i])*2)+'px')}""")
        space=page.locator('#iam-active-space')
        expect(space).to_be_visible()
        assert '非常长' not in space.locator('option:checked').inner_text()
        logout=page.locator('.sidebar [data-action="logout"]')
        logout.scroll_into_view_if_needed()
        role=page.locator('.sidebar-account-role')
        expect(role).to_be_visible()
        for loc in [role,logout]:
            box=loc.bounding_box()
            assert box and box['y']>=0 and box['y']+box['height']<=height+1
        evidence=Path('.work/sidebar-arrows04-delivery');evidence.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(evidence/f'light-{width}-{height}-{scale}-account-bottom.png'))
    finally:
        context.close()


@pytest.mark.parametrize('width', [390,1440])
def test_sidebar_select_indicator_inset_and_accessibility_modes(stack, chat_browser_pool, width):
    from pathlib import Path
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':1000 if width==1440 else 844})
    page=context.new_page()
    try:
        _login(page,stack)
        if width<901: page.locator('.mobile-menu').click()
        evidence=Path('.work/sidebar-arrows04');evidence.mkdir(parents=True,exist_ok=True)
        for theme in ['light','dark']:
            page.evaluate('t=>CodePierAppearance.setPreference(t)',theme)
            for selector in ['#iam-active-space','.sidebar [data-ui-appearance]']:
                loc=page.locator(selector)
                loc.scroll_into_view_if_needed()
                loc.focus()
                styles=loc.evaluate("e=>{const s=getComputedStyle(e);return {appearance:s.appearance,image:s.backgroundImage,position:s.backgroundPosition,size:s.backgroundSize,padding:s.paddingRight,repeat:s.backgroundRepeat,height:e.getBoundingClientRect().height}}")
                assert styles['appearance']=='none'
                assert styles['image'].startswith('url(') and styles['image'].count('url(')==1
                assert styles['padding']=='44px'
                assert '14px' in styles['position'] and '50%' in styles['position']
                assert styles['size'] in ['16px', '16px auto'] and styles['repeat']=='no-repeat'
                assert styles['height']>=44
            page.screenshot(path=str(evidence/f'{theme}-{width}-focused.png'))
        space=page.locator('#iam-active-space')
        space.evaluate("e=>{e.options[e.selectedIndex].textContent='这是一个很长的空间名称 Long workspace name that must not touch the arrow';e.disabled=true}")
        assert space.evaluate('e=>getComputedStyle(e).paddingRight')=='44px'
        assert space.evaluate('e=>getComputedStyle(e).backgroundImage').startswith('url(')
        page.screenshot(path=str(evidence/f'dark-{width}-long-disabled.png'))
        # Representative native listbox probes: these are not app business controls.
        page.evaluate("""()=>{for(const attr of ['multiple','size']){const e=document.createElement('select');e.id='arrow-probe-'+attr;e.setAttribute(attr,attr==='size'?'4':'');e.innerHTML='<option>one</option><option>two</option>';document.body.append(e)}}""")
        for kind in ['multiple','size']:
            assert page.locator('#arrow-probe-'+kind).evaluate('e=>getComputedStyle(e).backgroundImage')=='none'
        page.emulate_media(forced_colors='active')
        for selector in ['#iam-active-space','.sidebar [data-ui-appearance]']:
            assert page.locator(selector).evaluate('e=>getComputedStyle(e).appearance')=='auto'
            assert page.locator(selector).evaluate('e=>getComputedStyle(e).backgroundImage')=='none'
        page.screenshot(path=str(evidence/f'forced-colors-{width}.png'))
    finally:
        context.close()
