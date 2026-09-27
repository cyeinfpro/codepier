from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub import iam
from hub.api.context import HubContext
import json
import time
import uuid
from fastapi import Request
from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.crypto import token
from shared.util import DevError, VERSION, normalize_url
from hub.api.models import DeviceCreate, DeviceUpdate

@iam.read_decision
def device_rows(store, runtime, principal):
    candidates = store.all("SELECT id,name,enabled,info,last_seen,created,owner_user_id FROM devices WHERE space_id=? ORDER BY created",(principal.space_id,))
    rows=[]
    for row in candidates:
        try: iam.require_device(store,principal,row['id'])
        except DevError: continue
        rows.append(row)
    lifecycle_names = tuple(sorted(DEVICE_ACTIONS))
    for row in rows:
        try:
            info = json.loads(row["info"] or "{}")
        except (TypeError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        row["can_manage"] = bool(principal.admin or row.get('owner_user_id')==principal.user_id)
        row["info"] = info
        row["online"] = runtime.online(row["id"])
        row["project_count"] = store.one("SELECT count(*) AS n FROM projects WHERE device_id=?", (row["id"],))["n"]
        management = info.get("management") if isinstance(info.get("management"), dict) else {}
        actions = info.get("device_actions") if isinstance(info.get("device_actions"), list) else []
        actions = sorted(set(actions) & DEVICE_ACTIONS)
        version = str(info.get("version") or management.get("installed_version") or "")[:80]
        latest = store.one(
            "SELECT id,tool,state,created,updated,error FROM operations WHERE device_id=? AND tool IN (?,?,?) ORDER BY created DESC LIMIT 1",
            (row["id"], *lifecycle_names),
        )
        if latest:
            try: runtime.operation_row(latest['id'],principal)
            except DevError: latest=None
        reason = str(management.get("reason") or "")[:300]
        if row["online"] and not actions and not reason:
            reason = "当前 Agent 版本尚未声明一键管理能力，请重新执行一次安装命令完成基础升级"
        elif not row["online"]:
            reason = "设备上线后才能执行更新、重启或卸载"
        row["agent"] = {
            "version": version,
            "target_version": VERSION,
            "update_available": bool(version and version != VERSION),
            "managed": bool(management.get("managed")),
            "service": bool(management.get("service")),
            "service_kind": str(management.get("service_kind") or "")[:40],
            "status": str(management.get("status") or "")[:40],
            "last_error": str(management.get("last_error") or "")[:500],
            "reason": reason,
            "actions": actions,
            "can_update": bool(row["enabled"] and row["online"] and "agent_update" in actions),
            "can_restart": bool(row["enabled"] and row["online"] and "agent_restart" in actions),
            "can_uninstall": bool(row["enabled"] and row["online"] and "agent_uninstall" in actions),
            "latest_action": latest,
        }
    return rows




def make_devices_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/devices")
    @database_endpoint(store)
    def devices(request: Request):
        principal = auth.panel(request)
        return {"devices": device_rows(store, runtime, principal)}

    @router.post("/api/devices")
    @database_endpoint(store)
    def create_device(request: Request, body: DeviceCreate):
        principal = auth.panel(request, True)
        try:
            hub_url = normalize_url(body.hub_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        if iam.membership(store,principal.user_id,principal.space_id)['level']=='guest':
            raise DevError('DEVICE_CREATE_DENIED','访客不能登记设备',403)
        if store.one('SELECT count(*) AS n FROM devices WHERE space_id=?',(principal.space_id,))['n']>=200:
            raise DevError('DEVICE_LIMIT','空间设备数量达到上限',409)
        id, secret = uuid.uuid4().hex, token(32)
        store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES (?,?,?,?,?,?)", (id, body.name, store.encrypt(secret), time.time(),principal.space_id,principal.user_id))
        store.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("device_hub_url:"+id, hub_url))
        store.audit(principal.actor, "device.created", body.name)
        runtime.publish("device", {"id": id})
        return {"pairing": {"device_id": id, "name": body.name, "secret": secret, "hub_url": hub_url}, "note": "此密钥仅本次显示，下载后导入家里 Agent。"}


    @router.patch("/api/devices/{id}")
    async def update_device(id: str, request: Request, body: DeviceUpdate):
        def update():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                if not store.one("SELECT id FROM devices WHERE id=?", (id,)):
                    raise DevError("NOT_FOUND", "设备不存在", 404)
                if body.name is not None:
                    store.execute("UPDATE devices SET name=? WHERE id=?", (body.name, id))
                if body.enabled is not None:
                    store.execute("UPDATE devices SET enabled=? WHERE id=?", (int(body.enabled), id))
                store.audit(principal.actor, "device.updated", id, detail=body.model_dump(exclude_none=True))
                runtime.publish("device", {"id": id})
        await store.run(update)
        if body.enabled is False:
            await runtime.disconnect_device(id, "Disabled by owner")
        return {"ok": True}

    @router.post("/api/devices/{id}/rotate")
    async def rotate_device(id: str, request: Request):
        def rotate():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                row = store.one("SELECT name FROM devices WHERE id=?", (id,))
                if not row:
                    raise DevError("NOT_FOUND", "设备不存在", 404)
                secret = token(32)
                store.execute("UPDATE devices SET secret=? WHERE id=?", (store.encrypt(secret), id))
                url = store.one("SELECT value FROM meta WHERE key=?", ("device_hub_url:" + id,))
                store.audit(principal.actor, "device.key_rotated", id)
                return {"pairing": {"device_id": id, "name": row["name"], "secret": secret,
                                    "hub_url": url["value"] if url else public_url()}}
        result = await store.run(rotate)
        await runtime.disconnect_device(id, "Device key rotated")
        def authorize_result():
            iam.require_device(store, auth.panel(request, True), id, manage=True)
        await store.run(authorize_result)
        return result

    @router.delete("/api/devices/{id}")
    async def delete_device(id: str, request: Request):
        def remove():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                if store.one("SELECT id FROM projects WHERE device_id=? LIMIT 1", (id,)):
                    raise DevError("DEVICE_HAS_PROJECTS", "请先移除这个设备的项目映射", 409)
                store.execute("DELETE FROM devices WHERE id=?", (id,))
                store.audit(principal.actor, "device.deleted", id)
        await store.run(remove)
        await runtime.disconnect_device(id, "Device removed")
        return {"ok": True}

    return router
