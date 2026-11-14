"""Synthetic provider projection: identity confirmation is never automated."""
from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import sign_in, create_room

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_masked_identity_still_requires_explicit_owner_confirmation(
    collaboration_stack, chat_browser_pool, engine
):
    context = chat_browser_pool(engine).new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    writes = []
    peer = "wxid_synthetic_owner_1234"
    flow = {
        "id": "synthetic-owner-check", "kind": "weixin", "stage": "confirm_owner", "version": 3,
        "binding": {"id": "synthetic-binding", "version": 2, "pending_peer_id": peer},
        "primary_action": "confirm_owner",
        "actions": [{"action": "confirm_owner", "label": "确认本人"}],
        "next_request": {"body": {"action": "confirm_owner", "expected_version": 3},
                         "form_fields": [{"name": "confirm_peer_id", "value": peer}]},
    }

    def projection(route):
        if route.request.method == "POST" and route.request.url.endswith("/actions"):
            writes.append(route.request.post_data_json)
        route.fulfill(status=200, json={"flow": deepcopy(flow)})

    try:
        sign_in(page, collaboration_stack)
        create_room(page, "微信身份确认测试")
        page.route("**/api/collaboration/rooms/*/channels/weixin/connect", projection)
        page.route("**/api/collaboration/channel-connect-flows/**", projection)
        page.locator('.cr-room-actions [data-cr-action="weixin"]').click()
        page.set_viewport_size({"width": 390, "height": 844})
        box = page.locator("[data-cr-channel-view]")
        expect(box).to_contain_text("wxid…1234")
        expect(box).not_to_contain_text(peer)
        expect(box.locator(".cr-steps li")).to_have_count(3)
        expect(box.locator('[aria-current="step"]')).to_contain_text("发送配对码")
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        expect(box.locator('[name="confirm_peer_id"]')).to_have_value(peer)
        assert writes == []
        with page.expect_response(lambda response: response.url.endswith("/actions")):
            box.locator('button[type="submit"]').click()
        assert len(writes) == 1
        assert writes[0]["confirm_peer_id"] == peer
        assert writes[0]["expected_version"] == 3
    finally:
        context.close()
