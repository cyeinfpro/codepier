"""Regression coverage for the condensed panel journeys."""

import pytest
from playwright.sync_api import expect

from tests.browser_support import chat_page as chat_page, login_panel
from tests.collaboration_support import collaboration_stack as collaboration_stack


@pytest.mark.parametrize("chat_page", ["chromium", "webkit"], indirect=True)
def test_cli_choice_and_stop_then_delete(chat_page):
    page = chat_page
    page.locator("#chat-new").click()
    picker = page.locator("#chat-project-dialog")
    picker.locator('[name="cli"][value="codex"]').check()
    picker.locator('[data-project-id="p2"]').click()
    expect(page.locator("#chat-config-summary")).to_contain_text("Workspace two · Codex")
    assert not page.evaluate("requests.some(r => r.path.endsWith('/start'))")
    page.evaluate("""() => chatSwitch({
        id: 'delete-fixture', project_id: 'p2', status: 'running', title: 'Delete fixture',
        provider: 'codex', root: '/workspace/two', cwd: '/workspace/two'
    }, 'p2')""")
    page.locator(".chat-overflow > summary").click()
    page.locator("#chat-delete").click()
    page.get_by_role("button", name="停止并删除", exact=True).click()
    expect(page.locator("#chat-status")).to_contain_text("正在停止")
    assert not page.evaluate("requests.some(r => r.path.endsWith('/delete'))")
    page.evaluate("""() => streams.at(-1).emit('session', {status: 'exited'}, 1)""")
    page.wait_for_function("requests.some(r => r.path.endsWith('/delete'))")
    actions = page.evaluate("requests.filter(r => /\\/(stop|delete)$/.test(r.path))")
    assert len(actions) == 2
    assert actions[0]["path"].endswith("/stop") and actions[1]["path"].endswith("/delete")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_room_creation_chooses_projects_and_allows_immediate_invite(chat_browser_pool, collaboration_stack, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        login_panel(page, stack, "collaboration")
        page.get_by_role("button", name="新建房间").first.click()
        form = page.locator('[data-cr-form="create"]')
        form.locator('[name="title"]').fill("UX onboarding fixture")
        for project in stack.projects[:2]:
            form.locator(f'[name="project"][value="{project["id"]}"]').check()
        with page.expect_response(lambda r: r.request.method == "POST" and
                                  r.request.url.endswith("/api/collaboration/rooms")) as created:
            form.locator('[type="submit"]').click()
        room = created.value.json()["room"]["id"]
        expect(page.locator("[data-cr-title]")).to_have_text("UX onboarding fixture")
        expect(page.locator("[data-cr-project-count]")).to_have_text("项目 2")
        policy = stack.must(stack.client.get(f"/api/collaboration/rooms/{room}/permissions"))
        assert policy["configured"]
        assert {rule["project_id"] for rule in policy["scope"]["project_scopes"]} == {
            project["id"] for project in stack.projects[:2]
        }
        assert all(rule["capabilities"] == ["read"] for rule in policy["scope"]["project_scopes"])
        page.locator('.cr-room-actions [data-cr-action="members"]').click()
        page.locator('dialog [data-cr-action="add-member"]').click()
        invite = page.locator('[data-cr-form="member"]')
        expect(invite).to_be_visible()
        invite.locator('[name="name"]').fill("Review dot")
        invite.locator('[type="submit"]').click()
        expect(page.locator("#cr-panel-title")).to_contain_text("Review dot")
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_workflow_name_defaults_and_pending_assignees_do_not_block(chat_browser_pool, stack, engine):
    context = chat_browser_pool(engine).new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    held = []
    try:
        page.route("**/workflow-assignees", lambda route: held.append(route))
        login_panel(page, stack, "workflows")
        page.locator('[data-wf-action="create"]').click()
        page.locator("#wf-goal").fill("Repair the upload queue\nVerify retry.")
        with page.expect_response(lambda r: r.request.method == "POST" and
                                  r.request.url.endswith("/api/tools/call") and
                                  r.request.post_data_json.get("tool") == "workflows_create") as response:
            page.locator("#workflow-create-save").click()
        assert response.value.ok
        body = response.value.request.post_data_json["arguments"]
        assert body["title"] == "Repair the upload queue"
        assert "assignee_grant_id" not in body
        expect(page.locator("#workflow-edit")).to_be_visible()
        page.locator("#workflow-edit").click()
        expect(page.locator("#wf-step-state")).to_have_value("completed")
        expect(page.locator("#wf-evidence-list")).to_contain_text("还没有成功的操作")
    finally:
        for route in held:
            route.abort()
        context.close()


@pytest.mark.parametrize("chat_page", ["chromium", "webkit"], indirect=True)
def test_orphaned_cli_session_times_out_without_deleting(chat_page):
    page = chat_page
    page.clock.install()
    page.evaluate("""() => chatSwitch({
        id: 'orphan-fixture', project_id: 'p2', status: 'orphaned', title: 'Orphan fixture',
        provider: 'codex', root: '/workspace/two', cwd: '/workspace/two'
    }, 'p2')""")
    page.locator(".chat-overflow > summary").click()
    page.locator("#chat-delete").click()
    page.get_by_role("button", name="停止并删除", exact=True).click()
    expect(page.locator("#chat-status")).to_contain_text("正在停止")
    page.clock.fast_forward(31000)
    expect(page.locator("#chat-status")).to_contain_text("稍后再试")
    assert not page.evaluate("requests.some(r => r.path.endsWith('/delete'))")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_empty_overview_offers_setup_instead_of_empty_metrics(chat_browser_pool, stack, engine):
    context = chat_browser_pool(engine).new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()

    def empty_overview(route):
        response = route.fetch()
        body = response.json()
        body.update(devices=[], projects=[], online_devices=0, active_grants=0)
        route.fulfill(response=response, json=body)

    try:
        page.route("**/api/overview?*", empty_overview)
        login_panel(page, stack)
        checklist = page.locator(".onboarding")
        expect(checklist.locator('[aria-current="step"]')).to_contain_text("接入电脑")
        expect(checklist.locator("li")).to_have_count(3)
        expect(page.locator(".workspace-stats")).to_have_count(0)
        checklist.get_by_role("button", name="接入电脑").click()
        expect(page.locator("#device-form")).to_be_visible()
    finally:
        context.close()
