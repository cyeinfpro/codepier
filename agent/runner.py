from __future__ import annotations
import asyncio
import contextlib
import codecs
import json
import math
import os
import platform
import random
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import websockets
from agent.filesystem import FileEngine
from agent.core_files import CoreFiles
from agent.resource_queue import Claim, ResourceQueue, claims_for
from shared.core_contracts import CORE_REMOTE
from agent.journal import Journal
from agent.telemetry import AgentTelemetry
from agent.artifacts import Artifacts
from agent.searches import Searches
from agent.computer import Computer
from agent.computer_approvals import AgentApprovals
from agent.lifecycle import LifecycleManager
from agent.native_cli import NativeCLI
from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.computer_diagnostics import safe_detail
from shared.computer_contracts import COMPUTER_TOOLS
from shared.build_info import BuildIdentity
from agent.config import validate_config
from agent.shell import execution_info, prepare_shell
from shared.contracts import TOOLS, MUTATING
from shared.crypto import SecureChannel
from shared.util import DevError, VERSION
from shared.tool_protocol import advertisement, negotiate, validate_call_epoch
from shared.execution_policy import enforce_argv, agent_blocks_codex, computer_denial, DENIAL_MESSAGE
from shared.instance_lock import InstanceLock


class Agent:
    def __init__(self, config_path: Path):
        self.config_path = config_path.expanduser().resolve()
        self.config = self.load_config()
        self.state_dir = Path(self.config.get("state_dir", str(self.config_path.parent / "state"))).expanduser().resolve()
        self.instance_lock = InstanceLock(self.state_dir / ".agent.lock")
        try:
            self.journal = Journal(self.state_dir)
            self.engine = FileEngine(self.config, self.journal, self.config_path)
            self.telemetry = AgentTelemetry(self.journal)
            self.build = BuildIdentity("agent")
            self.lifecycle = LifecycleManager(self.config_path, self.state_dir, lambda: self.config)
            self.native = NativeCLI(self)
            self.lifecycle.native_live = self.native.live
            self.artifacts = Artifacts(self.engine)
            self.searches = Searches(self.engine)
            self.computer_approvals = AgentApprovals(self.send)
            self.computer = Computer(lambda: self.config, self.state_dir, self.journal, approvals=self.computer_approvals)
            from agent.integrations import Integrations
            self.integrations = Integrations(self)
        except BaseException:
            if hasattr(self, "journal"):
                self.journal.db.close()
            self.instance_lock.close()
            raise
        self.socket = None
        self.channel = None
        self.send_lock = asyncio.Lock()
        self.jobs: dict[str, asyncio.Task] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.cancelled: set[str] = set()
        self.project_locks: dict[str, asyncio.Lock] = {}
        self.slot_owners = {}
        self.project_condition = asyncio.Condition()
        self.active_roots: set[Path] = set()
        self._active_slots: dict[object, tuple[Path, bool]] = {}
        self._waiting_writes: list[tuple[object, Path]] = []
        self.semaphore = asyncio.Semaphore(4)
        self.read_semaphore = asyncio.Semaphore(4)
        self.resources = ResourceQueue()
        self.stop_event = asyncio.Event()
        self.connection_number = 0
        self.pending_output: dict[str, dict] = {}
        self.output_wake = asyncio.Event()
        self.kill_tasks: set[asyncio.Task] = set()
        self.finishing: set[str] = set()
        self.transfer_tasks = set()
        self.integration_projects = {}
        self.integration_tool_names = {}
        self.lifecycle.activity_check = self.check_maintenance_idle

    def check_maintenance_idle(self, operation_id):
        if getattr(self.computer, 'session', None):
            raise DevError('COMPUTER_BUSY', '请先结束桌面控制会话，再维护 Agent', 409)
        busy = [key for key, task in self.jobs.items() if key != operation_id and not task.done()]
        if (busy or self.processes or any(not task.done() for task in self.transfer_tasks)
                or any(not task.done() for task in self.kill_tasks)):
            raise DevError('AGENT_BUSY', '设备仍有活动工作，维护交接尚未开始', 409)

    def load_config(self):
        try:
            with self.config_path.open(encoding="utf-8") as f:
                return validate_config(json.load(f), self.config_path)
        except RecursionError as exc:
            raise ValueError("Agent 配置嵌套过深") from exc

    async def send(self, body: dict):
        async with self.send_lock:
            socket, channel = self.socket, self.channel
            if not socket or not channel:
                return False
            try:
                await asyncio.wait_for(socket.send(channel.pack(body)), 4)
                return True
            except (Exception, asyncio.CancelledError) as exc:
                # A failed send consumes a sequence number; discard this channel.
                if self.socket is socket:
                    self.socket = self.channel = None
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), 1)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return False

    async def output_sender(self):
        """Coalesced snapshots; stdout draining never waits for the network."""
        while not self.stop_event.is_set():
            self.output_wake.clear()
            for id, packet in list(self.pending_output.items()):
                if not await self.send(packet):
                    break
                if self.pending_output.get(id) is packet:
                    self.pending_output.pop(id, None)
            try:
                await asyncio.wait_for(self.output_wake.wait(), 1)
            except asyncio.TimeoutError:
                pass
            # Bound update rate even for tasks printing thousands of lines/second.
            await asyncio.sleep(.2)

    def phase(self, id, stage, **detail):
        self.telemetry.record(id, stage, **detail)
        self.pending_output['trace:'+id] = {"type": "trace", "id": id, "trace": self.telemetry.snapshot(id)}
        self.output_wake.set()

    async def report_status(self, id):
        state = self.journal.status(id)
        await self.send({"type": "trace", "id": id, "trace": self.telemetry.snapshot(id)})
        if state.get("result") is not None:
            await self.send({"type": "result", "id": id, "result": state["result"]})
        else:
            await self.send({"type": "status", "id": id, "status": state["status"]})
        if state.get("output_seq"):
            self.pending_output[id] = {"type": "output", "id": id, "snapshot": True,
                                       "seq": state["output_seq"], "text": state["output"]}
            self.output_wake.set()

    async def report_outbox(self):
        for item in self.journal.outbox():
            if not await self.send({"type": "result", **item}):
                break

    async def heartbeat(self):
        socket = self.socket
        try:
            while not self.stop_event.is_set():
                await self.send({"type": "heartbeat", "at": time.time(), "running": len(self.jobs),
                                 "build": self.build.describe(), "management": self.lifecycle.describe(),
                                 "device_actions": self.lifecycle.actions()})
                await self.report_outbox()
                await asyncio.sleep(12)
        except Exception:
            # End the reader too, so a failed outbox task cannot silently leave
            # an apparently connected Agent that no longer reports results.
            if socket is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), 1)
            raise

    async def watch_config(self):
        # The file can be temporarily absent during an editor's save/replace.
        old = None
        while not self.stop_event.is_set():
            await asyncio.sleep(2)
            try:
                stat = self.config_path.stat()
                current = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if current != old:
                    c = self.load_config()
                    if c.get("state_dir", str(self.config_path.parent / "state")) != self.config.get("state_dir", str(self.config_path.parent / "state")):
                        print("[Agent] state_dir 更改需要手动重启。", file=sys.stderr)
                    if c.get("device_id") != self.config["device_id"] or c.get("secret") != self.config["secret"]:
                        print("[Agent] 设备身份/密钥更改需要停止 Agent 后重新启动；未在运行中切换身份。", file=sys.stderr)
                        old = current
                        continue
                    # Do not pretend a hot-reloaded state_dir changed the open journal.
                    c["state_dir"] = str(self.state_dir)
                    if c == self.config:
                        old = current
                        continue
                    self.config = c
                    self.engine.config = c
                    old = current
                    print("[Agent] 配置已重新加载，连接新的面板地址。", flush=True)
                    if self.socket:
                        await self.socket.close(code=1000, reason="Configuration reloaded")
            except (ValueError, OSError, TypeError, KeyError) as exc:
                print(f"[Agent] 新配置无效，继续使用原配置：{exc}", file=sys.stderr)

    async def connect_once(self):
        config = self.config
        base = config["hub_url"]
        ws_url = ("wss://" if base.startswith("https://") else "ws://") + base.split("://", 1)[1] + "/agent/ws/" + config["device_id"]
        async with websockets.connect(ws_url, open_timeout=15, ping_interval=20, ping_timeout=25,
                                      max_size=16 * 1024 * 1024, compression=None, proxy=None, close_timeout=3) as socket:
            challenge = json.loads(await asyncio.wait_for(socket.recv(), 10))
            if not isinstance(challenge, dict) or challenge.get("type") != "challenge":
                raise ValueError("面板握手无效")
            channel = SecureChannel(config["secret"], challenge["challenge"], config["device_id"], "agent")
            await socket.send(channel.pack({"type": "hello", "name": config.get("name", platform.node()),
                          "version": VERSION, "build": self.build.describe(), "platform": platform.system(), "hostname": platform.node(),
                          "python": platform.python_version(), "roots": config["allowed_roots"], "capabilities": list(TOOLS),
                          "management": self.lifecycle.describe(), "device_actions": self.lifecycle.actions(),
                          "journal_id": self.journal.journal_id, "delivery_protocol": 2, "native_protocol": 1, "native_chat_protocol": 3, **advertisement(self.build.catalog_sha256)}))
            reply = channel.unpack(await asyncio.wait_for(socket.recv(), 10))
            if reply.get("type") != "ready":
                raise ValueError("面板未确认 Agent 身份")
            self.hub_protocol = negotiate(reply)
            if self.stop_event.is_set() or self.config is not config:
                return
            self.socket, self.channel = socket, channel
            self.connection_number += 1
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S%z')}] [Agent] 已连接 {base}；设备 {self.config['device_id']}；授权目录 {len(self.config['allowed_roots'])} 个。", flush=True)
            self.output_wake.set()
            # heartbeat sends the outbox concurrently with the socket reader,
            # so result ACKs are processed during backlog recovery.
            heartbeat = asyncio.create_task(self.heartbeat())
            native_sync = asyncio.create_task(self.native.sync())
            try:
                async for packet in socket:
                    data = channel.unpack(packet)
                    kind = data.get("type")
                    if kind in {"call", "probe", "ack", "cancel"}:
                        id = data.get("id", "")
                        if not isinstance(id, str) or not id or len(id) > 100:
                            raise ValueError("Invalid operation ID")
                    if kind == "native_ack":
                        offsets = data.get("offsets", {})
                        if isinstance(offsets, dict):
                            self.native.offsets.update({k: v for k, v in offsets.items() if isinstance(k,str) and type(v) is int and v >= 0})
                        continue
                    if kind == "native_request":
                        if self.lifecycle.handoff_pending:
                            await self.send({"type":"native_reply", "request_id":data.get("request_id"), "result":{"ok":False,"error":{"code":"AGENT_MAINTENANCE","message":"CodePier 正在交接更新，请稍后重试"}}})
                        elif len(self.transfer_tasks) >= 8:
                            await self.send({"type":"native_reply", "request_id":data.get("request_id"), "result":{"ok":False,"error":{"code":"CLI_BUSY","message":"控制通道繁忙"}}})
                        else:
                            task = asyncio.create_task(self.native.handle(data, socket))
                            self.transfer_tasks.add(task)
                            task.add_done_callback(self.transfer_done)
                        continue
                    if kind == "computer_approval_decision":
                        self.computer_approvals.decide(data)
                        continue
                    if kind == "artifact_read":
                        request_id=data.get("request_id")
                        if self.lifecycle.handoff_pending:
                            await self.send({"type":"artifact_chunk","request_id":request_id,"result":{"ok":False,"error":{"code":"AGENT_MAINTENANCE","message":"CodePier 正在安全切换，请稍后重试"}}})
                            continue
                        if not isinstance(request_id,str) or len(request_id)!=32:
                            raise ValueError("Invalid artifact request ID")
                        if len(self.transfer_tasks)>=4:
                            await self.send({"type":"artifact_chunk","request_id":request_id,"result":{"ok":False,"error":{"code":"ARTIFACT_BUSY","message":"产物通道繁忙"}}})
                        else:
                            task=asyncio.create_task(self.send_artifact(data,socket))
                            self.transfer_tasks.add(task)
                            task.add_done_callback(self.transfer_done)
                        continue
                    if kind == "call":
                        if id not in self.jobs:
                            task = asyncio.create_task(self.handle(data))
                            self.jobs[id] = task
                            task.add_done_callback(lambda done, key=id: self.job_done(key, done))
                        else:
                            await self.report_status(id)
                    elif kind == "probe":
                        await self.report_status(data["id"])
                    elif kind == "ack":
                        lifecycle_started = False
                        if self.lifecycle.has_pending(data["id"]):
                            try:
                                lifecycle_started = await self.lifecycle.acknowledge(data["id"])
                            except Exception as exc:
                                # Keep the durable result in the outbox. A repeated Hub ACK
                                # will retry the handoff without creating another operation.
                                print(f"[Agent] 生命周期操作交接失败，将等待重试：{type(exc).__name__}", file=sys.stderr, flush=True)
                                continue
                        self.journal.ack(data["id"])
                        if lifecycle_started:
                            self.stop_event.set()
                            with contextlib.suppress(Exception):
                                await socket.close(code=1000, reason="Agent lifecycle change")
                    elif kind == "cancel":
                        self.cancel_call(id)
            finally:
                self.computer_approvals.cancel()
                native_sync.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await native_sync
                heartbeat.cancel()
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                finally:
                    if self.socket is socket:
                        self.socket = self.channel = None

    def cancel_call(self, id):
        self.journal.cancel(id)
        self.cancelled.add(id)
        process = self.processes.get(id)
        if process:
            task = asyncio.create_task(self.kill_process(process, include_finished=True))
            self.kill_tasks.add(task)
            task.add_done_callback(self.kill_done)
        job = self.jobs.get(id)
        # Wake accepted work waiting behind a long task. Never cancel a thread
        # already mutating files: it must keep its project lock until it finishes.
        if job and id not in self.finishing and self.journal.status(id)["status"] == "accepted":
            job.cancel()
        if not job:
            self.cancelled.discard(id)

    def kill_done(self, task):
        self.kill_tasks.discard(task)
        if not task.cancelled() and task.exception():
            print(f"[Agent] 停止任务进程失败：{type(task.exception()).__name__}", file=sys.stderr, flush=True)

    def job_done(self, id, task):
        self.jobs.pop(id, None)
        self.finishing.discard(id)
        self.cancelled.discard(id)
        if not task.cancelled() and task.exception():
            # Storage errors must be observable; never send success without a durable result.
            print(f"[Agent] 操作 {id} 结果持久化异常：{type(task.exception()).__name__}", file=sys.stderr, flush=True)

    @staticmethod
    def _projects_overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    def _slot_conflicts(self, root: Path, write: bool, token: object) -> bool:
        for active_root, active_write in self._active_slots.values():
            if self._projects_overlap(root, active_root) and (write or active_write):
                return True
        # Readers yield to every queued overlapping writer. Writers only yield
        # to EARLIER overlapping writers: yielding to later ones creates a cycle
        # when multiple writes accumulate behind an active shared reader.
        for waiting_token, waiting_root in self._waiting_writes:
            if waiting_token is token:
                break
            if self._projects_overlap(root, waiting_root):
                return True
        return False

    async def _acquire_project_slot(self, root: Path, write: bool, token: object):
        async with self.project_condition:
            await self.project_condition.wait_for(lambda: not self._slot_conflicts(root, write, token))
            self._active_slots[token] = (root, write)
            self.active_roots.add(root)

    async def _release_project_slot(self, root: Path, token: object):
        async with self.project_condition:
            self._active_slots.pop(token, None)
            if not any(active_root == root for active_root, _ in self._active_slots.values()):
                self.active_roots.discard(root)
            self.project_condition.notify_all()

    @contextlib.asynccontextmanager
    async def project_slot(self, root, write=True, operation_id=None):
        path = Path(root).expanduser().resolve().as_posix()
        if os.name == 'nt': path = path.casefold()
        async with self._legacy_project_slot(root, write=write, operation_id=operation_id):
            async with self.resources.slot(operation_id or 'local', [Claim('agent', 'path', path, write)],
                    lambda blockers: self.phase(operation_id, 'waiting_resource', blocked_by=blockers) if operation_id else None):
                yield

    @contextlib.asynccontextmanager
    async def execution_slot(self, identifier, tool, project, args, root, write, deadline):
        # Only queue acquisition has this deadline. Once execution begins the
        # command timeout / file commit rules own cancellation and durability.
        timeout = max(0, deadline - time.time()) if deadline is not None else None
        async with contextlib.AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(timeout) as timer:
                    # Imports publish one destination. A project-wide writer
                    # here also stalls unrelated projects under a mapped parent.
                    if tool in CORE_REMOTE or tool == 'download_artifact':
                        claims = claims_for(self.engine, tool, project, args, root)
                        await stack.enter_async_context(self.resources.slot(identifier, claims,
                            lambda blockers: self.phase(identifier, 'waiting_resource', blocked_by=blockers)))
                    else:
                        await stack.enter_async_context(self.project_slot(root, write=write, operation_id=identifier))
                    self.phase(identifier, 'waiting_worker')
                    worker = self.read_semaphore if tool in {'read', 'write', 'edit'} else self.semaphore
                    await stack.enter_async_context(worker)
                    timer.reschedule(None)
            except TimeoutError as exc:
                raise DevError('QUEUE_EXPIRED', '超过首次执行期限，未开始操作') from exc
            yield

    @contextlib.asynccontextmanager
    async def _legacy_project_slot(self, root, write=True, operation_id=None):
        """Coordinate overlapping project paths with shared reads and exclusive writes.

        Named tasks remain exclusive by default. A task explicitly marked
        ``allow_read_concurrency`` is admitted as a shared reader: reads may
        proceed while it runs, but writes still wait for it to finish.
        """
        root = Path(root).expanduser().resolve()
        token = object()
        if operation_id:
            self.slot_owners[token] = operation_id
        lock = self.project_locks.setdefault(str(root), asyncio.Lock()) if write else None
        if write:
            self._waiting_writes.append((token, root))
        acquired = False
        lock_acquired = False
        try:
            if lock is not None:
                await lock.acquire()
                lock_acquired = True
            await self._acquire_project_slot(root, write, token)
            acquired = True
            if write:
                self._waiting_writes = [(waiting_token, waiting_root)
                                        for waiting_token, waiting_root in self._waiting_writes
                                        if waiting_token is not token]
                async with self.project_condition:
                    self.project_condition.notify_all()
            yield
        finally:
            if acquired:
                await self._release_project_slot(root, token)
            elif write:
                self._waiting_writes = [(waiting_token, waiting_root)
                                        for waiting_token, waiting_root in self._waiting_writes
                                        if waiting_token is not token]
                async with self.project_condition:
                    self.project_condition.notify_all()
            self.slot_owners.pop(token, None)
            if lock_acquired:
                lock.release()

    async def handle(self, request):
        id = request["id"]
        not_after = request.get("not_after")
        tool_contract_version = request.get("tool_contract_version")
        # Hub-authenticated delivery metadata is not part of the journal request
        # fingerprint: tightening policy must not break receipt recovery.
        execution_policy = request.get("execution_policy")
        integration_context = request.get('integration_context')
        core_ssh = request.get('core_ssh')
        request = {k: request.get(k) for k in ("tool", "project", "args")}
        try:
            previous = self.journal.start(id, request)
            if previous is not None:
                await self.send({"type": "result", "id": id, "result": previous})
                return
        except DevError as exc:
            if exc.code == "ALREADY_RUNNING":
                await self.report_status(id)
            else:
                await self.send({"type": "result", "id": id, "result": {"ok": False, "error": {"code": exc.code, "message": exc.message}}})
            return
        try:
            if self.lifecycle.handoff_pending or self.lifecycle.base and (self.lifecycle.base / ".install.lock").exists():
                raise DevError("AGENT_MAINTENANCE", "Agent 正在本机安装、升级或卸载，请完成后重试", 409)
            self.phase(id, "accepted")
            await self.send({"type": "accepted", "id": id})
            tool, project, args = request["tool"], request["project"], request["args"]
            if not isinstance(tool, str) or not isinstance(project, dict) or not isinstance(args, dict):
                raise DevError("INVALID_REQUEST", "操作必须包含 tool、project 和 args 对象")
            validate_call_epoch(tool, tool_contract_version)
            # Never accept a project/argument-supplied exemption. Legacy frames
            # without metadata still obey the independently configured local floor.
            project = {**project, "_execution_policy": execution_policy, "_core_ssh": core_ssh, '_integration_admin': False, '_integration_owner': None}
            if isinstance(integration_context, dict):
                owner = integration_context.get('owner')
                if not isinstance(owner, str) or not 1 <= len(owner) <= 600 or integration_context.get('device_id') != self.config['device_id']:
                    raise DevError('INTEGRATION_IDENTITY', '经过认证的执行身份无效', 403)
                admin = integration_context.get('admin') is True and isinstance(execution_policy, dict) and execution_policy.get('origin') == 'panel'
                project.update(_integration_owner=owner, _integration_admin=admin, _coding_owner=owner,
                    _coding_device=integration_context['device_id'], device_id=integration_context['device_id'],
                    _coding_scopes=integration_context.get('scopes', []))
                self.integrations.remember_project(project)
            if args.get('workspace_id'):
                project = self.integrations.project(project, args)
            self.integration_projects[id] = project
            self.integration_tool_names[id] = tool
            self.integrations.control.guard(tool, project)
            if not_after is not None and (type(not_after) not in {int, float} or not math.isfinite(not_after)):
                raise DevError("INVALID_REQUEST", "操作期限必须是有限时间戳")
            if id in self.cancelled or self.journal.is_cancelled(id) or self.stop_event.is_set():
                raise DevError("CANCELLED", "操作开始前已取消")
            if not_after is not None and time.time() > not_after:
                raise DevError("QUEUE_EXPIRED", "超过首次执行期限，尚未开始操作；请确认后重新提交")
            if tool == "system_validate":
                root, spec = self.engine.root(project)
                self.journal.mark_running(id)
                result = {"root": str(root), "writable": spec.get("writable", True), "allow_tasks": spec.get("allow_tasks", False)}
            elif tool in DEVICE_ACTIONS:
                if getattr(self.computer, 'session', None):
                    raise DevError("COMPUTER_BUSY", "请先结束桌面控制会话，再更新、重启或卸载 Agent", 409)
                live_cli = self.native.live()
                if live_cli:
                    raise DevError("CLI_BUSY", "先显式停止原生会话，再更新、重启或卸载 Agent", 409, sessions=[{"id":r["id"],"project_id":r["project_id"]} for r in live_cli])
                if any(job_id != id for job_id in self.jobs) or self.processes:
                    raise DevError("AGENT_BUSY", "设备仍有本机操作在执行；等待完成后再管理 Agent")
                self.journal.mark_running(id)
                await self.send({"type": "started", "id": id})
                self.phase(id, "preparing_lifecycle", action=tool)
                result = await self.lifecycle.prepare(id, tool, args)
                self.phase(id, "lifecycle_prepared", action=tool)
            elif tool not in TOOLS or TOOLS[tool].local:
                raise DevError("UNKNOWN_TOOL", "该操作不能由 Agent 执行")
            elif tool in {"execution_info", "agent_diagnostics"}:
                TOOLS[tool].model.model_validate(args)
                root, spec = self.engine.root(project)
                self.journal.mark_running(id)
                result = ({"build": self.build.describe(), "running_jobs": len(self.jobs), "telemetry_errors": self.telemetry.errors}
                          if tool == "agent_diagnostics" else execution_info(self.config, project, spec, root))
            elif tool in COMPUTER_TOOLS:
                args = TOOLS[tool].model.model_validate(args).model_dump()
                self.engine.root(project, tool in MUTATING and tool != "computer_session_close")
                if agent_blocks_codex(self.config, project) and computer_denial(tool, args):
                    raise DevError("CODEX_REMOTE_DISABLED", DENIAL_MESSAGE, 403)
                self.journal.mark_running(id)
                await self.send({"type": "started", "id": id})
                self.phase(id, "executing")
                result = await self.computer.execute(tool, project, args, not_after=not_after, operation_id=id,
                    phase=lambda stage, **detail: self.phase(id, stage, **detail))
            elif tool in {"tasks_list", "searches_get", "searches_cancel", 'readiness_get', 'lsp_status', 'browser_status', 'integration_control', 'validations_list', 'browser_close'}:
                # Config/status queries do not read project contents. Keep them
                # available while a shell owns the project and all workers are busy.
                args = TOOLS[tool].model.model_validate(args).model_dump()
                self.engine.root(project)
                self.journal.mark_running(id)
                await self.send({"type": "started", "id": id})
                self.phase(id, "executing")
                result = await self.execute(id, tool, project, args)
            else:
                args = TOOLS[tool].model.model_validate(args).model_dump()
                root, _ = self.engine.root(project, tool in MUTATING)
                # Wait for the project BEFORE consuming a global worker slot: a queue
                # of Imago tests must not starve a read of Nexus or Lumen.
                slot_write = tool in MUTATING
                if tool == "tasks_run":
                    task_config = self.config.get("tasks", {}).get(args["task"], {})
                    slot_write = not task_config.get("allow_read_concurrency", False)
                blockers = [self.slot_owners[token] for token, (active_root, active_write) in self._active_slots.items()
                            if token in self.slot_owners and self._projects_overlap(root, active_root) and (slot_write or active_write)]
                self.phase(id, "waiting_project", blocked_by=blockers[:64])
                async with self.execution_slot(id, tool, project, args, root, slot_write, not_after):
                    if id in self.cancelled or self.journal.is_cancelled(id) or self.stop_event.is_set():
                        raise DevError("CANCELLED", "操作开始前已取消")
                    if not_after is not None and time.time() > not_after:
                        raise DevError("QUEUE_EXPIRED", "超过首次执行期限，尚未开始操作；请确认后重新提交")
                    self.integrations.control.guard(tool, project)
                    if args.get('workspace_id'): project = self.integrations.project(project, args)
                    current_root, _ = self.engine.root(project, tool in MUTATING)
                    if current_root != root:
                        raise DevError("ROOT_CHANGED", "排队期间项目根目录发生变化，请确认映射后重新提交")
                    self.journal.mark_running(id)
                    await self.send({"type": "started", "id": id})
                    if id in self.cancelled or self.journal.is_cancelled(id) or self.stop_event.is_set():
                        raise DevError("CANCELLED", "操作开始前已取消")
                    self.integrations.control.guard(tool, project)
                    if args.get('workspace_id'): project = self.integrations.project(project, args)
                    current_root, _ = self.engine.root(project, tool in MUTATING)
                    if current_root != root:
                        raise DevError("ROOT_CHANGED", "项目根目录发生变化，请确认映射后重新提交")
                    self.phase(id, "executing")
                    result = await self.execute(id, tool, project, args)
            result = {"ok": True, "data": result}
        except asyncio.CancelledError:
            result = {"ok": False, "error": {"code": "CANCELLED" if self.journal.is_cancelled(id) else "INTERRUPTED", "message": "操作已取消" if self.journal.is_cancelled(id) else "Agent 进程被停止；检查本机结果后再决定是否重做"}}
        except DevError as exc:
            result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            if isinstance(request.get('tool'), str) and request['tool'] in COMPUTER_TOOLS:
                result['error'].update(safe_detail(exc.details))
                if exc.details.get('next') == 'computer_observe':
                    result['error']['next'] = 'computer_observe'
                if exc.details.get('input_sent') is False:
                    result['error']['input_sent'] = False
        except Exception as exc:
            # Pydantic model errors may include raw input (including passwords).
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                issues = [{"field": '.'.join(map(str, e['loc'])), "message": e['msg']}
                          for e in exc.errors()]
                result = {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": json.dumps(issues, ensure_ascii=False)[:1500]}}
            else:
                result = {"ok": False, "error": {"code": "AGENT_ERROR", "message": str(exc)[:1500]}}
        finally:
            self.cancelled.discard(id)
        self.finishing.add(id)
        self.phase(id, "persisting")
        result["trace"] = self.telemetry.snapshot(id)
        delay = 1
        while True:
            try:
                self.journal.finish(id, result)
                break
            except (sqlite3.Error, OSError) as exc:
                # Keep the real result in this job while a transient disk fault
                # recovers; reporting running forever after dropping it loses
                # the only safe way to acknowledge an already executed write.
                print(f"[Agent] 操作 {id} 结果暂时无法持久化，将重试：{type(exc).__name__}", file=sys.stderr, flush=True)
                if self.stop_event.is_set():
                    raise
                try:
                    await asyncio.wait_for(self.stop_event.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30)
        self.phase(id, "result_ready")
        self.telemetry.starts.pop(id, None)
        self.pending_output.pop(id, None)
        self.finishing.discard(id)
        self.integration_projects.pop(id, None)
        self.integration_tool_names.pop(id, None)
        await self.send({"type": "result", "id": id, "result": result})

    def transfer_done(self,task):
        self.transfer_tasks.discard(task)
        if not task.cancelled():task.exception()

    async def send_artifact(self,request,socket):
        try:
            work=asyncio.create_task(asyncio.to_thread(self.artifacts.chunk,request["project"],request["artifact_id"],request["offset"]))
            try:data=await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise
            result={"ok":True,"data":data}
        except DevError as exc:
            result={"ok":False,"error":{"code":exc.code,"message":exc.message}}
        except (OSError,KeyError,TypeError):
            result={"ok":False,"error":{"code":"ARTIFACT_UNAVAILABLE","message":"产物快照不可读取"}}
        if self.socket is socket:
            await self.send({"type":"artifact_chunk","request_id":request["request_id"],"result":result})

    async def execute(self, id, tool, project, args):
        from shared.integration_contracts import REMOTE_TOOLS
        if tool == 'exec':
            if args['task']:
                from shared.contracts import Task
                forwarded = Task.model_validate({k: v for k, v in args.items() if k in Task.model_fields}).model_dump()
                result = await self.execute(id, 'tasks_run', project, forwarded)
            elif args['target'] == 'agent':
                from shared.contracts import ShellExec
                forwarded = ShellExec.model_validate({k: v for k, v in args.items() if k in ShellExec.model_fields}).model_dump()
                result = await self.execute(id, 'shell_exec', project, forwarded)
            else:
                from shared.contracts import SSHExec
                if not isinstance(project.get('_core_ssh'), dict):
                    raise DevError('VPS_TRANSPORT_MISSING', '缺少 Hub 验证的 VPS 连接，未执行命令', 409)
                forwarded = SSHExec.model_validate(project['_core_ssh']).model_dump()
                result = await self.execute(id, 'ssh_exec', project, forwarded)
            output = result.get('output', '')
            tail = '\n'.join(output.split('\n')[-2000:]).encode('utf-8')[-50 * 1024:].decode('utf-8', errors='ignore')
            return {**result, 'output': tail, 'output_truncated': bool(result.get('output_truncated') or tail != output),
                    'target': args['target'], 'resource_coordination': 'declared' if args['resources'] else 'independent'}
        if tool in {'read', 'write', 'edit'}:
            work = asyncio.create_task(asyncio.to_thread(CoreFiles(self.engine).call, tool, project, args))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                return await work
        if tool in REMOTE_TOOLS:
            return await self.integrations.execute(id, tool, project, args)
        if tool in {"searches_start", "searches_get", "searches_cancel"}:
            method = self.searches.start if tool == "searches_start" else self.searches.get if tool == "searches_get" else self.searches.cancel
            params = (id, project, args) if tool == "searches_start" else (project, args)
            work=asyncio.create_task(asyncio.to_thread(method,*params))
            try:return await asyncio.shield(work)
            except asyncio.CancelledError:return await work
        if tool == "artifacts_register":
            work=asyncio.create_task(asyncio.to_thread(self.artifacts.register,id,project,args))
            try:return await asyncio.shield(work)
            except asyncio.CancelledError:return await work
        if tool == "ssh_exec":
            from agent.ssh import prepare_ssh, ssh_error
            root, spec = self.engine.root(project, True)
            command, env = prepare_ssh(self.config, project, spec, args)
            result = await self.run_process(id, command, root, args['timeout_seconds'], stream=True,
                                            task_env=env, full_access=True,
                                            inherit_env=self.config['shell']['inherit_env'], execution_project=project)
            return {**result, 'host': args['host'], 'port': args['port'], 'username': args['username'],
                    'ssh_error': ssh_error(result)}
        if tool == "shell_exec":
            root, spec = self.engine.root(project, True)
            command, cwd, env = prepare_shell(self.config, project, spec, root, args)
            result = await self.run_process(id, command, cwd, args["timeout_seconds"], stream=True,
                                            task_env=env, full_access=True,
                                            inherit_env=self.config["shell"]["inherit_env"], execution_project=project)
            return {**result, "cwd": str(cwd), "shell": command[:-1]}
        if tool == "tasks_list":
            _, spec = self.engine.root(project)
            enabled = project.get("allow_tasks", False) and spec.get("allow_tasks", False)
            return {"enabled": enabled, "tasks": [{"name": name, "description": t.get("description", ""), "command": t.get("command"), "timeout": t.get("timeout", 300), "cwd": t.get("cwd", "."), "executable_available": bool(shutil.which(t["command"][0])), "environment_keys": sorted(t.get("env", {})), "allow_read_concurrency": bool(t.get("allow_read_concurrency", False))}
                    for name, t in self.config.get("tasks", {}).items() if enabled and self.task_allowed(t, project)],
                    "warning": "Tasks execute project code with the Agent user's OS permissions. Not an OS sandbox."}
        if tool == "tasks_run":
            root, spec = self.engine.root(project, True)
            if not project.get("allow_tasks") or not spec.get("allow_tasks"):
                raise DevError("TASKS_DISABLED", "面板项目与本机目录必须同时允许执行任务", 403)
            task = self.config.get("tasks", {}).get(args["task"])
            if not task or not self.task_allowed(task, project):
                raise DevError("TASK_NOT_ALLOWED", "任务未在本机为此项目授权", 403)
            command = task.get("command")
            if not isinstance(command, list) or not command or any(not isinstance(x, str) or "\x00" in x for x in command):
                raise DevError("TASK_CONFIG", "本机 command 必须是命令参数数组")
            cwd = self.engine.path(root, task.get("cwd", "."))
            timeout = max(1, min(int(task.get("timeout", 300)), 3600))
            return await self.run_process(id, command, cwd, timeout, stream=True,
                                          task_env=task.get("env", {}), execution_project=project)
        if tool.startswith("git_"):
            root, _ = self.engine.root(project)
            # Git is only used for fixed read operations. Disable external helpers and hooks.
            flags = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + str(self.state_dir / "no-hooks"), "-c", "diff.external=", "-c", "core.pager=cat"]
            if tool == "git_status":
                cmd = flags + ["status", "--porcelain=v1", "--untracked-files=normal"]
            elif tool == "git_diff":
                cmd = flags + ["diff", "--no-ext-diff", "--no-textconv", "--no-color"] + (["--cached"] if args["staged"] else [])
                # Pathspec exclusions keep common credential files out of diffs.
                cmd += ["--", ".", ":(glob,exclude)**/.env", ":(glob,exclude)**/.env.*", ":(glob,exclude)**/*.pem", ":(glob,exclude)**/*.key", ":(glob,exclude)**/.npmrc", ":(glob,exclude)**/.ssh/**", ":(glob,exclude)**/.aws/**", ":(glob,exclude)**/.kube/**", ":(glob,exclude)**/*.p12", ":(glob,exclude)**/*.pfx", ":(glob,exclude)**/id_rsa", ":(glob,exclude)**/id_ed25519", ":(glob,exclude)**/.netrc", ":(glob,exclude)**/.pypirc", ":(glob,exclude)**/.azure/**", ":(glob,exclude)**/.remote-dev*/**", ":(glob,exclude)**/.codepier*/**"]
            else:
                cmd = flags + ["log", f"-{args['limit']}", "--format=%h %ad %an %s", "--date=iso-strict", "--no-show-signature"]
            result = await self.run_process(id, cmd, root, 20)
            if result["exit_code"] != 0:
                raise DevError("GIT_ERROR", result["output"][-1500:] or "Git 命令失败")
            return result
        work = asyncio.create_task(asyncio.to_thread(self.engine.call, tool, project, args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # A Python thread cannot be cancelled. Keep the project lock and
            # persist its actual outcome before allowing another mutation.
            return await work

    @staticmethod
    def task_allowed(task, project):
        allowed = task.get("projects", [])
        return "*" in allowed or project.get("alias") in allowed or project.get("id") in allowed

    async def run_process(self, id, command, cwd, timeout, stream=False, task_env=None,
                          full_access=False, inherit_env=False, execution_project=None):
        from shared.secret_output import SecretOutput
        environment = {k: v for k, v in os.environ.items() if inherit_env or k in {
            "PATH", "HOME", "USER", "USERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "SystemRoot",
            "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "SHELL", "NVM_DIR"}}
        environment.setdefault("GIT_TERMINAL_PROMPT", "0")
        if not full_access:
            environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "NO_COLOR": "1", "CI": "1"})
        # Full-access shell can override env; named tasks remain local-config-only.
        environment.update(task_env or {})
        output_filter = SecretOutput(environment.get('SSHPASS', ''))
        if execution_project is not None:
            # Inspect the exact argv, cwd and environment used below, including
            # local task definitions, PATH overrides and resolved symlink targets.
            enforce_argv(self.config, execution_project, command, cwd, environment)
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(*command, cwd=cwd, env=environment,
                      stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.STDOUT, **extra))
        try:
            # Cancelling subprocess creation can leave a live child without a
            # Process handle. Keep creation alive until we can reap that child.
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                process = await spawning
                await self.kill_process(process, include_finished=True)
            raise
        except FileNotFoundError as exc:
            raise DevError("COMMAND_MISSING", f"找不到本机命令 {command[0]}，请安装或使用绝对路径") from exc
        self.processes[id] = process
        total = 0
        start = time.monotonic()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        decoded_output = ""
        seq = 0
        last_snapshot = 0.0
        def snapshot(force=False):
            nonlocal seq, last_snapshot
            if not stream or not force and time.monotonic() - last_snapshot < .25:
                return
            seq += 1
            last_snapshot = time.monotonic()
            self.journal.update_output(id, decoded_output, seq)
            self.pending_output[id] = {"type": "output", "id": id, "snapshot": True,
                                       "seq": seq, "text": decoded_output}
            self.output_wake.set()

        async def drain():
            nonlocal total, decoded_output
            while True:
                # Read available bytes in larger batches; StreamReader.read does not wait to fill n.
                chunk = await process.stdout.read(65536)
                if not chunk:
                    decoded_output = (decoded_output + output_filter.feed(decoder.decode(b"", final=True), final=True))[-131072:]
                    snapshot(True)
                    return
                total += len(chunk)
                decoded_output = (decoded_output + output_filter.feed(decoder.decode(chunk)))[-131072:]
                snapshot()
                # stdout may remain immediately readable; let heartbeat/cancel run.
                await asyncio.sleep(0)
        async def exited():
            # Process.wait() may wait for inherited stdout to close as well as
            # process exit. Observe the leader so its children cannot extend a
            # completed task to its full timeout by retaining the pipe.
            while process.returncode is None:
                await asyncio.sleep(.05)

        reader = waiter = None
        timed_out = False
        try:
            seq = self.journal.status(id).get("output_seq", 0)
            reader = asyncio.create_task(drain())
            waiter = asyncio.create_task(exited())
            if id in self.cancelled or self.journal.is_cancelled(id) or self.stop_event.is_set():
                await self.kill_process(process, include_finished=True)
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    # A disk error in a streaming snapshot must stop the child
                    # immediately, otherwise its undrained pipe can deadlock it.
                    reader.result()
                await waiter
            try:
                await asyncio.wait_for(asyncio.shield(reader), 3)
            except asyncio.TimeoutError:
                await self.kill_process(process, include_finished=True)
        except TimeoutError:
            timed_out = True
        finally:
            try:
                await self.kill_process(process, include_finished=True)
                if reader is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(reader), 1)
                    except asyncio.TimeoutError:
                        reader.cancel()
            finally:
                for task in (reader, waiter):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(t for t in (reader, waiter) if t is not None), return_exceptions=True)
                self.processes.pop(id, None)
        return {"exit_code": process.returncode, "output": decoded_output,
                "output_truncated": total > 128 * 1024, "timed_out": timed_out, "cancelled": id in self.cancelled or self.journal.is_cancelled(id),
                "duration_ms": round((time.monotonic() - start) * 1000), "command": [output_filter.redact(value) for value in command]}

    @staticmethod
    async def kill_process(process, include_finished=False):
        if process.returncode is not None and not include_finished:
            return
        if os.name == "nt":
            # Windows taskkill resolves a PID, not a process-group identity.
            # Once the leader exits that PID may already belong to another app.
            if process.returncode is not None:
                return
            killer = None
            try:
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), 3)
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                if killer is not None and killer.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        killer.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(killer.wait(), 1)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
        else:
            def signal_group(sig):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS may report EPERM once an exited leader's group has
                    # been reaped. There is no owned live leader left to kill.
                    if process.returncode is None:
                        raise

            signal_group(signal.SIGTERM)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    pass
            signal_group(signal.SIGKILL)
        if process.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5)

    async def run(self):
        await self.integrations.start()
        self.computer.start_guard()
        watcher = asyncio.create_task(self.watch_config())
        sender = asyncio.create_task(self.output_sender())
        stopping = asyncio.create_task(self.stop_event.wait())
        from agent.brand_upgrade import watch as watch_brand_upgrade
        branding = asyncio.create_task(watch_brand_upgrade(self))
        connection = None
        delay = 1
        try:
            while not self.stop_event.is_set():
                before = self.connection_number
                try:
                    connection = asyncio.create_task(self.connect_once())
                    await asyncio.wait({connection, stopping}, return_when=asyncio.FIRST_COMPLETED)
                    if self.stop_event.is_set():
                        connection.cancel()
                    await connection
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S%z')}] [Agent] 连接断开：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr, flush=True)
                if self.connection_number != before:
                    delay = 1
                try:
                    await asyncio.wait_for(self.stop_event.wait(), delay + random.random())
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30)
        finally:
            self.stop_event.set()
            await self.computer.close()
            await self.integrations.close()
            background = [watcher, sender, stopping, branding] + ([connection] if connection else [])
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            try:
                self.cancelled.update(self.jobs)
                for id, job in list(self.jobs.items()):
                    if id not in self.finishing and self.journal.status(id)["status"] == "accepted":
                        job.cancel()
                await asyncio.gather(*(self.kill_process(p, include_finished=True)
                                       for p in list(self.processes.values())), return_exceptions=True)
                # Finish in-flight file operations and persist outcomes before exit.
                await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
                await asyncio.gather(*list(self.kill_tasks), return_exceptions=True)
                await asyncio.gather(*list(self.transfer_tasks), return_exceptions=True)
            finally:
                try:
                    self.journal.db.close()
                finally:
                    self.instance_lock.close()

    async def stop(self):
        self.stop_event.set()
        if self.socket:
            await self.socket.close()
