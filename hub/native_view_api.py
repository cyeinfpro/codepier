"""Version 2 native view HTTP & SSE endpoints."""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import time
from typing import Any, Mapping

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from hub.db_worker import run_db, run_db_read
from hub.native_projection import (
    DEFAULT_BODY_CHUNK_BYTES,
    DEFAULT_BODY_INLINE_BYTES,
    DEFAULT_HISTORY_BYTE_BUDGET,
    DEFAULT_HISTORY_ROW_BUDGET,
    DEFAULT_OUTPUT_TAIL_BYTES,
    DEFAULT_SCAN_MAX_BYTES,
    DEFAULT_SCAN_MAX_LINES,
    DEFAULT_WINDOW_LIMIT,
    PROJECTION_SCHEMA_VERSION,
    make_resume_token,
    parse_history_token,
    parse_resume_token,
    read_head,
    read_history_page,
    read_item_body_chunk,
    read_item_detail,
    read_snapshot,
    read_tombstone,
    read_tool_output_view,
)
from shared.native_cli import database
from shared.native_contracts import strict_safe_int
from shared.native_flags import resolve_native_flags
from shared.util import DevError

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
}
DEFAULT_CATCHUP_MAX_BATCHES = 64
DEFAULT_CATCHUP_MAX_BYTES = 1048576
DEFAULT_CATCHUP_MAX_SECONDS = 5.0


def session_scope_key(principal, project: Mapping[str, Any]) -> str:
    """Derive a non-secret scope key for token binding and cache isolation."""
    raw = f"{principal.space_id}:{principal.user_id}:{project['id']}:{project['device_id']}".encode("utf-8")
    return "sc_" + hashlib.sha256(raw).hexdigest()[:24]


def _authorize_chat_session(auth, service, request: Request, sid: str):
    principal = auth.panel(request)
    project = service.session_project(sid, principal)
    scope_key = session_scope_key(principal, project)
    with closing(database(service.directory)) as db:
        srow = db.execute("SELECT mode, status FROM sessions WHERE id=?", (sid,)).fetchone()
        if not srow or srow["status"] == "deleted":
            raise DevError("CLI_NOT_FOUND", "未找到会话", 404)
        if srow["mode"] != "chat":
            raise DevError("CLI_TERMINAL_SESSION", "此记录属于旧终端历史，不能作为聊天对话回放；原记录仍保留", 409)
    return principal, project, scope_key


def _reset_payload(reason: str, sid: str, head, tomb) -> dict[str, Any]:
    return {
        "reason": reason,
        "session_id": sid,
        "stream_epoch": head.watermark.epoch if head else (tomb["epoch"] if tomb else ""),
        "retained_from_revision": head.retained_from_revision if head else 0,
        "retained_from_cursor": 0,
        "applied_cursor": head.watermark.applied_cursor if head else 0,
        "target_revision": head.watermark.revision if head else 0,
        "blocked_cursor": head.blocked_cursor if head else None,
    }


def _head_reset_reason(head, tomb, expected_epoch: str, seen_rev: int) -> str | None:
    if tomb and tomb["deleted"]:
        return "EPOCH_CHANGED"
    if head is None or head.source_state == "building":
        return "PROJECTION_BUILDING"
    if head.watermark.epoch != expected_epoch:
        return "EPOCH_CHANGED"
    if head.source_state == "source_gap":
        return "SOURCE_GAP"
    if head.source_state in {"source_corrupt", "blocked_control"}:
        return "SOURCE_CORRUPT"
    if seen_rev < head.retained_from_revision or seen_rev > head.watermark.revision:
        return "REPLAY_WINDOW_EXPIRED"
    return None


def _encode_batch_for_scope(db, row, *, sid: str, scope_key: str) -> tuple[dict[str, Any], int]:
    payload = json.loads(row["payload_json"])
    payload["scope_key"] = scope_key
    payload["payload_digest"] = row["payload_digest"]
    payload["resume_token"] = make_resume_token(
        db,
        session_id=sid,
        scope_key=scope_key,
        epoch=row["epoch"],
        revision=int(row["to_revision"]),
        applied_cursor=int(row["to_cursor"]),
    )
    encoded = _sse_frame("batch", payload, event_id=str(payload["to_revision"]))
    return payload, len(encoded.encode("utf-8"))


