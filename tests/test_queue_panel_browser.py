"""Real Chromium/WebKit checks for the shipped queue view, using synthetic data only."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.browser_launch import launch_browser

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const S={session:{user_id:'owner'},space_id:'space',page:'audit',auditMode:'operations',renderSeq:0,auditSource:'',auditStatus:'',auditQuery:'',auditOffset:0};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=v=>JSON.stringify(v,null,2), badge=s=>'<span class="badge">'+esc(s)+'</span>';
const CP={ui:{confirmSheet:()=>{window.confirmCount++;return new Promise(resolve=>window.decide=resolve);}}};
const toast=m=>window.lastToast=m, copy=async value=>{window.copied=value;};
window.confirmCount=0;window.toolCalls=[];window.readCalls=[];
window.queueData={diagnostics_available:true,operations:[
 {id:'hub',tool:'exec',state:'queued',queue_stage:'hub_queue',summary:'npm test <img src=x onerror=alert(1)>',alias:'Fixture',device_name:'Node',can_cancel:true,waiting:{reason:'hub_delivery',queue_age_ms:12000,blocked_by:[]}},
 {id:'reader',tool:'read',state:'queued',queue_stage:'agent_waiting',summary:'path=README.md',alias:'Fixture',can_cancel:true,waiting:{reason:'resource_conflict',lane:'read',queue_age_ms:14000,blocked_by:[{operation_id:'writer',tool:'exec',state:'running',elapsed_s:24,summary:'npm test'}],blocker_visibility:'restricted_or_unavailable'}},
 {id:'writer',tool:'exec',state:'running',queue_stage:'running',summary:'npm test',alias:'Fixture',can_cancel:true}
]};
window.report={contention:{operations:8,waits_over_10s:2,convoy_ratio:.25,alerts:{operations_over_60s:['reader'],projects_over_5:[],holders_over_5:[]}},metrics:{worker:{queue_ms:{p50:1,p99:5}},lock:{wait_ms:{p50:0,p99:2}}}};
const api=async(path,options={})=>{
 window.readCalls.push(path);
 if(window.apiError){const error=new Error('权限已失效');error.status=window.apiError;throw error;}
 if(path==='/api/queue-diagnostics')return window.holdReport ? new Promise(resolve=>window.finishReport=resolve) : structuredClone(window.report);
 if(path.startsWith('/api/call-log'))return {operations:[],next_cursor:null,projects:[],tools:[]};
 return structuredClone(window.queueData);
};
const tool=async(name,args)=>{window.toolCalls.push({name,args});return window.holdTool ? new Promise(resolve=>window.finishTool=resolve) : {pending:true};};
const ACTIVE_STATES=['queued','running','reconnecting','cancelling','unknown'];
const TERMINAL_STATES=['succeeded','failed','cancelled','needs_review','interrupted'];
const stateNames={},icon=()=>'',empty=t=>'<p>'+esc(t)+'</p>',heading=(t,s,b,actions='')=>'<h1>'+esc(t)+'</h1>'+actions,uiHelp=()=>'',download=()=>{};
'''


@pytest.fixture(params=["chromium", "webkit"])
def queue_page(request):
    with sync_playwright() as playwright:
        browser = launch_browser(getattr(playwright, request.param), headless=True)
        page = browser.new_page(viewport={"width":1200,"height":900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content('<html lang="zh-CN"><body><main id="page"></main><div id="details"></div></body></html>')
        page.add_script_tag(content=HARNESS)
        for name in ("tokens.css", "styles.css", "call-log.css", "queue.css"):
            page.add_style_tag(path=str(ROOT / "web" / name))
        page.add_script_tag(path=str(ROOT / "web/queue.js"))
        page.evaluate("document.querySelector('#page').innerHTML=CodePierQueue.placeholder();CodePierQueue.bind();CodePierQueue.setLive(false)")
        page.locator("[data-queue-id=reader]").wait_for()
        yield page
        page.evaluate("CodePierQueue.detach();window.CodePierCallLog?.clear()")
        assert not errors
        browser.close()


def test_three_stages_blockers_xss_mobile_and_safe_run_entry(queue_page, tmp_path):
    page = queue_page
    assert page.locator(".queue-column").count() == 3
    assert page.locator(".queue-column").nth(0).locator("[data-queue-id=hub]").count() == 1
    assert page.locator(".queue-column").nth(1).locator("[data-queue-id=reader]").count() == 1
    assert page.locator(".queue-column").nth(2).locator("[data-queue-id=writer]").count() == 1
    assert "24 秒" in page.locator("[data-queue-id=reader]").inner_text()
    assert "部分阻塞操作受限或不可用" in page.locator("[data-queue-id=reader]").inner_text()
    assert not page.locator("img").count()
    page.evaluate("""document.querySelector('#details').innerHTML=CodePierQueue.liveHTML({concurrency:{mode:'live',overlapping_writers:[{operation_id:'writer',tool:'exec'}]}})+CodePierQueue.runHTML({run_directory:'.codepier/runs/run-1',snapshot_artifacts:['build/<x>.txt']})""")
    assert "不是一致快照" in page.locator("#details").inner_text()
    assert "可见并行写入" in page.locator("#details").inner_text()
    page.get_by_role("button", name="复制相对路径").click()
    assert page.evaluate("window.copied") == ".codepier/runs/run-1"
    assert page.evaluate("CodePierQueue.runHTML({run_directory:'.codepier/runs/../../secret'})") == ""
    assert not page.locator("#details a").count()
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(tmp_path/"queue-mobile.png"), full_page=True)


def test_cancel_repeated_clicks_one_request_and_navigation_guard(queue_page):
    page = queue_page
    button = page.locator("[data-queue-cancel=hub]")
    button.click()
    assert button.is_disabled()
    page.evaluate("document.querySelector('[data-queue-cancel=hub]').click()")
    assert page.evaluate("window.confirmCount") == 1
    page.evaluate("window.decide(true)")
    page.wait_for_function("window.toolCalls.length===1")
    assert page.evaluate("window.toolCalls[0]") == {"name":"operations_cancel","args":{"operation_id":"hub"}}
    page.wait_for_function("!document.querySelector('[data-queue-cancel=reader]').disabled")
    page.locator("[data-queue-cancel=reader]").click()
    page.evaluate("S.space_id='new-space';CodePierQueue.detach();window.decide(true)")
    assert page.evaluate("window.toolCalls.length") == 1


def test_refresh_preserves_focused_evidence_and_denial_clears_view(queue_page):
    page = queue_page
    button = page.locator("[data-queue-cancel=reader]")
    button.focus()
    page.evaluate("window.focused=document.activeElement;window.queueData.operations[1].summary='new summary';CodePierQueue.refresh()")
    page.wait_for_function("window.readCalls.length>=2")
    assert page.evaluate("window.focused===document.activeElement && window.focused.isConnected")
    assert "new summary" not in page.locator("[data-queue-id=reader]").inner_text()
    page.evaluate("document.activeElement.blur();CodePierQueue.refresh()")
    page.wait_for_function("document.querySelector('[data-queue-id=reader]').textContent.includes('new summary')")
    page.evaluate("window.apiError=403;CodePierQueue.refresh()")
    page.wait_for_function("!document.querySelector('[data-queue-content]').firstChild")
    assert not page.locator("[data-queue-report]").count()


def test_admin_diagnostics_and_role_revocation(queue_page):
    page = queue_page
    page.get_by_text("实例队列诊断（24 小时）",exact=True).click()
    page.get_by_role("button",name="读取诊断").click()
    page.wait_for_function("document.querySelector('[data-queue-report-content]').textContent.includes('25.0%')")
    assert "等待超过 60 秒 1" in page.locator("[data-queue-report-content]").inner_text()
    page.evaluate("window.queueData.diagnostics_available=false;CodePierQueue.refresh()")
    page.wait_for_function("!document.querySelector('[data-queue-diagnostics]').firstChild")


def test_call_log_mount_pause_and_detach_queue(queue_page):
    page = queue_page
    page.add_script_tag(path=str(ROOT/"web/call-log.js"))
    page.evaluate("""async()=>{CodePierQueue.detach();document.querySelector('#page').innerHTML=await CodePierCallLog.html(++S.renderSeq);CodePierCallLog.bind();}""")
    page.locator("[data-queue-id=reader]").wait_for()
    page.get_by_role("button",name="暂停实时更新").click()
    before = page.evaluate("window.readCalls.filter(x=>x==='/api/operations/queue').length")
    page.clock.install()
    page.clock.fast_forward(6000)
    assert page.evaluate("window.readCalls.filter(x=>x==='/api/operations/queue').length") == before
    page.evaluate("CodePierCallLog.detach();document.querySelector('#page').replaceChildren()")
    page.clock.fast_forward(10000)
    assert page.evaluate("window.readCalls.filter(x=>x==='/api/operations/queue').length") == before


def test_late_diagnostic_cannot_restore_revoked_admin_view(queue_page):
    page = queue_page
    page.evaluate("window.holdReport=true")
    page.get_by_text("实例队列诊断（24 小时）",exact=True).click()
    page.get_by_role("button",name="读取诊断").click()
    page.wait_for_function("typeof window.finishReport==='function'")
    page.evaluate("window.queueData.diagnostics_available=false;CodePierQueue.refresh()")
    page.wait_for_function("!document.querySelector('[data-queue-diagnostics]').firstChild")
    page.evaluate("window.finishReport(window.report)")
    page.wait_for_timeout(30)
    assert not page.locator("[data-queue-report-content]").count()


def test_old_cancel_completion_preserves_new_mount_duplicate_guard(queue_page):
    page = queue_page
    page.evaluate("window.holdTool=true")
    page.locator("[data-queue-cancel=hub]").click()
    page.evaluate("window.decide(true)")
    page.wait_for_function("typeof window.finishTool==='function'")
    page.evaluate("CodePierQueue.detach();document.querySelector('#page').innerHTML=CodePierQueue.placeholder();CodePierQueue.bind()")
    page.locator("[data-queue-cancel=hub]").click()
    assert page.evaluate("window.confirmCount") == 2
    page.evaluate("window.finishTool({pending:true})")
    page.wait_for_timeout(30)
    page.evaluate("document.activeElement.blur();CodePierQueue.refresh()")
    page.wait_for_function("window.readCalls.length>=3")
    assert page.locator("[data-queue-cancel=hub]").is_disabled()
    page.evaluate("window.decide(false)")
