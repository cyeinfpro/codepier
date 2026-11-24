"""Base contracts, fencing, legacy facade, and shared state helpers for native adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass, field
from hashlib import sha256
import time
from typing import Any, Protocol, runtime_checkable

from agent.chat_catalog import describe_provider_capabilities, validate_settings
from shared.native_contracts import (
    ApprovalPayload,
    NativeCommand,
    NativeSelection,
    ProviderCapabilities,
    SendPayload,
    canonical_jcs_bytes,
)


@dataclass(frozen=True)
class SessionFence:
    """Authoritative session, provider instance, and worker epoch binding."""

    session_id: str
    provider_instance_id: str
    worker_epoch: int

    def __post_init__(self) -> None:
        if type(self.worker_epoch) is not int or self.worker_epoch < 1:
            raise ValueError("worker_epoch must be a positive integer")
        if not self.session_id or not self.provider_instance_id:
            raise ValueError("session_id and provider_instance_id must be non-empty")


@dataclass(frozen=True)
class AdapterTransition:
    """Pure state transition returned by NativeAdapter methods."""

    wire_requests: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    state_updates: dict[str, Any] = field(default_factory=dict)


class AdapterProtocolError(Exception):
    """Structured error raised when a command or frame violates protocol/fence invariants."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@runtime_checkable
class NativeAdapter(Protocol):
    """Normalized provider adapter contract (monotonic clock only for timeouts)."""

    def describe(self) -> ProviderCapabilities: ...

    def initialize(
        self, fence: SessionFence, settings: NativeSelection | Mapping[str, Any] | None = None
    ) -> AdapterTransition: ...

    def command(self, cmd: NativeCommand) -> AdapterTransition: ...

    def receive(self, frame: Mapping[str, Any], fence: SessionFence) -> AdapterTransition: ...

    def expire(self, now_monotonic: float) -> AdapterTransition: ...


def compute_approval_digest(method: str, details: Mapping[str, Any]) -> str:
    """Compute canonical JCS SHA-256 digest for an observed approval request."""
    return sha256(canonical_jcs_bytes({"method": method, "details": dict(details)})).hexdigest()


