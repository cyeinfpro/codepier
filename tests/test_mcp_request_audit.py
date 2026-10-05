"""Server-owned correlation, bounded redaction and transport semantics."""
import asyncio
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import mcp_request_audit as audit
from hub.http import install_http_behaviors
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from tests.test_audit_api import api, rpc

SECRET = "PRIVATE_INJECTION_SECRET"


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(audit, "write_event", lambda event: captured.append(dict(event)))
    return captured


def identifier(response):
    value = response.headers["X-CodePier-Request-ID"]
    assert re.fullmatch(r"[a-f0-9]{32}", value)
    return value


def test_auth_failure_has_ingress_id_before_activity(api, events):
    app, client, _ = api
    response = client.post("/mcp?private=" + SECRET, content=SECRET,
        headers={"Authorization": "Bearer " + SECRET, "X-CodePier-Request-ID": SECRET,
                 "Cookie": "private=" + SECRET})
    rid = identifier(response)
    assert response.status_code == 401
    assert [e["stage"] for e in events] == ["received", "authenticate", "rejected", "response_started", "finished"]
    assert all(e["request_id"] == rid for e in events)
    assert SECRET not in json.dumps(events)
    assert app.state.store.one("SELECT COUNT(*) AS n FROM mcp_activity")["n"] == 0
    assert "X-CodePier-Request-ID" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("payload", ["{broken", '{"method": []}', "[" * 1100 + "]" * 1100])
