"""Mobile browser acceptance against the real disposable Hub and shipped panel.

Chromium/WebKit touch viewports are browser automation, not physical phones.
Composition and background transitions are explicitly injected DOM events.
Only the Goal history payload is a labelled UI fixture; messages, SSE, team
approval, receipt recovery and the temporary Hub/Agent processes are real.
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from hub.store import Store
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from tests.collaboration_support import collaboration_stack as collaboration_stack, key

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]
EVIDENCE = Path(os.environ.get("MULTIDOT_MOBILE_EVIDENCE", ".work/multidot-mobile-evidence"))

HOOK = r"""(() => {
  window.mobileEvidence={sources:[],changes:0,opens:0,reads:0};
  const NativeSource=window.EventSource;
  mobileEvidence.NativeSource=NativeSource;
  window.EventSource=class extends NativeSource {
    constructor(url, options) {
      super(url,options);
      if(!String(url).includes('/collaboration/relay-hints?'))return;
      const item={url:String(url),closed:false};
      mobileEvidence.sources.push(item);
      this.addEventListener('open',()=>mobileEvidence.opens++);
      this.addEventListener('change',event=>{mobileEvidence.changes++;
        mobileEvidence.lastRevision=JSON.parse(event.data).revision;});
      const close=this.close.bind(this);
      this.close=()=>{item.closed=true;close();};
    }
  };
  for(const [name,label] of [['CodePierCollaborationRelay','Relay'],['CodePierCollaborationGoals','Goals']]){
    let current;
    Object.defineProperty(window,name,{configurable:true,get(){return current;},set(value){
      current=value;
      if(label==='Relay'){
        const stream=value.hintStream;
        value.hintStream=function(options){
          const changed=options.onChange;
          return stream.call(this,{...options,onChange:(...args)=>{
            mobileEvidence.flushedRevision=mobileEvidence.lastRevision;
            return changed?.(...args);
          }});
        };
      }
      const create=value.create;
      value.create=function(ctx){
        const instance=create.call(this,ctx);
        window['mobile'+label]=instance;
        window['mobile'+label+'Context']=ctx;
        return instance;
      };
    }});
  }
})()"""


def wait_js(page, expression, timeout=15):
    deadline = time.monotonic() + timeout
    while not page.evaluate("() => (" + expression + ")"):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(25)


def scope(stack):
    return {"project": stack.project["id"], "environment_id": "production"}


def post(stack, operation, **extra):
    return stack.must(stack.client.post("/api/collaboration/" + operation, json={
        **scope(stack), "idempotency_key": key(), **extra,
    }))


def mcp(stack, arguments):
    params = {"name": "collaboration", "arguments": arguments,
              "_meta": {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}}
    body = {"jsonrpc": "2.0", "id": key(), "method": "tools/call", "params": params}
    envelope = stack.must(stack.client.post("/mcp", json=body, headers={
        "Authorization": "Bearer " + stack.pat,
        "Accept": "application/json, text/event-stream", **request_headers(body),
    }))
    assert "error" not in envelope, envelope
    result = envelope["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def add_dot(stack, label):
    dot = post(stack, "dot", label=label, duplex=True, capabilities=["read"],
               confirm_tasks=True)["dot"]
    joined = mcp(stack, {"action": "join", "code": dot["join_code"], "idempotency_key": key()})
    assert joined["registered"] and not joined["subscription_created"]
    return joined["slot"]


def prepare(page, stack):
    page.goto(stack.url + "/#projects")
    page.fill("#username", "admin")
    page.fill("#password", stack.password)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/login")) as response:
        page.click("#login-form button")
    assert response.value.status == 200
    expect(page.locator("#page h1")).to_have_text("项目映射")
    page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
    page.locator('[data-cc-action="create-room"]').click()
    expect(page.locator("#cc-message-input")).to_be_enabled()
    dot = add_dot(stack, "移动验收主责")
    page.reload()
    expect(page.locator("#cc-message-input")).to_be_enabled()
    expect(page.locator(".collaboration")).to_have_class(re.compile("cc-relay-focus"))
    expect(page.locator(".cc-mention-chip")).to_contain_text("移动验收主责")
    wait_js(page, "mobileEvidence.opens >= 1")
    wait_js(page, "!mobileRelayContext.chat().relaySync")
    return dot


def owner_message(stack, dot, text):
    state = stack.must(stack.client.get("/api/collaboration", params=scope(stack)))
    return post(stack, "message", room_id=state["room"]["id"],
                conversation_id=dot["conversation_id"], body_text=text,
                mentions=[{"slot_id": dot["id"]}], client_message_id=key())["message"]


def ledger_count(stack, client_id):
    store = Store(stack.hubdir)
    try:
        return store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE source_id=?",
                         (client_id,))["n"]
    finally:
        store.close()


def no_execution(stack):
    store = Store(stack.hubdir)
    try:
        assert store.one("SELECT COUNT(*) AS n FROM delegation_requests")["n"] == 0
        assert store.one("SELECT COUNT(*) AS n FROM coordination_work")["n"] == 0
    finally:
        store.close()


def record(page, engine, label, extra=None):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    geometry = page.evaluate("""() => {
      const root=document.querySelector('.collaboration'),area=document.querySelector('#cc-message-input');
      const r=area?.getBoundingClientRect(),v=visualViewport;
      return {inner_width:innerWidth,inner_height:innerHeight,scroll_width:document.documentElement.scrollWidth,
        viewport_height:v?.height,viewport_offset:v?.offsetTop,composer_bottom:r?.bottom,
        root_viewport_height:root?.style.getPropertyValue('--cc-viewport-height'),
        touch:matchMedia('(pointer:coarse)').matches,
        live_sse_sources:mobileEvidence.sources.filter(s=>!s.closed).length,
        sse_changes:mobileEvidence.changes};
    }""")
    assert geometry["scroll_width"] <= geometry["inner_width"]
    data = {"engine": engine, "browser_version": page.context.browser.version,
            "physical_device_verified": False, "native_dot_verified": False,
            "geometry": geometry, **(extra or {})}
    (EVIDENCE / f"{engine}-{label}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    page.screenshot(path=str(EVIDENCE / f"{engine}-{label}.png"), animations="disabled", full_page=False)


def mobile_context(pool, engine):
    context = pool(engine).new_context(viewport={"width": 390, "height": 844},
                                       is_mobile=True, has_touch=True, device_scale_factor=1)
    context.add_init_script(HOOK)
    return context


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_composition_real_sse_and_visual_viewport(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = mobile_context(chat_browser_pool, engine)
    page = context.new_page()
    errors, writes = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda r: writes.append(r.post_data_json)
            if r.method == "POST" and r.url.endswith("/api/collaboration/message") else None)
    try:
        dot = prepare(page, stack)
        area = page.locator("#cc-message-input")
        original = area.element_handle()
        area.fill("中文组合输入，光标与草稿应保持。")
        area.evaluate("(node)=>node.setSelectionRange(2,6)")
        area.dispatch_event("compositionstart", {"data": "组合"})
        area.dispatch_event("keydown", {"key": "Enter", "isComposing": True, "keyCode": 229})
        before = page.evaluate("mobileEvidence.changes")
        incoming = owner_message(stack, dot, "SSE 输入期间到达的真实持久消息")
        wait_js(page, f"mobileEvidence.changes > {before}")
        wait_js(page, "!mobileRelayContext.chat().relaySync")
        assert original.evaluate("(node)=>node===document.querySelector('#cc-message-input')")
        expect(area).to_be_focused()
        expect(area).to_have_value("中文组合输入，光标与草稿应保持。")
        assert area.evaluate("(node)=>[node.selectionStart,node.selectionEnd]") == [2, 6]
        assert page.evaluate("mobileRelayContext.state.composing")
        assert writes == []
        area.dispatch_event("compositionend", {"data": "组合"})
        expect(page.locator(f'[data-message-id="{incoming["id"]}"]')).to_be_visible(timeout=15000)
        measurements = []
        for width, height in [(320, 568), (390, 460), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            wait_js(page, "Math.abs(parseFloat(document.querySelector('.collaboration').style.getPropertyValue('--cc-viewport-height'))-visualViewport.height)<1")
            metrics = area.evaluate("""node=>{
              const r=node.getBoundingClientRect(),v=visualViewport;
              return {bottom:r.bottom,top:r.top,limit:v.offsetTop+v.height,
                overflow:document.documentElement.scrollWidth>innerWidth};
            }""")
            assert not metrics["overflow"], metrics
            assert 0 <= metrics["top"] < metrics["bottom"] <= metrics["limit"] + 1, metrics
            measurements.append({"width": width, "height": height, **metrics})
            expect(area).to_be_focused()
            assert original.evaluate("(node)=>node.isConnected")
            expect(area).to_have_value("中文组合输入，光标与草稿应保持。")
        record(page, engine, "composition-sse", {"composer_replaced": False, "unexpected_sends": len(writes),
                                                "viewport_samples": measurements})
        no_execution(stack)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_offline_lost_ack_recovers_once_and_keeps_draft(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = mobile_context(chat_browser_pool, engine)
    page = context.new_page()
    posts, receipts = [], []
    try:
        prepare(page, stack)

        def lose_ack(route):
            posts.append(route.request.post_data_json)
            response = route.fetch()
            assert response.ok, response.text()
            if len(posts) == 1:
                route.abort("failed")
            else:
                route.fulfill(response=response)

        def receipt(route):
            response = route.fetch()
            assert response.ok, response.text()
            receipts.append(response.json())
            route.fulfill(response=response)

        page.route("**/api/collaboration/message", lose_ack)
        page.route("**/api/collaboration/message-receipt?*", receipt)
        context.set_offline(True)
        area = page.locator("#cc-message-input")
        area.fill("离线点击发送的唯一意图")
        page.locator('#cc-command button[type="submit"]').tap()
        expect(area).to_have_value("")
        expect(page.locator("#cc-relay-outbox [data-pending-id]")).to_have_count(1)
        original_id = page.locator("#cc-relay-outbox [data-pending-id]").get_attribute("data-pending-id")
        assert ledger_count(stack, original_id) == 0
        area.fill("仍在输入的下一条草稿")
        area.evaluate("(node)=>node.setSelectionRange(2,5)")
        context.set_offline(False)
        page.evaluate("()=>{for(let i=0;i<12;i++)window.dispatchEvent(new Event('online'));}")
        expect(page.locator("#cc-relay-outbox [data-pending-id]")).to_have_count(0, timeout=20000)
        wait_js(page, "!mobileRelayContext.chat().relayFlight")
        assert len(posts) == 1 and posts[0]["client_message_id"] == original_id
        assert ledger_count(stack, original_id) == 1
        assert any(r["authoritative"] and r["state"] == "committed" for r in receipts)
        expect(area).to_have_value("仍在输入的下一条草稿")
        expect(area).to_be_focused()
        assert area.evaluate("(node)=>[node.selectionStart,node.selectionEnd]") == [2, 5]
        assert not page.evaluate("""()=>[...Object.values(localStorage),...Object.values(sessionStorage)]
          .some(value=>value.includes('离线点击发送的唯一意图')||value.includes('仍在输入的下一条草稿'))""")
        record(page, engine, "offline-ack", {"post_attempts": len(posts), "committed_messages": 1,
                                           "receipt_recovered": True, "draft_retained": True})
        no_execution(stack)
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_background_restore_coalesces_real_hint_reads(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = mobile_context(chat_browser_pool, engine)
    page = context.new_page()
    held, reads = [], []
    try:
        dot = prepare(page, stack)
        page.locator("#cc-message-input").fill("后台恢复仍保留草稿")
        page.evaluate("""()=>{
          window.mobileHidden=true;
          Object.defineProperty(document,'hidden',{configurable:true,get:()=>mobileHidden});
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        wait_js(page, "mobileEvidence.sources.every(s=>s.closed)")
        owner_message(stack, dot, "后台期间保留在服务器的消息")
        page.wait_for_timeout(150)

        def hold_first(route):
            reads.append(route.request.url)
            if len(reads) == 1:
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/collaboration/relay-sync?*", hold_first)
        page.evaluate("""()=>{
          mobileHidden=false;
          for(let i=0;i<8;i++)document.dispatchEvent(new Event('visibilitychange'));
        }""")
        wait_js(page, "mobileRelayContext.chat().relaySync")
        deadline = time.monotonic() + 10
        while not held:
            assert time.monotonic() < deadline
            page.wait_for_timeout(20)
        before = page.evaluate("mobileEvidence.changes")
        for i in range(3):
            owner_message(stack, dot, f"并发提示保留原消息 {i}")
        wait_js(page, f"mobileEvidence.changes > {before}")
        # A second real stream gives the authoritative post-commit revision.
        # Hold the first GET until the main stream has flushed that revision;
        # otherwise a legitimately later SSE would require a third sync.
        final_revision = page.evaluate("""()=>new Promise((resolve,reject)=>{
          const url=mobileEvidence.sources.findLast(s=>!s.closed).url;
          const probe=new mobileEvidence.NativeSource(url);
          const timeout=setTimeout(()=>{probe.close();reject(new Error('SSE revision barrier timed out'));},5000);
          probe.addEventListener('change',event=>{
            clearTimeout(timeout);probe.close();resolve(JSON.parse(event.data).revision);
          },{once:true});
        })""")
        wait_js(page, "mobileEvidence.flushedRevision === " + json.dumps(final_revision))
        wait_js(page, "mobileRelayContext.chat().relayRefreshPending === true")
        assert len(reads) == 1
        held[0].continue_()
        wait_js(page, "!mobileRelayContext.chat().relaySync")
        assert len(reads) == 2, reads
        assert page.evaluate("mobileEvidence.sources.filter(s=>!s.closed).length") == 1
        expect(page.locator("#cc-message-input")).to_have_value("后台恢复仍保留草稿")
        expect(page.locator(".cc-message.is-owner")).to_have_count(4, timeout=10000)
        record(page, engine, "background-singleflight", {
            "background_method": "Injected visibilitychange and document.hidden; not OS backgrounding",
            "sync_reads_after_reentry": len(reads), "server_messages": 4,
        })
        no_execution(stack)
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_team_approval_and_explicit_goal_history_page(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = mobile_context(chat_browser_pool, engine)
    page = context.new_page()
    queries, held_history = [], []
    try:
        first = prepare(page, stack)
        second = add_dot(stack, "移动验收成员")
        page.reload()
        expect(page.locator("#cc-message-input")).to_be_enabled()
        area = page.locator("#cc-message-input")
        area.fill("选择团队期间保留的中文草稿")
        page.locator('[data-cc-view="jobs"]').tap()
        page.locator('.cc-team-list [data-cc-action="team-new"]').tap()
        form = page.locator("#cc-team-create")
        form.locator(f'[name="dot_ids"][value="{first["id"]}"]').check()
        form.locator(f'[name="dot_ids"][value="{second["id"]}"]').check()
        form.locator('[name="coordinator_dot_id"]').select_option(second["id"])
        form.locator('[name="label"]').fill("移动双人协作验收")
        assert not form.locator('[name="confirm_tasks"]').is_checked()
        form.locator('button[type="submit"]').click()
        assert page.evaluate("mobileGoalsContext.state.snapshot.teams.length") == 0
        form.locator('[name="confirm_tasks"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator("#cc-drawer")).not_to_be_visible(timeout=10000)
        wait_js(page, "mobileGoalsContext.chat().teamId !== ''")
        assert page.evaluate("mobileGoalsContext.chat().mentions") == [second["id"]]
        expect(area).to_have_value("选择团队期间保留的中文草稿")
        approved = page.evaluate("mobileGoalsContext.state.snapshot.teams[0]")
        assert approved["state"] == "active" and approved["coordinator_dot_id"] == second["id"]
        assert len(approved["participants"]) == 2
        wait_js(page, "!mobileGoalsContext.state.busy && !mobileRelayContext.chat().relaySync")

        def goal_pages(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get("kind") != ["coordination_goals"]:
                route.continue_()
                return
            cursor = query.get("cursor", [""])[0]
            queries.append({"cursor": cursor, "limit": query.get("limit")})
            if cursor and not held_history:
                held_history.append(route)
                return
            start, end = (80, 100) if cursor == "ui-history-last" else (40, 80) if cursor else (0, 40)
            route.fulfill(status=200, json={"items": [{
                "id": f"ui-history-{i}", "objective": f"移动端分页目标 {i}",
                "acceptance": "仅用于验证生产页面的分页请求和布局；不是服务器任务。",
                "state": "proposed", "version": 1, "project_ids": [stack.project["id"]],
                "participant_grant_ids": [], "can_approve": False,
            } for i in range(start, end)], "next_cursor":
                "ui-history-next" if start == 0 else "ui-history-last" if start == 40 else None})

        page.route("**/api/collaboration?*", goal_pages)
        page.evaluate("()=>mobileGoals.load().then(()=>mobileGoalsContext.onProjectionChange())")
        expect(page.locator(".cc-coordination-list .cc-goal-summary")).to_have_count(40)
        assert queries == [{"cursor": "", "limit": ["40"]}]
        page.locator('[data-cc-action="goal-more"]').tap()
        deadline = time.monotonic() + 10
        while not held_history:
            assert time.monotonic() < deadline
            page.wait_for_timeout(20)
        # Reproduce a newer background metadata projection while the user's
        # first history page remains in flight. The old page must be rejected,
        # but the user's explicit expand action must continue with fresh data.
        page.evaluate("()=>mobileGoals.load().then(()=>mobileGoalsContext.onProjectionChange())")
        held_history[0].fulfill(status=200, json={"items": [{
            "id": "discarded-history", "objective": "过期历史页不得渲染",
            "state": "proposed", "version": 1,
        }], "next_cursor": None})
        try:
            expect(page.locator(".cc-coordination-list .cc-goal-summary")).to_have_count(80)
        except AssertionError:
            print("PAGINATION_DIAGNOSTIC", json.dumps({
                "queries": queries,
                "ui": page.evaluate("""()=>({
                  busy:!!mobileGoalsContext.state.busy,
                  model_count:(mobileGoals.list().match(/class="cc-goal-summary"/g)||[]).length,
                  deferred:document.querySelector('.cc-workspace')._ccWorkspaceDeferred,
                  active:document.activeElement?.tagName,
                  activeAction:document.activeElement?.dataset?.ccAction,
                  feedback:document.querySelector('#cc-feedback')?.textContent,
                  records_active:document.querySelector('.cc-workspace-records')?.matches(':active')
                })"""),
            }, ensure_ascii=False), flush=True)
            raise
        assert [q for q in queries if q["cursor"]] == [
            {"cursor": "ui-history-next", "limit": ["40"]},
            {"cursor": "ui-history-next", "limit": ["40"]},
        ]
        expect(page.locator(".cc-coordination-list")).not_to_contain_text("过期历史页不得渲染")
        # A normal later first-page refresh must retain the already expanded
        # continuation (80), rather than rewinding it to the first page (40).
        page.evaluate("()=>mobileGoals.load().then(()=>mobileGoalsContext.onProjectionChange())")
        expect(page.locator(".cc-coordination-list .cc-goal-summary")).to_have_count(80)
        page.locator('[data-cc-action="goal-more"]').tap()
        try:
            expect(page.locator(".cc-coordination-list .cc-goal-summary")).to_have_count(100)
        except AssertionError:
            print("PAGINATION_FINAL_DIAGNOSTIC", json.dumps({
                "queries": queries,
                "ui": page.evaluate("""()=>({
                  busy:!!mobileGoalsContext.state.busy,
                  model_count:(mobileGoals.list().match(/class="cc-goal-summary"/g)||[]).length,
                  deferred:document.querySelector('.cc-workspace')._ccWorkspaceDeferred,
                  active:document.activeElement?.tagName,
                  activeAction:document.activeElement?.dataset?.ccAction,
                  feedback:document.querySelector('#cc-feedback')?.textContent,
                  records_active:document.querySelector('.cc-workspace-records')?.matches(':active')
                })"""),
            }, ensure_ascii=False), flush=True)
            raise
        assert [q for q in queries if q["cursor"]] == [
            {"cursor": "ui-history-next", "limit": ["40"]},
            {"cursor": "ui-history-next", "limit": ["40"]},
            {"cursor": "ui-history-last", "limit": ["40"]},
        ]
        assert all(query["limit"] == ["40"] for query in queries)
        expect(page.locator('[data-cc-action="goal-more"]')).to_have_count(0)
        page.set_viewport_size({"width": 320, "height": 568})
        wait_js(page, "Math.abs(parseFloat(document.querySelector('.collaboration').style.getPropertyValue('--cc-viewport-height'))-visualViewport.height)<1")
        record(page, engine, "team-history", {"approved_members": 2, "coordinators": 1,
                                             "history_source": "labelled HTTP UI payload fixture",
                                             "goal_page_requests": queries, "visible_goals": 100})
        page.locator('[data-cc-view="discussion"]').tap()
        expect(area).to_have_value("选择团队期间保留的中文草稿")
        no_execution(stack)
    finally:
        context.close()
