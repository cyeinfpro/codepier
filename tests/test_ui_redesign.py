"""Command Deck: real browser, real isolated Hub + outbound Agent, no production data."""
import json
import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

PAGES = {
    'overview': '控制总览', 'devices': '设备节点', 'projects': '项目映射',
    'workbench': '远程工作台', 'workflows': '开发任务', 'audit': '操作审计',
    'connect': 'MCP 接入', 'diagnostics': '运行诊断', 'artifacts': '产物交付',
    'settings': '系统设置',
}
OUT = Path(os.getenv('CODEPIER_UI_SCREENSHOTS', 'docs/evidence/ui-20260913/screenshots'))


def login(page, stack, route='overview'):
    page.goto(stack.url + '/#' + route)
    page.fill('#username', 'admin');page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text(PAGES[route])


def navigate(page, name):
    if page.viewport_size['width'] <= 900:
        page.click('.mobile-menu')
        expect(page.locator('.sidebar')).to_have_class('sidebar open')
    page.locator(f'.nav [data-nav="{name}"]').click()
    expect(page.locator('#page h1')).to_have_text(PAGES[name])


def layout(page, label):
    result = page.evaluate('''() => {
      const viewport=innerWidth;
      const errors=[...document.querySelectorAll('#page .page-head, #page .panel, #page .notice, .modal')].flatMap(el=>{
        if(!el.getClientRects().length)return [];
        const r=el.getBoundingClientRect();
        return r.left < -1 || r.right > viewport+1 ? [{tag:el.className,left:r.left,right:r.right}] : [];
      });
      return {width:viewport,document:document.documentElement.scrollWidth,errors};
    }''')
    assert result['document'] <= result['width'], (label, result)
    assert not result['errors'], (label, result)
    return result


@pytest.mark.parametrize('width,height', [(1440,1000),(768,1024),(390,844),(320,568)])
def test_all_pages_layout_and_screenshots(stack, width, height):
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width':width,'height':height})
        errors=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(stack.url)
        expect(page.locator('#login-form')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(OUT/f'{width}-login.png'),full_page=True,animations='disabled')
        page.fill('#username', 'admin');page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('.stats')).to_be_visible()
        measurements={}
        for name in PAGES:
            navigate(page,name)
            if name=='workbench':
                expect(page.locator('[data-action="read-file"][data-path="src/main.py"]')).to_be_visible()
                page.click('[data-action="read-file"][data-path="src/main.py"]')
                expect(page.locator('#code-editor')).to_be_visible()
            if width>900:
                expect(page.locator('.mobile-menu')).to_be_hidden()
                expect(page.locator('.mobile-close')).to_be_hidden()
            if width>640 and name=='workbench':
                expect(page.locator('.mobile-work-tabs')).to_be_hidden()
            assert 'undefined' not in page.locator('#page').inner_text()
            measurements[name]=layout(page,name)
            page.screenshot(path=str(OUT/f'{width}-{name}.png'),full_page=True,animations='disabled')
        (OUT/f'{width}-layout.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2))
        assert not errors,errors
        browser.close()


def test_command_palette_filter_focus_and_navigation(stack):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        login(page,stack)
        page.locator('[data-ui="command"]').focus()
        page.keyboard.press('Control+k')
        expect(page.locator('#command-query')).to_be_focused()
        page.fill('#command-query','系统设置')
        expect(page.locator('.command-item')).to_have_count(1)
        page.keyboard.press('Enter')
        expect(page.locator('#page h1')).to_have_text('系统设置')
        page.locator('[data-ui="command"]').click()
        page.fill('#command-query','Imago')
        expect(page.locator('.command-item')).to_have_count(1)
        page.keyboard.press('Enter')
        expect(page.locator('#page h1')).to_have_text('远程工作台')
        expect(page.locator('#work-project')).to_have_value(stack.project['id'])
        page.locator('[data-ui="command"]').click()
        page.fill('#command-query','no-such-page')
        expect(page.locator('.command-results')).to_contain_text('没有匹配')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        expect(page.locator('.command-dialog')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('[data-ui="command"]')).to_be_focused()
        assert not page.locator('#app').evaluate('(el)=>el.inert')
        browser.close()


def test_mobile_drawer_accessibility_and_editor_panes(stack):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':390,'height':844})
        login(page,stack)
        assert page.locator('.sidebar').evaluate('(el)=>el.inert')
        page.click('.mobile-menu')
        expect(page.locator('.mobile-menu')).to_have_attribute('aria-expanded','true')
        assert page.locator('.main').evaluate('(el)=>el.inert')
        expect(page.locator('.mobile-close')).to_be_focused()
        page.keyboard.press('Shift+Tab')
        expect(page.locator('.sidebar [data-action="logout"]')).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.locator('.mobile-menu')).to_be_focused()
        assert not page.locator('.main').evaluate('(el)=>el.inert')
        navigate(page,'workbench')
        expect(page.locator('.explorer')).to_be_visible()
        expect(page.locator('.editor-wrap')).to_be_hidden()
        page.click('[data-action="read-file"][data-path="src/main.py"]')
        expect(page.locator('#code-editor')).to_be_visible()
        page.locator('#code-editor').fill('draft preserved across mobile panes\n')
        page.click('[data-ui-pane="files"]')
        expect(page.locator('.explorer')).to_be_visible()
        page.click('[data-ui-pane="editor"]')
        expect(page.locator('#code-editor')).to_have_value('draft preserved across mobile panes\n')
        page.set_viewport_size({'width':1440,'height':1000})
        expect(page.locator('.explorer')).to_be_visible()
        expect(page.locator('#code-editor')).to_be_visible()
        # matchMedia change is dispatched asynchronously after set_viewport_size.
        expect(page.locator('.sidebar')).to_have_js_property('inert', False)
        browser.close()


