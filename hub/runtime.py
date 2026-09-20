from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from hub.store import Store
from hub.computer_approvals import ComputerApprovals
from shared.computer_diagnostics import safe_detail
from hub.workflows import Workflows
from hub.diagnostics import Diagnostics
from hub.artifacts import ArtifactService
from hub.native_cli import NativeService
from shared.agent_lifecycle import DEVICE_ACTIONS, public_management
from shared.contracts import TOOLS, PROCESS_TOOLS, MUTATING
from shared.computer_contracts import COMPUTER_TOOLS
from shared.computer_media import scrub_expired, purge_database
from shared.crypto import SecureChannel, digest, token
from shared.util import DevError, safe_summary
from shared.execution_policy import POLICY_VERSION, DENIAL_MESSAGE, InvocationInspector, hub_blocks_codex, computer_denial

ACTIVE = {"queued", "running", "reconnecting", "cancelling"}
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}


def alias_key(value: str):
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass
class Principal:
    actor: str
    user_id: str
    scopes: set[str]
    projects: list[str]
    grant_id: str | None = None
    admin: bool = False


def remote_codex_denial(name: str, args: dict, principal: Principal) -> str | None:
    """Inspect invocations, not mentions. Authentication decides the exemption."""
    if not hub_blocks_codex() or principal.admin:
        return None
    if name == "shell_exec" and InvocationInspector(env=args.get("env")).shell(args.get("command", "")):
        return DENIAL_MESSAGE
    if computer_denial(name, args):
        return "MCP 不允许启动 Codex Computer Use；probe=false 静态状态和关闭现有会话仍可使用"
    return None


class Connection:
    def __init__(self, socket: WebSocket, channel: SecureChannel):
        self.socket, self.channel = socket, channel
        self.lock = asyncio.Lock()
        self.last_seen = time.time()
        self.journal_id = None
        self.protocol = 1
        self.unusable = False
        self.device_secret = None
        self.native_protocol = 0
        self.native_chat_protocol = 0

    async def send(self, body):
        # A half-open peer must not hold the scheduler/send lock indefinitely.
        async with self.lock:
            if self.unusable:
                raise ConnectionError("Authenticated channel is no longer usable")
            try:
                await asyncio.wait_for(self.socket.send_text(self.channel.pack(body)), 4)
            except BaseException:
                self.unusable = True
                # pack advanced its sequence number. Never reuse this channel after
                # an ambiguous send: reconnect and replay the *operation*, not ciphertext.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.socket.close(code=1011), 1)
                raise


