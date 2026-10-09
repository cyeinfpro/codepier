"""Desktop and mobile settings journeys through the real Hub and original APIs."""
import pytest
from playwright.sync_api import expect

from tests.test_account_connection_journeys import local_user


@pytest.fixture(params=[{"width": 1440, "height": 1000}, {"width": 390, "height": 844}])
def settings_page(request, chat_browser_pool, stack):
    stack.must(stack.client.put("/api/settings/access", json={"all_projects": False, "developer_scopes": False}))
    context = chat_browser_pool("chromium").new_context(viewport=request.param)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page, errors
    context.close()
    assert not errors, errors


def login(page, stack, username="admin"):
    page.goto(stack.url + "/#settings")
    page.fill("#username", username)
    page.fill("#password", stack.password)
    page.click("#login-form button")
    expect(page.locator("#settings-center")).to_be_visible()


def test_search_scopes_cancel_and_save_readback(settings_page, stack):
    page, _ = settings_page
    login(page, stack)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "PUT" else None)
    form = page.locator("#access-settings-form")
    form.locator('[name="all_projects"]').check()
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.evaluate("navigate('identity')")
    expect(page.locator("#settings-center")).to_be_visible()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    form.locator("[data-settings-cancel]").click()
    expect(form.locator('[name="all_projects"]')).not_to_be_checked()
    assert not writes
    form.locator('[name="developer_scopes"]').check()
    form.locator('[type="submit"]').click()
    expect(form.locator('[role="status"]')).to_contain_text("核对")
    assert stack.client.get("/api/settings").json()["access_defaults"] == {
        "all_projects": False, "developer_scopes": True}
    assert page.evaluate("CodePierSettings.dirty()") is False
    page.locator("#settings-search").fill("HUB_PORT")
    expect(page.locator("#setting-hub_process")).to_be_visible()
    expect(page.locator("#setting-access_defaults")).not_to_be_visible()
    page.locator("#settings-search").fill("不存在的设置")
    expect(page.locator("#settings-no-results")).to_be_visible()
    page.locator("#settings-search").fill("")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_conflict_keeps_draft_and_cancel_reads_current_value(settings_page, stack):
    page, _ = settings_page
    login(page, stack)
    form = page.locator("#access-settings-form")
    form.locator('[name="developer_scopes"]').check()
    stack.must(stack.client.put("/api/settings/access", json={"all_projects": True, "developer_scopes": False}))
    form.locator('[type="submit"]').click()
    expect(form.locator('[role="status"]')).to_contain_text("草稿")
    expect(form.locator('[name="developer_scopes"]')).to_be_checked()
    assert stack.client.get("/api/settings").json()["access_defaults"] == {
        "all_projects": True, "developer_scopes": False}
    form.locator("[data-settings-cancel]").click()
    expect(form.locator('[name="all_projects"]')).to_be_checked()
    expect(form.locator('[name="developer_scopes"]')).not_to_be_checked()


def test_non_instance_admin_has_personal_editor_without_deployment_paths(settings_page, stack):
    page, _ = settings_page
    username, _ = local_user(stack)
    login(page, stack, username)
    expect(page.locator("#access-settings-form")).to_be_visible()
    expect(page.locator("#settings-form")).to_have_count(0)
    assert str(stack.hubdir) not in page.content()
    expect(page.locator("#setting-hub_process")).to_contain_text("仅管理员")
    page.locator('#setting-account_security [data-settings-entry="identity"]').click()
    expect(page.locator("#password-form")).to_be_visible()


def test_node_read_is_explicit_and_late_response_cannot_replace_other_page(settings_page, stack):
    page, _ = settings_page
    login(page, stack)
    pending = []
    page.route("**/api/settings/node?*", lambda route: pending.append(route))
    page.locator("#settings-project").select_option(stack.project["id"])
    expect(page.locator("#settings-check-node")).to_be_enabled()
    assert not pending
    page.locator("#settings-check-node").click()
    page.wait_for_timeout(100)
    assert len(pending) == 1
    page.evaluate("navigate('identity')")
    expect(page.locator("#password-form")).to_be_visible()
    try:
        pending[0].fulfill(json={"state": "checked", "project_id": stack.project["id"],
            "values": {"node_ingress": True}, "note": "late-private-node-value"})
    except Exception:
        # The page lifetime is allowed to cancel this read-only request.
        pass
    expect(page.locator("#password-form")).to_be_visible()
    assert "late-private-node-value" not in page.locator("#page").inner_text()


def test_file_import_preview_cancel_and_confirm_are_distinct(settings_page, stack):
    page, _ = settings_page
    before = stack.must(stack.client.get("/api/settings/file-import"))
    login(page, stack)
    form = page.locator("#file-import-settings-form")
    expect(form).to_be_visible()
    proposed = not before["settings"]["streaming_enabled"]["effective_value"]
    form.locator('[name="streaming_enabled"]').select_option(str(proposed).lower())
    form.locator('[type="submit"]').click()
    expect(page.locator("#file-import-confirm")).to_be_visible()
    assert stack.must(stack.client.get("/api/settings/file-import"))["revision"] == before["revision"]
    form.locator("[data-file-import-cancel]").click()
    expect(page.locator("#file-import-confirm")).to_have_count(0)
    assert stack.must(stack.client.get("/api/settings/file-import"))["revision"] == before["revision"]
    form.locator('[name="streaming_enabled"]').select_option(str(proposed).lower())
    form.locator('[type="submit"]').click()
    page.locator("#file-import-confirm").click()
    expect(form.locator('[name="streaming_enabled"]')).to_have_value(str(proposed).lower())
    expect(page.locator("#file-import-confirm")).to_have_count(0)
    after = stack.must(stack.client.get("/api/settings/file-import"))
    assert after["settings"]["streaming_enabled"]["configured_value"] is proposed
    assert after["settings"]["streaming_enabled"]["source"] == "database"
    # Restore the exact previous override in this disposable fixture.
    patch = {"streaming_enabled": before["settings"]["streaming_enabled"]["configured_value"]}
    preview = stack.must(stack.client.post("/api/settings/file-import/preview",
        json={"expected_revision": after["revision"], "patch": patch}))
    stack.must(stack.client.put("/api/settings/file-import", json={"expected_revision": after["revision"],
        "patch": patch, "confirmation": preview["confirmation"]}))
