"""Workspace privacy regressions with disposable Hub state and synthetic data."""
from __future__ import annotations

import asyncio
from pathlib import Path

from hub.api.activity import make_activity_router
from hub.api.context import HubContext

import pytest
from fastapi import Request

from tests.test_iam_integration import team as team, shared_role


def event_endpoint(app):
    # Use the public router factory across FastAPI versions; included routers
    # are not guaranteed to expose the private flattened app.routes shape.
    context = HubContext(app.state.store, app.state.runtime, app.state.auth,
        app.state.config, app.state.runtime.panel_maintenance,
        lambda: "http://testserver", Path(__file__).resolve().parents[1])
    return next(route.endpoint for route in make_activity_router(context).routes
                if route.path == "/api/events")


@pytest.mark.parametrize("revocation", ["membership", "session", "role_assignment", "role_scope", "expiry"])
def test_live_panel_stream_signals_authority_loss_without_private_payload(team, revocation):
    app, browsers = team
    role = shared_role(app, browsers, ["read", "execute"])
    browser = browsers["alice"]
    request = Request({
        "type": "http", "method": "GET", "path": "/api/events", "query_string": b"",
        "headers": [(b"cookie", ("rd_session=" + browser.cookie).encode()),
                    (b"x-codepier-space", b"team")],
    })
    async def connected():
        return False
    request.is_disconnected = connected
    endpoint = event_endpoint(app)

    async def run():
        response = await endpoint(request)
        stream = response.body_iterator
        try:
            assert "event: ready" in await anext(stream)
            if revocation == "membership":
                app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            elif revocation == "session":
                app.state.store.execute("DELETE FROM sessions WHERE user_id='alice'")
            elif revocation == "expiry":
                app.state.store.execute("UPDATE sessions SET expires=1 WHERE user_id='alice'")
            elif revocation == "role_assignment":
                app.state.store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
            else:
                import json
                policy = json.loads(app.state.store.one("SELECT policy FROM access_roles WHERE id=?", (role["id"],))["policy"])
                policy["project_rules"][0]["actions"] = ["read"]
                app.state.store.execute("UPDATE access_roles SET policy=? WHERE id=?", (json.dumps(policy), role["id"]))
            for queue in app.state.runtime.watchers:
                queue.put_nowait({"type": "iam", "data": {
                    "space_id": "team", "user_id": "alice", "private": "PRIVATE_SENTINEL"}})
            frame = await anext(stream)
            assert frame == "event: access_revoked\ndata: {}\n\n"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await stream.aclose()
        assert not app.state.runtime.watchers
    asyncio.run(run())


def test_audit_export_query_is_an_authorized_selector_not_authority(team):
    app, browsers = team
    store = app.state.store
    for space in ("team", "legacy"):
        store.execute("INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(1,'panel:owner',?,'','ok','{}',?,'owner')",
                      ("SENTINEL_" + space, space))
    owner = browsers["owner"]
    # Anchor downloads have only the cookie; they cannot send the panel header.
    headers = {"Cookie": "rd_session=" + owner.cookie}
    default = owner.client.get("/api/audit-export", headers=headers)
    assert default.status_code == 200
    assert "SENTINEL_legacy" in default.text and "SENTINEL_team" not in default.text
    chosen = owner.client.get("/api/audit-export?space_id=team", headers=headers)
    assert chosen.status_code == 200
    assert "SENTINEL_team" in chosen.text and "SENTINEL_legacy" not in chosen.text
    denied = browsers["alice"].get("/api/audit-export?space_id=legacy", headers={"X-CodePier-Space": ""})
    assert denied.status_code == 403 and "SENTINEL_" not in denied.text
    assert chosen.headers["cache-control"] == "no-store"


def test_idle_heartbeat_rechecks_membership_and_benign_changes_preserve_stream(team):
    app, browsers = team
    shared_role(app, browsers)
    browser = browsers["alice"]
    request = Request({
        "type": "http", "method": "GET", "path": "/api/events", "query_string": b"",
        "headers": [(b"cookie", ("rd_session=" + browser.cookie).encode()),
                    (b"x-codepier-space", b"team")],
    })
    async def connected():
        return False
    request.is_disconnected = connected
    endpoint = event_endpoint(app)
    async def run():
        response = await endpoint(request)
        stream = response.body_iterator
        try:
            await anext(stream)
            queue = next(iter(app.state.runtime.watchers))
            app.state.store.execute("UPDATE spaces SET label='A harmless rename' WHERE id='team'")
            queue.put_nowait(None)  # The same branch as an idle queue timeout.
            assert await anext(stream) == ": heartbeat\n\n"
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            queue.put_nowait(None)
            assert await anext(stream) == "event: access_revoked\ndata: {}\n\n"
        finally:
            await stream.aclose()
    asyncio.run(run())
