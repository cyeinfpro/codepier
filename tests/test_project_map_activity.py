"""Project-map classification is read-only, scoped and applied before pagination."""

import json
import time

import pytest

from tests.test_queue_panel import panel, operation  # noqa: F401
from hub.call_log import operation_summary, public_row


def seed(app):
    for n, tool in enumerate(
        [
            "read",
            "project_knowledge_build",
            "write",
            "project_knowledge_query",
            "project_knowledge_query_extra",
        ]
    ):
        identifier = f"{n + 1:032x}"
        operation(app, identifier, tool=tool, state="succeeded")
        app.state.store.execute(
            "UPDATE operations SET created=?,updated=? WHERE id=?",
            (time.time() - n, time.time() - n, identifier),
        )
    # User-controlled arguments do not turn a normal tool into map work.
    app.state.store.execute(
        "UPDATE operations SET args_summary=? WHERE id=?",
        (json.dumps({"tool": "project_knowledge_build", "category": "project_map"}), f"{1:032x}"),
    )


@pytest.mark.parametrize("endpoint", ["/api/operations", "/api/call-log"])
def test_category_default_compatibility_exact_match_and_counts(panel, endpoint):
    app, client = panel
    seed(app)
    default = client.get(endpoint).json()
    assert default["total"] == 5 and default["category"] == "all"
    maps = client.get(endpoint, params={"category": "project_map", "limit": 1}).json()
    assert maps["total"] == 2
    assert maps["operations"][0]["tool"] == "project_knowledge_build"
    params = {"category": "project_map", "limit": 1}
    params.update(
        {"cursor": maps["next_cursor"]}
        if endpoint.endswith("call-log")
        else {"offset": maps["next_offset"]}
    )
    last = client.get(endpoint, params=params).json()
    assert last["total"] == 2
    assert last["operations"][0]["tool"] == "project_knowledge_query"
    tools = client.get(endpoint, params={"category": "tools"}).json()
    assert tools["total"] == 3
    assert {row["tool"] for row in tools["operations"]} == {
        "read",
        "write",
        "project_knowledge_query_extra",
    }
    assert client.get(endpoint, params={"category": "not_valid"}).status_code == 422
    assert (
        client.get(endpoint, params={"category": "project_map", "project": "absent"}).json()[
            "total"
        ]
        == 0
    )


def test_call_log_cursor_watch_and_options_share_category(panel):
    app, client = panel
    seed(app)
    first = client.get("/api/call-log", params={"category": "project_map", "limit": 1}).json()
    assert set(first["tools"]) == {"project_knowledge_build", "project_knowledge_query"}
    assert (
        client.get(
            "/api/call-log", params={"category": "tools", "cursor": first["next_cursor"]}
        ).status_code
        == 400
    )
    watched = ",".join(f"{n:032x}" for n in range(1, 6))
    result = client.get("/api/call-log", params={"category": "tools", "watch": watched}).json()
    assert len(result["updates"]) == 3
    assert set(result["tools"]) == {"read", "write", "project_knowledge_query_extra"}


@pytest.mark.parametrize("endpoint", ["/api/operations", "/api/call-log"])
def test_map_count_preserves_private_and_space_boundaries(panel, endpoint):
    app, client = panel
    seed(app)
    store = app.state.store
    store.execute(
        "INSERT INTO users VALUES (?,?,?,?)", ("other", "other", "not-a-login", time.time())
    )
    store.execute(
        "INSERT INTO spaces(id,label,kind,created) VALUES (?,?,?,?)",
        ("other", "Other", "team", time.time()),
    )
    operation(app, "foreign-private", tool="project_knowledge_build", owner="other")
    operation(app, "foreign-space", tool="project_knowledge_query", space="other")
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    result = client.get(endpoint, params={"category": "project_map"}).json()
    assert result["total"] == 2
    assert all(
        row["id"] not in {"foreign-private", "foreign-space"} for row in result["operations"]
    )
    client.cookies.clear()
    assert client.get(endpoint, params={"category": "project_map"}).status_code == 401


