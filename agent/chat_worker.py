"""Independent native JSON pipe owner; no browser RPC passthrough or PTY parsing."""

from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import copy
from collections import deque
from dataclasses import dataclass
import jsonschema
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.native_cli import (
    database,
    identifier,
    WorkerLock,
    SESSION_QUOTA,
    TOTAL_QUOTA,
    retained_bytes,
    ATTACHMENT_CAP,
    append_spool_bytes_tx,
    immediate,
)
from shared.native_contracts import NativeCommand
from shared.native_flags import resolve_native_flags

MAX_LINE = 64 * 1024 * 1024  # Native Pi events may echo one bounded 20 MiB image as base64.
MAX_EVENT = 256 * 1024


from agent.chat_catalog import public_model, commands, CAPABILITIES, COMMANDS, validate_settings
from agent.native_adapters import (
    AdapterProtocolError,
    NativeAdapterRegistry,
    ProviderInstanceRegistry,
    SessionFence,
)
from agent.native_process import structured_chat_platform_support
from agent.native_project_knowledge import NativeProjectKnowledge, reference_text
from agent.native_review_worker import NativeReviewWorker
from agent.native_turn_store import NativeTurnStore, TurnKey, TurnRecord
from agent.resource_queue import Claim, canonical_path
from agent.workspace_ownership import WorkspaceOwnershipStore


def bounded_event(event):
    if event.get("type") == "settings":
        if "model" in event:
            event["model"] = public_model(event["model"])
        if "models" in event:
            event["models"] = [public_model(m) for m in event["models"][:200]]
    data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) <= MAX_EVENT:
        return data
    # Tool snapshots and model inventories can be huge. Never silently present
    # truncated approvals as actionable; they require the node-local native CLI.
    if event.get("type") == "approval":
        event = {
            "type": "error",
            "receipt": event.get("receipt"),
            "text": "Native approval exceeds display limit; interrupt and use the node-local CLI",
            "truncated": True,
        }
    else:
        event = {
            k: v
            for k, v in event.items()
            if k in {"type", "receipt", "tool_id", "name", "status", "item_id"}
        }
        event["text"] = data[:60000].decode("utf-8", errors="replace")
        event["truncated"] = True
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def image_inputs(attachments, provider):
    """The manager has bound validated uploads; recheck identity before reading."""
    images, files = [], []
    for attachment in attachments:
        path = Path(attachment["path"])
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > ATTACHMENT_CAP
        ):
            raise ValueError("Attachment is no longer a valid upload")
        if attachment.get("size") is not None and path.stat().st_size != attachment["size"]:
            raise ValueError("Attachment size changed after upload")
        if attachment.get("sha256"):
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != attachment["sha256"]:
                    raise ValueError("Attachment checksum changed after upload")
        mime = attachment.get("mime", "")
        if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            if provider == "pi":
                images.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(path.read_bytes()).decode(),
                        "mimeType": mime,
                    }
                )
            elif provider == "claude":
                images.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(path.read_bytes()).decode(),
                        },
                    }
                )
            else:
                images.append({"type": "localImage", "path": str(path)})
        else:
            files.append(str(path))
    return images, files


