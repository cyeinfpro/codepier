"""Terminal ownership, authority leases, idempotency and attachment fencing."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from hub import iam
from shared.crypto import digest, token
from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import (
    FrameKind,
    TerminalFrame,
    TerminalProtocolError,
    canonical_uuid,
)
from shared.util import VERSION, DevError

from .diagnostics import TerminalMetrics, counter, queue_bytes, safe_metadata, session_snapshot
from .repository import TERMINAL_STATES, TerminalRepository


@dataclass
class HubSession:
    row: dict
    principal: object
    project: dict
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    attachment_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attachment: object = None
    challenge: dict | None = None
    challenge_until: float = 0
    render_epoch: int = 0
    create_task: object = None
    revoked: bool = False
    authority_revision: str = field(default_factory=lambda: uuid.uuid4().hex)
    metrics: TerminalMetrics = field(default_factory=TerminalMetrics)
    diagnostic_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    diagnostic_next_probe: float = 0


class TerminalService:
    def __init__(self, runtime, auth, *, config=None, clock=time.monotonic):
        self.runtime, self.auth, self.store = runtime, auth, runtime.store
        self.config, self.clock = config or TerminalConfig.from_env(), clock
        self.repository = TerminalRepository(self.store)
        self.sessions: dict[str, HubSession] = {}
        self.agents = {}
        self.tickets = {}
        self.browser_socket_slots = {}
        self.agent_handshake_slots = {}
        self.diagnostic_slots = {}
        self.metrics = TerminalMetrics()
        self.admission = asyncio.Lock()
        self.tasks = set()
        self.lease_task = None
        self.closing = False

    @property
    def enabled(self):
        return os.getenv("CODEPIER_SSH_TERMINAL_ENABLED", "1").lower() in {"1", "true", "on", "yes"}

    def _guard_maintenance(self):
        gate = getattr(self.runtime, "panel_maintenance", None)
        if gate is not None:
            gate.guard()

    def maintenance_busy(self):
        # Health is read both on the event loop and in Store workers. Copy the
        # loop-owned collections before inspecting them; never wait for their
        # async locks or issue an Agent RPC from this synchronous drain check.
        sessions = tuple(self.sessions.copy().values())
        tasks = tuple(self.tasks.copy())
        if any(not task.done() for task in tasks):
            return True
        for session in sessions:
            if (
                session.attachment is not None
                or session.attachment_lock.locked()
                or session.create_task is not None
                and not session.create_task.done()
            ):
                # EOF can already be durable while the browser still has an
                # ordered output/exit tail or detach cleanup in progress.
                return True
        # Cached history can be evicted, and a Hub restart loses every Python
        # object. Durable unknown/retained tmux sessions must still prevent a
        # managed stop. Only a confirmed end/missing result retires tmux.
        # Store.one uses its connection-local lock, including in read workers.
        return (
            self.store.one(
                "SELECT 1 AS busy FROM vps_terminal_sessions "
                "WHERE state NOT IN ('exited','failed') OR mode NOT IN ('direct','tmux') "
                "OR (mode='tmux' AND CASE WHEN json_valid(metadata) THEN "
                "COALESCE(json_extract(metadata,'$.remote_session_state'),'unknown') "
                "NOT IN ('ended','missing') ELSE 1 END) LIMIT 1"
            )
            is not None
        )

    def reserve_browser_socket(self):
        # Reserve before the first DB await. The count includes accepted,
        # authenticating and waiting-for-attachment sockets, with room for a
        # transient overlap during reconnect. All mutations run in one loop.
        self._guard_maintenance()
        if self.closing or len(self.browser_socket_slots) >= 2 * self.config.max_sessions_per_hub:
            raise DevError("TERMINAL_CONNECTION_LIMIT", "终端连接过多，请稍后重试", 429)
        slot = object()
        self.browser_socket_slots[slot] = None
        return slot

    def bind_browser_socket(self, slot, user_id):
        self._guard_maintenance()
        if not user_id or slot not in self.browser_socket_slots:
            raise DevError("AUTH_REQUIRED", "请从面板登录后打开终端", 401)
        if (
            sum(owner == user_id for owner in self.browser_socket_slots.values())
            >= 2 * self.config.max_sessions_per_user
        ):
            raise DevError("TERMINAL_CONNECTION_LIMIT", "当前用户终端连接过多，请稍后重试", 429)
        self.browser_socket_slots[slot] = user_id

    def release_browser_socket(self, slot):
        self.browser_socket_slots.pop(slot, None)

    def reserve_agent_handshake(self, device_id):
        if (
            self.closing
            or len(self.agent_handshake_slots) >= max(16, 2 * self.config.max_sessions_per_hub)
            or sum(owner == device_id for owner in self.agent_handshake_slots.values()) >= 2
        ):
            raise DevError("TERMINAL_CONNECTION_LIMIT", "节点终端认证连接过多，请稍后重试", 429)
        slot = object()
        self.agent_handshake_slots[slot] = device_id
        return slot

    def release_agent_handshake(self, slot):
        self.agent_handshake_slots.pop(slot, None)

    def observe(self, session, name, *, milliseconds=None):
        for metrics in (self.metrics, session.metrics):
            if milliseconds is None:
                metrics.increment(name)
            else:
                metrics.observe(name, milliseconds)

    def observe_rejection(self, session, reason):
        self.observe(session, "protocol_reject_total")
        if reason == "HISTORY_GAP":
            self.observe(session, "history_gap_total")

    def observe_backpressure(self, session):
        self.observe(session, "backpressure_total")

    async def _audit(self, session, action, *, principal=None, outcome="ok"):
        row = session.row
        details = {
            key: row[key]
            for key in ("space_id", "owner_user_id", "project_id", "device_id", "vps_id")
        }
        await self.store.run(
            self.store.audit,
            (principal or session.principal).actor,
            "vps_terminal." + action,
            row["id"],
            outcome,
            details,
        )

    async def _connected(self, session, attachment, elapsed):
        attachment.lifecycle_connected = True
        metadata = json.loads(session.row["metadata"])
        reconnect = metadata.get("connected_at") is not None
        await self.update(
            session,
            {
                "connected_at": time.time(),
                "hub_version": VERSION,
                "protocol_version": 1,
                "reconnect_count": counter(metadata.get("reconnect_count")) + int(reconnect),
                "stage_timings_ms": {"attach": elapsed},
            },
        )
        self.observe(session, "connect_total")
        if reconnect:
            self.observe(session, "reconnect_total")
        await self._audit(session, "connect", principal=attachment.principal)

    async def _disconnected(self, session, attachment):
        if not getattr(attachment, "lifecycle_connected", False):
            return
        attachment.lifecycle_connected = False
        await self.update(session, {"disconnected_at": time.time()})
        self.observe(session, "disconnect_total")
        await self._audit(session, "disconnect", principal=attachment.principal)

    async def _recovered(self, session):
        count = counter(json.loads(session.row["metadata"]).get("recover_count"))
        await self.update(session, {"last_recovered_at": time.time(), "recover_count": count + 1})
        self.observe(session, "recover_total")
        await self._audit(session, "recover")

    def metrics_snapshot(self):
        states = (
            "creating",
            "connecting",
            "ready",
            "detached",
            "closing",
            "exited",
            "failed",
            "frozen",
            "reconnecting",
            "close_unknown",
        )
        active = {mode: dict.fromkeys(states, 0) for mode in ("direct", "tmux")}
        for session in self.sessions.values():
            if (
                session.row["mode"] in active
                and session.row["state"] in active[session.row["mode"]]
            ):
                active[session.row["mode"]][session.row["state"]] += 1
        return {
            "scope": "hub_process",
            "protocol_version": 1,
            **self.metrics.snapshot(),
            "gauges": {
                "cached_sessions": len(self.sessions),
                "sessions_by_mode_state": active,
                "browser_sockets": len(self.browser_socket_slots),
                "agent_handshakes": len(self.agent_handshake_slots),
                "browser_buffer_bytes": sum(
                    queue_bytes(s.attachment.queue) for s in self.sessions.values() if s.attachment
                ),
                "agent_buffer_bytes": sum(
                    queue_bytes(getattr(agent, "queue", None)) for agent in self.agents.values()
                ),
            },
        }

    @contextlib.asynccontextmanager
    async def _diagnostic_slot(self, session, principal):
        if (
            len(self.diagnostic_slots) >= min(16, self.config.max_sessions_per_hub)
            or sum(owner == principal.user_id for owner in self.diagnostic_slots.values()) >= 2
        ):
            raise DevError("DIAGNOSTIC_BUSY", "连接诊断正在进行，请稍后重试", 429)
        sid = session.row["id"]
        self.diagnostic_slots[sid] = principal.user_id
        try:
            yield
        finally:
            self.diagnostic_slots.pop(sid, None)

    async def diagnostics(self, sid, principal):
        session = await self.load(sid, principal)
        await self.authorize(session, principal=principal)
        # One short status probe per session at a time, at most once a second.
        # A GET never calls ensure_ready, renew, create or a remote SSH command.
        if session.diagnostic_lock.locked() or self.clock() < session.diagnostic_next_probe:
            raise DevError("DIAGNOSTIC_BUSY", "连接诊断正在进行，请稍后重试", 429)
        async with session.diagnostic_lock, self._diagnostic_slot(session, principal):
            session.diagnostic_next_probe = self.clock() + 1
            agent = self.agents.get(session.row["device_id"])
            status, rtt, link_reason = {}, None, None
            link_status = "offline"
            if agent and not agent.closed:
                link_status = "unconfirmed"
                started = self.clock()
                try:
                    status = await agent.request(
                        "status",
                        {**self.target_ref(session), "runtime_generation": agent.generation},
                        session.project,
                        wait_seconds=min(2.0, self.config.write_timeout),
                    )
                    if (
                        not isinstance(status, dict)
                        or status.get("session_id", sid) != sid
                        or status.get("runtime_generation", agent.generation) != agent.generation
                    ):
                        status, link_reason = {}, "STALE_GENERATION"
                    elif self.agents.get(session.row["device_id"]) is agent and not agent.closed:
                        rtt = round(max(0, self.clock() - started) * 1000, 3)
                        link_status = "connected"
                        self.observe(session, "agent_control_rtt_ms", milliseconds=rtt)
                        # Diagnostics never changes the operational state or
                        # its revision fence. Normal Agent events/lease status
                        # own reconciliation; this response is a local sample.
                    else:
                        status, link_reason = {}, "AGENT_RECONNECTED"
                except (DevError, TerminalProtocolError) as exc:
                    status, link_reason = {}, exc.code
                except (TimeoutError, ConnectionError):
                    status, link_reason = {}, "AGENT_RESPONSE_UNKNOWN"
            else:
                link_reason = "AGENT_OFFLINE"
            # Permissions or login can be revoked while the control RPC waits.
            await self.authorize(session, principal=principal)
            current_agent = self.agents.get(session.row["device_id"])
            if agent is not current_agent or agent and agent.closed:
                agent, status, rtt = current_agent, {}, None
                link_status = "unconfirmed" if agent and not agent.closed else "offline"
                link_reason = (
                    "AGENT_RECONNECTED" if link_status == "unconfirmed" else "AGENT_OFFLINE"
                )
            self.observe(session, "diagnostics_total")
            return session_snapshot(
                session,
                agent=agent,
                status=status,
                link_status=link_status,
                rtt_ms=rtt,
                link_reason=link_reason,
                hub_version=VERSION,
            )

    def prune_session_cache(self, *, keep=None):
        # Durable identity/idempotency lives in SQLite. Completed idle Python
        # objects do not need to remain resident for the Hub's entire uptime.
        limit = max(128, 2 * self.config.max_sessions_per_hub)
        if len(self.sessions) <= limit:
            return
        ticket_sessions = {item["session"].row["id"] for item in self.tickets.values()}
        candidates = sorted(self.sessions.values(), key=lambda session: session.row["updated"])
        for session in candidates:
            if len(self.sessions) <= limit:
                break
            sid = session.row["id"]
            if (
                sid == keep
                or sid in ticket_sessions
                or sid in self.diagnostic_slots
                or session.attachment is not None
                or session.create_task
                and not session.create_task.done()
                or session.row["state"] not in TERMINAL_STATES
            ):
                continue
            self.sessions.pop(sid, None)

    def spawn(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)

        def done(item):
            self.tasks.discard(item)
            if not item.cancelled():
                item.exception()

        task.add_done_callback(done)
        return task

    async def start(self):
        if self.lease_task is None:
            self.lease_task = asyncio.create_task(self._leases(), name="hub-terminal-authority")

    async def stop(self):
        self.closing = True
        if self.lease_task:
            self.lease_task.cancel()
            await asyncio.gather(self.lease_task, return_exceptions=True)
            self.lease_task = None
        # Closing the data channel does not claim that remote tmux processes
        # ended. The Agent's independent authority deadline fences further I/O.
        for session in tuple(self.sessions.values()):
            if session.attachment:
                await session.attachment.shutdown("HUB_RESTARTING", 1012)
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await asyncio.gather(
            *(agent.close() for agent in tuple(self.agents.values())), return_exceptions=True
        )
        self.agents.clear()
        self.tickets.clear()

    def _authorize(self, principal, project_id, vps_id=None, row=None, *, credentials=False):
        principal = iam.live_principal(self.store, principal)
        if not principal.user_id or not principal.session_hash:
            raise DevError("AUTH_REQUIRED", "请从面板登录后打开终端", 401)
        project = self.runtime.project(project_id, principal)
        self.runtime.authorize(principal, "write", project_id=project["id"])
        self.runtime.authorize(principal, "execute", project_id=project["id"])
        if (
            not project["device_enabled"]
            or project["mode"] != "write"
            or not project["allow_tasks"]
        ):
            raise DevError("SSH_TERMINAL_FORBIDDEN", "项目须启用并允许写入与执行", 403)
        if row is not None:
            if (row["owner_user_id"], row["space_id"]) != (principal.user_id, principal.space_id):
                raise DevError("SESSION_NOT_FOUND", "找不到当前身份的终端会话", 404)
            if row["device_id"] != project["device_id"] or row["project_root"] != project["root"]:
                raise DevError("PROJECT_MAPPING_CHANGED", "项目映射已变化，不能继续原终端", 403)
            ref = {
                "id": row["vps_id"],
                "connection_revision": row["connection_revision"],
                "binding_id": row["binding_id"],
            }
        else:
            ref = self.runtime.vps.reference({"vps": vps_id}, project)
        target = self.runtime.vps._authorized_row(
            {"vps_ref": ref}, project["id"], credential=credentials
        )
        project = {
            **project,
            "_terminal_owner": "user:" + principal.user_id,
            "_terminal_space": principal.space_id,
        }
        if credentials:
            try:
                target = {**target, "password": self.store.decrypt(target["secret"])}
            except Exception as exc:
                raise DevError(
                    "VPS_CREDENTIAL_UNAVAILABLE", "保存的 VPS 凭据不可用，请检查连接设置", 409
                ) from exc
            target.pop("secret", None)
        return principal, project, target

    async def authorize(self, session, *, credentials=False, principal=None):
        if session.revoked and principal is None:
            raise DevError("AUTH_REVOKED", "终端授权已失效", 403)
        _, project, target = await self.store.run(
            self._authorize,
            principal or session.principal,
            session.row["project_id"],
            None,
            session.row,
            credentials=credentials,
        )
        session.project = project
        return target

    def agent(self, device_id):
        connection = self.agents.get(device_id)
        if connection is None or connection.closed:
            raise DevError(
                "AGENT_UPGRADE_REQUIRED", "节点终端通道不可用，请确认节点在线且支持 Web SSH", 409
            )
        return connection

    async def capabilities(self, principal, vps_id, project_id):
        _, project, _ = await self.store.run(self._authorize, principal, project_id, vps_id)
        agent = self.agents.get(project["device_id"])
        available = self.enabled and agent is not None and not agent.closed and not self.closing
        online = project["device_id"] in self.runtime.connections
        return {
            "enabled": self.enabled,
            "available": available,
            "protocol": 1,
            "protocol_version": 1,
            "reason_code": None
            if available
            else "FEATURE_DISABLED"
            if not self.enabled
            else "AGENT_OFFLINE"
            if not online
            else "AGENT_UPGRADE_REQUIRED",
            "runtime_generation": agent.generation if agent and not agent.closed else None,
            "modes": ["auto", "tmux", "direct"],
            "limits": self.config.public_limits(),
            "persistence": "probed_on_connect",
        }

    def _new_row(self, principal, project, target, body, generation, fingerprint):
        now = time.time()
        return {
            "id": uuid.uuid4().hex,
            "space_id": principal.space_id,
            "owner_user_id": principal.user_id,
            "project_id": project["id"],
            "device_id": project["device_id"],
            "vps_id": target["id"],
            "connection_revision": target["connection_revision"],
            "binding_id": target["binding_id"],
            "request_id": body.request_id,
            "request_fingerprint": fingerprint,
            "project_root": project["root"],
            "runtime_generation": generation,
            "requested_mode": body.mode,
            "mode": "direct",
            "state": "creating",
            "reason_code": None,
            "writer_epoch": "0",
            "stream_epoch": "1",
            "cols": body.cols,
            "rows": body.rows,
            "created": now,
            "updated": now,
            "metadata": json.dumps(
                {
                    "vps_name": target["name"],
                    "username": target["username"],
                    "host": target["host"],
                    "port": target["port"],
                    "project_alias": project["alias"],
                    "device_name": project.get("device_name", ""),
                    "hub_version": VERSION,
                    "protocol_version": 1,
                    "recover_count": 0,
                    "reconnect_count": 0,
                },
                ensure_ascii=False,
            ),
        }

    async def create(self, principal, body):
        if not self.enabled or self.closing:
            raise DevError("FEATURE_DISABLED", "新建终端已暂停", 503)
        self._guard_maintenance()
        principal, project, target = await self.store.run(
            self._authorize, principal, body.project_id, body.vps_id
        )
        self._guard_maintenance()
        if (
            not self.config.min_cols <= body.cols <= self.config.max_cols
            or not self.config.min_rows <= body.rows <= self.config.max_rows
        ):
            raise DevError("INVALID_SIZE", "终端尺寸超出配置范围", 400)
        fingerprint = hashlib.sha256(
            json.dumps(body.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        async with self.admission:
            self._guard_maintenance()
            old = await self.store.run(
                self.repository.by_request, principal.space_id, principal.user_id, body.request_id
            )
            if old:
                if old["request_fingerprint"] != fingerprint:
                    raise DevError("REQUEST_CONFLICT", "同一请求编号不能创建不同终端", 409)
                # A lost POST response resolves this exact logical session. It
                # cannot silently open a replacement shell.
                return self.repository.public(old)
            agent = self.agent(project["device_id"])
            total, user, device = await self.store.run(
                self.repository.counts, principal.space_id, principal.user_id, project["device_id"]
            )
            active_devices = await self.store.run(self.repository.active_device_count)
            self._guard_maintenance()
            # Empty Agent connections contain no terminal output. Account the
            # bounded data queues only for devices admitted to live sessions.
            active_devices += int(device == 0)
            if (
                total >= self.config.max_sessions_per_hub
                or user >= self.config.max_sessions_per_user
                or device >= self.config.max_sessions_per_agent
                or (total + 1) * (2 * self.config.hub_relay_bytes + self.config.frame_bytes)
                + active_devices * (4 * self.config.hub_relay_bytes + self.config.frame_bytes)
                > self.config.hub_buffer_bytes
            ):
                raise DevError("SESSION_LIMIT", "终端会话已达到上限，请先结束不再使用的会话", 429)
            row = self._new_row(principal, project, target, body, agent.generation, fingerprint)
            row = await self.store.run(self.repository.insert, row)
            session = HubSession(row, principal, project)
            self.sessions[row["id"]] = session
            self.prune_session_cache(keep=row["id"])
            session.create_task = self.spawn(self._begin(session), "hub-ssh-create-" + row["id"])
            await self._audit(session, "create", principal=principal, outcome="accepted")
            return self.repository.public(row)

    @staticmethod
    def target_ref(session):
        row = session.row
        return {
            "session_id": row["id"],
            "runtime_generation": row["runtime_generation"],
            "vps_id": row["vps_id"],
            "connection_revision": row["connection_revision"],
            "binding_id": row["binding_id"],
            "authority_revision": str(session.authority_revision),
        }

    async def update(self, session, changes, *, allow_generation_change=False):
        async with session.update_lock:
            previous = session.row
            row = await self.store.run(
                self.repository.update,
                session.row["id"],
                changes,
                expected_generation=session.row["runtime_generation"],
                allow_generation_change=allow_generation_change,
            )
            if row:
                session.row = row
                before, after = json.loads(previous["metadata"]), json.loads(row["metadata"])
                for key, measured in safe_metadata(after).get("stage_timings_ms", {}).items():
                    if before.get("stage_timings_ms", {}).get(key) != measured:
                        self.observe(session, key + "_ms", milliseconds=measured)
                for name, key in (("backpressure_total", "backpressure_count"),):
                    delta = max(0, counter(after.get(key)) - counter(before.get(key)))
                    self.metrics.increment(name, delta)
                backpressure = max(
                    0,
                    counter(after.get("backpressure_duration_ms"))
                    - counter(before.get("backpressure_duration_ms")),
                )
                if backpressure:
                    self.observe(session, "backpressure_ms", milliseconds=backpressure)
                if previous["state"] != "exited" and row["state"] == "exited":
                    self.observe(session, "ssh_exit_total")
        if session.row["state"] in {"ready", "detached", "exited", "failed", "close_unknown"}:
            session.ready.set()

    async def _begin(self, session, *, recover=False):
        payload = None
        target = None
        started = self.clock()
        try:
            self._guard_maintenance()
            target = await self.authorize(session, credentials=True)
            self._guard_maintenance()
            agent = self.agent(session.row["device_id"])
            if agent.generation != session.row["runtime_generation"]:
                await self._sync_begin_generation(session, agent, recover=recover)
            self._guard_maintenance()
            reservation = await agent.request(
                "reserve_create",
                self.target_ref(session),
                session.project,
                wait_seconds=self.config.write_timeout,
            )
            self._guard_maintenance()
            payload = {
                **self.target_ref(session),
                "owner_user_id": session.principal.user_id,
                "space_id": session.principal.space_id,
                "host": target["host"],
                "port": target["port"],
                "username": target["username"],
                "password": target.pop("password"),
                "host_key_policy": target["host_key_policy"],
                "mode": "tmux" if recover else session.row["requested_mode"],
                "cols": session.row["cols"],
                "rows": session.row["rows"],
                "recover": recover,
                "admission_token": reservation.get("admission_token"),
            }
            await self.update(
                session,
                {"stage_timings_ms": {"hub_prepare": max(0, self.clock() - started) * 1000}},
            )
            self._guard_maintenance()
            result = await agent.request("create", payload, session.project)
            # A ready event may arrive before the create receipt. Never regress
            # its lifecycle back to creating.
            if session.row["state"] == "creating":
                await self.update(session, result)
        except (DevError, TerminalProtocolError) as exc:
            code = exc.code
            state = (
                "frozen"
                if code in {"AGENT_RESPONSE_UNKNOWN", "AGENT_OFFLINE", "AGENT_UPGRADE_REQUIRED"}
                else "failed"
            )
            await self.update(session, {"state": state, "reason_code": code})
            session.ready.set()
            if session.attachment:
                await session.attachment.error(code)
        except Exception:
            await self.update(session, {"state": "failed", "reason_code": "SSH_CONNECT_FAILED"})
            session.ready.set()
            if session.attachment:
                await session.attachment.error("SSH_CONNECT_FAILED")
        finally:
            if payload:
                payload.pop("password", None)
            if target:
                target.pop("password", None)

    async def _sync_begin_generation(self, session, agent, *, recover):
        if not recover:
            raise TerminalProtocolError("STALE_GENERATION", "连接期间 Agent 已重启")
        if self.agents.get(session.row["device_id"]) is not agent:
            raise TerminalProtocolError("STALE_GENERATION", "终端节点连接已被替换")
        await self.update(
            session,
            {"runtime_generation": agent.generation, "state": "creating"},
            allow_generation_change=True,
        )

    async def load(self, sid, principal):
        try:
            sid = canonical_uuid(sid)
        except TerminalProtocolError as exc:
            raise DevError("INVALID_SESSION", "终端会话编号无效", 400) from exc
        row = await self.store.run(self.repository.get, sid)
        if row is None:
            raise DevError("SESSION_NOT_FOUND", "找不到终端会话", 404)
        principal, project, _ = await self.store.run(
            self._authorize, principal, row["project_id"], None, row
        )
        session = self.sessions.get(sid)
        if session is None:
            session = self.sessions[sid] = HubSession(row, principal, project)
            self.prune_session_cache(keep=sid)
        return session

    async def list(self, principal):
        principal = await self.store.run(iam.live_principal, self.store, principal)
        rows = await self.store.run(self.repository.list, principal.space_id, principal.user_id)
        visible = []
        for row in rows:
            try:
                await self.store.run(self._authorize, principal, row["project_id"], None, row)
                visible.append(self.repository.public(row))
            except DevError:
                continue
        return {"sessions": visible}

    async def ticket(self, sid, principal, body):
        self._guard_maintenance()
        session = await self.load(sid, principal)
        await self.authorize(session, principal=principal)
        self._guard_maintenance()
        now = self.clock()
        self.tickets = {key: value for key, value in self.tickets.items() if value["expires"] > now}
        if len(self.tickets) >= self.config.max_sessions_per_hub * 4:
            raise DevError("TICKET_LIMIT", "终端连接请求过多，请稍后重试", 429)
        secret = token()
        self.tickets[digest(secret)] = {
            "session": session,
            "principal": principal,
            "resume": body.model_dump(exclude_none=True),
            "expires": now + self.config.ticket_ttl_seconds,
        }
        return {
            "ticket": secret,
            "ws_path": "/ws/vps-terminal",
            "expires_in": self.config.ticket_ttl_seconds,
        }

    async def consume_ticket(self, body, websocket):
        self._guard_maintenance()
        # Pop before the first await: one ticket cannot win two concurrent WSs.
        item = self.tickets.pop(digest(body["ticket"]), None)
        if (
            item is None
            or item["expires"] <= self.clock()
            or body["renderer_id"] != item["resume"]["renderer_id"]
        ):
            raise DevError("ATTACH_TICKET_INVALID", "终端连接票据已过期或已使用", 401)
        current = await self.store.run(self.auth.session, websocket)
        principal = item["principal"]
        if current["id_hash"] != principal.session_hash or current["user_id"] != principal.user_id:
            raise DevError("ATTACH_TICKET_INVALID", "终端票据不属于当前登录", 401)
        # Use the Space from the CSRF-authenticated ticket request; WebSockets
        # cannot set the panel's x-codepier-space header.
        session = item["session"]
        await self.authorize(session, principal=principal)
        self._guard_maintenance()
        return session, item["resume"], principal

    async def ensure_ready(self, session, *, principal=None):
        # This is an attach/recovery path, never a prerequisite for explicit
        # close, detach, OTP cleanup, output ACKs or authority lease renewal.
        self._guard_maintenance()
        if session.create_task and not session.create_task.done():
            await asyncio.shield(session.create_task)
        if session.row["state"] in {"creating", "connecting"}:
            await asyncio.wait_for(
                session.ready.wait(), self.config.connect_timeout + self.config.login_timeout + 10
            )
        self._guard_maintenance()
        agent = self.agent(session.row["device_id"])
        try:
            status = await agent.request(
                "status",
                {**self.target_ref(session), "runtime_generation": agent.generation},
                session.project,
            )
            if agent.generation == session.row["runtime_generation"]:
                return await self._accept_current_agent_status(session, agent, status, principal)
        except TerminalProtocolError as exc:
            if exc.code not in {"SESSION_NOT_FOUND", "STALE_GENERATION"}:
                raise
        if session.row["mode"] != "tmux":
            raise TerminalProtocolError(
                "SESSION_NOT_RECOVERABLE", "Agent 已重启，普通 SSH 会话不能恢复原 shell"
            )
        # Cross-generation recovery only asks tmux to attach this known session;
        # the Agent's recover path is forbidden from creating a new session.
        self._guard_maintenance()
        session.ready.clear()
        await self._begin(session, recover=True)
        await asyncio.wait_for(
            session.ready.wait(), self.config.connect_timeout + self.config.login_timeout + 10
        )
        self._guard_maintenance()
        if session.row["state"] not in {"ready", "detached"}:
            raise TerminalProtocolError(
                session.row.get("reason_code") or "SESSION_NOT_RECOVERABLE", "持久会话未恢复"
            )
        await self._recovered(session)
        return agent

    async def _accept_current_agent_status(self, session, agent, status, principal):
        await self.update(session, status)
        self._guard_maintenance()
        if status.get("mode") == "tmux" and status.get("recoverability") == "reauthenticate":
            return await self._reauthenticate_ready_session(session, agent, principal)
        if status.get("state") in {"ready", "detached", "exited"}:
            return agent
        if status.get("state") in {"creating", "connecting"}:
            # Clear before requesting status so a later ready event is
            # never erased; when already set, re-check the row instead.
            if session.row["state"] not in {"ready", "detached"}:
                await asyncio.wait_for(
                    session.ready.wait(),
                    self.config.connect_timeout + self.config.login_timeout + 10,
                )
            self._guard_maintenance()
            return agent
        raise TerminalProtocolError(
            status.get("reason_code") or "SESSION_ENDED", "原会话已经结束或不可恢复"
        )

    async def _reauthenticate_ready_session(self, session, agent, principal):
        target = await self.authorize(session, credentials=True, principal=principal)
        payload = None
        try:
            self._guard_maintenance()
            session.authority_revision = uuid.uuid4().hex
            session.ready.clear()
            payload = {**self.target_ref(session), "password": target.pop("password")}
            recovered = await agent.request("recover", payload, session.project)
        finally:
            target.pop("password", None)
            if payload:
                payload.pop("password", None)
        await self.update(session, recovered)
        await asyncio.wait_for(
            session.ready.wait(),
            self.config.connect_timeout + self.config.login_timeout + 10,
        )
        self._guard_maintenance()
        if session.row["state"] not in {"ready", "detached"}:
            raise TerminalProtocolError(
                session.row.get("reason_code") or "SESSION_NOT_RECOVERABLE",
                "原持久会话未恢复",
            )
        await self._recovered(session)
        return agent

    async def attach(self, session, attachment, resume):
        self._guard_maintenance()
        started = self.clock()
        async with session.attachment_lock:
            await self.authorize(session, principal=attachment.principal)
            self._guard_maintenance()
            previous = session.attachment
            if previous is not None and not previous.closed:
                # A new socket for the same renderer replaces an interrupted
                # connection; a different tab needs explicit takeover.
                if previous.renderer_id != attachment.renderer_id:
                    raise TerminalProtocolError(
                        "WRITER_CONFLICT", "此终端正在另一窗口使用，请选择接管"
                    )
                if previous.agent and not previous.agent.closed:
                    await previous.agent.request(
                        "detach",
                        {**self.target_ref(session), "attachment_id": previous.id},
                        session.project,
                        wait_seconds=self.config.write_timeout,
                    )
                await previous.shutdown("STALE_WRITER", 4409)
            if previous is not None:
                await self._disconnected(session, previous)
            self._guard_maintenance()
            # A new explicitly authenticated attachment may become the logical
            # lease owner. Read/list/ticket calls never replace a live socket's
            # immutable login principal.
            session.principal = attachment.principal
            session.revoked = False
            session.attachment = attachment
            if session.challenge and self.clock() < session.challenge_until:
                await attachment.control(session.challenge)
            agent = await self.ensure_ready(session, principal=attachment.principal)
            await self.authorize(session, principal=attachment.principal)
            self._guard_maintenance()
            if attachment.closed or session.attachment is not attachment:
                raise TerminalProtocolError("STALE_WRITER", "连接已被替换")
            session.render_epoch += 1
            writer_epoch = int(session.row["writer_epoch"]) + 1
            payload = {
                **self.target_ref(session),
                "attachment_id": attachment.id,
                "renderer_id": attachment.renderer_id,
                "render_epoch": str(session.render_epoch),
                "writer_epoch": str(writer_epoch),
                "stream_epoch": resume.get("known_epoch", session.row["stream_epoch"]),
                "rendered_offset": resume.get("rendered_offset", "0"),
            }
            if resume.get("runtime_generation") != agent.generation:
                payload["rendered_offset"] = "0"
                payload["stream_epoch"] = session.row["stream_epoch"]
            status = await agent.request("attach", payload, session.project)
            await self.update(session, status)
            strategy = {"resume": "resume_unconsumed", "replay": "history_replay"}.get(
                status.get("resume_strategy"), status.get("resume_strategy", "live")
            )
            start = status.get(
                "start_offset",
                payload["rendered_offset"] if strategy == "resume_unconsumed" else "0",
            )
            attachment.bind(
                agent,
                session.row["runtime_generation"],
                int(status["stream_epoch"]),
                writer_epoch,
                session.render_epoch,
                int(status.get("output_end_offset", status.get("available_end", "0"))),
                strategy,
            )
            message = {
                "type": "attached",
                "session_id": session.row["id"],
                "runtime_generation": agent.generation,
                "attachment_id": attachment.id,
                "render_epoch": str(session.render_epoch),
                "stream_epoch": status["stream_epoch"],
                "writer_epoch": str(writer_epoch),
                "mode": status["mode"],
                "state": status["state"],
                "resume_strategy": strategy,
                "start_offset": start,
                "output_end_offset": status.get(
                    "output_end_offset", status.get("available_end", "0")
                ),
                "next_input_seq": status.get("next_input_seq", "1"),
                "cols": status["cols"],
                "rows": status["rows"],
            }
            await attachment.send_attached(message)
            await self._connected(session, attachment, max(0, self.clock() - started) * 1000)
            if (
                strategy not in {"live", "history_replay"}
                and int(start) >= attachment.replay_boundary
            ):
                await attachment.complete_replay()

    async def detach(self, session, attachment):
        async with session.attachment_lock:
            await self._detach_locked(session, attachment)

    async def _detach_locked(self, session, attachment):
        if session.attachment is not attachment:
            return
        session.attachment = None
        if attachment.agent and not attachment.agent.closed:
            with contextlib.suppress(Exception):
                await attachment.agent.request(
                    "detach",
                    {**self.target_ref(session), "attachment_id": attachment.id},
                    session.project,
                    wait_seconds=self.config.write_timeout,
                )
        if (
            session.attachment is None
            and session.row["state"] not in TERMINAL_STATES
            and not session.revoked
        ):
            await self.update(session, {"state": "detached"})
        await self._disconnected(session, attachment)

    async def action(self, sid, principal, action, body):
        session = await self.load(sid, principal)
        await self.authorize(session, principal=principal)
        expected = body.runtime_generation or body.expected_generation
        if expected and expected != session.row["runtime_generation"]:
            raise DevError("STALE_GENERATION", "Agent 运行代次已变化，请先确认原会话状态", 409)
        if action == "takeover":
            return await self._takeover(session, principal, body)
        if action == "detach":
            previous = session.attachment
            if previous:
                await previous.shutdown("USER_DETACHED", 1000)
                await self.detach(session, previous)
            return self.repository.public(session.row)
        if action != "close":
            raise DevError("INVALID_ACTION", "未知终端动作", 400)
        return await self._close_session(session, principal)

    async def _close_session(self, session, principal):
        state = session.row["state"]
        reason = session.row.get("reason_code")
        if (state, reason) == ("exited", "USER_CLOSED"):
            return self.repository.public(session.row)
        if (state, reason) == ("failed", "SESSION_NOT_RECOVERABLE") and self._confirmed_direct(
            session.row
        ):
            return self.repository.public(session.row)
        if session.create_task and not session.create_task.done():
            # Once the user closes, the Hub must not later enqueue create
            # after its close RPC. The Agent channel's control FIFO reconciles
            # a create already sent; cancelling this task is not a rollback.
            session.create_task.cancel()
            await asyncio.gather(session.create_task, return_exceptions=True)
        if session.attachment:
            session.attachment.input_allowed = False
        await self.update(session, {"state": "closing"})
        try:
            agent = self.agent(session.row["device_id"])
            if (
                self._confirmed_direct(session.row)
                and agent.generation != session.row["runtime_generation"]
            ):
                status = await self._reconcile_replaced_direct(session, principal, agent)
            else:
                status = await self._request_terminal_close(session, principal, agent)
            await self.update(session, status)
        except (DevError, TerminalProtocolError, TimeoutError, ConnectionError):
            await self.update(session, {"state": "close_unknown", "reason_code": "CLOSE_UNKNOWN"})
        if session.attachment:
            await session.attachment.state(self.repository.public(session.row))
        await self._audit(
            session,
            "close",
            principal=principal,
            outcome=self._close_outcome(session.row),
        )
        return self.repository.public(session.row)

    async def _request_terminal_close(self, session, principal, agent):
        target = await self.authorize(session, credentials=True, principal=principal)
        payload = {**self.target_ref(session), "password": target.pop("password")}
        try:
            return await agent.request("close", payload, session.project)
        finally:
            payload.pop("password", None)

    @staticmethod
    def _close_outcome(row):
        if row["state"] == "close_unknown":
            return "unknown"
        if row.get("reason_code") == "SESSION_NOT_RECOVERABLE":
            return "transport_gone"
        return "ok"

    @staticmethod
    def _confirmed_direct(row):
        if row["mode"] != "direct":
            return False
        if row["requested_mode"] == "direct":
            return True
        # Before opening a PTY, "direct" on an auto/tmux request is only
        # the provisional default. It cannot prove that no tmux was created.
        return row["requested_mode"] == "auto" and "pty_open" in safe_metadata(
            json.loads(row["metadata"])
        ).get("stage_timings_ms", {})

    def _current_terminal_agent(self, device_id, agent):
        main = self.runtime.connections.get(device_id)
        return (
            self.agents.get(device_id) is agent
            and not agent.closed
            and main is not None
            and getattr(agent, "main_connection", None) is main
        )

    async def _reconcile_replaced_direct(self, session, principal, agent):
        device_id = session.row["device_id"]
        old_generation = session.row["runtime_generation"]
        if not self._current_terminal_agent(device_id, agent):
            raise TerminalProtocolError("AGENT_RESPONSE_UNKNOWN", "无法确认替换节点身份")
        try:
            await agent.request(
                "status",
                {**self.target_ref(session), "runtime_generation": agent.generation},
                session.project,
                wait_seconds=self.config.write_timeout,
            )
        except TerminalProtocolError as exc:
            if exc.code != "SESSION_NOT_FOUND":
                raise
        else:
            raise TerminalProtocolError("AGENT_RESPONSE_UNKNOWN", "替换节点仍有会话记录")
        # Recheck ownership/authority and identity after the await. Retire
        # only this unaddressable direct transport, not remote child processes.
        await self.authorize(session, principal=principal)
        if (
            not self._current_terminal_agent(device_id, agent)
            or session.row["runtime_generation"] != old_generation
            or not self._confirmed_direct(session.row)
        ):
            raise TerminalProtocolError("AGENT_RESPONSE_UNKNOWN", "原终端状态已变化")
        return {
            "state": "failed",
            "reason_code": "SESSION_NOT_RECOVERABLE",
            "recoverability": "none",
            "remote_session_state": "unknown",
            "ssh_connected": False,
            "output_complete": False,
        }

    async def _takeover(self, session, principal, body):
        async with session.attachment_lock:
            self._guard_maintenance()
            if (
                body.expected_writer_epoch is not None
                and body.expected_writer_epoch != session.row["writer_epoch"]
            ):
                raise DevError("STALE_WRITER", "终端控制权已经变化，请刷新后接管", 409)
            previous = session.attachment
            if previous:
                previous.input_allowed = False
                if previous.agent and not previous.agent.closed:
                    await previous.agent.request(
                        "detach",
                        {**self.target_ref(session), "attachment_id": previous.id},
                        session.project,
                        wait_seconds=self.config.write_timeout,
                    )
                await previous.shutdown("STALE_WRITER", 4409)
                await self._detach_locked(session, previous)
            await self._audit(session, "takeover", principal=principal)
        return self.repository.public(session.row)

    async def on_agent_event(self, connection, event):
        if self.agents.get(connection.device_id) is not connection:
            return
        sid = event.get("session_id")
        session = self.sessions.get(sid)
        if (
            session is None
            or session.row["device_id"] != connection.device_id
            or event.get("runtime_generation") != connection.generation
            or session.row["runtime_generation"] != connection.generation
        ):
            return
        kind = event.get("type")
        attachment = session.attachment
        if kind == "terminal_output":
            await self._relay_agent_output(connection, event, sid, attachment)
        elif kind == "terminal_replay_complete":
            await self._complete_agent_replay(session, event, attachment)
        elif kind == "terminal_auth_challenge":
            await self._forward_agent_challenge(session, sid, event, attachment)
        elif kind == "terminal_input_ack":
            await self._forward_agent_input_ack(session, connection, event, attachment)
        elif kind == "terminal_error":
            await self._forward_agent_error(session, event, attachment)
        elif kind in {"terminal_state", "terminal_exit"}:
            await self._apply_agent_lifecycle_event(session, kind, event, attachment)

    async def _relay_agent_output(self, connection, event, sid, attachment):
        if attachment is None or attachment.closed or event.get("attachment_id") != attachment.id:
            return
        raw = event.get("data")
        if not isinstance(raw, str) or len(raw) > (self.config.frame_bytes * 4 // 3 + 16):
            raise TerminalProtocolError("INVALID_FRAME", "终端输出编码无效")
        payload = base64.b64decode(raw, validate=True)
        frame = TerminalFrame(
            FrameKind.OUTPUT,
            sid,
            connection.generation,
            int(event["stream_epoch"]),
            int(event["writer_epoch"]),
            int(event["offset"]),
            payload,
        )
        await attachment.output(frame)

    async def _complete_agent_replay(self, session, event, attachment):
        if (
            attachment
            and not attachment.closed
            and session.row["state"] == "ready"
            and event.get("attachment_id") == attachment.id
            and str(event.get("render_epoch")) == str(attachment.render_epoch)
            and str(event.get("stream_epoch")) == str(attachment.stream_epoch)
            and str(event.get("final_offset")) == str(attachment.replay_boundary)
            and attachment.replay_boundary <= attachment.acked_offset
        ):
            await attachment.complete_replay()

    async def _forward_agent_challenge(self, session, sid, event, attachment):
        challenge = {
            "type": "auth_challenge",
            "session_id": sid,
            "challenge_id": event["challenge_id"],
            "prompts": event["prompts"],
            "deadline_seconds": min(
                self.config.login_timeout,
                event.get("deadline_seconds", self.config.login_timeout),
            ),
        }
        session.challenge = challenge
        session.challenge_until = self.clock() + challenge["deadline_seconds"]
        if attachment:
            await attachment.control(challenge)

    async def _forward_agent_input_ack(self, session, connection, event, attachment):
        if (
            attachment
            and not attachment.closed
            and str(event.get("writer_epoch")) == str(attachment.writer_epoch)
        ):
            if event.get("attachment_id") not in {None, attachment.id} or str(
                event.get("stream_epoch", attachment.stream_epoch)
            ) != str(attachment.stream_epoch):
                return
            if event.get("status") == "uncertain":
                self.observe(session, "input_uncertain_total")
            await attachment.control(
                {
                    "type": "input_ack",
                    "attachment_id": attachment.id,
                    "runtime_generation": connection.generation,
                    "render_epoch": str(attachment.render_epoch),
                    "stream_epoch": str(attachment.stream_epoch),
                    "input_seq": str(event["input_seq"]),
                    "writer_epoch": str(event["writer_epoch"]),
                    "status": event["status"],
                }
            )

    async def _forward_agent_error(self, session, event, attachment):
        if (
            attachment
            and not attachment.closed
            and str(event.get("stream_epoch", attachment.stream_epoch))
            == str(attachment.stream_epoch)
            and str(event.get("writer_epoch", attachment.writer_epoch))
            == str(attachment.writer_epoch)
        ):
            if event.get("reason_code") == "HISTORY_GAP":
                self.observe(session, "history_gap_total")
            await attachment.error(event.get("reason_code", "AGENT_ERROR"))

    async def _apply_agent_lifecycle_event(self, session, kind, event, attachment):
        changes = dict(event)
        if kind == "terminal_exit":
            if (
                str(event.get("stream_epoch")) != str(session.row["stream_epoch"])
                or attachment
                and event.get("attachment_id") not in {None, attachment.id}
            ):
                return
            changes["state"] = event.get(
                "state",
                "detached"
                if session.row["mode"] == "tmux"
                and event.get("remote_session_state") not in {"missing", "ended"}
                else "exited",
            )
        await self.update(session, changes)
        if session.row["state"] != "creating":
            session.challenge = None
        if attachment:
            if kind == "terminal_exit":
                await attachment.exit(event)
            elif session.row["state"] not in {"ready", "detached"} or attachment.attached.is_set():
                await attachment.state(self.repository.public(session.row))

    async def revoke(self, session, reason="AUTH_REVOKED"):
        if session.revoked:
            return
        started = self.clock()
        session.revoked = True
        session.authority_revision = uuid.uuid4().hex
        if session.create_task and not session.create_task.done():
            session.create_task.cancel()
            await asyncio.gather(session.create_task, return_exceptions=True)
        if session.attachment:
            await session.attachment.shutdown(reason, 4403)
        await self.update(
            session, {"state": "failed", "reason_code": reason, "revoked_at": time.time()}
        )
        agent = self.agents.get(session.row["device_id"])
        if agent and not agent.closed:
            with contextlib.suppress(Exception):
                await agent.request(
                    "revoke",
                    self.target_ref(session),
                    session.project,
                    wait_seconds=self.config.write_timeout,
                )
        self.observe(session, "revocation_total")
        self.observe(
            session, "auth_revocation_ms", milliseconds=max(0, self.clock() - started) * 1000
        )
        await self._audit(session, "revoke")

    async def _renew_one(self, session):
        if session.revoked or session.row["state"] in TERMINAL_STATES:
            return
        try:
            await self.authorize(session)
        except DevError:
            await self.revoke(session)
            return
        agent = self.agents.get(session.row["device_id"])
        if agent and not agent.closed and agent.generation == session.row["runtime_generation"]:
            try:
                status = await agent.request(
                    "renew",
                    self.target_ref(session),
                    session.project,
                    wait_seconds=self.config.write_timeout,
                )
                # A missed state notification can be reconciled without
                # replaying create/input or touching the remote command.
                await self.update(session, status)
            except (TerminalProtocolError, TimeoutError):
                if session.attachment:
                    await session.attachment.error("AUTHORITY_UNCONFIRMED")

    async def _leases(self):
        while not self.closing:
            await asyncio.sleep(self.config.lease_renew_seconds)
            await asyncio.gather(
                *(self._renew_one(session) for session in tuple(self.sessions.values())),
                return_exceptions=True,
            )
            now = self.clock()
            self.tickets = {
                key: value for key, value in self.tickets.items() if value["expires"] > now
            }
            self.prune_session_cache()

    async def register_agent(self, connection):
        old = self.agents.get(connection.device_id)
        self.agents[connection.device_id] = connection
        if old is not None and old is not connection:
            for session in tuple(self.sessions.values()):
                attachment = session.attachment
                if (
                    session.row["device_id"] == connection.device_id
                    and attachment
                    and attachment.agent is old
                ):
                    await attachment.shutdown("AGENT_RECONNECTED", 1012)
            await old.close()

    async def unregister_agent(self, connection):
        if self.agents.get(connection.device_id) is not connection:
            return
        self.agents.pop(connection.device_id, None)
        for session in tuple(self.sessions.values()):
            if session.row["device_id"] == connection.device_id and session.attachment:
                await session.attachment.shutdown("AGENT_OFFLINE", 1012)
