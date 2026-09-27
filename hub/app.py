from __future__ import annotations
import asyncio
import csv
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from hub.auth import Auth, SESSION_SECONDS
from hub import iam
from hub.iam_api import make_iam_router, spaces_for
from hub.oidc import OIDCService
from hub.access_profiles import make_profiles_router, refresh_profile_principal
from hub.roles import make_roles_router, require_role, role_project_scopes, require_new_mapping
from shared.role_contracts import ROLE_SCOPE
from hub.access import access_defaults, make_access_router, project_selection
from hub.artifacts import make_artifact_router
from hub.agent_install import make_agent_install_router
from hub.native_cli import make_native_router
from hub.vps import make_vps_router
from hub.panel_update import make_panel_update_router
from shared.panel_maintenance import PanelMaintenance, PanelMaintenanceMiddleware
from hub.mcp import make_router, VERSIONS
from hub.oauth import OAuth
from hub.runtime import Runtime, alias_key
from hub.store import Store
from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.contracts import TOOLS, INSTRUCTIONS, tool_definitions
from shared.crypto import digest, password_hash, password_verify, token
from shared.util import DevError, VERSION, normalize_url, valid_json_value
from shared.instance_lock import InstanceLock

BASE = Path(__file__).resolve().parent.parent

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError("参数必须是有效的 UTF-8 JSON，且不含非有限数值或过深嵌套")
        return value

class ComputerDecision(Model):
    action: str = Field(pattern=r"^(accept|decline|cancel)$")

class Login(Model):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)

class DeviceCreate(Model):
    name: str = Field(min_length=1, max_length=80)
    hub_url: str = Field(min_length=1, max_length=500)

class DeviceUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None

class ProjectInput(Model):
    alias: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=100)
    root: str = Field(min_length=1, max_length=2048)
    description: str = Field(default="", max_length=1000)
    mode: str = Field(default="write", pattern=r"^(read|write)$")
    allow_tasks: bool = False
    idempotency_key: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9_.:-]*$")

class ToolCall(Model):
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict = Field(default_factory=dict)

class TokenInput(Model):
    label: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=4)
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False
    days: int = Field(default=30, ge=1, le=365)
    profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    profile_version: int | None = Field(default=None, ge=1)
    authorization_mode: str = Field(default='fixed', pattern=r'^(fixed|role)$')
    role_version: int | None = Field(default=None, ge=1)
    confirm_dynamic_role: bool = False

class PasswordInput(Model):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)

class SettingsInput(Model):
    public_url: str = Field(min_length=1, max_length=500)


class BodyLimit:
    """Bound JSON/form bodies before validation, including chunked requests."""
    def __init__(self, app, limit=6 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = self.limit + 1
        if length < 0 or length > self.limit:
            return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "请求超过 6 MiB"}}, status_code=413)(scope, receive, send)
        # FastAPI translates receive exceptions during body parsing into a
        # generic 400. Read bounded chunks here so overflow reliably stays 413
        # and handlers cannot perform side effects before the cap is checked.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "请求超过 6 MiB"}}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        sent = False
        async def buffered_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, buffered_receive, send)


