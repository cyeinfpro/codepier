"""Actual browser + temporary HTTP Hub/Agent. No production or model requests."""
from __future__ import annotations
import json
from pathlib import Path
import re
import sys

import pytest
from playwright.sync_api import sync_playwright, expect
from tests.browser_launch import launch_browser
from tests.support import running_stack, BASE
from tests.test_integrations_stack import integrated_stack

OUT=BASE/'docs/evidence/devtools-flow-20260918/real-stack-screenshots'


def login(page,s):
    page.goto(s.url+'/#integrations')
    page.locator('#username').fill('admin');page.locator('#password').fill(s.password)
    page.locator('#login-form button[type=submit]').click()
    page.wait_for_selector('#integration-center')
    page.locator('#i-project').select_option(s.project['id'])
    page.wait_for_selector('#i-checks .integration-check')


def tab(page,name):
    page.locator('[data-i-tab="'+name+'"]').click()
    expect(page.locator('#i-body')).to_have_attribute('aria-labelledby','i-tab-'+name)


def capture_integration_renders(page):
    # Await real render promises rather than string-predicate polling, which
    # can require unsafe-eval under WebKit's strict application CSP.
    page.evaluate("""() => {
        window.integrationRenders = [];
        window.previousIntegrationRoot = document.querySelector('#integration-center');
        const original = renderPage;
        renderPage = (...args) => {
            const pending = original(...args);
            window.integrationRenders.push(pending);
            return pending;
        };
    }""")