class Protocol:
    """Allowlisted native protocol translator. Only replies to observed requests."""

    def __init__(
        self,
        provider,
        row,
        send,
        emit,
        finish,
        persist,
        *,
        on_accept=None,
        on_terminal=None,
        on_capture_wake=None,
    ):
        self.provider, self.row = provider, row
        self.send, self.emit, self.finish, self.persist = send, emit, finish, persist
        self._on_accept = on_accept
        self._on_terminal = on_terminal
        self._on_capture_wake = on_capture_wake
        self.active = None
        self.turn = None
        self.turn_start_call = None
        self.closed_turns = deque(maxlen=64)
        self.thread = row.get("native_thread", "")
        self.ready = False
        self.pending = {}
        self.calls = {}
        self.serial = 0
        self.interrupted = False
        self.failure = None
        saved = json.loads(row.get("chat_settings", "{}"))
        self.settings = saved.get("actual", {})
        self.settings["capabilities"] = dict(CAPABILITIES, auto_compaction=False, auto_retry=False)
        self.settings_busy = False
        self.settings_receipt = None
        self.settings_remaining = 0
        self.next_settings = saved.get("next", {})
        self.next_settings_receipt = saved.get("next_receipt")
        self.turn_settings_receipt = None
        self.selection = {}
        self.operations = {}
        self.refresh_calls = {}
        self.refresh_errors = set()
        self.refresh_receipt = None
        self.settings_deadline = 0
        self.settings_readbacks = {}
        self.settings_readback_deadlines = {}
        self.settings_generation = 0
        self.compaction_receipt = None
        self.operation_deadlines = {}
        self.defaults = saved.get("defaults", {})
        self.turn_settings = {}
        self.message_serial = 0
        self.message_id = ""
        self.command_prompt = False
        self.knowledge_context = None

    def _notify_accept(self, receipt, *, native_thread_id=None, native_turn_id=None):
        if self._on_accept and receipt:
            self._on_accept(
                receipt,
                native_thread_id=native_thread_id,
                native_turn_id=native_turn_id,
            )

    def _notify_terminal(self, receipt, status, text):
        if self._on_terminal and receipt:
            return self._on_terminal(receipt, status, text)
        return None

    def _notify_capture_wake(self, receipt):
        if self._on_capture_wake and receipt:
            self._on_capture_wake(receipt)

    def call(self, method, params=None, tag=None):
        self.serial += 1
        key = "codepier-" + str(self.serial)
        self.calls[key] = tag or method
        if self.refresh_receipt:
            self.refresh_calls[key] = self.refresh_receipt
            self.operation_deadlines[key] = time.monotonic() + 15
        if self.provider == "pi":
            self.send({"id": key, "type": method, **(params or {})})
        else:
            self.send({"id": key, "method": method, "params": params or {}})
        return key

    def start(self):
        if self.provider == "pi":
            self.call("get_state")
            self.call("get_available_models")
            self.call("get_available_thinking_levels")
            self.call("get_commands")
        else:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "codepier", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )

    def prompt(self, receipt, payload):
        if self.active or not self.ready or self.settings_busy:
            raise ValueError("Native session is not ready")
        images, files = image_inputs(payload.get("attachments", []), self.provider)
        text = payload["text"]
        reference = reference_text(self.knowledge_context, text, self.emit, receipt=receipt)
        if files:
            text += "\n\nAttached files:\n" + "\n".join(files)
        self.active, self.interrupted, self.failure = receipt, False, None
        self.message_id = ""
        self.command_prompt = self.provider == "pi" and payload["text"].lstrip().startswith("/")
        self.emit(
            "user",
            receipt=receipt,
            text=payload["text"],
            attachments=[
                {"name": a.get("name", ""), "mime": a.get("mime", "")}
                for a in payload.get("attachments", [])
            ],
        )
        if self.provider == "pi":
            message = text + ("\n\n" + reference if reference else "")
            self.call(
                "prompt", {"message": message, **({"images": images} if images else {})}, "prompt"
            )
        else:
            self.turn_settings = dict(self.next_settings)
            self.turn_settings_receipt = self.next_settings_receipt
            content = [{"type": "text", "text": text}] + images
            if reference:
                content.append({"type": "text", "text": reference})
            params = {"threadId": self.thread, "input": content, **self.turn_settings}
            self.turn_start_call = self.call("turn/start", params)

    def save_settings(self):
        self.row["chat_settings"] = json.dumps(
            {
                "actual": self.settings,
                "next": self.next_settings,
                "defaults": self.defaults,
                "next_receipt": self.next_settings_receipt,
            }
        )
        self.persist(self.thread)

    def publish_settings(self):
        self.settings["model"] = public_model(self.settings.get("model"))
        self.settings["models"] = [public_model(m) for m in self.settings.get("models", [])]
        current = self.settings.get("configured_model")
        if (
            self.provider == "codex"
            and current
            and not any(current in (m.get("id"), m.get("model")) for m in self.settings["models"])
        ):
            self.settings["models"].append(
                dict(
                    id=current,
                    model=current,
                    displayName=current + " (configured)",
                    configured=True,
                )
            )
        if self.provider == "codex":
            selected = self.next_settings.get("model") or self.settings.get("model")
            match = next(
                (m for m in self.settings["models"] if selected in (m.get("id"), m.get("model"))),
                {},
            )
            self.settings["thinking_levels"] = [
                x["reasoningEffort"]
                for x in match.get("supportedReasoningEfforts", [])
                if "reasoningEffort" in x
            ]
        self.save_settings()
        self.emit("settings", **self.settings)

    def change_settings(self, receipt, payload):
        if self.active or self.settings_busy or not self.ready:
            raise ValueError("Wait for the native turn to finish before changing settings")
        selected = validate_settings(payload, self.provider)
        if not selected:
            raise ValueError("No settings supplied")
        for key, value in list(selected.items()):
            if value == "":
                if key not in self.defaults:
                    raise ValueError(
                        "Native default unavailable; refresh native configuration first"
                    )
                selected[key] = self.defaults[key]
        if self.provider == "codex":
            self.next_settings.update(selected)
            self.next_settings_receipt = receipt
            self.save_settings()
            self.finish(receipt, "completed")
            self.emit(
                "settings_pending",
                receipt=receipt,
                **self.next_settings,
                text="Accepted for the next turn; native validation occurs at turn/start",
            )
            return
        self.settings_busy, self.settings_receipt = True, receipt
        self.settings_deadline = time.monotonic() + 15
        self.settings_generation += 1
        self.selection = selected
        self.settings_before = dict(self.settings)
        self.emit("settings_pending", receipt=receipt, **selected)
        if selected.get("model"):
            provider, mid = selected["model"].split("/", 1)
            self.call("set_model", {"provider": provider, "modelId": mid}, "settings_model")
        else:
            self.call("get_available_thinking_levels", tag="settings_levels")

    def settings_response(self, tag, success, data=None, error="Native setting rejected"):
        receipt = self.settings_receipt
        if not self.settings_busy:
            return
        if not success:
            for key in [
                k for k, v in self.calls.items() if isinstance(v, str) and v.startswith("settings_")
            ]:
                self.calls.pop(key, None)
            self.settings_busy = False
            self.settings_receipt = None
            self.finish(receipt, "error")
            self.emit("error", receipt=receipt, text=error)
            self.emit("settings_pending", receipt=receipt, cleared=True)
            if receipt is None:
                raise RuntimeError("Initial native settings rejected: " + error)
            tag = "readback_settings:" + receipt
            self.settings_readbacks[tag] = (
                receipt,
                dict(self.selection),
                dict(self.settings_before),
                self.settings_generation,
            )
            self.settings_readback_deadlines[tag] = time.monotonic() + 15
            self.call("get_state", tag=tag)
            return
        data = data or {}
        if tag == "settings_model":
            self.call("get_available_thinking_levels", tag="settings_levels")
        elif tag == "settings_levels":
            levels = data.get("levels", []) if isinstance(data, dict) else data
            self.settings["thinking_levels"] = levels
            effort = self.selection.get("effort")
            if effort and effort not in levels:
                self.settings_response(
                    tag, False, error="Unsupported thinking level for selected native model"
                )
            elif effort:
                self.call("set_thinking_level", {"level": effort}, "settings_effort")
            else:
                self.call("get_state", tag="settings_confirm")
        elif tag == "settings_effort":
            self.call("get_state", tag="settings_confirm")
        elif tag == "settings_confirm":
            self.settings.update({k: data[k] for k in ("model", "thinkingLevel") if k in data})
            model = data.get("model") or {}
            if self.selection.get("model") and self.selection["model"] != model.get(
                "provider", ""
            ) + "/" + model.get("id", ""):
                self.settings_response(tag, False, error="Native model acknowledgement mismatch")
                return
            if (
                self.selection.get("effort")
                and data.get("thinkingLevel") != self.selection["effort"]
            ):
                self.settings_response(
                    tag, False, error="Native thinking level acknowledgement mismatch"
                )
                return
            self.next_settings.update(self.selection)
            self.settings_busy = False
            self.settings_receipt = None
            self.publish_settings()
            self.emit("settings", receipt=receipt, **self.settings)
            self.finish(receipt, "completed")
            self.emit("settings_pending", receipt=receipt, cleared=True)

    def expire_settings_readbacks(self):
        for tag, deadline in list(self.settings_readback_deadlines.items()):
            if time.monotonic() <= deadline:
                continue
            self.settings_readback_deadlines.pop(tag, None)
            receipt, _, _, generation = self.settings_readbacks.pop(tag)
            for key, value in list(self.calls.items()):
                if value == tag:
                    self.calls.pop(key, None)
            if generation == self.settings_generation:
                self.emit("settings_result", receipt=receipt, outcome="unknown")

    def operation(self, receipt, name, method, params=None):
        key = self.call(method, params)
        self.operations[key] = (receipt, name)
        self.operation_deadlines[key] = time.monotonic() + (600 if name == "compact" else 15)
        if name == "compact":
            self.compaction_receipt = receipt
            self.compaction_deadline = time.monotonic() + 600
        self.emit("command_result", receipt=receipt, name=name, state="running")

    def operation_response(self, m):
        op = self.operations.pop(m.get("id"), None)
        if not op:
            return False
        self.operation_deadlines.pop(m.get("id"), None)
        receipt, name = op
        if name == "compact" and receipt != self.compaction_receipt:
            # Completion events can precede the RPC acknowledgement. An old
            # acknowledgement cannot settle a later compaction command.
            self.calls.pop(m.get("id"), None)
            return True
        success = m.get("success", False) if self.provider == "pi" else "error" not in m
        state = "completed" if success else "error"
        if name == "compact" and self.provider == "codex" and success:
            self.calls.pop(m.get("id"), None)
            self.emit("command_result", receipt=receipt, name=name, state="accepted")
            return True
        self.finish(receipt, state)
        if name == "compact" and (self.provider == "pi" or not success):
            self.compaction_receipt = None
        self.calls.pop(m.get("id"), None)
        self.emit("command_result", receipt=receipt, name=name, state=state)
        if not success:
            self.emit(
                "error",
                receipt=receipt,
                text="Native " + name + " rejected: " + str(m.get("error", "unavailable"))[:1000],
            )
        if success and name == "compact":
            self.refresh_receipt = receipt
            try:
                self.refresh()
            finally:
                self.refresh_receipt = None
        return True

    def refresh(self):
        if self.provider == "pi":
            for method in (
                "get_state",
                "get_available_models",
                "get_available_thinking_levels",
                "get_commands",
                "get_session_stats",
            ):
                self.call(method)
        else:
            self.settings["models"] = []
            self.call("model/list")
            self.call("skills/list", {"cwds": [self.row["cwd"]]})
            self.call("config/read", {"cwd": self.row["cwd"], "includeLayers": False})
            self.emit("stats", stats=self.settings.get("stats", {}))

    def command(self, receipt, payload):
        name = payload["name"]
        if name not in COMMANDS[self.provider]:
            raise ValueError("Unsupported native command")
        if name == "compact" and self.compaction_receipt:
            raise ValueError("Native compaction is already active")
        if self.provider == "pi" and name in ("set_auto_compaction", "set_auto_retry"):
            raise ValueError(
                "Installed Pi persists this option globally; session-only toggle unavailable"
            )
        if name == "refresh":
            self.refresh_receipt = receipt
            try:
                self.refresh()
            finally:
                self.refresh_receipt = None
            self.emit("command_result", receipt=receipt, name=name, state="running")
        elif self.provider == "pi":
            params = {"enabled": payload["enabled"]} if name.startswith("set_auto_") else {}
            if name == "compact" and payload.get("instructions"):
                params["customInstructions"] = payload["instructions"]
            self.operation(receipt, name, name, params)
        else:
            if payload.get("instructions"):
                raise ValueError("Codex compact does not support instructions")
            self.operation(receipt, name, "thread/compact/start", {"threadId": self.thread})

    def steer(self, receipt, payload):
        if not self.active or self.provider == "codex" and not self.turn:
            raise ValueError(
                "Steer requires an observed active native turn; send a prompt while idle"
            )
        images, files = image_inputs(payload.get("attachments", []), self.provider)
        text = payload["text"] + ("\n\nAttached files:\n" + "\n".join(files) if files else "")
        reference = reference_text(
            self.knowledge_context, payload["text"], self.emit, receipt=receipt
        )
        self.emit(
            "user",
            receipt=receipt,
            parent_receipt=self.active,
            steering=True,
            text=payload["text"],
            attachments=[
                {"name": a.get("name", ""), "mime": a.get("mime", "")}
                for a in payload.get("attachments", [])
            ],
        )
        if self.provider == "pi":
            message = text + ("\n\n" + reference if reference else "")
            self.operation(
                receipt,
                "steer",
                "steer",
                {"message": message, **({"images": images} if images else {})},
            )
        else:
            content = [{"type": "text", "text": text}] + images
            if reference:
                content.append({"type": "text", "text": reference})
            self.operation(
                receipt,
                "steer",
                "turn/steer",
                {"threadId": self.thread, "expectedTurnId": self.turn, "input": content},
            )

    def interrupt(self):
        if not self.active:
            raise ValueError("No active native turn")
        self.interrupted = True
        if self.provider == "pi":
            self.call("abort")
        elif self.turn:
            self.call("turn/interrupt", {"threadId": self.thread, "turnId": self.turn})
        else:
            # turn/start response can be in flight. Its response issues interrupt.
            return

    def answer(self, request_id, answer, guard=None):
        request = self.pending.get(str(request_id))
        if not request or request.get("_deadline", float("inf")) < time.monotonic():
            raise ValueError("Native request is not pending")
        method = request.get("method")
        if guard is not None:
            details = (
                {k: v for k, v in request.items() if k != "_deadline"}
                if self.provider == "pi"
                else request.get("params", {})
            )
            fingerprint = hashlib.sha256(
                json.dumps({"method": method, "details": details}, sort_keys=True).encode()
            ).hexdigest()
            if (
                guard.get("parent_receipt") != self.active
                or guard.get("request_guard") != fingerprint
            ):
                raise ValueError("Native request changed after answer was queued")
        if self.provider == "pi":
            result = {"type": "extension_ui_response", "id": request["id"]}
            if answer is None:
                result["cancelled"] = True
            elif method == "confirm" and isinstance(answer, bool):
                result["confirmed"] = answer
            elif method in {"select", "input", "editor"} and isinstance(answer, str):
                if method == "select" and answer not in request.get("options", []):
                    raise ValueError("Invalid native choice")
                result["value"] = answer
            else:
                raise ValueError("Invalid native answer")
            self.send(result)
        else:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                choices = request.get("params", {}).get("availableDecisions")
                if (choices is not None and answer not in choices) or (
                    choices is None
                    and answer not in ("accept", "acceptForSession", "decline", "cancel")
                ):
                    raise ValueError("Invalid native approval choice")
                result = {"decision": copy.deepcopy(answer)}
            elif method == "item/permissions/requestApproval":
                requested = request.get("params", {}).get("permissions", {})
                if (
                    not isinstance(answer, dict)
                    or set(answer) - {"permissions", "scope", "strictAutoReview"}
                    or "permissions" not in answer
                ):
                    raise ValueError("Invalid native permission answer")

                def subset(value, observed):
                    if isinstance(value, dict):
                        return isinstance(observed, dict) and all(
                            k in observed and subset(v, observed[k]) for k, v in value.items()
                        )
                    if isinstance(value, list):
                        return isinstance(observed, list) and all(v in observed for v in value)
                    return value == observed or value is None or value is False

                if not isinstance(answer["permissions"], dict) or not subset(
                    answer["permissions"], requested
                ):
                    raise ValueError("Permissions exceed observed request")
                if answer.get("scope", "turn") not in ("turn", "session"):
                    raise ValueError("Invalid permission scope")
                if "strictAutoReview" in answer and answer["strictAutoReview"] is not True:
                    raise ValueError("Cannot weaken native auto review")
                result = copy.deepcopy(answer)
            elif method == "mcpServer/elicitation/request":
                if (
                    not isinstance(answer, dict)
                    or set(answer) - {"action", "content"}
                    or answer.get("action") not in ("accept", "decline", "cancel")
                ):
                    raise ValueError("Invalid elicitation answer")
                schema = request.get("params", {}).get("requestedSchema")
                if answer["action"] == "accept" and schema:
                    if "$ref" in json.dumps(schema):
                        raise ValueError("Referenced elicitation schema requires native client")
                    try:
                        jsonschema.validate(answer.get("content"), schema)
                    except jsonschema.ValidationError:
                        raise ValueError(
                            "Answer does not match observed elicitation schema"
                        ) from None
                elif answer.get("content") is not None:
                    raise ValueError("This elicitation does not accept form content")
                result = copy.deepcopy(answer)
            elif method == "item/tool/requestUserInput":
                questions = request.get("params", {}).get("questions", [])
                if not isinstance(answer, dict) or set(answer) != {q["id"] for q in questions}:
                    raise ValueError("Answer must match pending question IDs")
                if any(
                    not isinstance(v, list) or any(not isinstance(x, str) for x in v)
                    for v in answer.values()
                ):
                    raise ValueError("Invalid question answers")
                result = {"answers": {k: {"answers": list(v)} for k, v in answer.items()}}
            else:
                raise ValueError("This native request requires the node-local CLI")
            self.send({"id": request["id"], "result": result})
        del self.pending[str(request_id)]
        self.emit("approval", request_id=str(request_id), resolved=True)

    def settled(self, status="completed", error=""):
        if not self.active:
            if self.provider == "codex" and self.turn:
                self.closed_turns.append(self.turn)
                self.turn = None
            return
        status = "interrupted" if self.interrupted else ("error" if self.failure else status)
        text = error or self.failure or ""
        receipt = self.active
        done_extra = self._notify_terminal(receipt, status, text)
        if done_extra is None:
            self.finish(receipt, status)
        self.emit("done", status=status, text=text, **(done_extra or {}))
        self._notify_capture_wake(receipt)
        for key in list(self.pending):
            self.emit("approval", request_id=key, resolved=True, resolution="closed")
        if self.provider == "codex" and self.turn:
            self.closed_turns.append(self.turn)
        self.active = self.turn = None
        self.turn_start_call = None
        self.pending.clear()
        if self.provider == "pi":
            self.call("get_session_stats")

    def receive(self, message):
        receipt = self.refresh_calls.pop(message.get("id"), None)
        if receipt:
            self.operation_deadlines.pop(message.get("id"), None)
        emit = self.emit
        if receipt:
            self.refresh_receipt = receipt
            self.emit = lambda kind, **values: emit(kind, **{"receipt": receipt, **values})
            if message.get("error") or self.provider == "pi" and not message.get("success", False):
                self.refresh_errors.add(receipt)
        try:
            if self.provider == "pi":
                self.pi(message)
            else:
                self.codex(message)
        finally:
            self.emit = emit
            self.refresh_receipt = None
            if receipt and receipt not in self.refresh_calls.values():
                state = "error" if receipt in self.refresh_errors else "completed"
                self.refresh_errors.discard(receipt)
                self.finish(receipt, state)
                self.emit("command_result", receipt=receipt, name="refresh", state=state)

    def _pi_prompt_ack(self):
        self._notify_accept(self.active, native_thread_id=self.thread or None)
        if self.command_prompt and self.active:
            self.call("get_state", tag="prompt_idle_check")

    def _codex_turn_start(self, data):
        self.turn = data["turn"]["id"]
        self._notify_accept(
            self.active, native_thread_id=self.thread or None, native_turn_id=self.turn
        )
        # A matching start reply is authoritative even if a native
        # implementation reuses a turn identifier.
        with contextlib.suppress(ValueError):
            self.closed_turns.remove(self.turn)
        if self.turn_settings:
            self.settings.update(self.turn_settings)
            if "effort" in self.turn_settings:
                self.settings["reasoningEffort"] = self.turn_settings["effort"]
            self.publish_settings()
            if self.turn_settings_receipt:
                self.emit("settings", receipt=self.turn_settings_receipt, **self.settings)
                self.emit("settings_pending", receipt=self.turn_settings_receipt, cleared=True)
                self.next_settings_receipt = None
                self.save_settings()
            self.emit("settings_pending", cleared=True)
        if self.interrupted:
            self.interrupt()

    def _codex_turn_started(self, params):
        self.turn = params["turn"]["id"]
        self._notify_accept(
            self.active, native_thread_id=self.thread or None, native_turn_id=self.turn
        )

    def pi(self, m):
        if self.operation_response(m):
            return
        kind = m.get("type")
        if kind == "response":
            tag = self.calls.pop(m.get("id"), "")
            if not tag:
                return
            if tag.startswith("readback_settings:"):
                self.settings_readback_deadlines.pop(tag, None)
                receipt, requested, before, generation = self.settings_readbacks.pop(tag)
                if generation != self.settings_generation:
                    return
                data = m.get("data") or {}
                if not m.get("success", False) or not isinstance(data, dict):
                    self.emit("settings_result", receipt=receipt, outcome="unknown")
                    return
                actual = {k: data[k] for k in ("model", "thinkingLevel") if k in data}
                if "model" in actual:
                    native_model = actual["model"]
                    if not isinstance(native_model, dict) or not all(
                        isinstance(native_model.get(key), str) and native_model[key]
                        for key in ("provider", "id")
                    ):
                        actual.pop("model")
                if "thinkingLevel" in actual and not isinstance(actual["thinkingLevel"], str):
                    actual.pop("thinkingLevel")
                model = actual.get("model") or {}
                values = {
                    "model": model.get("provider", "") + "/" + model.get("id", ""),
                    "effort": actual.get("thinkingLevel"),
                }
                complete = all(
                    ("model" if k == "model" else "thinkingLevel") in actual for k in requested
                )
                matches = complete and all(values.get(k) == v for k, v in requested.items())
                changed = any(actual.get(k) != before.get(k) for k in actual)
                outcome = (
                    "applied"
                    if matches
                    else "partial"
                    if complete and changed
                    else "rejected"
                    if complete
                    else "unknown"
                )
                self.settings.update(actual)
                self.publish_settings()
                self.emit(
                    "settings_result",
                    receipt=receipt,
                    outcome=outcome,
                    actual=actual,
                    requested=requested,
                )
                return
            if tag and tag.startswith("settings_"):
                self.settings_response(
                    tag,
                    m.get("success", False),
                    m.get("data"),
                    m.get("error", "Native setting rejected"),
                )
                return
            if not m.get("success", False):
                if tag == "prompt":
                    self.settled("error", m.get("error", "Native prompt rejected"))
                else:
                    self.emit(
                        "error",
                        receipt=self.refresh_receipt,
                        text=m.get("error", "Native command failed"),
                    )
                    if tag == "get_state" and not self.ready:
                        raise RuntimeError("Native Pi state initialization failed")
                return
            data = m.get("data") or {}
            if tag == "prompt":
                self._pi_prompt_ack()
            elif tag == "prompt_idle_check":
                if (
                    self.active
                    and not self.pending
                    and not data.get("isStreaming")
                    and not data.get("isCompacting")
                    and not data.get("pendingMessageCount")
                ):
                    self.settled()
            elif tag == "get_state":
                initial = not self.ready
                native = data.get("model") or {}
                if native:
                    self.defaults.setdefault(
                        "model", native.get("provider", "") + "/" + native.get("id", "")
                    )
                self.defaults.setdefault("effort", data.get("thinkingLevel", "off"))
                self.ready = True
                self.settings.update(
                    {
                        k: data[k]
                        for k in ("model", "thinkingLevel", "autoCompactionEnabled")
                        if k in data
                    }
                )
                self.publish_settings()
                if initial and any(self.next_settings.values()):
                    self.change_settings(None, self.next_settings)
            elif tag == "get_commands":
                self.emit("commands", commands=commands(data, "pi"))
            elif tag == "get_session_stats":
                self.emit(
                    "stats",
                    stats={
                        k: data[k]
                        for k in (
                            "userMessages",
                            "assistantMessages",
                            "toolCalls",
                            "toolResults",
                            "totalMessages",
                            "tokens",
                            "cost",
                            "contextUsage",
                        )
                        if k in data
                    },
                )
            elif tag == "get_available_models":
                self.settings["models"] = data.get("models", []) if isinstance(data, dict) else data
                self.publish_settings()
            elif tag == "get_available_thinking_levels":
                self.settings["thinking_levels"] = (
                    data.get("levels", []) if isinstance(data, dict) else data
                )
                self.publish_settings()
        elif kind == "message_start" and m.get("message", {}).get("role") == "assistant":
            self.message_serial += 1
            self.message_id = "pi-message-" + str(self.message_serial)
        elif kind == "message_update":
            event = m.get("assistantMessageEvent", {})
            if event.get("type") in ("text_delta", "thinking_delta"):
                self.emit(
                    "delta" if event["type"] == "text_delta" else "reasoning",
                    text=event.get("delta", ""),
                    item_id=self.message_id,
                )
        elif kind == "message_end":
            message = m.get("message", {})
            if message.get("role") == "assistant":
                text = "".join(
                    c.get("text", "") for c in message.get("content", []) if c.get("type") == "text"
                )
                if text:
                    self.emit("message", text=text, item_id=self.message_id)
            if message.get("stopReason") in ("error", "aborted"):
                self.failure = message.get("errorMessage", "Native response failed")
                self.emit("error", text=self.failure)
        elif kind == "auto_retry_end":
            if m.get("success") is True:
                self.failure = ""
                self.emit("status", text="原生重试已成功")
            elif m.get("success") is False:
                self.failure = m.get("errorMessage") or self.failure or "Native retries exhausted"
        elif kind and kind.startswith("tool_execution_"):
            self.emit(
                "tool",
                tool_id=m.get("toolCallId", ""),
                name=m.get("toolName", ""),
                status="error"
                if m.get("isError") is True
                else kind.removeprefix("tool_execution_"),
                isError=m.get("isError") is True,
                text=json.dumps(
                    m.get("result", m.get("partialResult", m.get("args", {}))), ensure_ascii=False
                ),
            )
        elif kind == "extension_ui_request":
            if m.get("method") in ("select", "confirm", "input", "editor"):
                self.pending[str(m["id"])] = json.loads(json.dumps(m))
                if m.get("timeout"):
                    self.pending[str(m["id"])]["_deadline"] = time.monotonic() + m["timeout"] / 1000
                self.emit(
                    "approval",
                    request_id=str(m["id"]),
                    method=m["method"],
                    text=m.get("title", ""),
                    options=m.get("options", []),
                    details=m,
                )
            elif m.get("method") == "notify":
                self.emit("tool", name="Notice", text=m.get("message", ""))
        elif kind in ("compaction_start", "compaction_end"):
            self.emit(
                "command_result",
                receipt=self.compaction_receipt,
                name="compact",
                state="running"
                if kind == "compaction_start"
                else ("error" if m.get("errorMessage") else "completed"),
                text=m.get("errorMessage", ""),
            )
        elif kind == "agent_settled":
            self.settled()

    def codex(self, m):
        if self.operation_response(m):
            return
        method, params = m.get("method", ""), m.get("params", {})
        if self.thread and params.get("threadId") and params["threadId"] != self.thread:
            return
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if turn_id and (turn_id in self.closed_turns or self.turn and turn_id != self.turn):
            # Native events can lag behind completion and a queued follow-up.
            # They must never change that follow-up's text, approvals or state.
            return
        if "id" in m and method:
            self.pending[str(m["id"])] = json.loads(json.dumps(m))
            self.emit(
                "approval",
                request_id=str(m["id"]),
                method=method,
                text=params.get("reason", method),
                details=params,
                options=params.get(
                    "availableDecisions", ["accept", "acceptForSession", "decline", "cancel"]
                )
                if method
                in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")
                else [],
            )
            return
        if "id" in m:
            tag = self.calls.pop(m["id"], "")
            if tag == "turn/start" and (not self.active or m["id"] != self.turn_start_call):
                return
            if tag.startswith("settings_"):
                self.settings_response(tag, "error" not in m)
                if "error" in m:
                    self.emit("error", text=m["error"].get("message", "Native setting rejected"))
                return
            if "error" in m:
                text = m["error"].get("message", "Native RPC failed")
                self.emit(
                    "error",
                    receipt=self.active if tag == "turn/start" else self.refresh_receipt,
                    text=text,
                )
                if tag == "turn/start":
                    if self.turn_settings_receipt:
                        self.emit(
                            "error",
                            receipt=self.turn_settings_receipt,
                            text="Next-turn settings were not accepted: " + text,
                        )
                        self.finish(self.turn_settings_receipt, "error")
                    self.settled("error", text)
                elif tag in ("initialize", "thread/start", "thread/resume"):
                    raise RuntimeError("Native session initialization failed: " + text)
                return
            data = m.get("result") or {}
            if tag == "initialize":
                self.send({"method": "initialized", "params": {}})
                self.call(
                    "thread/resume" if self.thread else "thread/start",
                    {"cwd": self.row["cwd"], **({"threadId": self.thread} if self.thread else {})},
                )
                self.call("model/list", {})
                self.call("skills/list", {"cwds": [self.row["cwd"]]})
                self.call("config/read", {"cwd": self.row["cwd"], "includeLayers": False})
            elif tag in ("thread/start", "thread/resume"):
                self.thread = data["thread"]["id"]
                self.persist(self.thread)
                self.ready = True
                self.settings.update(
                    {k: data[k] for k in ("model", "reasoningEffort") if k in data}
                )
                self.publish_settings()
            elif tag == "skills/list":
                self.emit("commands", commands=commands(data, "codex"))
            elif tag == "config/read":
                config = data.get("config", {})
                current = config.get("model")
                if isinstance(current, str):
                    self.settings["configured_model"] = current
                    self.defaults["model"] = current
                self.defaults["effort"] = config.get("model_reasoning_effort")
                self.publish_settings()
            elif tag == "model/list":
                self.settings["models"] = self.settings.get("models", []) + data.get("data", [])
                self.publish_settings()
                if data.get("nextCursor"):
                    self.call("model/list", {"cursor": data["nextCursor"]})
            elif tag == "turn/start":
                self._codex_turn_start(data)
            return
        if method == "thread/tokenUsage/updated":
            self.settings["stats"] = params.get("tokenUsage", {})
            self.emit("stats", stats=self.settings["stats"])
        elif method == "thread/settings/updated":
            settings = params.get("threadSettings", {})
            self.settings.update({k: settings[k] for k in ("model", "effort") if k in settings})
            self.publish_settings()
        elif method == "item/agentMessage/delta":
            self.emit("delta", text=params.get("delta", ""), item_id=params.get("itemId", ""))
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self.emit("reasoning", text=params.get("delta", ""))
        elif method in ("item/started", "item/completed"):
            item = params.get("item", {})
            if item.get("type") == "contextCompaction":
                self.emit(
                    "command_result",
                    receipt=self.compaction_receipt,
                    name="compact",
                    state="running" if method == "item/started" else "completed",
                )
                if method == "item/completed":
                    if self.compaction_receipt:
                        self.finish(self.compaction_receipt, "completed")
                    self.compaction_receipt = None
                    if not self.active:
                        # This completion releases queued prompts/commands now;
                        # retire the idle compaction turn before admitting them.
                        self.turn = self.turn or turn_id
                        self.settled()
            if (
                item.get("type") == "agentMessage"
                and method == "item/completed"
                and item.get("text")
            ):
                self.emit("message", text=item["text"], item_id=item.get("id", ""))
            if item.get("type") not in ("agentMessage", "userMessage", "reasoning"):
                self.emit(
                    "tool",
                    tool_id=item.get("id", ""),
                    name=item.get("type", ""),
                    status=method.split("/")[-1],
                    text=json.dumps(item, ensure_ascii=False),
                )
        elif method == "item/commandExecution/outputDelta":
            self.emit(
                "tool",
                tool_id=params.get("itemId", ""),
                name="commandExecution",
                status="delta",
                text=params.get("delta", ""),
            )
        elif method == "serverRequest/resolved":
            key = str(params.get("requestId"))
            if key in self.pending:
                del self.pending[key]
                self.emit("approval", request_id=key, resolved=True)
        elif method == "turn/started":
            self._codex_turn_started(params)
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if not self.turn:
                self.turn = turn.get("id")
            state = {"failed": "error", "interrupted": "interrupted"}.get(
                turn.get("status"), "completed"
            )
            error = (turn.get("error") or {}).get("message", "")
            if not self.active and self.compaction_receipt:
                self.finish(self.compaction_receipt, state)
                self.emit(
                    "command_result",
                    receipt=self.compaction_receipt,
                    name="compact",
                    state=state,
                    text=error,
                )
                self.compaction_receipt = None
            self.settled(state, error)
        elif method == "error":
            self.emit("error", text=(params.get("error") or {}).get("message", "Native error"))


