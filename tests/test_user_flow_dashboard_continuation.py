"""F29/F30: the shipped Apps SDK resource, real iframe visibility and timers."""
import time

from playwright.sync_api import expect
from tests.test_mcp_apps_host import host_bundle  # noqa: F401
from tests.test_workspace_dashboard import mount, response, fixture_workspace, fixture_task


def test_empty_dashboard_discovers_a_new_task_without_manual_refresh(host_bundle, chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    calls=[];started=False;task=fixture_task('a'*32,'Created after opening')
    def host_tool(params):
        calls.append(params['name'])
        assert params['name']=='workspace_status'
        return response(fixture_workspace(workflows=[task] if started else []))
    page.expose_function('codepierHostTool',host_tool)
    try:
        app=mount(page,host_bundle,response({'workspace':{'project':'Imago','project_id':'p'}}))
        expect(app.locator('.task-hero')).to_contain_text('还没有可见任务')
        started=True
        expect(app.get_by_label('选择任务').locator('option')).to_have_count(2,timeout=15000)
        expect(app.get_by_label('自动刷新任务状态')).to_be_checked()
        assert len(calls)>=2
        assert not page.evaluate('window.codepierHostErrors')
    finally:page.context.close()


def test_hidden_log_following_resumes_when_the_card_becomes_visible(host_bundle, chat_browser_pool):
    page=chat_browser_pool('chromium').new_page()
    task=fixture_task('a'*32,'Running fixture');operation='b'*32;calls=[]
    state={'output':'initial fixture log','seq':1}
    evidence=[{'operation_id':operation,'tool':'shell_exec','state':'running','pending':True,'updated':time.time()}]
    def host_tool(params):
        calls.append(params['name'])
        if params['name']=='workspace_status':return response(fixture_workspace(task,evidence,[task]))
        assert params['name']=='operations_get'
        return response({'id':operation,'operation_id':operation,'tool':'shell_exec','project_id':'p',
            'state':'running','pending':True,'args_summary':{},'output':state['output'],'output_seq':state['seq']})
    page.expose_function('codepierHostTool',host_tool)
    try:
        app=mount(page,host_bundle,response({'workspace':{'project':'Imago','project_id':'p'},'workflow_id':task['workflow_id']}))
        app.get_by_role('button',name='查看日志与回执').click()
        expect(app.locator('.inspector > pre')).to_have_text(state['output'])
        frame=page.locator('#app-frame').element_handle().content_frame()
        frame.evaluate('''() => {Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
          document.dispatchEvent(new Event('visibilitychange'));}''')
        before=calls.count('operations_get')
        page.wait_for_timeout(2200)
        assert calls.count('operations_get')==before
        state.update(output='new log on visibility restoration',seq=2)
        frame.evaluate('''() => {Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});
          document.dispatchEvent(new Event('visibilitychange'));}''')
        expect(app.locator('.inspector > pre')).to_have_text(state['output'])
        assert calls.count('operations_get')>before
        expect(app.locator('.inspector input[type=checkbox]')).to_be_checked()
        assert not page.evaluate('window.codepierHostErrors')
    finally:page.context.close()
