"""Browser document lifecycle must cancel reads without replaying room actions."""
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from tests.browser_support import open_project_collaboration

from tests.legacy_collaboration_ui import use_legacy_collaboration_ui
from tests.lifecycle_http import held_legacy_reload

from tests.collaboration_support import collaboration_stack  # noqa: F401
from tests.test_collaboration_chatroom_browser import login, overview

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


POLL_KINDS = ["delegation_policies", "members", "overview", "timeline"]
POLL_PROBE = """(() => {
  const originalTimer = window.setTimeout, originalFetch = window.fetch;
  const probe = window.lifecycleProbe = {armed: false, rows: [], poll: null,
    origin: crypto.randomUUID(), pagehide: false, signals: [], owners: [], cycles: new Set(),
    pollAttempts: 0, pollCompletions: 0, paused: false, timers: new Set()};
  probe.record = (event, details={}) => {
    const key = 'codepier-lifecycle-read-evidence';
    const records = JSON.parse(sessionStorage.getItem(key) || '[]');
    records.push({event, origin:probe.origin, ...details});
    sessionStorage.setItem(key, JSON.stringify(records));
  };
  probe.firstCycle = new Promise(resolve => probe.resolveCycle = resolve);
  window.addEventListener('pagehide', () => {
    probe.pagehide = true;
    if (probe.armed) probe.record('pagehide');
  });
  window.setTimeout = function(callback, delay, ...args) {
    if (typeof callback !== 'function' || callback.name !== 'poll')
      return originalTimer.call(this, callback, delay, ...args);
    const run = () => {
      probe.pollAttempts++;
      const pending = Promise.resolve(callback(...args));
      probe.cycles.add(pending);
      const done = () => {probe.pollCompletions++; probe.cycles.delete(pending);};
      pending.then(done, done);
      return pending;
    };
    const id = originalTimer.call(this, () => {
      probe.timers.delete(id);
      if (!probe.paused) return run();
    }, delay);
    probe.poll = {id, run};
    if (probe.paused) clearTimeout(id);
    else probe.timers.add(id);
    return id;
  };
  probe.drain = async () => {
    clearTimeout(probe.poll?.id);
    while (probe.cycles.size) {
      await Promise.all([...probe.cycles]);
      clearTimeout(probe.poll?.id);
    }
  };
  probe.pause = async () => {
    // Hold only setup scheduling. Keep the original callback and wait for
    // admitted work; the measured pending-reload cycle is resumed explicitly.
    probe.paused = true;
    for (const id of probe.timers) clearTimeout(id);
    probe.timers.clear();
    await probe.drain();
  };
  window.fetch = function(input, init = {}) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const kind = url.searchParams.get('kind');
    if (!probe.armed || url.pathname !== '/api/collaboration' ||
        !['timeline', 'overview', 'members', 'delegation_policies'].includes(kind))
      return originalFetch.call(this, input, init);
    const row = {kind, beforePagehide: !probe.pagehide, keepalive: init.keepalive === true};
    probe.rows.push(row);
    probe.signals.push({kind, signal:init.signal});
    probe.record('read-start', {kind, hasSignal:!!init.signal, aborted:!!init.signal?.aborted});
    const aborted = () => {
      probe.record('read-abort', {kind, aborted:!!init.signal?.aborted});
      console.log('LIFECYCLE_READ_ABORT ' + kind);
    };
    init.signal?.addEventListener('abort', aborted, {once:true});
    const finish = result => {
      Object.assign(row, result, {settled:true});
      probe.record('read-settled', {kind, ...result});
      init.signal?.removeEventListener('abort', aborted);
      if (probe.rows.filter(item => item.settled).length >= 4) probe.resolveCycle();
    };
    if (probe.routeMarker) {
      const headers = new Headers(init.headers);
      headers.set('X-CodePier-Test-Lifecycle', probe.routeMarker);
      init = {...init, headers};
    }
    return originalFetch.call(this, input, init).then(response => {
      finish({status:response.status}); return response;
    }, error => {
      finish({error:error.name}); throw error;
    });
  };
})();"""


