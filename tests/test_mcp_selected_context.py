"""Explicit context attachment and host-removal races using the real Apps SDK."""
import json

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.test_mcp_apps_host import host_bundle
from tests.test_mcp_workbench_ui import workbench_html

PROJECT = {'id':'a'*32,'alias':'Selected project','root':'/private/fixture','mode':'read'}
SHA = 'c'*64


def mount(page,host_bundle,html,*,supported=True):
    page.set_content('<html><body></body></html>')
    page.add_script_tag(path=str(host_bundle))
    page.evaluate(r"""([project,supported])=>{
      window.contextCalls=[]; window.contextUpdates=[]; window.contextPending=[];
      window.contextDelayRead=false; window.contextDelayUpdate=false;
      window.contextRevoked=false; window.contextBadSha=false; window.contextLarge=false;
      window.contextProjects=[project];
      window.currentContext=null;
      window.codepierHostCapabilities=supported?{
        experimental:{'openai/modelContext':{}},updateModelContext:{text:{}}}:{};
      window.codepierInitialHostContext={'openai/modelContext':null};
      window.codepierUpdateContext=async params=>{
        window.contextUpdates.push(params);
        if(window.contextDelayUpdate && params.content.length)
          await new Promise(resolve=>window.contextPending.push({kind:'update',resolve}));
        window.currentContext=params.content;
        window.codepierBridge.sendHostContextChange({'openai/modelContext':params.content.length?{updateId:'own-'+window.contextUpdates.length,content:params.content}:null});
        return {};
      };
      window.codepierHostTool=async params=>{
        window.contextCalls.push(params);
        const args=params.arguments;
        let data;
        if(params.name==='project_query' && args.operation==='list')
          data={projects:window.contextRevoked?[]:window.contextProjects};
        else if(params.name==='read'){
          if(window.contextDelayRead)
            await new Promise(resolve=>window.contextPending.push({kind:'read',resolve}));
          data={path:args.path,offset:args.offset,end_line:args.offset+1,
            sha256:window.contextBadSha?'d'.repeat(64):'c'.repeat(64),
            content:window.contextLarge?'x'.repeat(8001):'first line\nsecond line\n'};
        } else data={project_id:project.id,project:project.alias,workspace_id:'',
          observed_at:1,device_online:true,workflow:null,workflows:[],evidence:[],
          recent_operations:[],next_workflow_cursor:null,next_evidence_offset:null};
        return {content:[],isError:false,structuredContent:data};
      };
    }""",[PROJECT,supported])
    page.evaluate('html=>window.codepierMount(html)',html)
    page.wait_for_function('window.codepierHostReady')
    deliver_project(page)
    app=page.frame_locator('#app-frame')
    expect(app.get_by_role('heading',name=PROJECT['alias'],exact=True)).to_be_visible()
    app.get_by_text('选定上下文',exact=True).click()
    return app


def deliver_project(page,project=PROJECT):
    page.evaluate("""project=>window.codepierDeliver({args:{project:project.id},
      result:{content:[],isError:false,structuredContent:{
        workspace:{project:project.alias,project_id:project.id,root:project.root}}}})""",project)


def preview(app):
    app.get_by_label('上下文文件相对路径').fill('src/example.py')
    app.get_by_role('button',name='读取选定片段',exact=True).click()


@pytest.mark.parametrize('engine,width',[('chromium',1100),('webkit',390)])
def test_context_requires_explicit_click_rechecks_sha_and_honors_host_removal(host_bundle,workbench_html,engine,width):
    with sync_playwright() as pw:
        browser=getattr(pw,engine).launch(headless=True)
        page=browser.new_page(viewport={'width':width,'height':1000})
        app=mount(page,host_bundle,workbench_html)
        assert page.evaluate('window.contextUpdates')==[]
        preview(app)
        expect(app.get_by_text('片段已准备，尚未添加到上下文。',exact=True)).to_be_visible()
        assert page.evaluate('window.contextUpdates')==[]
        app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True).click()
        expect(app.get_by_text('已添加到下一条消息的上下文；尚未发送消息。',exact=True)).to_be_visible()
        updates=page.evaluate('window.contextUpdates')
        assert len(updates)==1 and len(updates[0]['content'])==2
        assert PROJECT['root'] not in json.dumps(updates)
        file=json.loads(updates[0]['content'][1]['text'])
        assert (file['path'],file['sha256'],file['start_line'],file['end_line'])==('src/example.py',SHA,1,2)
        reads=[c for c in page.evaluate('window.contextCalls') if c['name']=='read']
        assert len(reads)==2 and reads[1]['arguments']['expected_sha256']==SHA
        page.evaluate("window.codepierBridge.sendHostContextChange({'openai/modelContext':null,theme:'dark'})")
        expect(app.get_by_text('宿主上下文已变化，待添加选择已清空；不会自动恢复已移除的附件。',exact=True)).to_be_visible()
        expect(app.get_by_role('button',name='移除片段 src/example.py',exact=True)).to_have_count(0)
        assert len(page.evaluate('window.contextUpdates'))==1
        assert not page.evaluate('window.codepierHostErrors')
        browser.close()


