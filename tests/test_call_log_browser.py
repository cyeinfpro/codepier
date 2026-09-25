"""Offline browser interaction tests of the shipped call-log component."""
import copy
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const S={session:{user_id:'owner'},page:'audit',auditMode:'operations',renderSeq:0,auditSource:'',auditStatus:'',auditQuery:'',auditOffset:0};
const ACTIVE_STATES=['queued','running','reconnecting','cancelling','unknown'];
const TERMINAL_STATES=['succeeded','failed','cancelled','needs_review','interrupted'];
const stateNames={running:'执行中',succeeded:'已完成',failed:'失败'};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=v=>JSON.stringify(v,null,2),icon=()=>'',badge=s=>`<span class="badge">${esc(stateNames[s]||s)}</span>`;
const empty=t=>`<p>${esc(t)}</p>`,heading=(t,s,b,actions='')=>`<h1>${esc(t)}</h1>${actions}`,uiHelp=(t,b)=>`<details><summary>${esc(t)}</summary><p>${esc(b)}</p></details>`;
const download=(name,content,type)=>{window.exported={name,content,type};};
const toast=m=>{window.lastToast=m;},copy=v=>{window.lastCopied=v;};
const api=async(path,options={})=>window.readCallLog(path);
async function renderPage(){const seq=++S.renderSeq;const html=await CodePierCallLog.html(seq);document.querySelector('#page').innerHTML=html;CodePierCallLog.bind();}
'''


@pytest.fixture
def log_page():
    requests=[]
    rows=[{'id':'a'*32,'tool':'shell_exec','actor':'mcp:test:ChatGPT','state':'running','created':100,'updated':101,'elapsed_ms':1000,'summary':'command=pytest -q <img src=x onerror=alert(1)>','alias':'MCP','device_name':'Node'},
          {'id':'b'*32,'tool':'fs_read','actor':'panel:admin','state':'succeeded','created':90,'updated':91,'elapsed_ms':1000,'summary':'path=README.md','alias':'MCP','device_name':'Node'}]
    details={rows[0]['id']:{**rows[0],'attempts':1,'args_summary':{'command':'pytest -q','env':'<redacted>'},'output':'output line\npassword=<redacted>','result':{'ok':True,'data':{'exit_code':0}},'timing':{'wait_ms':20,'execution_ms':150},'trace':{'current':{'reason':'本机正在执行'},'events':[]}}}
    with sync_playwright() as p:
        executable=shutil.which('chromium')
        browser=p.chromium.launch(headless=True,**({'executable_path':executable} if executable else {}))
        page=browser.new_page(viewport={'width':1100,'height':900})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def read(path):
            requests.append(path)
            if urlsplit(path).path=='/api/call-log':
                return {'operations':copy.deepcopy(rows),'next_cursor':None,'projects':[{'id':'p1','alias':'MCP'}],'tools':['fs_read','shell_exec']}
            return copy.deepcopy(details.get(path.rsplit('/',1)[-1],{}))
        page.expose_function('readCallLog',read)
        page.set_content('<html lang="zh-CN"><body><main id="page"></main></body></html>')
        page.add_script_tag(content=HARNESS)
        page.add_style_tag(path=str(ROOT/'web/call-log.css'))
        page.add_script_tag(path=str(ROOT/'web/call-log.js'))
        page.evaluate('renderPage()')
        yield page,rows,details,requests
        page.evaluate('CodePierCallLog.clear()')
        assert not errors
        browser.close()


def test_native_expansion_copy_xss_and_mobile(log_page):
    page,rows,details,requests=log_page
    entry=page.locator('[data-call-id]').first
    assert not page.locator('img').count()
    entry.locator('summary').first.focus()
    page.keyboard.press('Enter')
    page.get_by_text('实际执行（本次 Agent 计时）',exact=True).wait_for()
    assert len([u for u in requests if u.endswith(rows[0]['id'])])==1
    entry.get_by_role('button',name='复制调用详情').click()
    assert '<redacted>' in page.evaluate('window.lastCopied')
    assert 'pytest -q' in page.evaluate('window.lastCopied')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    entry.locator('summary').first.click()
    entry.locator('summary').first.click()
    assert len([u for u in requests if u.endswith(rows[0]['id'])])==1


def test_live_updates_do_not_move_open_rows_and_pause(log_page):
    page,rows,details,requests=log_page
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    before=entry.bounding_box()['y']
    rows.insert(0,{**rows[1],'id':'c'*32,'created':200})
    rows[1]['state']='failed'
    details['a'*32]['state']='failed'
    details['a'*32]['error']='command failed'
    page.evaluate('CodePierCallLog.refresh(true)')
    assert page.locator('[data-call-id]').count()==2
    assert entry.bounding_box()['y']==before
    assert entry.get_attribute('open') is not None
    assert page.locator('#call-new').is_visible()
    assert '失败' in entry.locator('.call-state').inner_text()
    page.get_by_role('button',name='暂停实时更新').click()
    count=len(requests)
    page.evaluate('CodePierCallLog.refresh(true)')
    assert len(requests)==count
    page.get_by_role('button',name='列表有更新，显示最新').click()
    page.wait_for_function('document.querySelectorAll("[data-call-id]").length===3')


def test_filters_and_search_reset_pagination(log_page):
    page,_,_,requests=log_page
    page.locator('#call-project').select_option('p1')
    page.wait_for_function('S.callLog.project==="p1" && document.querySelector("#call-project").value==="p1"')
    page.locator('#audit-query').fill('path/a_%.py')
    page.locator('#call-search').get_by_role('button',name='搜索',exact=True).click()
    page.wait_for_function('S.auditQuery==="path/a_%.py"')
    page.wait_for_timeout(100)
    query=parse_qs(urlsplit(requests[-1]).query)
    assert query['q']==['path/a_%.py']
    assert query['project']==['p1']
    assert page.evaluate('S.callLog.history.length')==0


def test_output_scroll_and_nested_folds_survive_update(log_page):
    page,rows,details,_=log_page
    data=details[rows[0]['id']]
    data['output']='\n'.join(f'line {i}' for i in range(1000))
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    output=entry.locator('[data-call-section="output"]')
    output.locator('summary').click()
    output.locator('pre').evaluate('(el)=>{el.scrollTop=500}')
    data['output']+='\nnew output'
    data['updated']=102
    page.evaluate('CodePierCallLog.refresh(true)')
    assert output.get_attribute('open') is not None
    assert output.locator('pre').evaluate('(el)=>el.scrollTop')==500


def test_automatic_latest_when_not_reading_and_session_clear(log_page):
    page,rows,details,_=log_page
    rows.insert(0,{**rows[1],'id':'c'*32,'created':200})
    page.evaluate('CodePierCallLog.refresh(true)')
    assert page.locator('[data-call-id]').count()==3
    assert '3 项' in page.locator('#call-count').inner_text()
    page.evaluate("S.session={user_id:'another'};CodePierCallLog.clear();document.querySelector('#page').replaceChildren()")
    assert page.locator('[data-call-id]').count()==0
    assert page.evaluate('S.callLog===null')


def test_manual_detail_refresh_and_truncated_trace_are_usable(log_page):
    page,rows,details,_=log_page
    data=details[rows[0]['id']]
    data['state']='succeeded'
    data['output']='old output'
    data['trace']['events']=[{'stage':'waiting_project','source':'agent','blocked_by':'<truncated>'}, '<truncated>']
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    data['output']='new output after manual refresh'
    entry.get_by_role('button',name='刷新详情',exact=True).click()
    page.wait_for_function('document.querySelector("[data-call-section=output] pre").textContent.includes("new output")')
    assert entry.get_by_role('button',name='刷新详情',exact=True).evaluate('(el)=>el===document.activeElement')


def test_network_error_preserves_rows_and_retry(log_page):
    page,_,_,_=log_page
    entry=page.locator('[data-call-id]').first
    page.evaluate('window.savedRead=window.readCallLog;window.readCallLog=()=>Promise.reject(new Error("offline"));void 0')
    entry.locator('summary').first.click()
    entry.get_by_role('button',name='重试详情').wait_for()
    assert page.locator('[data-call-id]').count()==2
    page.evaluate('CodePierCallLog.refresh(true)')
    assert '连接暂不可用' in page.locator('#call-sync').inner_text()
    page.evaluate('window.readCallLog=window.savedRead;void 0')
    entry.get_by_role('button',name='重试详情').click()
    entry.locator('.call-facts').wait_for()


def test_late_detail_after_logout_cannot_repopulate_the_page(log_page):
    page,_,_,_=log_page
    page.evaluate('window.readCallLog=()=>new Promise(resolve=>window.resolveOldDetail=resolve);void 0')
    page.locator('[data-call-id]').first.locator('summary').first.click()
    page.wait_for_function('typeof window.resolveOldDetail==="function"')
    page.evaluate('CodePierCallLog.clear();S.session=null;document.querySelector("#page").innerHTML="signed out";window.resolveOldDetail({id:"a".repeat(32),args_summary:{command:"private old command"}})')
    page.wait_for_timeout(30)
    assert page.locator('#page').inner_text()=='signed out'


def test_export_is_explicitly_limited_to_the_visible_sanitized_page(log_page):
    page,rows,_,_=log_page
    page.get_by_role('button',name='导出本页调用',exact=True).click()
    exported=page.evaluate('JSON.parse(window.exported.content)')
    assert exported['scope']=='visible_page' and len(exported['calls'])==len(rows)
    assert 'args_summary' not in exported['calls'][0]
