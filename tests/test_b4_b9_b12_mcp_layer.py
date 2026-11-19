"""Focused unit and HTTP-layer tests for B3, B4, B9, B12, B17, and cheat sheet presentation."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
import hub.core_tools as core_tools_module
from hub.mcp import presentation_options
from hub.mcp_streams import Streams, wait_cancelled
from hub.principal import Principal
from hub.tool_router import ToolRouter
from hub import unified_tools
from shared import contracts as contracts_module
from shared import public_collaboration as collab_contracts_module
from shared import token_estimate
from shared import unified_contracts as unified_contracts_module
from shared.crypto import password_hash
from shared.util import DevError
from tests.test_audit_api import api


def _read_sse_events(raw_text: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for chunk in raw_text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith(":"):
            events.append(("comment", chunk))
            continue
        event_name = ""
        data_lines: list[str] = []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        events.append((event_name or "message", "\n".join(data_lines)))
    return events


def _sse_jsonrpc_body(raw_text: str) -> dict:
    for event_name, data in _read_sse_events(raw_text):
        if event_name == "message" and data:
            return json.loads(data)
    raise AssertionError(f"No JSON-RPC SSE message frame found in {raw_text!r}")


def _post_mcp(client, pat, payload, query=""):
    return client.post(
        "/mcp" + query,
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + pat,
        },
    )


def test_unified_tool_resolve_skips_schema_deepcopy_and_capture_output_off_writer(api, monkeypatch):
    app, client, pat = api
    router = ToolRouter(app.state.store, app.state.runtime.gateway)
    grant_id = app.state.store.one("SELECT id FROM grants ORDER BY created DESC LIMIT 1")["id"]
    principal = Principal("pat:owner", "owner", {"read", "write", "execute"}, ["project"], grant_id=grant_id)

    # Warm collision cache for the current security epoch and gateway catalog revision.
    route_q = router.resolve(principal, "query")
    route_a = router.resolve(principal, "act")
    assert route_q.name == "query" and route_q.backend == "native"
    assert route_a.name == "act" and route_a.backend == "native"

    # Also warm read-only catalog definitions once.
    defs_full = router.definitions(principal, schemas="full")
    defs_lean = router.definitions(principal, schemas="lean")
    assert defs_full[1] and defs_lean[1]

    deepcopy_calls = 0
    real_deepcopy = copy.deepcopy

    def counting_deepcopy(obj, *args, **kwargs):
        nonlocal deepcopy_calls
        deepcopy_calls += 1
        return real_deepcopy(obj, *args, **kwargs)

    monkeypatch.setattr(copy, "deepcopy", counting_deepcopy)
    monkeypatch.setattr(contracts_module.copy, "deepcopy", counting_deepcopy, raising=False)

    def forbid_definitions(*_args, **_kwargs):
        raise AssertionError("ToolRouter.resolve must fast-path UNIFIED_TOOLS without definitions()")

    monkeypatch.setattr(router, "definitions", forbid_definitions)
    assert router.resolve(principal, "query").backend == "native"
    assert router.resolve(principal, "act").backend == "native"
    assert deepcopy_calls == 0

    # Frozen schemas must be immutable to accidental in-place mutation while remaining JSON-serializable.
    native_defs = contracts_module.tool_definitions()
    collab_defs = collab_contracts_module.tool_definitions()
    with pytest.raises(TypeError):
        native_defs[0]["inputSchema"]["properties"]["x"] = {}
    with pytest.raises(TypeError):
        collab_defs[0]["inputSchema"]["type"] = "array"
    assert json.dumps(native_defs)
    assert json.dumps(collab_defs)

    # Full HTTP tools/call check: read-only ops & capture_output stay off store.run().
    writer_calls: list[str] = []
    capture_threads: list[str] = []
    real_store_run = app.state.store.run
    real_capture = app.state.runtime.integrations.capture_output

    def tracking_store_run(fn, *args, **kwargs):
        writer_calls.append(getattr(fn, "__name__", repr(fn)))
        return real_store_run(fn, *args, **kwargs)

    def tracking_capture(trace, result):
        capture_threads.append(threading.current_thread().name)
        return real_capture(trace, result)

    async def fake_invoke(name, arguments, caller):
        assert name == "read"
        return {
            "path": arguments.get("path", "a.py"),
            "content": "hello = 1\n",
            "sha256": "abc123",
        }

    monkeypatch.setattr(app.state.store, "run", tracking_store_run)
    monkeypatch.setattr(app.state.runtime.integrations, "capture_output", tracking_capture)
    monkeypatch.setattr(app.state.runtime, "invoke", fake_invoke)

    res = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
    )
    assert res.status_code == 200
    assert not res.json()["result"].get("isError")

    forbidden_on_writer = {
        "origin_allowed",
        "public_url",
        "resolve",
        "project",
        "refresh_principal",
        "bearer",
        "attach",
        "capture_output",
        "list_tools",
    }
    assert forbidden_on_writer.isdisjoint(writer_calls), f"Read/metering ran on writer: {writer_calls}"
    assert capture_threads and all(name.startswith("hub-metering") for name in capture_threads)

    # Check cached origin/public_url helper and per-request auth reuse across repeated fast requests.
    bearer_calls = 0
    real_bearer = app.state.auth.bearer

    def counting_bearer(req):
        nonlocal bearer_calls
        bearer_calls += 1
        return real_bearer(req)

    monkeypatch.setattr(app.state.auth, "bearer", counting_bearer)
    res2 = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
    )
    assert res2.status_code == 200
    assert bearer_calls == 1

    # Check Redline #1 mid-flight revocation: revoking during await invalidates cached principal immediately.
    async def revoking_invoke(name, arguments, caller):
        await real_store_run(lambda: app.state.store.execute("UPDATE grants SET revoked=1"))
        return {"path": "a.py", "content": "hello = 1\n", "sha256": "abc123"}

    monkeypatch.setattr(app.state.runtime, "invoke", revoking_invoke)
    mid_revoked = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 1025,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
    )
    assert mid_revoked.status_code == 200
    mid_payload = mid_revoked.json()["result"]
    assert mid_payload["isError"] is True
    assert mid_payload["structuredContent"]["error_code"] == "INVALID_TOKEN"

    # Next request also rejected.
    revoked = _post_mcp(client, pat, {"jsonrpc": "2.0", "id": 103, "method": "ping"})
    assert revoked.status_code == 401


def test_default_unified_view_uses_meta_mirror_and_single_bound_pass(api, monkeypatch):
    app, client, pat = api

    assert presentation_options({})[:6] == ("unified", "full", "3", "lean", "compact", "meta")
    assert presentation_options({"view": "2"})[:6] == ("unified", "full", "2", "lean", "json", "full")
    assert presentation_options({"mirror": "full"})[:6] == (
        "unified",
        "full",
        "3",
        "lean",
        "compact",
        "full",
    )

    # Contract cheat sheet check (~355 bytes total across query & act, B5/B13 schema fields preserved).
    visible = unified_tools.directory({"read", "write", "execute"}, collaboration=True)
    assert "Common args:" in unified_contracts_module.description("query", visible["query"])
    assert "Common args:" in unified_contracts_module.description("act", visible["act"])
    assert "per_dir" in contracts_module.TOOLS["fs_tree"].model.model_fields
    assert "resume_after_path" in contracts_module.TOOLS["fs_search"].model.model_fields

    bound_calls = 0
    real_bound = core_tools_module.bound_batch_response

    def counting_bound(*args, **kwargs):
        nonlocal bound_calls
        bound_calls += 1
        return real_bound(*args, **kwargs)

    monkeypatch.setattr(core_tools_module, "bound_batch_response", counting_bound)

    async def fake_invoke(name, arguments, caller):
        if name == "read" and arguments.get("operation") == "batch":
            return {
                "batch": True,
                "files": [
                    {
                        "path": "big.txt",
                        "ok": True,
                        "content": "line\n" * 40,
                        "sha256": "sha-big",
                    }
                ],
            }
        if name == "read":
            return {
                "path": "a.py",
                "content": "print('hello')\n" * 30,
                "sha256": "sha-a",
            }
        if name == "collaboration_query":
            return {
                "action": "overview",
                "ok": True,
                "room_id": "room-1",
                "messages": [{"id": f"m{i}", "body": "x" * 200} for i in range(10)],
                "next_call": {
                    "tool": "collaboration_query",
                    "arguments": {"action": "overview", "project": "fixture"},
                },
            }
        raise DevError("NOT_FOUND", "missing path", 404)

    monkeypatch.setattr(app.state.runtime, "invoke", fake_invoke)

    # 1) Default unified call uses view=3, mirror="meta", and runs bound_batch_response exactly once.
    resp_meta = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 201,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
    )
    assert resp_meta.status_code == 200
    assert bound_calls == 1
    result_meta = resp_meta.json()["result"]
    assert result_meta["structuredContent"]["content_in"] == "text"
    assert "content" not in result_meta["structuredContent"]
    assert "print('hello')" in result_meta["content"][0]["text"]

    # 2) Explicit ?view=2 and ?mirror=full stay backward-compatible (Redline #4).
    bound_calls = 0
    resp_v2 = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 202,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
        query="?view=2",
    )
    assert resp_v2.status_code == 200
    assert bound_calls == 1
    assert "content" in resp_v2.json()["result"]["structuredContent"]

    resp_full_mirror = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 203,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "file", "project": "fixture", "path": "a.py"},
            },
        },
        query="?mirror=full",
    )
    assert resp_full_mirror.status_code == 200
    assert "content" in resp_full_mirror.json()["result"]["structuredContent"]

    # 3) isError: True receipts enforce compact structuredContent in mirror="meta".
    resp_err = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 204,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "tree", "project": "fixture"},
            },
        },
    )
    assert resp_err.status_code == 200
    err_result = resp_err.json()["result"]
    assert err_result["isError"] is True
    assert set(err_result["structuredContent"]) <= {"error_code", "recovery_summary", "next_call"}
    assert err_result["structuredContent"]["error_code"] == "NOT_FOUND"
    assert "recovery_summary" in err_result["structuredContent"]
    assert "error" in json.loads(err_result["content"][0]["text"])

    # 4) COLLABORATION_TOOLS: unified `query` collab defaults to mirror="meta",
    # while legacy `collaboration_query` keeps mirror="full" on bare /mcp unless
    # ?view=3&mirror=meta is requested explicitly.
    monkeypatch.setattr(
        app.state.runtime.collaboration,
        "config",
        dataclasses.replace(app.state.runtime.collaboration.config, enabled=True),
    )
    monkeypatch.setattr(app.state.runtime.store, "collaboration", app.state.runtime.collaboration, raising=False)

    resp_unified_collab = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 205,
            "method": "tools/call",
            "params": {
                "name": "query",
                "arguments": {"action": "collab.list", "view": "overview", "project": "fixture"},
            },
        },
    )
    assert resp_unified_collab.status_code == 200
    unified_collab = resp_unified_collab.json()["result"]
    assert not unified_collab.get("isError"), unified_collab
    assert unified_collab["structuredContent"]["content_in"] == "text"
    assert unified_collab["structuredContent"]["room_id"] == "room-1"
    assert "messages" not in unified_collab["structuredContent"]
    assert "messages" in json.loads(unified_collab["content"][0]["text"])

    resp_legacy_collab_default = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 206,
            "method": "tools/call",
            "params": {
                "name": "collaboration_query",
                "arguments": {"action": "overview", "project": "fixture"},
            },
        },
    )
    assert resp_legacy_collab_default.status_code == 200
    assert "messages" in resp_legacy_collab_default.json()["result"]["structuredContent"]

    resp_collab_meta = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 207,
            "method": "tools/call",
            "params": {
                "name": "collaboration_query",
                "arguments": {"action": "overview", "project": "fixture"},
            },
        },
        query="?view=3&mirror=meta",
    )
    assert resp_collab_meta.status_code == 200
    collab_meta = resp_collab_meta.json()["result"]
    assert collab_meta["structuredContent"]["content_in"] == "text"
    assert collab_meta["structuredContent"]["room_id"] == "room-1"
    assert "messages" not in collab_meta["structuredContent"]
    assert "messages" in json.loads(collab_meta["content"][0]["text"])

    resp_collab_full = _post_mcp(
        client,
        pat,
        {
            "jsonrpc": "2.0",
            "id": 208,
            "method": "tools/call",
            "params": {
                "name": "collaboration_query",
                "arguments": {"action": "overview", "project": "fixture"},
            },
        },
        query="?mirror=full",
    )
    assert resp_collab_full.status_code == 200
    assert "messages" in resp_collab_full.json()["result"]["structuredContent"]


def test_sse_keepalive_works_for_non_unified_profiles_and_emits_error_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    monkeypatch.setenv("CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL", "0.02")
    monkeypatch.setenv("CODEPIER_MCP_SSE_GRACE_SECONDS", "0.02")

    # 1) Default per-principal limit raised to 64 with stale stream reclamation.
    streams = Streams()
    assert streams.per_principal_limit == 64

    async def _exercise_stream_limits():
        req = SimpleNamespace(headers={"authorization": "Bearer tok"})
        principal = Principal("pat:owner", "owner", {"read"}, ["project"], grant_id="g1")
        events = [streams.begin(req, principal, f"id-{i}") for i in range(64)]
        with pytest.raises(DevError) as exc_info:
            streams.begin(req, principal, "id-overflow")
        assert exc_info.value.code == "TOO_MANY_STREAMS"
        events[0].set()  # Marks stream done/stale; next begin() reclaims it.
        reclaimed = streams.begin(req, principal, "id-reclaimed")
        reclaimed.set()
        for ev in events[1:]:
            ev.set()

    asyncio.run(_exercise_stream_limits())

    # 2) Real FastAPI HTTP SSE keepalive for ?profile=core + valid JSON-RPC error SSE frame.
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    store.execute("INSERT INTO users VALUES (?,?,?,?)", ("owner", "admin", password_hash("pw"), 1e12))
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)",
        ("device", "fixture", store.encrypt("x" * 43), 1.0),
    )
    store.execute(
        "INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES (?,?,?,?,?,?,?,?,?)",
        ("project", "fixture", "fixture", "device", "/tmp/fixture", "", "write", 0, 1.0),
    )
    principal = Principal("panel:admin", "owner", {"read", "write", "execute"}, ["*"], admin=True)
    pat = app.state.auth.issue_grant(principal, "fixture", ["read", "execute"], ["project"])["token"]

    mode = {"action": "success"}

    async def fake_invoke(name, arguments, caller):
        if mode["action"] == "cancel_wait":
            if arguments.get("wait_seconds", 0) == 0:
                return {"operations": [{"operation_id": "op-1", "pending": True}]}
            await asyncio.sleep(2.0)
            return {"operations": [{"operation_id": "op-1", "pending": False}]}
        await asyncio.sleep(0.08)
        if mode["action"] == "fail":
            raise RuntimeError("unexpected boom")
        return {
            "operations": [
                {"operation_id": "op-1", "pending": False, "exit_code": 0, "output": "ok"}
            ]
        }

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.runtime, "invoke", fake_invoke)

        # Keepalive on non-unified profile=core process(operation="wait", wait_seconds=15).
        res_ok = _post_mcp(
            client,
            pat,
            {
                "jsonrpc": "2.0",
                "id": "core-sse-1",
                "method": "tools/call",
                "params": {
                    "name": "process",
                    "arguments": {"operation": "wait", "operation_ids": ["op-1"], "wait_seconds": 15},
                },
            },
            query="?profile=core",
        )
        assert res_ok.status_code == 200
        assert res_ok.headers["content-type"].startswith("text/event-stream")
        frames_ok = _read_sse_events(res_ok.text)
        assert any(kind == "comment" and "keepalive" in data for kind, data in frames_ok)
        msg_ok = _sse_jsonrpc_body(res_ok.text)
        assert msg_ok["id"] == "core-sse-1"
        assert not msg_ok["result"].get("isError")

        # notifications/cancelled wakes non-unified profile=core wait promptly.
        mode["action"] = "cancel_wait"
        cancel_done = threading.Event()

        def _send_cancel():
            time.sleep(0.05)
            cancel_resp = _post_mcp(
                client,
                pat,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "core-cancel-1"},
                },
                query="?profile=core",
            )
            assert cancel_resp.status_code == 202
            cancel_done.set()

        canceller = threading.Thread(target=_send_cancel)
        canceller.start()
        try:
            res_cancel = _post_mcp(
                client,
                pat,
                {
                    "jsonrpc": "2.0",
                    "id": "core-cancel-1",
                    "method": "tools/call",
                    "params": {
                        "name": "process",
                        "arguments": {"operation": "wait", "operation_ids": ["op-1"], "wait_seconds": 20},
                    },
                },
                query="?profile=core",
            )
        finally:
            canceller.join(timeout=2)
        assert cancel_done.is_set()
        assert res_cancel.status_code == 200
        cancel_msg = (
            _sse_jsonrpc_body(res_cancel.text)
            if res_cancel.headers["content-type"].startswith("text/event-stream")
            else res_cancel.json()
        )
        assert cancel_msg["id"] == "core-cancel-1"
        assert cancel_msg["result"]["structuredContent"]["operations"][0]["pending"] is True

        # Unexpected exception inside work AFTER SSE opened must emit valid JSON-RPC error frame.
        mode["action"] = "fail"
        res_err = _post_mcp(
            client,
            pat,
            {
                "jsonrpc": "2.0",
                "id": "core-sse-err",
                "method": "tools/call",
                "params": {
                    "name": "process",
                    "arguments": {"operation": "wait", "operation_ids": ["op-1"], "wait_seconds": 15},
                },
            },
            query="?profile=core",
        )
        assert res_err.status_code == 200
        assert res_err.headers["content-type"].startswith("text/event-stream")
        frames_err = _read_sse_events(res_err.text)
        assert any(kind == "comment" and "keepalive" in data for kind, data in frames_err)
        msg_err = _sse_jsonrpc_body(res_err.text)
        assert msg_err["jsonrpc"] == "2.0"
        assert msg_err["id"] == "core-sse-err"
        assert msg_err["error"]["code"] == -32603
        assert msg_err["error"]["message"] == "Internal error"


def test_unified_wait_single_event_and_dispatch_lock_scoped(api, monkeypatch):
    app, _client, _pat = api
    runtime = app.state.runtime
    principal = Principal("pat:owner", "owner", {"read", "write", "execute"}, ["project"], grant_id="g1")

    # 1) B17 handles execute cleanly inline without store.run().
    writer_calls: list[str] = []
    real_store_run = app.state.store.run

    def tracking_store_run(fn, *args, **kwargs):
        writer_calls.append(getattr(fn, "__name__", repr(fn)))
        return real_store_run(fn, *args, **kwargs)

    monkeypatch.setattr(app.state.store, "run", tracking_store_run)

    # Verify single event-driven wait (both long >10s wait and <1s sub-second remainder, plus cancel).
    async def _run_wait_checks():
        wait_calls: list[tuple[str, float, object]] = []
        cancel_after = {"delay": None}

        async def fake_wait_for_operation(op_id, wait_seconds, *, cancel_event=None):
            wait_calls.append((op_id, wait_seconds, cancel_event))
            if cancel_after["delay"] is not None:
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
                return None
            # Even a dict-shaped wake result is not the caller-authorized view.
            return {"operations": [{"operation_id": op_id, "output": "raw-wake"}]}

        async def fake_invoke(name, arguments, caller):
            if name == "exec":
                return {
                    "operation_id": "op-exec-1",
                    "pending": True,
                    "consumer_proof": "secret-proof",
                    "consumer_session_id": "sess-1",
                }
            if name == "process":
                assert arguments["wait_seconds"] == 0
                return {
                    "operations": [
                        {"operation_id": "op-exec-1", "pending": False, "exit_code": 0, "output": "done"}
                    ]
                }
            raise AssertionError(f"Unexpected invoke tool: {name}")

        monkeypatch.setattr(runtime, "wait_for_operation", fake_wait_for_operation)
        monkeypatch.setattr(runtime, "invoke", fake_invoke)

        # (a) wait=25 + collab handle minting runs single wait_for_operation call and 0 store.run calls.
        plan_25 = SimpleNamespace(
            local=None,
            tool="exec",
            args={"project": "fixture", "command": "echo hi"},
            wait=25,
            collab=True,
            notes=None,
            action="exec",
        )
        val_25, tool_25, _ = await unified_tools.execute(runtime, plan_25, principal)
        assert len(wait_calls) == 1
        assert 20.0 < wait_calls[0][1] <= 25.0
        assert tool_25 == "process"
        assert val_25["operations"][0]["exit_code"] == 0
        assert not writer_calls

        # Test handle mint/open roundtrip uses zero store.run calls.
        token = unified_tools.mint_handle(app.state.store, principal, {"consumer_proof": "p1", "consumer_session_id": "s1"})
        plan_handle = SimpleNamespace(
            tool="collaboration_query",
            args={"action": "dot_inbox", "project": "fixture"},
            handle=token,
            notes=None,
        )
        await unified_tools._apply_handle(runtime, plan_handle, principal)
        assert plan_handle.args["consumer_proof"] == "p1"
        assert not writer_calls

        # (b) <1s wait (wait=0.25) is NOT dropped (legacy `if remaining < 1: break` bug fixed).
        wait_calls.clear()
        plan_subsec = SimpleNamespace(
            local=None,
            tool="exec",
            args={"project": "fixture", "command": "echo subsec"},
            wait=0.25,
            collab=False,
            notes=None,
            action="exec",
        )
        await unified_tools.execute(runtime, plan_subsec, principal)
        assert len(wait_calls) == 1
        assert 0 < wait_calls[0][1] <= 0.25

        # (c) wait_cancelled Event interrupts wait immediately without cancelling durable op.
        wait_calls.clear()
        cancel_after["delay"] = 0.02
        stop = asyncio.Event()
        token_ctx = wait_cancelled.set(stop)
        try:
            plan_cancel = SimpleNamespace(
                local=None,
                tool="exec",
                args={"project": "fixture", "command": "sleep 30"},
                wait=30,
                collab=False,
                notes=None,
                action="exec",
            )

            async def _trigger_cancel():
                await asyncio.sleep(0.02)
                stop.set()

            canceller = asyncio.create_task(_trigger_cancel())
            try:
                val_cancel, tool_cancel, _ = await asyncio.wait_for(
                    unified_tools.execute(runtime, plan_cancel, principal), timeout=1.0
                )
            finally:
                await canceller
        finally:
            wait_cancelled.reset(token_ctx)

        assert len(wait_calls) == 1
        assert tool_cancel == "exec"
        assert val_cancel["pending"] is True

        # 2) B12 concurrency: dispatch lock is distinct from delivery/prepare lock.
        dispatch_lock = runtime.device_dispatch_lock("device")
        delivery_lock = runtime.device_delivery_lock("device")
        assert dispatch_lock is not delivery_lock

    asyncio.run(_run_wait_checks())


def test_token_estimate_reports_wire_and_semantic_tokens_for_full_mirror():
    sample = "alpha beta gamma delta\n" * 100
    full_rendered = core_tools_module.result(
        "read",
        {"path": "notes.txt"},
        {"path": "notes.txt", "content": sample, "sha256": "abc"},
        view=2,
        text_mode="json",
        mirror="full",
    )
    meta_rendered = core_tools_module.result(
        "read",
        {"path": "notes.txt"},
        {"path": "notes.txt", "content": sample, "sha256": "abc"},
        view=3,
        text_mode="compact",
        mirror="meta",
    )

    full_metric = token_estimate.estimate_output(full_rendered)
    meta_metric = token_estimate.estimate_output(meta_rendered)
    text_only_metric = token_estimate.estimate_output({"content": meta_rendered["content"]})

    # Backward-compatible keys preserved.
    assert full_metric["state"] == "available"
    assert full_metric["estimated_tokens"] == full_metric["wire_tokens_est"]
    assert meta_metric["estimated_tokens"] == meta_metric["wire_tokens_est"]

    # Dual metering: mirror="full" doubles wire tokens relative to semantic tokens.
    assert full_metric["semantic_tokens_est"] > 0
    assert full_metric["wire_tokens_est"] >= full_metric["semantic_tokens_est"] * 1.9
    assert full_metric["mirror_overhead_ratio"] > 1.8

    # mirror="meta" preserves exact compact semantic tokens with nearly ~1x wire overhead and cuts wire size ~2x.
    assert 0 < meta_metric["semantic_tokens_est"] <= full_metric["semantic_tokens_est"]
    assert meta_metric["semantic_tokens_est"] == text_only_metric["semantic_tokens_est"]
    assert meta_metric["wire_tokens_est"] < full_metric["wire_tokens_est"] * 0.65
    assert meta_metric["mirror_overhead_ratio"] < 1.35

    # _meta / inputSchema / non-batch files are measured cleanly (no over-exclusion) and zero plaintext leaks (Redline #5).
    with_meta = {
        "content": [{"type": "text", "text": "hello world"}],
        "structuredContent": {"content_in": "text"},
        "_meta": {"com.codepier/requestId": "req-1", "extra": "m" * 200},
    }
    schema_payload = {
        "content": [{"type": "text", "text": "{}"}],
        "structuredContent": {
            "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            "files": ["a.py", "b.py"],
        },
        "_meta": {"requestId": "r1"},
    }
    schema_metric = token_estimate.estimate_output(schema_payload)
    assert schema_metric["excluded_fields"] == 0
    assert schema_metric["wire_tokens_est"] > 0
    assert token_estimate.estimate_output(with_meta)["wire_tokens_est"] > token_estimate.estimate_output(
        {"content": [{"type": "text", "text": "hello world"}], "structuredContent": {"content_in": "text"}}
    )["wire_tokens_est"]
    assert "alpha beta gamma" not in json.dumps(full_metric)
