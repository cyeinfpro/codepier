"""Site-wide experience contracts against a disposable, real Hub and Agent.

The route matrix exercises real HTTP, rendered CSS, keyboard navigation and
appearance media queries. It never connects a user account or changes production.
"""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[1]
ROUTES = [
    "overview", "projects", "workbench", "native", "collaboration", "workflows",
    "artifacts", "audit", "connect", "profiles", "mcp-gateway", "integrations",
    "identity", "members", "roles", "devices", "vps", "identity-admin",
    "diagnostics", "settings",
]


def login(page, stack, route="overview", username="admin"):
    page.goto(stack.url + "/#" + route)
    page.fill("#username", username)
    page.fill("#password", stack.password)
    page.click("#login-form button")
    expect(page.locator("#page h1")).to_be_visible()


def navigate(page, route):
    page.evaluate("(route) => navigate(route)", route)
    expect(page.locator("#page .skeleton")).to_have_count(0)
    expect(page.locator("#page h1")).to_be_visible()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [360, 390, 1440])
def test_every_route_has_consistent_frame_and_no_horizontal_overflow(chat_browser_pool, stack, engine, width):
    context = chat_browser_pool(engine).new_context(viewport={"width": width, "height": 1000 if width == 1440 else 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        assert page.locator('link[href*="/static/experience.css"]').count() == 1
        for route in ROUTES:
            navigate(page, route)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), route
            assert page.locator("#page").get_attribute("aria-busy") != "true", route
            assert page.locator("#page h1").inner_text().strip(), route
            if route != "native":
                assert page.evaluate("""() => {
                  const bar = document.querySelector(".topbar").getBoundingClientRect();
                  return [...document.querySelectorAll(".topbar button")].every(x => {
                    const r = x.getBoundingClientRect();
                    return !r.height || r.bottom <= bar.bottom + 1;
                  });
                }"""), route
                assert page.locator('.nav [aria-current="page"]').get_attribute("data-nav") == route
                assert page.locator("#breadcrumb-page").inner_text().strip()
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [360, 390, 1440])
def test_navigation_search_keyboard_escape_and_focus_return(chat_browser_pool, stack, width):
    context = chat_browser_pool("chromium").new_context(viewport={"width": width, "height": 844})
    page = context.new_page()
    try:
        login(page, stack)
        if width < 900:
            page.locator(".mobile-menu").click()
            expect(page.locator(".mobile-close")).to_be_focused()
            assert page.locator(".main").evaluate("el => el.inert")
        query = page.locator("#navigation-query")
        query.fill("not-a-page")
        expect(page.locator("#navigation-status")).to_contain_text("没有匹配")
        expect(page.locator(".nav [data-nav]:visible")).to_have_count(0)
        query.press("Escape")
        expect(query).to_have_value("")
        expect(query).to_be_focused()
        if width < 900:
            expect(page.locator(".sidebar")).to_have_class("sidebar open")
        query.fill("设置中心")
        expect(page.locator(".nav [data-nav]:visible")).to_have_count(1)
        query.press("Tab")
        expect(page.locator('.nav [data-nav="settings"]')).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#settings-center")).to_be_visible()
        expect(page.locator("#page h1")).to_be_focused()
        expect(query).to_have_value("")
        if width < 900:
            assert not page.locator(".main").evaluate("el => el.inert")
            page.locator(".mobile-menu").click()
            page.keyboard.press("Escape")
            expect(page.locator(".mobile-menu")).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


def test_navigation_filter_cannot_reveal_unavailable_identity_routes(chat_browser_pool, stack):
    from tests.test_account_connection_journeys import local_user
    username, _ = local_user(stack)
    context = chat_browser_pool("chromium").new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    try:
        login(page, stack, username=username)
        query = page.locator("#navigation-query")
        for name in ["", "members", "身份管理", "identity-admin"]:
            query.fill(name)
            # Personal-space owners retain the members explanation.
            if not page.evaluate("CodePierIdentity.canNavigate('members')"):
                expect(page.locator('.nav [data-nav="members"]')).to_be_hidden()
            expect(page.locator('.nav [data-nav="identity-admin"]')).to_be_hidden()
    finally:
        context.close()


