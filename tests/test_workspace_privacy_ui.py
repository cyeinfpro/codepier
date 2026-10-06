"""Synthetic-browser checks for stale workspace content and scoped exports."""
from pathlib import Path

import pytest
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["chromium", "webkit"])
def privacy_page(request, chat_browser_pool):
    page = chat_browser_pool(request.param).new_page()
    page.route("https://privacy.fixture/**", lambda route: route.fulfill(
        status=200, content_type="text/html",
        body='<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>'))
    page.goto("https://privacy.fixture/")
    for name in ("core/bundle.js", "ui.js"):
        page.add_script_tag(path=str(ROOT / "web" / name))
    page.add_script_tag(content=panel_without_boot())
    page.add_script_tag(path=str(ROOT / "web/identity.js"))
    page.evaluate("""() => {
      S.session={user_id:'fixture-owner',username:'Fixture'};
      S.space_id='team';S.page='workbench';
      window.fetch=async()=>new Response(JSON.stringify({providers:[]}),
        {status:200,headers:{'Content-Type':'application/json'}});
      window.EventSource=class {
        constructor(){this.listeners={};window.fixtureEvents=this;}
        addEventListener(name,handler){this.listeners[name]=handler;}
        close(){this.closed=true;}
      };
    }""")
    try:
        yield page
    finally:
        page.context.close()


def test_logout_clears_all_space_snapshots(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      for(const key of ['overview','projects','devices','settings','grants'])
        S[key]=[{private:'PRIVATE_SENTINEL'}];
      endSession(true);
    }""")
    assert "PRIVATE_SENTINEL" not in page.evaluate("JSON.stringify(S)")
    assert page.locator("#login-form").is_visible()


def test_sse_revocation_clears_loaded_private_content(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      S.work.content='PRIVATE_SENTINEL';S.work.original='PRIVATE_SENTINEL';
      document.querySelector('#app').textContent='PRIVATE_SENTINEL';
      connectEvents();
      fixtureEvents.listeners.access_revoked?.({data:'{}'});
    }""")
    assert "PRIVATE_SENTINEL" not in page.locator("body").inner_text()
    assert page.evaluate("S.work.content") == ""
    assert page.locator("#login-form").is_visible()


def test_audit_download_keeps_selected_space(privacy_page):
    page=privacy_page
    page.evaluate("""async () => {
      window.downloads=[];
      HTMLAnchorElement.prototype.click=function(){downloads.push(this.href);};
      await panelActions.dispatch('export-audit',document.createElement('button'),{});
    }""")
    from urllib.parse import parse_qs, urlsplit
    query=parse_qs(urlsplit(page.evaluate("downloads[0]")).query)
    assert query["space_id"] == ["team"]


def test_stale_stream_cannot_clear_new_space(privacy_page):
    page=privacy_page
    page.evaluate("""() => {
      connectEvents();window.oldEvents=fixtureEvents;
      S.session={user_id:'fixture-owner'};S.space_id='new-team';
      S.work.content='NEW_SPACE_DRAFT';
      oldEvents.listeners.access_revoked?.({data:'{}'});
    }""")
    assert page.evaluate("S.work.content") == "NEW_SPACE_DRAFT"
    assert page.evaluate("S.space_id") == "new-team"


@pytest.mark.parametrize("status,code,revoked", [
    (403, "SPACE_FORBIDDEN", True), (404, "SPACE_NOT_FOUND", True),
    (401, "LOGIN_REQUIRED", True), (503, "TEMPORARILY_UNAVAILABLE", False),
])
def test_rejected_reconnect_rechecks_authority_without_erasing_on_network_failure(privacy_page, status, code, revoked):
    page=privacy_page
    page.evaluate("""({status,code}) => {
      S.work.content='PRIVATE_SENTINEL';
      window.fetch=async(path)=>path==='/api/auth/providers'
        ? new Response(JSON.stringify({providers:[]}),{status:200})
        : new Response(JSON.stringify({error:{code,message:'fixture'}}),
          {status,headers:{'Content-Type':'application/json'}});
      connectEvents();fixtureEvents.onerror();
    }""", {"status": status, "code": code})
    if revoked:
        page.wait_for_function("S.session===null")
        assert page.evaluate("S.work.content") == ""
    else:
        page.wait_for_function("document.querySelector('#toasts')!==null")
        assert page.evaluate("S.work.content") == "PRIVATE_SENTINEL"
        assert page.evaluate("S.session!==null")


def test_audit_download_without_space_fails_closed(privacy_page):
    page=privacy_page
    result=page.evaluate("""async () => {
      S.space_id=null;window.downloads=[];
      HTMLAnchorElement.prototype.click=function(){downloads.push(this.href);};
      try { await panelActions.dispatch('export-audit',document.createElement('button'),{}); }
      catch(error) { return {error:error.message,downloads}; }
      return {downloads};
    }""")
    assert result["downloads"] == []
    assert result.get("error")
