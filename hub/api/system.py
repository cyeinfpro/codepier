from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import sqlite3
from datetime import datetime
from fastapi import Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from shared.core_contracts import CORE_TOOLS
from shared.util import VERSION
from hub.api.models import ComputerDecision, ToolCall
from hub.api.devices import device_rows
from hub.api.activity import operation_rows, audit_rows



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

    @router.get("/api/overview")
    @database_endpoint(store)
    def overview(request: Request):
        principal = auth.admin(request)
        tz = config.timezone
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        devices = device_rows(store, runtime)
        projects = runtime.list_projects(principal)
        counts = {r["state"]: r["n"] for r in store.all("SELECT state,count(*) AS n FROM operations WHERE created>=? GROUP BY state", (today,))}
        return {"devices": devices, "projects": projects, "online_devices": sum(1 for d in devices if d["online"]),
            "today_operations": sum(counts.values()), "today_succeeded": counts.get("succeeded", 0), "today_failed": counts.get("failed", 0),
            "active_operations": store.one("SELECT count(*) AS n FROM operations WHERE state IN ('running','queued','reconnecting','cancelling')")["n"],
            "recent_operations": operation_rows(store, limit=8), "recent_audit": audit_rows(store, limit=6),
            "mcp_url": public_url() + "/mcp", "version": VERSION, "tool_count": len(CORE_TOOLS), "timezone": str(tz)}

    @router.post("/api/tools/call")
    async def call_tool(request: Request, body: ToolCall):
        principal = await store.run(auth.admin, request, True)
        return await runtime.invoke(body.tool, body.arguments, principal)

    @router.get("/api/computer/approvals")
    async def computer_approvals(request: Request):
        await store.run(auth.admin, request)
        return {"approvals": await store.run(runtime.computer_approvals.list)}

    @router.post("/api/computer/approvals/{identifier}")
    async def computer_decision(identifier: str, request: Request, body: ComputerDecision):
        principal = await store.run(auth.admin, request, True)
        return await runtime.computer_approvals.decide(identifier, body.action, principal)

    @router.websocket("/agent/ws/{device_id}")
    async def agent_ws(websocket: WebSocket, device_id: str):
        await runtime.agent_socket(websocket, device_id)

    @router.get("/")
    async def index():
        return FileResponse(BASE / "web" / "index.html", headers={"Cache-Control": "no-cache"})


    return router