def test_panel_real_validation_semantics_navigation_and_draft_retention(integrated_stack):
    s=integrated_stack
    with sync_playwright() as pw:
        browser=launch_browser(pw.chromium);page=browser.new_page(viewport={'width':1366,'height':768},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
        login(page,s)
        expect(page.locator('#i-checks')).to_contain_text('原生模型对话')
        tab(page,'validation');form=page.locator('[data-i-form=validate]')
        form.locator('[name=command]').fill('printf panel-verified')
        form.locator('[name=label]').fill('UI verification')
        tab(page,'navigation');page.wait_for_selector('[data-i-form=lsp]')
        tab(page,'validation');expect(page.locator('[name=command]')).to_have_value('printf panel-verified')
        page.locator('[data-i-form=validate] button[type=submit]').click()
        page.locator('#i-confirm-submit').click()
        expect(page.locator('#i-result')).to_contain_text('passed',timeout=25000)
        page.locator('[data-i-action=validation-detail]').first.click()
        expect(page.get_by_role('button',name='接受当前验收',exact=True)).to_be_disabled()
        expect(page.locator('.integration-evidence-output')).to_contain_text('panel-verified')
        page.locator('#i-validation-reviewed').check()
        expect(page.get_by_role('button',name='接受当前验收',exact=True)).to_be_enabled()
        page.get_by_role('button',name='接受当前验收',exact=True).click()
        page.locator('#i-confirm-submit').click()
        expect(page.locator('#i-result')).to_contain_text('accepted_current',timeout=15000)
        (s.projectalpha/'ui-after-validation.py').write_text('value=1\n')
        page.locator('[data-i-action=validation-detail]').first.click()
        expect(page.get_by_role('button',name='接受当前验收',exact=True)).to_be_disabled()
        expect(page.locator('#i-result')).to_contain_text('stale')
        tab(page,'navigation');f=page.locator('[data-i-form=lsp]')
        f.locator('[name=path]').fill('source.py');f.locator('[name=action]').select_option('hover')
        f.locator('button[type=submit]').click();expect(page.locator('#i-result')).to_contain_text('fixture: str',timeout=15000)
        tab(page,'browser');expect(page.locator('#i-browser-status')).to_contain_text('未连接')
        tab(page,'handoff');expect(page.locator('#i-workflows')).not_to_contain_text('读取任务')
        tab(page,'setup');expect(page.locator('[href="/static/browser-extension.zip"]')).to_be_visible()
        with page.expect_download() as download:
            page.locator('[data-i-form=settings] button[type=submit]').click()
        data=json.loads(Path(download.value.path()).read_text())
        from agent.integration_config import validate_integrations
        validate_integrations(data)
        assert data['language_servers']['python']['projects']==['ProjectAlpha']
        assert not errors,errors
        browser.close()


def test_worktree_editor_writes_only_selected_workspace(integrated_stack):
    s=integrated_stack
    with sync_playwright() as pw:
        browser=launch_browser(pw.chromium);page=browser.new_page(viewport={'width':1366,'height':768},reduced_motion='reduce')
        page.on('dialog',lambda d:d.accept());errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        login(page,s);tab(page,'worktrees')
        page.locator('[data-i-form=worktree] [name=label]').fill('UI isolated worktree')
        page.locator('[data-i-form=worktree] button[type=submit]').click()
        page.locator('#i-confirm-submit').click()
        expect(page.locator('#i-trees')).to_contain_text('UI isolated worktree',timeout=20000)
        row=page.locator('#i-trees .integration-row').filter(has_text='UI isolated worktree')
        workspace=row.locator('[data-i-action=tree-open]').get_attribute('data-id')
        target=Path(row.locator('code').inner_text())
        original=(s.projectalpha/'README.md').read_bytes()
        row.locator('[data-i-action=tree-open]').click()
        page.wait_for_selector('#file-tree [data-path="README.md"]')
        assert page.evaluate('S.work.workspace_id')==workspace
        page.locator('#file-tree [data-path="README.md"]').click()
        page.wait_for_selector('#code-editor')
        page.locator('#code-editor').fill('Changed only in managed workspace\n')
        page.locator('[data-action=preview-save]').click();page.locator('#confirm-save').click()
        expect(page.locator('#dirty-state')).to_have_text('已同步',timeout=15000)
        assert (target/'README.md').read_text()=='Changed only in managed workspace\n'
        assert (s.projectalpha/'README.md').read_bytes()==original
        page.locator('[data-action=move-file]').click();page.locator('#move-destination').fill('WORKTREE-README.md');page.locator('#confirm-move').click()
        expect(page.locator('.editor-path')).to_have_text('WORKTREE-README.md',timeout=15000)
        assert (target/'WORKTREE-README.md').exists() and not (s.projectalpha/'WORKTREE-README.md').exists()
        page.locator('[data-action=delete-file]').click()
        expect(page.locator('.workspace-empty')).to_be_visible(timeout=15000)
        assert not (target/'WORKTREE-README.md').exists() and (s.projectalpha/'README.md').exists()
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('scheme',['light','dark'])
@pytest.mark.parametrize('width,height',[(1366,768),(390,844),(320,568)])
def test_integration_views_keep_readable_geometry(integrated_stack,engine,scheme,width,height):
    s=integrated_stack;OUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        browser=launch_browser(getattr(pw,engine));page=browser.new_page(viewport={'width':width,'height':height},color_scheme=scheme,reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        login(page,s);page.evaluate('(scheme)=>document.documentElement.dataset.appearance=scheme',scheme)
        for name in ['overview','status','validation','worktrees','navigation','browser','handoff','setup']:
            tab(page,name)
            expect(page.locator('#i-body')).not_to_be_empty()
            page.wait_for_timeout(150)
            dimensions=page.evaluate('''() => ({viewport:innerWidth,width:document.documentElement.scrollWidth,
              fields:[...document.querySelectorAll('.integration-field input,.integration-field select,.integration-field textarea')].filter(el=>el.getClientRects().length).map(el=>{const r=el.getBoundingClientRect();return [r.left,r.right]}),overflow:[...document.querySelectorAll('body *')].filter(el=>el.getClientRects().length && el.getBoundingClientRect().right>innerWidth+1).slice(0,40).map(el=>({tag:el.tagName,id:el.id,cls:el.className,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width,minWidth:getComputedStyle(el).minWidth,overflowX:getComputedStyle(el).overflowX,parent:el.parentElement.className}))})''')
            if dimensions['width']>width+1:
                print('OVERFLOW_DIAGNOSTICS',json.dumps(dimensions,ensure_ascii=False))
                isolation=page.evaluate('''() => {const report={scrollX,body:document.body.getBoundingClientRect().toJSON(),root:document.documentElement.getBoundingClientRect().toJSON(),parts:[]};for(const selector of ['.integration-tabs','.integration-context','#i-project','#i-workspace','.integration-scope','#i-body','.integration-hero','.integration-action-grid','.integration-grid','.page-heading','.page-header','.sidebar','.topbar','#page','.ambient']){const el=document.querySelector(selector);if(!el)continue;const old=el.getAttribute('style');el.style.display='none';report.parts.push({selector,width:document.documentElement.scrollWidth});if(old===null)el.removeAttribute('style');else el.setAttribute('style',old);}report.selectFixes=[];for(const [selector,property,value] of [['#i-project','appearance','none'],['#i-project','overflow','hidden'],['#i-project','contain','inline-size'],['.integration-context label','overflow','hidden']]){const el=document.querySelector(selector),old=el.getAttribute('style');el.style.setProperty(property,value);report.selectFixes.push({selector,property,width:document.documentElement.scrollWidth});if(old===null)el.removeAttribute('style');else el.setAttribute('style',old);}report.pseudo=[...document.querySelectorAll('body,body *')].flatMap(el=>['::before','::after'].map(p=>({el:el.id||el.className||el.tagName,p,style:getComputedStyle(el,p)}))).filter(r=>r.style.content!=='none'&&r.style.content!=='normal').slice(0,30).map(r=>({el:r.el,p:r.p,content:r.style.content,width:r.style.width,position:r.style.position,left:r.style.left,right:r.style.right}));return report;}''')
                print('OVERFLOW_ISOLATION',json.dumps(isolation,ensure_ascii=False))
            assert dimensions['width']<=width+1,(engine,scheme,width,name,dimensions)
            assert all(left>=-1 and right<=width+1 for left,right in dimensions['fields']),dimensions
            if name in ['status','validation','navigation']:
                page.screenshot(path=str(OUT/f'{engine}-{scheme}-{width}-{name}.png'),animations='disabled')
        assert not errors,errors
        browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width,height', [(1366, 768), (390, 844), (320, 568)])
def test_tabs_accept_latest_click_while_basics_read_is_pending(integrated_stack, engine, width, height):
    """Visible tabs must stay interactive until their replacement is mounted."""
    s = integrated_stack
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={'width': width, 'height': height}, color_scheme='dark', reduced_motion='reduce')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        held = []
        try:
            login(page, s)
            tab(page, 'validation')
            draft = page.locator('[data-i-form=validate] [name=command]')
            draft.fill('printf retained-navigation-draft')
            # Hold only the refresh read; existing content stays on screen.
            page.route('**/api/projects', lambda route: held.append(route))
            with page.expect_request('**/api/projects'):
                page.locator('[data-i-tab=overview]').click()
            for name in ['status', 'navigation', 'overview', 'validation']:
                page.locator('[data-i-tab=' + name + ']').click()
                assert page.evaluate('S.integrations.tab') == name
            assert held
            for route in held:
                route.continue_()
            page.unroute('**/api/projects')
            expect(page.locator('#i-body')).to_have_attribute('aria-labelledby', 'i-tab-validation')
            expect(page.locator('[data-i-form=validate] [name=command]')).to_have_value('printf retained-navigation-draft')
            expect(page.locator('[data-i-tab=validation]')).to_be_focused()
            tab(page, 'status')
            tab(page, 'validation')
            expect(page.locator('[data-i-form=validate] [name=command]')).to_have_value('printf retained-navigation-draft')
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('leave', ['navigate', 'logout'])
def test_pending_tab_read_cannot_rebind_after_leaving(integrated_stack, engine, leave):
    s = integrated_stack
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={'width': 390, 'height': 844}, color_scheme='dark')
        held = []
        try:
            login(page, s)
            capture_integration_renders(page)
            page.evaluate("window.oldIntegrationTab = document.querySelector('[data-i-tab=status]')")
            page.route('**/api/projects', lambda route: held.append(route))
            with page.expect_request('**/api/projects'):
                page.locator('[data-i-tab=overview]').click()
            if leave == 'navigate':
                page.locator('[data-nav=overview]:visible').first.click()
                expect(page.locator('#integration-center')).to_have_count(0)
                assert page.evaluate('S.page') == 'overview'
            else:
                page.get_by_role('button', name='展开菜单', exact=True).click()
                page.locator('[data-action=logout]').click()
                expect(page.locator('#login-form')).to_be_visible()
            before = page.evaluate("({page:S.page,tab:S.integrations?.tab || null,seq:S.renderSeq})")
            page.evaluate('window.oldIntegrationTab.click()')
            assert page.evaluate("({page:S.page,tab:S.integrations?.tab || null,seq:S.renderSeq})") == before
            assert held
            with page.expect_response('**/api/projects'):
                for route in held:
                    route.continue_()
            page.unroute('**/api/projects')
            # Drain the original render promise before checking its late result.
            page.evaluate('async () => { await window.integrationRenders[0]; }')
            expect(page.locator('#integration-center')).to_have_count(0)
            if leave == 'logout':
                expect(page.locator('#login-form')).to_be_visible()
                assert page.evaluate('S.session') is None
            else:
                assert page.evaluate('S.page') == 'overview'
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('transition', ['tabs', 'leave-return'])
def test_superseded_tab_render_cannot_steal_focus_after_aba(integrated_stack, engine, transition):
    s = integrated_stack
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={'width': 390, 'height': 844}, color_scheme='dark', reduced_motion='reduce')
        held = []
        request_count = 0

        def hold_first_read(route):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                held.append(route)
            else:
                route.continue_()

        try:
            login(page, s)
            tab(page, 'validation')
            capture_integration_renders(page)
            page.route('**/api/projects', hold_first_read)
            with page.expect_request('**/api/projects'):
                page.locator('[data-i-tab=validation]').click()
            # A completed mapping event makes a newer read independent of R1.
            page.evaluate('invalidateBasics()')
            if transition == 'tabs':
                page.locator('[data-i-tab=status]').click()
                page.locator('[data-i-tab=validation]').click()
            else:
                page.locator('[data-nav=overview]:visible').first.click()
                expect(page.locator('#integration-center')).to_have_count(0)
                page.get_by_role('button', name='展开菜单', exact=True).click()
                page.locator('#sidebar [data-nav=integrations]').click()
            page.evaluate('async () => { await window.integrationRenders.at(-1); }')
            assert page.evaluate("document.querySelector('#integration-center') !== window.previousIntegrationRoot")
            expect(page.locator('#i-body')).to_have_attribute('aria-labelledby', 'i-tab-validation')
            command = page.locator('[data-i-form=validate] [name=command]')
            command.fill('printf keep-new-focus')
            expect(command).to_be_focused()
            assert len(held) == 1 and request_count >= 2
            held[0].continue_()
            # Await the old render itself, including its chained fresh read.
            page.evaluate('async () => { await window.integrationRenders[0]; }')
            expect(command).to_be_focused()
            expect(command).to_have_value('printf keep-new-focus')
            expect(page.locator('#i-body')).to_have_attribute('aria-labelledby', 'i-tab-validation')
        finally:
            browser.close()
