"""Real Chromium/WebKit checks for the shipped queue view, using synthetic data only."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

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
    assert page.locator("[data-queue-stage]").count() == 4
    assert page.locator("[data-queue-stage=hub_queue] strong").inner_text() == "1"
    assert page.locator("[data-queue-stage=agent_waiting] strong").inner_text() == "1"
    assert page.locator("[data-queue-stage=running] strong").inner_text() == "1"
    assert page.locator(".queue-list > [data-queue-id]").count() == 3
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


@pytest.fixture(params=["chromium", "webkit"])
def queue_design_page(request):
    """Exercise the actual bundled confirmation layer rather than the test stub."""
    with sync_playwright() as playwright:
        browser = launch_browser(getattr(playwright, request.param), headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content('<html lang="zh-CN"><body><main id="page"></main><div id="toasts"></div></body></html>')
        page.add_script_tag(path=str(ROOT / "web/core/bundle.js"))
        harness = "\n".join(line for line in HARNESS.splitlines()
                            if not line.startswith("const CP="))
        page.add_script_tag(content=harness)
        for name in ("tokens.css", "styles.css", "call-log.css", "queue.css"):
            page.add_style_tag(path=str(ROOT / "web" / name))
        page.add_script_tag(path=str(ROOT / "web/queue.js"))
        page.evaluate("document.querySelector('#page').innerHTML=CodePierQueue.placeholder();CodePierQueue.bind();CodePierQueue.setLive(false)")
        page.locator("[data-queue-id=reader]").wait_for()
        try:
            yield page
            assert not errors
        finally:
            page.evaluate("CodePierQueue.detach()")
            browser.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_queue_real_sheet_cancel_focus_and_single_submission(queue_design_page, scheme, tmp_path):
    page = queue_design_page
    page.evaluate("scheme => document.documentElement.dataset.appearance=scheme", scheme)
    button = page.locator("[data-queue-cancel=hub]")
    button.click()
    sheet = page.locator("dialog.ui-sheet")
    sheet.wait_for()
    assert "已发生的副作用不会回滚" in sheet.inner_text()
    expect(sheet.get_by_role("button", name="取消", exact=True)).to_be_focused()
    page.evaluate("document.querySelector('[data-queue-cancel=hub]').click()")
    assert sheet.count() == 1
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    expect(sheet).to_have_css("opacity", "1")
    page.screenshot(path=str(tmp_path / ("queue-sheet-" + scheme + ".png")))
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('dialog.ui-sheet')")
    assert button.is_enabled()
    expect(button).to_be_focused()
    assert page.evaluate("window.toolCalls") == []
    button.click()
    page.evaluate("window.holdTool=true")
    sheet.get_by_role("button", name="请求取消", exact=True).click()
    page.wait_for_function("window.toolCalls.length===1")
    assert button.is_disabled()
    page.evaluate("document.querySelector('[data-queue-cancel=hub]').click()")
    assert page.locator("dialog.ui-sheet").count() == 0
    assert page.evaluate("window.toolCalls.length") == 1
    page.evaluate("window.finishTool({pending:true})")
    page.wait_for_function("!document.querySelector('[data-queue-cancel=hub]').disabled")
    assert "等待执行回执确认结果" in page.evaluate("window.lastToast")


@pytest.mark.parametrize("change", [
    "S.space_id='changed'",
    "S.session={user_id:'another'}",
    "S.page='overview'",
    "CodePierQueue.detach();document.querySelector('#page').replaceChildren()",
])
def test_queue_real_sheet_rechecks_context(queue_design_page, change):
    page = queue_design_page
    page.locator("[data-queue-cancel=hub]").click()
    page.evaluate(change)
    page.locator("dialog.ui-sheet").get_by_role("button", name="请求取消", exact=True).click()
    page.wait_for_function("!document.querySelector('dialog.ui-sheet')")
    assert page.evaluate("window.toolCalls") == []


def test_queue_missing_confirmation_component_fails_closed(queue_design_page):
    page = queue_design_page
    page.evaluate("() => {CP={ui:{}};window.nativeConfirmCalls=0;window.confirm=()=>{window.nativeConfirmCalls++;return true}}")
    page.locator("[data-queue-cancel=hub]").click()
    page.wait_for_function("!document.querySelector('[data-queue-cancel=hub]').disabled")
    assert page.evaluate("window.nativeConfirmCalls") == 0
    assert page.evaluate("window.toolCalls") == []
    assert "确认组件" in page.evaluate("window.lastToast")


def test_queue_stage_filters_are_keyboard_accessible_and_keep_totals(queue_page):
    page = queue_page
    stage = page.locator('[data-queue-stage="agent_waiting"]')
    stage.focus()
    page.keyboard.press("Enter")
    expect(page.locator('[data-queue-stage="agent_waiting"]')).to_be_focused()
    expect(page.locator('[data-queue-stage="agent_waiting"]')).to_have_attribute("aria-pressed", "true")
    assert page.locator("[data-queue-id]").count() == 1
    assert page.locator("[data-queue-id=reader]").count() == 1
    assert page.locator('[data-queue-stage=""] strong').inner_text() == "3"
    page.locator('[data-queue-stage=""]').click()
    assert page.locator("[data-queue-id]").count() == 3
    page.evaluate("document.activeElement.blur();window.queueData.operations=window.queueData.operations.filter(x=>x.queue_stage==='running');CodePierQueue.refresh()")
    page.wait_for_function("document.querySelectorAll('[data-queue-id]').length===1")
    page.locator('[data-queue-stage="hub_queue"]').click()
    assert page.locator(".queue-empty").inner_text().startswith("此阶段暂无操作")
    assert page.locator('[data-queue-stage="running"] strong').inner_text() == "1"
    page.locator('[data-queue-stage=""]').click()
    assert page.locator("[data-queue-id=writer]").count() == 1
    page.evaluate("document.activeElement.blur();window.queueData.operations=[];CodePierQueue.refresh()")
    page.wait_for_function("document.querySelector('.queue-empty')?.textContent.includes('当前没有活动操作')")
    assert page.locator("[data-queue-id]").count() == 0
    assert page.locator('[data-queue-stage=""] strong').inner_text() == "0"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 390, 320])
def test_queue_long_commands_use_available_width_and_bounded_height(queue_page, scheme, width, tmp_path):
    page = queue_page
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate(r"""scheme => {
        document.documentElement.dataset.appearance=scheme;
        const base=window.queueData.operations[2];
        window.longSummary="command=python - <<'PY'\n" + "print('synthetic evidence <script>')\n".repeat(60);
        window.queueData.operations=[{...base,id:'first',alias:'Atlas',device_name:'Studio',summary:window.longSummary},
            {...base,id:'second',alias:'Console',device_name:'Studio',summary:window.longSummary}];
        CodePierQueue.refresh();
    }""", scheme)
    page.locator("[data-queue-id=first]").wait_for()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    dimensions = page.evaluate("""() => {
        const row=document.querySelector('.queue-item').getBoundingClientRect();
        const list=document.querySelector('.queue-list').getBoundingClientRect();
        return {ratio:row.width/list.width, rowHeight:row.height, panel:document.querySelector('.queue-panel').getBoundingClientRect().height};
    }""")
    assert dimensions["ratio"] > .90
    assert dimensions["rowHeight"] < (200 if width > 680 else 280)
    assert dimensions["panel"] < (560 if width > 680 else 740)
    assert not page.locator(".queue-column").count()
    assert not page.locator("[data-queue-id=first] pre").is_visible()
    assert page.locator(".queue-summary").first.evaluate("el=>el.scrollHeight>el.clientHeight")
    page.screenshot(path=str(tmp_path / f"queue-redesign-{scheme}-{width}.png"), full_page=True)
    summary = page.locator("[data-queue-id=first] summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.locator("[data-queue-id=first] pre")).to_be_visible()
    assert page.locator("[data-queue-id=first] pre").inner_text() == page.evaluate("window.longSummary")
    assert page.locator("[data-queue-id=first] script").count() == 0
    page.get_by_role("button", name="刷新队列", exact=True).click()
    expect(page.locator("[data-queue-id=first] details")).to_have_attribute("open", "")
    page.locator("[data-queue-id=first] summary").click()
    assert not page.locator("[data-queue-id=first] pre").is_visible()


def test_queue_list_scroll_and_open_command_survive_refresh(queue_page):
    page = queue_page
    page.evaluate(r"""() => {
        const base=window.queueData.operations[2];
        window.queueData.truncated=true;
        window.queueData.operations=Array.from({length:30},(_,i)=>({...base,id:'item-'+i,summary:'command='+('line\n'.repeat(60))}));
        CodePierQueue.refresh();
    }""")
    page.locator("[data-queue-id=item-0]").wait_for()
    page.locator("[data-queue-id=item-0] summary").click()
    page.evaluate("""() => {
        document.activeElement.blur();
        document.querySelector('.queue-list').scrollTop=180;
        window.listScroll=document.querySelector('.queue-list').scrollTop;
        CodePierQueue.refresh();
    }""")
    page.wait_for_function("window.readCalls.length>=3")
    assert page.evaluate("document.querySelector('.queue-list').scrollTop===window.listScroll")
    assert page.locator("[data-queue-id=item-0] details").get_attribute("open") is not None
    assert "不代表全部队列" in page.locator(".queue-limit").inner_text()



def test_queue_filter_focus_does_not_freeze_live_updates(queue_page):
    page = queue_page
    page.locator('[data-queue-stage="running"]').click()
    page.evaluate("window.queueData.operations[2].summary='updated while filter focused';CodePierQueue.refresh()")
    page.wait_for_function("document.querySelector('[data-queue-id=writer]').textContent.includes('updated while filter focused')")
    expect(page.locator('[data-queue-stage="running"]')).to_be_focused()
    expect(page.locator('[data-queue-stage="running"]')).to_have_attribute("aria-pressed", "true")
    assert page.locator('[data-queue-id]').count() == 1