@dataclass
class _V2TurnState:
    turn: TurnRecord
    armed: bool = False
    bytes_before: int = 0
    accepted: bool = False


def _run_capture_and_review(worker: NativeReviewWorker, job_id: str) -> None:
    res = worker.run_capture_job(job_id)
    if isinstance(res, dict) and res.get("review_job_id"):
        worker.run_review_job(res["review_job_id"])


class _CaptureV2Manager:
    """Coordinates v2 baseline, write-started fencing, terminal barrier, and async capture."""

    def __init__(self, directory, sid, row, owner_epoch, reviews, *, enabled):
        self.directory = directory
        self.sid = sid
        self.row = row
        self.owner_epoch = owner_epoch
        self.enabled = enabled
        self.turns: dict[str, _V2TurnState] = {}
        self.pending_capture_jobs: dict[str, str] = {}
        self.turn_store = NativeTurnStore()
        self.claims = WorkspaceOwnershipStore()
        self.review_worker = (
            NativeReviewWorker(
                directory,
                lambda _s: reviews.engine(),
                owner=f"chat-worker:{sid}",
                owner_epoch=owner_epoch,
            )
            if (enabled and reviews is not None)
            else None
        )

    def is_v2_turn(self, receipt):
        return receipt in self.turns

    def prepare_prompt(self, db, receipt, reviews):
        if not self.enabled:
            if reviews:
                reviews.begin(receipt)
            return
        sess = db.execute(
            "SELECT workspace_version, provider_instance_id FROM sessions WHERE id=?",
            (self.sid,),
        ).fetchone()
        ws_ver = (
            int(sess["workspace_version"]) if sess else int(self.row.get("workspace_version", 0))
        )
        pid = (sess["provider_instance_id"] if sess else self.row.get("provider_instance_id")) or (
            "0" * 32
        )
        attempt_id = uuid.uuid4().hex
        now_ts = time.time()
        claim = Claim("agent", "path", str(self.row["root"]), True, "process")
        with immediate(db):
            self.claims.acquire_claim_tx(
                db,
                claims=[claim],
                phase="execute",
                receipt=receipt,
                attempt_id=attempt_id,
                owner_epoch=self.owner_epoch,
                workspace_version=ws_ver,
                process_identity={"session_id": self.sid, "pid": os.getpid()},
                now=now_ts,
            )
            turn = self.turn_store.begin_turn_tx(
                db,
                receipt=receipt,
                attempt_id=attempt_id,
                session_id=self.sid,
                provider_instance_id=pid,
                workspace_key=canonical_path(self.row["root"]),
                workspace_version=ws_ver,
                owner_epoch=self.owner_epoch,
                evidence_requested=bool(reviews),
                now=now_ts,
            )
        if reviews:
            prep = reviews.prepare_turn_baseline(receipt, attempt_id, before_ref=turn.before_ref)
            with immediate(db):
                turn = self.turn_store.record_baseline_tx(
                    db,
                    TurnKey(receipt, attempt_id),
                    owner_epoch=self.owner_epoch,
                    expected_state_version=turn.state_version,
                    status=prep["status"],
                    before_ref=prep.get("before_ref"),
                    error_code=prep.get("error_code"),
                    now=time.time(),
                )
        self.turns[receipt] = _V2TurnState(turn=turn)

    def arm_prompt_bytes(self, receipt, bytes_before):
        state = self.turns.get(receipt)
        if state is not None:
            state.armed = True
            state.bytes_before = max(0, int(bytes_before))

    def before_os_write(self, db):
        for state in self.turns.values():
            if state.armed and state.bytes_before == 0 and state.turn.send_phase == "not_sent":
                with immediate(db):
                    state.turn = self.turn_store.mark_write_started_tx(
                        db,
                        TurnKey(state.turn.receipt, state.turn.attempt_id),
                        owner_epoch=self.owner_epoch,
                        expected_state_version=state.turn.state_version,
                        now=time.time(),
                    )

    def on_outgoing_written(self, count):
        for state in self.turns.values():
            if state.armed and state.bytes_before > 0:
                state.bytes_before = max(0, state.bytes_before - count)

    def on_accept(self, db, receipt, *, native_thread_id=None, native_turn_id=None):
        state = self.turns.get(receipt)
        if state is None or state.turn.terminal_event_id is not None:
            return
        if state.accepted and (
            native_turn_id is None or state.turn.native_turn_id == native_turn_id
        ):
            return
        key = TurnKey(state.turn.receipt, state.turn.attempt_id)
        with immediate(db):
            if state.turn.send_phase == "not_sent":
                state.turn = self.turn_store.mark_write_started_tx(
                    db,
                    key,
                    owner_epoch=self.owner_epoch,
                    expected_state_version=state.turn.state_version,
                    now=time.time(),
                )
            state.turn = self.turn_store.mark_accepted_tx(
                db,
                key,
                owner_epoch=self.owner_epoch,
                expected_state_version=state.turn.state_version,
                native_thread_id=native_thread_id,
                native_turn_id=native_turn_id,
                now=time.time(),
            )
        state.accepted = True

    def on_terminal(self, db, flush_fn, receipt, status, text, size_cb):
        state = self.turns.get(receipt)
        if state is None:
            return None
        flush_fn()
        outcome = {
            "error": "failed",
            "failed": "failed",
            "interrupted": "interrupted",
            "uncertain": "uncertain",
        }.get(status, "completed")
        err_code = {
            "interrupted": "INTERRUPTED",
            "failed": "PROVIDER_ERROR",
            "uncertain": "UNCERTAIN",
        }.get(outcome)
        res = self.turn_store.persist_provider_terminal(
            db,
            TurnKey(receipt, state.turn.attempt_id),
            outcome,
            owner_epoch=self.owner_epoch,
            claims=self.claims,
            now=time.time(),
            error_code=err_code,
        )
        if res.get("spool_range"):
            size_cb(res["spool_range"][1])
        evidence = res["evidence_state"]
        review_state = (
            "pending"
            if evidence == "capture_pending"
            else ("not_requested" if evidence == "not_requested" else "unavailable")
        )
        if evidence == "capture_pending":
            self.pending_capture_jobs[receipt] = f"capture:{receipt}:{state.turn.attempt_id}"
        self.turns.pop(receipt, None)
        return {"evidence_state": evidence, "review_state": review_state}

    def wake_capture(self, receipt):
        job_id = self.pending_capture_jobs.pop(receipt, None)
        if job_id and self.review_worker is not None:
            self.review_worker._executor.submit(_run_capture_and_review, self.review_worker, job_id)

    def on_interrupt(self, db, receipt):
        state = self.turns.get(receipt)
        if state is not None and state.turn.send_phase != "not_sent":
            with immediate(db):
                self.turn_store.mark_interrupt_requested_tx(
                    db,
                    TurnKey(state.turn.receipt, state.turn.attempt_id),
                    owner_epoch=self.owner_epoch,
                    expected_native_turn_id=state.turn.native_turn_id,
                    now=time.time(),
                )

    def settle_remaining(self, db, flush_fn, size_cb, *, stop):
        for receipt in list(self.turns):
            state = self.turns[receipt]
            status = (
                "uncertain"
                if state.turn.send_phase == "write_started"
                else ("interrupted" if stop else "error")
            )
            self.on_terminal(db, flush_fn, receipt, status, "", size_cb)
            self.wake_capture(receipt)

    def shutdown(self):
        if self.review_worker is not None:
            self.review_worker.__exit__(None, None, None)


