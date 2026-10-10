"""Actual Chromium/WebKit renders with synthetic context stats and no accounts."""
from pathlib import Path
import json

import pytest
from playwright.sync_api import expect

from tests.context_usage_fixtures import synthetic_dashboard

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 390, 320])
def test_context_card_compact_ranges_wire_details_and_late_refresh(chat_browser_pool, browser_kind, width):
    context = chat_browser_pool(browser_kind).new_context(viewport={"width": width, "height": 1000})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_content('<div id="fixture"></div>')
        page.add_style_tag(content="body{margin:16px;background:#171717;color:#eee;font-family:sans-serif} #fixture{padding:20px;border-radius:24px;background:#40364f} *{box-sizing:border-box}")
        page.add_style_tag(path=str(ROOT / "web/token-usage.css"))
        page.add_script_tag(path=str(ROOT / "web/token-usage.js"))
        page.evaluate("""data => {
            window.pending = [];
            const api = window.CodePierTokenUsage, box = document.querySelector('#fixture');
            box.innerHTML = api.dashboard(data);
            api.bindDashboard(box, url => new Promise((resolve, reject) => pending.push({url,resolve,reject})), data);
        }""", synthetic_dashboard())
        card = page.locator("[data-token-dashboard]")
        expect(card.locator(".is-total")).to_contain_text("含上下文 Token")
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        expect(card.locator(".is-cost")).to_contain_text("$0.5174")
        expect(card.locator(".token-dashboard-limit")).to_contain_text("131.85K–363.3K")
        expect(card).to_contain_text("输入缓存 90% 假设")
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        expect(card.locator("[data-token-panel=breakdown] > p").first).to_be_hidden()
        assert card.bounding_box()["height"] < 340
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        evidence = ROOT / ".work/context-estimate-preview"
        evidence.mkdir(parents=True, exist_ok=True)
        card.screenshot(path=str(evidence / f"context-{browser_kind}-{width}.png"))
        card.locator("[data-token-filters] > summary").focus()
        page.keyboard.press("Enter")
        card.locator("[data-token-panel=breakdown] > summary").click()
        expect(card.locator("[data-token-panel=breakdown]")).to_contain_text("工具文本 Token：约 11K")
        expect(card.locator("[data-token-panel=breakdown]")).to_contain_text("不包含历史重读")
        card.screenshot(path=str(evidence / f"context-details-{browser_kind}-{width}.png"))
        card.locator("[data-token-panel=settings] > summary").click()
        assumption = card.locator("[data-token-filter=cache_read_percent]")
        assumption.fill("0")
        assumption.press("Tab")
        page.wait_for_function("pending.length === 1")
        assumption.fill("100")
        assumption.press("Tab")
        page.wait_for_function("pending.length === 2")
        page.evaluate("data => pending[1].resolve(data)", synthetic_dashboard(100))
        expect(card.locator(".is-cost")).to_contain_text("$0.3339")
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        page.evaluate("data => pending[0].resolve(data)", synthetic_dashboard(0))
        expect(assumption).to_have_value("100")
        expect(card.locator(".is-cost")).to_contain_text("$0.3339")
        assumption.fill("50")
        assumption.press("Tab")
        page.wait_for_function("pending.length === 3")
        page.evaluate("pending[2].reject(new Error('synthetic failure'))")
        expect(assumption).to_have_value("100")
        expect(card.locator("[role=status]")).to_contain_text("上一次结果")
        with page.expect_download() as downloaded:
            card.locator("[data-token-export]").click()
        exported = json.loads(Path(downloaded.value.path()).read_text())
        assert exported["kind"] == "mcp_context_scenario_estimate"
        assert exported["summary"]["context_estimate"]["actual_usage"] is None
        card.locator("[data-token-filters] > summary").click()
        expect(assumption).to_be_hidden()
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1280, 720), (1366, 768), (1180, 640)])
def test_context_estimate_in_real_short_desktop_overview(chat_browser_pool, stack, browser_kind, scheme, width, height):
    """Real overview parents preserve their original short-screen geometry."""
    from scripts.ui_comfort_audit import MEASURE
    from tests.test_ui_unification import _login, _set_scheme
    from tests.test_ui_comfort import within

    context = chat_browser_pool(browser_kind).new_context(
        viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def overview_fixture(route):
        response = route.fetch()
        data = response.json()
        data["token_usage"] = synthetic_dashboard()
        route.fulfill(response=response, json=data)

    page.route("**/api/overview", overview_fixture)
    try:
        _login(page, stack)
        _set_scheme(page, scheme)
        card = page.locator(".overview-summary [data-token-dashboard]")
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        expect(card.locator(".is-cost")).to_contain_text("$0.5174")
        expect(card.locator(".token-dashboard-limit")).to_contain_text("131.85K–363.3K")
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        within(page, ".stats,.codepier-focus-card,.workspace-operations", width, height)
        within(page, ".token-dashboard-metric,.token-dashboard-note,.token-dashboard-limit,"
                     "[data-token-filters] > summary", width, height)
        report = page.evaluate(MEASURE)
        assert report["documentWidth"] <= width + 1
        assert not report["failures"], (scheme, width, report["failures"])
        evidence = ROOT / ".work/context-estimate-preview"
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f"overview-context-{browser_kind}-{scheme}-{width}.png"),
                        animations="disabled")
        (evidence / f"overview-context-{browser_kind}-{scheme}-{width}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
        card.locator("[data-token-filters] > summary").focus()
        page.keyboard.press("Enter")
        card.locator("[data-token-panel=breakdown] > summary").click()
        expect(card.locator("[data-token-panel=breakdown]")).to_contain_text("工具文本 Token：约 11K")
        card.locator("[data-token-filters] > summary").click()
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        within(page, ".stats,.codepier-focus-card,.workspace-operations", width, height)
        assert not errors
    finally:
        context.close()
