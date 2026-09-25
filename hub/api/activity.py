from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import asyncio
import csv
import io
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Query, Request
from fastapi.responses import Response, StreamingResponse
from shared.util import DevError

def operation_rows(store, limit=40, offset=0, status="", project="", source=""):
    where, args = [], []
    if status:
        where.append("o.state=?")
        args.append(status)
    if project:
        where.append("o.project_id=?")
        args.append(project)
    if source in {"mcp", "panel"}:
        where.append("o.actor LIKE ?")
        args.append(source + ":%")
    sql = "SELECT o.id,o.device_id,o.project_id,o.actor,o.tool,o.state,o.created,o.updated,o.error,o.attempts,o.accepted_at,o.deadline,o.cancel_requested,o.transport_error,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return store.all(sql + " ORDER BY o.created DESC LIMIT ? OFFSET ?", (*args, limit, offset))


def audit_rows(store, limit=40, offset=0, query="", status="", source=""):
    where, args = [], []
    if query:
        where.append("(actor LIKE ? OR action LIKE ? OR target LIKE ?)")
        args += ["%" + query[:100] + "%"] * 3
    if status:
        where.append("status=?")
        args.append(status)
    if source in {"mcp", "panel", "device"}:
        where.append("actor LIKE ?")
        args.append(source + ":%")
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "")
    rows = store.all(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return rows



def make_activity_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/operations")
    @database_endpoint(store)
    def operations(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), status: str = "", project: str = "", source: str = ""):
        auth.admin(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = operation_rows(store, limit + 1, offset, status, project, source)
        return {"operations": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/operations/{id}")
    @database_endpoint(store)
    def operation(id: str, request: Request):
        return runtime.operation(id, auth.admin(request))

    @router.get("/api/audit")
    @database_endpoint(store)
    def audit(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), q: str = "", status: str = "", source: str = ""):
        auth.admin(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = audit_rows(store, limit + 1, offset, q, status, source)
        return {"events": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/audit-export")
    @database_endpoint(store)
    def audit_export(request: Request, q: str = "", status: str = "", source: str = "", offset: int = Query(default=0, le=2**63 - 1)):
        principal = auth.admin(request)
        rows = audit_rows(store, 10000, max(0, offset), q, status, source)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "utc_time", "actor", "action", "target", "status", "detail"])
        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        for r in rows:
            writer.writerow([r["id"], datetime.fromtimestamp(r["at"], ZoneInfo("UTC")).isoformat(), *[cell(r[k]) for k in ("actor", "action", "target", "status")], cell(json.dumps(r["detail"], ensure_ascii=False))])
        store.audit(principal.actor, "audit.exported", detail={"rows": len(rows), "offset": offset, "limit": 10000})
        return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="codepier-audit.csv"', "X-Export-Limit": "10000"})

    @router.get("/api/events")
    async def events(request: Request):
        def session_snapshot():
            with store.lock:
                return auth.session(request), store.session_revision
        session, revision = await store.run(session_snapshot)
        if len(runtime.watchers) >= 100:
            raise DevError("TOO_MANY_STREAMS", "实时连接过多", 429)
        q = asyncio.Queue(maxsize=100)
        runtime.watchers.add(q)
        async def stream():
            nonlocal session, revision
            next_check = time.monotonic() + 15
            try:
                yield "event: ready\ndata: {}\n\n"
                while not runtime.stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        item = None
                    if time.time() >= session["expires"]:
                        break
                    # Session writes invalidate immediately in-process; a local
                    # CLI reset/independent writer is detected within 15 seconds.
                    if revision != store.session_revision or time.monotonic() >= next_check:
                        try:
                            session, revision = await store.run(session_snapshot)
                        except DevError:
                            break
                        next_check = time.monotonic() + 15
                    if item is None:
                        yield ": heartbeat\n\n"
                    else:
                        yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            finally:
                runtime.watchers.discard(q)
        class EventStream(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    runtime.watchers.discard(q)
        return EventStream(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
