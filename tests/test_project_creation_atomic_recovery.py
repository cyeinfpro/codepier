"""Project creation must commit only authorized mappings and correctly scope device audit."""
import json

import pytest

from tests.test_access_profiles import call
from tests.test_iam_integration import assign, team  # noqa: F401
from tests.test_roles import credential, must, profile, role


def mapping(**changes):
    return {
        "alias": "MemberCreated",
        "device_id": "device-team",
        "root": "/tmp/fixture-member-created",
        "mode": "read",
        "allow_tasks": False,
        "idempotency_key": "fixture-member-create",
        **changes,
    }


def test_unreadable_panel_mapping_rolls_back_and_same_receipt_can_recover(team, monkeypatch):
    app, browsers = team
    store = app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    body = mapping()

    async def validate(*_args, **_kwargs):
        return {"root": body["root"], "writable": True, "allow_tasks": False}

    monkeypatch.setattr(app.state.runtime, "dispatch", validate)
    target = None
    for _ in range(2):
        response = browsers["alice"].post("/api/projects", json=body)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
        assert not store.one("SELECT id FROM projects WHERE alias=?", (body["alias"],))
        plan = json.loads(store.one("SELECT value FROM meta WHERE key LIKE 'project_save:team:%'")["value"])
        assert not plan.get("committed")
        target = target or plan["target"]
        assert plan["target"] == target
        assert not store.one("SELECT id FROM audit WHERE action='project.created' AND target=?", (body["alias"],))

    # An explicit pre-existing-role authorization change can unblock this same
    # validation receipt; the create endpoint does not manufacture permissions.
    authorized = role(browsers["owner"], project_rules=[{"actions": ["read"], "all_projects": True}])
    assign(browsers["owner"], authorized, "alice")
    result = must(browsers["alice"].post("/api/projects", json=body))
    assert result["id"] == target
    assert must(browsers["alice"].post("/api/projects", json=body))["id"] == target
    assert store.one("SELECT count(*) AS n FROM projects WHERE alias=?", (body["alias"],))["n"] == 1


@pytest.mark.parametrize("caller", ["panel", "role"])
@pytest.mark.parametrize("conflict", ["canonical", "concurrent"])
def test_nonadmin_creation_rechecks_canonical_and_concurrent_overlap(team, monkeypatch, caller, conflict):
    app, browsers = team
    store = app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    store.execute("UPDATE projects SET mode='read',allow_tasks=0 WHERE id='project-team'")
    policy = role(
        browsers["owner"],
        project_rules=[{"actions": ["read", "write"], "all_projects": True}],
        device_rules=[{
            "actions": ["devices.read", "projects.create"],
            "devices": ["device-team"],
            "max_project_mode": "write",
        }],
    )
    assign(browsers["owner"], policy, "alice")
    connection = None
    if caller == "role":
        identity = profile(browsers["alice"], policy)
        connection = must(credential(browsers["alice"], policy, identity))["token"]
    body = mapping(alias="NoOverlap", mode="write")
    existing = store.one("SELECT root FROM projects WHERE id='project-team'")["root"]

    async def validate(*_args, **_kwargs):
        root = existing
        if conflict == "concurrent":
            root = body["root"]
            store.execute(
                "INSERT INTO projects(id,alias,alias_key,device_id,root,mode,space_id,owner_user_id,created) "
                "VALUES('concurrent','Concurrent','concurrent','device-team',?,'read','team','owner',1)",
                (root,),
            )
        return {"root": root, "writable": True, "allow_tasks": False}

    monkeypatch.setattr(app.state.runtime, "dispatch", validate)
    if caller == "panel":
        response = browsers["alice"].post("/api/projects", json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PROJECT_ROOT_OVERLAP"
    else:
        result = call(browsers["alice"], connection, "projects_create", body).json()["result"]
        assert result["isError"]
        error = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert error["error"]["code"] == "PROJECT_ROOT_OVERLAP"
    assert not store.one("SELECT id FROM projects WHERE alias=?", (body["alias"],))
    assert not store.one("SELECT id FROM audit WHERE action='project.created' AND target=?", (body["alias"],))


def test_connected_audit_uses_device_space_not_legacy(team, monkeypatch):
    app, _ = team
    runtime, store = app.state.runtime, app.state.store
    monkeypatch.setattr(runtime, "_touch_agent", lambda *_args: True)
    monkeypatch.setattr(runtime, "publish", lambda *_args: None)
    monkeypatch.setattr(runtime, "wake_delivery", lambda: None)
    for space in ("team", "legacy"):
        device = store.one("SELECT * FROM devices WHERE id=?", ("device-" + space,))
        assert runtime._register_agent(device["id"], object(), {}, device)
    rows = store.all("SELECT target,space_id FROM audit WHERE action='device.connected' ORDER BY id")
    assert rows == [
        {"target": "device-team", "space_id": "team"},
        {"target": "device-legacy", "space_id": "legacy"},
    ]
