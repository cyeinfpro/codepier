"""Desktop density and paired text colors, on isolated Hub/Agent fixtures."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from playwright.sync_api import expect, sync_playwright
from scripts.ui_comfort_audit import MEASURE
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture, _set_scheme
from tests.browser_support import chat_page

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/evidence/ui-comfort-20260916/verification'
OUT.mkdir(parents=True,exist_ok=True)


def contrast(foreground,background):
    def luminance(value):
        rgb=[int(value.lstrip('#')[i:i+2],16)/255 for i in (0,2,4)]
        linear=[x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4 for x in rgb]
        return sum(a*b for a,b in zip(linear,(.2126,.7152,.0722)))
    a,b=sorted([luminance(foreground),luminance(background)])
    return (b+.05)/(a+.05)


def within(page,selector,width,height):
    for node in page.locator(selector).all():
        if not node.is_visible():
            continue
        box=node.bounding_box()
        assert box and box['x']>=-1 and box['x']+box['width']<=width+1,(selector,box)
        assert box['y']>=-1 and box['y']+box['height']<=height+1,(selector,box)


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('scheme',['light','dark'])
@pytest.mark.parametrize('width,height',[(1280,720),(1366,768),(1440,900),(1180,640)])
def test_desktop_essentials_fit_and_text_is_readable(stack,engine,scheme,width,height):
    _prepare_native_fixture(stack)
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch()
        page=browser.new_page(viewport={'width':width,'height':height},color_scheme=scheme,reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        _login(page,stack);_set_scheme(page,scheme)
        measurements=[]
        for route in ['overview','projects','devices','workbench','settings','native']:
            _navigate(page,route)
            result=page.evaluate(MEASURE)
            assert result['documentWidth']<=width+1,(route,result['documentWidth'])
            assert not result['failures'],(route,scheme,result['failures'])
            if route!='native':
                within(page,'.side-bottom',width,height)
                within(page,'.page-head',width,height)
                assert result['boxes']['.page-head'][0]['bottom']<=160
            if route=='overview':
                within(page,'.stats,.codepier-focus-card,.workspace-operations',width,height)
                table=page.locator('.workspace-operations .table-scroll')
                expect(table).to_have_attribute('tabindex','0')
                expect(table).to_have_attribute('role','region')
            if route=='projects' and height>=720:
                within(page,'.workspace-project-row',width,height)
                # Every project still exposes its original native/editor actions.
                assert page.locator('[data-action="open-project"]').count()==3
            if route=='devices':
                within(page,'.workspace-device-actions',width,height)
                assert page.locator('.workspace-device').first.evaluate('''card =>
                    card.querySelector('.workspace-device-actions').getBoundingClientRect().bottom <=
                    card.querySelector('.device-lifecycle').getBoundingClientRect().top + 1''')
            if route=='workbench':
                within(page,'.work-controls,.taskbar,.terminal',width,height)
                page.locator('[data-action="read-file"][data-path="src/main.py"]').click()
                expect(page.locator('#code-editor')).to_be_visible()
                page.fill('#code-editor','long_readable_line_'*30+'\n'+'\n'.join('line '+str(i) for i in range(120)))
                within(page,'.editor-top,.editor-bottom,.taskbar,.terminal',width,height)
                assert page.locator('#code-editor').bounding_box()['height']>=100
                assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth+1')
                # Fixture draft is intentionally discarded before leaving; never save.
                page.evaluate('() => {S.work.dirty=false;}')
            if route=='native':
                within(page,'#chat-root,#chat-compose,#chat-send,#chat-model-picker',width,height)
                expect(page.locator('.chat-new-button')).to_be_visible()
            result['route']=route;measurements.append(result)
            page.screenshot(path=str(OUT/f'{engine}-{scheme}-{width}-{height}-{route}.png'),animations='disabled')
        (OUT/f'{engine}-{scheme}-{width}-{height}.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2))
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize('scheme',['light','dark'])
def test_semantic_palette_pairing_including_hover_and_selection(stack,scheme):
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page()
        _login(page,stack);_set_scheme(page,scheme)
        names=['text','muted','soft','panel','surface','raised','hover','selected','danger','danger-bg','warning','warning-bg','success','success-bg','info','info-bg','purple','purple-bg','primary','primary-text','primary-hover','message-bg','message-text']
        values=page.evaluate('names=>Object.fromEntries(names.map(n=>[n,getComputedStyle(document.documentElement).getPropertyValue("--ui-"+n).trim()]))',names)
        rows=[]
        for fg in ['text','muted','soft']:
            for bg in ['panel','surface','raised','hover','selected']:
                value=contrast(values[fg],values[bg]);rows.append((fg,bg,value));assert value>=4.5,(scheme,fg,bg,value)
        for fg,bg in [(x,x+'-bg') for x in ['danger','warning','success','info','purple']]+[('primary-text','primary'),('primary-text','primary-hover'),('message-text','message-bg')]:
            value=contrast(values[fg],values[bg]);rows.append((fg,bg,value));assert value>=4.5,(scheme,fg,bg,value)
        (OUT/f'{scheme}-palette.json').write_text(json.dumps(rows,indent=2))
        # Reference-led entity covers are intentional. They remain paired and
        # must not inherit the primary button color or cover the whole page.
        expected={'.codepier-focus-card':'--ui-cover-project','.workspace-stat':'--ui-cover-project'}
        for selector,token in expected.items():
            for node in page.locator(selector).all():
                assert node.evaluate("(el,token) => {const probe=document.createElement('span');probe.style.backgroundColor=getComputedStyle(document.documentElement).getPropertyValue(token);document.body.append(probe);const expected=getComputedStyle(probe).backgroundColor;probe.remove();return getComputedStyle(el).backgroundColor===expected;}",token)
        browser.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('scheme',['light','dark'])
def test_notices_dialog_buttons_and_login_colors(stack,engine,scheme):
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch();page=browser.new_page(viewport={'width':1366,'height':768},color_scheme=scheme,reduced_motion='reduce')
        page.goto(stack.url);expect(page.locator('#login-form')).to_be_visible();_set_scheme(page,scheme)
        initial=page.evaluate(MEASURE)
        assert not initial['failures'],('login',initial['failures'])
        _login(page,stack);_set_scheme(page,scheme)
        page.evaluate('''() => modal('表单与提示',notice('警告说明 <strong>需要核实</strong>，路径 <code>src/main.py</code>')+notice('普通说明 <strong>保留现有授权</strong>',true)+'<div class="field"><label>项目名称</label><input value="测试数据，不会提交"></div>',buttons('qa-submit','保存'))''')
        for action in ['normal','hover','focus']:
            if action=='hover':page.locator('#qa-submit').hover()
            if action=='focus':page.locator('#qa-submit').focus()
            result=page.evaluate(MEASURE)
            assert not result['failures'],(scheme,action,result['failures'])
        page.screenshot(path=str(OUT/f'{engine}-{scheme}-notices.png'),animations='disabled')
        page.keyboard.press('Escape');browser.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('width,height',[(320,568),(390,844),(768,1024)])
def test_mobile_reflow_and_navigation_remain_reachable(stack,engine,width,height):
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch();page=browser.new_page(viewport={'width':width,'height':height},reduced_motion='reduce')
        _login(page,stack)
        within(page,'.mobile-dock',width,height)
        for button in page.locator('.mobile-dock button').all():
            assert button.bounding_box()['height']>=44
        page.locator('.mobile-dock [data-action="toggle-menu"]').click()
        expect(page.locator('.mobile-close')).to_be_focused()
        within(page,'.side-bottom',width,height)
        page.keyboard.press('Escape')
        page.locator('.mobile-dock [data-nav="projects"]').click()
        expect(page.locator('#page h1')).to_have_text('项目映射')
        report=page.evaluate(MEASURE);assert report['documentWidth']<=width+1
        assert not report['failures'],report['failures']
        page.screenshot(path=str(OUT/f'{engine}-{width}-mobile.png'),animations='disabled')
        browser.close()


@pytest.mark.parametrize('chat_page',['chromium','webkit'],indirect=True)
@pytest.mark.parametrize('scheme',['light','dark'])
def test_standalone_chat_color_pairs_and_draft_preserved(chat_page,scheme):
    page=chat_page;page.select_option('#chat-appearance',scheme)
    page.set_viewport_size({'width':1280,'height':720})
    page.emulate_media(reduced_motion='reduce')
    report=page.evaluate(MEASURE);assert not report['failures'],report['failures']
    page.fill('#chat-compose','保留草稿，不向模型提交')
    page.click('#chat-model-picker');expect(page.locator('#chat-model-search')).to_be_visible()
    report=page.evaluate(MEASURE);assert not report['failures'],report['failures']
    page.keyboard.press('Escape');expect(page.locator('#chat-compose')).to_have_value('保留草稿，不向模型提交')
    assert not page.evaluate("requests.some(r=>r.path.endsWith('/start'))")