class _AdapterProtocolBridge:
    """Optional NativeAdapterRegistry driver enabled by native_provider_adapter_v2."""

    def __init__(
        self,
        adapter,
        row,
        sid,
        owner_epoch,
        send,
        emit,
        finish,
        persist,
        *,
        on_accept=None,
        on_terminal=None,
        on_capture_wake=None,
    ):
        self.adapter = adapter
        self.provider = adapter.provider_kind
        self.row = row
        self.sid = sid
        self.owner_epoch = owner_epoch
        self.send = send
        self.emit = emit
        self.finish = finish
        self.persist = persist
        self._on_accept = on_accept
        self._on_terminal = on_terminal
        self._on_capture_wake = on_capture_wake
        self.fence = SessionFence(
            session_id=sid,
            provider_instance_id=adapter.provider_instance_id,
            worker_epoch=owner_epoch,
        )
        self.ready = False
        self.active = None
        self.turn = None
        self.settings_busy = False
        self.settings_deadline = 0.0
        self.compaction_receipt = None
        self.compaction_deadline = 0.0
        self.operation_deadlines = {}
        self.knowledge_context = None

    def _cmd(self, receipt, payload):
        return NativeCommand.model_validate(
            {
                "receipt": receipt,
                "session_id": self.sid,
                "provider_instance_id": self.adapter.provider_instance_id,
                "payload": payload,
            }
        )

    def _apply(self, transition):
        for req in transition.wire_requests:
            self.send(req)
        updates = transition.state_updates
        if "native_thread" in updates:
            self.persist(updates["native_thread"])
        self.ready = bool(self.adapter._ready)
        self.active = self.adapter._active_run_id
        self.turn = self.adapter._active_turn_id
        self.settings_busy = bool(self.adapter._settings_busy)
        self.settings_deadline = float(getattr(self.adapter, "_settings_deadline", 0.0))
        if self.active and self._on_accept:
            prompt_pending = any(
                tag in ("prompt", "turn/start") for tag in self.adapter._calls.values()
            )
            if (
                not prompt_pending
                or self.turn
                or any(
                    ev["kind"] in ("delta", "message", "reasoning.delta", "done")
                    for ev in transition.events
                )
            ):
                self._on_accept(
                    self.active,
                    native_thread_id=getattr(self.adapter, "thread_id", None) or None,
                    native_turn_id=self.turn,
                )
        for ev in transition.events:
            self._emit_adapter_event(ev)

    def _emit_adapter_event(self, ev):
        kind = ev["kind"]
        body = ev["body"]
        ev_receipt = ev.get("receipt")
        if kind == "done":
            done_extra = (
                self._on_terminal(ev_receipt, body["status"], body.get("text", ""))
                if self._on_terminal and ev_receipt
                else None
            )
            if done_extra is None and ev_receipt:
                self.finish(ev_receipt, body["status"])
            self.emit(
                "done",
                receipt=ev_receipt,
                status=body["status"],
                text=body.get("text", ""),
                **(done_extra or {}),
            )
            if self._on_capture_wake and ev_receipt:
                self._on_capture_wake(ev_receipt)
            return
        if kind == "settings.applied" and ev_receipt:
            self.finish(ev_receipt, "completed" if body.get("status") == "applied" else "error")
            self.emit("settings", **{**body, "receipt": ev_receipt})
            return
        mapped = "reasoning" if kind == "reasoning.delta" else kind
        if kind == "approval.resolved":
            self.emit(
                "approval",
                receipt=ev_receipt,
                request_id=body.get("request_id"),
                resolved=True,
                resolution=body.get("decision", "resolved"),
            )
            return
        # Some normalized events retain a receipt in the legacy body as
        # well. Prefer the envelope identity and emit a single field.
        fields = {**body, "receipt": ev_receipt} if ev_receipt else body
        self.emit(mapped, **fields)

    def start(self):
        saved = json.loads(self.row.get("chat_settings", "{}"))
        next_sel = saved.get("next") or {}
        settings = (
            {
                "model": next_sel["model"],
                "options": {k: v for k, v in next_sel.items() if k != "model"},
            }
            if next_sel.get("model")
            else None
        )
        self._apply(self.adapter.initialize(self.fence, settings=settings))

    def prompt(self, receipt, payload):
        if payload.get("attachments") or payload.get("attachment_ids"):
            raise AdapterProtocolError(
                "CLI_ADAPTER_ATTACHMENTS_UNSUPPORTED",
                "当前新版原生对话不支持附件，消息未发送。请使用兼容模式发送附件。",
            )
        self._apply(
            self.adapter.command(
                self._cmd(
                    receipt,
                    {
                        "action": "send",
                        "text": payload.get("text", ""),
                        "attachment_ids": payload.get("attachment_ids", []),
                        "selection_mode": "inherit_at_dispatch",
                    },
                )
            )
        )

    def steer(self, receipt, payload):
        self._apply(
            self.adapter.command(
                self._cmd(
                    receipt,
                    {
                        "action": "steer",
                        "target_run_id": self.active,
                        "provider_turn_id": self.turn or self.active,
                        "worker_epoch": self.owner_epoch,
                        "text": payload["text"],
                    },
                )
            )
        )

    def interrupt(self, receipt=None):
        self._apply(
            self.adapter.command(
                self._cmd(
                    receipt or uuid.uuid4().hex,
                    {
                        "action": "interrupt",
                        "target_run_id": self.active,
                        "provider_turn_id": self.turn or self.active,
                        "worker_epoch": self.owner_epoch,
                    },
                )
            )
        )

    def answer(self, request_id, answer, guard=None):
        pending = self.adapter._pending_approvals.get(str(request_id))
        if not pending:
            raise ValueError("Native request is not pending")
        self._apply(
            self.adapter.command(
                self._cmd(
                    uuid.uuid4().hex,
                    {
                        "action": "answer_approval",
                        "observed_request_id": str(request_id),
                        "target_run_id": pending["run_id"],
                        "worker_epoch": self.owner_epoch,
                        "request_digest": pending["digest"],
                        "answer": answer,
                    },
                )
            )
        )

    def change_settings(self, receipt, payload):
        rev = self.adapter._settings_revision
        if "model" in payload:
            cmd_payload = {
                "action": "change_model",
                "model": payload["model"],
                "options": {k: v for k, v in payload.items() if k != "model"},
                "expected_settings_revision": rev,
            }
        else:
            cmd_payload = {
                "action": "change_options",
                "options": dict(payload),
                "expected_settings_revision": rev,
            }
        self._apply(self.adapter.command(self._cmd(receipt, cmd_payload)))

    def command(self, receipt, payload):
        raise ValueError(
            "Adapter v2 command dispatch is unsupported for " + str(payload.get("name"))
        )

    def receive(self, message):
        self._apply(self.adapter.receive(message, self.fence))

    def expire_settings_readbacks(self):
        self._apply(self.adapter.expire(time.monotonic()))

    def settings_response(self, tag, success, data=None, error="Native setting rejected"):
        self.expire_settings_readbacks()

    def settled(self, status="completed", error=""):
        if self.adapter._active_run_id:
            self.adapter._begin_transition()
            self.adapter._settle_run(status, error)
            self._apply(self.adapter._finish_transition())


