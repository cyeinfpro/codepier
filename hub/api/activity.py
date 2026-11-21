from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import asyncio
import csv
import io
import json
import time
from typing import Literal
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Query, Request
from fastapi.responses import Response, StreamingResponse
from shared.util import DevError
from hub import iam
from hub.operation_categories import operation_category_sql


# Upper bound for reusing one SSE authorization snapshot. session_revision only
# observes writes made by THIS process; an out-of-process writer (for example
# `python -m hub reset-password`, which revokes every session) is noticed only by
# a fresh snapshot. Keep this equal to the idle heartbeat so a busy stream never
# extends the pre-existing revocation latency.
SSE_FAST_AUTH_WINDOW_S = 15.0


def _session_valid_until(store, request: Request, principal) -> float:
    from shared.crypto import digest

    now = time.time()
    limit = now + SSE_FAST_AUTH_WINDOW_S
    cookie = request.cookies.get("rd_session", "")
    if cookie:
        sess = store.one("SELECT expires FROM sessions WHERE id_hash=?", (digest(cookie),))
        if sess and isinstance(sess.get("expires"), (int, float)):
            limit = min(limit, float(sess["expires"]))
    identity_id = getattr(principal, "identity_id", None)
    if identity_id:
        ident = store.one("SELECT fresh_until FROM external_identities WHERE id=?", (identity_id,))
        if ident and isinstance(ident.get("fresh_until"), (int, float)):
            limit = min(limit, float(ident["fresh_until"]))
    for table in ("memberships", "role_assignments"):
        exp = store.one(
            f"SELECT MIN(expires) AS exp FROM {table} WHERE user_id=? AND space_id=? AND active=1 AND expires IS NOT NULL",
            (principal.user_id, principal.space_id),
        )
        if exp and isinstance(exp.get("exp"), (int, float)):
            limit = min(limit, float(exp["exp"]))
    return limit


def _fast_operation_visible(aud: dict, cached: dict) -> bool:
    principal = cached["principal"]
    if aud.get("space_id") != principal.space_id:
        return False
    record_ok = (
        principal.instance_admin
        or aud.get("owner_user_id") == principal.user_id
        or aud.get("visibility") == "space"
    )
    if not record_ok:
        return False
    proj = aud.get("project_id")
    if proj:
        # Mirrors require_record (read) followed by authorize(scope).
        actions = cached["permissions"].get(proj, ())
        if "read" not in actions or aud.get("scope", "read") not in actions:
            return False
    return not (aud.get("needs_computer") and "computer" not in getattr(principal, "scopes", ()))


def _fast_event_decision(item, cached: dict) -> tuple[bool, bool]:
    if item is None:
        # An idle heartbeat is the revocation probe for quiet streams: always
        # take a fresh snapshot, exactly as before the fast path existed.
        return False, False
    kind = item.get("type")
    if kind == "resync":
        return True, True
    principal = cached["principal"]
    if kind == "computer_approval":
        aud = item.get("_audience") or {}
        vis = aud.get("space_id") == principal.space_id and (
            aud.get("user_id") == principal.user_id or principal.instance_admin
        )
        return True, vis
    aud = (
        item.get("_audience")
        if kind in {"operation", "operation_event", "output", "trace"}
        else None
    )
    if isinstance(aud, dict) and aud.get("fast_auth") is True:
        return True, _fast_operation_visible(aud, cached)
    return False, False


def _sse_snapshot(store, runtime, auth, request: Request, item=None):
    with iam.read_scope(store):
        principal = auth.panel(request)
        permissions = iam.project_permissions(store, principal)
        access = frozenset(
            (project, action) for project, actions in permissions.items() for action in actions
        )
        access |= frozenset(
            ("@", flag)
            for flag, allowed in [
                ("space_admin", principal.admin),
                ("instance_admin", principal.instance_admin),
            ]
            if allowed
        )
        visible = item is not None and iam.event_visible(runtime, principal, item)
        state = {
            "access": access,
            "owner": (principal.space_id, principal.user_id),
            "principal": principal,
            "permissions": {k: frozenset(v) for k, v in permissions.items()},
            "revision": getattr(store, "session_revision", None),
            "valid_until": _session_valid_until(store, request, principal)
            if iam.installed(store)
            else 0.0,
        }
        return state, visible


