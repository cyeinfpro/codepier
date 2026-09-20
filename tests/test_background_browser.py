"""Browser unit contracts plus a real isolated Chrome/native-host/Hub/Agent flow.

The browser test grants only loopback host access in its temporary extension
manifest. It does not approve browser permission dialogs or use a personal profile.
The shipped manifest remains opt-in with optional_host_permissions only.
"""
from __future__ import annotations
import contextlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

import pytest
from playwright.sync_api import sync_playwright, expect
from scripts.install_browser_bridge import install, uninstall
from shared.util import atomic_json
from tests.support import BASE, running_stack, wait_for


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


@pytest.fixture
def website(tmp_path):
    folder=tmp_path/'website';folder.mkdir()
    (folder/'index.html').write_text('''<!doctype html><html><head><title>CodePier controlled form</title></head><body>
      <h1>Controlled browser test</h1><label>Name<input id="name" value="before"></label>
      <label>Password<input id="password" type="password" value="NEVER_RETURN_PASSWORD"></label>
      <label>Plan<select id="plan"><option value="a">A</option><option value="b">B</option></select></label>
      <button id="save">Save once</button><output id="result">0</output><div style="height:2400px">scroll area</div>
      <script>document.querySelector('#name').onkeydown=e=>document.body.dataset.lastKey=e.key;document.querySelector('#save').onclick=()=>{document.querySelector('#result').textContent=String(Number(document.querySelector('#result').textContent)+1)};</script>
    </body></html>''')
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(folder)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


