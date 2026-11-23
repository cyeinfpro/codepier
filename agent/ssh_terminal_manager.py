"""Agent-owned SSH sessions, independent of browser/WebSocket lifetimes."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import time
import uuid

from shared.instance_lock import InstanceLock
from shared.ssh_host_pin import CONFIRM_CHANGE, validate_optional_host_pin
from shared.ssh_terminal_config import DEFAULT_CONFIG
from shared.ssh_terminal_flow import (
    ByteRingBuffer,
    HistoryGap,
    InputSequenceTracker,
    OutputCreditWindow,
)
from shared.ssh_terminal_protocol import PROTOCOL_VERSION, FrameKind, TerminalProtocolError
from shared.util import VERSION

from .ssh_terminal_transport import AsyncSSHTransport, SSHTransportError, available, classify_error
from .ssh_terminal_tmux import TmuxController, TmuxReference


_DIAGNOSTIC_LIMIT = (1 << 31) - 1
_PROBE_COMMAND = "printf 'CODEPIER_VPS_OK\\n'; hostname; id -un; uname -sr; df -h /"


def _elapsed_ms(started, now):
    return int(min(_DIAGNOSTIC_LIMIT, max(0.0, (now - started) * 1000)))


def _connected(transport):
    return transport is not None and bool(
        getattr(transport, "connected", not getattr(transport, "closed", False))
    )


def _id(value, field_name="session_id"):
    try:
        return uuid.UUID(value).hex
    except (ValueError, TypeError, AttributeError) as exc:
        raise TerminalProtocolError("INVALID_ARGUMENT", f"无效的 {field_name}") from exc


def _integer(value, name, minimum=0, maximum=2**64 - 1):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise TerminalProtocolError("INVALID_ARGUMENT", f"无效的 {name}")
    return value


def _text(value, name, maximum=200):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise TerminalProtocolError("INVALID_ARGUMENT", f"无效的 {name}")
    return value


def _display_text(value, maximum_bytes):
    value = "".join(
        char for char in str(value) if (ord(char) >= 32 or char in "\t\r\n") and ord(char) != 127
    )
    return value.encode("utf-8", "replace")[:maximum_bytes].decode("utf-8", "ignore")


@dataclass(repr=False)
class _Session:
    id: str
    project: dict
    owner: str
    space: str
    target: dict
    requested_mode: str
    generation: str
    config: object
    lease_until: float
    authority_revision: str
    ring: object
    create_key: tuple = ()
    mode: str = "direct"
    state: str = "creating"
    state_revision: int = 0
    lifecycle_revision: int = 0
    reason: str = ""
    stream_epoch: int = 1
    writer_epoch: int = 0
    renderer_id: str | None = None
    attachment_id: str | None = None
    render_epoch: int = 0
    attached: bool = False
    input_enabled: bool = False
    frozen: bool = False
    detached_at: float | None = None
    transport: object = None
    tmux: object = None
    credit: object = None
    tracker: object = None
    send_offset: int = 0
    replay_end: int | None = None
    resize_sequence: int = 0
    resize_writer_epoch: int = 0
    resized: bool = False
    output_paused_at: float | None = None
    backpressure_count: int = 0
    backpressure_duration_ms: int = 0
    stage_timings_ms: dict = field(default_factory=dict)
    clock: object = field(default=time.monotonic, repr=False, compare=False)
    ended: dict | None = None
    exit_sent: bool = False
    bytes_in: int = 0
    queued_input_bytes: int = 0
    challenge: object = None
    challenge_id: str | None = None
    close_authority_until: float = 0
    close_task: object = None
    finish_task: object = None
    pump_task: object = None
    input_task: object = None
    start_task: object = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    attach_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))

    def public(self):
        # Snapshots are captured synchronously on the Agent event loop. The Hub
        # can discard an earlier RPC receipt arriving after a later event.
        self.state_revision += 1
        remote_ended = self.ended is not None and self.ended.get("remote_end_confirmed") is True
        remote_missing = self.reason == "REMOTE_SESSION_MISSING"
        recoverability = "none"
        if self.mode == "tmux" and not remote_ended and not remote_missing:
            recoverability = (
                "reauthenticate" if self.frozen or not _connected(self.transport) else "redraw"
            )
        elif self.mode == "direct" and self.ended is None and _connected(self.transport):
            recoverability = "resume"
        result = {
            "session_id": self.id,
            "runtime_generation": self.generation,
            "state": self.state,
            "reason_code": self.reason or None,
            "state_revision": str(self.state_revision),
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "stream_epoch": str(self.stream_epoch),
            "writer_epoch": str(self.writer_epoch),
            "render_epoch": str(self.render_epoch),
            "attachment_id": self.attachment_id,
            "input_enabled": self.input_enabled and not self.frozen,
            "available_start": str(self.ring.start_offset),
            "available_end": str(self.ring.end_offset),
            "cols": self.target["cols"],
            "rows": self.target["rows"],
            "bytes_in": self.bytes_in,
            "bytes_out": self.ring.end_offset,
            "recoverability": recoverability,
            "remote_session_state": "ended"
            if remote_ended
            else ("missing" if remote_missing else "unknown"),
            "ssh_connected": _connected(self.transport),
            "stage_timings_ms": dict(self.stage_timings_ms),
            "backpressure_count": self.backpressure_count,
            "backpressure_duration_ms": min(
                _DIAGNOSTIC_LIMIT,
                self.backpressure_duration_ms
                + (
                    _elapsed_ms(self.output_paused_at, self.clock())
                    if self.output_paused_at is not None
                    else 0
                ),
            ),
            "agent_version": VERSION,
            "protocol_version": PROTOCOL_VERSION,
        }
        if self.mode == "tmux":
            result["tmux_ref"] = TmuxReference.for_session(self.id).public()
        if self.ended is not None:
            result.update({key: value for key, value in self.ended.items() if key != "reason_code"})
        return result


@dataclass(frozen=True, repr=False)
class _CreateAdmission:
    scope: tuple
    target: tuple
    token: str
    expires: float


@dataclass(frozen=True, repr=False)
class _Tombstone:
    scope: tuple
    target: tuple
    create_key: tuple | None
    snapshot: dict

    def public(self):
        return dict(self.snapshot)


class TerminalSessionManager:
    def __init__(
        self, agent=None, *, emit, config=None, connector=None, authorize=None, clock=time.monotonic
    ):
        self.agent, self.emit = agent, emit
        self.config, self.clock = config or DEFAULT_CONFIG, clock
        self.connector = connector or (lambda: AsyncSSHTransport(self.config))
        self._authorize_callback = authorize
        self.generation = uuid.uuid4().hex
        self.sessions: dict[str, _Session] = {}
        self._sweep_task = None
        self._closed = False
        self._metadata_limit = max(128, min(1024, self.config.max_sessions_per_agent * 16))
        self._tombstone_limit = self._metadata_limit
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self._admissions: dict[str, _CreateAdmission] = {}
        self._admission_limit = max(64, min(256, self.config.max_sessions_per_agent * 4))
        # One Agent owns each state directory. All its terminal sessions share
        # one exclusive advisory lock, also used by the standalone installer.
        # Do not acquire it at startup: an upgrade must be able to start and
        # health-check the new Agent while retaining its maintenance lock.
        self._activity_lock = None

    def capabilities(self):
        enabled = not self._closed and available()
        return {
            "protocol": 1 if enabled else 0,
            "engine": "asyncssh",
            "runtime_generation": self.generation,
            "pty": enabled,
            "binary_io": enabled,
            "flow_control": enabled,
            "tmux_probe": enabled,
        }

    def maintenance_busy(self):
        """Only confirmed, fully retired history is idle for managed maintenance.

        Detached/recoverable tmux and uncertain closes still represent remote
        work. Maintenance never kills them or marks them ended just to proceed.
        Unconsumed create reservations also fence a delayed create/upgrade race.
        """
        return any(item.expires > self.clock() for item in self._admissions.values()) or any(
            not self._definitively_retired(session) for session in self.sessions.values()
        )

    def _check_maintenance(self, *, closing_auth=False):
        lifecycle = getattr(self.agent, "lifecycle", None)
        base = getattr(lifecycle, "base", None)
        if (
            getattr(lifecycle, "handoff_pending", False)
            or base is not None
            and (Path(base) / ".install.lock").exists()
        ) and not closing_auth:
            raise TerminalProtocolError(
                "AGENT_MAINTENANCE", "节点正在交接维护，不能继续终端输入或连接"
            )

    def _acquire_activity_lock(self):
        self._check_maintenance()
        state = getattr(self.agent, "state_dir", None)
        if self._activity_lock is not None or state is None:
            return
        try:
            lock = InstanceLock(Path(state) / ".ssh-terminal-activity.lock")
        except (RuntimeError, OSError) as exc:
            raise TerminalProtocolError(
                "AGENT_MAINTENANCE", "节点正在进行本机维护，暂不能建立终端连接"
            ) from exc
        try:
            # The CLI creates .install.lock before competing for this lock.
            # Recheck after acquisition to close the check/acquire race.
            self._check_maintenance()
        except Exception:
            lock.close()
            raise
        self._activity_lock = lock

    def _release_activity_if_idle(self):
        if self._activity_lock is not None and not self.maintenance_busy():
            lock, self._activity_lock = self._activity_lock, None
            lock.close()

    def _activity_task_done(self, task):
        if not task.cancelled():
            task.exception()
        self._release_activity_if_idle()

    def _authorize_execution(self, project):
        direct = project.get("_direct_vps") is True
        if direct and (
            project.get("id") not in {None, ""} or project.get("root") not in {None, ""}
        ):
            raise TerminalProtocolError("AUTH_REVOKED", "直连终端不能携带项目路径映射")
        if self._authorize_callback is not None:
            self._authorize_callback(project)
            return
        if self.agent is None:
            raise TerminalProtocolError("AUTH_REVOKED", "Agent 执行授权尚未初始化")
        if direct:
            from agent.shell import direct_vps_denial

            denial = direct_vps_denial(getattr(self.agent, "config", None))
        else:
            from agent.shell import shell_denial

            _, spec = self.agent.engine.root(project, write=True)
            denial = shell_denial(self.agent.config, project, spec)
        if denial:
            raise TerminalProtocolError(*denial)

    def _authorize(self, project):
        if not isinstance(project, dict):
            raise TerminalProtocolError("AUTH_REVOKED", "缺少可信项目授权")
        self._authorize_execution(project)
        owner = project.get("_terminal_owner")
        space = project.get("_terminal_space")
        if not isinstance(owner, str) or not owner.startswith("user:") or len(owner) <= 5:
            raise TerminalProtocolError("AUTH_REVOKED", "终端缺少可信用户归属")
        _text(space, "space_id")
        return owner[5:], space

    def _owned(self, payload, project, *, live_authority=True):
        owner, space = self._authorize(project)
        session = self.sessions.get(_id(payload.get("session_id")))
        if session is None:
            raise TerminalProtocolError("SESSION_NOT_FOUND", "Agent 上没有该终端会话")
        if bool(project.get("_direct_vps")) != bool(
            session.project.get("_direct_vps")
        ) or self._scope(owner, space, project) != self._scope(
            session.owner, session.space, session.project
        ):
            raise TerminalProtocolError("SESSION_NOT_FOUND", "会话不属于当前身份或项目映射")
        if payload.get("runtime_generation", self.generation) != self.generation:
            raise TerminalProtocolError("STALE_GENERATION", "Agent 已重启，请恢复原会话")
        if live_authority and (session.frozen or self.clock() >= session.lease_until):
            raise TerminalProtocolError("AUTH_REVOKED", "终端授权租约已失效")
        return session

    def _ensure_loop(self):
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop(), name="ssh-terminal-leases")

    @staticmethod
    def _scope(owner, space, project):
        if project.get("_direct_vps") is True:
            return owner, space, "", ""
        return owner, space, project.get("id"), project.get("root")

    def _target_identity(self, payload):
        if payload.get("runtime_generation") != self.generation:
            raise TerminalProtocolError("STALE_GENERATION", "Agent 运行代次已变化")
        return (
            self.generation,
            _text(payload.get("vps_id"), "vps_id"),
            _integer(payload.get("connection_revision"), "connection_revision", 1),
            _text(payload.get("binding_id"), "binding_id"),
        )

    def _reserve_create(self, payload, project):
        owner, space = self._authorize(project)
        sid = _id(payload.get("session_id"))
        scope = self._scope(owner, space, project)
        target = self._target_identity(payload)
        existing = self.sessions.get(sid)
        tombstone = self._tombstones.get(sid)
        if existing is not None:
            self._owned(payload, project, live_authority=False)
            self._check_target(existing, payload)
            return {"admission_token": None, "expires_in": 30}
        if tombstone is not None:
            if tombstone.scope != scope or tombstone.target != target:
                raise TerminalProtocolError("SESSION_NOT_FOUND", "会话不属于当前身份或项目映射")
            return {"admission_token": None, "expires_in": 30}
        now = self.clock()
        self._admissions = {
            key: value for key, value in self._admissions.items() if value.expires > now
        }
        old = self._admissions.get(sid)
        if old is not None:
            if old.scope != scope or old.target != target:
                raise TerminalProtocolError("SESSION_CONFLICT", "同一会话编号对应不同创建授权")
            return {"admission_token": old.token, "expires_in": max(0, old.expires - now)}
        if len(self._admissions) >= self._admission_limit:
            raise TerminalProtocolError("ADMISSION_LIMIT", "待确认的终端创建请求过多，请稍后重试")
        self._acquire_activity_lock()
        admission = _CreateAdmission(scope, target, uuid.uuid4().hex, now + 30)
        self._admissions[sid] = admission
        self._ensure_loop()  # Expired permits must release the lock even without a create.
        return {"admission_token": admission.token, "expires_in": 30}

    @staticmethod
    def _definitively_retired(session):
        if (
            not session.stopped.is_set()
            or session.transport is not None
            or session.attached
            or session.challenge is not None
        ):
            return False
        if any(
            task is not None and not task.done()
            for task in (
                session.start_task,
                session.pump_task,
                session.input_task,
                session.finish_task,
                session.close_task,
            )
        ):
            return False
        if session.mode == "tmux":
            return bool(
                session.ended
                and session.ended.get("remote_end_confirmed") is True
                or session.reason == "REMOTE_SESSION_MISSING"
            )
        return session.state in {"exited", "failed"}

    def _prune_metadata(self):
        if len(self.sessions) < self._metadata_limit:
            return
        for sid, session in tuple(self.sessions.items()):
            if not self._definitively_retired(session):
                continue
            self._remember_tombstone(
                sid,
                _Tombstone(
                    self._scope(session.owner, session.space, session.project),
                    (
                        self.generation,
                        session.target["vps_id"],
                        session.target["connection_revision"],
                        session.target["binding_id"],
                    ),
                    session.create_key,
                    session.public(),
                ),
            )
            self.sessions.pop(sid)
            if len(self.sessions) < self._metadata_limit:
                return

    def _remember_tombstone(self, sid, tombstone):
        self._tombstones[sid] = tombstone
        while len(self._tombstones) > self._tombstone_limit:
            self._tombstones.popitem(last=False)

    def _cancel_admission(self, action, payload, project):
        sid = _id(payload.get("session_id"))
        admission = self._admissions.get(sid)
        if admission is None or sid in self.sessions:
            return None
        owner, space = self._authorize(project)
        if admission.scope != self._scope(
            owner, space, project
        ) or admission.target != self._target_identity(payload):
            raise TerminalProtocolError("SESSION_NOT_FOUND", "创建授权不属于当前身份或项目映射")
        self._admissions.pop(sid)
        snapshot = {
            "session_id": sid,
            "runtime_generation": self.generation,
            "state_revision": "1",
            "state": "exited" if action == "close" else "failed",
            "reason_code": "USER_CLOSED" if action == "close" else "AUTH_REVOKED",
            "recoverability": "none",
            "remote_session_state": "ended",
            "remote_end_confirmed": True,
            "ssh_connected": False,
            "input_enabled": False,
            "output_complete": False,
        }
        self._remember_tombstone(sid, _Tombstone(admission.scope, admission.target, None, snapshot))
        self._release_activity_if_idle()
        return dict(snapshot)

    def _list(self, payload, project):
        owner, space = self._authorize(project)
        limit = _integer(payload.get("limit", 32), "limit", 1, 64)
        items = [
            item
            for item in reversed(tuple(self.sessions.values()))
            if bool(item.project.get("_direct_vps")) == bool(project.get("_direct_vps"))
            and self._scope(item.owner, item.space, item.project)
            == self._scope(owner, space, project)
        ]
        cursor = payload.get("cursor")
        if cursor is not None:
            cursor = _id(cursor, "cursor")
            position = next((index for index, item in enumerate(items) if item.id == cursor), None)
            if position is None:
                raise TerminalProtocolError("CURSOR_EXPIRED", "终端列表已更新，请从第一页重新读取")
            items = items[position + 1 :]
        rows, size = [], 256
        for session in items[:limit]:
            snapshot = session.public()
            encoded_size = (
                len(json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
                + 1
            )
            if size + encoded_size > 48 * 1024:
                if not rows:
                    raise TerminalProtocolError("STATE_TOO_LARGE", "终端元数据超出列表响应上限")
                break
            rows.append(snapshot)
            size += encoded_size
        more = len(rows) < len(items)
        return {
            "sessions": rows,
            "next_cursor": rows[-1]["session_id"] if more and rows else None,
            "truncated": more,
            "retention": "active_and_recent",
        }

    def _check_capacity(self, owner, space, *, replacing=None):
        retained = [item for item in self.sessions.values() if item is not replacing]
        # An uncertain tmux create may already have made a remote shell. Keep
        # that reservation until the exact ID is confirmed missing or closed.
        live = [
            item
            for item in retained
            if item.state not in {"exited", "failed"}
            or (
                item.mode == "tmux"
                and item.reason != "REMOTE_SESSION_MISSING"
                and not (item.ended and item.ended.get("remote_end_confirmed") is True)
            )
        ]
        reservation = (
            self.config.ring_bytes
            + self.config.ssh_window_bytes * 2
            + self.config.pending_input_bytes
        )
        buffers = sum(item.ring.capacity_bytes for item in retained)
        if (
            len(live) >= self.config.max_sessions_per_agent
            or buffers + len(live) * (reservation - self.config.ring_bytes) + reservation
            > self.config.agent_buffer_bytes
            or sum(item.owner == owner and item.space == space for item in live)
            >= self.config.max_sessions_per_user
        ):
            raise TerminalProtocolError("SESSION_LIMIT", "Agent 已达到终端会话上限")

    async def _event(self, session, kind, **details):
        event = {
            "type": kind,
            "session_id": session.id,
            "runtime_generation": self.generation,
            **details,
        }
        try:
            result = self.emit(event)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, self.config.write_timeout)
        except TerminalProtocolError as exc:
            raise ConnectionError("Terminal data channel unavailable") from exc

    async def _state(self, session):
        try:
            await self._event(
                session,
                "terminal_state",
                **{
                    key: value
                    for key, value in session.public().items()
                    if key not in {"session_id", "runtime_generation"}
                },
            )
        except (TimeoutError, ConnectionError):
            # Control metadata is queryable; losing a notification never opens
            # a second shell or destroys the underlying remote process.
            pass

    def _end_output_pause(self, session):
        if session.output_paused_at is not None:
            session.backpressure_duration_ms = min(
                _DIAGNOSTIC_LIMIT,
                session.backpressure_duration_ms
                + _elapsed_ms(session.output_paused_at, self.clock()),
            )
            session.output_paused_at = None

    def _probe_target(self, payload):
        host = _text(payload.get("host"), "host", 253)
        username = _text(payload.get("username"), "username", 128)
        if (
            any(ch in host for ch in " /\\\t\r\n")
            or host.startswith("-")
            or username.startswith("-")
        ):
            raise TerminalProtocolError("INVALID_ARGUMENT", "SSH 地址或账号格式无效")
        policy = payload.get("host_key_policy", "strict")
        if policy not in {"strict", "accept-new", CONFIRM_CHANGE}:
            raise TerminalProtocolError("INVALID_ARGUMENT", "未知主机密钥政策")
        timeout = min(
            60, max(5, _integer(payload.get("timeout_seconds", 30), "timeout_seconds", 1, 86400))
        )
        return {
            "host": host,
            "port": _integer(payload.get("port", 22), "port", 1, 65535),
            "username": username,
            "host_key_policy": policy,
            "host_key_fingerprint": self._host_pin(policy, payload),
        }, timeout

    @staticmethod
    def _host_pin(policy, payload):
        try:
            return validate_optional_host_pin(policy, payload.get("host_key_fingerprint", ""))
        except ValueError as exc:
            raise TerminalProtocolError("INVALID_ARGUMENT", str(exc)) from None

    @staticmethod
    def _redact_secret(text, secret):
        if not text or not secret:
            return text or ""
        return text.replace(secret, "<redacted>")

    async def _probe(self, payload, project):
        self._authorize(project)
        self._check_maintenance()
        target, wait_seconds = self._probe_target(payload)
        password = self._password(payload)
        payload.pop("password", None)
        transport = self.connector()
        try:
            await transport.connect(target, password, challenge=None)
            exit_code, raw = await transport.run_control(
                _PROBE_COMMAND,
                wait_seconds=wait_seconds,
                max_bytes=16384,
            )
            text = (
                raw.decode("utf-8", "replace")
                if isinstance(raw, (bytes, bytearray))
                else str(raw or "")
            )
            output = self._redact_secret(text, password)
            return {
                "ok": exit_code == 0,
                "state": "succeeded" if exit_code == 0 else "failed",
                "pending": False,
                "exit_code": exit_code,
                "output": output,
                "error": None,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, TerminalProtocolError)
                else classify_error(exc, getattr(transport, "trust", None))
            )
            message = self._redact_secret(getattr(failure, "message", "SSH 连接检查失败"), password)
            return {
                "ok": False,
                "state": "failed",
                "pending": False,
                "exit_code": None,
                "output": "",
                "error": {"code": failure.code, "message": message},
            }
        finally:
            password = None
            await transport.close()

    async def control(self, action, payload, project):
        self._check_control(action, payload)
        if action == "probe":
            return await self._probe(payload, project)
        if action == "reserve_create":
            return self._reserve_create(payload, project)
        if action == "create":
            return await self._create(payload, project)
        if action == "list":
            return self._list(payload, project)
        if action in {"close", "revoke"}:
            cancelled = self._cancel_admission(action, payload, project)
            if cancelled is not None:
                return cancelled
        tombstone = self._tombstones.get(_id(payload.get("session_id")))
        if tombstone is not None:
            return self._control_tombstone(tombstone, action, payload, project)
        session = self._owned(
            payload,
            project,
            live_authority=action
            not in {
                "status",
                "close",
                "revoke",
                "renew",
                "recover",
                "auth_answer",
            },
        )
        return await self._control_session(session, action, payload, project)

    def _check_control(self, action, payload):
        if not isinstance(payload, dict):
            raise TerminalProtocolError("INVALID_ARGUMENT", "终端控制参数必须为对象")
        if action not in {"close", "revoke", "detach", "status", "list", "auth_answer"}:
            self._check_maintenance()
        if self._closed and action not in {"status", "list"}:
            raise TerminalProtocolError("AGENT_STOPPING", "Agent 正在关闭")

    def _control_tombstone(self, tombstone, action, payload, project):
        owner, space = self._authorize(project)
        if tombstone.scope != self._scope(owner, space, project):
            raise TerminalProtocolError("SESSION_NOT_FOUND", "会话不属于当前身份或项目映射")
        if payload.get("runtime_generation", self.generation) != self.generation:
            raise TerminalProtocolError("STALE_GENERATION", "Agent 已重启")
        if action in {"status", "close", "detach", "revoke"}:
            return tombstone.public()
        raise TerminalProtocolError("SESSION_ENDED", "原会话已结束")

    async def _control_session(self, session, action, payload, project):
        if action == "status":
            return self._status(session)
        if action == "renew":
            return self._renew(session, payload)
        if action in {"attach", "takeover"}:
            return await self._attach(session, payload)
        if action == "resize":
            return self._resize(session, payload)
        if action == "detach":
            return await self._control_detach(session, payload)
        if action in {"close", "revoke"}:
            return await self._control_close(session, action, payload, project)
        if action == "recover":
            return await self._recover(session, payload, project)
        if action == "auth_answer":
            return self._answer_challenge(session, payload)
        raise TerminalProtocolError("INVALID_ACTION", "未知终端控制动作")

    @staticmethod
    def _status(session):
        result = {**session.public(), "ring_bytes": session.ring.size}
        if session.attached and session.credit is not None:
            result["output_outstanding_bytes"] = session.credit.in_flight
        return result

    def _renew(self, session, payload):
        self._check_target(session, payload)
        if session.stopped.is_set() or session.frozen:
            raise TerminalProtocolError("AUTH_REVOKED", "已撤销的会话不能通过续租恢复")
        session.lease_until = self.clock() + self.config.lease_seconds
        session.authority_revision = _text(payload.get("authority_revision"), "authority_revision")
        return session.public()

    def _resize(self, session, payload):
        self._writer(session, payload)
        sequence = _integer(payload.get("resize_seq"), "resize_seq", 1)
        if (
            session.resize_writer_epoch == session.writer_epoch
            and sequence <= session.resize_sequence
        ):
            return session.public()
        cols, rows = self._size(payload)
        session.resize_writer_epoch = session.writer_epoch
        session.resize_sequence = sequence
        if (cols, rows) != (session.target["cols"], session.target["rows"]):
            session.transport.resize(cols, rows)
            session.target.update(cols=cols, rows=rows)
            session.resized = True
        return session.public()

    async def _control_detach(self, session, payload):
        # Control RPCs are dispatched concurrently. If the Hub abandoned an
        # attach whose redraw is still running, wait until that writer commits.
        async with session.attach_lock:
            if payload.get("attachment_id") != session.attachment_id:
                return session.public()  # Never detach a newer writer.
            await self._detach(session)
            return session.public()

    async def _control_close(self, session, action, payload, project):
        if action == "revoke":
            await self._finish(session, "AUTH_REVOKED")
            return session.public()
        if session.finish_task is not None and not session.finish_task.done():
            await asyncio.shield(session.finish_task)
            self._authorize(project)
        if session.ended and session.ended.get("remote_end_confirmed") is True:
            return session.public()
        if session.mode == "tmux" and self._needs_detached_close(session):
            self._check_target(session, payload)
            return await self._run_detached_close(session, payload)
        await self._finish(session, "USER_CLOSED", end_remote=True)
        return session.public()

    @staticmethod
    def _needs_detached_close(session):
        return (
            not _connected(session.transport)
            or session.tmux is None
            or session.start_task is not None
            and not session.start_task.done()
        )

    async def _run_detached_close(self, session, payload):
        if session.close_task is None or session.close_task.done():
            session.close_task = asyncio.create_task(
                self._close_detached_tmux(session, dict(payload)),
                name="ssh-terminal-close-" + session.id,
            )

            def close_done(done):
                if not done.cancelled():
                    done.exception()
                # An exception traceback can retain a connecting
                # coroutine's temporary password. Keep only its safe
                # state result, never a completed close task.
                if session.close_task is done:
                    session.close_task = None
                self._release_activity_if_idle()

            session.close_task.add_done_callback(close_done)
        return await asyncio.shield(session.close_task)

    async def _recover(self, session, payload, project):
        self._check_target(session, payload)
        lifecycle = session.lifecycle_revision
        if session.finish_task is not None and not session.finish_task.done():
            await asyncio.shield(session.finish_task)
            if lifecycle != session.lifecycle_revision:
                raise TerminalProtocolError("AUTH_REVOKED", "恢复期间会话已被撤销或关闭")
        if not self._needs_recovery(session):
            return session.public()
        revision = _text(payload.get("authority_revision"), "authority_revision")
        if revision == session.authority_revision:
            raise TerminalProtocolError("AUTH_RECONFIRM_REQUIRED", "恢复原会话需要新的完整授权确认")
        if session.frozen and session.reason not in {"AUTH_REVOKED", "AUTH_LEASE_EXPIRED"}:
            raise TerminalProtocolError("AUTH_REVOKED", "会话已关闭，不能恢复")
        self._check_capacity(session.owner, session.space, replacing=session)
        password = self._password(payload)
        self._acquire_activity_lock()
        lifecycle = session.lifecycle_revision
        if session.transport is not None:
            await session.transport.close()
        self._check_maintenance()
        if lifecycle != session.lifecycle_revision or self._closed:
            raise TerminalProtocolError("AUTH_REVOKED", "恢复期间会话已被撤销或关闭")
        self._authorize(project)

        self._restart_session(session, project, revision, password)
        return session.public()

    def _needs_recovery(self, session):
        if session.mode != "tmux":
            raise TerminalProtocolError("SESSION_NOT_RECOVERABLE", "普通 SSH 连接不能恢复原 shell")
        if (
            session.reason == "USER_CLOSED"
            or session.ended
            and session.ended.get("remote_end_confirmed") is True
        ):
            raise TerminalProtocolError("SESSION_ENDED", "已结束的持久会话不能恢复")
        if session.close_task is not None and not session.close_task.done():
            raise TerminalProtocolError("SESSION_CLOSING", "会话结束请求仍在处理中")
        if session.start_task and not session.start_task.done():
            return False
        if (
            _connected(session.transport)
            and not session.frozen
            and not session.stopped.is_set()
            and self.clock() < session.lease_until
        ):
            return False
        return True

    def _restart_session(self, session, project, revision, password):
        session.lifecycle_revision += 1
        session.transport = session.tmux = None
        # Explicit recovery has freshly checked the same current owner,
        # target revision, local project grant, credentials and authority.
        # Renew alone can never clear these fences.
        session.project = dict(project)
        session.authority_revision = revision
        session.lease_until = self.clock() + self.config.lease_seconds
        session.state, session.reason, session.ended, session.exit_sent = (
            "creating",
            "",
            None,
            False,
        )
        session.frozen, session.attached, session.input_enabled = False, False, False
        self._end_output_pause(session)
        session.credit, session.tracker, session.replay_end = None, None, None
        session.stage_timings_ms.clear()
        session.stream_epoch += 1
        session.ring = ByteRingBuffer(self.config.ring_bytes)
        session.stopped.clear()
        if session.pump_task is None or session.pump_task.done():
            session.pump_task = asyncio.create_task(
                self._output_pump(session), name="ssh-output-" + session.id
            )
        if session.input_task is None or session.input_task.done():
            session.input_task = asyncio.create_task(
                self._input_pump(session), name="ssh-input-" + session.id
            )
        session.start_task = asyncio.create_task(
            self._start(session, password, recover=True),
            name="ssh-terminal-recover-" + session.id,
        )
        session.start_task.add_done_callback(self._activity_task_done)

    def _answer_challenge(self, session, payload):
        # A retained maintenance plan must not prevent the authenticated
        # control-only close needed to resolve an uncertain remote tmux.
        self._check_maintenance(
            closing_auth=(
                session.close_task is not None
                and not session.close_task.done()
                and self.clock() < session.close_authority_until
            )
        )
        if session.close_authority_until <= self.clock() and (
            session.frozen or session.stopped.is_set() or self.clock() >= session.lease_until
        ):
            raise TerminalProtocolError("AUTH_REVOKED", "认证请求的当前授权已失效")
        if (
            session.challenge is None
            or session.challenge.done()
            or payload.get("challenge_id") != session.challenge_id
        ):
            raise TerminalProtocolError("AUTH_CHALLENGE_EXPIRED", "认证问题已失效")
        answers = payload.get("responses")
        if not self._valid_challenge_answers(answers):
            raise TerminalProtocolError("INVALID_ARGUMENT", "认证回答格式无效")
        session.challenge.set_result(answers)
        return {"accepted": True}

    def _valid_challenge_answers(self, answers):
        try:
            return (
                isinstance(answers, list)
                and 1 <= len(answers) <= 8
                and all(
                    isinstance(item, str) and len(item.encode("utf-8")) <= 4096 for item in answers
                )
                and sum(len(item.encode("utf-8")) for item in answers) <= self.config.control_bytes
            )
        except UnicodeError:
            return False

    def _size(self, payload):
        return (
            _integer(payload.get("cols"), "cols", self.config.min_cols, self.config.max_cols),
            _integer(payload.get("rows"), "rows", self.config.min_rows, self.config.max_rows),
        )

    @staticmethod
    def _password(payload):
        value = payload.get("password")
        try:
            valid = (
                isinstance(value, str)
                and value
                and len(value.encode("utf-8")) <= 4096
                and "\x00" not in value
            )
        except UnicodeError:
            valid = False
        if not valid:
            raise TerminalProtocolError("INVALID_CREDENTIAL", "缺少有效的保存凭据")
        return value

    @staticmethod
    def _check_target(session, payload):
        for key in ("vps_id", "connection_revision", "binding_id"):
            if payload.get(key) != session.target.get(key):
                raise TerminalProtocolError("TARGET_REVISION_CHANGED", "VPS 连接或项目绑定已经变化")

    async def _create(self, payload, project):
        if self._closed:
            raise TerminalProtocolError("AGENT_STOPPING", "Agent 正在关闭")
        owner, space = self._authorize(project)
        sid = _id(payload.get("session_id"))
        if payload.get("runtime_generation", self.generation) != self.generation:
            raise TerminalProtocolError("STALE_GENERATION", "Agent 运行代次已变化")
        if payload.get("owner_user_id", owner) != owner or payload.get("space_id", space) != space:
            raise TerminalProtocolError("AUTH_REVOKED", "终端归属与可信项目授权不一致")
        mode = payload.get("mode", "tmux")
        if mode not in {"auto", "direct", "tmux"}:
            raise TerminalProtocolError("INVALID_ARGUMENT", "未知持久模式")
        if payload.get("recover") is True and mode == "direct":
            raise TerminalProtocolError("SESSION_NOT_RECOVERABLE", "普通会话不能跨 Agent 重启恢复")
        target = self._creation_target(payload)
        create_key = (tuple(sorted(target.items())), mode)
        old = self._existing_creation(sid, payload, project, owner, space, target, create_key)
        if old is not None:
            return old
        self._check_create_admission(sid, payload, project, owner, space)
        self._check_capacity(owner, space)
        self._prune_metadata()
        if len(self.sessions) >= self._metadata_limit:
            raise TerminalProtocolError(
                "SESSION_LIMIT", "尚有未完成或状态未确认的终端，请先结束不再使用的会话"
            )
        password = self._password(payload)
        authority_revision = _text(payload.get("authority_revision"), "authority_revision")
        self._acquire_activity_lock()
        # This method has no await before registration. Consuming a permit and
        # recording the new identity is atomic on this event loop. Once a
        # retired tombstone ages out, its old create packet still has no permit
        # and can never silently open another shell.
        return self._register_session(
            sid,
            project,
            owner,
            space,
            target,
            mode,
            create_key,
            authority_revision,
            password,
            recover=payload.get("recover") is True,
        )

    def _creation_target(self, payload):
        cols, rows = self._size(payload)
        host = _text(payload.get("host"), "host", 253)
        username = _text(payload.get("username"), "username", 128)
        if (
            any(ch in host for ch in " /\\\t\r\n")
            or host.startswith("-")
            or username.startswith("-")
        ):
            raise TerminalProtocolError("INVALID_ARGUMENT", "SSH 地址或账号格式无效")
        target = {
            "vps_id": _text(payload.get("vps_id"), "vps_id"),
            "connection_revision": _integer(
                payload.get("connection_revision"), "connection_revision", 1
            ),
            "binding_id": _text(payload.get("binding_id"), "binding_id"),
            "host": host,
            "port": _integer(payload.get("port", 22), "port", 1, 65535),
            "username": username,
            "host_key_policy": payload.get("host_key_policy", "strict"),
            "cols": cols,
            "rows": rows,
        }
        if target["host_key_policy"] not in {"strict", "accept-new", CONFIRM_CHANGE}:
            raise TerminalProtocolError("INVALID_ARGUMENT", "未知主机密钥政策")
        target["host_key_fingerprint"] = self._host_pin(target["host_key_policy"], payload)
        return target

    def _existing_creation(self, sid, payload, project, owner, space, target, create_key):
        old = self.sessions.get(sid)
        if old is not None:
            self._owned(payload, project, live_authority=False)
            if old.create_key != create_key:
                raise TerminalProtocolError("SESSION_CONFLICT", "同一会话编号对应不同连接请求")
            return old.public()
        tombstone = self._tombstones.get(sid)
        if tombstone is not None:
            if tombstone.scope != self._scope(owner, space, project):
                raise TerminalProtocolError("SESSION_NOT_FOUND", "会话不属于当前身份或项目映射")
            if (
                tombstone.target
                != (
                    self.generation,
                    target["vps_id"],
                    target["connection_revision"],
                    target["binding_id"],
                )
                or tombstone.create_key is not None
                and tombstone.create_key != create_key
            ):
                raise TerminalProtocolError("SESSION_CONFLICT", "同一会话编号对应不同连接请求")
            return tombstone.public()

    def _check_create_admission(self, sid, payload, project, owner, space):
        admission = self._admissions.get(sid)
        if (
            admission is None
            or admission.expires <= self.clock()
            or admission.token != payload.get("admission_token")
            or admission.scope != self._scope(owner, space, project)
            or admission.target != self._target_identity(payload)
        ):
            raise TerminalProtocolError(
                "CREATE_ADMISSION_REQUIRED", "创建授权已失效，请重新确认该会话创建请求"
            )

    def _register_session(
        self,
        sid,
        project,
        owner,
        space,
        target,
        mode,
        create_key,
        authority_revision,
        password,
        *,
        recover,
    ):
        self._admissions.pop(sid)
        session = _Session(
            sid,
            dict(project),
            owner,
            space,
            target,
            mode,
            self.generation,
            self.config,
            self.clock() + self.config.lease_seconds,
            authority_revision,
            ByteRingBuffer(self.config.ring_bytes),
            clock=self.clock,
        )
        if recover:
            # The identity already refers to a persistent remote session. A
            # failed reauthentication must not downgrade it to direct mode and
            # discard its ability to recover on a later explicit attempt.
            session.mode = "tmux"
        session.create_key = create_key
        self.sessions[sid] = session
        self._ensure_loop()
        session.pump_task = asyncio.create_task(
            self._output_pump(session), name="ssh-output-" + sid
        )
        session.input_task = asyncio.create_task(self._input_pump(session), name="ssh-input-" + sid)
        session.start_task = asyncio.create_task(
            self._start(session, password, recover=recover),
            name="ssh-start-" + sid,
        )
        session.start_task.add_done_callback(self._activity_task_done)
        return session.public()

    async def _start(self, session, password, *, recover=False):
        transport = self.connector()
        session.transport = transport
        session.stage_timings_ms.clear()
        try:
            self._check_maintenance()
            started = self.clock()
            try:
                # AsyncSSH completes host verification and authentication in
                # this await; separate TCP/auth durations would be fabricated.
                await transport.connect(
                    session.target,
                    password,
                    challenge=lambda n, i, p: self._challenge(session, n, i, p),
                )
            finally:
                session.stage_timings_ms["ssh_connect"] = _elapsed_ms(started, self.clock())
            password = None
            self._check_maintenance()
            if session.stopped.is_set() or session.frozen or self.clock() >= session.lease_until:
                raise TerminalProtocolError("AUTH_REVOKED", "连接期间终端授权已失效")
            await self._prepare_start_mode(session, transport, recover=recover)
            await self._open_channel(session)
            self._check_maintenance()
            if session.stopped.is_set() or session.frozen:
                await transport.close()
                return
            if session.ended is None:
                session.state = "ready"
            session.detached_at = self.clock()
            await self._state(session)
        except asyncio.CancelledError:
            await transport.close()
            if session.transport is transport:
                session.transport = session.tmux = None
            raise
        except Exception as exc:
            await self._record_start_failure(session, transport, exc)
        finally:
            if session.transport is None:
                self._end_output_pause(session)
            password = None

    async def _prepare_start_mode(self, session, transport, *, recover):
        if recover:
            if session.requested_mode == "direct":
                raise TerminalProtocolError(
                    "SESSION_NOT_RECOVERABLE", "普通会话不能跨 Agent 重启恢复"
                )
            session.tmux = TmuxController(transport, session.id, self.config)
            await session.tmux.recover()
            session.mode = "tmux"
        elif session.requested_mode != "direct":
            candidate = TmuxController(transport, session.id, self.config)
            capability = await candidate.probe()
            self._check_maintenance()
            if capability["available"]:
                session.tmux = candidate
                session.mode = "tmux"
                await candidate.create(session.target["cols"], session.target["rows"])
            else:
                # Legacy requests for auto are a compatibility spelling for tmux.
                raise SSHTransportError("PERSISTENCE_UNAVAILABLE", "该 VPS 暂不支持持久终端")

    async def _record_start_failure(self, session, transport, exc):
        failure = exc if isinstance(exc, TerminalProtocolError) else classify_error(exc)
        session.reason = failure.code
        session.state = "failed"
        session.input_enabled = False
        session.ended = {
            "output_complete": False,
            "exit_status": None,
            "reason_code": failure.code,
            "remote_end_confirmed": True if failure.code == "REMOTE_SESSION_MISSING" else None,
        }
        await transport.close()
        if session.transport is transport:
            session.transport = session.tmux = None
        # Connection setup has definitively stopped. A possible remote
        # tmux creation still retains its logical identity/reservation,
        # while a failed direct setup needs no idle pumps or byte history.
        session.stopped.set()
        session.attached = False
        tasks = [
            task
            for task in (session.pump_task, session.input_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session.ring = ByteRingBuffer(1, initial_offset=session.ring.end_offset)
        session.changed.set()
        await self._state(session)

    async def _challenge(self, session, name, instructions, prompts, *, closing=False):
        if (
            closing
            and self.clock() >= session.close_authority_until
            or not closing
            and (session.frozen or session.stopped.is_set() or self.clock() >= session.lease_until)
        ):
            return None
        challenge_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        session.challenge, session.challenge_id = future, challenge_id
        # The encrypted channel uses ASCII-escaped JSON. A non-BMP codepoint
        # can occupy three times its UTF-8 byte length in that representation.
        text_budget = max(1, (self.config.control_bytes - 600) // (3 * (len(prompts) + 5)))
        safe_prompts = [
            {"text": _display_text(text, min(512, text_budget)), "echo": bool(echo)}
            for text, echo in prompts[:8]
        ]
        try:
            await self._event(
                session,
                "terminal_auth_challenge",
                challenge_id=challenge_id,
                name=_display_text(name, min(256, text_budget)),
                instruction=_display_text(instructions, min(1024, text_budget * 4)),
                prompts=safe_prompts,
                deadline_seconds=self.config.login_timeout,
            )
            answer = await asyncio.wait_for(future, self.config.login_timeout)
            if len(answer) != len(prompts):
                raise TerminalProtocolError("INVALID_ARGUMENT", "认证回答数量不匹配")
            return answer
        finally:
            if not future.done():
                future.cancel()
            session.challenge = session.challenge_id = None

    async def _open_channel(self, session):
        self._check_maintenance()
        epoch = session.stream_epoch
        self._end_output_pause(session)

        def on_data(data):
            if epoch != session.stream_epoch or session.stopped.is_set():
                return
            session.ring.append(data)
            if session.attached and session.credit is not None:
                backlog = session.ring.end_offset - session.credit.acked_offset
                if backlog >= self.config.ring_bytes - self.config.ssh_window_bytes:
                    session.transport.pause_output()
                    if session.output_paused_at is None:
                        session.output_paused_at = self.clock()
                        session.backpressure_count = min(
                            _DIAGNOSTIC_LIMIT, session.backpressure_count + 1
                        )
            session.changed.set()

        def on_exit(result):
            if epoch != session.stream_epoch or session.stopped.is_set():
                return
            self._end_output_pause(session)
            session.ended = result
            session.input_enabled = False
            session.state = "detached" if session.mode == "tmux" else "exited"
            session.reason = result["reason_code"]
            session.changed.set()

        started = self.clock()
        try:
            await session.transport.open_pty(
                session.target["cols"],
                session.target["rows"],
                on_data,
                on_exit,
                command=session.tmux.attach_command() if session.mode == "tmux" else None,
            )
        finally:
            session.stage_timings_ms["pty_open"] = _elapsed_ms(started, self.clock())

    async def _attach(self, session, payload):
        self._check_target(session, payload)
        async with session.attach_lock:
            self._check_maintenance()
            if session.state not in {"ready", "detached", "exited"} or not _connected(
                session.transport
            ):
                raise TerminalProtocolError("SESSION_NOT_READY", "终端尚未就绪或需要重新连接")
            attachment = _id(payload.get("attachment_id"), "attachment_id")
            renderer = _id(payload.get("renderer_id"), "renderer_id")
            render_epoch = _integer(payload.get("render_epoch"), "render_epoch", 1)
            writer = _integer(payload.get("writer_epoch"), "writer_epoch", 1)
            if writer < session.writer_epoch or (
                writer == session.writer_epoch
                and session.attached
                and attachment != session.attachment_id
            ):
                raise TerminalProtocolError("STALE_WRITER", "终端已由另一窗口接管")
            offset, strategy = await self._attachment_resume(session, payload, renderer)
            self._check_maintenance()
            if session.frozen or session.stopped.is_set() or self.clock() >= session.lease_until:
                raise TerminalProtocolError("AUTH_REVOKED", "终端授权已失效")
            return self._commit_attachment(
                session, renderer, attachment, render_epoch, writer, offset, strategy
            )

    async def _attachment_resume(self, session, payload, renderer):
        offset = _integer(payload.get("rendered_offset", "0"), "rendered_offset")
        epoch = _integer(payload.get("stream_epoch", str(session.stream_epoch)), "stream_epoch", 1)
        same_renderer = renderer == session.renderer_id and epoch == session.stream_epoch
        first_renderer = session.renderer_id is None
        gap = not session.ring.start_offset <= offset <= session.ring.end_offset
        strategy = "resume_unconsumed" if same_renderer else "live"
        session.input_enabled = False
        lifecycle = session.lifecycle_revision
        if payload.get("resume_from_live") is True:
            return await self._continue_after_history_gap(session, lifecycle)
        if session.mode == "tmux" and (
            not first_renderer and not same_renderer or gap or session.ended is not None
        ):
            await self._redraw_attachment(session, lifecycle)
            offset, strategy = 0, "redraw"
        elif not same_renderer and not first_renderer:
            if session.ring.start_offset != 0 or session.resized:
                raise TerminalProtocolError("HISTORY_GAP", "无法完整恢复普通终端画面，请新建终端")
            offset, strategy = 0, "history_replay"
        elif gap or epoch != session.stream_epoch:
            raise TerminalProtocolError("HISTORY_GAP", "请求的终端输出已超出保留范围")
        return offset, strategy

    async def _continue_after_history_gap(self, session, lifecycle):
        if session.mode == "tmux":
            await self._redraw_attachment(session, lifecycle)
            return 0, "redraw"
        if session.ended is not None or not _connected(session.transport):
            raise TerminalProtocolError("SESSION_NOT_RECOVERABLE", "原临时 SSH 已结束，不能继续")
        # This is the verified current cursor of the exact live PTY, not
        # recovered scrollback. Do not read or replay any older bytes.
        return session.ring.end_offset, "live_tail"

    async def _redraw_attachment(self, session, lifecycle):
        await session.tmux.recover()
        self._check_maintenance()
        if (
            lifecycle != session.lifecycle_revision
            or session.frozen
            or session.stopped.is_set()
            or self.clock() >= session.lease_until
        ):
            raise TerminalProtocolError("AUTH_REVOKED", "重绘期间终端授权已失效")
        session.stream_epoch += 1
        session.ring = ByteRingBuffer(self.config.ring_bytes)
        session.ended, session.exit_sent = None, False
        await self._open_channel(session)

    def _commit_attachment(
        self, session, renderer, attachment, render_epoch, writer, offset, strategy
    ):
        session.renderer_id, session.attachment_id = renderer, attachment
        session.render_epoch, session.writer_epoch = render_epoch, writer
        session.credit = OutputCreditWindow(
            attachment,
            render_epoch,
            session.stream_epoch,
            start_offset=offset,
            high_water_bytes=self.config.output_high_water_bytes,
            low_water_bytes=self.config.output_low_water_bytes,
        )
        # Each attachment gets a fresh writer epoch from the Hub. A same-
        # attachment reconnect preserves the tracker and never repeats input.
        if (
            session.tracker is None
            or session.tracker.writer_epoch != writer
            or session.tracker.stream_epoch != session.stream_epoch
        ):
            session.tracker = InputSequenceTracker(
                self.generation,
                session.stream_epoch,
                writer,
                receipt_limit=self.config.input_receipts,
            )
        session.send_offset = offset
        session.exit_sent = False
        session.replay_end = session.ring.end_offset if strategy == "history_replay" else None
        session.attached, session.detached_at = True, None
        session.input_enabled = strategy != "history_replay" and session.ended is None
        session.state = "ready" if session.ended is None else "exited"
        session.reason = ""
        session.changed.set()
        return {
            **session.public(),
            "resume_strategy": "live" if strategy == "live_tail" else strategy,
            **({"history_recovery": "live_tail"} if strategy == "live_tail" else {}),
            "start_offset": str(offset),
            "next_input_seq": str(session.tracker.next_input_seq),
            "output_end_offset": str(session.ring.end_offset),
        }

    def _writer(self, session, payload):
        # Both admission and the input pump call this immediately before the
        # synchronous SSH write, fencing input queued before handoff began.
        self._check_maintenance()
        if (
            not session.attached
            or not session.input_enabled
            or session.frozen
            or self.clock() >= session.lease_until
            or _integer(payload.get("writer_epoch"), "writer_epoch", 1) != session.writer_epoch
            or _integer(payload.get("stream_epoch"), "stream_epoch", 1) != session.stream_epoch
        ):
            raise TerminalProtocolError("STALE_WRITER", "当前终端不可输入或已被接管")

    async def input(self, frame):
        if frame.kind != FrameKind.INPUT:
            raise TerminalProtocolError("INVALID_FRAME", "Agent 只接受输入帧")
        session = self.sessions.get(frame.session_id)
        if session is None or frame.runtime_generation != self.generation:
            raise TerminalProtocolError("STALE_GENERATION", "终端不属于当前 Agent 运行代次")
        self._writer(
            session, {"writer_epoch": frame.writer_epoch, "stream_epoch": frame.stream_epoch}
        )
        if not 1 <= len(frame.payload) <= self.config.input_frame_bytes:
            raise TerminalProtocolError("INPUT_TOO_LARGE", "输入帧超出限制")
        if session.queued_input_bytes + len(frame.payload) > self.config.pending_input_bytes:
            raise TerminalProtocolError("INPUT_BACKPRESSURE", "终端输入队列已满")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            session.input_queue.put_nowait((frame, future))
        except asyncio.QueueFull as exc:
            raise TerminalProtocolError("INPUT_BACKPRESSURE", "终端输入队列已满") from exc
        session.queued_input_bytes += len(frame.payload)
        return await asyncio.shield(future)

    handle_frame = input

    async def _input_pump(self, session):
        try:
            while not session.stopped.is_set():
                frame, future = await session.input_queue.get()
                claimed = False
                tracker = session.tracker
                transport = session.transport
                try:
                    self._writer(
                        session,
                        {"writer_epoch": frame.writer_epoch, "stream_epoch": frame.stream_epoch},
                    )
                    receipt = tracker.claim(
                        frame.sequence,
                        runtime_generation=frame.runtime_generation,
                        stream_epoch=frame.stream_epoch,
                        writer_epoch=frame.writer_epoch,
                    )
                    if receipt.is_new:
                        claimed = True
                        transport.write(frame.payload)
                        session.bytes_in += len(frame.payload)
                        tracker.mark_forwarded(frame.sequence)
                        await transport.wait_writable()
                    if not future.done():
                        future.set_result(
                            {
                                "session_id": session.id,
                                "input_seq": str(frame.sequence),
                                "runtime_generation": frame.runtime_generation,
                                "stream_epoch": str(frame.stream_epoch),
                                "writer_epoch": str(frame.writer_epoch),
                                "status": "forwarded" if receipt.is_new else receipt.status,
                            }
                        )
                except BaseException as exc:
                    if claimed:
                        tracker.mark_uncertain(frame.sequence)
                        if tracker is session.tracker:
                            session.input_enabled = False
                            session.reason = "INPUT_UNCERTAIN"
                    if not future.done():
                        future.set_exception(
                            TerminalProtocolError(
                                "INPUT_UNCERTAIN"
                                if claimed
                                else getattr(exc, "code", "INPUT_CANCELLED"),
                                "输入状态未确认，未自动重发"
                                if claimed
                                else "输入已取消或不再获授权",
                            )
                        )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    await self._state(session)
                finally:
                    session.queued_input_bytes -= len(frame.payload)
                    session.input_queue.task_done()
                    # Do not keep the last keystroke/password alive in the
                    # suspended pump frame while waiting for its next item.
                    frame = future = None
        finally:
            self._cancel_queued_input(session)

    @staticmethod
    def _cancel_queued_input(session):
        while not session.input_queue.empty():
            frame, future = session.input_queue.get_nowait()
            session.queued_input_bytes -= len(frame.payload)
            if not future.done():
                future.set_exception(
                    TerminalProtocolError("INPUT_CANCELLED", "未发送的终端输入已取消")
                )
            session.input_queue.task_done()

    async def acknowledge(self, payload):
        session = self.sessions.get(_id(payload.get("session_id")))
        if session is None or session.credit is None:
            raise TerminalProtocolError("SESSION_NOT_FOUND", "终端 attachment 不存在")
        if payload.get("runtime_generation", self.generation) != self.generation:
            return {"accepted": False}
        accepted = session.credit.acknowledge(
            _integer(payload.get("rendered_offset"), "rendered_offset"),
            attachment_id=payload.get("attachment_id"),
            render_epoch=_integer(payload.get("render_epoch"), "render_epoch", 1),
            stream_epoch=_integer(payload.get("stream_epoch"), "stream_epoch", 1),
        )
        if accepted:
            if session.replay_end is not None and session.credit.acked_offset >= session.replay_end:
                final_offset = session.replay_end
                session.replay_end = None
                session.input_enabled = session.ended is None and not session.frozen
                await self._event(
                    session,
                    "terminal_replay_complete",
                    attachment_id=session.attachment_id,
                    render_epoch=str(session.render_epoch),
                    stream_epoch=str(session.stream_epoch),
                    final_offset=str(final_offset),
                )
                await self._state(session)
            if (
                session.output_paused_at is not None
                and session.transport is not None
                and session.ring.end_offset - session.credit.acked_offset
                <= self.config.output_low_water_bytes
            ):
                self._end_output_pause(session)
                session.transport.resume_output()
            session.changed.set()
        return {"accepted": accepted}

    async def _output_pump(self, session):
        while not session.stopped.is_set():
            await session.changed.wait()
            session.changed.clear()
            if not session.attached or session.credit is None:
                continue
            try:
                while session.attached and session.credit.available_bytes > 0:
                    end = (
                        min(session.ring.end_offset, session.replay_end)
                        if session.replay_end is not None
                        else session.ring.end_offset
                    )
                    if session.send_offset >= end:
                        break
                    count = min(
                        self.config.frame_bytes,
                        session.credit.available_bytes,
                        end - session.send_offset,
                    )
                    data = session.ring.read(session.send_offset, count)
                    if not data:
                        break
                    offset = session.send_offset
                    attachment = session.attachment_id
                    epoch = session.stream_epoch
                    credit = session.credit
                    # Record before await so an immediate browser ACK cannot
                    # race the sender's accounting.
                    credit.record_sent(offset, len(data))
                    session.send_offset += len(data)
                    await self._event(
                        session,
                        "terminal_output",
                        payload=data,
                        offset=str(offset),
                        stream_epoch=str(epoch),
                        writer_epoch=str(session.writer_epoch),
                        attachment_id=attachment,
                        render_epoch=str(session.render_epoch),
                    )
                    if attachment != session.attachment_id or epoch != session.stream_epoch:
                        break
                if (
                    session.ended is not None
                    and not session.exit_sent
                    and session.send_offset >= session.ring.end_offset
                ):
                    session.exit_sent = True
                    snapshot = session.public()
                    await self._event(
                        session,
                        "terminal_exit",
                        **session.ended,
                        stream_epoch=str(session.stream_epoch),
                        attachment_id=session.attachment_id,
                        render_epoch=str(session.render_epoch),
                        state=snapshot["state"],
                        state_revision=snapshot["state_revision"],
                        mode=session.mode,
                        attachment_exit_status=session.ended.get("exit_status"),
                        remote_session_state=snapshot["remote_session_state"],
                        recoverability="reauthenticate" if session.mode == "tmux" else "none",
                        final_offset=str(session.ring.end_offset),
                    )
            except HistoryGap:
                session.input_enabled = False
                session.reason = "HISTORY_GAP"
                await self._detach(session)
                await self._state(session)
            except (TimeoutError, ConnectionError, OSError):
                await self._detach(session)

    async def _detach(self, session):
        session.attached, session.input_enabled = False, False
        session.detached_at = self.clock()
        session.state = "detached" if session.ended is None else session.state
        self._cancel_queued_input(session)
        if session.transport is not None and session.output_paused_at is not None:
            self._end_output_pause(session)
            session.transport.resume_output()

    async def sweep(self):
        now = self.clock()
        for session in list(self.sessions.values()):
            if session.stopped.is_set():
                continue
            try:
                self._authorize(session.project)
            except Exception:
                await self._finish(session, "AUTH_REVOKED")
                continue
            if now >= session.lease_until:
                await self._finish(session, "AUTH_LEASE_EXPIRED", preserve_tmux=True)
                continue
            if (
                session.output_paused_at is not None
                and now - session.output_paused_at >= self.config.attachment_grace_seconds
            ):
                session.reason = "HISTORY_GAP"
                await self._detach(session)
                await self._state(session)
            if not session.attached and session.detached_at is not None:
                await self._retire_detached_channel(session, now)
        self._release_activity_if_idle()

    async def _retire_detached_channel(self, session, now):
        # The transport removes its channel before awaiting SSH close. Holding the
        # same lock as attach ensures a new renderer cannot adopt that missing
        # channel or be overwritten by an earlier cleanup.
        async with session.attach_lock:
            if session.attached or session.detached_at is None or session.stopped.is_set():
                return
            if session.mode != "tmux":
                if now - session.detached_at >= self.config.direct_detach_seconds:
                    await self._finish(session, "DETACH_EXPIRED")
                return
            if now - session.detached_at < self.config.attachment_grace_seconds or session.ended:
                return
            transport, lifecycle = session.transport, session.lifecycle_revision
            if transport is None:
                return
            await transport.close_channel()
            if session.transport is transport and session.lifecycle_revision == lifecycle:
                session.ended = {
                    "output_complete": False,
                    "exit_status": None,
                    "reason_code": "DETACHED",
                }

    async def _sweep_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(min(1.0, self.config.lease_seconds / 4))
                await self.sweep()
        except asyncio.CancelledError:
            raise

    async def _finish(self, session, reason, *, end_remote=False, preserve_tmux=False):
        session.lifecycle_revision += 1
        if not preserve_tmux:
            session.frozen = True
        session.input_enabled = False
        # A lost control WebSocket may cancel its request coroutine. The bounded
        # SSH cleanup must still finish, otherwise stopped sessions would be
        # skipped by the lease sweeper while retaining a live connection.
        while session.finish_task is not None and not session.finish_task.done():
            await asyncio.shield(session.finish_task)
        task = asyncio.create_task(
            self._finish_impl(session, reason, end_remote=end_remote, preserve_tmux=preserve_tmux),
            name="ssh-terminal-finish-" + session.id,
        )
        session.finish_task = task
        task.add_done_callback(self._activity_task_done)
        await asyncio.shield(task)

    async def _finish_impl(self, session, reason, *, end_remote=False, preserve_tmux=False):
        self._end_output_pause(session)
        current = asyncio.current_task()
        await self._cancel_interrupted_close(session, reason, current)
        if session.stopped.is_set():
            await self._record_repeat_finish(session, reason)
            return
        await self._stop_session_tasks(session, current, preserve_tmux=preserve_tmux)
        uncertain = await self._disconnect_session_transport(session, end_remote=end_remote)
        self._record_finish(
            session, reason, uncertain, end_remote=end_remote, preserve_tmux=preserve_tmux
        )
        await self._state(session)

    @staticmethod
    async def _cancel_interrupted_close(session, reason, current):
        if (
            reason not in {"USER_CLOSED", "CLOSE_REQUESTED"}
            and session.close_task is not None
            and session.close_task is not current
            and not session.close_task.done()
        ):
            session.close_authority_until = 0
            session.close_task.cancel()
            await asyncio.gather(session.close_task, return_exceptions=True)

    async def _record_repeat_finish(self, session, reason):
        if reason == "AUTH_REVOKED":
            session.reason, session.state = reason, "frozen" if session.mode == "tmux" else "exited"
            if session.ended is not None:
                session.ended["reason_code"] = reason
            await self._state(session)

    async def _stop_session_tasks(self, session, current, *, preserve_tmux):
        # Fence synchronously before waiting for a network close or an input
        # pump blocked by SSH write backpressure.
        if not preserve_tmux:
            session.frozen = True
        session.input_enabled, session.attached = False, False
        session.writer_epoch += 1
        session.stopped.set()
        session.changed.set()
        if session.challenge is not None and not session.challenge.done():
            session.challenge.cancel()
        self._cancel_queued_input(session)
        tasks = [
            t
            for t in (session.start_task, session.pump_task, session.input_task)
            if t is not None and t is not current and not t.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _disconnect_session_transport(self, session, *, end_remote):
        transport = session.transport
        uncertain = False
        if transport is not None:
            if end_remote and session.mode == "tmux":
                if session.tmux is None:
                    uncertain = True
                else:
                    try:
                        await session.tmux.close()
                    except Exception:
                        uncertain = True
            try:
                await asyncio.wait_for(transport.close(), self.config.shutdown_timeout * 2)
            except (TimeoutError, ConnectionError, OSError):
                uncertain = end_remote
            finally:
                # The logical tmux reference is derived from the session ID.
                # Drop the controller too: it retains the closed connection and
                # its parsed known_hosts data, which is not useful history.
                session.transport = session.tmux = None
        return uncertain

    @staticmethod
    def _record_finish(session, reason, uncertain, *, end_remote, preserve_tmux):
        session.reason = "SSH_CONTROL_UNCERTAIN" if uncertain else reason
        session.state = (
            "close_unknown"
            if uncertain
            else "closing"
            if reason == "CLOSE_REQUESTED"
            else "detached"
            if preserve_tmux and session.mode == "tmux"
            else "frozen"
            if session.mode == "tmux" and not end_remote
            else "exited"
        )
        session.ended = {
            "output_complete": False,
            "exit_status": None,
            "reason_code": session.reason,
            "remote_end_confirmed": not uncertain if end_remote else None,
        }
        session.ring = ByteRingBuffer(1, initial_offset=session.ring.end_offset)

    async def _close_detached_tmux(self, session, payload):
        # A retained tmux task outlives its SSH attachment. Ending it requires a
        # new, currently authorized connection; a local tombstone is not proof
        # that the remote session was killed.
        try:
            password = self._password(payload)
        except TerminalProtocolError as exc:
            raise TerminalProtocolError(
                "REMOTE_END_UNCONFIRMED", "需要重新连接 VPS 后才能结束持久会话"
            ) from exc
        payload.pop("password", None)
        async with session.attach_lock:
            # Reconcile only after a possibly in-flight create is fenced and
            # cancelled. Otherwise has-session could report missing just before
            # that older operation creates the remote session.
            lifecycle = session.lifecycle_revision + 1
            await self._finish(session, "CLOSE_REQUESTED", preserve_tmux=True)
            if lifecycle != session.lifecycle_revision:
                raise TerminalProtocolError("AUTH_REVOKED", "结束请求期间会话授权已变化")
            session.input_enabled, session.attached = False, False
            session.writer_epoch += 1
            session.state = "closing"
            session.close_authority_until = self.clock() + self.config.lease_seconds
            self._cancel_queued_input(session)
            transport = self.connector()
            try:
                # The fresh control-only connection is bounded by the same
                # authority deadline. It never opens a shell or a new tmux.
                async with asyncio.timeout(self.config.lease_seconds):
                    await transport.connect(
                        session.target,
                        password,
                        challenge=lambda n, i, p: self._challenge(session, n, i, p, closing=True),
                    )
                    password = None
                    self._authorize(session.project)
                    if self.clock() >= session.close_authority_until:
                        raise TerminalProtocolError("AUTH_REVOKED", "结束请求的授权已失效")
                    result = await TmuxController(transport, session.id, self.config).close()
                await self._finish(session, "USER_CLOSED")
                session.frozen = True
                session.state, session.reason = "exited", "USER_CLOSED"
                session.ended = {
                    "output_complete": False,
                    "exit_status": None,
                    "reason_code": "USER_CLOSED",
                    "remote_end_confirmed": result["ended"],
                }
                await self._state(session)
                return session.public()
            except asyncio.CancelledError:
                session.state, session.reason = "close_unknown", "REMOTE_END_UNCONFIRMED"
                raise
            except Exception as exc:
                session.state, session.reason = "close_unknown", "REMOTE_END_UNCONFIRMED"
                await self._state(session)
                failure = exc if isinstance(exc, TerminalProtocolError) else classify_error(exc)
                raise TerminalProtocolError(failure.code, failure.message) from None
            finally:
                password = None
                session.close_authority_until = 0
                await transport.close()

    async def close_all(self, reason="AGENT_SHUTDOWN"):
        self._closed = True
        self._admissions.clear()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
        await asyncio.gather(
            *(self._finish(s, reason) for s in self.sessions.values()), return_exceptions=True
        )
        if self._activity_lock is not None:
            # The local owner has shut down. This releases only a local lock;
            # retained tmux sessions remain explicitly unconfirmed/recoverable.
            lock, self._activity_lock = self._activity_lock, None
            lock.close()
