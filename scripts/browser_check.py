"""Render and exercise the real UI against real Hub/Agent test fixtures.

Run: python -m scripts.browser_check --output /tmp/codepier-previews
Requires requirements-dev.txt and Chromium (system browser or Playwright install).
"""
import argparse, json, os, shutil, sys, tempfile, time, uuid
import httpx
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from tests.support import running_stack, wait_for
from shared.util import atomic_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--offline-adapter',action='store_true',help='Render local assets without browser network; use a scoped loopback HTTP test adapter');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report={'screenshots':[],'checks':[],'browser_errors':[],'data':'Generated test repositories; not user projects or a real ChatGPT session.','mode':'offline Chromium component rendering with real loopback API adapter (browser network/cookies/SSE not exercised)' if args.offline_adapter else 'direct HTTP browser integration'}
    with tempfile.TemporaryDirectory(prefix='codepier-browser-') as tmp, running_stack(tmp) as s:
        # A counter on the local fixture proves a lost HTTP reply does not rerun the task.
        s.config['tasks']['browser-once']={'command':[sys.executable,'-u','-c','from pathlib import Path; Path("ui-task-count.txt").open("a").write("x"); print("PASS: browser task executed once")'],'projects':['Imago'],'timeout':15}
        atomic_json(s.config_path,s.config)
        wait_for(lambda:any(t['name']=='browser-once' for t in s.fs('tasks_list')['tasks']),timeout=15)
        faults={'fs_read':1,'fs_write':1,'tasks_run':1,'operation_get':2} if args.offline_adapter else {}
        attempts={}
        for project in ('Imago','Nexus','Lumen'):
            s.mcp('fs_tree',{'project':project,'depth':2})
            s.mcp('fs_read',{'project':project,'path':'src/main.py'})
        task=s.call('tasks_run',{'project':'Nexus','task':'smoke','idempotency_key':uuid.uuid4().hex});s.poll(task['operation_id'])
        with sync_playwright() as p:
            chromium=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
            browser=p.chromium.launch(headless=True,**({'executable_path':chromium} if chromium else {}),args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
            page=context.new_page();page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
            page.on('dialog',lambda d:d.accept())
            def shot(name):
                page.screenshot(path=str(out/name),full_page=True,animations='disabled');report['screenshots'].append(name)
            def check(label,condition=True):
                assert condition,label
                report['checks'].append(label)
            def nav(name):
                if page.viewport_size['width'] <= 900 and not page.locator('.sidebar').evaluate("el=>el.classList.contains('open')"):
                    page.locator('.mobile-menu').click()
                page.locator(f'.nav [data-nav="{name}"]').click()
                page.locator('.skeleton').wait_for(state='detached',timeout=15000)
            if args.offline_adapter:
                # The execution environment disallows all browser URL navigation. Keep that
                # policy intact. Render local strings on about:blank; do not open network
                # sockets from Chromium. A narrow test adapter calls only this fixture Hub.
                ui_client=httpx.Client(base_url=s.url,timeout=35)
                def fixture_fetch(source,path,options):
                    if not isinstance(path,str) or not path.startswith('/api/') or '://' in path:
                        raise ValueError('UI test adapter accepts fixture /api/ paths only')
                    body=json.loads(options.get('body') or '{}')
                    name=body.get('tool') if path=='/api/tools/call' else 'operation_get' if path.startswith('/api/operations/') else path
                    if name in {'fs_read','fs_write','tasks_run'}:
                        attempts.setdefault(name,[]).append(body.get('arguments',{}).get('idempotency_key'))
                    if name=='operation_get' and faults.get(name,0)>0:
                        faults[name]-=1
                        return {'transport_error':True}
                    result=ui_client.request(options.get('method','GET'),path,headers=options.get('headers',{}),content=options.get('body'))
                    if name in {'fs_read','fs_write','tasks_run'} and result.is_success and faults.get(name,0)>0:
                        faults[name]-=1  # execute for real, but lose the reply before the browser sees it
                        return {'transport_error':True}
                    return {'status':result.status_code,'body':result.text}
                page.expose_binding('__fixtureFetch',fixture_fetch)
                page.set_content('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div class="ambient"></div><div id="app"></div><div id="modal-root"></div><div id="toasts"></div></body></html>')
                page.add_style_tag(content=(Path(__file__).resolve().parent.parent/'web/styles.css').read_text())
                page.evaluate('''() => {
                  window.fetch = async (path,options={}) => { const r=await window.__fixtureFetch(path,options); if(r.transport_error)throw new TypeError('Injected temporary transport failure'); return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}}); };
                  window.EventSource=class { constructor(){setTimeout(()=>this.onopen?.(),20);} close(){} };
                }''')
                for asset in ('ui.js','workflows.js','insights.js','skills.js'):
                    page.add_script_tag(content=(Path(__file__).resolve().parent.parent/'web'/asset).read_text())
                source=(Path(__file__).resolve().parent.parent/'web/app.js').read_text()
                source=source.replace('location.origin',json.dumps(s.url)).replace('location.protocol',"'http:'")
                page.add_script_tag(content=source)
            else:
                page.goto(s.url,wait_until='domcontentloaded')
            expect(page.locator('#login-form')).to_be_visible();shot('01-login-desktop.png')
            page.locator('#password').fill(s.password);page.locator('#login-form button').click();expect(page.locator('.stats')).to_be_visible();check('Login form with real administrator credentials');shot('02-overview-desktop.png')
            nav('devices');expect(page.locator('.device-card')).to_have_count(1);check('Real online home Agent displayed');shot('03-devices-desktop.png')
            # Device creation UI + one-time key dialog; no secret-containing screenshot.
            page.locator('[data-action="add-device"]').first.click();page.locator('#device-form [name="name"]').fill('Browser offline node');page.locator('#create-device').click();expect(page.locator('#download-pairing')).to_be_visible();page.locator('[data-action="close-modal"]').first.click();check('Device pairing dialog generated through the panel')
            nav('projects');expect(page.locator('.data-table tbody tr')).to_have_count(3);shot('04-projects-desktop.png')
            page.locator('[data-action="add-project"]').click();page.locator('#project-form [name="alias"]').fill('UI-Mapped');page.locator('#project-form [name="root"]').fill(str(s.imago));page.locator('#save-project').click();expect(page.locator('#modal-root')).to_be_empty();expect(page.locator('.data-table tbody tr')).to_have_count(4);check('Create mapping with live local-directory validation')
            nav('workbench');page.locator('#work-project').select_option(s.project['id']);expect(page.locator('[data-path="src/main.py"]')).to_be_visible();page.locator('[data-action="read-file"][data-path="src/main.py"]').click();expect(page.locator('#code-editor')).to_be_visible();check('Read local source through browser workbench');shot('05-workbench-desktop.png')
            old=page.locator('#code-editor').input_value();page.locator('#code-editor').fill(old+'\n# Verified browser change\n');page.locator('[data-action="preview-save"]').click();expect(page.locator('#confirm-save')).to_be_visible();shot('06-diff-confirmation.png');page.locator('#confirm-save').click();expect(page.locator('#modal-root')).to_be_empty();expect(page.locator('#dirty-state')).to_have_text('已同步');check('Preview + SHA-protected write actually changed home file','Verified browser change' in (s.imago/'src/main.py').read_text())
            if args.offline_adapter:
                check('Lost write response retries with identical idempotency key',len(attempts['fs_write'])==2 and len(set(attempts['fs_write']))==1)
                check('Lost write reply did not create a second local backup',len(s.fs('history_list',path='src/main.py')['backups'])==1)
                check('Read recovers after lost response with same key',attempts['fs_read'][0]==attempts['fs_read'][1])
            page.locator('[data-action="history"]').click();expect(page.locator('[data-action="restore-backup"]')).to_be_visible();page.locator('[data-action="restore-backup"]').first.click();expect(page.locator('#modal-root')).to_be_empty();check('Restore backup actually changed home file back','Verified browser change' not in (s.imago/'src/main.py').read_text())
            page.locator('[data-action="load-tasks"]').click();expect(page.locator('#task-select option[value="browser-once"]')).to_have_count(1);page.locator('#task-select').select_option('browser-once');page.locator('[data-action="run-task"]').click();expect(page.locator('#task-output')).to_contain_text('PASS',timeout=15000);check('Browser starts local allowlisted task and displays real output');check('Real local task counter confirms one execution',(s.imago/'ui-task-count.txt').read_text()=='x')
            if args.offline_adapter:
                check('Lost task submit reply retries the same key',len(attempts['tasks_run'])==2 and len(set(attempts['tasks_run']))==1)
                check('Task polling recovers after two injected fetch failures',faults['operation_get']==0)
            nav('audit');expect(page.locator('.data-table tbody tr')).not_to_have_count(0);shot('07-audit-desktop.png');page.locator('[data-action="operation-detail"]').first.click();expect(page.locator('.modal')).to_be_visible();shot('08-operation-detail.png');page.locator('[data-action="close-modal"]').first.click();check('Audit operation detail rendered')
            nav('connect');expect(page.locator('.tool-chip')).to_have_count(len(s.client.get('/api/settings').json()['tools']));shot('09-mcp-connect-desktop.png');check('Current tool catalog present in the expandable directory')
            page.locator('[data-action="new-grant"]').click();page.locator('#grant-form [name="label"]').fill('UI credential test');page.locator(f'input[name="project"][value="{s.project["id"]}"]').check();page.locator('#create-grant').click();expect(page.locator('#download-token')).to_be_visible();page.locator('[data-action="close-modal"]').first.click();check('Scoped PAT issued by browser; secret omitted from screenshots')
            nav('settings');expect(page.locator('#settings-form')).to_be_visible();shot('10-settings-desktop.png');check('Settings and change-address instructions rendered')
            # Mobile view with existing authenticated context.
            page.set_viewport_size({'width':390,'height':844});page.locator('.mobile-menu').click();nav('overview');expect(page.locator('.stats')).to_be_visible();shot('11-overview-mobile.png');check('390px overview has no document-wide horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('.mobile-menu').click();nav('workbench');expect(page.locator('#code-editor')).to_be_visible();shot('12-workbench-mobile.png');check('390px editor has no document-wide horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('.mobile-menu').click();nav('connect');expect(page.locator('.tool-chip')).to_have_count(len(s.client.get('/api/settings').json()['tools']));shot('13-mcp-connect-mobile.png');check('390px MCP page has no document-wide horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.set_viewport_size({'width':320,'height':568});page.locator('.mobile-menu').click();nav('overview');shot('14-overview-small-mobile.png');check('320px overview has no document-wide horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            check('No uncaught browser JavaScript errors',not report['browser_errors'])
            browser.close()
            if args.offline_adapter: ui_client.close()
    (out/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(report['checks']),'screenshots':len(report['screenshots']),'browser_errors':report['browser_errors']},ensure_ascii=False))

if __name__=='__main__':main()
