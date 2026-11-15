"""Real isolated-panel geometry, not merely document overflow.

Workbench/room use dedicated immersive frames and are excluded from normal-page
rails. Card backgrounds may differ: compare inner content rails, not backgrounds.
Screenshots and metrics go to an independent alignment-final run directory.
"""
import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser
from tests.browser_support import login_panel, set_appearance
from tests.ui_alignment_geometry import (
    geometry, close, assert_content_rails, assert_same_row,
    assert_two_by_two, assert_page_heading_rail, write_metrics,
)

SIZES = [(320, 568), (390, 844), (640, 800), (641, 800),
         (768, 375), (900, 900), (901, 900), (1024, 768),
         (1440, 1000), (1920, 1080)]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", SIZES)
def test_real_page_alignment(stack, width, height, scheme):
    evidence = Path(os.getenv("CODEPIER_ALIGNMENT_DIR",
                             ".work/alignment-final-" + uuid.uuid4().hex))
    evidence.mkdir(parents=True, exist_ok=True)
    key = f"chromium-{scheme}-{width}x{height}"
    metrics = {}
    with sync_playwright() as pw:
        browser = launch_browser(pw.chromium)
        context = browser.new_context(viewport={"width": width, "height": height},
                                      reduced_motion="reduce", color_scheme=scheme)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login_panel(page, stack, "projects")
            set_appearance(page, scheme)
            expect(page.locator(".workspace-project-row").first).to_be_visible()
            metrics["project_page"] = assert_page_heading_rail(page)
            card = page.locator(".workspace-project-row").first
            head = geometry(card.locator(".workspace-project-head"))
            path = geometry(card.locator(".workspace-project-path"))
            facts = geometry(card.locator(".workspace-project-facts"))
            foot = geometry(card.locator(".workspace-project-actions"))
            for name, edge in (("path", path["left"]), ("facts", facts["left"]),
                               ("footer", foot["contentLeft"])):
                close(edge, head["contentLeft"], "project inner rail " + name)
            metrics["card"] = [head, path, facts, foot]
            metrics["view_controls"] = assert_same_row(
                page.locator("#project-count"),
                page.locator('[aria-label="项目显示方式"]'))
            group = geometry(page.locator(".project-view-controls"))
            search = geometry(page.locator(".workspace-project-search > label"))
            if width <= 640:
                assert search["bottom"] <= group["top"]+1, (search, group)
            metrics["search"] = [search, group]
            if width <= 900:
                dock = geometry(page.locator(".mobile-dock"))
                expected_inset = max(16, (width-520)/2)
                close(dock["left"], expected_inset, "phone dock left", 1)
                close(width-dock["right"], expected_inset, "phone dock right", 1)
                # Desktop browser emulation reports safe-area env as zero.
                # Actual notch/safe inset needs a physical iOS verification.
                assert dock["bottom"] <= height, dock
                metrics["dock"] = dock
                last_action = page.locator(
                    '.workspace-project-actions [data-action="open-project"]').last
                last_action.evaluate("""n => {
                  for (let p=n.parentElement;p;p=p.parentElement) {
                    if(p.scrollHeight>p.clientHeight) p.scrollTop=p.scrollHeight;
                  }
                  window.scrollTo(0,document.documentElement.scrollHeight);
                }""")
                page.wait_for_function("""() => {
                  const n=[...document.querySelectorAll(
                    '.workspace-project-actions [data-action="open-project"]')].at(-1);
                  const a=n.getBoundingClientRect();
                  const d=document.querySelector('.mobile-dock').getBoundingClientRect();
                  return a.bottom<=d.top+1 && a.top>=0;
                }""", timeout=5000)
                last_box = geometry(last_action)
                dock_box = geometry(page.locator(".mobile-dock"))
                assert last_box["bottom"] <= dock_box["top"]+1, (last_box, dock_box)
                assert last_action.evaluate("""n => {
                  const r=n.getBoundingClientRect();
                  return n.contains(document.elementFromPoint(
                    (r.left+r.right)/2,(r.top+r.bottom)/2));
                }"""), "Last project action center is occluded at page bottom"
                metrics["last_action_dock_clearance"] = [last_box, dock_box]
                page.screenshot(path=str(evidence / (key+"-projects-bottom.png")))
                page.evaluate("""() => {
                  window.scrollTo(0,0);
                  for(const n of document.querySelectorAll('.main,#page')) n.scrollTop=0;
                }""")
            page.screenshot(path=str(evidence / (key+"-projects.png")), full_page=True)

            page.evaluate("navigate('connect')")
            expect(page.locator("#connection-setup")).to_be_visible()
            metrics["connection"] = assert_content_rails(
                page.locator("#connection-setup > .panel-head"),
                page.locator("#connection-setup > .panel-body"))
            page.screenshot(path=str(evidence / (key+"-connection.png")), full_page=True)

            page.evaluate("navigate('settings')")
            expect(page.locator("[data-setting-id]:visible").first).to_be_visible()
            metrics["settings"] = assert_page_heading_rail(page)
            buttons = page.locator(".settings-groups button")
            assert buttons.count() > 0
            for button in buttons.all():
                assert button.is_visible()
                r = geometry(button)
                assert r["left"] >= -1 and r["right"] <= width+1, r
            page.screenshot(path=str(evidence / (key+"-settings.png")), full_page=True)

            page.evaluate("navigate('overview')")
            expect(page.locator(".overview-summary-heading h2")).to_have_text("工作区统计与用量")
            expect(page.locator(".overview-summary .workspace-stats")).to_be_visible()
            assert page.locator(".editorial-board > :first-child").get_attribute("class").find("overview-summary") >= 0
            onboarding = page.locator(".onboarding-compact > summary")
            metrics["statistics"] = geometry(page.locator(".overview-summary"))
            table = page.locator(".workspace-operations .table-scroll")
            expect(table).to_be_visible()
            table_metrics = table.evaluate("""n => {
                const r=n.getBoundingClientRect(), last=n.querySelector('tbody tr:last-child');
                return {bottom:r.bottom, scrollHeight:n.scrollHeight,clientHeight:n.clientHeight,
                    lastBottom:last ? last.getBoundingClientRect().bottom : null};
            }""")
            assert table_metrics["scrollHeight"] <= table_metrics["clientHeight"]+1, table_metrics
            if table_metrics["lastBottom"] is not None:
                assert table_metrics["lastBottom"] <= table_metrics["bottom"]+1, table_metrics
            metrics["overview_last_row"] = table_metrics
            recent_panel = geometry(page.locator(".workspace-operations"))
            statistics = geometry(page.locator(".overview-summary"))
            assert statistics["bottom"] <= recent_panel["top"]+1, (
                "statistics overlap subsequent operations", statistics, recent_panel)
            if onboarding.count():
                onboarding_box = geometry(onboarding)
                assert recent_panel["bottom"] <= onboarding_box["top"]+1, (
                    "recent operations panel overlaps onboarding summary",
                    recent_panel, onboarding_box)
            metrics["overview_sibling_clearance"] = [statistics, recent_panel]
            page.screenshot(path=str(evidence / (key+"-overview.png")), full_page=True)

            if width <= 640:
                page.evaluate("navigate('projects')")
                expect(page.locator('[data-action="open-project"]').first).to_be_visible()
                page.locator('[data-action="open-project"]').first.click()
                expect(page.locator(".work-controls")).to_be_visible()
                entries = page.locator(".work-controls > .actions > *")
                expect(entries).to_have_count(4)
                metrics["work_tools"] = assert_two_by_two(entries)
                page.screenshot(path=str(evidence / (key+"-workbench.png")), full_page=True)
            assert not errors, errors
        finally:
            page.screenshot(path=str(evidence / (key+"-last-state.png")), full_page=True)
            write_metrics(evidence / (key+".json"), metrics)
            context.close()
            browser.close()


