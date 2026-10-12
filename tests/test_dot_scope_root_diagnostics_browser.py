"""Synthetic mapping diagnostics in real browsers, without live accounts."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_dot_channel_navigation_browser import act, overview, no_overflow
from tests.weixin_browser_support import full_styles, qr_page as qr_page


def open_scope(page):
    act(page, "open")
    overview(page)
    act(page, "view-advanced")
    return page.locator("[data-channel-form=scope]")


@pytest.mark.parametrize("width", [360, 390, 1440])
def test_root_conflict_explains_selected_project_before_save(qr_page, width, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 900})
    full_styles(page)
    page.evaluate("""() => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接'}];
      data.scopes.project_root_checks=[{project_id:'projectA',
        overlap_with_project_ids:[],has_outside_projects:true}];
    }""")
    form = open_scope(page)
    expect(form.locator(".cc-scope-root-check")).to_contain_text("合成项目")
    expect(form.locator(".cc-scope-root-check")).to_contain_text("项目管理")
    expect(form.locator(".cc-scope-root-check")).to_contain_text("独立目录")
    expect(form.locator("[data-cc-action=dot-channel-scope-save]")).to_be_disabled()
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    form.locator("[name=confirm_scope]").check()
    error = page.evaluate("clickAction('dot-channel-scope-save').then(()=>'',e=>e.message)")
    assert "当前范围无法保存" in error
    assert page.evaluate("writes.length") == 0
    assert page.evaluate("reads.find(url=>url.includes('room-scopes'))").endswith("conversation_id=conversationA")
    no_overflow(page)
    evidence = Path(os.environ.get("CODEPIER_TEST_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f"scope-overlap-{engine}-{width}.png"), full_page=True)
    # A refreshed mapping result is needed; navigation alone never edits maps.
    page.evaluate("data.scopes.project_root_checks[0].has_outside_projects=false")
    act(page, "open")
    expect(form.locator(".cc-scope-root-check")).to_be_hidden()
    expect(form.locator("[data-cc-action=dot-channel-scope-save]")).to_be_enabled()
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    assert page.evaluate("writes.length") == 0


def test_explicit_project_selection_rechecks_without_adding_scope(qr_page):
    page, _ = qr_page
    page.evaluate("""() => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接'}];
      state.snapshot.conversation.projects.push(
        {project_id:'projectB',project_label:'合成子项目',environment_id:'test'});
      data.scopes.project_root_checks=[
        {project_id:'projectA',overlap_with_project_ids:['projectB'],has_outside_projects:false},
        {project_id:'projectB',overlap_with_project_ids:[],has_outside_projects:false}];
    }""")
    form = open_scope(page)
    child = form.locator("[data-project-id=projectB] [name=project_selected]")
    expect(child).not_to_be_checked()
    expect(form.locator("[data-cc-action=dot-channel-scope-save]")).to_be_disabled()
    child.check()
    expect(form.locator("[data-cc-action=dot-channel-scope-save]")).to_be_enabled()
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    assert page.evaluate("writes.length") == 0
    child.uncheck()
    expect(form.locator("[data-cc-action=dot-channel-scope-save]")).to_be_disabled()
    assert page.evaluate("writes.length") == 0
