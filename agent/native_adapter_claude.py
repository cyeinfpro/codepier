"""Claude Code stream-json native provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any
import uuid

from agent.chat_catalog import validate_settings
from agent.claude_cli import catalog as claude_catalog
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
)


class ClaudeAdapter(BaseProviderAdapter):
    """P1.5-compliant adapter for Claude Code stream-json control protocol."""

    provider_kind = "claude"

    def __init__(self, *, cwd: str = ".", thread_id: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cwd = cwd
        self.thread_id = thread_id
        self._closed_results: set[str] = set()
        self._closed_requests: set[str] = set()
        self._failure: str | None = None
        self._had_text = False

    def _call(self, method: str, params: dict[str, Any] | None = None, tag: str | None = None) -> str:
        self._call_serial += 1
        key = f"claude-{self._call_serial}"
        self._calls[key] = f"{method}:{tag or ''}"
        self._emit_wire({"type": "control_request", "request_id": key, "request": {"subtype": method, **(params or {})}})
        return key

    def initialize(
        self, fence: SessionFence, settings: NativeSelection | Mapping[str, Any] | None = None
    ) -> AdapterTransition:
        self._begin_transition()
        self._fence = fence
        if settings is not None:
            parsed = validate_settings(_selection_dict(settings), "claude")
            self._next_settings = {k: v for k, v in parsed.items() if v}
        self._call("initialize", {"hooks": None})
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
            raise AdapterProtocolError("UNSUPPORTED_ACTION", "Claude does not support live steer")
        elif action == "interrupt":
            self._cmd_interrupt(cmd.receipt, payload)
        elif action == "answer_approval":
            self._cmd_approval(cmd.receipt, payload)
        elif action == "change_model":
            self._cmd_change_model(cmd.receipt, payload.model, payload.options, payload.expected_settings_revision)
        elif action == "change_options":
            self._cmd_change_options(cmd.receipt, dict(payload.options), payload.expected_settings_revision)
        elif action == "stop_session":
            self._cmd_stop(payload.worker_epoch)
        else:
            raise AdapterProtocolError("UNSUPPORTED_ACTION", f"Unsupported Claude action: {action}")
        return self._finish_transition()

    def _cmd_send(self, receipt: str, payload: SendPayload) -> None:
        if not self._ready or self._active_run_id or self._settings_busy:
            raise AdapterProtocolError("SESSION_NOT_READY", "Claude session is not ready for send")
        effective, rev = self._resolve_send_selection(payload)
        if payload.selection_mode == "explicit" and effective.get("model") and effective["model"] != self._confirmed_settings.get("model"):
            self._call("set_model", {"model": effective["model"]}, tag="explicit")
        self._active_run_id = receipt
        self._active_turn_id = receipt
        self._failure = None
        self._had_text = False
        self._pending_updates["effective_selection"] = effective
        self._pending_updates["settings_revision"] = rev
        self._pending_updates["selection_mode"] = payload.selection_mode
        self._emit_event(
            "command.claimed",
            {"receipt": receipt, "attempt_id": receipt, "provider_instance_id": self.provider_instance_id},
            receipt=receipt,
            run_id=receipt,
        )
        self._emit_event("user", {"text": payload.text, "effective_selection": effective}, receipt=receipt, run_id=receipt)
        self._emit_wire(
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "session_id": self.thread_id,
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": [{"type": "text", "text": payload.text}]},
            }
        )

    def _cmd_interrupt(self, receipt: str, payload: InterruptPayload) -> None:
        self._check_epoch_and_run(payload.worker_epoch, payload.target_run_id)
        self._interrupted_run_id = payload.target_run_id
        self._pending_interrupt = (receipt, payload.target_run_id, payload.provider_turn_id, payload.worker_epoch)
        self._call("interrupt", tag=receipt)

    def _cmd_approval(self, receipt: str, payload: ApprovalPayload) -> None:
        req = self._validate_approval_claim(payload)
        response = _validate_claude_approval_answer(req["method"], req["raw"], payload.answer)
        self._pending_approvals.pop(payload.observed_request_id)
        self._closed_requests.add(payload.observed_request_id)
        self._emit_wire(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": payload.observed_request_id, "response": response},
            }
        )
        self._emit_event(
            "approval.resolved",
            {"request_id": payload.observed_request_id, "decision": "answered", "receipt": receipt},
            receipt=receipt,
            run_id=payload.target_run_id,
        )

    def _cmd_change_model(
        self, receipt: str, model: str, options: dict[str, Any], expected_rev: int
    ) -> None:
        if expected_rev != self._settings_revision:
            raise AdapterProtocolError("STALE_SETTINGS_REVISION", "Settings revision does not match")
        if not self._ready or self._active_run_id or self._settings_busy:
            raise AdapterProtocolError("SESSION_NOT_READY", "Claude settings require an idle session")
        patch = validate_settings({"model": model, **options}, "claude")
        if "effort" in patch and patch["effort"] != self._confirmed_settings.get("effort"):
            raise AdapterProtocolError("UNSUPPORTED_OPTION", "Claude runtime effort change is unsupported")
        self._settings_busy = True
        self._settings_receipt = receipt
        self._settings_generation += 1
        self._settings_requested = {"model": patch["model"]}
        self._settings_deadline = self._clock() + 15.0
        self._call("set_model", {"model": patch["model"]}, tag=receipt)

    def _cmd_change_options(
        self, receipt: str, options: dict[str, Any], expected_rev: int
    ) -> None:
        if expected_rev != self._settings_revision:
            raise AdapterProtocolError("STALE_SETTINGS_REVISION", "Settings revision does not match")
        patch = validate_settings(options, "claude")
        if "effort" in patch and patch["effort"] != self._confirmed_settings.get("effort"):
            raise AdapterProtocolError("UNSUPPORTED_OPTION", "Claude runtime effort change is unsupported")
        if "model" in patch:
            self._cmd_change_model(receipt, patch["model"], {}, expected_rev)
            return
        raise AdapterProtocolError("INVALID_SETTINGS", "No supported Claude options supplied")

    def _cmd_stop(self, worker_epoch: int) -> None:
        assert self._fence is not None
        if worker_epoch != self._fence.worker_epoch:
            raise AdapterProtocolError("STALE_WORKER_EPOCH", "Stop worker_epoch is stale")
        if self._active_run_id:
            self._interrupted_run_id = self._active_run_id
            self._call("interrupt", tag="stop")
        self._pending_updates["stop_requested"] = True

    def receive(self, frame: Mapping[str, Any], fence: SessionFence) -> AdapterTransition:
        self._begin_transition()
        self._check_fence(fence)
        kind = frame.get("type")
        if kind == "control_response":
            self._handle_control_response(frame.get("response") or {})
        elif kind == "control_request":
            self._handle_control_request(frame)
        elif kind == "control_cancel_request":
            self._handle_cancel_request(frame)
        elif kind == "result":
            self._handle_result(frame)
        elif kind == "stream_event" and self._active_run_id:
            self._handle_stream_event(frame)
        elif kind == "assistant" and self._active_run_id:
            self._handle_assistant(frame)
        else:
            self._emit_unknown_notification(str(kind or "unknown"))
        return self._finish_transition()

    def _handle_control_response(self, response: Mapping[str, Any]) -> None:
        req_id = response.get("request_id")
        call = self._calls.pop(req_id, None)
        if not call:
            return
        method, tag = call.split(":", 1)
        ok = response.get("subtype") == "success"
        data = response.get("response") or {}
        if method == "initialize":
            if not ok:
                raise AdapterProtocolError("INIT_FAILED", "Claude initialize failed")
            info = claude_catalog(data, self.cwd, self._next_settings.get("model", ""))
            self._ready = True
            model = info.get("model")
            if isinstance(model, dict) and model.get("id"):
                self._confirmed_settings["model"] = model["id"]
            if info.get("effort"):
                self._confirmed_settings["effort"] = info["effort"]
            self._emit_event("settings", {"actual": dict(self._confirmed_settings), "revision": self._settings_revision})
            self._emit_event("commands", {"commands": info.get("commands", [])})
        elif method == "set_model":
            self._handle_set_model_response(tag, ok)
        elif method == "interrupt":
            if not ok and self._pending_interrupt and self._pending_interrupt[0] == tag:
                self._interrupted_run_id = None
                self._emit_event("error", {"text": "Claude interrupt rejected"}, receipt=tag or None)

    def _handle_set_model_response(self, receipt: str, ok: bool) -> None:
        if receipt == "explicit":
            return
        self._settings_busy = False
        self._settings_receipt = None
        if ok:
            self._confirmed_settings.update(self._settings_requested)
            self._settings_revision += 1
        status = "applied" if ok else "rejected"
        if receipt:
            self._emit_event(
                "settings.applied",
                {
                    "receipt": receipt,
                    "status": status,
                    "settings_revision": self._settings_revision,
                    "actual": dict(self._confirmed_settings),
                },
                receipt=receipt,
            )

    def _handle_control_request(self, frame: Mapping[str, Any]) -> None:
        req_id = str(frame.get("request_id", ""))
        request = frame.get("request") or {}
        subtype = request.get("subtype")
        if not req_id or req_id in self._pending_approvals or req_id in self._closed_requests:
            return
        if subtype != "can_use_tool":
            self._emit_wire(
                {
                    "type": "control_response",
                    "response": {"subtype": "error", "request_id": req_id, "error": "Unsupported control request"},
                }
            )
            self._emit_unknown_request(str(subtype or "control_request"), req_id)
            return
        if not self._active_run_id or self._interrupted_run_id == self._active_run_id:
            self._emit_wire(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": req_id, "response": {"behavior": "deny", "message": "No active run"}},
                }
            )
            return
        tool = str(request.get("tool_name", ""))
        inputs = request.get("input") if isinstance(request.get("input"), dict) else {}
        method = "claude/question" if tool == "AskUserQuestion" else "confirm"
        details = {"tool_name": tool, "input": copy.deepcopy(inputs), "cwd": self.cwd}
        self._register_approval(req_id, method, details, frame)

    def _handle_cancel_request(self, frame: Mapping[str, Any]) -> None:
        req_id = str(frame.get("request_id", ""))
        if req_id in self._pending_approvals:
            self._pending_approvals.pop(req_id)
            self._closed_requests.add(req_id)
            self._emit_event("approval.resolved", {"request_id": req_id, "decision": "closed", "receipt": self._active_run_id}, receipt=self._active_run_id)

    def _handle_stream_event(self, frame: Mapping[str, Any]) -> None:
        event = frame.get("event") or {}
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self._had_text = True
                self._emit_event("delta", {"text": delta["text"]}, receipt=self._active_run_id)

    def _handle_assistant(self, frame: Mapping[str, Any]) -> None:
        content = (frame.get("message") or {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                self._had_text = True
                self._emit_event("message", {"text": block["text"]}, receipt=self._active_run_id)

    def _handle_result(self, frame: Mapping[str, Any]) -> None:
        identity = frame.get("uuid")
        if identity and identity in self._closed_results:
            return
        if identity:
            self._closed_results.add(str(identity))
        if isinstance(frame.get("session_id"), str) and frame["session_id"]:
            self.thread_id = frame["session_id"]
            self._pending_updates["native_thread"] = self.thread_id
        failed = frame.get("is_error") is True or frame.get("subtype", "success") != "success"
        if not failed and self._active_run_id and not self._had_text and isinstance(frame.get("result"), str):
            self._emit_event("message", {"text": frame["result"]}, receipt=self._active_run_id)
        status = "interrupted" if self._interrupted_run_id == self._active_run_id else ("error" if failed else "completed")
        self._settle_run(status)

    def expire(self, now_monotonic: float) -> AdapterTransition:
        self._begin_transition()
        if self._settings_busy and now_monotonic > self._settings_deadline:
            receipt = self._settings_receipt
            self._settings_busy = False
            self._settings_receipt = None
            if receipt:
                self._emit_event(
                    "settings.applied",
                    {
                        "receipt": receipt,
                        "status": "unknown",
                        "settings_revision": self._settings_revision,
                        "actual": dict(self._confirmed_settings),
                    },
                    receipt=receipt,
                )
        return self._finish_transition()


def _validate_claude_approval_answer(
    method: str, raw: Mapping[str, Any], answer: Any
) -> dict[str, Any]:
    inputs = copy.deepcopy((raw.get("request") or {}).get("input") or {})
    if answer is None or answer is False:
        return {"behavior": "deny", "message": "User declined in CodePier"}
    if method == "confirm" and answer is True:
        return {"behavior": "allow", "updatedInput": inputs}
    if method == "claude/question" and isinstance(answer, dict):
        questions = inputs.get("questions", [])
        expected = {q.get("question") for q in questions if isinstance(q, dict) and q.get("question")}
        if set(answer) != expected:
            raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Answer must match observed questions")
        answers: dict[str, str] = {}
        for q in questions:
            qid = q["question"]
            vals = answer[qid]
            if not isinstance(vals, list) or not vals or (not q.get("multiSelect") and len(vals) != 1):
                raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid question selection")
            answers[qid] = ", ".join(str(v) for v in vals)
        inputs["answers"] = answers
        return {"behavior": "allow", "updatedInput": inputs}
    raise AdapterProtocolError("INVALID_APPROVAL_ANSWER", "Invalid Claude approval answer")