def ratio(a, b):
    def luminance(value):
        values = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return sum(v * w for v, w in zip(values, [.2126, .7152, .0722]))
    x, y = sorted([luminance(a), luminance(b)])
    return (y + .05) / (x + .05)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_color_contrast_reduced_motion_and_readable_controls(chat_browser_pool, stack, engine, scheme):
    context = chat_browser_pool(engine).new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme, reduced_motion="reduce")
    page = context.new_page()
    try:
        login(page, stack, "settings")
        page.evaluate("(scheme) => CodePierAppearance.setPreference(scheme)", scheme)
        pairs = [("text", "panel"), ("muted", "panel"), ("soft", "panel"),
                 ("primary-text", "primary"), ("danger", "danger-bg"),
                 ("warning", "warning-bg"), ("success", "success-bg"),
                 ("info", "info-bg"), ("purple", "purple-bg"),
                 ("cover-text", "cover-project"), ("cover-muted", "cover-project")]
        colors = page.evaluate("""(pairs) => {
          const style = getComputedStyle(document.documentElement);
          return pairs.map(([a,b]) => [style.getPropertyValue('--ui-'+a).trim(), style.getPropertyValue('--ui-'+b).trim()]);
        }""", pairs)
        for pair, values in zip(pairs, colors):
            assert ratio(*values) >= 4.5, (scheme, pair, values, ratio(*values))
        assert page.locator("#page").evaluate("el => getComputedStyle(el).animationName") == "none"
        assert page.locator(".settings-groups button").first.evaluate("el => el.getBoundingClientRect().height") >= 44
        page.locator(".settings-groups button").first.focus()
        assert page.locator(".settings-groups button").first.evaluate("el => parseFloat(getComputedStyle(el).outlineWidth)") >= 2
        page.emulate_media(contrast="more")
        assert page.locator(".panel").first.evaluate("el => getComputedStyle(el).borderTopColor") == page.locator("body").evaluate("el => getComputedStyle(el).color")
        page.set_viewport_size({"width": 720, "height": 500})
        # 720 CSS px is the layout width of a 1440px display at 200% browser zoom.
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


def test_loading_error_retry_and_stale_navigation_have_context(chat_browser_pool, stack):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        login(page, stack)
        pending = []
        page.route("**/api/vps*", lambda route: pending.append(route))
        page.evaluate("void navigate('vps')")
        expect(page.locator(".page-loading")).to_contain_text("VPS 管理")
        expect(page.locator("#page")).to_have_attribute("aria-busy", "true")
        navigate(page, "projects")
        expect(page.locator("#page h1")).to_have_text("项目映射")
        for route in pending:
            route.fulfill(status=400, json={"error": {"message": "Fixture unavailable"}})
        page.unroute("**/api/vps*")
        expect(page.locator("#page h1")).to_have_text("项目映射")
        page.route("**/api/vps*", lambda route: route.fulfill(status=400, json={"error": {"message": "Fixture unavailable"}}))
        page.evaluate("void navigate('vps')")
        expect(page.locator(".page-error")).to_be_visible()
        expect(page.locator(".page-error h1")).to_have_text("VPS 管理")
        expect(page.locator("#page")).not_to_have_attribute("aria-busy", "true")
        page.unroute("**/api/vps*")
        page.locator('.page-error [data-action="refresh"]').click()
        expect(page.locator(".page-error")).to_have_count(0)
        expect(page.locator("#page h1")).to_have_text("VPS 管理")
    finally:
        context.close()


def test_native_navigation_releases_stale_loading_state(chat_browser_pool, stack):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 360, "height": 844})
    page = context.new_page()
    try:
        login(page, stack)
        pending = []
        page.route("**/api/vps*", lambda route: pending.append(route))
        page.evaluate("void navigate('vps')")
        expect(page.locator("#page")).to_have_attribute("aria-busy", "true")
        navigate(page, "native")
        expect(page.locator("#page")).not_to_have_attribute("aria-busy", "true")
        assert page.locator("#chat-root").is_visible()
        for route in pending:
            route.fulfill(status=400, json={"error": {"message": "Late fixture reply"}})
        expect(page.locator("#chat-root")).to_be_visible()
        assert page.locator(".page-error").count() == 0
        for selector in [".chat-text-button", "#chat-catalog-retry"]:
            for button in page.locator(selector).all():
                if button.is_visible():
                    assert button.bounding_box()["height"] >= 44
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_collaboration_shared_tokens_keep_reduced_motion(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.set_content('<div class="cc-workspace cc-chat-shell"><details class="cc-drawer" open><summary>Details</summary></details><div class="cc-project-strip"><button class="btn">Project</button></div></div>')
        for name in ["tokens.css", "collaboration-workspace.css", "experience.css"]:
            page.add_style_tag(path=str(ROOT / "web" / name))
        assert page.locator(".cc-workspace").evaluate("""el => {
          const style = getComputedStyle(el), root = getComputedStyle(document.documentElement);
          return [['radius-control', 'control-radius'], ['radius-shell', 'window-radius'],
                  ['motion-feedback', 'motion-fast'], ['motion-panel', 'motion-normal'],
                  ['ease', 'ease']].every(([a,b]) =>
              style.getPropertyValue('--cc-'+a).trim() === root.getPropertyValue('--ui-'+b).trim());
        }""")
        assert page.locator(".cc-drawer").evaluate("el => getComputedStyle(el).animationDuration") == "0.18s"
        for width in [360, 390]:
            page.set_viewport_size({"width": width, "height": 844})
            for selector in [".cc-drawer summary", ".cc-project-strip .btn"]:
                assert page.locator(selector).evaluate("el => el.getBoundingClientRect().height") >= 44
        page.emulate_media(reduced_motion="reduce")
        assert page.locator(".cc-drawer").evaluate("el => getComputedStyle(el).animationName") == "none"
    finally:
        context.close()


def test_experience_asset_is_content_addressed():
    manifest = json.loads((ROOT / "web/assets-manifest.json").read_text())
    assert "experience.css" in manifest["assets"]
    css = (ROOT / "web/experience.css").read_text()
    assert "prefers-reduced-motion" in css
    assert "prefers-contrast" in css
    assert "backdrop-filter:" not in css
    assert "transition: all" not in css
