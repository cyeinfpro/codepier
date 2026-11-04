"""Current room UI geometry and theme contracts, using synthetic read-only data.

Unlike test_collaboration_fluid_ui, this exercises the shipped room store,
renderer, application shell and styles, not the legacy collaboration fixture.
"""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser
from tests.browser_support import login_panel as _login, set_appearance as _set_scheme


def _room_data(long_content):
    room = {
        "id": "design-room", "title": "设计回归房间_" * 12,
        "state": "active", "revision": 1, "version": 1,
        "projects": [], "members": [], "channels": [],
        "display": {"subtitle": "仅合成数据，不发送消息"},
    }
    messages = [
        {
            "id": f"design-message-{i}", "version": 1,
            "server_sequence": i + 1, "created_at": 1700000000 + i,
            "author_kind": "owner" if i % 2 else "dot",
            "body_text": ("这条消息应完整可见。" if i == 49 else
                          f"合成消息 {i}\n" + ("长文本与布局验证 " * 30)
                          + "\n" + ("long_unbroken_identifier_" * 30)),
        }
        for i in range(50)
    ] if long_content else []
    return room, messages


def _fixture_routes(page, long_content):
    room, messages = _room_data(long_content)
    mutations = []

    def serve(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method != "GET":
            mutations.append((request.method, path))
            route.fulfill(status=405, json={"error": "Synthetic read-only fixture"})
        elif path.endswith("/events"):
            # Disable this fixture's streaming subscription, leaving the real
            # renderer's ordinary bounded polling/read path available.
            route.fulfill(status=204, body="")
        elif path == "/api/collaboration/rooms":
            route.fulfill(json={"items": [room], "next_cursor": None})
        elif path.endswith("/design-room/messages"):
            route.fulfill(json={
                "messages": messages, "revision": 1, "visibility_revision": "design-v1",
                "works": [], "next_cursor": None,
            })
        elif path.endswith("/design-room"):
            route.fulfill(json={"room": room})
        else:
            route.fulfill(json={"items": [], "works": [], "next_cursor": None})
    page.route("**/api/collaboration/**", serve)
    return mutations


def _geometry(page):
    return page.evaluate("""() => {
      const box = sel => {
        const n = document.querySelector(sel), r = n.getBoundingClientRect();
        return {x:r.x, y:r.y, width:r.width, height:r.height, bottom:r.bottom, right:r.right};
      };
      const feed = document.querySelector('.cr-feed');
      return {
        width:innerWidth, height:innerHeight,
        docWidth:document.documentElement.scrollWidth,
        docHeight:document.documentElement.scrollHeight,
        shell:box('.cr-shell'), composer:box('.cr-composer'),
        input:box('#cr-message'), feed:box('.cr-feed'),
        feedScroll:feed.scrollHeight, feedClient:feed.clientHeight,
      };
    }""")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(667, 375), (320, 568)])
@pytest.mark.parametrize("long_content", [False, True], ids=["empty", "long"])
def test_current_room_theme_and_bounded_scrolling(
    stack, tmp_path, engine, scheme, width, height, long_content
):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=scheme, reduced_motion="reduce",
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        mutations = _fixture_routes(page, long_content)
        try:
            _login(page, stack, "collaboration")
            _set_scheme(page, scheme)
            expect(page.locator(".cr-shell")).to_be_visible()
            expect(page.locator("[data-cr-title]")).to_contain_text("设计回归房间")
            expect(page.locator(".cr-message")).to_have_count(50 if long_content else 0)
            expect(page.locator("[data-cr-empty]")).to_be_hidden() if long_content else \
                expect(page.locator("[data-cr-empty]")).to_be_visible()
            page.screenshot(path=str(tmp_path / f"{engine}-{scheme}-{width}-initial-room.png"))
            metrics = _geometry(page)
            assert metrics["docWidth"] <= width + 1, metrics
            assert metrics["docHeight"] <= height + 1, metrics
            for key in ("shell", "composer", "input", "feed"):
                rect = metrics[key]
                assert rect["height"] > 0, (key, metrics)
                assert rect["x"] >= -1 and rect["right"] <= width + 1, (key, metrics)
                assert rect["y"] >= -1 and rect["bottom"] <= height + 1, (key, metrics)
            assert metrics["feed"]["bottom"] <= metrics["composer"]["y"] + 1, metrics
            if height <= 520:
                assert metrics["feed"]["height"] >= 112, metrics
            if long_content:
                assert metrics["feedScroll"] > metrics["feedClient"], metrics
                page.locator(".cr-feed").evaluate("n => { n.scrollTop = 0; }")
                assert page.locator(".cr-feed").evaluate("n => n.scrollTop") == 0
                page.locator(".cr-feed").evaluate("n => { n.scrollTop = n.scrollHeight; }")
                assert page.locator(".cr-feed").evaluate("n => n.scrollTop") > 0
                message = page.locator(".cr-message").last
                expect(message).to_contain_text("这条消息应完整可见")
                assert message.evaluate("""node => {
                    const feed = document.querySelector('.cr-feed').getBoundingClientRect();
                    const rect = node.getBoundingClientRect();
                    return rect.top >= feed.top - 1 && rect.bottom <= feed.bottom + 1;
                }"""), _geometry(page)
            page.locator("#cr-message").fill("合成草稿，仅检查不发送")
            expect(page.locator(".cr-send")).to_be_enabled()
            for selector in (".cr-send", '[data-cr-action="attach"]',
                             '.cr-recipient select', '.cr-room-actions .cr-button'):
                for control in page.locator(selector).all():
                    rect = control.bounding_box()
                    assert rect and rect["height"] >= 44 and rect["width"] >= 44, (selector, rect)
            page.locator(".cr-send").click(trial=True)
            assert page.locator("#cr-message").evaluate(
                "n => getComputedStyle(n).fontSize"
            ) == "16px"
            palette = page.evaluate("""() => {
              const root=getComputedStyle(document.documentElement);
              const color=(selector,property)=>getComputedStyle(document.querySelector(selector))[property];
              const resolve=name=>{
                const p=document.createElement('span');p.style.color=root.getPropertyValue(name);
                document.body.append(p);const c=getComputedStyle(p).color;p.remove();return c;
              };
              return {
                shell:color('.cr-shell','backgroundColor'), panel:resolve('--ui-panel'),
                send:color('.cr-send','backgroundColor'), primary:resolve('--ui-primary'),
                primaryHover:resolve('--ui-primary-hover'),
                sendText:color('.cr-send','color'), primaryText:resolve('--ui-primary-text')
              };
            }""")
            assert palette["shell"] == palette["panel"], palette
            # The trial click above leaves the pointer on the send button.
            assert palette["send"] == palette["primaryHover"], palette
            page.mouse.move(0, 0)
            expect(page.locator(".cr-send")).to_have_css("background-color", palette["primary"])
            assert palette["sendText"] == palette["primaryText"], palette
            page.screenshot(path=str(tmp_path / f"{engine}-{scheme}-{width}-room.png"))
            assert not errors, errors
            assert not mutations, mutations
        finally:
            context.close()
            browser.close()
