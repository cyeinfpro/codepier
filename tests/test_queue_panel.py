"""Queue panel projections and revision-backed semantics against synthetic Hub data."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub.queue_panel import cancellable
from hub.scheduler_settings import public_snapshot
from shared.crypto import digest, password_hash
from shared.util import DevError
from tests.legacy_iam_fixture import attach_session_security


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    now = time.time()
    store.execute("INSERT INTO users VALUES (?,?,?,?)", ("owner", "admin", password_hash("synthetic-only"), now))
    store.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest("queue-session"), "owner", "csrf", now + 3600))
    attach_session_security(store)
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)",
                  ("device", "Fixture node", store.encrypt("x" * 43), now))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("project", "Fixture", "fixture", "device", "/tmp/fixture", "", "write", 0, now))
    with TestClient(app) as client:
        client.cookies.set("rd_session", "queue-session")
        client.headers["X-RD-CSRF"] = "csrf"
        yield app, client


def operation(app, identifier, *, state="queued", attempts=0, tool="read", owner="owner", space="legacy"):
    now = time.time() - 12
    app.state.store.execute("""INSERT INTO operations
        (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,
        attempts,space_id,owner_user_id,visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (identifier, "device" if space == "legacy" else None, "project" if space == "legacy" else None, "panel:admin", tool, json.dumps({"path":"README.md"}),
         identifier, state, now, now, attempts, space, owner, "private"))


def test_queue_full_authorization_precedes_annotation_and_no_cold_body_reads(panel, monkeypatch):
    app, client = panel
    for identifier in ("allowed", "restricted", "finished"):
        operation(app, identifier, state="succeeded" if identifier == "finished" else "queued")
    runtime = app.state.runtime
    original = runtime.operation_row
    seen = []
    def authorize(identifier, principal, **kwargs):
        assert kwargs == {"status_only": True}
        seen.append(identifier)
        if identifier == "restricted":
            raise DevError("FORBIDDEN", "restricted", 403)
        return original(identifier, principal, **kwargs)
    monkeypatch.setattr(runtime, "operation_row", authorize)
    def no_output(*args, **kwargs):
        raise AssertionError("Queue status must not load output")
    monkeypatch.setattr(runtime.outputs, "read", no_output)
    before = app.state.store.one("SELECT count(*) n FROM audit")["n"]
    response = client.get("/api/operations/queue")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [row["id"] for row in data["operations"]] == ["allowed"]
    assert seen == ["allowed", "restricted"]
    row = data["operations"][0]
    assert row["queue_stage"] == "hub_queue" and row["can_cancel"]
    assert not {"payload", "result", "output", "args_summary"} & row.keys()
    assert app.state.store.one("SELECT count(*) n FROM audit")["n"] == before


def test_queue_tenant_private_scope_and_instance_diagnostics(panel):
    app, client = panel
    operation(app, "mine")
    app.state.store.execute("INSERT INTO users VALUES (?,?,?,?)", ("other", "other", "not-a-login", time.time()))
    app.state.store.execute("INSERT INTO spaces(id,label,kind,created) VALUES (?,?,?,?)", ("other","Other","team",time.time()))
    operation(app, "foreign-private", owner="other")
    operation(app, "foreign-space", space="other")
    assert client.get("/api/queue-diagnostics").status_code == 200
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    response = client.get("/api/operations/queue")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["operations"]] == ["mine"]
    assert response.json()["diagnostics_available"] is False
    assert client.get("/api/queue-diagnostics").status_code == 403
    client.cookies.clear()
    assert client.get("/api/operations/queue").status_code == 401


def test_queue_blockers_waiting_and_bounded_result(panel):
    app, client = panel
    operation(app, "writer", state="running", attempts=1)
    operation(app, "reader", state="queued", attempts=1)
    app.state.runtime.diagnostics.record("reader", "waiting_resource", source="agent", seq=1,
        detail={"queue_reason":"resource_conflict", "lane":"read", "blocked_by":["writer"]})
    response = client.get("/api/operations/queue")
    assert response.status_code == 200, response.text
    rows = {row["id"]:row for row in response.json()["operations"]}
    assert rows["reader"]["queue_stage"] == "agent_waiting"
    assert rows["reader"]["waiting"]["blocked_by"][0]["operation_id"] == "writer"
    for number in range(102):
        operation(app, f"more-{number:03}")
    data = client.get("/api/operations/queue").json()
    assert len(data["operations"]) == 100 and data["truncated"] is True


@pytest.mark.parametrize("tool,attempts,state,cancel_requested,allowed", [
    ("read",1,"running",0,True), ("exec",1,"running",0,True),
    ("edit",1,"running",0,False), ("edit",0,"queued",0,True),
    ("read",0,"succeeded",0,False), ("read",0,"queued",1,False),
    ("agent_update",0,"queued",0,False),
])
def test_cancel_affordance_matches_operation_policy(tool, attempts, state, cancel_requested, allowed):
    runtime = SimpleNamespace(authorize=lambda *a, **k: None)
    row = {"tool":tool,"attempts":attempts,"state":state,"cancel_requested":cancel_requested,"project_id":"p"}
    assert cancellable(runtime, object(), row) is allowed


def test_cancel_affordance_requires_execute():
    def denied(*args, **kwargs):
        raise DevError("FORBIDDEN", "execute required", 403)
    assert not cancellable(SimpleNamespace(authorize=denied), object(),
        {"tool":"read","attempts":0,"state":"queued","project_id":"p"})


def snapshot(semantics):
    return {"running":0,"queued":0,"reason":"healthy","resource_semantics":semantics,
        "lanes":{name:{"running":0,"queued":0,"capacity":4} for name in ("execution","read","remote")}}


@pytest.mark.parametrize("value", [None, [], {}, True, 2, "unknown"])
def test_malformed_semantics_report_is_ignored(value):
    assert "resource_semantics" not in public_snapshot(snapshot(value))


def test_saved_semantics_never_claim_applied_before_agent_report(panel):
    app, client = panel
    path = "/api/settings/scheduler?device=device"
    initial = client.get(path).json()
    assert initial["resource_semantics"]["effective"] == "strict"
    saved = client.put("/api/settings/scheduler", json={"device_id":"device",
        "expected_revision":initial["revision"],"config":{"resource_semantics":"relaxed"}})
    assert saved.status_code == 200, saved.text
    data = saved.json()
    assert data["resource_semantics"] == {"supported":False,"configured":"relaxed","effective":"strict"}
    info = {"scheduler_protocol":1, "resource_semantics_protocol":2,
        "scheduler_revision":data["revision"], "scheduler":snapshot("relaxed")}
    app.state.store.execute("UPDATE devices SET info=? WHERE id='device'", (json.dumps(info),))
    assert client.get(path).json()["resource_semantics"]["effective"] == "relaxed"
    info["scheduler"].pop("resource_semantics")
    app.state.store.execute("UPDATE devices SET info=? WHERE id='device'", (json.dumps(info),))
    assert client.get(path).json()["resource_semantics"]["effective"] is None
