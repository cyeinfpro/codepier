from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import json
import time
from fastapi import Request, Query
from pydantic import Field
import asyncio
from collections import OrderedDict
from hub.settings_snapshot import snapshot, selected_project, node_snapshot
from hub.access import access_defaults, project_selection
from hub.mcp import VERSIONS
from hub import iam
from shared.contracts import INSTRUCTIONS, tool_definitions
from shared.util import DevError, VERSION, normalize_url
from hub.api.models import TokenInput, SettingsInput


class SettingsUpdateInput(SettingsInput):
    expected_public_url: str | None = Field(default=None, min_length=1, max_length=500)


def make_settings_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base
    node_reads = OrderedDict()
    node_lock = asyncio.Lock()

    @router.get("/api/settings/catalog")
    @database_endpoint(store)
    def settings_catalog(request: Request, project: str = Query(default="", max_length=100)):
        return snapshot(context, auth.panel(request), project)

    @router.get("/api/settings/node")
    async def settings_node(request: Request, project: str = Query(min_length=1, max_length=100)):
        def scope():
            principal = auth.panel(request)
            target = dict(selected_project(runtime, principal, project))
            target["_settings_permissions"] = tuple(sorted(iam.project_permissions(store, principal).get(target["id"], ())))
            return principal, target
        principal, target = await store.run(scope)
        # Key includes the live mapping and role epoch, never a model selector.
        key = (principal.user_id, principal.space_id, target["id"], target["device_id"],
               target["root"], target["mode"], target["allow_tasks"],
               principal.user_epoch, principal.identity_id, principal.admin,
               tuple(sorted(principal.scopes)), target["_settings_permissions"])
        if not runtime.online(target["device_id"]):
            return {"state": "offline", "values": {}, "checked_at": time.time()}
        async def inspect(previous=None):
            if previous and previous.get("operation_id"):
                def resume():
                    current, current_target = scope()
                    row = runtime.operation_row(previous["operation_id"], current)
                    if row["tool"] != "readiness_get" or row["project_id"] != current_target["id"] or row["device_id"] != current_target["device_id"]:
                        raise DevError("SETTINGS_SCOPE_CHANGED", "原检查不属于当前项目，请重新核对", 409)
                    return runtime.operation(row["id"], current, {"include_output": False})
                return node_snapshot(await store.run(resume))
            return node_snapshot(await runtime.invoke("readiness_get", {"project": target["id"]}, principal))
        async with node_lock:
            now = time.monotonic()
            for old_key, (created, task) in list(node_reads.items()):
                if task.done() and now - created > 30:
                    if task.cancelled() or task.exception() or task.result().get("state") != "pending":
                        node_reads.pop(old_key, None)
            entry = node_reads.get(key)
            if entry and entry[1].done() and not entry[1].cancelled() and not entry[1].exception() and entry[1].result().get("state") == "pending":
                entry = (now, asyncio.create_task(inspect(entry[1].result())))
                node_reads[key] = entry
            if not entry:
                if len(node_reads) >= 128:
                    raise DevError("SETTINGS_CHECK_LIMIT", "节点检查正在进行，请先核对原回执", 429)
                entry = (now, asyncio.create_task(inspect()))
                node_reads[key] = entry
            task = entry[1]
        result = await asyncio.shield(task)
        current, current_target = await store.run(scope)
        if (current_target["device_id"], current_target["root"], current_target["mode"], current_target["allow_tasks"], current_target["_settings_permissions"]) != (
                target["device_id"], target["root"], target["mode"], target["allow_tasks"], target["_settings_permissions"]):
            raise DevError("SETTINGS_SCOPE_CHANGED", "检查期间项目映射已变化，请重新核对", 409)
        return {**result, "project_id": current_target["id"]}

    @router.get("/api/grants")
    @database_endpoint(store)
    def grants(request: Request):
        principal = auth.panel(request)
        rows = store.all("""SELECT g.*,
            (SELECT max(expires) FROM tokens t WHERE t.grant_id=g.id AND t.kind IN ('access','refresh','pat')) AS expires,
            (SELECT max(expires) FROM oauth_codes c WHERE c.grant_id=g.id) AS pending_until
            FROM grants g WHERE g.user_id=? AND g.space_id=? ORDER BY g.created DESC""", (principal.user_id,principal.space_id))
        now = time.time()
        for row in rows:
            row["scopes"], row["projects"] = json.loads(row["scopes"]), json.loads(row["projects"])
            row["status"] = ("revoked" if row["revoked"] else
                "active" if row["expires"] and row["expires"] > now else
                "pending" if row["pending_until"] and row["pending_until"] > now else "expired")
            del row["pending_until"]
        return {"grants": rows}

    @router.post("/api/grants")
    @database_endpoint(store)
    def add_grant(request: Request, body: TokenInput):
        principal = auth.panel(request, True)
        if body.authorization_mode == 'role' and (body.projects or body.all_projects):
            raise DevError('INVALID_PROJECT', '动态角色不保存首次项目清单；请在角色管理中配置')
        projects = [] if body.authorization_mode == 'role' else project_selection(store, body.projects, body.all_projects, space_id=principal.space_id)
        result = auth.issue_grant(principal, body.label, body.scopes, projects, body.days,
                                  profile_id=body.profile_id, profile_version=body.profile_version, authorization_mode=body.authorization_mode,
                                  role_version=body.role_version, confirm_dynamic_role=body.confirm_dynamic_role, confirm_external_mcp=body.confirm_external_mcp)
        store.audit(principal.actor, "token.created", body.label, detail={"grant_id": result["grant_id"], "scopes": body.scopes, "projects": projects,
                    "authorization_mode": body.authorization_mode, "profile_id": body.profile_id, "role_version": body.role_version})
        return result


    @router.delete("/api/grants/{id}")
    @database_endpoint(store)
    def revoke_grant(id: str, request: Request):
        principal = auth.panel(request, True)
        store.execute("UPDATE grants SET revoked=1 WHERE id=? AND user_id=? AND space_id=?", (id, principal.user_id, principal.space_id))
        store.audit(principal.actor, "token.revoked", id)
        return {"ok": True}


    @router.get("/api/settings")
    @database_endpoint(store)
    def settings(request: Request):
        principal = auth.panel(request)
        return {"access_defaults": access_defaults(store, principal.user_id), "public_url": public_url(), "mcp_url": public_url() + "/mcp", "role_mcp_url": public_url() + "/mcp?authorization=role", "http_supported": True, "version": VERSION,
            "protocol_versions": sorted(VERSIONS), "tools": tool_definitions(), "instructions": INSTRUCTIONS,
            "single_process": True, "reliability": {"queue_ttl_seconds": runtime.queue_seconds, "call_wait_seconds": runtime.wait_seconds, "delivery_retry_seconds": runtime.retry_seconds, "durable_queue": True}, "listen_port": config.port, "data_dir": str(store.directory) if principal.instance_admin else "", "space_id":principal.space_id, "space_admin":principal.admin,"instance_admin":principal.instance_admin,
            "oauth": {"authorization_endpoint": public_url() + "/oauth/authorize", "token_endpoint": public_url() + "/oauth/token", "registration_endpoint": public_url() + "/oauth/register"}}


    @router.put("/api/settings")
    @database_endpoint(store)
    def update_settings(request: Request, body: SettingsUpdateInput):
        principal = auth.instance(request, True)
        try:
            value = normalize_url(body.public_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        with store.transaction():
            principal = auth.instance(request, True)
            current = public_url()
            if body.expected_public_url is not None and current != body.expected_public_url and current != value:
                raise DevError("SETTINGS_CHANGED", "地址已在其他窗口修改；你的草稿已保留，请重新读取并核对", 409)
            store.execute("INSERT INTO meta(key,value) VALUES ('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
            store.audit(principal.actor, "settings.updated", detail={"public_url": value})
        return {"public_url": value, "note": "只更新 MCP/OAuth 对外标识；不改变监听端口或家里 Agent 的连接地址。已有 OAuth 连接建议撤销后重新连接。"}


    return router
