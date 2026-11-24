"""Tests for bounded tool stdout externalization, tail/append output view, and live output SSE."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json

import pytest

from hub.native_cli import make_native_router
from hub.native_projection import read_snapshot, read_tool_output_view
from shared.native_cli import database
from tests.test_chat_sse import TestAuth, request, service, sync


def _endpoint(routes, suffix: str):
    return next(r.endpoint for r in routes if r.path.endswith(suffix))


def _parse_sse(raw: str) -> tuple[str, dict]:
    event = "message"
    data = {}
    for line in raw.strip().splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return event, data


@pytest.mark.asyncio
async def test_large_tool_stdout_externalized_tail_and_contiguous_append(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    chunk1 = "line-" + ("A" * 5000) + "\n"
    chunk2 = "tail-你好🚀-end\n"
    events = [
        {"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "started", "text": "{}"},
        {"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "delta", "text": chunk1},
        {"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "delta", "text": chunk2},
    ]
    raw = b"".join(json.dumps(e, ensure_ascii=False).encode() + b"\n" for e in events)
    await sync(obj, sid, raw, 0)
    await obj.drain_projections(sid)

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request())
    tool_item = snap["items"][0]
    assert tool_item["kind"] == "tool"
    assert "output_ref" in tool_item
    assert tool_item["output_ref"]["byte_length"] > 4096
    # Summary body does NOT embed the 5KB stdout
    assert tool_item["body"]["kind"] == "inline"
    assert "AAAA" not in tool_item["body"]["text"]

    # Tail open returns kind=replace with truncated_before=True
    out_ep = _endpoint(routes, "/sessions/{sid}/items/{key}/output")
    tail = await out_ep(sid, tool_item["key"], request(), tail_bytes=64)
    assert tail["kind"] == "replace"
    assert tail["truncated_before"] is True
    assert tail["running"] is True
    assert tail["text"].endswith(chunk2)

    # Append from current to_offset after new delta arrives
    chunk3 = "more-output\n"
    ev3 = json.dumps({"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "delta", "text": chunk3}).encode() + b"\n"
    await sync(obj, sid, ev3, len(raw))
    await obj.drain_projections(sid)

    appended = await out_ep(sid, tool_item["key"], request(), after_offset=tail["to_offset"], tail_bytes=1024)
    assert appended["kind"] == "append"
    assert appended["truncated_before"] is False
    assert appended["from_offset"] == tail["to_offset"]
    assert appended["text"] == chunk3
    assert appended["output_epoch"] == tail["output_epoch"]

    # Out-of-range offset returns replace instead of broken append
    stale = await out_ep(sid, tool_item["key"], request(), after_offset=9999999, tail_bytes=64)
    assert stale["kind"] == "replace"
    assert stale["truncated_before"] is True


@pytest.mark.asyncio
async def test_tool_output_sse_stream_completes_and_checks_auth(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    ev1 = json.dumps({"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "delta", "text": "step1\n"}).encode() + b"\n"
    await sync(obj, sid, ev1, 0)
    await obj.drain_projections(sid)

    req = request()
    req.is_disconnected = lambda: asyncio.sleep(0, result=False)
    out_ep = _endpoint(routes, "/sessions/{sid}/items/{key}/output")
    resp = await out_ep(sid, "r1:tool:t1", req, stream=True)
    it = resp.body_iterator

    kind, first = _parse_sse(await anext(it))
    assert kind == "output"
    assert first["kind"] == "replace"
    assert first["text"] == "step1\n"
    assert first["running"] is True

    # Complete tool with exit_code
    ev2 = json.dumps({"type": "tool", "receipt": "r1", "tool_id": "t1", "name": "bash", "status": "completed", "exit_code": 0}).encode() + b"\n"
    await sync(obj, sid, ev2, len(ev1))
    await obj.drain_projections(sid)

    kind2, final = _parse_sse(await anext(it))
    assert kind2 == "output"
    assert final["running"] is False
    with pytest.raises(StopAsyncIteration):
        await anext(it)

    with closing(database(obj.directory)) as db:
        snap = read_snapshot(db, sid, scope_key="sc_1")
        assert snap["items"][0]["exit_code"] == 0
        assert snap["items"][0]["status"] == "completed"
