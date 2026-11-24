"""Tests for v2 capabilities, snapshot -> view-events catch-up, reset_required, and auth revocation."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
from types import SimpleNamespace
import uuid

import pytest

from hub.native_cli import make_native_router
from hub.native_projection import (
    commit_projection_step,
    prepare_projection_step,
    read_head,
)
from shared.native_cli import database, public
from shared.native_contracts import semantic_batch_digest
from shared.util import DevError
from tests.test_chat_sse import TestAuth, request, service, sync


def _endpoint(routes, suffix: str, method: str = "GET"):
    for r in routes:
        if r.path.endswith(suffix) and method in getattr(r, "methods", {"GET"}):
            return r.endpoint
    raise KeyError(suffix)


def _parse_sse_frame(raw: str) -> tuple[str, dict]:
    event = "message"
    data = {}
    for line in raw.strip().splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return event, data


@pytest.mark.asyncio
async def test_capabilities_and_snapshot_to_view_events_gapless_catchup(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    caps = await _endpoint(routes, "/capabilities")(request())
    assert caps["native_view_v2"] is True
    assert caps["schema_version"] == 2
    assert "window_limit" in caps["budgets"]

    # Sync first line and project it
    l1 = b'{"type":"user","receipt":"r1","text":"hello"}\n'
    await sync(obj, sid, l1, 0)
    await obj.drain_projections(sid)

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    assert snap["projection_revision"] >= 1
    assert snap["applied_cursor"] == len(l1)
    assert len(snap["items"]) == 1

    # Gap between snapshot and view-events subscription: 2 more lines committed
    l2 = b'{"type":"delta","receipt":"r1","item_id":"m1","text":"part1 "}\n'
    l3 = b'{"type":"message","receipt":"r1","item_id":"m1","text":"part1 final"}\n'
    await sync(obj, sid, l2, len(l1))
    await obj.drain_projections(sid)
    await sync(obj, sid, l3, len(l1) + len(l2))
    await obj.drain_projections(sid)

    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req, after=snap["resume_token"])
    it = resp.body_iterator

    batches = []
    while True:
        ev, payload = _parse_sse_frame(await anext(it))
        if ev == "synchronized":
            assert payload["projection_revision"] == snap["projection_revision"] + 2
            break
        assert ev == "batch"
        assert payload["payload_digest"] == semantic_batch_digest(payload)
        batches.append(payload)

    assert len(batches) == 2
    assert batches[0]["from_revision"] == snap["projection_revision"]
    assert batches[1]["to_revision"] == snap["projection_revision"] + 2

    # Live event arrives after synchronized
    l4 = b'{"type":"done","receipt":"r1","status":"completed"}\n'
    await sync(obj, sid, l4, len(l1) + len(l2) + len(l3))
    await obj.drain_projections(sid)
    ev, live_batch = _parse_sse_frame(await anext(it))
    assert ev == "batch"
    assert live_batch["to_cursor"] == len(l1 + l2 + l3 + l4)

    # Auth revocation mid-SSE stops private stream immediately
    auth.allowed = False
    ev, err = _parse_sse_frame(await anext(it))
    assert ev == "error"
    assert err["code"] == "LOGIN_REQUIRED"
    with pytest.raises(StopAsyncIteration):
        await anext(it)


@pytest.mark.asyncio
async def test_view_events_live_bursts_get_fresh_budget_after_idle(service, monkeypatch):
    obj, _, sid = service
    routes = make_native_router(TestAuth(), obj.runtime).routes
    snapshot = _endpoint(routes, "/sessions/{sid}/view")
    open_stream = _endpoint(routes, "/sessions/{sid}/view-events")
    clock = [100.0]
    monkeypatch.setattr("hub.native_view_api.time", SimpleNamespace(monotonic=lambda: clock[0]))

    first = b'{"type":"user","receipt":"r1","text":"hello"}\n'
    await sync(obj, sid, first, 0)
    await obj.drain_projections(sid)
    start = await snapshot(sid, request())
    second = b'{"type":"delta","receipt":"r1","item_id":"m1","text":"part"}\n'
    await sync(obj, sid, second, len(first))
    await obj.drain_projections(sid)

    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    response = await open_stream(sid, req, after=start["resume_token"], max_batches=1)
    iterator = response.body_iterator
    try:
        assert _parse_sse_frame(await anext(iterator))[0] == "batch"
        assert _parse_sse_frame(await anext(iterator))[0] == "synchronized"
        cursor = len(first + second)
        for index in (2, 3):
            clock[0] += 60.0
            line = json.dumps({"type": "message", "receipt": "r1", "item_id": f"m{index}", "text": "live"})
            data = (line + "\n").encode()
            await sync(obj, sid, data, cursor)
            await obj.drain_projections(sid)
            event, payload = _parse_sse_frame(await anext(iterator))
            assert event == "batch", payload
            assert payload["from_cursor"] == cursor
            cursor += len(data)
            assert payload["to_cursor"] == cursor
            assert _parse_sse_frame(await anext(iterator))[0] == "synchronized"
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_view_events_does_not_send_a_first_batch_over_byte_budget(service):
    obj, _, sid = service
    routes = make_native_router(TestAuth(), obj.runtime).routes
    first = b'{"type":"user","receipt":"r1","text":"hello"}\n'
    await sync(obj, sid, first, 0)
    await obj.drain_projections(sid)
    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    large = (json.dumps({"type": "message", "receipt": "r1", "item_id": "m2", "text": "x" * 5000}) + "\n").encode()
    await sync(obj, sid, large, len(first))
    await obj.drain_projections(sid)
    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    response = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req, after=snap["resume_token"], max_bytes=1024)
    iterator = response.body_iterator
    try:
        event, payload = _parse_sse_frame(await anext(iterator))
        assert event == "reset_required"
        assert payload["reason"] == "BUDGET_EXCEEDED"
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_view_events_reset_required_reasons_and_non_looping(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    # 1. PROJECTION_BUILDING when head is absent
    l1 = b'{"type":"user","receipt":"r1","text":"one"}\n'
    with closing(database(obj.directory)) as db, db:
        db.execute("INSERT INTO output VALUES (?,?,?)", (sid, 0, l1))
    snap_building = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    assert snap_building["sync"]["projection_state"] == "building"

    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req, after=snap_building["resume_token"])
    ev, payload = _parse_sse_frame(await anext(resp.body_iterator))
    assert ev == "reset_required"
    assert payload["reason"] == "PROJECTION_BUILDING"
    with pytest.raises(StopAsyncIteration):
        await anext(resp.body_iterator)

    # Now build projection and test REPLAY_WINDOW_EXPIRED + BUDGET_EXCEEDED
    with closing(database(obj.directory)) as db:
        staged = prepare_projection_step(db, sid)
        assert staged is not None and commit_projection_step(db, sid, staged)
    snap1 = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())

    l2 = b'{"type":"message","receipt":"r1","item_id":"m1","text":"two"}\n'
    l3 = b'{"type":"message","receipt":"r1","item_id":"m2","text":"three"}\n'
    await sync(obj, sid, l2, len(l1))
    await obj.drain_projections(sid)
    await sync(obj, sid, l3, len(l1) + len(l2))
    await obj.drain_projections(sid)

    # BUDGET_EXCEEDED
    req_b = request()
    req_b.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp_b = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req_b, after=snap1["resume_token"], max_batches=1)
    ev, p_b = _parse_sse_frame(await anext(resp_b.body_iterator))
    assert ev == "reset_required"
    assert p_b["reason"] == "BUDGET_EXCEEDED"
    with pytest.raises(StopAsyncIteration):
        await anext(resp_b.body_iterator)

    # REPLAY_WINDOW_EXPIRED when journal pruned
    with closing(database(obj.directory)) as db, db:
        db.execute("DELETE FROM native_projection_batches WHERE session_id=? AND to_revision<=?", (sid, snap1["projection_revision"] + 1))
        db.execute("UPDATE native_projection_heads SET retained_from_revision=? WHERE session_id=?", (snap1["projection_revision"] + 2, sid))
    req_exp = request()
    req_exp.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp_exp = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req_exp, after=snap1["resume_token"])
    ev, p_exp = _parse_sse_frame(await anext(resp_exp.body_iterator))
    assert ev == "reset_required"
    assert p_exp["reason"] == "REPLAY_WINDOW_EXPIRED"

    # EPOCH_CHANGED after clear action
    async def fake_request(action, project, args, **kwargs):
        return {"ok": True}

    obj.request = fake_request
    action_ep = _endpoint(routes, "/{action}", method="POST")
    await action_ep("clear", request([(b"x-rd-csrf", b"valid")], json.dumps({"project": "project", "args": {"id": sid, "receipt": "c" * 32}}).encode()))
    req_ep = request()
    req_ep.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp_ep = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req_ep, after=snap1["resume_token"])
    ev, p_ep = _parse_sse_frame(await anext(resp_ep.body_iterator))
    assert ev == "reset_required"
    assert p_ep["reason"] == "EPOCH_CHANGED"


@pytest.mark.asyncio
async def test_view_events_source_gap_and_corrupt_reset_reasons(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    valid = b'{"type":"user","receipt":"r1","text":"ok"}\n'
    corrupt = b'{bad-json\n'
    with closing(database(obj.directory)) as db, db:
        db.execute("INSERT INTO output VALUES (?,?,?)", (sid, 0, valid + corrupt))
        db.execute("UPDATE sessions SET size=? WHERE id=?", (len(valid + corrupt), sid))
    with closing(database(obj.directory)) as db:
        while True:
            st = prepare_projection_step(db, sid)
            if st is None:
                break
            commit_projection_step(db, sid, st)
        head = read_head(db, sid)
        assert head is not None and head.source_state == "source_corrupt"

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req, after=snap["resume_token"])
    ev, payload = _parse_sse_frame(await anext(resp.body_iterator))
    assert ev == "reset_required"
    assert payload["reason"] == "SOURCE_CORRUPT"

    # SOURCE_GAP
    with closing(database(obj.directory)) as db, db:
        db.execute("UPDATE native_projection_heads SET source_state='source_gap' WHERE session_id=?", (sid,))
    snap_gap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    req_g = request()
    req_g.is_disconnected = lambda: asyncio.sleep(0, result=False)
    resp_g = await _endpoint(routes, "/sessions/{sid}/view-events")(sid, req_g, after=snap_gap["resume_token"])
    ev_g, payload_g = _parse_sse_frame(await anext(resp_g.body_iterator))
    assert ev_g == "reset_required"
    assert payload_g["reason"] == "SOURCE_GAP"


@pytest.mark.asyncio
async def test_snapshot_preserves_pending_approvals_outside_window(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    lines = [
        json.dumps({"type": "approval", "receipt": "r0", "request_id": "req0", "method": "confirm", "text": "Approve me"})
    ]
    for i in range(6):
        lines.append(json.dumps({"type": "message", "receipt": f"r{i+1}", "item_id": f"m{i}", "text": f"msg {i}"}))
    raw = b"".join(l.encode() + b"\n" for l in lines)
    await sync(obj, sid, raw, 0)
    await obj.drain_projections(sid)

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request(), window_limit=2)
    keys = [it["key"] for it in snap["items"]]
    assert "r0:approval:req0" in keys
    assert snap["pending"]["count"] == 1
    assert snap["history"]["has_more"] is True