class LegacyProtocolFacade:
    """Wrap an existing Protocol/ClaudeProtocol instance for zero-regression parity testing."""

    def __init__(
        self,
        protocol: Any,
        *,
        provider_instance_id: str = "0" * 32,
        instance_revision: int = 1,
        provider_version: str = "legacy",
        fence: SessionFence | None = None,
    ) -> None:
        self.protocol = protocol
        self.provider_instance_id = provider_instance_id
        self.instance_revision = instance_revision
        self.provider_version = provider_version
        self.fence = fence
        self._orig_send = protocol.send
        self._orig_emit = protocol.emit
        self._orig_finish = protocol.finish
        self._orig_persist = protocol.persist

    def describe(self) -> ProviderCapabilities:
        return describe_provider_capabilities(
            self.protocol.provider,
            provider_instance_id=self.provider_instance_id,
            instance_revision=self.instance_revision,
            provider_version=self.provider_version,
        )

    def _capture(self, fn: Callable[[], None]) -> AdapterTransition:
        wire: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        updates: dict[str, Any] = {}

        def cap_send(msg: Mapping[str, Any]) -> None:
            copied = copy.deepcopy(dict(msg))
            wire.append(copied)
            self._orig_send(msg)

        def cap_emit(kind: str, **values: Any) -> None:
            ev = {"type": kind, "receipt": self.protocol.active, **copy.deepcopy(values)}
            events.append(ev)
            self._orig_emit(kind, **values)

        def cap_finish(receipt: str, state: str) -> None:
            updates.setdefault("finished", []).append((receipt, state))
            self._orig_finish(receipt, state)

        def cap_persist(thread: str) -> None:
            updates["native_thread"] = thread
            self._orig_persist(thread)

        self.protocol.send = cap_send
        self.protocol.emit = cap_emit
        self.protocol.finish = cap_finish
        self.protocol.persist = cap_persist
        try:
            fn()
        except ValueError as exc:
            raise AdapterProtocolError("ADAPTER_REJECTED", str(exc)) from exc
        finally:
            self.protocol.send = self._orig_send
            self.protocol.emit = self._orig_emit
            self.protocol.finish = self._orig_finish
            self.protocol.persist = self._orig_persist
        return AdapterTransition(
            wire_requests=tuple(wire), events=tuple(events), state_updates=updates
        )

    def initialize(
        self, fence: SessionFence, settings: NativeSelection | Mapping[str, Any] | None = None
    ) -> AdapterTransition:
        self.fence = fence
        if settings is not None:
            sel = _selection_dict(settings)
            if sel:
                self.protocol.next_settings.update(validate_settings(sel, self.protocol.provider))
        return self._capture(self.protocol.start)

    def command(self, cmd: NativeCommand) -> AdapterTransition:
        self._check_cmd_fence(cmd)
        return self._capture(lambda: self._dispatch_legacy(cmd))

    def _dispatch_legacy(self, cmd: NativeCommand) -> None:
        payload = cmd.payload
        action = payload.action
        if action in {"start_session", "resume_session"}:
            if not self.protocol.ready:
                self.protocol.start()
            return
        handlers: dict[str, Callable[[], None]] = {
            "send": lambda: self._legacy_send(cmd.receipt, payload),
            "steer": lambda: self.protocol.steer(
                cmd.receipt, {"text": payload.text, "attachments": []}
            ),
            "interrupt": self.protocol.interrupt,
            "answer_approval": lambda: self.protocol.answer(
                payload.observed_request_id, payload.answer
            ),
            "change_model": lambda: self.protocol.change_settings(
                cmd.receipt, {"model": payload.model, **payload.options}
            ),
            "change_options": lambda: self.protocol.change_settings(
                cmd.receipt, dict(payload.options)
            ),
            "stop_session": self._legacy_stop,
        }
        handler = handlers.get(action)
        if handler is None:
            raise AdapterProtocolError("UNSUPPORTED_ACTION", f"Unsupported legacy action: {action}")
        handler()

    def _legacy_stop(self) -> None:
        if self.protocol.active:
            self.protocol.interrupt()

    def _legacy_send(self, receipt: str, payload: SendPayload) -> None:
        if payload.selection_mode == "explicit" and payload.selection is not None:
            sel = validate_settings(
                {"model": payload.selection.model, **payload.selection.options},
                self.protocol.provider,
            )
            if sel:
                self.protocol.next_settings.update(sel)
        self.protocol.prompt(receipt, {"text": payload.text, "attachments": []})

    def receive(self, frame: Mapping[str, Any], fence: SessionFence) -> AdapterTransition:
        if self.fence is not None and fence != self.fence:
            raise AdapterProtocolError("FENCE_MISMATCH", "Frame fence does not match session fence")
        return self._capture(lambda: self.protocol.receive(dict(frame)))

    def expire(self, now_monotonic: float) -> AdapterTransition:
        return self._capture(self.protocol.expire_settings_readbacks)

    def _check_cmd_fence(self, cmd: NativeCommand) -> None:
        if self.fence is None:
            return
        if cmd.session_id != self.fence.session_id or (
            cmd.provider_instance_id != self.fence.provider_instance_id
        ):
            raise AdapterProtocolError("FENCE_MISMATCH", "Command does not match active fence")