def test_poll_probe_pause_seals_setup_timer_window():
    # A controlled timer unit test checks the probe, not browser/HTTP behavior.
    # The end-to-end case below still executes the original callback and reads.
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
function fixture() {
  let next = 0;
  const timers = new Map();
  const context = {
    Promise, Set, URL, Headers, crypto: {randomUUID: () => 'fixture-document'},
    location: {href: 'http://fixture/'}, console,
    sessionStorage: {getItem: () => null, setItem() {}},
    addEventListener() {},
    fetch() {throw new Error('Unexpected network in timer unit test');},
    setTimeout(callback, delay, ...args) {
      const id = ++next;
      timers.set(id, () => callback(...args));
      return id;
    },
    clearTimeout(id) {timers.delete(id);}
  };
  context.window = context;
  vm.runInNewContext(source, context);
  return {context, timers, probe: context.lifecycleProbe};
}
(async () => {
  // Draining admitted promises alone leaves a later scheduling window open.
  const old = fixture();
  async function poll() {}
  old.context.setTimeout(poll, 5000);
  await old.probe.drain();
  old.context.setTimeout(poll, 5000);
  assert.equal(old.timers.size, 1);

  for (const alreadyRunning of [false, true]) {
    const {context, timers, probe} = fixture();
    let calls = 0, release;
    const gate = new Promise(resolve => {release = resolve;});
    async function poll() {calls++; await gate;}
    const id = context.setTimeout(poll, 5000);
    if (alreadyRunning) {
      const fire = timers.get(id);
      timers.delete(id);
      void fire();
      assert.equal(probe.cycles.size, 1);
    }
    let paused = false;
    const pausing = probe.pause().then(() => {paused = true;});
    await Promise.resolve();
    if (alreadyRunning) assert.equal(paused, false);
    assert.equal(timers.size, 0);
    // A lifecycle rebind during/after the drain must not start an unarmed poll.
    context.setTimeout(poll, 5000);
    assert.equal(timers.size, 0);
    assert.equal(calls, alreadyRunning ? 1 : 0);
    release();
    await pausing;
    assert.equal(probe.cycles.size, 0);
    probe.armed = true;
    probe.paused = false;
    await probe.poll.run();
    await Promise.resolve();
    assert.equal(calls, alreadyRunning ? 2 : 1);
    assert.equal(probe.pollCompletions, calls);
    assert.equal(probe.cycles.size, 0);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(['node', '-e', script], input=POLL_PROBE, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def polling_page(stack, chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.add_init_script(POLL_PROBE)
    if getattr(stack, 'native_legacy_document', False):
        page._codepier_legacy_collaboration_fixture = True
    errors, writes = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: writes.append(request.url)
            if request.method == "POST" and "/api/collaboration" in request.url else None)
    login(page, stack)
    page.locator('[data-cc-view="jobs"]').click()
    expect(page.locator(".cc-coordination-list")).to_be_visible()
    if not page.locator('[data-cc-action="refresh"]').is_visible():
        page.locator('[data-cc-more]').click()
    page.locator('[data-cc-action="refresh"]').click()
    expect(page.locator("#cc-feedback")).to_contain_text("状态已刷新。")
    page.evaluate("""() => {
      const original = window.api;
      window.api = function(path, options = {}) {
        const url = new URL(path, location.href), kind = url.searchParams.get('kind');
        if (lifecycleProbe.armed && url.pathname === '/api/collaboration' &&
            ['timeline', 'overview', 'members', 'delegation_policies'].includes(kind))
          lifecycleProbe.owners.push({kind, signal:options.signal});
        return original.call(this, path, options);
      };
    }""")
    # A scheduled cycle may already be running by the time the refresh UI is
    # visible. Finish that original cycle before routes are held and the probe
    # is armed, so its late/retried fetches cannot stand in for the new cycle.
    page.evaluate("lifecycleProbe.drain()")
    return context, page, errors, writes


@pytest.fixture
def pending_reload_stack(collaboration_stack):
    with held_legacy_reload(collaboration_stack) as stack:
        yield stack


@pytest.mark.parametrize("engine", ["webkit"])
def test_pending_reload_reads_use_the_original_document_without_page_errors(
        pending_reload_stack, chat_browser_pool, tmp_path, engine):
    stack = pending_reload_stack
    context, page, errors, writes = polling_page(stack, chat_browser_pool, engine)
    try:
        original = page.evaluate_handle("() => lifecycleProbe")
        original_document = page.evaluate_handle("document")
        original.evaluate("p => p.pause()")
        before_writes = list(writes)
        stack.reload_armed.set()
        with page.expect_request(lambda request: request.is_navigation_request()
                                 and request.frame == page.main_frame):
            page.evaluate("() => {setTimeout(() => location.reload(), 0);}")
        # Wait for the real HTTP server to receive the reload and hold its
        # response, rather than suspending WebKit at native request interception.
        assert stack.reload_arrived.wait(10), "Reload did not reach the HTTP response gate"
        stack.reload_marker = original.evaluate("p => p.origin")
        original.evaluate("p => {p.navigationHeld = true; p.routeMarker = p.origin;}")
        assert not stack.reload_release.is_set()
        # Run the real scheduled callback in its original document while the
        # replacement response is held. Native fetches and API responses remain
        # real; no synthetic success, timer delay, retry or error filtering.
        result = original.evaluate("""async p => {
          // A wake while another poll is active may return immediately while
          // scheduling the real cycle. Await its four actual response records,
          // rather than treating that early return as completed work.
          if (p.cycles.size) throw new Error('Setup poll is still active');
          p.armed = true;
          p.paused = false;
          void p.poll.run();
          await Promise.race([p.firstCycle, new Promise((_, reject) =>
            setTimeout(() => reject(new Error('Four polling reads did not finish: ' +
              JSON.stringify({pagehide:p.pagehide, hidden:document.hidden,
                navigationHeld:p.navigationHeld === true,
                sameDocument:p.origin === window.lifecycleProbe.origin,
                pollAttempts:p.pollAttempts, pollCompletions:p.pollCompletions,
                activeCycles:p.cycles.size, rows:p.rows,
                owners:p.owners.map(item => ({kind:item.kind,
                  hasSignal:!!item.signal, aborted:!!item.signal?.aborted})),
                signals:p.signals.map(item => ({kind:item.kind,
                  hasSignal:!!item.signal, aborted:!!item.signal?.aborted}))}))), 15000))]);
          return {origin:p.origin, currentOrigin:window.lifecycleProbe.origin,
            pagehide:p.pagehide, rows:p.rows};
        }""")
        assert stack.reload_arrived.is_set() and not stack.reload_release.is_set()
        assert page.evaluate("old => document === old", original_document)
        original_document.dispose()
        assert result["origin"] == result["currentOrigin"]
        assert not result["pagehide"]
        assert sorted(row["kind"] for row in result["rows"]) == POLL_KINDS
        assert all(row.get("status") == 200 and row["beforePagehide"] for row in result["rows"])
        assert errors == []
        assert sorted(row["kind"] for row in stack.reload_records
                      if row["event"] == "read-response" and row["kind"] in POLL_KINDS) == POLL_KINDS
        assert all(row["status"] == 200 for row in stack.reload_records)
        with page.expect_navigation(wait_until="domcontentloaded"):
            stack.reload_release.set()
        expect(page.locator(".cc-chat-shell")).to_be_visible()
        assert page.evaluate("lifecycleProbe.origin") != result["origin"]
        assert writes == before_writes
        assert errors == []
    finally:
        (tmp_path / "pending-reload-http.json").write_text(json.dumps(stack.reload_records, indent=2) + "\n")
        stack.reload_release.set()
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_pagehide_aborts_pending_reads_and_history_return_reauthorizes(
        collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context, page, errors, writes = polling_page(stack, chat_browser_pool, engine)
    held, aborted, failed = [], [], []
    evidence = []
    passed = False
    phase = {"value": "navigation"}
    evidence_dir = Path(os.environ.get("CODEPIER_LIFECYCLE_EVIDENCE_DIR", str(tmp_path)))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def preserve(stage):
        (evidence_dir / f"pagehide-{engine}.json").write_text(json.dumps({
            "stage": stage, "engine": engine, "records": evidence,
            "console_aborts": aborted, "native_failed": failed, "pageerrors": errors,
        }, ensure_ascii=False, indent=2) + "\n")

    page.on("console", lambda message: aborted.append(message.text.removeprefix("LIFECYCLE_READ_ABORT "))
            if message.text.startswith("LIFECYCLE_READ_ABORT ") else None)
    page.on("requestfailed", lambda request: failed.append({
        "kind": parse_qs(urlsplit(request.url).query).get("kind", [""])[0],
        "failure": request.failure, "phase": phase["value"],
    }) if parse_qs(urlsplit(request.url).query).get("kind", [""])[0] in POLL_KINDS else None)

    def hold_read(route):
        kind = parse_qs(urlsplit(route.request.url).query).get("kind", [""])[0]
        # Routing is installed before arming. A background read in that gap
        # belongs to the unarmed document setup, not the cycle under test.
        if (kind in POLL_KINDS
                and route.request.headers.get("x-codepier-test-lifecycle") == original_origin):
            held.append(route)
            page.evaluate("count => {lifecycleProbe.heldRoutes = count;}", len(held))
        else:
            route.continue_()

    def release_routes():
        phase["value"] = "fixture-cleanup"
        for route in held:
            if route.request.failure is None:
                route.abort("aborted")
        held.clear()

    try:
        before_writes = list(writes)
        original_origin = page.evaluate("lifecycleProbe.origin")
        conversation = page.locator(".cc-chat-shell").get_attribute("data-conversation")
        page.evaluate("""() => window.addEventListener('pagehide', () => {
          lifecycleProbe.record('pagehide-after-cleanup', {signals:lifecycleProbe.signals.map(
            item => ({kind:item.kind, aborted:item.signal?.aborted === true})),
            owners:lifecycleProbe.owners.map(item => ({kind:item.kind,
              hasSignal:!!item.signal, aborted:item.signal?.aborted === true})),
            sameOwner:lifecycleProbe.owners.every(item => item.signal === lifecycleProbe.owners[0].signal)});
        })""")
        page.route("**/api/collaboration?*", hold_read)
        with page.expect_request(lambda request: parse_qs(urlsplit(request.url).query)
                                 .get("kind") == ["delegation_policies"]):
            page.evaluate("""() => {
              const p = lifecycleProbe;
              clearTimeout(p.poll.id); p.routeMarker = p.origin;
              p.armed = true; p.pending = p.poll.run();
            }""")
        page.wait_for_function("() => lifecycleProbe.heldRoutes === 4")
        assert len(held) == 4
        assert page.evaluate("lifecycleProbe.rows.map(row => row.kind).sort()") == POLL_KINDS
        assert page.evaluate("lifecycleProbe.rows.every(row => !row.settled)")
        # Capture which view-owned signals started the four held reads before
        # navigation, so a stale in-flight cycle cannot masquerade as this one.
        owners = page.evaluate("lifecycleProbe.owners.map(item => item.kind).sort()")
        assert owners == POLL_KINDS, {"owners": owners, "held": len(held)}
        page.evaluate("lifecycleProbe.record('navigation-start')")
        page.goto(stack.url + "/healthz")
        # Store only fixed request kinds and lifecycle facts synchronously in
        # this isolated context: destroying a document can drop its console
        # events before Playwright receives them, especially on Chromium.
        evidence = page.evaluate("JSON.parse(sessionStorage.getItem('codepier-lifecycle-read-evidence') || '[]')")
        preserve("replacement-loaded")
        assert evidence and all(item["origin"] == original_origin for item in evidence)
        assert len([item for item in evidence if item["event"] == "pagehide"]) == 1
        cleanup = next(item for item in evidence if item["event"] == "pagehide-after-cleanup")
        assert cleanup["sameOwner"]
        assert sorted(item["kind"] for item in cleanup["owners"]) == POLL_KINDS
        assert all(item["hasSignal"] and item["aborted"] for item in cleanup["owners"])
        navigation_start = next(i for i, item in enumerate(evidence) if item["event"] == "navigation-start")
        settled = [item for item in evidence[navigation_start + 1:] if item["event"] == "read-settled"]
        native_failures = [item for item in failed if item["phase"] == "navigation"]
        # Chromium can settle fetch before the application's pagehide abort;
        # api then removes the already-finished fetch's abort forwarding. The
        # owner must still abort, preventing retries after the old view leaves.
        assert all(item.get("error") in {"TypeError", "AbortError"} for item in settled)
        assert all("abort" in item["failure"].lower() or "cancel" in item["failure"].lower()
                   for item in native_failures)
        assert sorted({item["kind"] for item in settled + native_failures}) == POLL_KINDS
        assert errors == []
        preserve("native-terminal-before-fixture-cleanup")
        release_routes()
        page.unroute("**/api/collaboration?*", hold_read)

        def reauthorized(response):
            parsed = urlsplit(response.url)
            query = parse_qs(parsed.query)
            return (parsed.path == "/api/collaboration" and query.get("kind") == ["overview"]
                    and query.get("project") == [stack.project["id"]]
                    and query.get("conversation_id") == [conversation])

        with page.expect_response(reauthorized) as restored:
            page.go_back()
        assert restored.value.status == 200
        assert restored.value.json()["conversation"]["id"] == conversation
        expect(page.locator(".cc-chat-shell")).to_be_visible()
        expect(page.locator(".collaboration")).not_to_have_attribute("aria-busy", "true")
        page.locator('[data-cc-view="discussion"]').click()
        page.locator("#cc-message-input").fill("实际返回后可继续编辑")
        expect(page.locator("#cc-message-input")).to_have_value("实际返回后可继续编辑")
        preserve("returned-and-reauthorized")
        assert writes == before_writes
        assert errors == []
        passed = True
    finally:
        try:
            preserve("passed" if passed else "failed")
            release_routes()
        finally:
            context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_cancelled_beforeunload_keeps_original_mutation_and_editable_document(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context, page, errors, writes = polling_page(stack, chat_browser_pool, engine)
    receipts = []

    def hold_acknowledgement(route):
        # Commit once at the actual Hub, retaining only its original response.
        receipts.append((route, route.fetch()))
        page.evaluate("count => {lifecycleProbe.heldReceipts = count;}", len(receipts))

    try:
        page.locator('[data-cc-view="discussion"]').click()
        area = page.locator("#cc-message-input")
        area.fill("取消离开期间只保存一次的消息")
        # Retain the actual Document, not a timestamp whose precision can vary.
        # A replacement document must fail even when its URL is unchanged.
        original_document = page.evaluate_handle("document")
        page.route("**/api/collaboration/message", hold_acknowledgement)
        with page.expect_request(lambda request: request.url.endswith("/api/collaboration/message")):
            page.locator('#cc-command button[type="submit"]').click()
        page.wait_for_function("() => lifecycleProbe.heldReceipts === 1")
        assert len(receipts) == 1 and receipts[0][1].status == 200
        assert len(overview(stack)["messages"]) == 1
        page.evaluate("""() => window.addEventListener('beforeunload', event => {
          event.preventDefault(); event.returnValue = '';
        })""")
        page.on("dialog", lambda dialog: dialog.dismiss())
        with page.expect_event("dialog") as confirmation:
            page.close(run_before_unload=True)
        assert confirmation.value.type == "beforeunload"
        assert not page.is_closed()
        assert page.evaluate("original => document === original", original_document)
        original_document.dispose()
        route, response = receipts.pop()
        route.fulfill(response=response)
        expect(area).to_have_value("")
        expect(page.locator(".cc-feed")).to_contain_text("取消离开期间只保存一次的消息")
        area.fill("取消离开后的草稿")
        expect(area).to_have_value("取消离开后的草稿")
        expect(area).to_be_enabled()
        assert not page.locator(".collaboration").evaluate("node => node.inert")
        assert len([url for url in writes if url.endswith("/api/collaboration/message")]) == 1
        assert len(overview(stack)["messages"]) == 1
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pagehide_cancels_refresh_and_cached_restore_preserves_draft(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors, mutations, reads = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def observe(request):
        parsed = urlsplit(request.url)
        if parsed.path.startswith("/api/collaboration") and request.method == "POST":
            mutations.append(parsed.path)
        if parsed.path == "/api/collaboration" and request.method == "GET":
            reads.append(parse_qs(parsed.query).get("kind", [""])[0])

    page.on("request", observe)
    try:
        use_legacy_collaboration_ui(page, stack)
        page.goto(stack.url + "/#projects")
        page.fill("#username", "admin")
        page.fill("#password", stack.password)
        page.click("#login-form button")
        expect(page.locator("#page h1")).to_have_text("项目")
        open_project_collaboration(page, stack.project["id"])
        page.locator('[data-cc-action="create-room"]').click()
        area = page.locator("#cc-message-input")
        expect(area).to_be_enabled()
        area.fill("页面恢复保留的合成草稿")
        page.locator('[data-cc-view="jobs"]').click()
        expect(page.locator(".cc-coordination-list")).to_be_visible()

        # Only hold future reads. Never fabricate successful HTTP responses,
        # acknowledge a mutation, filter page errors, or wait on network-idle.
        page.evaluate("""() => {
          window.lifecycleReadProbe = {held: [], aborted: [], released: false};
          const original = window.fetch;
          window.lifecycleOriginalFetch = original;
          window.fetch = function(input, init = {}) {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            const kind = url.searchParams.get('kind');
            if (!lifecycleReadProbe.released && url.pathname === '/api/collaboration' &&
                ['timeline', 'overview', 'members', 'delegation_policies'].includes(kind)) {
              lifecycleReadProbe.held.push(kind);
              return new Promise((resolve, reject) => {
                const cancel = () => {
                  lifecycleReadProbe.aborted.push(kind);
                  reject(new DOMException('Synthetic pending read cancelled', 'AbortError'));
                };
                if (init.signal?.aborted) cancel();
                else init.signal?.addEventListener('abort', cancel, {once:true});
              });
            }
            return original.call(this, input, init);
          };
        }""")
        page.wait_for_function("() => lifecycleReadProbe.held.length === 4", timeout=12000)
        before_mutations = list(mutations)
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
        assert sorted(page.evaluate("lifecycleReadProbe.aborted")) == [
            "delegation_policies", "members", "overview", "timeline"]
        expect(page.locator(".collaboration")).to_have_attribute("aria-busy", "true")

        before_reads = len(reads)
        page.evaluate("""() => {
          lifecycleReadProbe.released = true;
          window.fetch = lifecycleOriginalFetch;
          window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }""")
        expect(page.locator(".collaboration")).not_to_have_attribute("aria-busy", "true")
        expect(page.locator(".cc-coordination-list")).to_be_visible()
        assert "overview" in reads[before_reads:]
        page.locator('[data-cc-view="discussion"]').click()
        expect(area).to_have_value("页面恢复保留的合成草稿")
        expect(area).to_be_enabled()
        assert mutations == before_mutations
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_repeated_lifecycle_does_not_replay_pending_message_or_erase_new_draft(
        collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    from tests.legacy_collaboration_ui import use_legacy_collaboration_ui
    context = chat_browser_pool(engine).new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    use_legacy_collaboration_ui(page, stack)
    errors, messages, held, response_order = [], [], [], []
    forwarded = 0
    page.on("pageerror", lambda error: errors.append(True))

    def observe(request):
        parsed = urlsplit(request.url)
        if parsed.path == "/api/collaboration/message" and request.method == "POST":
            messages.append(request)

    def hold_message(route):
        held.append(route)
        page.locator("html").evaluate("(node) => node.dataset.lifecycleMessageHeld = 'yes'")

    page.on("request", observe)
    try:
        page.goto(stack.url + "/#projects")
        page.fill("#username", "admin")
        page.fill("#password", stack.password)
        page.click("#login-form button")
        expect(page.locator("#page h1")).to_have_text("项目")
        open_project_collaboration(page, stack.project["id"])
        page.locator('[data-cc-action="create-room"]').click()
        area = page.locator("#cc-message-input")
        expect(area).to_be_enabled()
        area.fill("生命周期切换期间只保存一次的合成消息")
        page.route("**/api/collaboration/message", hold_message)
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator("html")).to_have_attribute("data-lifecycle-message-held", "yes")
        assert len(messages) == len(held) == 1
        original_request = messages[0]
        original_payload = original_request.post_data_json
        identity_matches = (original_payload["idempotency_key"] == original_payload["client_message_id"])
        assert identity_matches

        # These are explicit handler-contract events, not proof of bfcache.
        # Duplicate hide is harmless; ordinary pageshow must not restore an
        # already suspended room, and duplicate cached show must not replay it.
        page.evaluate("""() => {
          dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
          dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
          dispatchEvent(new PageTransitionEvent('pageshow', {persisted:false}));
        }""")
        expect(page.locator(".collaboration")).to_have_attribute("aria-busy", "true")
        page.evaluate("""() => {
          dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
          dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
        }""")
        expect(page.locator(".collaboration")).not_to_have_attribute("aria-busy", "true")
        expect(area).to_be_enabled()
        expect(area).to_have_value("生命周期切换期间只保存一次的合成消息")
        area.fill("恢复之后新输入的合成草稿")
        assert len(messages) == len(held) == 1
        def original_ack(response):
            if response.request != original_request:
                return False
            response_order.append("ack")
            return True

        def saved_projection(response):
            parsed = urlsplit(response.url)
            query = parse_qs(parsed.query)
            if (response.request.method != "GET" or parsed.path != "/api/collaboration"
                    or query.get("kind") != ["timeline"] or not response.ok
                    or not all(query.get(name) == [original_payload[name]] for name in
                               ("project", "environment_id", "conversation_id", "room_id"))):
                return False
            # The unique synthetic body is an event anchor only. Below, its ID
            # must equal the original server/client POST receipt and durable row.
            if not any(item.get("body_text") == original_payload["body_text"]
                       for item in response.json()["items"]):
                return False
            response_order.append("projection")
            return True

        # Install both observers before the only forward. A legitimate poll can
        # project the saved message before the browser receives the POST reply,
        # including a GET already in flight. Never require a later empty delta
        # to contain it again, or install an observer after pumping body events.
        with page.expect_response(original_ack, timeout=15000) as response:
            with page.expect_response(saved_projection, timeout=15000) as projection:
                forwarded += 1
                actual_response = held[0].fetch(max_redirects=0, max_retries=0, timeout=15000)
                assert actual_response.ok
            # Deliberately force projection-before-ack using the real response.
            # No fabricated success, second POST, retry, or route.continue_().
            held[0].fulfill(response=actual_response)
        assert response_order == ["projection", "ack"]
        assert forwarded == 1
        assert response.value.ok
        persisted_message_id = response.value.json()["message"]["id"]
        same_receipt = actual_response.json()["message"]["id"] == persisted_message_id
        assert same_receipt
        projected = [item for item in projection.value.json()["items"]
                     if item.get("body_text") == original_payload["body_text"]]
        assert len(projected) == 1
        same_projection = projected[0].get("id") == persisted_message_id
        assert same_projection
        page.unroute("**/api/collaboration/message", hold_message)
        expect(page.locator(".cc-feed")).to_contain_text("生命周期切换期间只保存一次的合成消息")
        expect(area).to_have_value("恢复之后新输入的合成草稿")
        assert len(messages) == 1
        # Read the durable result independently; do not issue a second POST to
        # turn a missing response into success or simulate a mutation receipt.
        timeline = stack.must(stack.client.get("/api/collaboration", params={
            "project": original_payload["project"],
            "environment_id": original_payload["environment_id"],
            "conversation_id": original_payload["conversation_id"],
            "room_id": original_payload["room_id"], "kind": "timeline", "limit": 100}))
        stored = [message for message in timeline["items"]
                  if message.get("id") == persisted_message_id]
        assert len(stored) == 1
        same_body = stored[0].get("body_text") == original_payload["body_text"]
        assert same_body
        assert sum(message.get("body_text") == original_payload["body_text"]
                   for message in timeline["items"]) == 1
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("transition", ["resume", "leave"])
def test_lifecycle_before_app_initialization_is_a_safe_noop(
        collaboration_stack, chat_browser_pool, engine, transition):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    errors, held_app = [], []
    page.on("pageerror", lambda error: errors.append(
        "ReferenceError" if error.name == "ReferenceError" else "other"))

    def hold_app(route):
        held_app.append(route)

    def app_script(url):
        return urlsplit(url).path == "/static/app.js"
    page.route(app_script, hold_app)
    try:
        # Use the real ordered entrypoint. Only app.js is withheld: the
        # collaboration listeners exist while app.js has not declared S yet.
        page.goto(stack.url + "/#projects", wait_until="commit")
        page.wait_for_function("() => Boolean(window.CodePierCollaboration)", timeout=5000)
        assert len(held_app) == 1
        assert page.evaluate("typeof S") == "undefined"
        if transition == "resume":
            # Synthetic events cover handler safety/repetition, not bfcache.
            for persisted in (False, True):
                page.evaluate("""persisted => {
                  dispatchEvent(new PageTransitionEvent('pagehide', {persisted}));
                  dispatchEvent(new PageTransitionEvent('pageshow', {persisted}));
                }""", persisted)
            held_app[0].continue_()
            page.unroute(app_script, hold_app)
            expect(page.locator("#username")).to_be_visible()
        else:
            # This navigation exercises a genuine early document departure.
            page.goto("about:blank")
            assert page.url == "about:blank"
        assert errors == []
    finally:
        context.close()