async def _evaluate_stream_item(store, runtime, auth, request: Request, item, state: dict):
    current_rev = getattr(store, "session_revision", None)
    if (
        iam.installed(store)
        and current_rev is not None
        and current_rev == state["revision"]
        and time.time() < state["valid_until"]
    ):
        handled, visible = _fast_event_decision(item, state)
        if handled:
            return state, visible, False
    try:
        next_state, visible = await store.run_read(
            _sse_snapshot, store, runtime, auth, request, item
        )
    except DevError:
        return state, False, True
    revoked = next_state["owner"] != state["owner"] or not state["access"] <= next_state["access"]
    return next_state, visible, revoked


def operation_rows(
    store,
    principal,
    limit=40,
    offset=0,
    status="",
    project="",
    source="",
    category="all",
    *,
    count_only=False,
):
    clause, args = iam.private_sql(principal, "o.")
    category_clause, category_args = operation_category_sql(category, "o.")
    where = [clause, category_clause]
    args.extend(category_args)
    if not principal.admin:
        where.append(
            "(o.project_id IS NULL OR o.project_id IN (%s))"
            % (",".join("?" for _ in principal.projects) or "NULL")
        )
        args.extend(principal.projects)
    if status:
        where.append("o.state=?")
        args.append(status)
    if project:
        where.append("o.project_id=?")
        args.append(project)
    if source in {"mcp", "panel"}:
        where.append("o.actor LIKE ?")
        args.append(source + ":%")
    if count_only:
        return store.one(
            "SELECT count(*) AS n FROM operations o WHERE " + " AND ".join(where), args
        )["n"]
    sql = "SELECT o.id,o.device_id,o.project_id,o.actor,o.tool,o.state,o.created,o.updated,o.error,o.attempts,o.accepted_at,o.deadline,o.cancel_requested,o.transport_error,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return store.all(
        sql + " ORDER BY o.created DESC,o.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)
    )


