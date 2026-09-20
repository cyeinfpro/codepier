"""Reference-inspired composition, with real browser geometry and retained actions."""
from pathlib import Path
from scripts.check_release import check_web_assets
from shared.util import VERSION
import json
import pytest
from playwright.sync_api import expect, sync_playwright
from scripts.ui_comfort_audit import MEASURE
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture, _set_scheme

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/evidence/ui-editorial-20260916/screenshots'


def within(page, selector):
    width=page.viewport_size['width']
    for el in page.locator(selector).all():
        if not el.is_visible():continue
        r=el.bounding_box()
        assert r and r['x']>=-1 and r['x']+r['width']<=width+1,(selector,r)


def readable(page,label):
    report=page.evaluate(MEASURE)
    assert report['documentWidth']<=page.viewport_size['width']+1,(label,report['documentWidth'])
    assert not report['failures'],(label,report['failures'])
    return report


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('scheme',['light','dark'])
@pytest.mark.parametrize('width,height',[(1280,720),(1440,900),(390,844)])
def test_editorial_composition_navigation_and_live_controls(stack,engine,scheme,width,height):
    OUT.mkdir(parents=True,exist_ok=True)
    _prepare_native_fixture(stack)
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch()
        page=browser.new_page(viewport={'width':width,'height':height},color_scheme=scheme,reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        _login(page,stack);_set_scheme(page,scheme)
        results=[]
        for route in ['overview','projects','devices','native']:
            _navigate(page,route)
            result=readable(page,(engine,scheme,width,route));result['route']=route;results.append(result)
            if route=='overview':
                expect(page.locator('.editorial-board')).to_be_visible()
                expect(page.locator('.workspace-stat')).to_have_count(4)
                if width>900:
                    hero=page.locator('.codepier-focus-card').bounding_box()
                    stats=page.locator('.workspace-stats').bounding_box()
                    assert stats['y']>hero['y']
                    assert stats['x']>=hero['x']
                    assert stats['x']+stats['width']<=hero['x']+hero['width']
                    ops=page.locator('.workspace-operations').bounding_box()
                    assert ops['y']+ops['height']<=height+1,ops
                expect(page.locator('.overview-summary .workspace-stat')).to_have_count(4)
                expect(page.locator('.editorial-board > .workspace-stats')).to_have_count(0)
                expect(page.locator('.codepier-focus-path,.codepier-focus-actions')).to_have_count(0)
            if route=='projects':
                expect(page.locator('[data-project-row]')).to_have_count(page.evaluate('() => S.projects.length'))
                expect(page.locator('[data-action="edit-project"]').first).to_be_visible()
                page.fill('#project-query','__nothing__')
                expect(page.locator('#project-no-match')).to_be_visible()
                page.get_by_role('button',name='清除筛选').click()
                expect(page.locator('#project-no-match')).to_be_hidden()
                page.locator('#page h1').click()
                within(page,'.workspace-project-actions .btn')
            if route=='devices':
                expect(page.locator('[data-action="device-detail"]').first).to_be_visible()
            if route=='native':
                page.locator('.chat-starters button').first.click()
                expect(page.locator('#chat-compose')).not_to_have_value('')
                page.fill('#chat-compose','保留草稿，不发送')
                page.locator('#chat-model-picker').click()
                expect(page.locator('#chat-model-search')).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#chat-compose')).to_have_value('保留草稿，不发送')
                within(page,'#chat-compose,#chat-send,#chat-effort-select')
            page.screenshot(path=str(OUT/f'{engine}-{scheme}-{width}-{route}.png'),animations='disabled')
        (OUT/f'{engine}-{scheme}-{width}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('width,height',[(320,568),(1280,720)])
def test_long_names_empty_states_and_colored_card_actions(stack,engine,width,height):
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch();page=browser.new_page(viewport={'width':width,'height':height},reduced_motion='reduce')
        # Synthetic project data must not be replaced by live event refreshes.
        page.route('**/api/events', lambda route: route.abort())
        _login(page,stack,'projects')
        page.evaluate('''() => {
          S.projects=S.projects.map(p=>({...p,alias:'长项目名称_'.repeat(12),description:'说明文字 '.repeat(20),root:'/project/'+('long-path/'.repeat(40))}));
          document.querySelector('#page').innerHTML=projectsHTML();uiPageReady();
        }''')
        within(page,'.workspace-project-row,.workspace-project-actions,.workspace-project-path')
        expect(page.locator('[data-action="edit-project"]').first).to_have_accessible_name(__import__('re').compile('编辑'))
        for btn in page.locator('.workspace-project-row').first.locator('button').all():
            btn.scroll_into_view_if_needed();btn.click(trial=True)
        page.evaluate('''() => {S.projects=[];document.querySelector('#page').innerHTML=projectsHTML();uiPageReady();}''')
        expect(page.locator('[data-action="add-project"]')).not_to_have_count(0)
        readable(page,'empty projects')
        browser.close()


def test_editorial_implementation_has_no_fake_metrics_or_hidden_mobile_primary():
    css=(ROOT/'web/workspace.css').read_text()
    app=(ROOT/'web/app.js').read_text()
    html=(ROOT/'web/index.html').read_text()
    assert 'editorial-board' in css and 'grid-template-columns' in css
    assert 'o.active_operations' in app and 'o.online_devices' in app
    assert 'data-action="open-project"' in app
    assert 'prefers-reduced-motion' in css and 'forced-colors' in css
    assets = check_web_assets(ROOT, VERSION)
    for asset in ['tokens.css','styles.css','workspace.css','app.js','chat.css','chat.js','computer.css','workflows.js']:
        assert asset in assets, asset
