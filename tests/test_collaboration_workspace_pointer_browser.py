"""Local render scheduling only; no network, accounts, or actions are simulated as success."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("finish", ["release", "navigate", "clear"])
def test_pointer_deferred_projection_flushes_without_another_read(chat_browser_pool, engine, finish):
    context = chat_browser_pool(engine).new_context(viewport={"width": 600, "height": 500})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.route("https://workspace.invalid/**", lambda route: route.fulfill(
            content_type="text/html", body='<!doctype html><html><body><main class="cc-workspace" data-project="p" data-environment="e">'
            '<section data-cc-panel="jobs"><div class="cc-workspace-records"></div></section></main></body></html>'))
        page.goto("https://workspace.invalid/")
        page.add_style_tag(content="button{width:180px;height:80px}button:active{opacity:.8}body{padding:30px}")
        for name in ["collaboration-runtime.js", "collaboration-workspace.js"]:
            page.add_script_tag(content=(ROOT / "web" / name).read_text())
        page.evaluate("""() => {
          window.total = 80;
          window.pointerEvents = [];
          for (const name of ["pointerdown", "pointerup", "click"])
            document.addEventListener(name, event => pointerEvents.push({name, tag:event.target.tagName}));
          window.renderState = {project:'p', environment:'e', generation:1,
            view:'jobs', snapshot:{room:{state:'active'}}};
          window.renderChat = {messages:[]};
          window.localWorkspace = CodePierCollaborationWorkspace.create({
            state:renderState, E:String, button:()=>'', chat:()=>renderChat,
            mentionSlots:()=>[], projectLabel:String, messageText:()=>'', bindDetails:()=>{},
            renderJobs:()=>'<button data-cc-action="goal-more">More</button><output>'+total+'</output>',
            renderAgents:()=>'', renderMonitor:()=>''
          });
          localWorkspace.refresh();
        }""")
        button = page.locator('[data-cc-action="goal-more"]')
        original = button.element_handle()
        bounds = button.bounding_box()
        assert bounds
        page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
        page.mouse.down()
        assert original.evaluate("(node)=>node.matches(':active')"), page.evaluate("pointerEvents")
        page.evaluate("()=>{total=100;localWorkspace.refresh();}")
        expect(page.locator("output")).to_have_text("80")
        assert original.evaluate("(node)=>node.isConnected")
        assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred") is True
        if finish == "navigate":
            page.evaluate("""() => {
              renderState.generation++;
              document.querySelector('.cc-workspace').outerHTML =
                '<main class="cc-workspace" data-project="other" data-environment="e"><output>new scope</output></main>';
            }""")
        elif finish == "clear":
            page.evaluate("()=>localWorkspace.clear()")
        # A real drag-away release cancels button activation. Only the deferred
        # local render may update the projection; no new read or action exists.
        page.mouse.move(500, 400)
        page.mouse.up()
        if finish == "release":
            expect(page.locator("output")).to_have_text("100", timeout=1500)
            assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred") is False
        else:
            page.wait_for_timeout(150)
            if finish == "navigate":
                expect(page.locator("output")).to_have_text("new scope")
            else:
                expect(page.locator('[data-cc-panel="jobs"]')).to_be_empty()
                assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred") is False
        assert errors == []
    finally:
        context.close()