def test_home_excludes_map_but_resource_queue_keeps_it(panel):
    app, client = panel
    seed(app)
    app.state.store.execute(
        "UPDATE operations SET state='queued' WHERE tool IN ('read','project_knowledge_build')"
    )
    app.state.store.execute("UPDATE operations SET state='failed' WHERE tool='write'")
    before = app.state.store.one("SELECT count(*) AS n FROM operations")["n"]
    result = client.get("/api/overview").json()
    assert result["today_operations"] == 3
    assert result["today_succeeded"] == 1
    assert result["today_failed"] == 1
    assert result["active_operations"] == 1
    assert all(
        row["tool"] not in {"project_knowledge_build", "project_knowledge_query"}
        for row in result["recent_operations"]
    )
    queue = client.get("/api/operations/queue").json()
    assert {row["tool"] for row in queue["operations"]} == {"read", "project_knowledge_build"}
    assert app.state.store.one("SELECT count(*) AS n FROM operations")["n"] == before


def test_existing_history_uses_indexes_for_counts_and_latest_device_action(panel, monkeypatch):
    from hub.iam_schema import migrate
    from hub.store_contract import CheckedConnection

    app, client = panel
    store = app.state.store
    seed(app)
    operation(app, "restart", tool="agent_restart", state="succeeded")
    # Exercise the upgrade path with existing records and the original recent index.
    with store.transaction() as db:
        db.execute("DROP INDEX IF EXISTS operations_panel_timeline")
        db.execute("DROP INDEX IF EXISTS operations_device_lifecycle")
        db.execute("""CREATE INDEX IF NOT EXISTS operations_panel_recent
                   ON operations(space_id,created DESC,state,owner_user_id,visibility,grant_id,project_id)""")
        migrate(db)
        migrate(db)
    store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    plans = {}
    execute = CheckedConnection.execute

    def observe(connection, sql, parameters=()):
        if "FROM operations WHERE " in sql and "AND created>=?" in sql and "count(*)" in sql:
            label = "daily"
        elif sql.startswith("SELECT id,tool,state,created,updated,error FROM operations"):
            label = "device"
        else:
            return execute(connection, sql, parameters)
        plans[label] = [
            row["detail"] for row in execute(connection, "EXPLAIN QUERY PLAN " + sql, parameters)
        ]
        return execute(connection, sql, parameters)

    monkeypatch.setattr(CheckedConnection, "execute", observe)
    response = client.get("/api/overview?include_token_usage=false")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["recent_operations"]) == 4
    assert data["devices"][0]["agent"]["latest_action"]["id"] == "restart"
    assert len(plans["daily"]) == 1 and "COVERING INDEX" in plans["daily"][0], plans
    assert len(plans["device"]) == 1 and "operations_device_lifecycle" in plans["device"][0], plans


def test_only_admission_metadata_is_a_source_not_user_arguments():
    args = {"_log": {"version": 2, "trigger_source": "system_maintenance"}}
    admitted = operation_summary(args)
    assert admitted["_log"]["trigger_source"] == "request"
    assert (
        public_row(
            {
                "tool": "project_knowledge_build",
                "args_summary": json.dumps(
                    {"_log": {"version": 1, "trigger_source": "system_maintenance"}}
                ),
            }
        )["trigger_source"]
        == "unknown"
    )
    assert (
        public_row({"tool": "project_knowledge_query", "args_summary": "not-json"})[
            "trigger_source"
        ]
        == "unknown"
    )


def test_default_call_log_cursor_is_compatible_with_explicit_all(panel):
    app, client = panel
    seed(app)
    first = client.get("/api/call-log", params={"limit": 1}).json()
    next_default = client.get("/api/call-log", params={"limit": 1, "cursor": first["next_cursor"]})
    next_all = client.get(
        "/api/call-log", params={"limit": 1, "category": "all", "cursor": first["next_cursor"]}
    )
    assert next_default.status_code == next_all.status_code == 200
    assert [row["id"] for row in next_default.json()["operations"]] == [
        row["id"] for row in next_all.json()["operations"]
    ]


def test_operation_sql_project_filter_never_expands_nonadmin_scope(panel):
    from hub.principal import Principal
    from hub.api.activity import operation_rows

    app, _ = panel
    seed(app)
    principal = Principal(
        actor="panel:admin", user_id="owner", scopes={"read"}, projects=[], admin=False
    )
    assert (
        operation_rows(app.state.store, principal, project="project", category="project_map") == []
    )
    assert (
        operation_rows(
            app.state.store, principal, project="project", category="project_map", count_only=True
        )
        == 0
    )
    principal.projects = ["project"]
    assert (
        operation_rows(
            app.state.store, principal, project="project", category="project_map", count_only=True
        )
        == 2
    )
