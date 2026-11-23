from uuid import uuid4

from playwright.sync_api import expect
from tests.test_vps_ui import browser, open_page, ui_stack  # noqa: F401


def test_saved_vps_no_fingerprint_controls_and_wrong_versions_fail_closed(browser, ui_stack):
    engine, name = browser
    page = open_page(engine, ui_stack, 390 if name == "webkit" else 1440)
    calls = []
    terminal_calls = []
    fixture_name = "no-mode-fixture-" + uuid4().hex[:8]
    page.on("request", lambda req: terminal_calls.append(req.url)
            if req.method == "POST" and req.url.endswith("/api/vps-terminal/sessions") else None)
    created = {}
    mode = "current"
    def vps_api(route):
        nonlocal created
        method, url = route.request.method, route.request.url
        if url.endswith("/api/vps") and method == "POST":
            calls.append(("create", set(route.request.post_data_json)))
            response = route.fetch()
            assert response.ok, response.status
            created = response.json()
            route.fulfill(response=response, json={
                **created,
                "saved_vps_host_verification": "skip",
                "terminal": {"available": True, "eligible": True, "check_ready": True, "direct": True},
            })
        elif method == "GET" and "/api/vps?" in url:
            response = route.fetch()
            inventory = response.json()
            for row in inventory["vps"]:
                if row["id"] == created.get("id"):
                    row["terminal"] = {"available": True, "eligible": True, "check_ready": True, "direct": True}
            route.fulfill(response=response, json=inventory)
        elif created and url.endswith("/api/vps/"+created["id"]+"/check") and method == "POST":
            calls.append(("check", set(route.request.post_data_json)))
            route.fulfill(json={"state": "failed", "error": {"code": "SSH_HOST_KEY_UNTRUSTED"},
                                "output": "obsolete fingerprint verification output"})
        elif created and url.endswith("/api/vps/"+created["id"]) and method == "GET":
            response = route.fetch()
            doc = response.json()
            doc.pop("saved_vps_host_verification", None)
            if mode == "current":
                doc["saved_vps_host_verification"] = "skip"
            route.fulfill(response=response, json=doc)
        else:
            route.continue_()

    page.route("**/api/vps**", vps_api)
    try:
        page.locator('.page-head [data-vps-action="add"]').click()
        form = page.locator("#vps-form")
        expect(form.locator('[name="host_key_policy"], [name="host_key_fingerprint"]')).to_have_count(0)
        form.locator('[name="name"]').fill(fixture_name)
        form.locator('[name="host"]').fill(fixture_name + ".invalid")
        form.locator('[name="password"]').fill("isolated-test-credential")
        page.locator("#vps-save").click()
        status = page.locator("#vps-check-state")
        expect(status).to_contain_text("请更新后重试")
        expect(status).not_to_contain_text("成功")
        expect(page.locator("#vps-check-output")).not_to_contain_text("fingerprint")
        assert calls[0][0] == "create"
        assert calls[1] == ("check", {"timeout_seconds", "idempotency_key"})
        assert "host_key_policy" not in calls[0][1] and "host_key_fingerprint" not in calls[0][1]
        page.locator('.modal-footer [data-action="close-modal"]').click()
        page.locator("#vps-query").fill(fixture_name)
        card = page.locator('[data-vps-card]:visible')
        expect(card).to_have_count(1)
        card.locator(".vps-card-more summary").click()
        card.locator('[data-vps-action="edit"]').click()
        expect(form.locator('[name="host_key_policy"], [name="host_key_fingerprint"]')).to_have_count(0)
        page.locator('.modal-footer [data-action="close-modal"]').click()
        mode = "older"
        more = card.locator(".vps-card-more")
        if more.get_attribute("open") is None:
            more.locator("summary").click()
        card.locator('[data-vps-action="check"]').click()
        expect(status).to_contain_text("请更新后重试")
        expect(page.locator("#vps-check-run")).to_be_disabled()
        assert [call[0] for call in calls] == ["create", "check"]
        page.locator('.modal-footer [data-action="close-modal"]').click()
        card.locator('[data-vps-action="terminal"]').click()
        expect(card.locator('[data-vps-terminal-inline]')).to_contain_text("请更新后重试")
        assert not terminal_calls
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        page.close()
