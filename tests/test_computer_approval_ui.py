"""Real DOM checks with a controlled inbox/network; no desktop input is sent."""
from pathlib import Path
import concurrent.futures
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright
from tests.test_computer_approval_integration import approval_stack, open_session, pending, tool
from tests.support import wait_for


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.clock.install()
    page.clock.pause_at(page.evaluate('Date.now()+1000'))
    page.set_content('<!doctype html><html><body></body></html>')
    page.add_script_tag(content='''
const S={session:null,projects:[{id:'p',alias:'Fixture'}]};
const fixture={approvals:[],gets:0,posts:[],holdGet:false,holdPost:false,fail:false,hidden:false,pendingGets:[],pendingPosts:[],toasts:[]};
Object.defineProperty(document,'hidden',{get:()=>fixture.hidden,configurable:true});
function toast(message){fixture.toasts.push(message);}
async function api(){
  fixture.gets++;
  const result={approvals:structuredClone(fixture.approvals)};
  if(fixture.holdGet)return new Promise(resolve=>fixture.pendingGets.push(()=>resolve(result)));
  if(fixture.fail)throw new Error('fixture offline');
  return result;
}
async function post(path,body){
  fixture.posts.push({path,body});
  if(fixture.holdPost)return new Promise((resolve,reject)=>fixture.pendingPosts.push({resolve,reject}));
  return {ok:true};
}
function item(id='a',seconds=60){return {request_id:id.repeat(32),session_id:'s'.repeat(32),project_id:'p',owner:'panel:fixture',app:'Fixture',message:'Allow Fixture? <img src=x onerror=alert(1)>',expires_at:Date.now()/1000+seconds};}
function login(){S.session={username:'fixture'};return startComputerApprovals();}
''')
    page.add_script_tag(path=str(Path(__file__).parents[1] / 'web/computer.js'))
    yield page
    page.close()


def test_no_session_never_reads_and_logout_clears_private_inbox(page):
    page.evaluate('refreshComputerApprovals()')
    page.clock.run_for(60000)
    assert page.evaluate('fixture.gets') == 0
    page.evaluate('fixture.approvals=[item()]; login()')
    expect(page.locator('#computer-approval-inbox')).to_be_visible()
    page.evaluate('S.session=null; stopComputerApprovals()')
    page.clock.run_for(60000)
    assert page.evaluate('fixture.gets') == 1
    expect(page.locator('#computer-approval-inbox')).to_have_count(0)


def test_refresh_keeps_focused_button_and_native_text_inert(page):
    page.evaluate('fixture.approvals=[item()]; login()')
    button = page.get_by_role('button', name='允许应用访问', exact=True)
    button.focus()
    page.evaluate('window.originalButton=document.activeElement; computerApprovalsConnection(true)')
    page.evaluate('fixture.approvals.push(item("b")); refreshComputerApprovals()')
    expect(page.locator('[data-request-id]')).to_have_count(2)
    assert page.evaluate('document.activeElement===window.originalButton')
    assert page.locator('#computer-approval-inbox img').count() == 0
    assert page.evaluate('fixture.posts') == []


def test_event_during_read_is_coalesced_then_fetches_newer_inbox(page):
    page.evaluate('fixture.holdGet=true; void login()')
    page.evaluate('fixture.approvals=[item()]; void refreshComputerApprovals(); void refreshComputerApprovals()')
    assert page.evaluate('fixture.gets') == 1
    page.evaluate('fixture.pendingGets.shift()()')
    page.clock.run_for(1)
    assert page.evaluate('fixture.gets') == 2
    page.evaluate('fixture.pendingGets.shift()()')
    expect(page.locator('[data-request-id]')).to_have_count(1)


def test_previous_login_response_cannot_populate_new_session(page):
    page.evaluate('fixture.approvals=[item("a")]; fixture.holdGet=true; void login()')
    page.evaluate('S.session=null; stopComputerApprovals(); fixture.approvals=[item("b")]; void login()')
    assert page.evaluate('fixture.gets') == 1
    page.evaluate('fixture.pendingGets.shift()()')
    page.clock.run_for(1)
    assert page.evaluate('fixture.gets') == 2
    expect(page.locator('[data-request-id]')).to_have_count(0)
    page.evaluate('fixture.pendingGets.shift()()')
    expect(page.locator('[data-request-id]')).to_have_attribute('data-request-id', 'b' * 32)