def _resolve_worker_instance_id(directory, row, argv):
    existing = row.get("provider_instance_id", "")
    if isinstance(existing, str) and len(existing) == 32:
        return existing
    reg = ProviderInstanceRegistry(Path(directory))
    digest = reg.compute_config_digest(argv[0] if argv else row["provider"], "")
    return reg.resolve(
        execution_account="local",
        provider_kind=row["provider"],
        executable_and_config_digest=digest,
    ).provider_instance_id


def _create_protocol(
    flags,
    directory,
    sid,
    row,
    argv,
    owner_epoch,
    platform,
    send,
    emit,
    finish,
    persist,
    v2,
    db,
    flush,
    set_size,
):
    on_accept = (lambda rec, **kw: v2.on_accept(db, rec, **kw)) if flags.native_capture_v2 else None
    on_terminal = (
        (lambda rec, st, txt: v2.on_terminal(db, flush, rec, st, txt, set_size))
        if flags.native_capture_v2
        else None
    )
    on_wake = v2.wake_capture if flags.native_capture_v2 else None
    if flags.native_provider_adapter_v2:
        pid = _resolve_worker_instance_id(directory, row, argv)
        extra = (
            {"cwd": row["cwd"], "thread_id": row.get("native_thread", "")}
            if row["provider"] in ("codex", "claude")
            else {}
        )
        adapter = NativeAdapterRegistry.create(
            row["provider"],
            provider_instance_id=pid,
            platform_supported=platform.available,
            platform_reason=platform.reason_code,
            **extra,
        )
        return _AdapterProtocolBridge(
            adapter,
            row,
            sid,
            owner_epoch,
            send,
            emit,
            finish,
            persist,
            on_accept=on_accept,
            on_terminal=on_terminal,
            on_capture_wake=on_wake,
        )
    cls = None
    if row["provider"] == "claude":
        from agent.claude_protocol import ClaudeProtocol

        cls = ClaudeProtocol
    else:
        cls = Protocol
    return cls(
        row["provider"],
        row,
        send,
        emit,
        finish,
        persist,
        on_accept=on_accept,
        on_terminal=on_terminal,
        on_capture_wake=on_wake,
    )