def test_extension_contract_suite_and_javascript_syntax():
    node=shutil.which('node');assert node,'Node is required for extension tests'
    result=subprocess.run([node,'--test',str(BASE/'web/browser-extension/workspace.test.mjs'),str(BASE/'web/browser-extension/background.test.mjs')],cwd=BASE,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    for path in [BASE/'web/integrations.js',BASE/'web/app.js',*list((BASE/'web/browser-extension').glob('*.js'))]:
        checked=subprocess.run([node,'--check',str(path)],capture_output=True,text=True,timeout=15)
        assert checked.returncode==0,checked.stderr


def test_real_extension_native_host_and_mcp_browser_actions(tmp_path,website):
    if os.name=='nt':pytest.skip('Windows needs the separately built native executable; this end-to-end fixture uses the POSIX launcher')
    extension=tmp_path/'extension';shutil.copytree(BASE/'web/browser-extension',extension)
    manifest=json.loads((extension/'manifest.json').read_text());manifest['host_permissions']=['http://127.0.0.1/*']
    (extension/'manifest.json').write_text(json.dumps(manifest))
    profile=tmp_path/'browser-profile'
    options={'channel':'chromium','headless':True,'args':['--disable-extensions-except='+str(extension),'--load-extension='+str(extension)],'viewport':{'width':1280,'height':720}}
    with running_stack(tmp_path/'stack') as s, sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(str(profile),**options)
        try:
            worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
            extension_id=worker.url.split('/')[2]
            state=wait_for(lambda:worker.evaluate("async()=> (await chrome.storage.local.get('codepierWorkspace')).codepierWorkspace"),timeout=10)
            identity=state['profile_id']
        finally:context.close()
        s.stop_agent()
        settings=json.loads(s.config_path.read_text());settings['mcp_policy']={'block_local_codex':True};atomic_json(s.config_path,settings)
        installed=install(s.config_path,extension_id,identity,['Imago'],[website],browser='chromium',root=profile,apply=True)
        s.start_agent()
        grant=s.client.post('/api/grants',json={'label':'Browser isolated fixture','projects':[s.project['id']],'scopes':['read','write','execute','computer'],'days':1}).json()
        context=pw.chromium.launch_persistent_context(str(profile),**options)
        executor=ThreadPoolExecutor(max_workers=1)
        def native_call(name,args=None,*,token=None,expect_failure=False):
            data={'project':'Imago',**(args or {})};data.setdefault('idempotency_key',uuid.uuid4().hex)
            response=s.mcp(name,data,token_value=token or grant['token'])
            result=response['structuredContent']
            if result.get('pending'):
                op=s.poll(result['operation_id'],timeout=40)
                if expect_failure:
                    assert op['state']!='succeeded';return op
                assert op['state']=='succeeded',json.dumps({k:op.get(k) for k in ('id','state','error','result')},ensure_ascii=False)
                return {**op['result']['data'],'operation_id':op['id']}
            if expect_failure:assert response['isError'];return result
            assert not response['isError'],response
            return result
        def call(name,args=None,*,token=None,expect_failure=False):
            # Chromium auto-attaches newly navigated targets through Playwright.
            # Keep that transport pumping while HTTP waits for the native host;
            # otherwise headless targets can remain debugger-paused by the harness.
            print('BROWSER CHECK START',name,flush=True)
            future=executor.submit(native_call,name,args,token=token,expect_failure=expect_failure)
            deadline=time.monotonic()+55
            while not future.done():
                assert time.monotonic()<deadline,'Browser HTTP call did not settle: '+name
                context.pages[0].wait_for_timeout(25)
            result=future.result();print('BROWSER CHECK DONE',name,flush=True);return result
        try:
            worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
            popup=context.new_page();popup.goto('chrome-extension://'+extension_id+'/popup.html');popup.bring_to_front()
            popup.locator('#origin').fill(website);popup.locator('#allow-form button').click()
            expect(popup.locator('#origins')).to_contain_text(website)
            popup.locator('#size').fill('2');popup.locator('#prepare').click()
            expect(popup.locator('#pool')).to_contain_text('2 个标签页',timeout=15000)
            wait_for(lambda:call('browser_status')['connected'],timeout=20)
            original_active=popup.evaluate('async()=> (await chrome.tabs.query({active:true})).map(t=>t.id)')
            opened=call('browser_open',{'url':website+'/index.html'})
            lease=opened['lease_id'];assert opened['focus_changed'] is False
            def navigated_page():
                # Pump Playwright's sync transport while inspecting Chrome's real
                # tab state. time.sleep alone leaves page.url event caches stale.
                tabs=popup.evaluate('async()=>await chrome.tabs.query({})')
                if not any(t.get('url')==website+'/index.html' for t in tabs):return None
                return next((page for page in context.pages if page.url==website+'/index.html'),None)
            owned_page=wait_for(navigated_page)
            owned_page.wait_for_load_state('load')
            snapshot=call('browser_snapshot',{'lease_id':lease})
            assert 'NEVER_RETURN_PASSWORD' not in json.dumps(snapshot)
            name=next(e for e in snapshot['elements'] if e['label']=='Name')
            result=call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'fill','element_id':name['id'],'value':'filled through native bridge'})
            assert result['action_outcome']=='confirmed'
            expect(owned_page.locator('#name')).to_have_value('filled through native bridge')
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'fill','element_id':name['id'],'value':'MUST NOT REPEAT'},expect_failure=True)
            expect(owned_page.locator('#name')).to_have_value('filled through native bridge')
            snapshot=call('browser_snapshot',{'lease_id':lease});save=next(e for e in snapshot['elements'] if e['label']=='Save once')
            key=uuid.uuid4().hex;args={'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'click','element_id':save['id'],'idempotency_key':key}
            call('browser_action',args);call('browser_action',args)
            expect(owned_page.locator('#result')).to_have_text('1')
            snapshot=call('browser_snapshot',{'lease_id':lease});select=next(e for e in snapshot['elements'] if e['tag']=='select')
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'select','element_id':select['id'],'value':'b'})
            expect(owned_page.locator('#plan')).to_have_value('b')
            snapshot=call('browser_snapshot',{'lease_id':lease})
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'scroll','delta_y':200})
            assert owned_page.evaluate('scrollY')==200
            snapshot=call('browser_snapshot',{'lease_id':lease});field=next(e for e in snapshot['elements'] if e['label']=='Name')
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'key','element_id':field['id'],'value':'Escape'})
            expect(owned_page.locator('body')).to_have_attribute('data-last-key','Escape')
            snapshot=call('browser_snapshot',{'lease_id':lease})
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'navigate','value':website+'/index.html?verified=1'})
            expect(owned_page).to_have_url(website+'/index.html?verified=1')
            assert popup.evaluate('async()=> (await chrome.tabs.query({active:true})).map(t=>t.id)')==original_active
            snapshot=call('browser_snapshot',{'lease_id':lease})
            call('browser_action',{'lease_id':lease,'observation_id':snapshot['observation_id'],'action':'navigate','value':'https://not-authorized.invalid/'},expect_failure=True)
            assert owned_page.url==website+'/index.html?verified=1'
            # Another authorized principal still cannot access this lease.
            other=s.client.post('/api/grants',json={'label':'Other browser reader','projects':[s.project['id']],'scopes':['read','computer'],'days':1}).json()
            call('browser_snapshot',{'lease_id':lease},token=other['token'],expect_failure=True)
            s.client.delete('/api/grants/'+other['grant_id'])
            released=call('browser_close',{'lease_id':lease});assert released['tab_cleanup_confirmed']
            assert call('browser_status')['active_leases']==[]
            output=BASE/'docs/evidence/integration-finish-20260917/screenshots';output.mkdir(parents=True,exist_ok=True)
            popup.screenshot(path=str(output/'isolated-browser-extension.png'))
            assert s.agent.poll() is None
        finally:
            executor.shutdown(wait=True,cancel_futures=True)
            context.close();s.stop_agent();uninstall(s.config_path,True)
