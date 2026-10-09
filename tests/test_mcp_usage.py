"""Payload estimates: multilingual math, wire semantics and live IAM boundaries."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from starlette.requests import Request

from hub.mcp_usage import MCPUsage
from shared.token_estimate import (
    MAX_CHARACTERS, NUMBERS, estimate_input, estimate_output, summarize, usage,
)
from shared.util import DevError
from tests.test_audit_api import api, rpc


def output(text, **kwargs):
    return {"content": [{"type": "text", "text": text}], **kwargs}


@pytest.mark.parametrize("text", [
    "Hello world!", "中文与 English 混合😀", "á café Ελληνικά",
    "def f(x):\n    return {'值': x + 1}\n", "",
])
def test_exact_included_unicode_counts_and_labelled_range(text):
    result = estimate_output(output(text))
    assert result["characters"] == len(text)
    assert result["utf8_bytes"] == len(text.encode("utf-8"))
    assert 0 <= result["low"] <= result["estimated_tokens"] <= result["high"]
    assert result["state"] == "available"


def test_unicode_density_is_not_calls_times_constant():
    english = estimate_output(output("a " * 100))
    chinese = estimate_output(output("中 " * 100))
    assert chinese["estimated_tokens"] > english["estimated_tokens"]
    assert english["characters"] == chinese["characters"]
    assert chinese["utf8_bytes"] > english["utf8_bytes"]


def test_json_normalization_mirrors_and_repeated_blocks():
    value = {"content": "中文😀", "code": "x = 42"}
    text = json.dumps(value, ensure_ascii=False)
    one = estimate_output(output(text, structuredContent=value))
    assert one == estimate_output({"structuredContent": value})
    repeated = estimate_output({"content": [{"type": "text", "text": "same"}] * 2})
    assert repeated["characters"] == 8
    assert repeated["characters"] == 2 * estimate_output(output("same"))["characters"]


def test_secret_binary_schema_and_host_meta_exclusions():
    baseline = {"command": "echo test"}
    expanded = {**baseline, "authorization": "Bearer TOP_SECRET",
                "api_key": "TOP_SECRET", "clientSecret": "TOP_SECRET",
                "file": {"download_url": "https://example.test/?sig=PRIVATE", "size": 999999},
                "chunk_base64": "TINY", "inputSchema": {"secret": "schema"},
                "_meta": {"actual_usage": {"input_tokens": 999999999}, "secret": "metadata"}}
    measured = estimate_input(expanded)
    expected = estimate_input(baseline)
    assert all(measured[key] == expected[key] for key in NUMBERS)
    assert measured["excluded_fields"] == 7
    image = {"type": "image", "data": "A" * 50000, "mimeType": "image/png"}
    assert estimate_output({"content": [image]})["estimated_tokens"] == 0
    assert estimate_output({"content": [image]})["characters"] == 0
    chunk = {"upload_id": "u", "chunk_sha256": "x", "offset": 0, "data": "AAAA"}
    tiny = estimate_input(chunk)
    chunk["data"] = "B" * 100000
    assert all(tiny[key] == estimate_input(chunk)[key] for key in NUMBERS)
    assert "TOP_SECRET" not in json.dumps(usage(measured))


def test_json_text_redacts_before_counting_and_urls_never_count():
    value = {"text": "hello", "password": "NEVER_COUNT"}
    assert estimate_output(output(json.dumps(value)))["characters"] == len('{"text":"hello"}')
    result = estimate_output(output("before https://files.example.test/?signature=PRIVATE after"))
    assert result["characters"] == len("before  after")
    assert result["excluded_fields"] == 1
    assert estimate_output(output("Bearer PRIVATE"))["characters"] == 0


def test_null_zero_source_truncation_and_estimator_budget_are_distinct():
    assert estimate_output(None)["estimated_tokens"] is None
    assert estimate_output({})["state"] == "unavailable"
    assert estimate_output(output(""))["estimated_tokens"] == 0
    result = estimate_output(output("字 " * MAX_CHARACTERS))
    assert result["truncated"] and result["state"] == "partial"
    assert result["characters"] == MAX_CHARACTERS
    source = estimate_output(output(json.dumps({"output": "first", "output_truncated": True})))
    assert source["source_truncated"] and not source["truncated"]
    nested = {}
    child = nested
    for _ in range(100):
        child["n"] = {}
        child = child["n"]
    assert estimate_input(nested)["state"] == "partial"


def principal(app, token):
    return app.state.auth.bearer(Request({
        "type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())],
    }))


def call(client, token, arguments=None, **extra):
    return rpc(client, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "read", "arguments": {"project": "fixture", "path": "file.txt", **(arguments or {})}, **extra}})


def rows(app, token, **args):
    return app.state.runtime.integrations.activity(
        {"project": "fixture", "before_id": None, "limit": 100, **args}, principal(app, token))


def materialize(monkeypatch, app, text="fixture 中文"):
    async def invoke(*_):
        return {"operation_id": "a" * 32, "path": "file.txt", "content": text,
                "sha256": "f" * 64, "bytes": len(text.encode()), "truncated": False}
    monkeypatch.setattr(app.state.runtime, "invoke", invoke)


def test_http_records_only_payload_estimates_and_historical_null(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    response = call(client, token, _meta={"actual_usage": {"input_tokens": 900000}})
    assert response.status_code == 200 and not response.json()["result"].get("isError")
    page = rows(app, token)
    assert page["token_usage_summary"]["wire_attempts"] == 1
    item = page["activities"][0]
    assert item["token_usage"]["actual_usage"] is None
    assert item["token_usage"]["input"]["estimated_tokens"] > 0
    assert item["token_usage"]["output"]["estimated_tokens"] > 0
    persisted = app.state.store.one("SELECT metrics FROM mcp_usage")["metrics"]
    assert "fixture 中文" not in persisted and "file.txt" not in persisted
    assert "900000" not in persisted and token not in persisted
    app.state.store.execute("DELETE FROM mcp_usage")
    historical = rows(app, token)
    assert historical["activities"][0]["token_usage"] is None
    assert historical["token_usage_summary"]["output"]["estimated_tokens"] is None


def test_wire_retries_repeated_chunks_and_distinct_queries_are_all_counted(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app, "first chunk")
    for _ in range(2):
        assert call(client, token).status_code == 200
    materialize(monkeypatch, app, "second distinctly longer chunk")
    assert call(client, token, {"offset": 2}).status_code == 200
    page = rows(app, token)
    summary = page["token_usage_summary"]
    assert summary["wire_attempts"] == 3
    assert summary["distinct_server_operation_ids"] == 1
    assert summary["output"]["estimated_tokens"] == sum(
        row["token_usage"]["output"]["estimated_tokens"] for row in page["activities"])
    assert len(app.state.store.all("SELECT * FROM mcp_usage")) == 3
    record = page["activities"][0]
    app.state.runtime.integrations.usage_metrics.record(record["id"], record["token_usage"])
    assert len(app.state.store.all("SELECT * FROM mcp_usage")) == 3
    partial_page = rows(app, token, limit=1)
    assert partial_page["token_usage_summary"]["wire_attempts"] == 1
    assert partial_page["next_before_id"] is not None


def test_live_grant_revocation_mapping_and_space_isolation(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    original = principal(app, token)
    other = app.state.auth.issue_grant(
        replace(original, grant_id=None, actor="panel:admin", admin=True, token_hash=""),
        "other", ["read"], ["project"])["token"]
    assert rows(app, other)["activities"] == []
    app.state.store.execute("UPDATE projects SET root='/moved' WHERE id='project'")
    assert rows(app, token)["activities"] == []
    app.state.store.execute("UPDATE projects SET root='/tmp/fixture',device_id='device' WHERE id='project'")
    assert len(rows(app, token)["activities"]) == 1
    app.state.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (original.grant_id,))
    with pytest.raises(DevError):
        app.state.runtime.integrations.activity(
            {"project": "fixture", "before_id": None, "limit": 100}, original)


def test_metrics_cannot_be_read_in_other_space(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    with pytest.raises(DevError):
        app.state.runtime.integrations.activity(
            {"project": "fixture", "before_id": None, "limit": 100},
            replace(principal(app, token), space_id="unrelated"))


def test_failure_to_collect_or_persist_metrics_never_changes_tool_result(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    def broken(*_):
        raise sqlite3.OperationalError("synthetic usage failure")
    monkeypatch.setattr(app.state.runtime.integrations.usage_metrics, "record", broken)
    response = call(client, token)
    assert response.status_code == 200 and not response.json()["result"].get("isError")
    assert rows(app, token)["activities"][0]["token_usage"] is None
    assert app.state.runtime.integrations.write_errors > 0


def test_tool_errors_and_interrupted_missing_outputs(api, monkeypatch):
    app, client, token = api
    async def fail(*_):
        raise DevError("EXPECTED", "controlled failure")
    monkeypatch.setattr(app.state.runtime, "invoke", fail)
    response = call(client, token)
    assert response.json()["result"]["isError"]
    page = rows(app, token)
    assert page["activities"][0]["status"] == "tool_error"
    assert page["activities"][0]["token_usage"]["output"]["estimated_tokens"] > 0
    p = principal(app, token)
    service = app.state.runtime.integrations
    project = app.state.runtime.project("fixture", p)
    trace = service.begin(p, project, "read", {}, {"project": "fixture"})
    service.finish(trace, status="interrupted")
    service.finish(trace, status="interrupted")
    item = rows(app, token)["activities"][0]
    assert item["token_usage"]["output"]["state"] == "unavailable"
    assert item["token_usage"]["output"]["estimated_tokens"] is None


def test_retention_quotas_restart_and_historical_absence(api, monkeypatch):
    import hub.mcp_usage as module
    app, client, token = api
    monkeypatch.setattr(module, "MAX_ROWS", 2)
    materialize(monkeypatch, app)
    for _ in range(3):
        call(client, token)
    assert len(app.state.store.all("SELECT * FROM mcp_usage")) == 2
    assert rows(app, token)["token_usage_summary"]["unavailable_attempts"] == 1
    service = MCPUsage(app.state.store)
    record = app.state.store.one("SELECT * FROM mcp_usage ORDER BY activity_id LIMIT 1")
    app.state.store.execute("UPDATE mcp_usage SET created=0 WHERE activity_id=?", (record["activity_id"],))
    service.prune()
    assert len(app.state.store.all("SELECT * FROM mcp_usage")) == 1
    app.state.store.execute("DELETE FROM mcp_activity")
    service.prune()
    assert app.state.store.all("SELECT * FROM mcp_usage") == []
    with pytest.raises(ValueError, match="quota"):
        service.record(1, {"unexpected": "x" * 5000})


def test_no_estimate_for_catalog_or_unauthorized_project(api):
    app, client, token = api
    response = rpc(client, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    denied = call(client, token, {"project": "not-authorized"})
    assert denied.json()["result"]["isError"]
    assert app.state.store.all("SELECT * FROM mcp_usage") == []


def test_large_json_secret_and_binary_payloads_are_never_scanned_as_prose():
    value = {"password": "SECRET " * MAX_CHARACTERS, "text": "ok"}
    raw = json.dumps(value)
    measured = estimate_output(output(raw, structuredContent=value))
    assert measured["characters"] == len('{"text":"ok"}')
    assert not measured["truncated"]
    without_object = estimate_output(output(raw))
    assert without_object["truncated"] and without_object["characters"] == 0
    binary = {"upload_id": "u", "chunk_sha256": "hash", "data": "ABCD"}
    binary["data"] = "A" * MAX_CHARACTERS * 4
    assert not estimate_input(binary)["truncated"]


def test_many_excluded_fields_obey_node_budget():
    result = estimate_input({str(index) + "_secret": "unused" for index in range(20000)})
    assert result["truncated"] and result["excluded_fields"] < 20000


def test_usage_initialization_and_query_failure_are_nonfatal(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    service = app.state.runtime.integrations
    def broken(*_, **__):
        raise sqlite3.OperationalError("synthetic usage failure")
    monkeypatch.setattr(service.usage_metrics, "enrich", broken)
    page = rows(app, token)
    assert page["activities"][0]["token_usage"] is None
    assert page["token_usage_summary"]["unavailable_attempts"] == 1
    from hub import integrations
    monkeypatch.setattr(integrations, "MCPUsage", broken)
    restarted = integrations.HubIntegrations(app.state.runtime)
    assert restarted.usage_metrics is None and restarted.write_errors == 1
    assert restarted.activity({"project": "fixture", "before_id": None, "limit": 100},
                              principal(app, token))["activities"][0]["token_usage"] is None


def test_modern_boundary_matches_materialized_result_without_metadata(api, monkeypatch):
    from shared.mcp_protocol import MODERN, PREFIX, request_headers
    from jsonschema import Draft202012Validator
    from shared.contracts import OUTPUT_SCHEMAS
    app, client, token = api
    materialize(monkeypatch, app)
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "read", "arguments": {"project": "fixture", "path": "file.txt"},
        "_meta": {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {},
                  "actual_usage": {"input_tokens": 999999}, "token": "PRIVATE"}}}
    response = client.post("/mcp", json=body, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
        **request_headers(body)})
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    page = rows(app, token)
    measurement = page["activities"][0]["token_usage"]
    assert measurement["input"] == estimate_input(body["params"]["arguments"])
    assert measurement["output"] == estimate_output(result)
    assert measurement["actual_usage"] is None
    Draft202012Validator(OUTPUT_SCHEMAS["activity_list"]).validate(page)


def test_current_grant_project_restriction_blocks_cached_principal(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    original = principal(app, token)
    app.state.store.execute("UPDATE grants SET projects='[]' WHERE id=?", (original.grant_id,))
    with pytest.raises(DevError):
        app.state.runtime.integrations.activity(
            {"project": "fixture", "before_id": None, "limit": 100}, original)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_invalid_unicode_never_reports_replacement_bytes_as_exact(surrogate):
    assert estimate_output(output(surrogate))["state"] == "unavailable"
    assert estimate_input({"text": surrogate})["utf8_bytes"] is None


def test_all_missing_is_unknown_not_zero():
    result = summarize([{"id": 1, "operation_id": None, "token_usage": None}])
    assert result["unavailable_attempts"] == 1 and result["input"]["estimated_tokens"] is None
    assert result["actual_usage"] is None
