import json

import pytest

from hub.scheduler_settings import public_snapshot, revision
from shared.scheduler_config import effective_scheduler
from tests.test_audit_api import api as api
from tests.support import wait_for


def current(client):
    response = client.get("/api/settings/scheduler", params={"device": "device"})
    assert response.status_code == 200, response.text
    return response.json()


def save(client, config, expected=None):
    return client.put("/api/settings/scheduler", json={"device_id": "device",
        "expected_revision": expected or current(client)["revision"], "config": config})


def test_scheduler_settings_persist_cas_replay_and_reset(api):
    app, client, _ = api
    initial = current(client)
    assert initial["config"] == {} and initial["state"] == "offline"
    config = {"maximum": 12, "initial": 6, "project_limits": {"project": 3}}
    changed = save(client, config)
    assert changed.status_code == 200, changed.text
    assert changed.json()["state"] == "offline"
    assert changed.json()["config"] == config
    assert save(client, config, initial["revision"]).status_code == 200
    assert save(client, {"maximum": 8}, initial["revision"]).status_code == 409
    row = app.state.store.one("SELECT value FROM meta WHERE key='device_scheduler:device'")
    assert json.loads(row["value"]) == config
    reset = save(client, {})
    assert reset.status_code == 200 and reset.json()["config"] == {}


@pytest.mark.parametrize("config", [
    {"maximum": True}, {"maximum": 65}, {"minimum": 8, "maximum": 2},
    {"secret": "hidden"}, {"allowed_roots": ["/"]},
    {"project_limits": {"other-project": 3}}, {"project_limits": {"project": 0}},
])
def test_scheduler_invalid_settings_do_not_mutate_preferences(api, config):
    _, client, _ = api
    before = current(client)
    assert save(client, config).status_code == 422
    assert current(client)["revision"] == before["revision"]


def test_scheduler_csrf_required_and_device_manager_only(api):
    app, client, _ = api
    before = current(client)
    client.headers.pop("X-RD-CSRF")
    assert save(client, {"maximum": 4}).status_code == 403
    client.headers["X-RD-CSRF"] = "csrf"
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    app.state.store.execute("UPDATE memberships SET level='guest' WHERE user_id='owner'")
    denied = client.get("/api/settings/scheduler", params={"device": "device"})
    assert denied.status_code in {403, 404}
    assert save(client, {"maximum": 4}, before["revision"]).status_code in {403, 404}
    assert not app.state.store.one("SELECT value FROM meta WHERE key='device_scheduler:device'")


def test_scheduler_snapshot_projects_only_primitive_known_fields():
    source = {"running": 3, "queued": 7, "reason": "healthy",
              "lanes": {name: {"running": 1, "queued": 2, "capacity": 4, "secret": "hidden"}
                        for name in ("execution", "read", "remote")},
              "command": "private-command", "project": "private-project"}
    result = public_snapshot(source)
    assert result["running"] == 3
    assert "private" not in json.dumps(result) and "hidden" not in json.dumps(result)
    source["lanes"]["read"]["capacity"] = True
    assert public_snapshot(source) is None


def test_panel_preferences_respect_local_node_and_project_ceilings():
    local = {"maximum": 8, "read_limit": 4, "remote_limit": 6,
             "remote_target_limit": 2, "project_limits": {"project": 2}}
    result = effective_scheduler(local, {"maximum": 32, "initial": 20, "read_limit": 16,
        "remote_limit": 24, "remote_target_limit": 8, "project_limits": {"project": 12}})
    assert result["maximum"] == 8 and result["initial"] == 8
    assert result["read_limit"] == 4 and result["remote_limit"] == 6
    assert result["remote_target_limit"] == 2 and result["project_limits"]["project"] == 2


def test_real_agent_reconciles_preferences_and_reports_applied_revision(stack):
    device = stack.project["device_id"]
    get = lambda: stack.must(stack.client.get("/api/settings/scheduler", params={"device": device}))
    first = get()
    config = {"maximum": 4, "initial": 3, "project_limits": {stack.project["id"]: 2}}
    response = stack.client.put("/api/settings/scheduler", json={"device_id": device,
        "expected_revision": first["revision"], "config": config})
    saved = stack.must(response)
    assert saved["revision"] == revision(config)
    def applied():
        result = get()
        return result if result["state"] == "applied" else None
    result = wait_for(applied, timeout=25)
    assert result["reported"]["lanes"]["execution"]["capacity"] <= 4
    assert result["config"] == config


@pytest.mark.parametrize("change", ["delete", "remap"])
def test_stale_project_quotas_are_read_only_and_explicitly_removable(api, change):
    app, client, _ = api
    config = {"maximum": 6, "project_limits": {"project": 3}}
    assert save(client, config).status_code == 200
    assert current(client)["stale_project_limits"] == []
    if change == "delete":
        assert client.delete("/api/projects/project").status_code == 200
    else:
        app.state.store.execute("""INSERT INTO devices(id,name,secret,created,space_id,owner_user_id)
            SELECT 'other-device','private-node-name',secret,created,space_id,owner_user_id
            FROM devices WHERE id='device'""")
        app.state.store.execute("""UPDATE projects SET device_id='other-device',
            alias='private-moved-project',alias_key='private-moved-project' WHERE id='project'""")
    before = app.state.store.one("SELECT total_changes() AS n")["n"]
    stale = current(client)
    assert app.state.store.one("SELECT total_changes() AS n")["n"] == before
    assert stale["config"] == config and stale["revision"] == revision(config)
    assert stale["stale_project_limits"] == ["project"]
    assert "private-node-name" not in json.dumps(stale)
    assert "private-moved-project" not in json.dumps(stale)
    # Neither reads nor unrelated edits silently remove the stale quota.
    assert save(client, {**config, "maximum": 4}, stale["revision"]).status_code == 422
    assert current(client)["config"] == config
    removed = save(client, {"maximum": 4}, stale["revision"])
    assert removed.status_code == 200, removed.text
    assert removed.json()["config"] == {"maximum": 4}
    assert removed.json()["stale_project_limits"] == []


def test_corrupt_stored_preferences_send_rejected_packet_not_empty_reset(api):
    from hub.scheduler_settings import packet
    app, client, _ = api
    app.state.store.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("device_scheduler:device", "{invalid"))
    delivered = packet(app.state.store, "device")
    assert delivered == {"type": "scheduler_config", "revision": "", "error": "SCHEDULER_SETTINGS_INVALID"}
    assert "config" not in delivered
    assert client.get("/api/settings/scheduler", params={"device": "device"}).status_code == 409