@pytest.mark.parametrize('interruption',['host-remove','new-card','hide'])
def test_context_drops_late_file_reads(host_bundle,workbench_html,interruption):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        app=mount(page,host_bundle,workbench_html)
        page.evaluate('window.contextDelayRead=true')
        preview(app)
        page.wait_for_function("window.contextPending.some(p=>p.kind==='read')")
        if interruption=='host-remove':
            page.evaluate("window.codepierBridge.sendHostContextChange({'openai/modelContext':null,theme:'dark'})")
        elif interruption=='new-card':
            deliver_project(page)
        else:
            app.locator('body').evaluate("""()=>{Object.defineProperty(document,'hidden',{value:true,configurable:true});
              document.dispatchEvent(new Event('visibilitychange'));}""")
        page.evaluate("window.contextPending.find(p=>p.kind==='read').resolve()")
        page.wait_for_timeout(150)
        assert page.evaluate('window.contextUpdates')==[]
        expect(app.get_by_role('button',name='移除片段 src/example.py',exact=True)).to_have_count(0)
        browser.close()


def test_context_inflight_update_is_cleared_once_after_host_removal(host_bundle,workbench_html):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        app=mount(page,host_bundle,workbench_html)
        page.evaluate('window.contextDelayUpdate=true')
        app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True).click()
        page.wait_for_function("window.contextPending.some(p=>p.kind==='update')")
        page.evaluate("window.codepierBridge.sendHostContextChange({'openai/modelContext':null,theme:'dark'})")
        page.evaluate("window.contextPending.find(p=>p.kind==='update').resolve()")
        page.wait_for_function('window.contextUpdates.length===2')
        assert page.evaluate('window.currentContext')==[]
        assert page.evaluate('window.contextUpdates[1].content')==[]
        browser.close()


@pytest.mark.parametrize('issue',['path','lines','size','sha','revoked'])
def test_context_rejects_unsafe_or_stale_selections(host_bundle,workbench_html,issue):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        app=mount(page,host_bundle,workbench_html)
        if issue=='path':
            app.get_by_label('上下文文件相对路径').fill('../private')
        else:
            app.get_by_label('上下文文件相对路径').fill('src/example.py')
        if issue=='lines':app.get_by_label('上下文结束行').fill('201')
        if issue=='size':page.evaluate('window.contextLarge=true')
        app.get_by_role('button',name='读取选定片段',exact=True).click()
        if issue in {'sha','revoked'}:
            expect(app.get_by_text('片段已准备，尚未添加到上下文。',exact=True)).to_be_visible()
            page.evaluate('window.contextBadSha=true' if issue=='sha' else 'window.contextRevoked=true')
            app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True).click()
        expect(app.get_by_role('alert')).to_be_visible()
        assert page.evaluate('window.contextUpdates')==[]
        browser.close()


def test_context_unsupported_host_has_visible_fallback(host_bundle,workbench_html):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        app=mount(page,host_bundle,workbench_html,supported=False)
        expect(app.locator('#app')).to_contain_text('当前宿主未提供支持移除通知的上下文附件')
        expect(app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True)).to_have_count(0)
        assert page.evaluate('window.contextUpdates')==[]
        browser.close()


def test_old_context_response_cannot_replace_new_project_selection(host_bundle,workbench_html):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        app=mount(page,host_bundle,workbench_html)
        page.evaluate('window.contextDelayUpdate=true')
        app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True).click()
        page.wait_for_function("window.contextPending.some(p=>p.kind==='update')")
        other={**PROJECT,'id':'b'*32,'alias':'Second selected project'}
        page.evaluate('p=>{window.contextProjects.push(p);window.contextDelayUpdate=false}',other)
        deliver_project(page,other)
        expect(app.get_by_role('heading',name=other['alias'],exact=True)).to_be_visible()
        app.get_by_text('选定上下文',exact=True).click()
        app.get_by_role('button',name='添加项目与选中文件到上下文',exact=True).click()
        page.evaluate("window.contextPending.find(p=>p.kind==='update').resolve()")
        page.wait_for_function('window.contextUpdates.length===3')
        updates=page.evaluate('window.contextUpdates')
        assert updates[1]['content']==[]
        assert json.loads(updates[2]['content'][0]['text'])['project_id']==other['id']
        assert json.loads(page.evaluate('window.currentContext')[0]['text'])['project_id']==other['id']
        browser.close()
