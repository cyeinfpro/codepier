from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import json
import re
import time
import uuid
from fastapi import Request
from hub.runtime import alias_key
from shared.crypto import digest
from shared.util import DevError
from hub.api.models import ProjectInput



def make_projects_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/projects")
    @database_endpoint(store)
    def projects(request: Request):
        return {"projects": runtime.list_projects(auth.admin(request))}

    async def save_project(body: ProjectInput, principal, id=None, request=None):
        alias = body.alias.strip()
        if not re.fullmatch(r"[\w.-]{1,64}", alias, flags=re.UNICODE) or alias in {".", ".."}:
            raise DevError("INVALID_ALIAS", "别名可使用中英文、数字、短横线、下划线和点，不要使用空格或路径")
        fields = body.model_dump(exclude={'idempotency_key'})
        fields['alias'] = alias
        fingerprint = digest(json.dumps([id,fields],sort_keys=True,ensure_ascii=False))
        # A durable save receipt owns the validation and the mapping commit.
        key = 'project_save:'+digest(principal.user_id+'\n'+(body.idempotency_key or fingerprint))
        def prepare():
            if request is not None:
                auth.admin(request, True)
            with store.lock, store.db:
                saved = store.one("SELECT value FROM meta WHERE key=?", (key,))
                plan = json.loads(saved['value']) if saved else None
                if plan and plan['fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT','保存回执已用于另一份项目配置',409)
                if plan and plan.get('committed') and body.idempotency_key:
                    current = store.one('SELECT * FROM projects WHERE id=?',(plan['target'],))
                    if current != plan['committed']:
                        raise DevError('PROJECT_CHANGED','原保存已完成，但项目随后改变；请刷新核对，未覆盖新配置',409)
                    return True, runtime.project_public(runtime.project(plan['target'],principal))
                old = store.one("SELECT * FROM projects WHERE id=?", (id,)) if id else None
                if id and not old:
                    raise DevError("NOT_FOUND", "项目映射不存在", 404)
                exists = store.one("SELECT id FROM projects WHERE alias_key=?", (alias_key(alias),))
                if exists and exists["id"] != id:
                    raise DevError("ALIAS_EXISTS", "这个别名已被使用（不区分大小写）", 409)
                if not plan or plan.get('committed'):
                    plan = {'fingerprint':fingerprint,'validation_key':'project-validation-'+uuid.uuid4().hex,
                            'target':id or uuid.uuid4().hex,'before':old,'created':time.time()}
                    store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(plan,ensure_ascii=False)))
                elif plan['before'] != old:
                    raise DevError('PROJECT_CHANGED','验证期间项目配置已改变，请刷新后重新编辑',409)
            return False, plan

        completed, prepared = await store.run(prepare)
        if completed:
            return prepared
        plan = prepared
        project = {"device_id": body.device_id, "root": body.root, "alias": alias, "mode": body.mode, "allow_tasks": body.allow_tasks}
        result = await runtime.dispatch("system_validate", {'idempotency_key':plan['validation_key']}, project, principal)
        def commit_mapping():
            if request is not None:
                auth.admin(request, True)
            if result.get("pending"):
                raise DevError("VALIDATION_PENDING", "原目录验证仍在进行；继续保存会查询同一验证，不会新建重复请求", 409,
                               operation_id=result['operation_id'],retryable=True)
            if body.mode == "write" and not result["writable"]:
                raise DevError("LOCAL_READ_ONLY", "本机授权为只读，请修改本机配置或选择只读映射", 403,
                               operation_id=result.get('operation_id'))
            if body.allow_tasks and not result["allow_tasks"]:
                raise DevError("LOCAL_TASKS_DISABLED", "本机 Agent 尚未允许任务执行。新安装可默认启用；已有 Agent 请在本机使用原配置运行 configure --shell full，或仅把对应 allowed_roots.allow_tasks 设为 true。配置会自动重载，随后再次验证保存；面板不会越过本机授权", 403,
                               operation_id=result.get('operation_id'))
            with store.lock, store.db:
                latest = json.loads(store.one('SELECT value FROM meta WHERE key=?',(key,))['value'])
                if latest.get('committed'):
                    current = store.one('SELECT * FROM projects WHERE id=?',(latest['target'],))
                    if current != latest['committed']:
                        raise DevError('PROJECT_CHANGED','原保存完成后项目已变化，请刷新核对',409)
                    return runtime.project_public(runtime.project(latest['target'],principal))
                current = store.one('SELECT * FROM projects WHERE id=?',(id,)) if id else None
                if current != plan['before']:
                    raise DevError('PROJECT_CHANGED','验证期间项目已改变；未覆盖另一窗口的更新',409)
                # Validation yields to other panel requests. Recheck constraints in
                # the same transaction that commits, not only before contacting Agent.
                device = store.one('SELECT enabled FROM devices WHERE id=?',(body.device_id,))
                if not device or not device['enabled']:
                    raise DevError('DEVICE_DISABLED','验证期间设备已停用或删除；未保存映射',409)
                occupied = store.one('SELECT id FROM projects WHERE alias_key=?',(alias_key(alias),))
                if occupied and occupied['id'] != plan['target']:
                    raise DevError('ALIAS_EXISTS','验证期间别名已被另一项目使用，请重新选择',409)
                target = plan['target']
                if id:
                    store.db.execute("UPDATE projects SET alias=?,alias_key=?,device_id=?,root=?,description=?,mode=?,allow_tasks=? WHERE id=?", (alias, alias_key(alias), body.device_id, result["root"], body.description, body.mode, int(body.allow_tasks), target))
                else:
                    store.db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)", (target, alias, alias_key(alias), body.device_id, result["root"], body.description, body.mode, int(body.allow_tasks), time.time()))
                latest['committed'] = store.one('SELECT * FROM projects WHERE id=?',(target,))
                store.db.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(latest,ensure_ascii=False),key))
            store.audit(principal.actor, "project.updated" if id else "project.created", alias, detail={"root": result["root"], "mode": body.mode, "allow_tasks": body.allow_tasks})
            runtime.publish("project", {"id": target})
            return runtime.project_public(runtime.project(target, principal))


        return await store.run(commit_mapping)
    @router.post("/api/projects")
    async def add_project(request: Request, body: ProjectInput):
        principal = await store.run(auth.admin, request, True)
        return await save_project(body, principal, request=request)

    @router.put("/api/projects/{id}")
    async def edit_project(id: str, request: Request, body: ProjectInput):
        principal = await store.run(auth.admin, request, True)
        return await save_project(body, principal, id, request=request)

    @router.delete("/api/projects/{id}")
    @database_endpoint(store)
    def delete_project(id: str, request: Request):
        principal = auth.admin(request, True)
        store.execute("DELETE FROM projects WHERE id=?", (id,))
        store.audit(principal.actor, "project.unmapped", id, detail={"local_files_deleted": False})
        runtime.publish("project", {"id": id})
        return {"ok": True, "note": "仅删除映射，不删除本机文件；已开始的任务不会自动撤销。"}


    return router