def test_project_search_disclosure_tools_and_modal_focus(stack):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':390,'height':844})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        login(page,stack,'projects')
        page.fill('#project-query','Nexus')
        expect(page.locator('[data-project-row]:visible')).to_have_count(1)
        page.fill('#project-query','missing-name')
        expect(page.locator('#project-no-match')).to_be_visible()
        page.click('[data-action="refresh"]')
        expect(page.locator('#project-query')).to_have_value('missing-name')
        navigate(page,'connect')
        expect(page.get_by_role('button',name='复制编码地址')).to_be_visible()
        assert page.evaluate('codingEndpoint()').endswith('/mcp?profile=coding')
        expect(page.locator('.tool-chip').first).to_be_hidden()
        page.locator('summary').filter(has_text='工具目录').click()
        page.fill('#tool-query','workspace')
        expect(page.locator('.tool-chip:visible')).to_have_count(1)
        expect(page.locator('.tool-chip:visible')).to_contain_text('workspace')
        layout(page,'expanded-tools')
        navigate(page,'workbench')
        page.locator('.tool-menu summary').click()
        page.click('[data-wf-action="context"]')
        expect(page.locator('.context-document summary').first).to_contain_text('README.md')
        assert page.locator('#app').evaluate('(el)=>el.inert')
        # Hidden buttons in closed details must not receive trapped focus.
        page.locator('.modal [data-action="close-modal"]').focus()
        page.keyboard.press('Shift+Tab')
        focused=page.evaluate('document.activeElement.getClientRects().length>0')
        assert focused
        page.keyboard.press('Escape')
        assert not page.locator('#app').evaluate('(el)=>el.inert')
        page.click('[data-local-skills]')
        expect(page.locator('#modal-title')).to_have_text('本地技能')
        expect(page.locator('#local-skills-results')).not_to_contain_text('读取技能目录…')
        layout(page,'skills-dialog')
        assert not errors,errors
        browser.close()


def test_reduced_motion_and_dirty_navigation_guard(stack):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
        login(page,stack)
        page.wait_for_function("() => {const el=document.querySelector('.overview-summary'); return el && getComputedStyle(el).animationName==='none';}")
        navigate(page,'workbench')
        page.click('[data-action="read-file"][data-path="src/main.py"]')
        expect(page.locator('#code-editor')).to_be_visible()
        page.fill('#code-editor','unsaved source\n')
        page.on('dialog',lambda dialog:dialog.dismiss())
        page.keyboard.press('Control+k')
        page.fill('#command-query','系统设置')
        page.keyboard.press('Enter')
        expect(page.locator('#page h1')).to_have_text('远程工作台')
        expect(page.locator('#code-editor')).to_have_value('unsaved source\n')
        assert page.url.endswith('#workbench')
        browser.close()


def test_live_refresh_preserves_filter_focus_and_disclosure(stack):
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        login(page,stack,'projects')
        page.locator('.page-help summary').click()
        page.fill('#project-query','Nex')
        page.evaluate("renderPage(false)")
        expect(page.locator('#project-query')).to_have_value('Nex')
        expect(page.locator('#project-query')).to_be_focused()
        expect(page.locator('.page-help')).to_have_attribute('open','')
        expect(page.locator('[data-project-row]:visible')).to_have_count(1)
        browser.close()