def _dispatch_interrupt(row, protocol, v2, db, receipt, finish):
    v2.on_interrupt(db, protocol.active)
    if row["provider"] == "claude":
        protocol.interrupt(receipt)
    else:
        protocol.interrupt()
        finish(receipt, "completed")


def _dispatch_worker_command(
    command,
    row,
    protocol,
    reviews,
    v2,
    db,
    outgoing,
    finish,
    emit,
    flush,
    set_size,
):
    try:
        payload = json.loads(command["payload"])
        kind = command["kind"]
        if kind == "stop":
            return True
        if kind == "chat_prompt":
            v2.prepare_prompt(db, command["id"], reviews)
            before_len = len(outgoing)
            protocol.prompt(command["id"], payload)
            v2.arm_prompt_bytes(command["id"], before_len)
        elif kind == "chat_interrupt":
            _dispatch_interrupt(row, protocol, v2, db, command["id"], finish)
        elif kind == "chat_answer":
            protocol.answer(
                payload["request_id"],
                payload["answer"],
                payload if "request_guard" in payload else None,
            )
            finish(command["id"], "completed")
        else:
            handlers = {
                "chat_steer": protocol.steer,
                "chat_command": protocol.command,
                "chat_settings": protocol.change_settings,
            }
            handlers[kind](command["id"], payload)
    except (ValueError, KeyError, OSError, AdapterProtocolError) as exc:
        if command["kind"] == "chat_prompt" and v2.is_v2_turn(command["id"]):
            v2.on_terminal(db, flush, command["id"], "error", str(exc), set_size)
        else:
            finish(command["id"], "error")
        details = {"code": exc.code} if isinstance(exc, AdapterProtocolError) else {}
        emit("error", receipt=command["id"], text=str(exc), **details)
    return False


