"""Tests for keyset history paging, byte budget caps, immutable first_cursor, and item endpoints."""

from __future__ import annotations

from contextlib import closing
import json

import pytest

from hub.native_cli import make_native_router
from hub.native_projection import clear_or_delete_projection_tx, read_history_page
from shared.native_cli import database
from shared.util import DevError
from tests.test_chat_sse import TestAuth, request, service, sync


def _endpoint(routes, suffix: str):
    return next(r.endpoint for r in routes if r.path.endswith(suffix))


@pytest.mark.asyncio
async def test_keyset_history_paging_immutable_first_cursor_and_byte_budget(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    # Create 5 messages; message 0 starts as delta and later receives final replace_body
    events = [
        {"type": "delta", "receipt": "r0", "item_id": "m0", "text": "part-a "},
        {"type": "message", "receipt": "r1", "item_id": "m1", "text": "msg-1 " + ("x" * 100)},
        {"type": "message", "receipt": "r2", "item_id": "m2", "text": "msg-2 " + ("y" * 100)},
        {"type": "message", "receipt": "r3", "item_id": "m3", "text": "msg-3 " + ("z" * 100)},
        {"type": "message", "receipt": "r0", "item_id": "m0", "text": "part-a final-body"},
        {"type": "message", "receipt": "r4", "item_id": "m4", "text": "msg-4 latest"},
    ]
    raw = b"".join(json.dumps(e).encode() + b"\n" for e in events)
    await sync(obj, sid, raw, 0)
    await obj.drain_projections(sid)

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request(), window_limit=2)
    # Latest 2 items are r0:assistant:m0? Wait: m0 kept first_cursor=0! So latest 2 are m3 and m4!
    assert [it["key"] for it in snap["items"]] == ["r3:assistant:m3", "r4:assistant:m4"]
    assert snap["history"]["has_more"] is True

    # Page backwards via HTTP endpoint
    page1 = await _endpoint(routes, "/sessions/{sid}/history")(sid, request(), before=snap["history"]["before"], row_budget=2)
    assert [it["key"] for it in page1["items"]] == ["r1:assistant:m1", "r2:assistant:m2"]
    assert page1["has_more"] is True
    assert page1["page_revision"] == snap["projection_revision"]

    page2 = await _endpoint(routes, "/sessions/{sid}/history")(sid, request(), before=page1["before"], row_budget=2)
    assert [it["key"] for it in page2["items"]] == ["r0:assistant:m0"]
    assert page2["items"][0]["first_cursor"] == 0
    assert page2["items"][0]["body"]["text"] == "part-a final-body"
    assert page2["has_more"] is False
    assert page2["before"] is None

    # Byte budget cap forces smaller page even when row_budget is larger
    with closing(database(obj.directory)) as db:
        capped = read_history_page(
            db,
            sid,
            epoch=snap["stream_epoch"],
            before_cursor=snap["items"][0]["first_cursor"],
            before_key=snap["items"][0]["key"],
            row_budget=10,
            byte_budget=260,
        )
        assert len(capped["items"]) == 1
        assert capped["items"][0]["key"] == "r2:assistant:m2"
        assert capped["has_more"] is True


@pytest.mark.asyncio
async def test_history_rejects_stale_epoch_and_item_detail_fetches_single_item(service):
    obj, _, sid = service
    auth = TestAuth()
    routes = make_native_router(auth, obj.runtime).routes

    events = [
        {"type": "message", "receipt": "r1", "item_id": "m1", "text": "one"},
        {"type": "message", "receipt": "r2", "item_id": "m2", "text": "two"},
    ]
    raw = b"".join(json.dumps(e).encode() + b"\n" for e in events)
    await sync(obj, sid, raw, 0)
    await obj.drain_projections(sid)

    snap = await _endpoint(routes, "/sessions/{sid}/view")(sid, request(), window_limit=1)
    detail = await _endpoint(routes, "/sessions/{sid}/items/{key}")(sid, "r1:assistant:m1", request())
    assert detail["item"]["body"]["text"] == "one"

    with closing(database(obj.directory)) as db, db:
        clear_or_delete_projection_tx(db, sid, transition_id="tx-rot", deleted=False)

    with pytest.raises(DevError) as exc:
        await _endpoint(routes, "/sessions/{sid}/history")(sid, request(), before=snap["history"]["before"])
    assert exc.value.code == "EPOCH_CHANGED"