class Runtime:
    def __init__(self, store: Store):
        self.store = store
        self.workflows = Workflows(self)
        from hub.integrations import HubIntegrations
        self.integrations = HubIntegrations(self)
        self.diagnostics = Diagnostics(self)
        self.artifacts = ArtifactService(self)
        self.native = NativeService(self)
        self.connections: dict[str, Connection] = {}
        self.futures: dict[str, asyncio.Future] = {}
        self.watchers: set[asyncio.Queue] = set()
        self.dispatch_lock = asyncio.Lock()
        self.delivery_slots = asyncio.Semaphore(8)
        self.stopping = False
        self.wake = asyncio.Event()
        self.worker = None
        self.computer_cleanup_at = 0.0
        self.computer_approvals = ComputerApprovals(self)
        self.queue_seconds = max(5, min(float(os.getenv("HUB_QUEUE_TTL_SECONDS", "1800")), 86400))
        self.wait_seconds = max(0, min(float(os.getenv("HUB_CALL_WAIT_SECONDS", "8")), 10))
        self.retry_seconds = max(.2, min(float(os.getenv("HUB_DELIVERY_RETRY_SECONDS", "5")), 30))

    async def start(self):
        self.stopping = False
        # Requests and their IDs are durable; restarting the Hub is not a task failure.
        self.store.execute("UPDATE operations SET state='reconnecting',next_attempt=0,transport_error='Hub restarted; reconciling with Agent' WHERE state IN ('running','reconnecting','cancelling') AND payload IS NOT NULL")
        self.store.execute("UPDATE operations SET state='needs_review',error='Legacy operation has no durable request; inspect its outcome' WHERE state IN ('queued','running','reconnecting','cancelling','unknown','interrupted') AND payload IS NULL AND result IS NULL")
        self.worker = asyncio.create_task(self.delivery_loop(), name="durable-delivery")

    def publish(self, kind: str, data=None):
        message = {"type": kind, "at": time.time(), "data": data or {}}
        for q in list(self.watchers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)

    def online(self, device_id: str):
        connection = self.connections.get(device_id)
        return bool(connection and not connection.unusable and time.time() - connection.last_seen < 45 and self.connection_authorized(device_id, connection))

    def connection_authorized(self, device_id, connection):
        device = self.store.one("SELECT enabled,secret FROM devices WHERE id=?", (device_id,))
        return bool(device and device["enabled"] and
                    (getattr(connection, "device_secret", None) is None or connection.device_secret == device["secret"]))

    def detach_connection(self, device_id, connection):
        if self.connections.get(device_id) is not connection:
            return False
        self.connections.pop(device_id, None)
        self.computer_approvals.drop(connection)
        connection.unusable = True
        with contextlib.suppress(Exception):
            self.store.execute("UPDATE operations SET state='reconnecting',transport_error='Agent disconnected; durable recovery pending',next_attempt=0 WHERE device_id=? AND state IN ('running','cancelling')", (device_id,))
        self.publish("device", {"id": device_id, "online": False})
        return True

    async def disconnect_device(self, device_id, reason):
        connection = self.connections.get(device_id)
        if connection:
            # Fence admission and late events before awaiting the close handshake.
            self.detach_connection(device_id, connection)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.socket.close(code=4003, reason=reason), 2)

    def visible_project(self, p: dict, principal: Principal):
        return principal.admin or "*" in principal.projects or p["id"] in principal.projects

    def project(self, value: str, principal: Principal):
        row = self.store.one("SELECT p.*,d.name AS device_name,d.enabled AS device_enabled FROM projects p JOIN devices d ON d.id=p.device_id WHERE p.id=? OR p.alias_key=?", (value, alias_key(value)))
        if not row or not self.visible_project(row, principal):
            raise DevError("PROJECT_NOT_FOUND", "未找到已授权的项目别名，请先调用 projects_list", 404)
        return row

    def project_public(self, p):
        return {k: p[k] for k in ("id", "alias", "device_id", "root", "description", "mode", "allow_tasks", "device_name") if k in p} | {"online": self.online(p["device_id"]), "allow_tasks": bool(p.get("allow_tasks"))}

    def list_projects(self, principal):
        return [self.project_public(p) for p in self.store.all("SELECT p.*,d.name AS device_name FROM projects p JOIN devices d ON d.id=p.device_id ORDER BY p.alias_key") if self.visible_project(p, principal)]

    def operation(self, id: str, principal: Principal, view: dict | None = None):
        row = self.store.one("SELECT * FROM operations WHERE id=?", (id,))
        if not row or (not principal.admin and (row["grant_id"] != principal.grant_id or row["project_id"] and "*" not in principal.projects and row["project_id"] not in principal.projects)):
            raise DevError("OPERATION_NOT_FOUND", "找不到此授权范围内的操作", 404)
        if row['tool'] in COMPUTER_TOOLS - {'computer_status'} and 'computer' not in principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', '读取桌面操作结果仍需 computer 权限', 403)
        result = dict(row)
        options = view or {}
        include_result = options.get("include_result", True)
        include_output = options.get("include_output", True)
        unchanged = options.get("after_output_seq") is not None and options["after_output_seq"] == row["output_seq"]
        include_output = include_output and not unchanged
        limit = options.get("output_limit", 131072)
        result["args_summary"] = json.loads(row["args_summary"]) if row["args_summary"] else None
        if include_result:
            result["result"] = json.loads(row["result"]) if row["result"] else None
            if isinstance(result["result"], dict):
                result["result"].pop("trace", None)
                if (row["tool"] in COMPUTER_TOOLS or row["tool"] in {"browser_open", "browser_snapshot", "browser_action", "browser_close"}):
                    scrub_expired(result["result"])
            data = (result["result"] or {}).get("data")
            if isinstance(data, dict) and "output" in data:
                original = data["output"]
                if not include_output:
                    data.pop("output")
                    data["output_omitted"] = True
                elif isinstance(original, str):
                    data["output"] = original[-limit:] if limit else ""
                    data["output_truncated"] = bool(data.get("output_truncated") or len(original) > limit)
        else:
            result.pop("result", None)
            result["result_omitted"] = True
        output = row["output"] or ""
        result["output_chars"] = len(output)
        if include_output:
            result["output"] = output[-limit:] if limit else ""
            result["output_truncated"] = len(output) > limit
        else:
            result.pop("output", None)
            result["output_omitted"] = True
            result["output_unchanged"] = unchanged
        for key in ("fingerprint", "idem", "payload", "journal_id"):
            result.pop(key, None)
        result["operation_id"] = id
        result["pending"] = row["state"] in ACTIVE or row["state"] == "unknown"
        result["next"] = "operations_wait" if result["pending"] else None
        result["retry_after_seconds"] = 2 if result["pending"] else None
        return result

    def list_operations(self, args, principal):
        clauses, values = [], []
        if not principal.admin:
            clauses.append("grant_id=?")
            values.append(principal.grant_id)
            if "*" not in principal.projects:
                clauses.append("(project_id IS NULL OR project_id IN (%s))" % (",".join("?" for _ in principal.projects) or "NULL"))
                values.extend(principal.projects)
        if args["project"]:
            clauses.append("project_id=?")
            values.append(self.project(args["project"], principal)["id"])
        for key, column in (("state", "state"), ("tool", "tool"), ("idempotency_key", "idem")):
            if args[key]:
                clauses.append(column + "=?")
                values.append(args[key])
        if args["before_created"] is not None:
            clauses.append("created<?")
            values.append(args["before_created"])
        sql = "SELECT id,project_id,tool,state,created,updated,attempts,accepted_at,cancel_requested,error FROM operations"
        rows = self.store.all(sql + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created DESC LIMIT ?", (*values, args["limit"] + 1))
        return {"operations": rows[:args["limit"]], "next_before_created": rows[args["limit"]-1]["created"] if len(rows) > args["limit"] else None}

    async def invoke(self, name: str, raw: dict, principal: Principal):
        tool = TOOLS.get(name)
        if not tool:
            raise DevError("UNKNOWN_TOOL", "不存在此工具", 404)
        if tool.scope not in principal.scopes:
            self.store.audit(principal.actor, name, status="denied", detail={"reason": "scope"})
            raise DevError("INSUFFICIENT_SCOPE", f"该凭据缺少 {tool.scope} 权限", 403)
        try:
            args = tool.model.model_validate(raw).model_dump()
            if args.get('workspace_id') == '': args.pop('workspace_id', None)
        except ValidationError as exc:
            issues = [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in exc.errors()]
            raise DevError("INVALID_ARGUMENTS", json.dumps(issues, ensure_ascii=False)[:1500]) from exc
        codex_denial = remote_codex_denial(name, args, principal)
        if codex_denial:
            self.store.audit(principal.actor, name, args.get("project", ""), status="denied",
                             detail={"reason": "remote_codex_policy"})
            raise DevError("CODEX_REMOTE_DISABLED", codex_denial, 403)
        from shared.integration_contracts import ADMIN_TOOLS, REMOTE_TOOLS as INTEGRATION_REMOTE_TOOLS
        if name in ADMIN_TOOLS and not principal.admin:
            raise DevError('OWNER_REQUIRED', '此操作只允许面板主理人执行', 403)
        if name == 'workflows_handoff':
            return self.integrations.handoff(args, principal)
        if name == 'activity_list':
            return self.integrations.activity(args, principal)
        if name == "projects_list":
            result = {"projects": self.list_projects(principal)}
        elif name == "projects_resolve":
            result = self.project_public(self.project(args["project"], principal))
        elif name == 'workspace_status':
            from hub.workspace_status import workspace_status
            result = workspace_status(self, args, principal)
        elif name == "diagnostics_get":
            result = self.diagnostics.get(args, principal)
        elif name == "operations_trace":
            result = self.diagnostics.trace(args, principal)
        elif name == "artifacts_get":
            result = self.artifacts.get(args, principal)
        elif name == "artifacts_list":
            result = self.artifacts.list(args, principal)
        elif name == "operations_list":
            result = self.list_operations(args, principal)
        elif name in {"workflows_create", "workflows_update"}:
            method = self.workflows.create if name == "workflows_create" else self.workflows.update
            result = method(args, principal)
            self.publish("workflow", {"id": result["workflow_id"], "state": result["state"], "version": result["version"]})
            return result
        elif name == "workflows_get":
            result = self.workflows.get(args, principal)
        elif name == "workflows_list":
            result = self.workflows.list(args, principal)
        elif name in {"operations_get", "operations_wait"}:
            result = self.operation(args["operation_id"], principal, {**args, "include_output": False, "include_result": False})
            if name == "operations_wait" and result["pending"]:
                until = time.monotonic() + args["wait_seconds"]
                while result["pending"] and time.monotonic() < until and not self.stopping:
                    await asyncio.sleep(min(.25, max(0, until - time.monotonic())))
                    result = self.operation(args["operation_id"], principal, {**args, "include_output": False, "include_result": False})
            result = self.operation(args["operation_id"], principal, args)
        elif name == "operations_cancel":
            return await self.cancel(args["operation_id"], principal)
        else:
            project = self.project(args["project"], principal)
            if name in MUTATING and name != "computer_session_close" and project["mode"] != "write":
                raise DevError("READ_ONLY", "面板中的这个项目设为只读", 403)
            if name in PROCESS_TOOLS and not project["allow_tasks"]:
                raise DevError("TASKS_DISABLED", "面板中的这个项目没有允许执行任务", 403)
            if name in (COMPUTER_TOOLS | INTEGRATION_REMOTE_TOOLS | {"open_workspace", "show_changes", "apply_patch", "skills_list", "skills_read", "agent_diagnostics", "artifacts_register", "searches_start", "searches_get", "searches_cancel", "code_symbols"}) and self.online(project["device_id"]):
                device=self.store.one("SELECT info FROM devices WHERE id=?",(project["device_id"],))
                capabilities=json.loads(device["info"]).get("capabilities") if device else None
                if isinstance(capabilities,list) and name not in capabilities:
                    raise DevError("AGENT_UPGRADE_REQUIRED","在线 Agent 未报告此工具能力；先用 diagnostics_get 核对版本，不要重复提交命令",409)
            if name in {"searches_get", "searches_cancel"}:
                source=self.operation(args["search_id"],principal,{"include_output":False})
                data=(source.get("result") or {}).get("data") or {}
                if source["tool"]!="searches_start" or source["project_id"]!=project["id"] or source["device_id"]!=project["device_id"]:
                    raise DevError("SEARCH_NOT_FOUND","搜索不属于当前项目和授权",404)
                source_workspace=(source.get('args_summary') or {}).get('workspace_id','')
                if source_workspace != args.get('workspace_id',''):
                    raise DevError('SEARCH_MAPPING_CHANGED','搜索属于另一工作目录，请使用原 workspace_id',409)
                if not source_workspace and data.get("project_root")!=project["root"]:
                    raise DevError("SEARCH_MAPPING_CHANGED","搜索尚未就绪或项目路径已变化，先核对原操作",409)
            if name == "artifacts_register" and args["source_operation_id"]:
                source=self.operation(args["source_operation_id"],principal,{"include_output":False,"include_result":False})
                if source["project_id"]!=project["id"] or source["device_id"]!=project["device_id"] or source["state"]!="succeeded":
                    raise DevError("ARTIFACT_SOURCE_INVALID","来源操作必须是同项目、同设备的真实成功操作",409)
            if name == "computer_session_close" and args.get("force") and not principal.admin:
                raise DevError("COMPUTER_FORCE_DENIED", "只有面板管理员可以强制停止其他会话", 403)
            return await self.dispatch(name, args, project, principal, background=name in PROCESS_TOOLS)
        self.store.audit(principal.actor, name, args.get("project", args.get("operation_id", args.get("workflow_id", ""))))
        return result

    async def cancel(self, id, principal):
        async with self.dispatch_lock:
            op = self.operation(id, principal)
            if not op["pending"]:
                return {"operation_id": id, "state": op["state"], "cancel_requested": False}
            if op["tool"] in DEVICE_ACTIONS:
                raise DevError("NOT_CANCELLABLE", "Agent 生命周期操作不能在准备或交接过程中取消；请等待结果后再处理", 409)
            if op["tool"] not in PROCESS_TOOLS and op["attempts"]:
                raise DevError("NOT_CANCELLABLE", "已投递的文件操作不能中途撤销；可在完成后使用备份恢复")
            self.store.execute("UPDATE operations SET cancel_requested=1,next_attempt=0 WHERE id=?", (id,))
            if not op["attempts"]:
                self.complete(op, {"ok": False, "error": {"code": "CANCELLED", "message": "操作尚未投递，已取消"}})
            else:
                self.store.execute("UPDATE operations SET state='cancelling',updated=? WHERE id=?", (time.time(), id))
        self.store.audit(principal.actor, "operations_cancel", id, detail={"cancel_requested": True, "durable": True})
        self.wake.set()
        return {"operation_id": id, "cancel_requested": True, "state": self.store.one("SELECT state FROM operations WHERE id=?", (id,))["state"], "next": "operations_wait"}

    async def dispatch_device_action(self, name: str, args: dict, device_id: str, principal: Principal):
        if name not in DEVICE_ACTIONS or not principal.admin:
            raise DevError("INSUFFICIENT_SCOPE", "只有面板管理员可以管理 Agent 生命周期", 403)
        device = self.store.one("SELECT id,name,enabled,info FROM devices WHERE id=?", (device_id,))
        if not device:
            raise DevError("NOT_FOUND", "设备不存在", 404)
        if not device["enabled"]:
            raise DevError("DEVICE_DISABLED", "设备已停用；启用并重新上线后再管理 Agent", 409)
        if not self.online(device_id):
            raise DevError("DEVICE_OFFLINE", "设备当前离线；生命周期操作不会排队等待未知时间，请上线后重试", 409)
        try:
            info = json.loads(device["info"] or "{}")
        except (TypeError, ValueError):
            info = {}
        actions = info.get("device_actions") if isinstance(info, dict) else None
        management = info.get("management") if isinstance(info, dict) else None
        if not isinstance(actions, list) or name not in actions:
            reason = management.get("reason") if isinstance(management, dict) else ""
            if not isinstance(reason, str) or not reason:
                reason = "当前在线 Agent 版本尚未声明安全生命周期能力；请先重新执行一次安装命令升级基础组件"
            raise DevError("AGENT_UPGRADE_REQUIRED", reason, 409)
        project = {"id": None, "alias": "", "root": "", "mode": "write", "allow_tasks": False,
                   "device_id": device_id}
        return await self.dispatch(name, args, project, principal, background=True)

    async def dispatch(self, name: str, args: dict, project: dict, principal: Principal, background=False):
        idem = args.get("idempotency_key")
        if name == "tasks_list" and not idem:
            # Resolve alias casing before identifying an unkeyed metadata query.
            args = {**args, "project": project["alias"]}
        snapshot = {k: project[k] for k in ("id", "alias", "root", "mode", "allow_tasks") if k in project}
        if name in {"open_workspace", "show_changes", "apply_patch"}:
            snapshot["_coding_owner"] = "grant:" + principal.grant_id if principal.grant_id else principal.actor
            snapshot["_coding_device"] = project["device_id"]
            snapshot["_coding_scopes"] = sorted(principal.scopes)
        if name in COMPUTER_TOOLS:
            snapshot["_computer_owner"] = "grant:" + principal.grant_id if principal.grant_id else principal.actor
            snapshot["_computer_admin"] = principal.admin
        fingerprint = digest(json.dumps({"tool": name, "args": args, "project": snapshot, "device": project["device_id"]}, sort_keys=True, ensure_ascii=False))
        async with self.dispatch_lock:
            old = self.store.one("SELECT * FROM operations WHERE actor=? AND idem=?", (principal.actor, idem)) if idem else None
            if name == "tasks_list" and not idem:
                # Reuse only an outstanding metadata query from this exact grant.
                # Explicit keys retain their own receipts; completed results are
                # never cached and file reads retain their submission ordering.
                old = self.store.one("""SELECT * FROM operations WHERE actor=? AND grant_id IS ?
                    AND device_id=? AND project_id=? AND tool='tasks_list' AND fingerprint=?
                    AND idem IS NULL AND state IN ('queued','running') AND result IS NULL
                    AND payload IS NOT NULL AND cancel_requested=0 AND deadline>?
                    ORDER BY created LIMIT 1""",
                    (principal.actor, principal.grant_id, project["device_id"], project["id"], fingerprint, time.time()))
            if old:
                if old["fingerprint"] != fingerprint:
                    raise DevError("IDEMPOTENCY_CONFLICT", "幂等键已经用于不同请求；未执行新操作", 409, operation_id=old["id"])
                id = old["id"]
                if old["result"]:
                    return self.unwrap(id, json.loads(old["result"]))
                # A retry wakes the durable worker; it does not allocate another operation.
                self.store.execute("UPDATE operations SET next_attempt=0 WHERE id=?", (id,))
            else:
                if name in TOOLS:
                    self.integrations.guard(name, args, project, principal)
                if name in (COMPUTER_TOOLS - {"computer_session_close"}) | {"browser_open", "browser_snapshot", "browser_action"} and not self.online(project["device_id"]):
                    raise DevError("COMPUTER_OFFLINE", "设备不在线，不排队保存延迟桌面操作；恢复后重新观察", 409)
                if name in DEVICE_ACTIONS and not self.online(project["device_id"]):
                    raise DevError("DEVICE_OFFLINE", "设备当前离线；生命周期操作未排队", 409)
                device = self.store.one("SELECT enabled FROM devices WHERE id=?", (project["device_id"],))
                if not device or not device["enabled"]:
                    raise DevError("DEVICE_DISABLED", "设备已停用；请先在面板启用", 409)
                active = self.store.one("SELECT count(*) AS n FROM operations WHERE device_id=? AND state IN ('queued','running','reconnecting','cancelling')", (project["device_id"],))["n"]
                if name in DEVICE_ACTIONS and active:
                    raise DevError("DEVICE_BUSY", "该设备还有操作未完成；为避免更新或卸载中断任务，请先等待现有操作结束", 409)
                if active >= 64 and name != 'integration_control':
                    raise DevError("DEVICE_BUSY", "该设备已有 64 个待完成操作，请先查询并等待已有操作", 429, retryable=True, retry_after_seconds=3)
                id, now = uuid.uuid4().hex, time.time()
                self.integrations.prepare(id, name, args, project, principal)
                payload = self.store.encrypt(json.dumps({"tool": name, "args": args, "project": snapshot}, ensure_ascii=False))
                lifecycle_ttl = 900 if name == "agent_update" else 120
                deadline_seconds = (min(self.queue_seconds, 15) if name in {"computer_action", "browser_action"} else
                                    min(self.queue_seconds, 60) if name in COMPUTER_TOOLS | {"browser_open", "browser_snapshot"} else
                                    min(self.queue_seconds, lifecycle_ttl) if name in DEVICE_ACTIONS else self.queue_seconds)
                self.store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,idem,state,created,updated,payload,deadline) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
                    (id, project["device_id"], project.get("id"), principal.actor, principal.grant_id, name,
                     json.dumps(safe_summary(args), ensure_ascii=False), fingerprint, idem, now, now, payload, now + deadline_seconds))
                self.store.audit(principal.actor, name, project.get("alias", ""), "queued", {"operation_id": id, "args": safe_summary(args)})
                self.diagnostics.record(id, "hub_received")
                self.publish("operation", {"id": id, "state": "queued", "tool": name})
            future = self.futures.get(id)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self.futures[id] = future
                expiry = asyncio.get_running_loop().call_later(30, self.expire_waiter, id, future)
                # Completed reads can hold multi-MiB results. Release the timer's
                # future reference promptly rather than retaining every result
                # for the full receipt-waiter lifetime.
                future.add_done_callback(lambda done, handle=expiry: handle.cancel())
        self.wake.set()
        # Offline and long task submissions return a durable receipt immediately.
        if not background and self.online(project["device_id"]):
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=self.wait_seconds)
                return self.unwrap(id, result)
            except asyncio.TimeoutError:
                pass
        op = self.store.one("SELECT state,deadline,result FROM operations WHERE id=?", (id,))
        if op["result"]:
            return self.unwrap(id, json.loads(op["result"]))
        pending = op["state"] in ACTIVE or op["state"] == "unknown"
        return {"operation_id": id, "pending": pending, "state": op["state"], "next": "operations_wait" if pending else None, "retry_after_seconds": 2 if pending else None,
                "deadline": op["deadline"], "note": "已持久保存。网络恢复后继续同一操作；请查询 operation_id，不要换新幂等键重复提交。"}

    def expire_waiter(self, id, future):
        if self.futures.get(id) is future:
            self.futures.pop(id, None)

    @staticmethod
    def unwrap(id, result):
        if not result.get("ok"):
            error = result.get("error") or {}
            details = safe_detail(error)
            if error.get('next') == 'computer_observe':
                details['next'] = 'computer_observe'
            if error.get('input_sent') is False:
                details['input_sent'] = False
            raise DevError(error.get("code", "REMOTE_ERROR"), error.get("message", "远端操作失败") + f" [operation_id={id}]", 409, operation_id=id, retryable=False, **details)
        scrub_expired(result)
        return {"operation_id": id, **(result.get("data") or {})}

    def permission_error(self, op, request):
        if not op["project_id"]:  # Admin-only root validation has no mapping yet.
            return None
        project = self.store.one("SELECT * FROM projects WHERE id=?", (op["project_id"],))
        if not project or project["root"] != request["project"]["root"] or project["device_id"] != op["device_id"] or project["alias"] != request["project"].get("alias"):
            return "项目映射在排队期间被删除或修改，未在新的路径执行"
        scope = TOOLS[op["tool"]].scope
        if op["tool"] in MUTATING and op["tool"] != "computer_session_close" and project["mode"] != "write" or op["tool"] in PROCESS_TOOLS and not project["allow_tasks"]:
            return "排队操作的项目写入/任务权限已撤销"
        if op["grant_id"]:
            grant = self.store.one("SELECT * FROM grants WHERE id=?", (op["grant_id"],))
            if not grant or grant["revoked"] or scope not in json.loads(grant["scopes"]) or not ("*" in json.loads(grant["projects"]) or op["project_id"] in json.loads(grant["projects"])):
                return "排队操作的 MCP 授权已撤销或缩小"
        return None

    async def delivery_loop(self):
        while not self.stopping:
            self.wake.clear()
            try:
                if time.monotonic() - self.computer_cleanup_at > 15:
                    with self.store.lock, self.store.db:
                        purge_database(self.store.db, 'operations')
                    self.computer_cleanup_at = time.monotonic()
                rows = self.store.all("SELECT id,device_id FROM operations WHERE state IN ('queued','running','reconnecting','cancelling') AND next_attempt<=? ORDER BY created LIMIT 128", (time.time(),))
                # Group per-device to retain submission order, without one dead link
                # delaying other home computers.
                devices = {}
                for row in rows:
                    devices.setdefault(row["device_id"], []).append(row["id"])
                await asyncio.gather(*(self.deliver_device(ids) for ids in devices.values()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient full/locked database must not kill the only worker
                # merely because its error audit cannot be persisted either.
                with contextlib.suppress(Exception):
                    self.store.audit("hub", "delivery.error", status="error", detail={"type": type(exc).__name__})
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except asyncio.TimeoutError:
                pass

    async def deliver_device(self, ids):
        for id in ids:
            if self.stopping:
                break
            try:
                # Each in-flight send holds a decrypted request. Bound that
                # memory even when many devices reconnect with large edits.
                async with self.delivery_slots:
                    await self.deliver(id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with contextlib.suppress(Exception):
                    self.store.execute("UPDATE operations SET transport_error=?,next_attempt=? WHERE id=?", ("Delivery interrupted: " + type(exc).__name__, time.time() + self.retry_seconds, id))
                # Retain the request and operation ID even if the send outcome is unknown.

    async def deliver(self, id):
        packets = []
        async with self.dispatch_lock:
            op = self.store.one("SELECT * FROM operations WHERE id=?", (id,))
            if not op or op["state"] not in ACTIVE:
                return
            now = time.time()
            if not op["payload"]:
                return
            try:
                request = json.loads(self.store.decrypt(op["payload"]))
                if (not isinstance(request, dict) or request.get("tool") != op["tool"] or
                        not isinstance(request.get("args"), dict) or
                        not isinstance(request.get("project"), dict) or
                        not isinstance(request["project"].get("root"), str) or
                        op["tool"] not in TOOLS and op["tool"] != "system_validate" and op["tool"] not in DEVICE_ACTIONS):
                    raise ValueError("Invalid durable request")
            except Exception:
                self.complete(op, {"ok": False, "error": {"code": "RECOVERY_DATA_INVALID", "message": "持久请求无法解密，请检查 Hub 数据库与 master.key 是否配套"}})
                return
            denied = self.permission_error(op, request) if not op["accepted_at"] else None
            # Never trust a client-supplied origin, UA or project field. Durable
            # actor/grant columns originate from Auth, not tool arguments.
            panel = not op["grant_id"] and op["actor"].startswith("panel:")
            grant = self.store.one('SELECT scopes FROM grants WHERE id=?', (op['grant_id'],)) if op['grant_id'] else None
            request['integration_context'] = {'owner': 'grant:'+op['grant_id'] if op['grant_id'] else op['actor'],
                'admin': panel, 'device_id': op['device_id'],
                'scopes': json.loads(grant['scopes']) if grant else (['read','write','execute','computer'] if panel else [])}
            if not op['accepted_at'] and op['project_id'] and op['tool'] in TOOLS:
                current_project = self.store.one('SELECT * FROM projects WHERE id=?', (op['project_id'],))
                if current_project:
                    try:self.integrations.guard(op['tool'], request['args'], current_project, Principal(op['actor'],'',set(),[],grant_id=op['grant_id'],admin=panel))
                    except DevError as exc:denied = denied or exc.message
            request["execution_policy"] = {"version": POLICY_VERSION,
                "origin": "panel" if panel else "mcp",
                "block_local_codex": not panel and hub_blocks_codex()}
            policy_denied = None
            if not op["accepted_at"]:
                policy_code = "CODEX_REMOTE_DISABLED"
                try:
                    policy_denied = remote_codex_denial(op["tool"], request["args"],
                        Principal(op["actor"], "", set(), [], grant_id=op["grant_id"], admin=panel))
                except DevError as exc:
                    # Missing grammar/analysis limits are terminal for unsent work,
                    # not a transport fault that should remain queued forever.
                    policy_code, policy_denied = exc.code, exc.message
                if policy_denied and not op["attempts"]:
                    self.complete(op, {"ok": False, "error": {"code": policy_code, "message": policy_denied}})
                    return
                denied = denied or policy_denied
            # Already accepted/ambiguous work is probed, never silently replayed or killed.
            expired = bool(op["deadline"] and now > op["deadline"])
            if not op["attempts"] and (expired or denied or op["cancel_requested"]):
                code = "AUTHORIZATION_CHANGED" if denied else "CANCELLED" if op["cancel_requested"] else "QUEUE_EXPIRED"
                self.complete(op, {"ok": False, "error": {"code": code, "message": denied or "排队操作已取消或超过首次执行期限；没有执行"}})
                return
            con = self.connections.get(op["device_id"])
            if not con or not self.online(op["device_id"]):
                self.store.execute("UPDATE operations SET next_attempt=? WHERE id=?", (now + self.retry_seconds, id))
                return
            if op.get("journal_id") and op["journal_id"] != con.journal_id:
                self.complete(op, {"ok": False, "error": {"code": "JOURNAL_CHANGED", "message": "Agent 操作账本已变化，不能证明旧操作是否执行；停止重放，请检查本机结果"}})
                return
            self.store.execute("UPDATE operations SET next_attempt=? WHERE id=?", (now + (15 if op["accepted_at"] else self.retry_seconds), id))
            # Commit the send decision without holding the global admission lock
            # across network I/O. Another device/HTTP caller must not wait for a
            # half-open home connection. A concurrent cancel is durable and follows
            # the same operation; it cannot be mistaken for an unsent cancellation.
            if op["cancel_requested"]:
                packets.append({"type": "cancel", "id": id})
            if op["accepted_at"] or expired or denied or op["cancel_requested"] and op["attempts"]:
                packets.append({"type": "probe", "id": id})
            else:
                self.store.execute("UPDATE operations SET attempts=attempts+1,journal_id=COALESCE(journal_id,?),updated=? WHERE id=?", (con.journal_id, now, id))
                self.diagnostics.record(id, "dispatched")
                packets.append({"type": "call", "id": id, **request, "not_after": op["deadline"]})
        for packet in packets:
            if self.connections.get(op["device_id"]) is not con or con.unusable or not self.connection_authorized(op["device_id"], con):
                return  # A fresh authenticated connection will replay the operation.
            await con.send(packet)

    def complete(self, op, result):
        # Callers may hold an old row while a newer result/output was committed.
        op = self.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
        if not op:
            return
        if op["state"] in {"succeeded", "failed", "cancelled"}:
            return
        if (not isinstance(result, dict) or type(result.get("ok")) is not bool or
                result.get("data") is not None and not isinstance(result["data"], dict) or
                result.get("error") is not None and not isinstance(result["error"], dict) or
                isinstance(result.get("error"), dict) and any(
                    k in result["error"] and not isinstance(result["error"][k], str) for k in ("code", "message"))):
            result = {"ok": False, "error": {"code": "INVALID_AGENT_RESULT", "message": "Agent 返回的结果格式无效，请检查本机执行结果"}}
        if result["ok"] and isinstance(result.get("data"), dict) and "exit_code" in result["data"]:
            data = dict(result["data"])
            data["command_ok"] = type(data["exit_code"]) is int and data["exit_code"] == 0 and not data.get("timed_out") and not data.get("cancelled")
            result = {**result, "data": data}
        self.integrations.complete(op, result)
        if op["tool"] == "artifacts_register" and result["ok"]:
            try:self.artifacts.register_result(op,result.get("data") or {})
            except DevError as exc:
                result={"ok":False,"error":{"code":exc.code,"message":exc.message}}
        self.diagnostics.ingest(op["id"], result.get("trace"))
        encoded = json.dumps(result, ensure_ascii=False)
        if op["result"] == encoded:
            return
        data = result.get("data") or {}
        error = result.get("error") or {}
        state = "succeeded" if result["ok"] else "failed"
        if error.get("code") in {"INTERRUPTED", "JOURNAL_CHANGED", "RECOVERY_DATA_INVALID", "INVALID_AGENT_RESULT", "DURABILITY_UNCONFIRMED", "COMPUTER_ACTION_UNCERTAIN", "BROWSER_ACTION_UNCERTAIN"}:
            state = "needs_review"
        elif error.get("code") == "CANCELLED" or data.get("cancelled"):
            state = "cancelled"
        elif op["tool"] in {"tasks_run", "shell_exec", "validation_run", "git_status", "git_diff", "git_log"} and result["ok"]:
            if not data.get("command_ok", False):
                state = "failed"
                error = {"message": "本机任务执行超时" if data.get("timed_out") else f"本机任务退出码 {data.get('exit_code')}；请查看操作输出"}
        if op['tool'] in COMPUTER_TOOLS and data.get('native_is_error'):
            state = 'needs_review' if data.get('action_outcome') == 'uncertain' else 'failed'
            error = {'message': '原生桌面工具报错；检查本机权限与界面，勿盲目重放输入'}
        if op['tool'] == 'apply_patch' and data.get('success') is False:
            state = 'needs_review' if data.get('outcome') == 'partial' else 'failed'
            error = data.get('error') or {'message': '批量补丁未完成，请核查备份与回滚结果'}
        output = str(data.get("output", op.get("output", "")))[-131072:]
        changed = self.store.execute("UPDATE operations SET state=?,result=?,error=?,output=?,updated=?,output_seq=output_seq+?,payload=NULL,transport_error=NULL WHERE id=? AND state NOT IN ('succeeded','failed','cancelled')",
            (state, encoded, error.get("message"), output, time.time(), int(output != (op.get("output") or "")), op["id"]))
        if not changed.rowcount:
            return
        self.store.audit(op["actor"], op["tool"], op["project_id"] or op["device_id"], state,
            {"operation_id": op["id"], "duration_ms": round((time.time() - op["created"]) * 1000),
             "error": error.get("message"), "path": data.get("path"), "backup_id": data.get("backup_id"), "exit_code": data.get("exit_code")})
        self.diagnostics.record(op["id"], "hub_completed")
        self.publish("operation", {"id": op["id"], "state": state, "tool": op["tool"]})
        future = self.futures.pop(op["id"], None)
        if future and not future.done():
            future.set_result(result)

    async def agent_socket(self, websocket: WebSocket, device_id: str):
        device = self.store.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not device or not device["enabled"]:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        challenge = token(32)
        connection = None
        authenticated = False
        try:
            await asyncio.wait_for(websocket.send_json({"type": "challenge", "challenge": challenge}), 4)
            channel = SecureChannel(self.store.decrypt(device["secret"]), challenge, device_id, "hub")
            connection = Connection(websocket, channel)
            connection.device_secret = device["secret"]
            hello = channel.unpack(await asyncio.wait_for(websocket.receive_text(), 10))
            if hello.get("type") != "hello":
                raise ValueError("Invalid hello")
            connection.journal_id = hello.get("journal_id")
            connection.protocol = hello.get("delivery_protocol", 1)
            connection.native_protocol = 1 if hello.get("native_protocol") == 1 else 0
            connection.native_chat_protocol = hello.get("native_chat_protocol") if type(hello.get("native_chat_protocol")) is int and hello["native_chat_protocol"] in (1, 2) else 0
            if (type(connection.protocol) is not int or connection.protocol != 2 or
                    not isinstance(connection.journal_id, str) or not 1 <= len(connection.journal_id) <= 128):
                raise ValueError("Agent must support durable delivery protocol 2 with a journal ID")
            if self.stopping or not self.connection_authorized(device_id, connection):
                return
            authenticated = True
            info = {k: safe_summary(hello[k], 1024) for k in ("name", "version", "platform", "hostname", "python", "roots", "journal_id", "delivery_protocol") if k in hello}
            capabilities = hello.get("capabilities")
            if isinstance(capabilities, list) and len(capabilities) <= 256 and all(isinstance(x, str) and len(x) <= 100 for x in capabilities):
                info["capabilities"] = capabilities
            actions = hello.get("device_actions")
            if isinstance(actions, list) and len(actions) <= 16 and all(isinstance(x, str) and len(x) <= 100 for x in actions):
                info["device_actions"] = sorted(set(actions) & DEVICE_ACTIONS)
            info["management"] = public_management(hello.get("management"))
            info["native_protocol"] = connection.native_protocol
            info["native_chat_protocol"] = connection.native_chat_protocol
            build = hello.get("build")
            if isinstance(build, dict) and len(json.dumps(build)) <= 12000:
                info["build"] = build
            # Complete the handshake before making this connection available for calls.
            await connection.send({"type": "ready", "heartbeat_seconds": 12, "delivery_protocol": 2})
            if self.stopping or not self.connection_authorized(device_id, connection):
                return
            old = self.connections.get(device_id)
            connection.last_seen = time.time()
            self.connections[device_id] = connection
            if old:
                old.unusable = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(old.socket.close(code=4001, reason="Replaced by new authenticated connection"), 2)
            if self.connections.get(device_id) is not connection or not self.connection_authorized(device_id, connection):
                return
            self.store.execute("UPDATE devices SET info=?,last_seen=? WHERE id=?", (json.dumps(info, ensure_ascii=False), time.time(), device_id))
            self.store.execute("UPDATE operations SET next_attempt=0 WHERE device_id=? AND state IN ('queued','running','reconnecting','cancelling')", (device_id,))
            self.store.audit("device:" + device["name"], "device.connected", device_id)
            self.publish("device", {"id": device_id, "online": True})
            self.wake.set()
            while not self.stopping:
                packet = await asyncio.wait_for(websocket.receive_text(), 45)
                data = channel.unpack(packet)
                if self.connections.get(device_id) is not connection or not self.connection_authorized(device_id, connection):
                    break  # Fence a replaced socket before accepting its late events.
                connection.last_seen = time.time()
                self.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id))
                kind, id = data.get("type"), data.get("id")
                if kind == "heartbeat":
                    changed = False
                    build = data.get("build")
                    if isinstance(build, dict) and len(json.dumps(build)) <= 12000:
                        info["build"] = build
                        changed = True
                    actions = data.get("device_actions")
                    if isinstance(actions, list) and len(actions) <= 16 and all(isinstance(x, str) and len(x) <= 100 for x in actions):
                        info["device_actions"] = sorted(set(actions) & DEVICE_ACTIONS)
                        changed = True
                    management = public_management(data.get("management"))
                    if management != info.get("management"):
                        info["management"] = management
                        changed = True
                    if changed:
                        self.store.execute("UPDATE devices SET info=? WHERE id=?", (json.dumps(info, ensure_ascii=False), device_id))
                    continue
                if kind in {"computer_approval", "computer_approval_closed"}:
                    self.computer_approvals.receive(device_id,connection,data)
                    continue
                if kind in {"native_reply", "native_sync"}:
                    await self.native.receive(device_id, connection, data)
                    continue
                if kind == "artifact_chunk":
                    self.artifacts.receive(connection,data)
                    continue
                op = self.store.one("SELECT * FROM operations WHERE id=? AND device_id=?", (id, device_id))
                if not op:
                    if kind == "result" and isinstance(id, str):
                        await connection.send({"type": "ack", "id": id})
                    continue
                if op["journal_id"] and op["journal_id"] != connection.journal_id:
                    # Outbox replay races with the delivery worker after hello.
                    # Apply the same epoch fence to inbound events as to calls.
                    self.complete(op, {"ok": False, "error": {"code": "JOURNAL_CHANGED", "message": "Agent 操作账本已变化，不能证明旧操作结果；请检查本机"}})
                    if kind == "result":
                        await connection.send({"type": "ack", "id": id})
                    continue
                if kind == "trace":
                    self.diagnostics.ingest(id, data.get("trace"))
                    self.publish("trace", {"id": id})
                    continue
                if kind in {"accepted", "started"} and op["state"] in ACTIVE:
                    state = "cancelling" if op["cancel_requested"] else "running" if kind == "started" else "queued"
                    self.store.execute("UPDATE operations SET accepted_at=COALESCE(accepted_at,?),state=?,transport_error=NULL,updated=? WHERE id=?", (time.time(), state, time.time(), id))
                    self.publish("operation", {"id": id, "state": state})
                elif kind == "status" and op["state"] in ACTIVE:
                    if data.get("status") in {"running", "accepted"}:
                        state = "cancelling" if op["cancel_requested"] else "running" if data["status"] == "running" else "queued"
                        self.store.execute("UPDATE operations SET accepted_at=COALESCE(accepted_at,?),state=?,transport_error=NULL WHERE id=?", (time.time(), state, id))
                    elif data.get("status") in {"missing", "retryable"}:
                        # retryable is issued only for a read or a command that had
                        # not started before an Agent restart. Never rerun side effects.
                        if data["status"] == "missing" and op["accepted_at"]:
                            self.complete(op, {"ok": False, "error": {"code": "JOURNAL_CHANGED", "message": "Agent 找不到已确认接收的操作记录；请检查本机，不自动重跑"}})
                        elif op["cancel_requested"] or op["deadline"] and time.time() > op["deadline"]:
                            self.complete(op, {"ok": False, "error": {"code": "CANCELLED" if op["cancel_requested"] else "QUEUE_EXPIRED", "message": "Agent 确认没有开始执行，该排队操作已取消或过期"}})
                        else:
                            # Revalidate current permissions in deliver() before replay.
                            self.store.execute("UPDATE operations SET accepted_at=NULL,next_attempt=0,state='queued',attempts=0 WHERE id=?", (id,))
                            self.wake.set()
                elif kind == "output" and op["tool"] in PROCESS_TOOLS and op["state"] in ACTIVE | {"unknown", "needs_review"}:
                    if data.get("snapshot"):
                        seq = data.get("seq")
                        if type(seq) is not int or seq <= op["output_seq"]:
                            continue
                        output = str(data.get("text", ""))[-131072:]
                    else:  # Legacy v1 Agent.
                        seq = op["output_seq"] + 1
                        output = (op["output"] + str(data.get("text", ""))[:8192])[-131072:]
                    self.store.execute("UPDATE operations SET output=?,output_seq=?,updated=? WHERE id=?", (output, seq, time.time(), id))
                    self.publish("output", {"id": id})
                elif kind == "result":
                    result = data.get("result")
                    self.complete(op, result)
                    await connection.send({"type": "ack", "id": id})
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.store.audit("device:" + device["name"], "device.protocol_error", device_id, "error", {"type": type(exc).__name__})
        finally:
            if connection is not None and self.detach_connection(device_id, connection):
                if authenticated:
                    with contextlib.suppress(Exception):
                        self.store.audit("device:" + device["name"], "device.disconnected", device_id, "offline")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(websocket.close(), 2)

    async def stop(self):
        self.stopping = True
        self.artifacts.close()
        self.wake.set()
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        async def close_connection(c):
            c.unusable = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(c.socket.close(code=1001), 2)
        await asyncio.gather(*(close_connection(c) for c in list(self.connections.values())))
        for future in self.futures.values():
            if not future.done():
                future.cancel()
        self.futures.clear()
