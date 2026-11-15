from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import asyncio
import csv
import io
import json
from typing import Literal
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Query, Request
from fastapi.responses import Response, StreamingResponse
from shared.util import DevError
from hub import iam
from hub.operation_categories import operation_category_sql


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


def make_activity_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth

    @router.get("/api/operations")
    @database_endpoint(store)
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
    @database_endpoint(store)
    def operation(id: str, request: Request):
        return runtime.operation(id, auth.panel(request))

    @router.get("/api/audit")
    @database_endpoint(store)
    def audit(
        request: Request,
        limit: int = 40,
        offset: int = Query(default=0, le=2**63 - 1),
        q: str = "",
        status: str = "",
        source: str = "",
    ):
        principal = auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = audit_rows(store, principal, limit + 1, offset, q, status, source)
        return {
            "events": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
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
        def snapshot(item=None):
            # One current authority decision for each frame/idle heartbeat.
            # Only an invalidation marker, never old event data, crosses a
            # lost-membership/session boundary.
            with iam.read_scope(store):
                principal = auth.panel(request)
                permissions = iam.project_permissions(store, principal)
                access = frozenset(
                    (project, action)
                    for project, actions in permissions.items()
                    for action in actions
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
                return access, visible, (principal.space_id, principal.user_id)

        access, _, owner = await store.run_read(snapshot)
        if (
            len(runtime.watchers) >= 100
            or sum(getattr(q, "_stream_owner", None) == owner for q in runtime.watchers) >= 4
        ):
            raise DevError("TOO_MANY_STREAMS", "实时连接过多", 429)
        q = asyncio.Queue(maxsize=100)
        q._stream_owner = owner
        runtime.watchers.add(q)

        async def stream():
            nonlocal access
            try:
                yield "event: ready\ndata: {}\n\n"
                while not runtime.stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        item = None
                    try:
                        current_access, visible, current_owner = await store.run_read(
                            snapshot, item
                        )
                    except DevError:
                        yield "event: access_revoked\ndata: {}\n\n"
                        break
                    if current_owner != owner or not access <= current_access:
                        yield "event: access_revoked\ndata: {}\n\n"
                        break
                    access = current_access
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