def _flush_outgoing_stdin(child, outgoing, v2, db, child_exited):
    if not outgoing:
        return
    v2.before_os_write(db)
    try:
        n = os.write(child.stdin.fileno(), outgoing[:65536])
        del outgoing[:n]
        v2.on_outgoing_written(n)
    except BlockingIOError:
        pass
    except BrokenPipeError:
        if not child_exited(child.pid):
            raise


def run(directory, sid, config_path=None, *, flags=None, windows_runtime_validated=False):
    sid = identifier(sid)
    ownership = WorkerLock(directory, sid)
    try:
        db = database(directory)
    except BaseException:
        ownership.close()
        raise
    resolved_flags = resolve_native_flags(flags)
    child = None
    protocol = None
    v2 = None
    stop = False
    status, error = "exited", ""
    selector = selectors.DefaultSelector()
    admitted = False
    buffers = {}
    outgoing = bytearray()
    try:
        raw = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not raw or raw["status"] != "starting":
            return
        admitted = True
        row = dict(raw)
        if row.get("mode") != "chat":
            raise ValueError("Not a structured chat session")
        if db.execute(
            "SELECT 1 FROM commands WHERE session=? AND state='claimed'", (sid,)
        ).fetchone():
            raise RuntimeError("Uncertain native prompt from previous worker; not replayed")
        reviews = None
        if config_path:
            from agent.chat_reviews import TurnReviews

            reviews = TurnReviews(directory, row, config_path, flags=resolved_flags)
        owner_epoch = int(row.get("worker_epoch") or 1)
        v2 = _CaptureV2Manager(
            directory,
            sid,
            row,
            owner_epoch,
            reviews,
            enabled=resolved_flags.native_capture_v2,
        )
        size = row["size"]

        def set_size(new_size):
            nonlocal size
            if new_size > size:
                size = new_size

        pending = bytearray()
        flush_at = None

        def flush():
            nonlocal size, flush_at
            if not pending:
                return
            with immediate(db):
                _, size = append_spool_bytes_tx(db, sid, bytes(pending), now=time.time())
            pending.clear()
            flush_at = None

        def emit(kind, **values):
            nonlocal flush_at
            event = {"type": kind, "receipt": protocol.active if protocol else None, **values}
            # bounded_event already caps one event at MAX_EVENT; only the quota can trip.
            data = bounded_event(event)
            if size + len(pending) + len(data) > SESSION_QUOTA:
                flush()
                raise RuntimeError("TRANSCRIPT_QUOTA")
            if not pending:
                flush_at = time.monotonic() + 0.05
            pending.extend(data)
            if (
                kind not in {"delta", "reasoning"}
                or len(pending) >= 16384
                or time.monotonic() >= flush_at
            ):
                flush()

        def finish(receipt, state):
            flush()
            with immediate(db):
                db.execute("UPDATE commands SET state=? WHERE id=?", (state, receipt))
            if reviews and not v2.is_v2_turn(receipt):
                review = reviews.finish(receipt)
                if review:
                    emit("review", receipt=receipt, **review)

        def persist(thread):
            flush()
            with immediate(db):
                db.execute(
                    "UPDATE sessions SET native_thread=?,chat_settings=?,updated=? WHERE id=?",
                    (thread, protocol.row.get("chat_settings", "{}"), time.time(), sid),
                )

        def send(value):
            data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
            if len(outgoing) + len(data) > 32 * 1024 * 1024:
                raise RuntimeError("Native input buffer quota exceeded")
            outgoing.extend(data)

        def stop_signal(*args):
            nonlocal stop
            stop = True

        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, stop_signal)
        platform = structured_chat_platform_support(
            resolved_flags,
            os_name=os.name,
            windows_runtime_validated=windows_runtime_validated,
        )
        if not platform.available:
            raise RuntimeError(
                f"{platform.reason_code or 'CLI_CHAT_PLATFORM'}: Structured chat pipe worker is not supported on Windows; use node-local CLI"
            )
        argv = json.loads(row["argv"])
        if row["provider"] == "pi":
            from agent.native_pi_bridge import prepare_shell_guard

            argv += ["--extension", str(prepare_shell_guard(Path(directory)))]
        from agent.owned_process_group import child_exited, stop_owned_group

        child = subprocess.Popen(
            argv,
            cwd=row["cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            bufsize=0,
        )
        for pipe in (child.stdout, child.stderr):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
            buffers[pipe] = bytearray()
        os.set_blocking(child.stdin.fileno(), False)
        with immediate(db):
            db.execute(
                "UPDATE sessions SET status='running',worker_pid=?,child_pid=?,heartbeat=?,updated=? WHERE id=?",
                (os.getpid(), child.pid, time.time(), time.time(), sid),
            )
        protocol = _create_protocol(
            resolved_flags,
            directory,
            sid,
            row,
            argv,
            owner_epoch,
            platform,
            send,
            emit,
            finish,
            persist,
            v2,
            db,
            flush,
            set_size,
        )
        if config_path:
            protocol.knowledge_context = NativeProjectKnowledge(directory, row, config_path)

        def receive_output(data):
            buffer = buffers[child.stdout]
            buffer.extend(data)
            while b"\n" in buffer:
                line, _, remaining = buffer.partition(b"\n")
                buffer[:] = remaining
                if line.strip():
                    if len(line) > MAX_LINE:
                        raise RuntimeError("Native JSON line quota exceeded")
                    protocol.receive(json.loads(line))
            if len(buffer) > MAX_LINE:
                raise RuntimeError("Native JSON line quota exceeded")

        protocol.start()
        heartbeat = 0
        initialize_deadline = time.monotonic() + 60
        last_history_check = 0
        while not child_exited(child.pid) and not stop:
            if flush_at is not None and time.monotonic() >= flush_at:
                flush()
            now = time.time()
            if not protocol.ready and time.monotonic() > initialize_deadline:
                raise RuntimeError(
                    "Native initialization timed out after 60 seconds; inspect node-local CLI"
                )
            if now - last_history_check >= 5 and row["provider"] == "pi":
                last_history_check = now
                native_history = (
                    Path(argv[argv.index("--session") + 1]) if "--session" in argv else None
                )
                if (
                    native_history
                    and native_history.exists()
                    and native_history.stat().st_size > SESSION_QUOTA
                ):
                    raise RuntimeError("NATIVE_HISTORY_QUOTA")
            if now - heartbeat >= 1:
                with immediate(db):
                    db.execute("UPDATE sessions SET heartbeat=? WHERE id=?", (now, sid))
                heartbeat = now
                if retained_bytes(db) > TOTAL_QUOTA:
                    raise RuntimeError("TOTAL_QUOTA")
            kinds = (
                ("stop", "chat_interrupt", "chat_answer", "chat_steer", "chat_command")
                if protocol.active
                or not protocol.ready
                or protocol.settings_busy
                or protocol.compaction_receipt
                else (
                    "stop",
                    "chat_interrupt",
                    "chat_answer",
                    "chat_prompt",
                    "chat_settings",
                    "chat_steer",
                    "chat_command",
                )
            )
            command = db.execute(
                "SELECT * FROM commands WHERE session=? AND state='queued' AND kind IN ("
                + ",".join("?" for _ in kinds)
                + ") ORDER BY CASE WHEN kind='stop' THEN 0 ELSE 1 END,rowid LIMIT 1",
                (sid, *kinds),
            ).fetchone()
            if command:
                flush()
                with immediate(db):
                    claimed = db.execute(
                        "UPDATE commands SET state='claimed' WHERE id=? AND session=? AND state='queued'",
                        (command["id"], sid),
                    ).rowcount
                if not claimed:
                    continue
                if _dispatch_worker_command(
                    command,
                    row,
                    protocol,
                    reviews,
                    v2,
                    db,
                    outgoing,
                    finish,
                    emit,
                    flush,
                    set_size,
                ):
                    stop = True
            for key, deadline in list(protocol.operation_deadlines.items()):
                if time.monotonic() > deadline:
                    protocol.receive(
                        {
                            "id": key,
                            "type": "response",
                            "success": False,
                            "error": {"message": "Native command acknowledgement timed out"},
                        }
                    )
            if protocol.compaction_receipt and time.monotonic() > protocol.compaction_deadline:
                finish(protocol.compaction_receipt, "error")
                emit(
                    "error",
                    receipt=protocol.compaction_receipt,
                    text="Native compaction completion timed out",
                )
                protocol.compaction_receipt = None
            protocol.expire_settings_readbacks()
            if protocol.settings_busy and time.monotonic() > protocol.settings_deadline:
                protocol.settings_response(
                    "settings_timeout", False, error="Native settings acknowledgement timed out"
                )
            _flush_outgoing_stdin(child, outgoing, v2, db, child_exited)
            for key, _ in selector.select(0.025):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is child.stderr:
                    # Runtime stderr can contain credentials/config. It is not chat.
                    continue
                receive_output(data)
        if not stop:
            # waitpid/poll observes process exit, not pipe exhaustion. Preserve
            # final messages and completion receipts already written by the CLI.
            while True:
                try:
                    data = os.read(child.stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                receive_output(data)
        flush()
        if not protocol.ready and not stop:
            raise RuntimeError(
                "Native CLI exited before initialization; check login/configuration in node-local CLI"
            )
        if protocol.active:
            protocol.settled(
                "interrupted" if stop else "error", "Native process stopped before completion"
            )
    except BaseException as exc:
        if not admitted:
            raise
        status = "quota_error" if "QUOTA" in str(exc) else "interrupted"
        error = (
            str(exc)[:500] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        )
        if protocol is not None:
            with contextlib.suppress(Exception):
                emit("error", text=error)
    finally:
        try:
            if child:
                try:
                    cleaned, child.returncode = stop_owned_group(child.pid, grouped=True)
                    if not cleaned:
                        status, error = (
                            "orphaned",
                            "Owned native process group cleanup is unverified; inspect Agent host",
                        )
                finally:
                    for pipe in (child.stdin, child.stdout, child.stderr):
                        with contextlib.suppress(OSError):
                            pipe.close()
            selector.close()
            if admitted:
                if db.in_transaction:
                    db.rollback()
                if v2 is not None:
                    with contextlib.suppress(Exception):
                        v2.settle_remaining(db, flush, set_size, stop=stop)
                with immediate(db):
                    db.execute(
                        "UPDATE commands SET state='uncertain' WHERE session=? AND state='claimed' AND kind!='stop'",
                        (sid,),
                    )
                    db.execute(
                        "UPDATE commands SET state=? WHERE session=? AND state='claimed' AND kind='stop'",
                        ("uncertain" if status == "orphaned" else "completed", sid),
                    )
                    db.execute(
                        "UPDATE commands SET state='interrupted' WHERE session=? AND state='queued'",
                        (sid,),
                    )
                    db.execute(
                        "UPDATE sessions SET status=?,error=?,exit_code=?,updated=?,heartbeat=? WHERE id=?",
                        (
                            status,
                            error,
                            child.returncode if child else None,
                            time.time(),
                            time.time(),
                            sid,
                        ),
                    )
        finally:
            if v2 is not None:
                with contextlib.suppress(Exception):
                    v2.shutdown()
            try:
                db.close()
            finally:
                ownership.close()


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