def test_malformed_body_is_redacted_before_activity(api, events, payload):
    app, client, pat = api
    response = client.post("/mcp", content=payload,
        headers={"Authorization": "Bearer " + pat, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    identifier(response)
    assert response.status_code == 400
    assert events[-1]["outcome"] == "protocol_error"
    assert not any(e["stage"] == "invoke_started" for e in events)
    assert app.state.store.one("SELECT COUNT(*) AS n FROM mcp_activity")["n"] == 0
    assert pat not in json.dumps(events)


def test_oversized_body_id_precedes_authentication(api, events):
    app, client, _ = api
    response = client.post("/mcp", content=b"x", headers={"Content-Length": str(7 * 1024 * 1024)})
    identifier(response)
    assert response.status_code == 413
    assert [e["stage"] for e in events] == ["received", "response_started", "finished"]
    assert events[-1]["outcome"] == "http_error"
    assert not app.state.store.all("SELECT * FROM mcp_activity")


@pytest.mark.parametrize("field", ["id", "method", "tool"])
def test_untrusted_labels_never_enter_audit(api, events, field):
    _, client, pat = api
    hostile = SECRET + "\n" + "x" * 10000
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    if field == "tool":
        body.update(method="tools/call", params={"name": hostile, "arguments": {"secret": SECRET}})
    else:
        body[field] = hostile
    response = rpc(client, pat, body)
    identifier(response)
    assert SECRET not in json.dumps(events)
    assert len(json.dumps(events)) < 10000
    if field in {"method", "tool"}:
        assert not any(e["stage"] == "invoke_started" for e in events)


def test_modern_metadata_activity_and_logs_share_server_id(api, events):
    app, client, pat = api
    body = {"jsonrpc": "2.0", "id": SECRET, "method": "tools/call",
        "params": {"name": "project_query", "arguments": {"operation": "open", "project": "fixture"},
                   "_meta": {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {},
                             "com.codepier/requestId": SECRET}}}
    response = client.post("/mcp", json=body, headers={
        "Authorization": "Bearer " + pat, "Accept": "application/json, text/event-stream",
        "X-CodePier-Request-ID": SECRET, **request_headers(body)})
    rid = identifier(response)
    assert response.json()["result"]["_meta"]["com.codepier/requestId"] == rid
    row = app.state.store.one("SELECT * FROM mcp_activity ORDER BY id DESC LIMIT 1")
    assert row["request_id"] == rid
    assert all(e["request_id"] == rid for e in events)
    stages = [e["stage"] for e in events]
    assert stages.index("authenticated") < stages.index("route_resolved") < stages.index("invoke_started")
    assert stages[-2:] == ["response_started", "finished"]
    assert SECRET not in json.dumps(events)
    second = rpc(client, pat, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert second.json()["result"] == {}  # Legacy payload compatibility.
    assert identifier(second) != rid


def test_trace_is_bounded_and_handles_non_scalar_labels(events):
    trace = audit.Trace(True, [])
    trace.record([], method={})
    trace.record("parsed", method=[], protocol={}, outcome=[], tool=SECRET + "\n", operation_id=SECRET, error_code={})
    for _ in range(100):
        trace.record("invoke_started")
    trace.finish()
    trace.record("rejected")
    assert len(events) == audit.MAX_EVENTS
    assert events[-1]["stage"] == "finished"
    assert events[-1]["omitted_events"] > 0
    assert events[0]["rpc_method"] == "unknown"
    assert events[0]["error_code"] == "OTHER"
    assert SECRET not in json.dumps(events)


def test_rate_gate_has_bounded_global_state_and_suppression_summary():
    now = [0.0]
    gate = audit.RateGate(rate=1, burst=2, clock=lambda: now[0])
    assert gate.admit() == (True, 0)
    assert gate.admit() == (True, 0)
    for _ in range(100):
        assert gate.admit() == (False, 0)
    now[0] = 60
    assert gate.admit() == (True, 100)
    assert gate.suppressed == 0 and gate.tokens <= 2


def test_logger_failure_never_changes_request(api, monkeypatch):
    _, client, pat = api
    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(audit.LOGGER, "info", broken)
    response = rpc(client, pat, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.json()["result"] == {}
    identifier(response)


def test_unhandled_error_keeps_default_response_and_server_id(events):
    app = FastAPI()
    install_http_behaviors(app)
    app.add_middleware(audit.MCPRequestAuditMiddleware)
    @app.post("/mcp")
    def explode():
        raise RuntimeError(SECRET)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/mcp")
        identifier(response)
        assert response.status_code == 500 and response.text == "Internal Server Error"
    assert events[-1]["outcome"] == "internal_error"
    assert SECRET not in json.dumps(events)
    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match=SECRET):
            client.post("/mcp")


@pytest.mark.parametrize("kind", ["cancelled", "reset", "disconnect"])
def test_transport_interruption_does_not_cancel_business_work(events, kind):
    async def scenario():
        business = {"cancel_requested": False}
        async def endpoint(scope, receive, send):
            if kind == "cancelled":
                raise asyncio.CancelledError()
            if kind == "reset":
                raise ConnectionResetError(SECRET)
            assert (await receive())["type"] == "http.disconnect"
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            raise AssertionError("No response expected")
        middleware = audit.MCPRequestAuditMiddleware(endpoint)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": []}
        if kind == "disconnect":
            await middleware(scope, receive, send)
        else:
            with pytest.raises(asyncio.CancelledError if kind == "cancelled" else ConnectionResetError):
                await middleware(scope, receive, send)
        assert not business["cancel_requested"]
        assert audit.request_id() is None
    asyncio.run(scenario())
    assert events[-1]["outcome"] == "transport_interrupted"
    assert not any(e["stage"] == "task_cancel_acknowledged" for e in events)
    assert SECRET not in json.dumps(events)


def test_concurrent_requests_keep_distinct_context_ids(events):
    async def scenario():
        async def endpoint(scope, receive, send):
            before = audit.request_id()
            await asyncio.sleep(0)
            assert audit.request_id() == before
            audit.mark("invoke_started")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            pass
        middleware = audit.MCPRequestAuditMiddleware(endpoint)
        await asyncio.gather(*(middleware({"type": "http", "path": "/mcp", "method": "POST"}, receive, send) for _ in range(10)))
        assert audit.request_id() is None
    asyncio.run(scenario())
    ids = {e["request_id"] for e in events}
    assert len(ids) == 10
    assert all(sum(e["stage"] == "finished" and e["request_id"] == rid for e in events) == 1 for rid in ids)


def test_sampling_still_returns_ids_without_logging_request_content(events):
    async def scenario():
        async def endpoint(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        async def receive():
            return {"type": "http.request", "body": b""}
        sent = []
        async def send(message):
            sent.append(message)
        await audit.MCPRequestAuditMiddleware(endpoint, audit.RateGate(rate=0, burst=0))(
            {"type": "http", "path": "/mcp", "method": "POST"}, receive, send)
        assert re.fullmatch(rb"[a-f0-9]{32}", dict(sent[0]["headers"])[audit.HEADER])
    asyncio.run(scenario())
    assert events == []
