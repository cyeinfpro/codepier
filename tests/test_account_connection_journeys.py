"""Account/connection journeys against disposable real Hub/Agent fixtures."""
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub import iam
from hub.store import Store


@pytest.fixture(params=["chromium", "webkit"])
def journey_browser(request, chat_browser_pool):
    return chat_browser_pool(request.param)


@pytest.fixture(params=[{"width": 1440, "height": 1000}, {"width": 390, "height": 844}])
def journey_page(request, journey_browser, stack):
    context = journey_browser.new_context(viewport=request.param)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page, errors
    context.close()
    assert not errors, errors


def local_user(stack):
    identifier = "journey-" + uuid.uuid4().hex[:12]
    store = Store(stack.hubdir)
    try:
        with store.transaction():
            hashed = store.one("SELECT password_hash FROM users WHERE username='admin'")["password_hash"]
            store.db.execute("INSERT INTO users VALUES(?,?,?,?)", (identifier, identifier, hashed, time.time()))
            store.db.execute("DELETE FROM memberships WHERE user_id=?", (identifier,))
            sid = iam.create_personal_space(store, identifier, "Private " + identifier)
        return identifier, sid
    finally:
        store.close()


def login(page, stack, username="admin", route="identity"):
    page.goto(stack.url + "/#" + route)
    page.fill("#username", username)
    page.fill("#password", stack.password)
    page.click("#login-form button")
    expect(page.locator("#page h1")).to_be_visible()