def create_app(data_dir: str | None = None):
    directory = Path(data_dir or os.getenv("HUB_DATA_DIR", str(BASE / "data"))).resolve()
    # Acquire ownership before opening/migrating durable state.
    instance_lock = InstanceLock(directory / ".hub.lock")
    store = None
    try:
        store = Store(directory)
        runtime, auth = Runtime(store), Auth(store)
    except BaseException:
        if store is not None:
            store.close()
        instance_lock.close()
        raise
    def public_url():
        row = store.one("SELECT value FROM meta WHERE key='public_url'")
        return normalize_url(row["value"] if row else (os.getenv("MCP_PUBLIC_URL") or os.getenv("HUB_PUBLIC_URL") or "http://127.0.0.1:8765"))

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
            await oidc.start()
            yield
        finally:
            try:
                await oidc.stop()
                await runtime.stop()
            finally:
                try:
                    store.close()
                finally:
                    instance_lock.close()

    app = FastAPI(title="CodePier Agent", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runtime, app.state.auth = store, runtime, auth
    app.add_middleware(BodyLimit)
    app.add_middleware(iam.AuditContextMiddleware)
    maintenance = PanelMaintenance(runtime, os.getenv("HUB_PANEL_UPDATE_SOCKET", ""))
    runtime.panel_maintenance = maintenance
    app.add_middleware(PanelMaintenanceMiddleware, gate=maintenance)
    from hub.integrations import CallTimingMiddleware
    app.add_middleware(CallTimingMiddleware, runtime=runtime)

    @app.exception_handler(DevError)
    async def dev_error(request, exc: DevError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.details}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default output includes raw password/token input; don't reflect it.
        issues = [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in exc.errors()]
        return JSONResponse({"error": {"code": "INVALID_ARGUMENTS", "message": "参数校验失败", "issues": issues}}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"error": {"code": "CONFLICT", "message": "名称已存在，或关联记录已改变，请刷新后重试"}}, status_code=409)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path.startswith(("/api", "/oauth", "/auth")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def health():
        try:
            ready = bool(not runtime.stopping and runtime.worker and not runtime.worker.done() and store.one("SELECT 1 AS ok"))
        except sqlite3.Error:
            ready = False
        if not ready:
            return JSONResponse({"status": "unavailable", "version": VERSION}, status_code=503)
        return {"status": "ok", "version": VERSION, "panel_update": maintenance.status()}

    @app.get("/api/session")
    async def session(request: Request):
        configured = bool(store.one("SELECT id FROM users LIMIT 1"))
        try:
            row = auth.session(request)
            return {"authenticated": True, "configured": configured, "username": row["username"], "user_id": row["user_id"], "csrf": row["csrf"], "version": VERSION, "instance_admin": row["instance_admin"], "spaces": spaces_for(store,row["user_id"])}
        except DevError:
            return {"authenticated": False, "configured": configured, "version": VERSION}

    @app.post("/api/login")
    async def login(request: Request, body: Login):
        value = await auth.login(request, body.username, body.password)
        response = JSONResponse({"username": value["username"], "user_id": value["user_id"], "csrf": value["csrf"]})
        response.set_cookie("rd_session", value["cookie"], httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=SESSION_SECONDS, path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        session = auth.session_write(request)
        store.execute("DELETE FROM sessions WHERE id_hash=?", (session['id_hash'],))
        store.audit('panel:'+session['username'], "auth.logout")
        response = JSONResponse({"ok": True})
        response.delete_cookie("rd_session", path="/")
        return response

    @app.post("/api/account/password")
    async def password(request: Request, body: PasswordInput):
        principal = auth.panel(request, True)
        if not iam.user_security(store,principal.user_id)["local_login"]:
            raise DevError("OIDC_ONLY_ACCOUNT", "外部账号请在身份提供者修改密码", 403)
        row = store.one("SELECT password_hash FROM users WHERE id=?", (principal.user_id,))
        if not await asyncio.to_thread(password_verify, body.current_password, row["password_hash"]):
            raise DevError("PASSWORD_INCORRECT", "当前密码不正确", 403)
        hashed = await asyncio.to_thread(password_hash, body.new_password)
        with store.lock, store.db:
            # Verification and hashing yield to other requests. A competing
            # password reset or logout must invalidate this in-flight request.
            auth.panel(request, True)
            changed = store.db.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?", (hashed, principal.user_id, row["password_hash"])).rowcount
            if not changed:
                raise DevError("PASSWORD_CHANGED", "密码已变化，请重新登录后重试", 409)
            store.db.execute("DELETE FROM sessions WHERE user_id=?", (principal.user_id,))
            store.db.execute("UPDATE grants SET revoked=1 WHERE user_id=?", (principal.user_id,))
        store.audit(principal.actor, "auth.password_changed", detail={"all_sessions_and_grants_revoked": True})
        return {"ok": True, "relogin_required": True}

    @app.get("/api/overview")
    async def overview(request: Request):
        principal = auth.panel(request)
        try:
            tz = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))
        except Exception:
            tz = ZoneInfo("UTC")
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        devices = device_rows(principal)
        projects = runtime.list_projects(principal)
        clause, args = iam.private_sql(principal)
        if not principal.admin:
            clause += ' AND (project_id IS NULL OR project_id IN (%s))' % (','.join('?' for _ in principal.projects) or 'NULL')
            args.extend(principal.projects)
        counts = {r['state']:r['n'] for r in store.all('SELECT state,count(*) AS n FROM operations WHERE '+clause+' AND created>=? GROUP BY state', (*args,today))}
        return {"devices": devices, "projects": projects, "online_devices": sum(1 for d in devices if d["online"]),
            "today_operations": sum(counts.values()), "today_succeeded": counts.get("succeeded", 0), "today_failed": counts.get("failed", 0),
            "active_operations": store.one("SELECT count(*) AS n FROM operations WHERE "+clause+" AND state IN ('running','queued','reconnecting','cancelling')",args)["n"],
            "recent_operations": operation_rows(principal,limit=8), "recent_audit": audit_rows(principal,limit=6),
            "mcp_url": public_url() + "/mcp", "role_mcp_url": public_url() + "/mcp?authorization=role", "version": VERSION, "tool_count": len(TOOLS), "timezone": str(tz)}

    def device_rows(principal):
        with iam.read_scope(store):
            return scoped_device_rows(principal)

    def scoped_device_rows(principal):
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

    @app.get("/api/devices")
    async def devices(request: Request):
        principal=auth.panel(request)
        return {"devices": device_rows(principal)}

    @app.post("/api/devices")
    async def create_device(request: Request, body: DeviceCreate):
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

    @app.patch("/api/devices/{id}")
    async def update_device(id: str, request: Request, body: DeviceUpdate):
        principal = auth.panel(request, True)
        iam.require_device(store,principal,id,manage=True)
        row = store.one("SELECT * FROM devices WHERE id=?", (id,))
        if not row:
            raise DevError("NOT_FOUND", "设备不存在", 404)
        if body.name is not None:
            store.execute("UPDATE devices SET name=? WHERE id=?", (body.name, id))
        if body.enabled is not None:
            store.execute("UPDATE devices SET enabled=? WHERE id=?", (int(body.enabled), id))
            if not body.enabled:
                await runtime.disconnect_device(id, "Disabled by owner")
        store.audit(principal.actor, "device.updated", id, detail=body.model_dump(exclude_none=True))
        runtime.publish("device", {"id": id})
        return {"ok": True}

    @app.post("/api/devices/{id}/rotate")
    async def rotate_device(id: str, request: Request):
        principal = auth.panel(request, True)
        iam.require_device(store,principal,id,manage=True)
        row = store.one("SELECT name FROM devices WHERE id=?", (id,))
        if not row:
            raise DevError("NOT_FOUND", "设备不存在", 404)
        secret = token(32)
        store.execute("UPDATE devices SET secret=? WHERE id=?", (store.encrypt(secret), id))
        await runtime.disconnect_device(id, "Device key rotated")
        store.audit(principal.actor, "device.key_rotated", id)
        return {"pairing": {"device_id": id, "name": row["name"], "secret": secret, "hub_url": (store.one("SELECT value FROM meta WHERE key=?", ("device_hub_url:"+id,)) or {"value":public_url()})["value"]}}

    @app.delete("/api/devices/{id}")
    async def delete_device(id: str, request: Request):
        principal = auth.panel(request, True)
        iam.require_device(store,principal,id,manage=True)
        if store.one("SELECT id FROM projects WHERE device_id=? LIMIT 1", (id,)):
            raise DevError("DEVICE_HAS_PROJECTS", "请先移除这个设备的项目映射", 409)
        store.execute("UPDATE devices SET enabled=0 WHERE id=?", (id,))
        await runtime.disconnect_device(id, "Device removed")
        store.execute("DELETE FROM devices WHERE id=?", (id,))
        store.audit(principal.actor, "device.deleted", id)
        return {"ok": True}

    @app.get("/api/projects")
    async def projects(request: Request):
        return {"projects": runtime.list_projects(auth.panel(request))}

    async def save_project(body: ProjectInput, principal, id=None, request=None):
        principal = refresh_profile_principal(store, principal)
        iam.require_device(store,principal,body.device_id,creation=body.model_dump())
        if not principal.admin:
            if id is not None:
                raise DevError('OWNER_REQUIRED', '角色委派仅允许新增映射，不允许替换现有映射', 403)
            require_role(store, principal, 'projects.create', device_id=body.device_id, creation=body.model_dump())
        alias = body.alias.strip()
        if not re.fullmatch(r"[\w.-]{1,64}", alias, flags=re.UNICODE) or alias in {".", ".."}:
            raise DevError("INVALID_ALIAS", "别名可使用中英文、数字、短横线、下划线和点，不要使用空格或路径")
        fields = body.model_dump(exclude={'idempotency_key'})
        fields['alias'] = alias
        fingerprint = digest(json.dumps([id,fields],sort_keys=True,ensure_ascii=False))
        # A durable save receipt owns the validation and the mapping commit.
        key = 'project_save:'+principal.space_id+':'+digest((principal.user_id if principal.admin else principal.actor)+'\n'+(body.idempotency_key or fingerprint))
        with store.lock, store.db:
            saved = store.one("SELECT value FROM meta WHERE key=?", (key,))
            plan = json.loads(saved['value']) if saved else None
            if plan and plan['fingerprint'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT','保存回执已用于另一份项目配置',409)
            if plan and plan.get('committed') and body.idempotency_key:
                current = store.one('SELECT * FROM projects WHERE id=?',(plan['target'],))
                if current != plan['committed']:
                    raise DevError('PROJECT_CHANGED','原保存已完成，但项目随后改变；请刷新核对，未覆盖新配置',409)
                return runtime.project_public(current)
            old = store.one("SELECT * FROM projects WHERE id=? AND space_id=?", (id,principal.space_id)) if id else None
            if id and not old:
                raise DevError("NOT_FOUND", "项目映射不存在", 404)
            exists = store.one("SELECT id FROM projects WHERE alias_key=? AND space_id=?", (alias_key(alias),principal.space_id))
            if exists and exists["id"] != id:
                raise DevError("ALIAS_EXISTS", "这个别名已被使用（不区分大小写）", 409)
            if not principal.admin:
                require_new_mapping(store, principal, body.device_id, body.root)
            if not plan or plan.get('committed'):
                plan = {'fingerprint':fingerprint,'validation_key':'project-validation-'+uuid.uuid4().hex,
                        'target':id or uuid.uuid4().hex,'before':old,'created':time.time()}
                store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(plan,ensure_ascii=False)))
            elif plan['before'] != old:
                raise DevError('PROJECT_CHANGED','验证期间项目配置已改变，请刷新后重新编辑',409)
        project = {"device_id": body.device_id, "root": body.root, "alias": alias, "mode": body.mode, "allow_tasks": body.allow_tasks}
        result = await runtime.dispatch("system_validate", {'idempotency_key':plan['validation_key']}, project, principal)
        if request is not None:
            auth.panel(request, True)
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
            store.db.execute('BEGIN IMMEDIATE')
            principal = refresh_profile_principal(store, principal)
            iam.require_device(store,principal,body.device_id,creation={**body.model_dump(),"root":result["root"]})
            role = require_role(store, principal, 'projects.create', device_id=body.device_id,
                                creation={**body.model_dump(), 'root': result['root']}) if not principal.admin else None
            latest = json.loads(store.one('SELECT value FROM meta WHERE key=?',(key,))['value'])
            if latest.get('committed'):
                current = store.one('SELECT * FROM projects WHERE id=?',(latest['target'],))
                if current != latest['committed']:
                    raise DevError('PROJECT_CHANGED','原保存完成后项目已变化，请刷新核对',409)
                return runtime.project_public(current)
            if not principal.admin:
                require_new_mapping(store, principal, body.device_id, result['root'])
            current = store.one('SELECT * FROM projects WHERE id=?',(id,)) if id else None
            if current != plan['before']:
                raise DevError('PROJECT_CHANGED','验证期间项目已改变；未覆盖另一窗口的更新',409)
            # Validation yields to other panel requests. Recheck constraints in
            # the same transaction that commits, not only before contacting Agent.
            device = store.one('SELECT enabled FROM devices WHERE id=?',(body.device_id,))
            if not device or not device['enabled']:
                raise DevError('DEVICE_DISABLED','验证期间设备已停用或删除；未保存映射',409)
            occupied = store.one('SELECT id FROM projects WHERE alias_key=? AND space_id=?',(alias_key(alias),principal.space_id))
            if occupied and occupied['id'] != plan['target']:
                raise DevError('ALIAS_EXISTS','验证期间别名已被另一项目使用，请重新选择',409)
            target = plan['target']
            if id:
                store.db.execute("UPDATE projects SET alias=?,alias_key=?,device_id=?,root=?,description=?,mode=?,allow_tasks=? WHERE id=?", (alias, alias_key(alias), body.device_id, result["root"], body.description, body.mode, int(body.allow_tasks), target))
            else:
                store.db.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created,space_id,owner_user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (target, alias, alias_key(alias), body.device_id, result["root"], body.description, body.mode, int(body.allow_tasks), time.time(),principal.space_id,principal.user_id))
            if role:
                store.db.execute('INSERT INTO role_created_projects(role_id,project_id,created) VALUES (?,?,?)',
                                 (role['id'], target, time.time()))
            latest['committed'] = store.one('SELECT * FROM projects WHERE id=?',(target,))
            store.db.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(latest,ensure_ascii=False),key))
        store.audit(principal.actor, "project.updated" if id else "project.created", alias, detail={"root": result["root"], "mode": body.mode, "allow_tasks": body.allow_tasks})
        runtime.publish("project", {"id": target})
        return runtime.project_public(store.one('SELECT * FROM projects WHERE id=?', (target,)))

    async def create_project_for_role(arguments, principal):
        result = await save_project(ProjectInput.model_validate(arguments), principal)
        return {**result, 'created_by_role': principal.role_id,
                'role_access': sorted(role_project_scopes(store, principal, result['id']))}

    runtime.project_creator = create_project_for_role

    @app.post("/api/projects")
    async def add_project(request: Request, body: ProjectInput):
        return await save_project(body, auth.panel(request, True), request=request)

    @app.put("/api/projects/{id}")
    async def edit_project(id: str, request: Request, body: ProjectInput):
        return await save_project(body, auth.admin(request, True), id, request=request)

    @app.delete("/api/projects/{id}")
    async def delete_project(id: str, request: Request):
        principal = auth.admin(request, True)
        iam.project_in_space(store,principal,id)
        store.execute("DELETE FROM projects WHERE id=? AND space_id=?", (id,principal.space_id))
        store.audit(principal.actor, "project.unmapped", id, detail={"local_files_deleted": False})
        runtime.publish("project", {"id": id})
        return {"ok": True, "note": "仅删除映射，不删除本机文件；已开始的任务不会自动撤销。"}

    @app.post("/api/tools/call")
    async def call_tool(request: Request, body: ToolCall):
        return await runtime.invoke(body.tool, body.arguments, auth.panel(request, True))

    def operation_rows(principal,limit=40, offset=0, status="", project="", source=""):
        clause,args=iam.private_sql(principal,'o.')
        where=[clause]
        if not principal.admin:
            where.append('(o.project_id IS NULL OR o.project_id IN (%s))' % (','.join('?' for _ in principal.projects) or 'NULL'))
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
        sql = "SELECT o.id,o.device_id,o.project_id,o.actor,o.tool,o.state,o.created,o.updated,o.error,o.attempts,o.accepted_at,o.deadline,o.cancel_requested,o.transport_error,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return store.all(sql + " ORDER BY o.created DESC LIMIT ? OFFSET ?", (*args, limit, offset))

    @app.get("/api/operations")
    async def operations(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), status: str = "", project: str = "", source: str = ""):
        principal=auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = operation_rows(principal,limit + 1, offset, status, project, source)
        return {"operations": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @app.get("/api/operations/{id}")
    async def operation(id: str, request: Request):
        return runtime.operation(id, auth.panel(request))

    def audit_rows(principal,limit=40, offset=0, query="", status="", source=""):
        where,args=['space_id=?'],[principal.space_id]
        if not principal.admin:
            where.append('owner_user_id=?');args.append(principal.user_id)
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

    @app.get("/api/audit")
    async def audit(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), q: str = "", status: str = "", source: str = ""):
        principal=auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = audit_rows(principal,limit + 1, offset, q, status, source)
        return {"events": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @app.get("/api/audit-export")
    async def audit_export(request: Request, q: str = "", status: str = "", source: str = "", offset: int = Query(default=0, le=2**63 - 1)):
        principal = auth.panel(request)
        rows = audit_rows(principal,10000, max(0, offset), q, status, source)
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

    @app.get("/api/events")
    async def events(request: Request):
        session = auth.session(request)
        auth.panel(request)
        if len(runtime.watchers) >= 100:
            raise DevError("TOO_MANY_STREAMS", "实时连接过多", 429)
        q = asyncio.Queue(maxsize=100)
        runtime.watchers.add(q)
        async def stream():
            try:
                yield "event: ready\ndata: {}\n\n"
                while not runtime.stopping:
                    if await request.is_disconnected():
                        break
                    if not store.one("SELECT id_hash FROM sessions WHERE id_hash=? AND expires>?", (session["id_hash"], time.time())):
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                        if not store.one("SELECT id_hash FROM sessions WHERE id_hash=? AND expires>?", (session["id_hash"], time.time())):
                            break
                        try:
                            principal=auth.panel(request)
                            visible=iam.event_visible(runtime,principal,item)
                        except DevError:
                            break
                        if visible:
                            yield "data: " + json.dumps({k: v for k, v in item.items() if k != "_audience"}, ensure_ascii=False) + "\n\n"
                    except asyncio.TimeoutError:
                        try: auth.panel(request)
                        except DevError: break
                        yield ": heartbeat\n\n"
            finally:
                runtime.watchers.discard(q)
        class EventStream(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    # send(response.start) can fail before stream() starts,
                    # in which case its generator finally never runs.
                    runtime.watchers.discard(q)
        return EventStream(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/grants")
    async def grants(request: Request):
        principal = auth.panel(request)
        rows = store.all("SELECT g.*,(SELECT max(expires) FROM tokens t WHERE t.grant_id=g.id) AS expires FROM grants g WHERE g.user_id=? AND g.space_id=? ORDER BY g.created DESC", (principal.user_id,principal.space_id,))
        for row in rows:
            row["scopes"], row["projects"] = json.loads(row["scopes"]), json.loads(row["projects"])
        return {"grants": rows}

    @app.post("/api/grants")
    async def add_grant(request: Request, body: TokenInput):
        principal = auth.panel(request, True)
        if body.authorization_mode == 'role' and (body.projects or body.all_projects):
            raise DevError('INVALID_PROJECT', '动态角色不保存首次项目清单；请在角色管理中配置')
        projects = [] if body.authorization_mode == 'role' else project_selection(store, body.projects, body.all_projects, space_id=principal.space_id)
        result = auth.issue_grant(principal, body.label, body.scopes, projects, body.days,
                                  profile_id=body.profile_id, profile_version=body.profile_version, authorization_mode=body.authorization_mode,
                                  role_version=body.role_version, confirm_dynamic_role=body.confirm_dynamic_role)
        store.audit(principal.actor, "token.created", body.label, detail={"grant_id": result["grant_id"], "scopes": body.scopes, "projects": projects,
                    "authorization_mode": body.authorization_mode, "profile_id": body.profile_id, "role_version": body.role_version})
        return result

    @app.delete("/api/grants/{id}")
    async def revoke_grant(id: str, request: Request):
        principal = auth.panel(request, True)
        store.execute("UPDATE grants SET revoked=1 WHERE id=? AND user_id=? AND space_id=?", (id, principal.user_id, principal.space_id))
        store.audit(principal.actor, "token.revoked", id)
        return {"ok": True}

    @app.get("/api/settings")
    async def settings(request: Request):
        principal = auth.panel(request)
        return {"access_defaults": access_defaults(store, principal.user_id), "public_url": public_url(), "mcp_url": public_url() + "/mcp", "role_mcp_url": public_url() + "/mcp?authorization=role", "http_supported": True, "version": VERSION,
            "protocol_versions": sorted(VERSIONS), "tools": tool_definitions(), "instructions": INSTRUCTIONS,
            "single_process": True, "reliability": {"queue_ttl_seconds": runtime.queue_seconds, "call_wait_seconds": runtime.wait_seconds, "delivery_retry_seconds": runtime.retry_seconds, "durable_queue": True}, "listen_port": int(os.getenv("HUB_PORT", "8765")), "data_dir": str(store.directory) if principal.instance_admin else "", "space_id":principal.space_id, "space_admin":principal.admin,"instance_admin":principal.instance_admin,
            "oauth": {"authorization_endpoint": public_url() + "/oauth/authorize", "token_endpoint": public_url() + "/oauth/token", "registration_endpoint": public_url() + "/oauth/register"}}

    @app.put("/api/settings")
    async def update_settings(request: Request, body: SettingsInput):
        principal = auth.instance(request, True)
        try:
            value = normalize_url(body.public_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        store.execute("INSERT INTO meta(key,value) VALUES ('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        store.audit(principal.actor, "settings.updated", detail={"public_url": value})
        return {"public_url": value, "note": "只更新 MCP/OAuth 对外标识；不改变监听端口或家里 Agent 的连接地址。已有 OAuth 连接建议撤销后重新连接。"}

    @app.get("/api/computer/approvals")
    async def computer_approvals(request: Request):
        principal=auth.panel(request)
        return {"approvals": runtime.computer_approvals.list(principal)}

    @app.post("/api/computer/approvals/{identifier}")
    async def computer_decision(identifier: str, request: Request, body: ComputerDecision):
        principal = auth.panel(request, True)
        return await runtime.computer_approvals.decide(identifier, body.action, principal)

    @app.websocket("/agent/ws/{device_id}")
    async def agent_ws(websocket: WebSocket, device_id: str):
        await runtime.agent_socket(websocket, device_id)

    try:
        oauth = OAuth(auth, runtime, public_url)
        runtime.oauth = oauth
        oidc = OIDCService(auth,runtime,public_url)
        app.state.oidc = oidc
        app.include_router(oidc.router)
        app.include_router(make_iam_router(auth,runtime))
        app.include_router(oauth.router)
        app.include_router(make_router(auth, runtime, public_url))
        app.include_router(make_artifact_router(auth, runtime))
        app.include_router(make_agent_install_router(runtime, auth))
        app.include_router(make_access_router(auth, runtime))
        app.include_router(make_profiles_router(auth, runtime))
        app.include_router(make_roles_router(auth, runtime))
        app.include_router(make_native_router(auth, runtime))
        app.include_router(make_vps_router(auth, runtime))
        app.include_router(make_panel_update_router(auth, runtime))
        app.mount("/static", StaticFiles(directory=BASE / "web"), name="static")
    except BaseException:
        try:
            store.close()
        finally:
            instance_lock.close()
        raise

    @app.get("/")
    async def index():
        return FileResponse(BASE / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    return app
