"""Real owner-only scheduler journeys, repeated actions and receipt recovery."""
import pytest
from playwright.sync_api import expect

from tests.test_settings_center_browser import login


@pytest.fixture(params=[
    ("chromium", {"width": 1280, "height": 800}),
    ("webkit", {"width": 1280, "height": 800}),
    ("chromium", {"width": 390, "height": 844}),
    ("webkit", {"width": 390, "height": 844}),
])
def scheduler_page(request, chat_browser_pool, stack):
    device = stack.project["device_id"]
    endpoint = "/api/settings/scheduler"
    get = lambda: stack.must(stack.client.get(endpoint, params={"device": device}))
    before = get()
    stack.must(stack.client.put(endpoint, json={"device_id": device,
        "expected_revision": before["revision"], "config": {}}))
    engine, viewport = request.param
    context = chat_browser_pool(engine).new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    login(page, stack)
    page.locator("#settings-search").fill("并发")
    expect(page.locator("#settings-scheduler")).to_be_visible()
    expect(page.locator("#settings-no-results")).to_be_hidden()
    yield page, get
    context.close()
    current = get()
    stack.must(stack.client.put(endpoint, json={"device_id": device,
        "expected_revision": current["revision"], "config": before["config"]}))
    assert not errors, errors


def test_scheduler_save_once_readback_and_per_project_quota(scheduler_page, stack):
    page, get = scheduler_page
    form = page.locator("#scheduler-settings-form")
    form.locator('[name="adaptive"]').select_option("false")
    form.locator('[name="maximum"]').fill("4")
    form.locator("summary").click()
    form.locator('[name="initial"]').fill("3")
    form.locator('[data-scheduler-project="' + stack.project["id"] + '"]').fill("2")
    assert page.evaluate("CodePierSettings.dirty()")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.evaluate("navigate('identity')")
    expect(form).to_be_visible()
    writes = []
    page.on("request", lambda request: writes.append(request) if request.method == "PUT"
            and request.url.endswith("/api/settings/scheduler") else None)
    form.evaluate("(form) => {form.requestSubmit(); form.requestSubmit();}")
    expect(page.locator("[data-scheduler-state]")).to_have_text("已生效", timeout=35000)
    assert len(writes) == 1
    assert get()["config"] == {"adaptive": False, "maximum": 4, "initial": 3,
                              "project_limits": {stack.project["id"]: 2}}
    assert page.evaluate("CodePierSettings.dirty()") is False
    expect(page.locator("[data-scheduler-metrics]")).to_contain_text("运行")
    expect(page.locator("#settings-draft-note")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.evaluate("navigate('identity')")
    expect(page.locator("#password-form")).to_be_visible()


def test_scheduler_conflict_preserves_draft_then_cancel_reads_current(scheduler_page, stack):
    page, get = scheduler_page
    form = page.locator("#scheduler-settings-form")
    form.locator('[name="maximum"]').fill("6")
    baseline = get()
    stack.must(stack.client.put("/api/settings/scheduler", json={
        "device_id": stack.project["device_id"], "expected_revision": baseline["revision"],
        "config": {"maximum": 4}}))
    form.locator('[type="submit"]').click()
    expect(page.locator("[data-scheduler-feedback]")).to_contain_text("草稿")
    expect(form.locator('[name="maximum"]')).to_have_value("6")
    assert get()["config"] == {"maximum": 4}
    form.locator("[data-scheduler-reset]").click()
    expect(form.locator('[name="maximum"]')).to_have_value("4")
    assert page.evaluate("CodePierSettings.dirty()") is False


def test_scheduler_lost_save_receipt_recovers_without_second_write(scheduler_page):
    page, get = scheduler_page
    form = page.locator("#scheduler-settings-form")
    form.locator('[name="maximum"]').fill("4")
    writes = []
    def lose_receipt(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        writes.append(route.request.post_data)
        result = route.fetch()
        assert result.status == 200
        route.abort("failed")
    page.route("**/api/settings/scheduler", lose_receipt)
    form.locator('[type="submit"]').click()
    expect(page.locator("[data-scheduler-feedback]")).to_contain_text("刷新", timeout=20000)
    assert get()["config"] == {"maximum": 4}
    form.locator("[data-scheduler-refresh]").click()
    expect(page.locator("[data-scheduler-state]")).to_have_text("已生效", timeout=35000)
    assert page.evaluate("CodePierSettings.dirty()") is False
    assert len(writes) == 1


def test_deleted_project_quota_can_be_explicitly_removed_without_its_name(scheduler_page, stack):
    page, get = scheduler_page
    project = stack.must(stack.client.post("/api/projects", json={
        "alias": "stale-quota-fixture", "device_id": stack.project["device_id"],
        "root": str(stack.projectalpha), "mode": "read", "allow_tasks": False}))
    project_id = project["id"]
    before = get()
    config = {"maximum": 6, "project_limits": {project_id: 2}}
    stack.must(stack.client.put("/api/settings/scheduler", json={
        "device_id": stack.project["device_id"], "expected_revision": before["revision"],
        "config": config}))
    # Observe this project in a fresh authenticated event stream before removal.
    # Shrinking access deliberately clears cached private UI; reauthenticate
    # rather than weakening that boundary to keep the settings form mounted.
    page.reload()
    expect(page.locator("#settings-center")).to_be_visible()
    page.wait_for_function("S.events?.readyState === EventSource.OPEN")
    stack.must(stack.client.delete("/api/projects/" + project_id))
    expect(page.locator("#login-form")).to_be_visible()
    page.fill("#username", "admin")
    page.fill("#password", stack.password)
    page.click("#login-form button")
    expect(page.locator("#settings-center")).to_be_visible()
    page.locator("#settings-search").fill("并发")
    form = page.locator("#scheduler-settings-form")
    expect(form).to_have_count(1)
    form.locator("summary").click()
    stale = form.locator("[data-scheduler-stale-project]")
    expect(stale).to_be_visible()
    expect(stale).not_to_contain_text("stale-quota-fixture")
    assert get()["config"] == config
    form.locator("[data-scheduler-remove-project]").click()
    assert page.evaluate("CodePierSettings.dirty()")
    assert get()["config"] == config
    form.locator("[data-scheduler-reset]").click()
    expect(stale.locator("input")).to_have_value("2")
    # Background replacement preserves the expanded section.
    expect(form.locator("details")).to_have_attribute("open", "")
    expect(stale).to_be_visible()
    form.locator("[data-scheduler-remove-project]").click()
    form.locator('[name="maximum"]').fill("4")
    form.locator('[type="submit"]').click()
    expect(page.locator("[data-scheduler-state]")).to_have_text("已生效", timeout=35000)
    assert get()["config"] == {"maximum": 4}
    assert get()["stale_project_limits"] == []
    assert page.evaluate("CodePierSettings.dirty()") is False
    expect(page.locator("#settings-draft-note")).to_be_hidden()


def test_scheduler_late_refresh_does_not_replace_another_page(scheduler_page):
    page, _ = scheduler_page
    pending = []
    page.route("**/api/settings/scheduler?*", lambda route: pending.append(route))
    page.locator("[data-scheduler-refresh]").click()
    page.wait_for_timeout(100)
    assert pending
    page.evaluate("navigate('identity')")
    expect(page.locator("#password-form")).to_be_visible()
    for route in pending:
        try:
            route.fulfill(json={"state": "offline", "revision": "late", "config": {}, "reported": None})
        except Exception:
            pass
    expect(page.locator("#password-form")).to_be_visible()
    expect(page.locator("#settings-scheduler")).to_have_count(0)
