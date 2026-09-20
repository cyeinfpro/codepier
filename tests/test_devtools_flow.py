"""Developer-tool UX using the real page and an isolated Hub/Agent.

Race/error cases replace only the authenticated HTTP boundary. Native model and
browser-control requests are deterministic fixtures, never a user's providers.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect
from tests.test_integrations_stack import integrated_stack

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/evidence/devtools-flow-20260918/screenshots'


def tab(page, name):
    page.locator(f'[data-i-tab="{name}"]').click()
    expect(page.locator('#i-body')).to_have_attribute('aria-labelledby','i-tab-'+name)
    expect(page.locator(f'[data-i-tab="{name}"]')).to_have_attribute('aria-selected','true')


def confirm(page):
    expect(page.locator('#i-confirm-submit')).to_be_visible()
    page.locator('#i-confirm-submit').click()


@pytest.fixture
def tools_page(integrated_stack, chat_browser_pool):
    s = integrated_stack
    context = chat_browser_pool('chromium').new_context(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(s.url + '/#integrations')
    page.locator('#username').fill('admin')
    page.locator('#password').fill(s.password)
    page.locator('#login-form button').click()
    page.wait_for_selector('#integration-center')
    page.select_option('#i-project', s.project['id'])
    # Selection clears readiness before the previous checks leave the DOM.
    page.wait_for_function('project => S.integrations.project === project && !!S.integrations.ready?.data', arg=s.project['id'])
    page.wait_for_selector('#i-checks .integration-check')
    page.evaluate('''() => {
      window.realToolsApi = api; window.toolCalls=[]; window.toolHandlers={}; window.nativeCalls=[];
      window.api = async (path, options={}) => {
        const payload=options.body?JSON.parse(options.body):{};
        if(path==='/api/tools/call') {
          toolCalls.push(structuredClone(payload));
          if(toolHandlers[payload.tool]) return await toolHandlers[payload.tool](payload.arguments,options);
        }
        if(path.startsWith('/api/native/')) {
          nativeCalls.push({path,payload});
          if(path.includes('/sessions?')) return {sessions:[]};
          if(path.endsWith('/chat_catalog')) return {cli:'pi',models:[],thinking_levels:[],commands:[],capabilities:{}};
          return {files:[],sessions:[],commands:[],state:'completed'};
        }
        return realToolsApi(path,options);
      };
    }''')
    try:
        yield page
        assert not errors, errors
    finally:
        try:
            page.evaluate("CodePierIntegrations.detach();stopEvents();S.page='test-finished';")
        except Exception:
            pass
        context.close()


def test_overview_readiness_guidance_raw_collapsed_and_keyboard(tools_page):
    page = tools_page
    expect(page.locator('h1')).to_have_text('开发工具')
    expect(page.locator('[data-i-tab=overview]')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('.integration-action-card')).to_have_count(4)
    expect(page.locator('#i-checks')).to_contain_text('基础开发工具已就绪')
    assert page.locator('#i-checks details[open]').count() == 0
    page.focus('[data-i-tab=overview]')
    page.keyboard.press('End')
    expect(page.locator('[data-i-tab=setup]')).to_be_focused()
    expect(page.locator('#i-body')).to_have_attribute('aria-labelledby', 'i-tab-setup')
    page.keyboard.press('Home')
    expect(page.locator('[data-i-tab=overview]')).to_be_focused()
    assert not page.evaluate("toolCalls.some(r=>['validation_run','integration_control','browser_open'].includes(r.tool))")


def test_status_failure_retries_locally_without_hiding_tasks(tools_page):
    page = tools_page
    page.evaluate('''() => {
      window.savedReady=S.integrations.ready.data; window.failReady=true;
      toolHandlers.readiness_get=async()=>{if(failReady)throw new Error('fixture connection missing');return savedReady;};
    }''')
    page.click('[data-i-action=refresh]')
    expect(page.locator('#i-checks')).to_contain_text('fixture connection missing')
    expect(page.locator('#i-workflows')).to_contain_text('建立开发任务')
    page.evaluate('() => {failReady=false;savedReady.browser={connected:true,enabled:true,profile_bound:false,pool:{available:0}};}')
    page.locator('#i-checks [data-i-action=section-retry]').click()
    expect(page.locator('#i-checks')).to_contain_text('基础开发工具已就绪')
    assert page.evaluate('S.integrations.tab') == 'overview'
    expect(page.locator('.integration-optional')).to_contain_text('已连接 · 打开前核验')


def test_old_project_response_cannot_replace_selected_project(tools_page):
    page = tools_page
    page.evaluate('''() => {
      window.firstProject=S.integrations.project;window.otherProject=S.projects.find(p=>p.id!==firstProject).id;
      window.savedReady=S.integrations.ready.data;
      toolHandlers.readiness_get=async args=>args.project===firstProject?
        await new Promise(resolve=>window.releaseOld=()=>resolve({...savedReady,checks:[{name:'project_read',state:'ready',reason:'LATE OLD PROJECT'}]})):
        {...savedReady,checks:[{name:'project_read',state:'ready',reason:'CURRENT PROJECT'}]};
    }''')
    page.click('[data-i-action=refresh]')
    page.wait_for_function('() => !!window.releaseOld')
    page.select_option('#i-project', page.evaluate('otherProject'))
    expect(page.locator('#i-checks')).to_contain_text('CURRENT PROJECT')
    page.evaluate('releaseOld()')
    expect(page.locator('#i-checks')).not_to_contain_text('LATE OLD PROJECT')
    assert page.evaluate('S.integrations.project===otherProject')
    page.evaluate("() => {S.integrations.workspace_id='d'.repeat(32);S.integrations.trees=[];return renderPage(false);}")
    expect(page.locator('.integration-scope code')).to_contain_text('路径待核对')
    page.evaluate("() => {S.integrations.trees=[{workspace_id:'d'.repeat(32),state:'ready',path:'/synthetic/isolated-checkout'}];return renderPage(false);}")
    expect(page.locator('.integration-scope code')).to_have_text('/synthetic/isolated-checkout')


def test_command_drafts_are_scoped_and_custom_confirmation_cancels(tools_page):
    page = tools_page
    tab(page, 'validation')
    page.locator('[name=command]').fill('printf not-executed')
    page.locator('[data-i-form=validate] button[type=submit]').click()
    expect(page.locator('#i-confirm-form')).to_contain_text('printf not-executed')
    page.keyboard.press('Escape')
    assert not page.evaluate("toolCalls.some(r=>r.tool==='validation_run')")
    original = page.evaluate('S.integrations.project')
    other = page.evaluate('S.projects.find(p=>p.id!==S.integrations.project).id')
    page.select_option('#i-project', other)
    expect(page.locator('[name=command]')).to_have_value('')
    page.select_option('#i-project', original)
    expect(page.locator('[name=command]')).to_have_value('printf not-executed')
    tab(page, 'navigation')
    tab(page, 'validation')
    expect(page.locator('[name=command]')).to_have_value('printf not-executed')


def test_pending_mutation_recovers_same_operation_without_reexecution(tools_page):
    page = tools_page
    page.evaluate('''() => {
      window.originalProject=S.integrations.project;window.fakeOperation='1'.repeat(32);
      toolHandlers.validation_run=async()=>await new Promise(resolve=>window.releaseWrite=()=>resolve({pending:true,operation_id:fakeOperation}));
      toolHandlers.operations_wait=async()=>({id:fakeOperation,state:'succeeded',pending:false,result:{ok:true,data:{validation_id:fakeOperation,state:'passed',exit_code:0,output:'ORIGINAL OUTPUT'}}});
    }''')
    tab(page, 'validation')
    page.fill('[name=command]', 'printf original-only-once')
    page.click('[data-i-form=validate] button[type=submit]')
    confirm(page)
    page.wait_for_function('() => !!window.releaseWrite')
    tab(page, 'setup')
    page.evaluate('releaseWrite()')
    tab(page, 'validation')
    expect(page.locator('#i-receipts')).to_contain_text('待确认的操作')
    page.fill('[name=command]', 'printf second-intent')
    page.click('[data-i-form=validate] button[type=submit]')
    confirm(page)
    expect(page.locator('.integration-form-error')).to_contain_text('待确认的原请求')
    page.locator('#i-receipts button').filter(has_text='查询原结果').click()
    expect(page.locator('#i-result')).to_contain_text('ORIGINAL OUTPUT')
    assert page.evaluate("toolCalls.filter(r=>r.tool==='validation_run').length") == 1
    assert page.evaluate("toolCalls.filter(r=>r.tool==='operations_wait').every(r=>r.arguments.operation_id===fakeOperation)")
    expect(page.locator('#i-receipts')).to_be_empty()
    assert page.evaluate('S.integrations.submissions.size') == 0


def test_missing_transport_receipt_queries_key_and_never_saves_command(tools_page):
    page = tools_page
    page.evaluate('''() => {
      window.fakeOperation='2'.repeat(32);
      toolHandlers.validation_run=async()=>{throw Object.assign(new Error('lost reply'),{code:'NETWORK_UNCERTAIN'});};
      toolHandlers.operations_list=async()=>({operations:[{id:fakeOperation}]});
      toolHandlers.operations_wait=async()=>({id:fakeOperation,state:'succeeded',pending:false,result:{ok:true,data:{validation_id:fakeOperation,state:'failed',exit_code:9,output:'original failure'}}});
    }''')
    tab(page, 'validation')
    page.fill('[name=command]', 'printf SYNTHETIC_PRIVATE_ARGUMENT')
    page.click('[data-i-form=validate] button[type=submit]')
    confirm(page)
    expect(page.locator('#i-receipts')).to_contain_text('待确认的操作')
    stored = page.evaluate("sessionStorage.getItem('codepier-integration-receipts')")
    assert 'SYNTHETIC_PRIVATE_ARGUMENT' not in stored and 'csrf' not in stored and 'arguments' not in stored
    page.locator('#i-receipts button').filter(has_text='查询原结果').click()
    expect(page.locator('#i-result')).to_contain_text('original failure')
    assert page.evaluate("toolCalls.filter(r=>r.tool==='validation_run').length") == 1
    assert page.evaluate("toolCalls.find(r=>r.tool==='operations_list').arguments.idempotency_key===toolCalls.find(r=>r.tool==='validation_run').arguments.idempotency_key")


def test_receipt_refresh_restores_lookup_only_and_logout_clears(tools_page):
    page = tools_page
    project = page.evaluate('S.integrations.project')
    stored = [{'key':'fixture-restore-key','name':'validation_run','project':project,'workspace_id':'','created':1,'phase':'uncertain','operation_id':'3'*32}]
    page.evaluate('(data)=>sessionStorage.setItem("codepier-integration-receipts",JSON.stringify(data))', stored)
    page.reload()
    page.wait_for_selector('#integration-center')
    page.select_option('#i-project', project)
    expect(page.locator('#i-receipts')).to_contain_text('待确认的操作')
    assert page.evaluate('[...S.integrations.submissions.values()].every(r=>!r.args)')
    assert page.locator('#i-receipts').get_by_text('重试同一请求', exact=True).count() == 0
    page.evaluate('endSession()')
    assert page.evaluate("sessionStorage.getItem('codepier-integration-receipts')") is None
    assert page.evaluate('S.integrations') is None


def test_readonly_capability_no_false_language_or_browser_readiness(tools_page):
    page = tools_page
    page.evaluate('''() => {
      toolHandlers.lsp_status=async()=>({servers:[{language:'python',configured:true,executable_available:false,reason:'Missing executable'}],semantic_queries_available:false});
      toolHandlers.browser_status=async()=>({enabled:false,connected:false,profile_bound:false,origins:[],pool:{available:null},active_leases:[]});
    }''')
    tab(page, 'navigation')
    expect(page.locator('[data-i-form=lsp] button[type=submit]')).to_be_disabled()
    expect(page.locator('#i-lsp-status')).to_contain_text('尚未启用语义查询')
    tab(page, 'browser')
    expect(page.locator('[data-i-form=browser-open] button[type=submit]')).to_be_disabled()
    expect(page.locator('#i-browser-status')).to_contain_text('未连接')
    page.locator('#i-browser-status [data-i-jump=setup]').click()
    expect(page.locator('#i-body')).to_contain_text('不会安装程序')


@pytest.mark.parametrize('action', ['symbols','workspace_symbols','definition','references','hover','diagnostics','incoming_calls','outgoing_calls'])
def test_navigation_form_uses_only_relevant_fields(tools_page, action):
    page = tools_page
    tab(page, 'navigation')
    expect(page.locator('[data-i-form=lsp] button[type=submit]')).to_be_enabled()
    page.select_option('[name=action]', action)
    if action == 'workspace_symbols':
        expect(page.locator('[name=path]')).to_be_hidden()
        expect(page.locator('[name=query]')).to_be_visible()
        page.fill('[name=language]', 'python')
        page.fill('[name=query]', 'fixture')
    else:
        page.fill('[name=path]', 'source.py')
        expect(page.locator('[name=query]')).to_be_hidden()
    positional = action in {'definition','references','hover','incoming_calls','outgoing_calls'}
    if positional:
        expect(page.locator('[name=line]')).to_be_visible()
    else:
        expect(page.locator('[name=line]')).to_be_hidden()
    page.click('[data-i-form=lsp] button[type=submit]')
    expect(page.locator('#i-result')).to_contain_text('语义查询结果', timeout=20000)
    assert page.locator('#i-result details[open]').count() == 0


def test_code_navigation_opens_exact_line_and_keeps_dirty_buffer(tools_page, integrated_stack):
    page = tools_page
    (integrated_stack.imago/'unicode-flow.py').write_text('header\n😀abc xyz\nend\n')
    page.evaluate('''() => {toolHandlers.lsp_query=async()=>({source_current:true,items:[{path:'unicode-flow.py',range:{start:{line:2,column:3}},name:'Unicode fixture'}]});}''')
    tab(page, 'navigation')
    page.fill('[name=path]', 'unicode-flow.py')
    page.click('[data-i-form=lsp] button[type=submit]')
    page.get_by_role('button', name='打开并定位', exact=True).click()
    expect(page.locator('#code-editor')).to_have_value('header\n😀abc xyz\nend\n')
    assert page.locator('#code-editor').evaluate('(el)=>el.selectionStart') == 10
    page.fill('#code-editor', 'unsaved local buffer')
    page.once('dialog', lambda dialog: dialog.accept())
    page.click('[data-devtools=navigation]')
    expect(page.locator('[name=path]')).to_have_value('unicode-flow.py')
    page.click('[data-i-form=lsp] button[type=submit]')
    page.get_by_role('button', name='打开并定位', exact=True).click()
    expect(page.locator('#code-editor')).to_have_value('unsaved local buffer')
    assert page.evaluate('S.work.dirty')


def test_handoff_is_readable_bound_and_enters_draft_without_sending(tools_page, integrated_stack):
    page = tools_page
    s = integrated_stack
    created=s.call('workflows_create',{'project':s.project['id'],'title':'Flow handoff fixture','goal':'Preserve the original goal','idempotency_key':uuid.uuid4().hex})
    wid=created['workflow_id']
    page.evaluate('(wid)=>CodePierIntegrations.open({project:S.integrations.project,tab:"handoff",workflow_id:wid})', wid)
    expect(page.locator('#i-result')).to_contain_text('Preserve the original goal')
    assert page.locator('#i-result details[open]').count() == 0
    page.click('[data-i-action=handoff-chat]')
    expect(page.locator('#chat-compose')).to_have_value(re.compile('Preserve the original goal'))
    assert wid in page.input_value('#chat-compose')
    assert not page.evaluate("nativeCalls.some(r=>r.path.endsWith('/start')||r.path.endsWith('/chat_prompt'))")
    page.locator('.chat-overflow summary').click()
    page.locator('[data-devtools=validation]').click()
    expect(page.locator('#i-project')).to_have_value(s.project['id'])
    expect(page.locator('[data-i-tab=validation]')).to_have_attribute('aria-selected','true')


def test_validation_details_do_not_replace_newer_selection(tools_page):
    page = tools_page
    page.evaluate('''() => {
      toolHandlers.validations_list=async()=>({validations:[{validation_id:'a'.repeat(32),label:'Receipt A',historical_state:'passed'},{validation_id:'b'.repeat(32),label:'Receipt B',historical_state:'passed'}]});
      toolHandlers.validations_get=async args=>args.validation_id[0]==='a'?await new Promise(resolve=>window.releaseA=()=>resolve({validation_id:args.validation_id,state:'stale'})):{validation_id:args.validation_id,state:'passed',source_current:true,execution_operation_id:'b'.repeat(32)};
      toolHandlers.operations_get=async()=>({id:'b'.repeat(32),state:'succeeded',output:'B output evidence'});
    }''')
    tab(page,'validation')
    page.locator('[data-i-action=validation-detail]').nth(0).click()
    page.wait_for_function('() => !!window.releaseA')
    page.locator('[data-i-action=validation-detail]').nth(1).click()
    expect(page.locator('#i-result')).to_contain_text('B output evidence')
    page.evaluate('releaseA()')
    expect(page.locator('#i-result')).to_contain_text('B output evidence')
    expect(page.locator('[data-i-action=validation-accept]')).to_be_disabled()
    page.check('#i-validation-reviewed')
    expect(page.locator('[data-i-action=validation-accept]')).to_be_enabled()


def test_control_confirmation_is_typed_and_does_not_default_to_native(tools_page):
    page = tools_page
    page.evaluate('() => {toolHandlers.integration_control=async args=>({paused:true,include_native:args.include_native});}')
    tab(page,'status')
    page.locator('.integration-control summary').click()
    page.click('[data-i-action=stop]')
    expect(page.locator('#i-confirm-submit')).to_be_disabled()
    page.fill('#i-confirm-name','wrong project')
    expect(page.locator('#i-confirm-submit')).to_be_disabled()
    page.keyboard.press('Escape')
    assert not page.evaluate("toolCalls.some(r=>r.tool==='integration_control')")
    page.click('[data-i-action=stop]')
    page.fill('#i-confirm-name',page.evaluate('S.projects.find(p=>p.id===S.integrations.project).alias'))
    confirm(page)
    expect(page.locator('#i-result')).to_contain_text('接入控制结果')
    assert page.evaluate("toolCalls.find(r=>r.tool==='integration_control').arguments.include_native") is False


def test_configuration_download_is_project_scoped_and_never_applied(tools_page):
    page = tools_page
    tab(page,'setup')
    page.fill('[name=command]','invalid JSON')
    page.click('[data-i-form=settings] button[type=submit]')
    expect(page.locator('.integration-form-error')).to_contain_text('JSON 数组')
    page.fill('[name=command]','["/fixture/pyright-langserver","--stdio"]')
    with page.expect_download() as got:
        page.click('[data-i-form=settings] button[type=submit]')
    data=json.loads(Path(got.value.path()).read_text())
    assert data['language_servers']['python']['projects']==['Imago']
    expect(page.locator('#i-result')).to_contain_text('尚未应用')
    assert not page.evaluate("toolCalls.some(r=>['shell_exec','fs_write','integration_control'].includes(r.tool))")


@pytest.mark.parametrize('value', ['../secret','/absolute.py','C:\\private.py','a//b.py','a/./b.py','a/../b.py','a\\b.py'])
def test_path_validation_rejects_outside_and_ambiguous_paths(tools_page,value):
    assert tools_page.evaluate('value=>{try{CodePierIntegrationUI.relativePath(value);return false;}catch{return true;}}',value)


def test_cli_developer_links_remain_reachable_on_short_screens(tools_page):
    page=tools_page
    project=page.evaluate('S.integrations.project')
    page.set_viewport_size({'width':390,'height':360})
    page.click('[data-i-action=start-chat]')
    expect(page.locator('#chat-compose')).to_be_visible()
    page.locator('.chat-overflow summary').click()
    menu=page.locator('.chat-overflow>div')
    expect(menu).to_be_visible()
    rect=menu.bounding_box()
    assert rect and rect['y']>=0 and rect['y']+rect['height']<=361,rect
    menu.locator('button').last.scroll_into_view_if_needed()
    last=menu.locator('button').last.bounding_box()
    assert last and last['y']>=rect['y'] and last['y']+last['height']<=rect['y']+rect['height'],last
    page.locator('[data-devtools=validation]').click()
    expect(page.locator('#i-body')).to_have_attribute('aria-labelledby','i-tab-validation')
    expect(page.locator('#i-project')).to_have_value(project)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    assert not page.evaluate("nativeCalls.some(r=>r.path.endsWith('/start')||r.path.endsWith('/chat_prompt'))")


@pytest.mark.parametrize('width,theme',[(1440,'light'),(1440,'dark'),(390,'light'),(320,'dark')])
def test_new_overview_setup_and_forms_have_bounded_geometry(tools_page,width,theme):
    page=tools_page
    page.set_viewport_size({'width':width,'height':900})
    page.emulate_media(color_scheme=theme)
    page.evaluate('(theme)=>document.documentElement.dataset.appearance=theme',theme)
    OUT.mkdir(parents=True,exist_ok=True)
    for name in ['overview','validation','handoff','navigation','worktrees','browser','status','setup']:
        tab(page,name)
        expect(page.locator('#i-body')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'),(width,theme,name)
        rects=page.evaluate('''() => [...document.querySelectorAll('#integration-center input,#integration-center textarea,#integration-center select')].filter(el=>el.getClientRects().length).map(el=>{const r=el.getBoundingClientRect();return {name:el.name||el.id,x:r.x,width:r.width};})''')
        assert rects,(width,name,'expected at least the project and directory selectors')
        for r in rects:
            assert r['x']>=-1 and r['x']+r['width']<=width+2,(width,name,r)
        if name in {'overview','validation','navigation','browser','setup'}:
            page.screenshot(path=str(OUT/f'{name}-{width}-{theme}.png'),full_page=True)