def _load_catchup_step(
    db,
    sid: str,
    *,
    scope_key: str,
    expected_epoch: str,
    seen_rev: int,
    budget: dict[str, Any],
) -> tuple[str, Any]:
    head = read_head(db, sid)
    tomb = read_tombstone(db, sid)
    reason = _head_reset_reason(head, tomb, expected_epoch, seen_rev)
    if reason is not None:
        return "reset", _reset_payload(reason, sid, head, tomb)
    if seen_rev == head.watermark.revision:
        tok = make_resume_token(
            db,
            session_id=sid,
            scope_key=scope_key,
            epoch=head.watermark.epoch,
            revision=head.watermark.revision,
            applied_cursor=head.watermark.applied_cursor,
        )
        return "caught_up", {
            "stream_epoch": head.watermark.epoch,
            "projection_revision": head.watermark.revision,
            "applied_cursor": head.watermark.applied_cursor,
            "resume_token": tok,
        }
    rows = db.execute(
        "SELECT * FROM native_projection_batches WHERE session_id=? AND epoch=? AND to_revision>? ORDER BY to_revision ASC LIMIT 32",
        (sid, expected_epoch, seen_rev),
    ).fetchall()
    if not rows or int(rows[0]["from_revision"]) != seen_rev:
        return "reset", _reset_payload("REPLAY_WINDOW_EXPIRED", sid, head, tomb)
    return _collect_bounded_batches(db, sid, rows, head, tomb, scope_key=scope_key, budget=budget)


def _collect_bounded_batches(
    db,
    sid: str,
    rows: list,
    head,
    tomb,
    *,
    scope_key: str,
    budget: dict[str, Any],
) -> tuple[str, Any]:
    batches: list[dict[str, Any]] = []
    for row in rows:
        if budget["batches"] >= budget["max_batches"] or budget["bytes"] >= budget["max_bytes"]:
            return "reset", _reset_payload("BUDGET_EXCEEDED", sid, head, tomb)
        if time.monotonic() - budget["started"] > budget["max_seconds"]:
            return "reset", _reset_payload("BUDGET_EXCEEDED", sid, head, tomb)
        payload, enc_bytes = _encode_batch_for_scope(db, row, sid=sid, scope_key=scope_key)
        if budget["bytes"] + enc_bytes > budget["max_bytes"]:
            if not batches:
                return "reset", _reset_payload("BUDGET_EXCEEDED", sid, head, tomb)
            break
        batches.append(payload)
        budget["batches"] += 1
        budget["bytes"] += enc_bytes
    return "batches", batches


async def _wait_service_change(service, seen_service_rev: int) -> bool:
    async with service.changed:
        if service.revision != seen_service_rev:
            return False
        try:
            await asyncio.wait_for(service.changed.wait(), 10)
            return False
        except asyncio.TimeoutError:
            return True