def test_fallback_catches_missed_events_and_pauses_while_hidden(page):
    page.evaluate('login()')
    page.evaluate('computerApprovalsConnection(true)')
    before = page.evaluate('fixture.gets')
    page.evaluate('fixture.approvals=[item()]')
    page.clock.run_for(15000)
    expect(page.locator('[data-request-id]')).to_have_count(1)
    assert page.evaluate('fixture.gets') == before + 1
    page.evaluate('fixture.hidden=true; document.dispatchEvent(new Event("visibilitychange"))')
    page.clock.run_for(60000)
    assert page.evaluate('fixture.gets') == before + 1
    page.evaluate('fixture.approvals=[]; fixture.hidden=false; document.dispatchEvent(new Event("visibilitychange"))')
    expect(page.locator('[data-request-id]')).to_have_count(0)
    assert page.evaluate('fixture.gets') == before + 2


def test_failed_reads_back_off_without_concurrent_or_unbounded_retry(page):
    page.evaluate('fixture.fail=true; login()')
    assert page.evaluate('fixture.gets') == 1
    for count, delay in enumerate([5000, 10000, 20000, 30000, 30000], start=2):
        page.clock.run_for(delay - 1)
        assert page.evaluate('fixture.gets') == count - 1
        page.clock.run_for(1)
        assert page.evaluate('fixture.gets') == count


def test_pending_decision_is_once_and_old_read_cannot_resurrect_it(page):
    page.evaluate('fixture.approvals=[item()]; fixture.holdPost=true; login()')
    page.evaluate('const button=document.querySelector("button"); void button.onclick(); void button.onclick()')
    assert len(page.evaluate('fixture.posts')) == 1
    expect(page.locator('[data-request-id]')).to_have_attribute('data-state', 'pending')
    page.evaluate('refreshComputerApprovals()')
    expect(page.get_by_role('button', name='允许应用访问', exact=True)).to_be_disabled()
    page.evaluate('fixture.holdGet=true; void refreshComputerApprovals()')
    page.evaluate('fixture.pendingPosts.shift().resolve({ok:true})')
    expect(page.locator('[data-request-id]')).to_have_count(0)
    page.evaluate('fixture.pendingGets.shift()()')
    page.clock.run_for(1)
    expect(page.locator('[data-request-id]')).to_have_count(0)
    assert len(page.evaluate('fixture.posts')) == 1


def test_local_expiry_disables_decisions_without_a_server_event(page):
    page.evaluate('fixture.approvals=[item("a",1)]; login()')
    page.clock.run_for(1000)
    expect(page.locator('[data-request-id]')).to_have_attribute('data-state', 'expired')
    expect(page.get_by_role('button', name='允许应用访问', exact=True)).to_be_disabled()
    page.evaluate('void document.querySelector("button").onclick()')
    assert page.evaluate('fixture.posts') == []


def test_ambiguous_decision_failure_requires_fresh_read_and_human_click(page):
    page.evaluate('fixture.approvals=[item()]; fixture.holdPost=true; login()')
    page.get_by_role('button', name='拒绝', exact=True).click()
    page.evaluate('fixture.fail=true; fixture.pendingPosts.shift().reject(new Error("fixture uncertain"))')
    expect(page.locator('[data-request-id]')).to_have_attribute('data-state', 'unavailable')
    page.clock.run_for(5000)
    assert len(page.evaluate('fixture.posts')) == 1
    page.evaluate('fixture.fail=false; refreshComputerApprovals()')
    expect(page.get_by_role('button', name='拒绝', exact=True)).to_be_enabled()
    assert len(page.evaluate('fixture.posts')) == 1


def test_real_sse_add_and_reconnect_refresh_the_human_inbox(browser, approval_stack):
    stack = approval_stack
    page = browser.new_page()
    try:
        page.goto(stack.url)
        page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('#event-state')).to_have_text('实时通道已连接')
        page.wait_for_function('computerApprovals.request===null')
        for reconnect in (False, True):
            # Disable the fallback so only the production SSE hooks can discover this request.
            page.evaluate('clearTimeout(computerApprovals.timer)')
            if reconnect:
                page.evaluate('stopEvents()')
            session = open_session(stack)
            try:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = executor.submit(stack.mcp, 'computer',
                                             {'operation': 'observe', 'project': 'Imago', 'session_id': session}, stack.approval_pat)
                    wait_for(lambda: pending(stack), timeout=5)
                    if reconnect:
                        expect(page.locator('#computer-approval-inbox')).to_have_count(0)
                        page.evaluate('connectEvents()')
                    expect(page.locator('#computer-approval-inbox')).to_be_visible(timeout=3000)
                    page.get_by_role('button', name='拒绝', exact=True).click()
                    assert result.result(timeout=10)['isError']
                    expect(page.locator('#computer-approval-inbox')).to_have_count(0)
            finally:
                tool(stack, 'close', {'session_id': session, 'idempotency_key': uuid.uuid4().hex})
            page.wait_for_function('computerApprovals.request===null')
    finally:
        page.close()