def _selection_dict(settings: NativeSelection | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(settings, NativeSelection):
        return {"model": settings.model, **dict(settings.options)}
    return dict(settings)


class BaseProviderAdapter:
    """Shared P1.5 state machine for Pi, Codex, and Claude native adapters."""

    provider_kind: str = ""

    def __init__(
        self,
        *,
        provider_instance_id: str,
        instance_revision: int = 1,
        provider_version: str = "unknown",
        platform_supported: bool = True,
        platform_reason: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_instance_id = provider_instance_id
        self.instance_revision = instance_revision
        self.provider_version = provider_version
        self.platform_supported = platform_supported
        self.platform_reason = platform_reason
        self._clock = clock
        self._fence: SessionFence | None = None
        self._ready = False
        self._event_seq = 0
        self._call_serial = 0
        self._calls: dict[str, str] = {}
        self._active_run_id: str | None = None
        self._active_turn_id: str | None = None
        self._interrupted_run_id: str | None = None
        self._closed_turns: set[str] = set()
        self._closed_runs: set[str] = set()
        self._pending_interrupt: tuple[str, str, str, int] | None = None
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._confirmed_settings: dict[str, Any] = {}
        self._defaults: dict[str, Any] = {}
        self._next_settings: dict[str, Any] = {}
        self._next_settings_receipt: str | None = None
        self._settings_revision = 0
        self._settings_generation = 0
        self._settings_busy = False
        self._settings_receipt: str | None = None
        self._settings_requested: dict[str, Any] = {}
        self._settings_before: dict[str, Any] = {}
        self._settings_deadline = 0.0
        self._readbacks: dict[str, tuple[str, dict[str, Any], dict[str, Any], int, float]] = {}
        self._pending_wire: list[dict[str, Any]] = []
        self._pending_events: list[dict[str, Any]] = []
        self._pending_updates: dict[str, Any] = {}

    def describe(self) -> ProviderCapabilities:
        return describe_provider_capabilities(
            self.provider_kind,
            provider_instance_id=self.provider_instance_id,
            instance_revision=self.instance_revision,
            provider_version=self.provider_version,
            platform_supported=self.platform_supported,
            platform_reason=self.platform_reason,
        )

    def _begin_transition(self) -> None:
        self._pending_wire.clear()
        self._pending_events.clear()
        self._pending_updates.clear()

    def _finish_transition(self) -> AdapterTransition:
        return AdapterTransition(
            wire_requests=tuple(self._pending_wire),
            events=tuple(self._pending_events),
            state_updates=copy.deepcopy(self._pending_updates),
        )

    def _emit_wire(self, frame: dict[str, Any]) -> None:
        self._pending_wire.append(copy.deepcopy(frame))

    def _emit_event(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        receipt: str | None = None,
        run_id: str | None = None,
    ) -> None:
        assert self._fence is not None
        self._event_seq += 1
        event_id = f"{self._fence.session_id}:{self._fence.worker_epoch}:{self._event_seq}"
        self._pending_events.append(
            {
                "schema_version": 1,
                "session_id": self._fence.session_id,
                "worker_epoch": self._fence.worker_epoch,
                "event_id": event_id,
                "event_seq": self._event_seq,
                "run_id": run_id if run_id is not None else self._active_run_id,
                "receipt": receipt,
                "kind": kind,
                "body": copy.deepcopy(body),
            }
        )

    def _check_fence(self, fence: SessionFence) -> None:
        if self._fence is None:
            raise AdapterProtocolError("NOT_INITIALIZED", "Adapter must be initialized first")
        if fence != self._fence:
            raise AdapterProtocolError("FENCE_MISMATCH", "Session fence mismatch")

    def _check_command_fence(self, cmd: NativeCommand) -> None:
        if self._fence is None:
            raise AdapterProtocolError("NOT_INITIALIZED", "Adapter must be initialized first")
        if cmd.session_id != self._fence.session_id or (
            cmd.provider_instance_id != self._fence.provider_instance_id
        ):
            raise AdapterProtocolError("FENCE_MISMATCH", "Command does not match session fence")

    def _resolve_send_selection(self, payload: SendPayload) -> tuple[dict[str, Any], int]:
        if payload.selection_mode == "explicit":
            assert payload.selection is not None
            raw = {"model": payload.selection.model, **dict(payload.selection.options)}
            return validate_settings(raw, self.provider_kind), self._settings_revision
        resolved = {
            k: v for k, v in self._confirmed_settings.items() if k in {"model", "effort"} and v
        }
        return resolved, self._settings_revision

    def _check_epoch_and_run(self, worker_epoch: int, target_run_id: str) -> None:
        assert self._fence is not None
        if worker_epoch != self._fence.worker_epoch:
            raise AdapterProtocolError("STALE_WORKER_EPOCH", "Command worker_epoch is stale")
        if not self._active_run_id or target_run_id != self._active_run_id:
            raise AdapterProtocolError("STALE_RUN_TARGET", "Target run is not active")

    def _register_approval(
        self,
        request_id: str,
        method: str,
        details: dict[str, Any],
        raw_frame: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        options: list[Any] | None = None,
    ) -> None:
        assert self._fence is not None
        digest = compute_approval_digest(method, details)
        deadline = (self._clock() + timeout_seconds) if timeout_seconds else float("inf")
        self._pending_approvals[request_id] = {
            "method": method,
            "details": copy.deepcopy(details),
            "digest": digest,
            "run_id": self._active_run_id,
            "worker_epoch": self._fence.worker_epoch,
            "raw": copy.deepcopy(dict(raw_frame)),
            "deadline": deadline,
        }
        self._emit_event(
            "approval.requested",
            {
                "request_id": request_id,
                "request_digest": digest,
                "method": method,
                "receipt": self._active_run_id,
            },
            receipt=self._active_run_id,
        )
        self._emit_event(
            "approval",
            {
                "request_id": request_id,
                "method": method,
                "request_digest": digest,
                "options": list(options or []),
                "details": copy.deepcopy(details),
            },
            receipt=self._active_run_id,
        )

    def _validate_approval_claim(self, payload: ApprovalPayload) -> dict[str, Any]:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        req = self._pending_approvals.get(payload.observed_request_id)
        if req is None or req["deadline"] < self._clock():
            raise AdapterProtocolError("APPROVAL_NOT_PENDING", "Approval request is not pending")
        if req["run_id"] != payload.target_run_id or req["worker_epoch"] != payload.worker_epoch:
            raise AdapterProtocolError("APPROVAL_SCOPE_MISMATCH", "Approval belongs to another run")
        if req["digest"] != payload.request_digest:
            raise AdapterProtocolError(
                "APPROVAL_DIGEST_MISMATCH", "Approval request digest changed"
            )
        return req

    def _settle_run(self, status: str, text: str = "") -> None:
        if not self._active_run_id:
            return
        run_id = self._active_run_id
        self._closed_runs.add(run_id)
        if self._active_turn_id:
            self._closed_turns.add(self._active_turn_id)
        for req_id in list(self._pending_approvals):
            self._pending_approvals.pop(req_id)
            self._emit_event(
                "approval.resolved",
                {"request_id": req_id, "decision": "closed", "receipt": run_id},
                receipt=run_id,
                run_id=run_id,
            )
        self._emit_event("done", {"status": status, "text": text}, receipt=run_id, run_id=run_id)
        self._pending_updates["run_settled"] = {"run_id": run_id, "status": status}
        self._active_run_id = None
        self._active_turn_id = None
        self._interrupted_run_id = None
        self._pending_interrupt = None

    def _emit_unknown_notification(self, wire_kind: str) -> None:
        self._emit_event(
            "notice",
            {
                "diagnostic": "unknown_wire_notification",
                "provider": self.provider_kind,
                "wire_kind": wire_kind,
            },
        )

    def _emit_unknown_request(self, wire_kind: str, request_id: str) -> None:
        self._emit_event(
            "notice",
            {
                "diagnostic": "unknown_wire_request",
                "provider": self.provider_kind,
                "wire_kind": wire_kind,
                "request_id": request_id,
                "auto_approved": False,
            },
        )
