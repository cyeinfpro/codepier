"""Pi RPC native provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


class PiAdapter(BaseProviderAdapter):
    """P1.5-compliant adapter for Pi JSON RPC mode."""

    provider_kind = "pi"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._message_serial = 0
        self._message_id = ""
        self._failure: str | None = None

    def _next_id(self, tag: str) -> str:
        self._call_serial += 1
        key = f"pi-{self._call_serial}"
        self._calls[key] = tag
        return key

    def _call(self, method: str, params: dict[str, Any] | None = None, tag: str | None = None) -> str:
        key = self._next_id(tag or method)
        self._emit_wire({"id": key, "type": method, **(params or {})})
        return key

    def initialize(
        self, fence: SessionFence, settings: NativeSelection | Mapping[str, Any] | None = None
    ) -> AdapterTransition:
        self._begin_transition()
        self._fence = fence
        if settings is not None:
            parsed = validate_settings(_selection_dict(settings), "pi")
            self._next_settings = {k: v for k, v in parsed.items() if v}
        for method in (
            "get_state",
            "get_available_models",
            "get_available_thinking_levels",
            "get_commands",
        ):
            self._call(method)
        return self._finish_transition()

    def command(self, cmd: NativeCommand) -> AdapterTransition:
        self._begin_transition()
        self._check_command_fence(cmd)
        payload = cmd.payload
        action = payload.action
        if action in {"start_session", "resume_session"}:
            self._emit_event("session", {"status": "ready" if self._ready else "initializing"}, receipt=cmd.receipt)
        elif action == "send":
            self._cmd_send(cmd.receipt, payload)
        elif action == "steer":
            self._cmd_steer(cmd.receipt, payload)
        elif action == "interrupt":
            self._cmd_interrupt(cmd.receipt, payload)
        elif action == "answer_approval":
            self._cmd_approval(cmd.receipt, payload)
        elif action == "change_model":
            self._cmd_settings(cmd.receipt, {"model": payload.model, **payload.options}, payload.expected_settings_revision)
        elif action == "change_options":
            self._cmd_settings(cmd.receipt, dict(payload.options), payload.expected_settings_revision)
        elif action == "stop_session":
            self._cmd_stop(cmd.receipt, payload.worker_epoch)
        else:
            raise AdapterProtocolError("UNSUPPORTED_ACTION", f"Unsupported Pi action: {action}")
        return self._finish_transition()

    def _cmd_send(self, receipt: str, payload: SendPayload) -> None:
        if not self._ready or self._active_run_id or self._settings_busy:
            raise AdapterProtocolError("SESSION_NOT_READY", "Pi session is not ready for send")
        effective, rev = self._resolve_send_selection(payload)
        if payload.selection_mode == "explicit":
            self._emit_explicit_selection(effective)
        self._active_run_id = receipt
        self._active_turn_id = receipt
        self._failure = None
        self._pending_updates["effective_selection"] = effective
        self._pending_updates["settings_revision"] = rev
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
        self._emit_event("user", {"text": payload.text, "effective_selection": effective}, receipt=receipt, run_id=receipt)
        self._call("prompt", {"message": payload.text}, tag="prompt")

    def _emit_explicit_selection(self, effective: dict[str, Any]) -> None:
        model = effective.get("model")
        if model and model != self._confirmed_settings.get("model"):
            provider, mid = model.split("/", 1)
            self._call("set_model", {"provider": provider, "modelId": mid}, tag="explicit_model")
        effort = effective.get("effort")
        if effort and effort != self._confirmed_settings.get("effort"):
            self._call("set_thinking_level", {"level": effort}, tag="explicit_effort")

    def _cmd_steer(self, receipt: str, payload: SteerPayload) -> None:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        self._emit_event("user", {"text": payload.text, "steering": True}, receipt=receipt, run_id=payload.target_run_id)
        self._call("steer", {"message": payload.text}, tag="steer")

    def _cmd_interrupt(self, receipt: str, payload: InterruptPayload) -> None:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        self._interrupted_run_id = payload.target_run_id
        self._pending_interrupt = (receipt, payload.target_run_id, payload.provider_turn_id, payload.worker_epoch)
        self._call("abort", tag="abort")

    def _cmd_approval(self, receipt: str, payload: ApprovalPayload) -> None:
        req = self._validate_approval_claim(payload)
        method = req["method"]
        reply: dict[str, Any] = {"type": "extension_ui_response", "id": payload.observed_request_id}
        ans = payload.answer
        if ans is None:
            reply["cancelled"] = True
        elif method == "confirm" and type(ans) is bool:
            reply["confirmed"] = ans
        elif method in {"select", "input", "editor"} and isinstance(ans, str):
            if method == "select" and ans not in req["raw"].get("options", []):
                raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid select choice")
            reply["value"] = ans
        else:
            raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid Pi approval answer")
        self._pending_approvals.pop(payload.observed_request_id)
        self._emit_wire(reply)
        self._emit_event(
            "approval.resolved",
            {"request_id": payload.observed_request_id, "decision": "answered", "receipt": receipt},
            receipt=receipt,
            run_id=payload.target_run_id,
        )

    def _cmd_settings(self, receipt: str, raw: dict[str, Any], expected_rev: int) -> None:
        if expected_rev != self._settings_revision:
            raise AdapterProtocolError("STALE_SETTINGS_REVISION", "Settings revision does not match")
        if not self._ready or self._active_run_id or self._settings_busy:
            raise AdapterProtocolError("SESSION_NOT_READY", "Pi settings require an idle session")
        selected = validate_settings(raw, "pi")
        if not selected:
            raise AdapterProtocolError("INVALID_SETTINGS", "No settings supplied")
        for key, value in list(selected.items()):
            if value == "":
                if key not in self._defaults:
                    raise AdapterProtocolError("DEFAULT_UNAVAILABLE", "Pi default unavailable")
                selected[key] = self._defaults[key]
        self._settings_busy = True
        self._settings_receipt = receipt
        self._settings_generation += 1
        self._settings_requested = selected
        self._settings_before = dict(self._confirmed_settings)
        self._settings_deadline = self._clock() + 15.0
        if selected.get("model"):
            provider, mid = selected["model"].split("/", 1)
            self._call("set_model", {"provider": provider, "modelId": mid}, "settings_model")
        else:
            self._call("get_available_thinking_levels", tag="settings_levels")

    def _cmd_stop(self, receipt: str, worker_epoch: int) -> None:
        assert self._fence is not None
        if worker_epoch != self._fence.worker_epoch:
            raise AdapterProtocolError("STALE_WORKER_EPOCH", "Stop worker_epoch is stale")
        if self._active_run_id:
            self._interrupted_run_id = self._active_run_id
            self._call("abort", tag="abort")
        self._pending_updates["stop_requested"] = True

    def receive(self, frame: Mapping[str, Any], fence: SessionFence) -> AdapterTransition:
        self._begin_transition()
        self._check_fence(fence)
        kind = frame.get("type")
        if kind == "response":
            self._handle_response(frame)
        elif kind == "message_start" and (frame.get("message") or {}).get("role") == "assistant":
            self._message_serial += 1
            self._message_id = f"pi-message-{self._message_serial}"
        elif kind == "message_update":
            self._handle_message_update(frame)
        elif kind == "message_end":
            self._handle_message_end(frame)
        elif kind == "extension_ui_request":
            self._handle_ui_request(frame)
        elif kind == "agent_settled":
            self._handle_settled(frame)
        else:
            self._emit_unknown_notification(str(kind or "unknown"))
        return self._finish_transition()

    def _handle_response(self, frame: Mapping[str, Any]) -> None:
        tag = self._calls.pop(frame.get("id", ""), "")
        if not tag:
            return
        if tag.startswith("readback_settings:"):
            self._handle_readback_response(tag, frame)
            return
        if tag.startswith("settings_"):
            self._handle_settings_step(tag, frame)
            return
        if tag == "abort":
            return
        if not frame.get("success", False):
            err = str(frame.get("error", "Native command failed"))
            if tag == "prompt":
                self._settle_run("error", err)
            else:
                self._emit_event("error", {"text": err})
            return
        self._handle_ok_response(tag, frame.get("data") or {})

    def _handle_ok_response(self, tag: str, data: dict[str, Any]) -> None:
        if tag == "get_state":
            self._apply_pi_state(data)
        elif tag == "get_commands":
            self._emit_event("commands", {"commands": format_commands(data, "pi")})
        elif tag == "get_available_models":
            self._pending_updates["models"] = data.get("models", []) if isinstance(data, dict) else data
        elif tag == "get_available_thinking_levels":
            self._pending_updates["thinking_levels"] = data.get("levels", []) if isinstance(data, dict) else data

    def _apply_pi_state(self, data: dict[str, Any]) -> None:
        self._ready = True
        native = data.get("model") or {}
        if isinstance(native, dict) and native.get("provider") and native.get("id"):
            model_id = f"{native['provider']}/{native['id']}"
            self._defaults.setdefault("model", model_id)
            self._confirmed_settings["model"] = model_id
        if isinstance(data.get("thinkingLevel"), str):
            self._defaults.setdefault("effort", data["thinkingLevel"])
            self._confirmed_settings["effort"] = data["thinkingLevel"]
        self._emit_event("settings", {"actual": dict(self._confirmed_settings), "revision": self._settings_revision})

    def _handle_settings_step(self, tag: str, frame: Mapping[str, Any]) -> None:
        if not self._settings_busy:
            return
        receipt = self._settings_receipt
        if not frame.get("success", False):
            self._abort_settings_with_readback(receipt, str(frame.get("error", "Native setting rejected")))
            return
        data = frame.get("data") or {}
        if tag == "settings_model":
            self._call("get_available_thinking_levels", tag="settings_levels")
        elif tag == "settings_levels":
            levels = data.get("levels", []) if isinstance(data, dict) else data
            effort = self._settings_requested.get("effort")
            if effort and effort not in levels:
                self._abort_settings_with_readback(receipt, "Unsupported thinking level for Pi model")
            elif effort:
                self._call("set_thinking_level", {"level": effort}, "settings_effort")
            else:
                self._call("get_state", tag="settings_confirm")
        elif tag == "settings_effort":
            self._call("get_state", tag="settings_confirm")
        elif tag == "settings_confirm":
            self._confirm_settings_state(receipt, data)

    def _abort_settings_with_readback(self, receipt: str | None, error_text: str) -> None:
        self._calls = {k: v for k, v in self._calls.items() if not v.startswith("settings_")}
        self._settings_busy = False
        self._settings_receipt = None
        if receipt:
            self._emit_event("error", {"text": error_text}, receipt=receipt)
            tag = f"readback_settings:{receipt}"
            self._readbacks[tag] = (
                receipt,
                dict(self._settings_requested),
                dict(self._settings_before),
                self._settings_generation,
                self._clock() + 15.0,
            )
            self._call("get_state", tag=tag)

    def _confirm_settings_state(self, receipt: str | None, data: dict[str, Any]) -> None:
        parsed = _parse_pi_actual(data)
        requested = self._settings_requested
        if requested.get("model") and parsed.get("model") != requested["model"]:
            self._abort_settings_with_readback(receipt, "Pi model acknowledgement mismatch")
            return
        if requested.get("effort") and parsed.get("effort") != requested["effort"]:
            self._abort_settings_with_readback(receipt, "Pi effort acknowledgement mismatch")
            return
        self._confirmed_settings.update(parsed)
        self._settings_revision += 1
        self._settings_busy = False
        self._settings_receipt = None
        if receipt:
            self._emit_event(
                "settings.applied",
                {
                    "receipt": receipt,
                    "status": "applied",
                    "settings_revision": self._settings_revision,
                    "actual": dict(self._confirmed_settings),
                },
                receipt=receipt,
            )
        self._pending_updates["settings_revision"] = self._settings_revision
        self._pending_updates["actual_settings"] = dict(self._confirmed_settings)

    def _handle_readback_response(self, tag: str, frame: Mapping[str, Any]) -> None:
        entry = self._readbacks.pop(tag, None)
        if entry is None:
            return
        receipt, requested, before, generation, _ = entry
        if generation != self._settings_generation:
            return
        data = frame.get("data")
        if not frame.get("success", False) or not isinstance(data, dict):
            self._emit_readback(receipt, "unknown", {})
            return
        actual = _parse_pi_actual(data)
        complete = all(k in actual for k in requested)
        matches = complete and all(actual.get(k) == v for k, v in requested.items())
        changed = any(actual.get(k) != before.get(k) for k in actual)
        status = "applied" if matches else "partial" if complete and changed else "rejected" if complete else "unknown"
        if actual and status != "unknown":
            self._confirmed_settings.update(actual)
            self._settings_revision += 1
        self._emit_readback(receipt, status, actual)

    def _emit_readback(self, receipt: str, status: str, actual: dict[str, Any]) -> None:
        self._emit_event(
            "settings.applied",
            {
                "receipt": receipt,
                "status": status,
                "settings_revision": self._settings_revision,
                "actual": actual,
            },
            receipt=receipt,
        )
        self._emit_event("settings_result", {"receipt": receipt, "outcome": status, "actual": actual}, receipt=receipt)

    def _handle_message_update(self, frame: Mapping[str, Any]) -> None:
        event = frame.get("assistantMessageEvent") or {}
        etype = event.get("type")
        if etype == "text_delta":
            self._emit_event("delta", {"text": event.get("delta", ""), "item_id": self._message_id}, receipt=self._active_run_id)
        elif etype == "thinking_delta":
            self._emit_event("reasoning.delta", {"text": event.get("delta", ""), "item_id": self._message_id}, receipt=self._active_run_id)

    def _handle_message_end(self, frame: Mapping[str, Any]) -> None:
        message = frame.get("message") or {}
        if message.get("role") == "assistant":
            text = "".join(c.get("text", "") for c in message.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
            if text:
                self._emit_event("message", {"text": text, "item_id": self._message_id}, receipt=self._active_run_id)
        if message.get("stopReason") in {"error", "aborted"}:
            self._failure = str(message.get("errorMessage", "Native response failed"))
            self._emit_event("error", {"text": self._failure}, receipt=self._active_run_id)

    def _handle_ui_request(self, frame: Mapping[str, Any]) -> None:
        method = frame.get("method")
        req_id = str(frame.get("id", ""))
        if method in {"select", "confirm", "input", "editor"} and req_id:
            details = {k: v for k, v in frame.items() if k != "_deadline"}
            timeout = frame["timeout"] / 1000.0 if isinstance(frame.get("timeout"), (int, float)) else None
            self._register_approval(req_id, method, details, frame, timeout_seconds=timeout, options=frame.get("options", []))
            return
        if method == "notify":
            self._emit_event("notice", {"text": str(frame.get("message", ""))}, receipt=self._active_run_id)
            return
        if req_id:
            self._emit_wire({"type": "extension_ui_response", "id": req_id, "cancelled": True})
        self._emit_unknown_request(str(method or "extension_ui_request"), req_id)

    def _handle_settled(self, frame: Mapping[str, Any]) -> None:
        if not self._active_run_id:
            return
        run_id = frame.get("run_id")
        if run_id and run_id != self._active_run_id:
            return
        status = "interrupted" if self._interrupted_run_id == self._active_run_id else ("error" if self._failure else "completed")
        self._settle_run(status, self._failure or "")

    def expire(self, now_monotonic: float) -> AdapterTransition:
        self._begin_transition()
        if self._settings_busy and now_monotonic > self._settings_deadline:
            receipt = self._settings_receipt
            self._settings_busy = False
            self._settings_receipt = None
            if receipt:
                self._emit_readback(receipt, "unknown", {})
        for tag, entry in list(self._readbacks.items()):
            receipt, _, _, generation, deadline = entry
            if now_monotonic <= deadline:
                continue
            self._readbacks.pop(tag)
            self._calls = {k: v for k, v in self._calls.items() if v != tag}
            if generation == self._settings_generation:
                self._emit_readback(receipt, "unknown", {})
        return self._finish_transition()


def _parse_pi_actual(data: Mapping[str, Any]) -> dict[str, Any]:
    actual: dict[str, Any] = {}
    native_model = data.get("model")
    if isinstance(native_model, dict) and isinstance(native_model.get("provider"), str) and isinstance(native_model.get("id"), str):
        if native_model["provider"] and native_model["id"]:
            actual["model"] = f"{native_model['provider']}/{native_model['id']}"
    if isinstance(data.get("thinkingLevel"), str):
        actual["effort"] = data["thinkingLevel"]
    return actual