def _sse_frame(event: str, data: Mapping[str, Any], *, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _view_events_stream(
    auth,
    runtime,
    service,
    request: Request,
    sid: str,
    *,
    expected_epoch: str,
    start_rev: int,
    budget: dict[str, Any],
):
    def read_step(seen_rev: int, start_live_burst: bool):
        _, _, scope_key = _authorize_chat_session(auth, service, request, sid)
        svc_rev = service.revision
        # A synchronized idle stream is not an actively replaying backlog.
        if start_live_burst:
            budget.update(batches=0, bytes=0, started=time.monotonic())
        with closing(database(service.directory)) as db:
            kind, payload = _load_catchup_step(
                db, sid, scope_key=scope_key, expected_epoch=expected_epoch, seen_rev=seen_rev, budget=budget
            )
        return svc_rev, kind, payload

    async def stream():
        seen_rev = start_rev
        synced_rev = -1
        while not await request.is_disconnected():
            try:
                svc_rev, kind, payload = await run_db(
                    runtime.store, read_step, seen_rev, synced_rev == seen_rev
                )
                if kind == "reset":
                    yield _sse_frame("reset_required", payload)
                    return
                if kind == "batches":
                    for batch in payload:
                        await run_db(runtime.store, _authorize_chat_session, auth, service, request, sid)
                        yield _sse_frame("batch", batch, event_id=str(batch["to_revision"]))
                        seen_rev = int(batch["to_revision"])
                    await asyncio.sleep(0)
                    continue
                if synced_rev != seen_rev:
                    yield _sse_frame("synchronized", payload)
                    synced_rev = seen_rev
                if await _wait_service_change(service, svc_rev):
                    yield ": keepalive\n\n"
            except DevError as exc:
                yield _sse_frame("error", {"code": exc.code, "message": str(exc)})
                return

    return stream()


def _should_emit_output(frame: dict[str, Any], cursor: int | None) -> bool:
    return cursor is None or frame["kind"] == "replace" or frame["to_offset"] > cursor


def _tool_output_stream(
    auth,
    runtime,
    service,
    request: Request,
    sid: str,
    key: str,
    *,
    after_offset: int | None,
    tail_bytes: int,
):
    def read_step(cursor: int | None):
        _authorize_chat_session(auth, service, request, sid)
        svc_rev = service.revision
        with closing(database(service.directory)) as db:
            frame = read_tool_output_view(db, sid, key, after_offset=cursor, tail_bytes=tail_bytes)
        return svc_rev, frame

    async def stream():
        cursor = after_offset
        while not await request.is_disconnected():
            try:
                svc_rev, frame = await run_db(runtime.store, read_step, cursor)
                emitted = _should_emit_output(frame, cursor)
                if emitted:
                    yield _sse_frame("output", frame, event_id=str(frame["to_offset"]))
                    cursor = int(frame["to_offset"])
                if not frame["running"]:
                    if not emitted:
                        yield _sse_frame("output", frame, event_id=str(frame["to_offset"]))
                    return
                if frame["to_offset"] < frame["total_bytes"]:
                    await asyncio.sleep(0)
                elif await _wait_service_change(service, svc_rev):
                    yield ": keepalive\n\n"
            except DevError as exc:
                yield _sse_frame("error", {"code": exc.code, "message": str(exc)})
                return

    return stream()


def _register_core_view_routes(router: APIRouter, auth, runtime, service) -> None:
    @router.get("/capabilities")
    async def capabilities(request: Request):
        await run_db_read(runtime.store, auth.panel, request)
        flags = resolve_native_flags(getattr(service, "native_flags", None))
        return {
            "native_view_v2": True,
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "flags": flags.as_dict(),
            "budgets": {
                "window_limit": DEFAULT_WINDOW_LIMIT,
                "body_inline_bytes": DEFAULT_BODY_INLINE_BYTES,
                "body_chunk_bytes": DEFAULT_BODY_CHUNK_BYTES,
                "history_row_budget": DEFAULT_HISTORY_ROW_BUDGET,
                "history_byte_budget": DEFAULT_HISTORY_BYTE_BUDGET,
                "output_tail_bytes": DEFAULT_OUTPUT_TAIL_BYTES,
                "scan_max_bytes": DEFAULT_SCAN_MAX_BYTES,
                "scan_max_lines": DEFAULT_SCAN_MAX_LINES,
            },
        }

    @router.get("/sessions/{sid}/view")
    async def view_snapshot(
        sid: str,
        request: Request,
        window_limit: int = DEFAULT_WINDOW_LIMIT,
        body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES,
    ):
        def load():
            _, _, scope_key = _authorize_chat_session(auth, service, request, sid)
            with closing(database(service.directory)) as db:
                return read_snapshot(db, sid, scope_key=scope_key, window_limit=window_limit, body_inline_bytes=body_inline_bytes)

        return await run_db(runtime.store, load)

    @router.get("/sessions/{sid}/view-events")
    async def view_events(
        sid: str,
        request: Request,
        after: str = "",
        max_batches: int = DEFAULT_CATCHUP_MAX_BATCHES,
        max_bytes: int = DEFAULT_CATCHUP_MAX_BYTES,
        max_seconds: float = DEFAULT_CATCHUP_MAX_SECONDS,
    ):
        def init_stream():
            _, _, scope_key = _authorize_chat_session(auth, service, request, sid)
            if not after:
                raise DevError("CLI_INVALID", "缺少恢复令牌", 400)
            with closing(database(service.directory)) as db:
                wm = parse_resume_token(db, after, session_id=sid, scope_key=scope_key)
            return wm

        wm = await run_db(runtime.store, init_stream)
        budget = {
            "batches": 0,
            "bytes": 0,
            "started": time.monotonic(),
            "max_batches": max(1, min(max_batches, 512)),
            "max_bytes": max(1024, min(max_bytes, 8 * 1024 * 1024)),
            "max_seconds": max(0.05, min(max_seconds, 30.0)),
        }
        return StreamingResponse(
            _view_events_stream(auth, runtime, service, request, sid, expected_epoch=wm.epoch, start_rev=wm.revision, budget=budget),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )


def _register_history_and_item_routes(router: APIRouter, auth, runtime, service) -> None:
    @router.get("/sessions/{sid}/history")
    async def history_page(
        sid: str,
        request: Request,
        before: str,
        row_budget: int = DEFAULT_HISTORY_ROW_BUDGET,
        byte_budget: int = DEFAULT_HISTORY_BYTE_BUDGET,
    ):
        def load():
            _, _, scope_key = _authorize_chat_session(auth, service, request, sid)
            with closing(database(service.directory)) as db:
                epoch, before_cursor, before_key = parse_history_token(db, before, session_id=sid, scope_key=scope_key)
                return read_history_page(db, sid, epoch=epoch, before_cursor=before_cursor, before_key=before_key, row_budget=row_budget, byte_budget=byte_budget, scope_key=scope_key)

        return await run_db(runtime.store, load)

    @router.get("/sessions/{sid}/items/{key}")
    async def item_detail(sid: str, key: str, request: Request):
        def load():
            _authorize_chat_session(auth, service, request, sid)
            with closing(database(service.directory)) as db:
                return read_item_detail(db, sid, key)

        return await run_db(runtime.store, load)


def _register_item_stream_routes(router: APIRouter, auth, runtime, service) -> None:
    @router.get("/sessions/{sid}/items/{key}/body")
    async def item_body(
        sid: str,
        key: str,
        request: Request,
        generation: int,
        start_byte: int = 0,
        max_bytes: int = DEFAULT_BODY_CHUNK_BYTES,
    ):
        try:
            strict_safe_int(generation, minimum=1)
            strict_safe_int(start_byte)
            strict_safe_int(max_bytes, minimum=1)
        except ValueError as exc:
            raise DevError("CLI_INVALID", "正文分页参数无效", 400) from exc

        def load():
            _authorize_chat_session(auth, service, request, sid)
            with closing(database(service.directory)) as db:
                return read_item_body_chunk(db, sid, key, generation=generation, start_byte=start_byte, max_bytes=max_bytes)

        return await run_db(runtime.store, load)

    @router.get("/sessions/{sid}/items/{key}/output")
    async def item_output(
        sid: str,
        key: str,
        request: Request,
        after_offset: int | None = None,
        tail_bytes: int = DEFAULT_OUTPUT_TAIL_BYTES,
        stream: bool = False,
    ):
        if after_offset is not None:
            try:
                strict_safe_int(after_offset)
            except ValueError as exc:
                raise DevError("CLI_OFFSET", "工具输出偏移无效", 400) from exc
        wants_sse = stream or "text/event-stream" in request.headers.get("accept", "").lower()
        if not wants_sse:
            def load():
                _authorize_chat_session(auth, service, request, sid)
                with closing(database(service.directory)) as db:
                    return read_tool_output_view(db, sid, key, after_offset=after_offset, tail_bytes=tail_bytes)

            return await run_db(runtime.store, load)
        await run_db(runtime.store, _authorize_chat_session, auth, service, request, sid)
        return StreamingResponse(
            _tool_output_stream(auth, runtime, service, request, sid, key, after_offset=after_offset, tail_bytes=tail_bytes),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )


def make_native_view_router(auth, runtime, *, prefix: str = "/api/native") -> APIRouter:
    """Create the v2 native projection HTTP/SSE router."""
    router = APIRouter(prefix=prefix)
    service = runtime.native
    _register_core_view_routes(router, auth, runtime, service)
    _register_history_and_item_routes(router, auth, runtime, service)
    _register_item_stream_routes(router, auth, runtime, service)
    return router


def build_native_router(auth, runtime) -> APIRouter:
    """Create the combined /api/native router with v2 view endpoints mounted."""
    router = APIRouter(prefix="/api/native")
    service = runtime.native
    _register_core_view_routes(router, auth, runtime, service)
    _register_history_and_item_routes(router, auth, runtime, service)
    _register_item_stream_routes(router, auth, runtime, service)
    return router