def test_settings_200_percent_css_zoom_reachability(stack):
    """CSS zoom stress test, NOT a claim of native browser menu zoom coverage."""
    evidence = Path(os.getenv("CODEPIER_ALIGNMENT_DIR", ".work/alignment-final"))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch_browser(pw.chromium)
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      reduced_motion="reduce")
        page = context.new_page()
        try:
            login_panel(page, stack, "settings")
            page.evaluate("document.documentElement.style.zoom='2'")
            expect(page.locator("[data-setting-id]:visible").first).to_be_visible()
            categories = page.locator(".settings-groups button")
            assert categories.count() > 0
            for index in range(categories.count()):
                button = categories.nth(index)
                button.scroll_into_view_if_needed()
                button.click()
                selected = button.get_attribute("data-settings-group")
                if selected == "files":
                    expect(page.locator("#settings-chain")).to_be_visible()
                    expect(page.locator("#settings-chain")).to_contain_text("文件")
                elif selected == "tasks":
                    expect(page.locator("#settings-scheduler")).to_be_visible()
                else:
                    card = page.locator("[data-setting-id]:not([hidden])").first
                    expect(card).to_be_attached()
                    # Advanced settings are intentionally disclosed, not blank.
                    for disclosure in card.locator("xpath=ancestor::details").all():
                        if disclosure.get_attribute("open") is None:
                            disclosure.locator(":scope > summary").click()
                    expect(card).to_be_visible()
                button.scroll_into_view_if_needed()
                box = geometry(button)
                assert box["left"] >= -1 and box["right"] <= 1441, box
            page.screenshot(path=str(evidence / "settings-css-zoom-200.png"), full_page=True)
        finally:
            context.close()
            browser.close()
