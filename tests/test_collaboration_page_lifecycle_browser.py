"""Browser document lifecycle must cancel reads without replaying room actions."""
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


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
        page.goto(stack.url + "/#projects")
        page.fill("#username", "admin")
        page.fill("#password", stack.password)
        page.click("#login-form button")
        expect(page.locator("#page h1")).to_have_text("项目映射")
        page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
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
