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
        expect(card.locator(".is-cost")).to_contain_text("$0.4256")
        expect(card.locator(".token-dashboard-limit")).to_contain_text("131.85K–363.3K")
        expect(card).to_contain_text("输入缓存 95% 假设")
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        expect(card.locator("[data-token-panel], [data-token-export]")).to_have_count(0)
        # The responsive card may stack; readability and reachability beat a fixed height cap.
        for metric in card.locator(".token-dashboard-metric").all():
            metric.scroll_into_view_if_needed()
            box = metric.bounding_box()
            assert box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1
            assert box["y"] >= -1 and box["y"] + box["height"] <= 901
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        evidence = ROOT / ".work/context-estimate-preview"
        evidence.mkdir(parents=True, exist_ok=True)
        card.screenshot(path=str(evidence / f"context-{browser_kind}-{width}.png"))
        card.locator("[data-token-filters] > summary").focus()
        page.keyboard.press("Enter")
        expect(card.locator('.token-trend-section')).to_be_visible()
        expect(card.locator('[data-token-panel], [data-token-export]')).to_have_count(0)
        field=card.locator('[data-token-filter=period]')
        field.select_option('7d');page.wait_for_function('pending.length===1')
        field.select_option('30d');page.wait_for_function('pending.length===2')
        result=synthetic_dashboard(100);result['period']['key']='30d'
        page.evaluate('data=>pending[1].resolve(data)',result)
        expect(card.locator('.is-cost')).to_contain_text('$0.3339')
        expect(card.locator('.is-total')).to_contain_text('206.49K')
        stale=synthetic_dashboard(0);stale['period']['key']='7d'
        page.evaluate('data=>pending[0].resolve(data)',stale)
        expect(field).to_have_value('30d')
        expect(card.locator('.is-cost')).to_contain_text('$0.3339')
        field.select_option('today');page.wait_for_function('pending.length===3')
        page.evaluate("pending[2].reject(new Error('synthetic failure'))")
        expect(field).to_have_value('30d')
        expect(card.locator('[role=status]')).to_contain_text('上一次结果')
        exported=json.loads(page.evaluate('data=>CodePierTokenUsage.exportJSON(data)',result))
        assert exported["kind"] == "mcp_context_scenario_estimate"
        assert exported["summary"]["context_estimate"]["actual_usage"] is None
        card.locator("[data-token-filters] > summary").click()
        expect(field).to_be_hidden()
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("browser_kind", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1280, 720), (1366, 768), (1180, 640)])
def test_context_estimate_in_real_short_desktop_overview(chat_browser_pool, stack, browser_kind, scheme, width, height):
    """Always-visible statistics precede scroll-accessible work panels on short screens."""
    from scripts.ui_comfort_audit import MEASURE
    from tests.test_ui_unification import _login, _set_scheme

    context = chat_browser_pool(browser_kind).new_context(
        viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def token_usage_fixture(route):
        response = route.fetch()
        route.fulfill(response=response, json=synthetic_dashboard())

    # Statistics now load independently of the lightweight overview response.
    page.route("**/api/token-usage", token_usage_fixture)
    try:
        _login(page, stack)
        _set_scheme(page, scheme)
        card = page.locator(".overview-summary [data-token-dashboard]")
        expect(card.locator(".is-total")).to_contain_text("206.49K")
        expect(card.locator(".is-cost")).to_contain_text("$0.4256")
        expect(card.locator(".token-dashboard-limit")).to_contain_text("131.85K–363.3K")
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        panels=page.locator('.editorial-board > section')
        boxes=[node.bounding_box() for node in panels.all() if node.is_visible()]
        # Side-by-side desktop panels must not overlap; mobile remains vertical.
        for i, a in enumerate(boxes):
            for b in boxes[i+1:]:
                assert (a['x']+a['width'] <= b['x']+1 or b['x']+b['width'] <= a['x']+1
                        or a['y']+a['height'] <= b['y']+1 or b['y']+b['height'] <= a['y']+1)
        operations = page.locator('.workspace-operations').bounding_box()
        projects = page.locator('.workspace-recent-projects').bounding_box()
        if width >= 901:
            assert abs(operations['y']-projects['y']) <= 1
            assert operations['x']+operations['width'] <= projects['x']+1
        else:
            assert projects['y']+projects['height'] <= operations['y']+1
        assert page.locator('.editorial-board > :first-child').evaluate("e=>e.classList.contains('overview-summary')")
        for node in page.locator('.stats,.codepier-focus-card,.workspace-operations').all():
            node.scroll_into_view_if_needed()
            box=node.bounding_box()
            assert box and box['x']>=-1 and box['x']+box['width']<=width+1
            assert box['y']<height and box['y']+box['height']>0
        card.scroll_into_view_if_needed()
        for node in card.locator('.token-dashboard-metric,.token-dashboard-note,.token-dashboard-limit,[data-token-filters] > summary').all():
            if not node.is_visible(): continue
            node.scroll_into_view_if_needed()
            box=node.bounding_box()
            assert box and box['x']>=-1 and box['x']+box['width']<=width+1
            assert box['y']>=-1 and box['y']+box['height']<=height+1
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
        expect(card.locator("[data-token-panel], [data-token-export]")).to_have_count(0)
        expect(card.locator(".token-trend-section")).to_be_visible()
        card.locator("[data-token-filters] > summary").click()
        expect(card.locator("[data-token-filter=period]")).to_be_hidden()
        panels=page.locator('.editorial-board > section')
        boxes=[node.bounding_box() for node in panels.all() if node.is_visible()]
        # Side-by-side desktop panels must not overlap; mobile remains vertical.
        for i, a in enumerate(boxes):
            for b in boxes[i+1:]:
                assert (a['x']+a['width'] <= b['x']+1 or b['x']+b['width'] <= a['x']+1
                        or a['y']+a['height'] <= b['y']+1 or b['y']+b['height'] <= a['y']+1)
        operations = page.locator('.workspace-operations').bounding_box()
        projects = page.locator('.workspace-recent-projects').bounding_box()
        if width >= 901:
            assert abs(operations['y']-projects['y']) <= 1
            assert operations['x']+operations['width'] <= projects['x']+1
        else:
            assert projects['y']+projects['height'] <= operations['y']+1
        assert page.locator('.editorial-board > :first-child').evaluate("e=>e.classList.contains('overview-summary')")
        for node in page.locator('.stats,.codepier-focus-card,.workspace-operations').all():
            node.scroll_into_view_if_needed()
            box=node.bounding_box()
            assert box and box['x']>=-1 and box['x']+box['width']<=width+1
            assert box['y']<height and box['y']+box['height']>0
        card.scroll_into_view_if_needed()
        assert not errors
    finally:
        context.close()
