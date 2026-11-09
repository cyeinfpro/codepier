from __future__ import annotations
from fastapi import APIRouter, Query
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import sqlite3
import time
from datetime import datetime
from fastapi import Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from shared.core_contracts import CORE_TOOLS
from shared.contracts import tool_definitions
from hub import iam
from shared.util import VERSION
from shared.token_cost import DEFAULT_CACHE_READ_PERCENT
from hub.api.models import ComputerDecision, ToolCall
from hub.api.devices import device_rows
from hub.api.activity import operation_rows, audit_rows
from hub.token_dashboard import Period, token_dashboard_async, overview_token_snapshot, validate_dashboard_snapshot



def make_system_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/healthz")
    @database_endpoint(store)
    def health():
        try:
            ready = bool(not runtime.stopping and runtime.worker and not runtime.worker.done() and store.one("SELECT 1 AS ok"))
        except sqlite3.Error:
            ready = False
        if not ready:
            return JSONResponse({"status": "unavailable", "version": VERSION}, status_code=503)
        return {"status": "ok", "version": VERSION, "panel_update": maintenance.status(),
                "diagnostics": {"write_errors": runtime.diagnostics.errors,
                                "activity_write_errors": runtime.integrations.write_errors}}

    def overview_data(request, token_usage, token_snapshot):
        principal = auth.panel(request)
        if token_snapshot is not None:
            validate_dashboard_snapshot(runtime, principal, token_snapshot)
        tz = config.timezone
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        devices = device_rows(store, runtime, principal)
        projects = runtime.list_projects(principal)
        clause, args = iam.private_sql(principal)
        if not principal.admin:
            clause += ' AND (project_id IS NULL OR project_id IN (%s))' % (','.join('?' for _ in principal.projects) or 'NULL')
            args.extend(principal.projects)
        counts = {r['state']:r['n'] for r in store.all('SELECT state,count(*) AS n FROM operations WHERE '+clause+' AND created>=? GROUP BY state', (*args,today))}
        now = time.time()
        active_grants = store.one(
            """SELECT count(*) AS n FROM grants g
               LEFT JOIN grant_expirations e ON e.grant_id=g.id
               WHERE g.user_id=? AND g.space_id=? AND g.revoked=0 AND g.client_id IS NOT NULL
                 AND (e.expires IS NULL OR e.expires>?)
                 AND EXISTS (SELECT 1 FROM tokens t WHERE t.grant_id=g.id
                             AND t.kind IN ('access','refresh') AND t.expires>?)""",
            (principal.user_id, principal.space_id, now, now),
        )["n"]
        return {"devices": devices, "projects": projects, "online_devices": sum(1 for d in devices if d["online"]),
            "active_grants": active_grants, "today_operations": sum(counts.values()), "today_succeeded": counts.get("succeeded", 0), "today_failed": counts.get("failed", 0),
            "active_operations": store.one("SELECT count(*) AS n FROM operations WHERE "+clause+" AND state IN ('running','queued','reconnecting','cancelling')",args)["n"],
            "recent_operations": operation_rows(store, principal, limit=8), "recent_audit": audit_rows(store, principal, limit=6),
            "token_usage": token_usage,
            "mcp_url": public_url() + "/mcp", "role_mcp_url": public_url() + "/mcp?authorization=role", "version": VERSION, "tool_count": len(tool_definitions()), "native_core_tool_count": len(CORE_TOOLS), "timezone": str(tz)}

    def overview_snapshot(request, token_usage, token_snapshot):
        with store.transaction(immediate=False), iam.read_scope(store):
            return overview_data(request, token_usage, token_snapshot)

    @router.get("/api/overview")
    async def overview(request: Request, include_token_usage: bool = True):
        # The panel may fetch live status independently. The default retains the
        # established combined response without holding the DB worker while
        # parsing metrics or replaying retained context scenarios.
        token_usage, token_snapshot = None, None
        if include_token_usage:
            principal = await store.run(auth.panel, request)
            token_usage, token_snapshot = await overview_token_snapshot(runtime, principal, config.timezone)
        return await store.run(overview_snapshot, request, token_usage, token_snapshot)

    @router.get("/api/token-usage")
    async def token_usage(request: Request, period: Period = "today",
                    project: str = Query(default="", max_length=128),
                    connection: str = Query(default="", max_length=128),
                    session: str = Query(default="", max_length=128),
                    cache_read_percent: int = Query(default=DEFAULT_CACHE_READ_PERCENT, ge=0, le=100),
                    reference_model: str = Query(default="gpt-6-astra", max_length=64)):
        principal = await store.run(auth.panel, request)
        return await token_dashboard_async(runtime, principal, config.timezone,
                                          period=period, project=project, connection=connection, session=session,
                                          cache_read_percent=cache_read_percent, reference_model=reference_model)

    @router.post("/api/tools/call")
    async def call_tool(request: Request, body: ToolCall):
        principal = await store.run(auth.panel, request, True)
        return await runtime.invoke(body.tool, body.arguments, principal)

    @router.get("/api/computer/approvals")
    async def computer_approvals(request: Request):
        principal = await store.run(auth.panel, request)
        return {"approvals": await store.run(runtime.computer_approvals.list, principal)}

    @router.post("/api/computer/approvals/{identifier}")
    async def computer_decision(identifier: str, request: Request, body: ComputerDecision):
        principal = await store.run(auth.panel, request, True)
        return await runtime.computer_approvals.decide(identifier, body.action, principal)

    @router.websocket("/agent/ws/{device_id}")
    async def agent_ws(websocket: WebSocket, device_id: str):
        await runtime.agent_socket(websocket, device_id)

    @router.get("/")
    async def index():
        return FileResponse(BASE / "web" / "index.html", headers={"Cache-Control": "no-cache"})


    return router
