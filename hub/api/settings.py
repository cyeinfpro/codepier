from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import json
from fastapi import Request
from hub.access import access_defaults, project_selection
from hub.mcp import VERSIONS
from shared.contracts import INSTRUCTIONS, tool_definitions
from shared.util import DevError, VERSION, normalize_url
from hub.api.models import TokenInput, SettingsInput



def make_settings_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/grants")
    @database_endpoint(store)
    def grants(request: Request):
        principal = auth.admin(request)
        rows = store.all("SELECT g.*,(SELECT max(expires) FROM tokens t WHERE t.grant_id=g.id) AS expires FROM grants g WHERE g.user_id=? ORDER BY g.created DESC", (principal.user_id,))
        for row in rows:
            row["scopes"], row["projects"] = json.loads(row["scopes"]), json.loads(row["projects"])
        return {"grants": rows}

    @router.post("/api/grants")
    @database_endpoint(store)
    def add_grant(request: Request, body: TokenInput):
        principal = auth.admin(request, True)
        projects = project_selection(store, body.projects, body.all_projects)
        result = auth.issue_grant(principal, body.label, body.scopes, projects, body.days)
        store.audit(principal.actor, "token.created", body.label, detail={"grant_id": result["grant_id"], "scopes": body.scopes, "projects": projects})
        return result

    @router.delete("/api/grants/{id}")
    @database_endpoint(store)
    def revoke_grant(id: str, request: Request):
        principal = auth.admin(request, True)
        store.execute("UPDATE grants SET revoked=1 WHERE id=? AND user_id=?", (id, principal.user_id))
        store.audit(principal.actor, "token.revoked", id)
        return {"ok": True}

    @router.get("/api/settings")
    @database_endpoint(store)
    def settings(request: Request):
        principal = auth.admin(request)
        return {"access_defaults": access_defaults(store, principal.user_id), "public_url": public_url(), "mcp_url": public_url() + "/mcp", "http_supported": True, "version": VERSION,
            "protocol_versions": sorted(VERSIONS), "tools": tool_definitions(), "instructions": INSTRUCTIONS,
            "single_process": True, "reliability": {"queue_ttl_seconds": runtime.queue_seconds, "call_wait_seconds": runtime.wait_seconds, "delivery_retry_seconds": runtime.retry_seconds, "durable_queue": True}, "listen_port": config.port, "data_dir": str(store.directory),
            "oauth": {"authorization_endpoint": public_url() + "/oauth/authorize", "token_endpoint": public_url() + "/oauth/token", "registration_endpoint": public_url() + "/oauth/register"}}

    @router.put("/api/settings")
    @database_endpoint(store)
    def update_settings(request: Request, body: SettingsInput):
        principal = auth.admin(request, True)
        try:
            value = normalize_url(body.public_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        store.execute("INSERT INTO meta(key,value) VALUES ('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        store.audit(principal.actor, "settings.updated", detail={"public_url": value})
        return {"public_url": value, "note": "只更新 MCP/OAuth 对外标识；不改变监听端口或家里 Agent 的连接地址。已有 OAuth 连接建议撤销后重新连接。"}


    return router