def test_personal_default_self_service_and_team_continuation(journey_page, stack):
    page, _ = journey_page
    username, sid = local_user(stack)
    login(page, stack, username)
    expect(page.locator("#page h1")).to_have_text("我的账号")
    assert page.evaluate("S.space_id") == sid
    assert not page.evaluate("S.identity.instance_admin")
    expect(page.locator("#iam-active-space")).to_have_count(0)
    expect(page.locator("#password-form")).to_be_visible()
    page.locator('#password-form [name="current_password"]').fill("fixture-only")
    page.locator('#password-form [name="new_password"]').fill("fixture-new-password")
    page.locator('#password-form [name="confirm_password"]').fill("fixture-other-password")
    page.locator("#password-form button").click()
    expect(page.locator("#password-match-error")).to_contain_text("不一致")
    for field in page.locator("#password-form input").all():
        field.fill("")
    page.evaluate("navigate('members')")
    expect(page.locator("#page")).to_contain_text("个人空间只属于你")
    expect(page.locator('[data-iam="invite"]')).to_have_count(0)
    page.locator('[data-iam="new-team-invite"]').click()
    page.locator('#iam-form [name="label"]').fill("Explicit team " + username)
    page.locator('button[form="iam-form"]').click()
    expect(page.locator("#page h1")).to_have_text("空间成员")
    expect(page.locator('[data-iam="invite"]')).to_be_visible()
    assert page.evaluate("S.space_id") != sid
    page.locator('[data-iam="invite"]').click()
    expect(page.locator('#iam-form [name="login_verified"]')).not_to_be_checked()
    page.locator('button[form="iam-form"]').click()
    expect(page.locator("#iam-form")).to_be_visible()
    expect(page.locator(".modal .secret")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(0)
    page.reload()
    expect(page.locator("#page h1")).to_have_text("空间成员")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_invite_accept_lands_in_team_and_role_assignment_is_explicit(journey_page, stack):
    page, _ = journey_page
    username, personal = local_user(stack)
    space = stack.must(stack.client.post("/api/iam/spaces", json={"label": "Invitation journey", "idempotency_key": uuid.uuid4().hex}))
    headers = {"X-CodePier-Space": space["id"]}
    invite = stack.must(stack.client.post("/api/iam/spaces/" + space["id"] + "/invites", headers=headers, json={"level": "member", "days": 1}))
    login(page, stack, username)
    page.locator('[data-iam="accept-invite"]').click()
    page.locator('#iam-form [name="invitation"]').fill(invite["invitation"])
    page.locator('button[form="iam-form"]').click()
    expect(page.locator("#page h1")).to_have_text("项目映射")
    assert page.evaluate("S.space_id") == space["id"]
    assert page.evaluate("S.projects.length") == 0
    assert page.evaluate("S.space_id") != personal
    page.reload()
    expect(page.locator("#page h1")).to_have_text("项目映射")
    assert page.evaluate("S.space_id") == space["id"]
    owner = page.context.browser.new_page(viewport=page.viewport_size)
    try:
        login(owner, stack)
        owner.evaluate("(sid) => CodePierIdentity.switchSpace(sid)", space["id"])
        owner.evaluate("navigate('members')")
        person = owner.locator('[data-member-person="' + username + '"]')
        expect(person).to_have_count(1)
        expect(person).to_contain_text("待分配项目权限")
        expect(owner.locator("#page")).to_contain_text("已接受 · 待分配项目权限")
        role = stack.must(stack.client.post("/api/access-roles", headers=headers, json={"label": "Explicit read role", "enabled": True, "project_rules": [], "device_rules": [], "idempotency_key": uuid.uuid4().hex}))
        stack.must(stack.client.put("/api/iam/spaces/" + space["id"] + "/members/" + username, headers=headers, json={"level": "member", "active": True, "expected_version": 0}))
        owner.evaluate("renderPage(false)")
        expect(person).to_have_count(1)
        expect(person).to_contain_text("成员来源 · 2")
        person.locator("[data-iam-assignment]").click()
        form = owner.locator("#iam-form")
        expect(form.locator('[name="role_id"]')).to_have_value("")
        expect(form.locator('[name="may_delegate"]')).not_to_be_checked()
        form.locator('[name="role_id"]').select_option(role["id"])
        expect(form.locator("[data-assignment-policy]")).to_contain_text("Explicit read role")
        owner.once("dialog", lambda dialog: dialog.dismiss())
        owner.locator('button[form="iam-form"]').click()
        assert stack.must(stack.client.get("/api/iam/spaces/" + space["id"] + "/assignments", headers=headers))["assignments"] == []
        owner.once("dialog", lambda dialog: dialog.accept())
        owner.locator('button[form="iam-form"]').click()
        expect(form).to_have_count(0)
        assigned = stack.must(stack.client.get("/api/iam/spaces/" + space["id"] + "/assignments", headers=headers))["assignments"]
        assert len(assigned) == 1 and not assigned[0]["may_delegate"]
        assert owner.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        owner.close()


def test_connection_preview_defaults_and_stale_response_do_not_leak(journey_page, stack):
    page, _ = journey_page
    label = "Bound " + uuid.uuid4().hex[:8]
    profile = stack.must(stack.client.post("/api/access-profiles", json={"label": label, "scopes": ["read", "write", "execute", "computer"], "all_projects": True, "projects": [], "idempotency_key": uuid.uuid4().hex}))
    grant = stack.must(stack.client.post("/api/grants", json={"label": label, "scopes": ["read"], "projects": [stack.project["id"]], "profile_id": profile["id"], "profile_version": profile["version"]}))
    login(page, stack, route="connect")
    expect(page.locator("#page")).to_contain_text("助手访问 CodePier")
    expect(page.locator("#page")).to_contain_text("CodePier 使用外部工具")
    page.locator('[data-action="new-grant"]').click()
    page.locator("#access-profile-selector").select_option(profile["id"])
    expect(page.locator('#grant-form [name="all_projects"]')).not_to_be_checked()
    expect(page.locator('#grant-form [name="scope"][value="computer"]')).not_to_be_checked()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('.modal [data-action="close-modal"]').first.click()
    expect(page.locator(".modal")).to_have_count(0)
    page.evaluate("(id) => CodePierProfiles.preview(id)", grant["grant_id"])
    expect(page.locator("[data-access-preview]")).to_contain_text("fixedProfile")
    expect(page.locator("[data-access-preview]")).to_contain_text("原同意 ∩ 当前上限")
    expect(page.locator("[data-access-preview]")).to_contain_text("拒绝")
    expect(page.locator("[data-access-preview]")).to_contain_text("没有执行任何工具")
    assert grant["token"] not in page.content()
    page.locator("[data-refresh-access]").click()
    expect(page.locator("[data-access-preview]")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    shots = Path(".work/account-connection-ui")
    shots.mkdir(parents=True, exist_ok=True)
    engine = page.context.browser.browser_type.name
    page.screenshot(path=str(shots / f"{engine}-{page.viewport_size['width']}-access-preview.png"), animations="disabled")
    page.go_back()
    expect(page.locator(".modal")).to_have_count(0)
    pending = []
    endpoint = "**/api/grants/" + grant["grant_id"] + "/access-preview"
    page.route(endpoint, lambda route: pending.append(route))
    page.evaluate("(id) => { void CodePierProfiles.preview(id); }", grant["grant_id"])
    expect(page.locator('.modal [role="status"]')).to_contain_text("正在读取")
    page.wait_for_timeout(100)
    assert pending
    space = stack.must(stack.client.post("/api/iam/spaces", json={"label": "Preview isolation", "idempotency_key": uuid.uuid4().hex}))
    page.evaluate("(sid) => CodePierIdentity.switchSpace(sid)", space["id"])
    response = stack.must(stack.client.get("/api/grants/" + grant["grant_id"] + "/access-preview"))
    pending[0].fulfill(json=response)
    page.wait_for_timeout(150)
    expect(page.locator(".modal")).to_have_count(0)
    assert label not in page.locator("#page").inner_text()
    assert page.evaluate("S.space_id") == space["id"]