def audit_rows(
    store, principal, limit=40, offset=0, query="", status="", source="", snapshot_id=0, before_id=0
):
    where, args = ["space_id=?"], [principal.space_id]
    if not principal.admin:
        where.append("owner_user_id=?")
        args.append(principal.user_id)
    if query:
        where.append("(actor LIKE ? OR action LIKE ? OR target LIKE ?)")
        args += ["%" + query[:100] + "%"] * 3
    if status:
        where.append("status=?")
        args.append(status)
    if source in {"mcp", "panel", "device"}:
        where.append("actor LIKE ?")
        args.append(source + ":%")
    if snapshot_id:
        where.append("id<=?")
        args.append(snapshot_id)
    if before_id:
        where.append("id<?")
        args.append(before_id)
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "")
    rows = store.all(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return rows


def _activity_queue():
    from hub.runtime import CoalescingQueue

    return CoalescingQueue(maxsize=100)


def make_activity_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth

    @router.get("/api/operations")
    @database_endpoint(store, read_only=True)
    def operations(
        request: Request,
        limit: int = 40,
        offset: int = Query(default=0, le=2**63 - 1),
        status: str = "",
        project: str = "",
        source: str = "",
        category: Literal["all", "tools", "project_map"] = "all",
    ):
        principal = auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        with store.transaction(immediate=False):
            rows = operation_rows(
                store, principal, limit + 1, offset, status, project, source, category
            )
            total = operation_rows(
                store,
                principal,
                status=status,
                project=project,
                source=source,
                category=category,
                count_only=True,
            )
        return {
            "operations": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
            "total": total,
            "category": category,
        }

    # Exact queue route must precede the operation-id route.
    from hub.queue_panel import register_queue_routes

    register_queue_routes(router, store, runtime, auth)

    @router.get("/api/operations/{id}")
    @database_endpoint(store, read_only=True)
    def operation(id: str, request: Request):
        return runtime.operation(id, auth.panel(request))

    @router.get("/api/audit")
    @database_endpoint(store, read_only=True)
    def audit(
        request: Request,
        limit: int = 40,
        offset: int = Query(default=0, le=2**63 - 1),
        before_id: int = Query(default=0, ge=0, le=2**63 - 1),
        q: str = "",
        status: str = "",
        source: str = "",
    ):
        principal = auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = audit_rows(
            store, principal, limit + 1, offset, q, status, source, before_id=before_id
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        return {
            "events": visible,
            "next_offset": offset + limit if has_more else None,
            "next_before_id": visible[-1]["id"] if has_more and visible else None,
        }

    @router.get("/api/audit-export")
    @database_endpoint(store)
    def audit_export(
        request: Request,
        q: str = "",
        status: str = "",
        source: str = "",
        offset: int = Query(default=0, le=2**63 - 1),
        snapshot_id: int = Query(default=0, ge=0, le=2**63 - 1),
        before_id: int = Query(default=0, ge=0, le=2**63 - 1),
    ):
        principal = auth.panel(request)
        offset = max(0, offset)
        rows = audit_rows(
            store, principal, 10001, offset, q, status, source, snapshot_id, before_id
        )
        snapshot_id = snapshot_id or (rows[0]["id"] if rows else 0)
        truncated = len(rows) > 10000
        rows = rows[:10000]
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "utc_time", "actor", "action", "target", "status", "detail"])

        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

        for r in rows:
            writer.writerow(
                [
                    r["id"],
                    datetime.fromtimestamp(r["at"], ZoneInfo("UTC")).isoformat(),
                    *[cell(r[k]) for k in ("actor", "action", "target", "status")],
                    cell(json.dumps(r["detail"], ensure_ascii=False)),
                ]
            )
        store.audit(
            principal.actor,
            "audit.exported",
            detail={"rows": len(rows), "offset": offset, "limit": 10000},
        )
        return Response(
            "\ufeff" + out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="codepier-audit.csv"',
                "X-Export-Limit": "10000",
                "X-Export-Truncated": str(truncated).lower(),
                "X-Export-Next-Offset": str(offset + 10000) if truncated else "",
                "X-Export-Rows": str(len(rows)),
                "X-Export-Snapshot-ID": str(snapshot_id),
                "X-Export-Next-Before-ID": str(rows[-1]["id"]) if truncated else "",
            },
        )

    @router.get("/api/events")
    async def events(request: Request):
        state, _ = await store.run_read(_sse_snapshot, store, runtime, auth, request)
        owner = state["owner"]
        if (
            len(runtime.watchers) >= 100
            or sum(getattr(q, "_stream_owner", None) == owner for q in runtime.watchers) >= 4
        ):
            raise DevError("TOO_MANY_STREAMS", "实时连接过多", 429)
        q = _activity_queue()
        q._stream_owner = owner
        runtime.watchers.add(q)

        async def stream():
            nonlocal state
            try:
                yield "event: ready\ndata: {}\n\n"
                while not runtime.stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        item = None
                    state, visible, revoked = await _evaluate_stream_item(
                        store, runtime, auth, request, item, state
                    )
                    if revoked:
                        yield "event: access_revoked\ndata: {}\n\n"
                        break
                    if item is None:
                        yield ": heartbeat\n\n"
                    elif item.get("type") == "resync":
                        yield "event: resync\ndata: {}\n\n"
                    elif visible:
                        yield (
                            "data: "
                            + json.dumps(
                                {k: v for k, v in item.items() if k != "_audience"},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
            finally:
                runtime.watchers.discard(q)

        class EventStream(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    runtime.watchers.discard(q)

        return EventStream(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
