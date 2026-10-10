"""Current-policy workflow assignees and final creation share one authorization gate."""
import json
import time
import uuid

import pytest

from tests.test_audit_api import api as api
from tests.test_access_profiles import create as fixed_profile, change as change_profile, pat
from tests.test_roles import setup_role, update_role, must
from tests.test_continuous_access import add_project


def candidates(client, project="project"):
    response = client.get("/api/projects/" + project + "/workflow-assignees")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] == project
    assert all(set(row) == {"id", "label", "authorization_mode"} for row in payload["assignees"])
    return {row["id"] for row in payload["assignees"]}


def create_workflow(client, grant):
    return client.post("/api/tools/call", json={"tool": "workflows_create", "arguments": {
        "project": "project", "title": "Assigned review", "goal": "Check current permissions",
        "template": "review_fix", "assignee_grant_id": grant, "idempotency_key": uuid.uuid4().hex,
    }})


def test_dynamic_role_candidate_can_receive_workflow(api):
    app, client, _ = api
    role, profile, token = setup_role(client, project_rules=[{"actions": ["read", "write"], "projects": ["project"]}])
    stored = app.state.store.one("SELECT scopes,projects FROM grants WHERE id=?", (token["grant_id"],))
    assert json.loads(stored["scopes"]) == ["codepier.role_access"]
    assert json.loads(stored["projects"]) == []
    assert candidates(client) == {token["grant_id"]}
    result = create_workflow(client, token["grant_id"])
    assert result.status_code == 200, result.text
    row = app.state.store.one("SELECT * FROM workflows WHERE id=?", (result.json()["workflow_id"],))
    assert row["grant_id"] == token["grant_id"]
    assert app.state.store.one("SELECT COUNT(*) n FROM operations")["n"] == 0


@pytest.mark.parametrize("change", ["pause", "revoke", "read_only", "expired"])
def test_candidates_and_final_create_recheck_role_changes(api, change):
    app, client, _ = api
    role, profile, token = setup_role(client, project_rules=[{"actions": ["read", "write"], "projects": ["project"]}])
    identifier = token["grant_id"]
    assert identifier in candidates(client)
    if change == "pause":
        must(update_role(client, role, enabled=False))
    elif change == "revoke":
        must(client.delete("/api/grants/" + identifier))
    elif change == "read_only":
        must(update_role(client, role, project_rules=[{"actions": ["read"], "projects": ["project"]}]))
    else:
        app.state.store.execute("UPDATE tokens SET expires=? WHERE grant_id=?", (time.time() - 1, identifier))
    assert identifier not in candidates(client)
    result = create_workflow(client, identifier)
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "INVALID_ASSIGNEE"
    assert app.state.store.one("SELECT COUNT(*) n FROM workflows")["n"] == 0


def test_fixed_profile_ceiling_and_exact_project_rules_are_live(api):
    app, client, _ = api
    add_project(app)
    role, _, dynamic = setup_role(client, project_rules=[
        {"actions": ["read"], "all_projects": True},
        {"actions": ["read", "write"], "projects": ["project"]},
    ])
    profile = fixed_profile(client, scopes=["read", "write"], projects=["project", "future"])
    fixed = pat(client, profile)
    assert candidates(client) == {dynamic["grant_id"], fixed["grant_id"]}
    assert candidates(client, "future") == {fixed["grant_id"]}
    narrowed = must(change_profile(client, profile, projects=["future"]))
    assert candidates(client) == {dynamic["grant_id"]}
    assert candidates(client, "future") == {fixed["grant_id"]}
    must(change_profile(client, narrowed, scopes=["read"]))
    assert candidates(client, "future") == set()


def test_candidates_are_owner_space_scoped_and_do_not_write(api):
    app, client, _ = api
    _, _, token = setup_role(client, project_rules=[{"actions": ["read", "write"], "projects": ["project"]}])
    store = app.state.store
    from tests.legacy_iam_fixture import seed_owner
    seed_owner(store, "other-owner", "other-owner")
    store.execute("UPDATE grants SET user_id=? WHERE id=?", ("other-owner", token["grant_id"]))
    before = {table: store.one("SELECT COUNT(*) n FROM " + table)["n"]
              for table in ("grants", "tokens", "workflows", "operations", "audit")}
    assert candidates(client) == set()
    after = {table: store.one("SELECT COUNT(*) n FROM " + table)["n"] for table in before}
    assert after == before
    store.execute("INSERT INTO spaces(id,label,kind,created) VALUES(\'elsewhere\',\'Other\',\'team\',1)")
    store.execute("INSERT INTO grants(id,user_id,label,scopes,projects,created,space_id,owner_user_id) "
                  "VALUES(\'other-space\',\'owner\',\'Other Space connection\',\'[\"read\",\"write\"]\',\'[\"project\"]\',1,\'elsewhere\',\'owner\')")
    assert candidates(client) == set()


def test_read_only_or_unknown_project_is_not_assignable(api):
    app, client, _ = api
    assert client.get("/api/projects/missing/workflow-assignees").status_code == 404
    app.state.store.execute("UPDATE projects SET mode='read' WHERE id='project'")
    response = client.get("/api/projects/project/workflow-assignees")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "READ_ONLY"
