"""Real Chromium/WebKit acceptance of the new compact estimate card."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.context_usage_fixtures import synthetic_dashboard

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1366, 320])
def test_calibrated_card_counts_filters_export_and_mobile(chat_browser_pool, browser_kind, width):
    context = chat_browser_pool(browser_kind).new_context(
        viewport={"width": width, "height": 900}, color_scheme="dark")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_content('<div id="fixture"></div>')
        page.add_style_tag(content="body{margin:16px;background:#171717;color:#eee;font-family:sans-serif} #fixture{padding:20px;border-radius:24px;background:#40364f} *{box-sizing:border-box}")
        page.add_style_tag(path=str(ROOT / "web/token-usage.css"))
        page.add_script_tag(path=str(ROOT / "web/token-usage.js"))
        page.evaluate("""data => {
            window.pending=[];
            const api=window.CodePierTokenUsage, box=document.querySelector('#fixture');
            box.innerHTML=api.dashboard(data);
            api.bindDashboard(box,url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),data);
        }""", synthetic_dashboard(calibrated=True))
        card = page.locator("[data-token-dashboard]")
        expect(card.locator(".is-total")).to_contain_text("按调用估算 Token")
        expect(card.locator(".is-total")).to_contain_text("1.5M")
        expect(card.locator(".is-cost")).to_contain_text("$2.43")
        expect(card).to_contain_text("10 次工具调用 · 约 11 个模型轮次")
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        assert card.bounding_box()["height"] < 340
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        evidence = ROOT / ".work/call-estimate-preview"
        evidence.mkdir(parents=True, exist_ok=True)
        card.screenshot(path=str(evidence / f"calls-{browser_kind}-{width}.png"))
        card.locator("[data-token-filters] > summary").focus()
        page.keyboard.press("Enter")
        card.locator("[data-token-panel=breakdown] > summary").click()
        expect(card.locator("[data-token-panel=breakdown]")).to_contain_text("工具文本 Token：约 11K")
        expect(card.locator("[data-token-panel=breakdown]")).to_contain_text("上下文重放参考：约 206.49K")
        card.locator("[data-token-panel=settings] > summary").click()
        field = card.locator("[data-token-filter=cache_read_percent]")
        field.fill("100")
        field.press("Tab")
        page.wait_for_function("pending.length===1")
        page.evaluate("data=>pending[0].resolve(data)", synthetic_dashboard(percent=100, calibrated=True))
        expect(card.locator(".is-cost")).to_contain_text("$1.85")
        expect(card.locator(".is-total")).to_contain_text("1.5M")
        expect(field).to_have_value("100")
        with page.expect_download() as downloaded:
            card.locator("[data-token-export]").click()
        exported = json.loads(Path(downloaded.value.path()).read_text())
        assert exported["kind"] == "mcp_call_calibration_estimate"
        assert exported["summary"]["call_estimate"]["estimated_model_rounds"] == 11
        assert exported["summary"]["call_estimate"]["actual_usage"] is None
        assert exported["summary"]["reference_cost"]["scope"] == "visible_tool_text_equivalent"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
@pytest.mark.parametrize("width,height", [(1366, 768), (390, 844)])
def test_calibrated_card_in_real_overview_layout(chat_browser_pool, stack, browser_kind, width, height):
    from tests.test_ui_unification import _login
    from tests.test_ui_comfort import within

    context = chat_browser_pool(browser_kind).new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def overview(route):
        response = route.fetch()
        data = response.json()
        data["token_usage"] = synthetic_dashboard(calibrated=True)
        route.fulfill(response=response, json=data)

    page.route("**/api/overview", overview)
    try:
        _login(page, stack)
        card = page.locator(".overview-summary [data-token-dashboard]")
        expect(card.locator(".is-total")).to_contain_text("1.5M")
        expect(card.locator(".is-cost")).to_contain_text("$2.43")
        within(page, ".token-dashboard-metric,.token-dashboard-note,.token-dashboard-limit", width, height)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        evidence = ROOT / ".work/call-estimate-preview"
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f"overview-calls-{browser_kind}-{width}.png"))
        assert not errors
    finally:
        context.close()
