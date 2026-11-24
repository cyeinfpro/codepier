"""Codex app-server native provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any

import jsonschema

from agent.chat_catalog import commands as format_commands, validate_settings
from agent.native_adapter_base import (
    AdapterProtocolError,
    AdapterTransition,
    BaseProviderAdapter,
    SessionFence,
    _selection_dict,
)
from shared.native_contracts import (
    ApprovalPayload,
    InterruptPayload,
    NativeCommand,
    NativeSelection,
    SendPayload,
    SteerPayload,
)

_CODEX_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
}


class CodexAdapter(BaseProviderAdapter):
    """P1.5-compliant adapter for Codex app-server (next-turn model/effort changes)."""

    provider_kind = "codex"

    def __init__(self, *, cwd: str = ".", thread_id: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cwd = cwd
        self.thread_id = thread_id
        self._turn_start_call_id: str | None = None
        self._turn_settings: dict[str, Any] = {}
        self._turn_settings_receipt: str | None = None

    def _call(
        self, method: str, params: dict[str, Any] | None = None, tag: str | None = None
    ) -> str:
        self._call_serial += 1
        key = f"codex-{self._call_serial}"
        self._calls[key] = tag or method
        self._emit_wire({"id": key, "method": method, "params": params or {}})
        return key

    def initialize(
        self, fence: SessionFence, settings: NativeSelection | Mapping[str, Any] | None = None
    ) -> AdapterTransition:
        self._begin_transition()
        self._fence = fence
        if settings is not None:
            parsed = validate_settings(_selection_dict(settings), "codex")
            self._next_settings = {k: v for k, v in parsed.items() if v}
        self._call(
            "initialize",
            {
                "clientInfo": {"name": "codepier", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        return self._finish_transition()

    def command(self, cmd: NativeCommand) -> AdapterTransition:
        self._begin_transition()
        self._check_command_fence(cmd)
        payload = cmd.payload
        action = payload.action
        if action in {"start_session", "resume_session"}:
            self._emit_event(
                "session",
                {"status": "ready" if self._ready else "initializing"},
                receipt=cmd.receipt,
            )
        elif action == "send":
            self._cmd_send(cmd.receipt, payload)
        elif action == "steer":
            self._cmd_steer(cmd.receipt, payload)
        elif action == "interrupt":
            self._cmd_interrupt(cmd.receipt, payload)
        elif action == "answer_approval":
            self._cmd_approval(cmd.receipt, payload)
        elif action == "change_model":
            self._cmd_next_turn_settings(
                cmd.receipt,
                {"model": payload.model, **payload.options},
                payload.expected_settings_revision,
            )
        elif action == "change_options":
            self._cmd_next_turn_settings(
                cmd.receipt, dict(payload.options), payload.expected_settings_revision
            )
        elif action == "stop_session":
            self._cmd_stop(payload.worker_epoch)
        else:
            raise AdapterProtocolError("UNSUPPORTED_ACTION", f"Unsupported Codex action: {action}")
        return self._finish_transition()

    def _cmd_send(self, receipt: str, payload: SendPayload) -> None:
        if not self._ready or self._active_run_id:
            raise AdapterProtocolError("SESSION_NOT_READY", "Codex session is not ready for send")
        if payload.selection_mode == "explicit":
            assert payload.selection is not None
            effective = validate_settings(
                {"model": payload.selection.model, **payload.selection.options}, "codex"
            )
            turn_receipt = None
        else:
            effective = {
                k: v
                for k, v in (self._next_settings or self._confirmed_settings).items()
                if k in {"model", "effort"} and v
            }
            turn_receipt = self._next_settings_receipt
        self._active_run_id = receipt
        self._active_turn_id = None
        self._turn_settings = dict(effective)
        self._turn_settings_receipt = turn_receipt
        self._pending_updates["effective_selection"] = effective
        self._pending_updates["settings_revision"] = self._settings_revision
        self._pending_updates["selection_mode"] = payload.selection_mode
        self._emit_event(
            "command.claimed",
            {
                "receipt": receipt,
                "attempt_id": receipt,
                "provider_instance_id": self.provider_instance_id,
            },
            receipt=receipt,
            run_id=receipt,
        )
        self._emit_event(
            "user",
            {"text": payload.text, "effective_selection": effective},
            receipt=receipt,
            run_id=receipt,
        )
        params = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": payload.text}],
            **effective,
        }
        self._turn_start_call_id = self._call("turn/start", params)

    def _cmd_steer(self, receipt: str, payload: SteerPayload) -> None:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        if not self._active_turn_id or payload.provider_turn_id != self._active_turn_id:
            raise AdapterProtocolError(
                "STALE_TURN_TARGET", "Codex steer requires observed active turn"
            )
        self._emit_event(
            "user",
            {"text": payload.text, "steering": True},
            receipt=receipt,
            run_id=payload.target_run_id,
        )
        self._call(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": self._active_turn_id,
                "input": [{"type": "text", "text": payload.text}],
            },
        )

    def _cmd_interrupt(self, receipt: str, payload: InterruptPayload) -> None:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        if self._active_turn_id and payload.provider_turn_id != self._active_turn_id:
            raise AdapterProtocolError(
                "STALE_TURN_TARGET", "Interrupt turn_id does not match active turn"
            )
        self._interrupted_run_id = payload.target_run_id
        self._pending_interrupt = (
            receipt,
            payload.target_run_id,
            payload.provider_turn_id,
            payload.worker_epoch,
        )
        if self._active_turn_id:
            self._call(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": self._active_turn_id}
            )

    def _cmd_approval(self, receipt: str, payload: ApprovalPayload) -> None:
        req = self._validate_approval_claim(payload)
        result = _validate_codex_approval_answer(req["method"], req["raw"], payload.answer)
        self._pending_approvals.pop(payload.observed_request_id)
        self._emit_wire({"id": payload.observed_request_id, "result": result})
        self._emit_event(
            "approval.resolved",
            {"request_id": payload.observed_request_id, "decision": "answered", "receipt": receipt},
            receipt=receipt,
            run_id=payload.target_run_id,
        )

    def _cmd_next_turn_settings(self, receipt: str, raw: dict[str, Any], expected_rev: int) -> None:
        if expected_rev != self._settings_revision:
            raise AdapterProtocolError(
                "STALE_SETTINGS_REVISION", "Settings revision does not match"
            )
        if not self._ready or self._active_run_id:
            raise AdapterProtocolError(
                "SESSION_NOT_READY", "Codex settings require an idle session"
            )
        selected = validate_settings(raw, "codex")
        if not selected:
            raise AdapterProtocolError("INVALID_SETTINGS", "No settings supplied")
        for key, value in list(selected.items()):
            if value == "":
                if key not in self._defaults:
                    raise AdapterProtocolError("DEFAULT_UNAVAILABLE", "Codex default unavailable")
                selected[key] = self._defaults[key]
        self._settings_generation += 1
        self._next_settings.update(selected)
        self._next_settings_receipt = receipt
        # Next-turn only: do NOT update confirmed_settings ("actual") until turn/start confirms!
        self._pending_updates["next_settings"] = dict(self._next_settings)
        self._pending_updates["next_settings_receipt"] = receipt
        self._emit_event(
            "notice",
            {
                "status": "pending_next_turn",
                "next_settings": dict(self._next_settings),
                "receipt": receipt,
            },
            receipt=receipt,
        )

    def _cmd_stop(self, worker_epoch: int) -> None:
        assert self._fence is not None
        if worker_epoch != self._fence.worker_epoch:
            raise AdapterProtocolError("STALE_WORKER_EPOCH", "Stop worker_epoch is stale")
        if self._active_run_id and self._active_turn_id:
            self._interrupted_run_id = self._active_run_id
            self._call(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": self._active_turn_id}
            )
        self._pending_updates["stop_requested"] = True

    def receive(self, frame: Mapping[str, Any], fence: SessionFence) -> AdapterTransition:
        self._begin_transition()
        self._check_fence(fence)
        method = frame.get("method", "")
        params = frame.get("params") or {}
        if self.thread_id and params.get("threadId") and params["threadId"] != self.thread_id:
            self._emit_unknown_notification("foreign_thread_event")
            return self._finish_transition()
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if turn_id and (
            turn_id in self._closed_turns
            or (self._active_turn_id and turn_id != self._active_turn_id)
        ):
            self._emit_event(
                "notice", {"diagnostic": "late_turn_event", "turn_id": turn_id, "method": method}
            )
            return self._finish_transition()
        if "id" in frame and method:
            self._handle_server_request(frame, method, params)
        elif "id" in frame:
            self._handle_rpc_response(frame)
        else:
            self._handle_notification(method, params)
        return self._finish_transition()

    def _handle_server_request(
        self, frame: Mapping[str, Any], method: str, params: dict[str, Any]
    ) -> None:
        req_id = str(frame["id"])
        if method not in _CODEX_APPROVAL_METHODS or not self._active_run_id:
            self._emit_wire(
                {"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}}
            )
            self._emit_unknown_request(method or "unknown", req_id)
            return
        options = params.get(
            "availableDecisions", ["accept", "acceptForSession", "decline", "cancel"]
        )
        self._register_approval(
            req_id, method, params, frame, options=options if isinstance(options, list) else []
        )

    def _handle_rpc_response(self, frame: Mapping[str, Any]) -> None:
        tag = self._calls.pop(frame.get("id"), "")
        if not tag:
            return
        if tag == "turn/interrupt":
            return
        if tag == "turn/start" and (
            not self._active_run_id or frame.get("id") != self._turn_start_call_id
        ):
            return
        if "error" in frame:
            self._handle_rpc_error(
                tag, str((frame.get("error") or {}).get("message", "Native RPC failed"))
            )
            return
        self._handle_rpc_success(tag, frame.get("result") or {})

    def _handle_rpc_error(self, tag: str, text: str) -> None:
        if tag == "turn/start":
            if self._turn_settings_receipt:
                self._emit_event(
                    "settings.applied",
                    {
                        "receipt": self._turn_settings_receipt,
                        "status": "rejected",
                        "settings_revision": self._settings_revision,
                        "actual": dict(self._confirmed_settings),
                    },
                    receipt=self._turn_settings_receipt,
                )
            self._emit_event("error", {"text": text}, receipt=self._active_run_id)
            self._settle_run("error", text)
            return
        self._emit_event("error", {"text": text})

    def _handle_rpc_success(self, tag: str, data: dict[str, Any]) -> None:
        if tag == "initialize":
            self._emit_wire({"method": "initialized", "params": {}})
            method = "thread/resume" if self.thread_id else "thread/start"
            params = {"cwd": self.cwd, **({"threadId": self.thread_id} if self.thread_id else {})}
            self._call(method, params)
            self._call("model/list", {})
            self._call("config/read", {"cwd": self.cwd, "includeLayers": False})
        elif tag in {"thread/start", "thread/resume"}:
            self.thread_id = data["thread"]["id"]
            self._ready = True
            self._pending_updates["native_thread"] = self.thread_id
            for key in ("model", "reasoningEffort"):
                if key in data:
                    self._confirmed_settings["effort" if key == "reasoningEffort" else key] = data[
                        key
                    ]
            self._emit_event(
                "settings",
                {"actual": dict(self._confirmed_settings), "revision": self._settings_revision},
            )
        elif tag == "config/read":
            cfg = data.get("config", {})
            if isinstance(cfg.get("model"), str):
                self._defaults["model"] = cfg["model"]
                self._confirmed_settings.setdefault("model", cfg["model"])
            if isinstance(cfg.get("model_reasoning_effort"), str):
                self._defaults["effort"] = cfg["model_reasoning_effort"]
                self._confirmed_settings.setdefault("effort", cfg["model_reasoning_effort"])
        elif tag == "skills/list":
            self._emit_event("commands", {"commands": format_commands(data, "codex")})
        elif tag == "turn/start":
            self._confirm_turn_start(data)

    def _confirm_turn_start(self, data: dict[str, Any]) -> None:
        turn_id = (data.get("turn") or {}).get("id")
        if turn_id:
            self._active_turn_id = turn_id
            self._closed_turns.discard(turn_id)
        if self._turn_settings:
            self._confirmed_settings.update(self._turn_settings)
            self._settings_revision += 1
            self._pending_updates["actual_settings"] = dict(self._confirmed_settings)
            self._pending_updates["settings_revision"] = self._settings_revision
            if self._turn_settings_receipt:
                self._next_settings_receipt = None
                self._emit_event(
                    "settings.applied",
                    {
                        "receipt": self._turn_settings_receipt,
                        "status": "applied",
                        "settings_revision": self._settings_revision,
                        "actual": dict(self._confirmed_settings),
                    },
                    receipt=self._turn_settings_receipt,
                )
        if self._interrupted_run_id == self._active_run_id and self._active_turn_id:
            self._call(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": self._active_turn_id}
            )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            self._emit_event(
                "delta",
                {"text": params.get("delta", ""), "item_id": params.get("itemId", "")},
                receipt=self._active_run_id,
            )
        elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            self._emit_event(
                "reasoning.delta", {"text": params.get("delta", "")}, receipt=self._active_run_id
            )
        elif method in {"item/started", "item/completed"}:
            self._handle_item_notification(method, params)
        elif method == "serverRequest/resolved":
            self._handle_server_resolved(params)
        elif method == "turn/started":
            self._active_turn_id = (params.get("turn") or {}).get("id")
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        elif method == "error":
            self._emit_event(
                "error",
                {"text": (params.get("error") or {}).get("message", "Native error")},
                receipt=self._active_run_id,
            )
        else:
            self._emit_unknown_notification(method or "unknown")

    def _handle_item_notification(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item", {})
        if item.get("type") == "agentMessage" and method == "item/completed" and item.get("text"):
            self._emit_event(
                "message",
                {"text": item["text"], "item_id": item.get("id", "")},
                receipt=self._active_run_id,
            )

    def _handle_server_resolved(self, params: dict[str, Any]) -> None:
        key = str(params.get("requestId", ""))
        if key in self._pending_approvals:
            self._pending_approvals.pop(key)
            self._emit_event(
                "approval.resolved",
                {"request_id": key, "decision": "resolved", "receipt": self._active_run_id},
                receipt=self._active_run_id,
            )

    def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn", {})
        if not self._active_turn_id:
            self._active_turn_id = turn.get("id")
        status = {"failed": "error", "interrupted": "interrupted"}.get(
            turn.get("status"), "completed"
        )
        err = (turn.get("error") or {}).get("message", "")
        self._settle_run(status, err)

    def expire(self, now_monotonic: float) -> AdapterTransition:
        self._begin_transition()
        return self._finish_transition()


def _validate_codex_approval_answer(
    method: str, raw: Mapping[str, Any], answer: Any
) -> dict[str, Any]:
    params = raw.get("params", {})
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        choices = params.get("availableDecisions") or (
            "accept",
            "acceptForSession",
            "decline",
            "cancel",
        )
        if answer not in choices:
            raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid Codex approval decision")
        return {"decision": copy.deepcopy(answer)}
    if method == "item/permissions/requestApproval":
        return _validate_codex_permissions(params.get("permissions", {}), answer)
    if method == "mcpServer/elicitation/request":
        return _validate_codex_elicitation(params.get("requestedSchema"), answer)
    raise AdapterProtocolError(
        "UNSUPPORTED_APPROVAL", f"Unsupported Codex approval method: {method}"
    )


def _validate_codex_permissions(requested: Any, answer: Any) -> dict[str, Any]:
    if (
        not isinstance(answer, dict)
        or set(answer) - {"permissions", "scope", "strictAutoReview"}
        or "permissions" not in answer
    ):
        raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid Codex permission payload")
    if not isinstance(answer["permissions"], dict) or not _is_subset(
        answer["permissions"], requested
    ):
        raise AdapterProtocolError("APPROVAL_SCOPE_EXCEEDED", "Permissions exceed observed request")
    if answer.get("scope", "turn") not in {"turn", "session"}:
        raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid permission scope")
    if "strictAutoReview" in answer and answer["strictAutoReview"] is not True:
        raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Cannot weaken strictAutoReview")
    return copy.deepcopy(answer)


def _validate_codex_elicitation(schema: Any, answer: Any) -> dict[str, Any]:
    if (
        not isinstance(answer, dict)
        or set(answer) - {"action", "content"}
        or answer.get("action") not in {"accept", "decline", "cancel"}
    ):
        raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid elicitation answer")
    if answer["action"] == "accept" and schema:
        try:
            jsonschema.validate(answer.get("content"), schema)
        except jsonschema.ValidationError as exc:
            raise AdapterProtocolError(
                "INVALID_APPROVAL_ANSWER", "Answer fails observed schema"
            ) from exc
    return copy.deepcopy(answer)


def _is_subset(value: Any, observed: Any) -> bool:
    if isinstance(value, dict):
        return isinstance(observed, dict) and all(
            k in observed and _is_subset(v, observed[k]) for k, v in value.items()
        )
    if isinstance(value, list):
        return isinstance(observed, list) and all(v in observed for v in value)
    return value == observed or value is None or value is False
