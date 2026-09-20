"""Real Apps SDK host + sandbox iframe + real authenticated Hub/Agent tools.

Controlled receipt delivery is delayed to exercise pending/restore paths. The
file edits, immutable snapshots, operations and tool results remain real.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import uuid
import pytest
from playwright.sync_api import sync_playwright,expect
from tests.support import BASE
from tests.test_integrations_stack import integrated_stack,resolved

@pytest.fixture(scope='module')
def host_bundle(tmp_path_factory):
    path=tmp_path_factory.mktemp('apps-host')/'host.js'
    esbuild=BASE/'web/mcp-apps/node_modules/esbuild/lib/main.js'
    assert esbuild.is_file(),'Install the locked Apps dependencies with npm ci --ignore-scripts'
    code='const {build}=require(process.argv[1]);build({entryPoints:[process.argv[2]],outfile:process.argv[3],bundle:true,platform:"browser",format:"iife"}).catch(e=>{console.error(e);process.exit(1)})'
    result=subprocess.run([shutil.which('node'),'-e',code,str(esbuild),str(BASE/'tests/fixtures/mcp-apps-host.js'),str(path)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    return path

@pytest.mark.parametrize('width',[1100,390])
def test_sdk_cards_restore_pending_and_immutable_paginated_review(integrated_stack,host_bundle,width):
    s=integrated_stack;calls=[]
    opened=s.mcp('open_workspace',{'project':'Imago','capture_baseline':True})
    data=opened['structuredContent']
    if data.get('pending'):data=s.poll(data['operation_id'],timeout=30)['result']['data']
    fixture='apps-host-'+str(width)+'.txt'
    (s.imago/fixture).write_text(''.join(f'row {i:04d} immutable original text '+('x'*50)+'\n' for i in range(500))+'<img src=x onerror="window.INJECTED=true">\nEND_OF_FROZEN_REVIEW\n')
    review=s.mcp('show_changes',{'project':'Imago','baseline_ref':data['baseline_ref']})
    frozen=review['structuredContent']
    if frozen.get('pending'):frozen=s.poll(frozen['operation_id'],timeout=30)['result']['data']
    (s.imago/fixture).write_text('LATER_CONTENT_MUST_NOT_REPLACE_FROZEN_REVIEW\n')
    def host_tool(params):
        calls.append(copy.deepcopy(params))
        response=s.mcp(params['name'],params.get('arguments',{}))
        # Deliver the legitimate initial receipt late, even when already settled.
        # This tests a host replay of an earlier pending projection, not fake data.
        if params['name']=='show_changes' and not response['structuredContent'].get('pending'):
            op=response['structuredContent']['operation_id']
            response={**response,'structuredContent':{'pending':True,'operation_id':op}}
        return response
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page(viewport={'width':width,'height':1000});errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.expose_function('codepierHostTool',host_tool)
        page.set_content('<!doctype html><html><body style="margin:0"></body></html>')
        page.add_script_tag(path=str(host_bundle))
        page.evaluate('html=>window.codepierMount(html)',(BASE/'web/mcp-apps/workspace-v1.html').read_text())
        page.wait_for_function('window.codepierHostReady')
        page.evaluate('v=>window.codepierDeliver(v)',{'args':{'project':'Imago'},'result':opened})
        app=page.frame_locator('#app-frame');expect(app.locator('h1')).to_have_text('Imago')
        app.get_by_text('项目与权限',exact=True).click()
        app.get_by_role('button',name='检查项目就绪状态').click()
        expect(app.locator('#app')).to_contain_text('native_model_turn',timeout=30000)
        assert any(p['name']=='readiness_get' for p in calls)
        page.evaluate('html=>window.codepierMount(html)',(BASE/'web/mcp-apps/changes-v1.html').read_text())
        page.wait_for_function('window.codepierHostReady')
        restored=copy.deepcopy(review);restored['structuredContent']={'review_ref':frozen['review_ref'],'summary':frozen['summary'],'coverage':frozen['coverage']}
        restored['_meta']={'com.codepier/binding':{'kind':'changes','project':'Imago','review_ref':frozen['review_ref']}}
        page.evaluate('v=>window.codepierDeliver(v)',{'args':{'project':'Imago','review_ref':frozen['review_ref']},'result':restored})
        expect(app.get_by_role('heading',name='本轮改动')).to_be_visible()
        app.get_by_role('button',name='读取文件清单').click()
        app.locator('summary').filter(has_text=fixture).click()
        expect(app.locator('pre')).to_contain_text('immutable original text',timeout=30000)
        # Await the completed page, not a fixed 150 ms. Otherwise the loop can
        # re-click the old disabled button while its receipt is still in flight;
        # the button is removed on the final page and that click waits forever.
        for _ in range(20):
            if 'END_OF_FROZEN_REVIEW' in app.locator('pre').inner_text():
                break
            previous=app.locator('pre').inner_text()
            app.get_by_role('button',name='继续读取差异').click()
            expect(app.locator('pre')).not_to_have_text(previous,timeout=30000)
        expect(app.locator('pre')).to_contain_text('END_OF_FROZEN_REVIEW')
        expect(app.locator('pre')).not_to_contain_text('LATER_CONTENT_MUST_NOT')
        assert app.locator('img').count()==0
        assert all('baseline_ref' not in c['arguments'] for c in calls if c['name']=='show_changes')
        assert any(c['name']=='operations_wait' for c in calls)
        assert not page.evaluate('window.codepierHostErrors') and not errors
        folder=BASE/'docs/evidence/workspace-dashboard-20260917/screenshots';folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'apps-review-{width}.png'),full_page=True)
        browser.close()
