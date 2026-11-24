"""Shared Pydantic contracts, RFC 8785 JCS digest, and normalized event decoder.

Single source of truth for native commands, provider capabilities, action
availability, and native event envelopes. Never imports Agent or Hub modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

MAX_SAFE_INT = 2**53 - 1
HEX32_PATTERN = r"^[a-f0-9]{32}$"
_HEX32_RE = re.compile(HEX32_PATTERN)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

SEMANTIC_BATCH_FIELDS = (
    "kind",
    "schema_version",
    "session_id",
    "stream_epoch",
    "from_revision",
    "to_revision",
    "from_cursor",
    "to_cursor",
    "changes",
    "summary_patch",
)

EVENT_CATEGORIES: dict[str, Literal["display", "ordered_content", "critical_control"]] = {
    "token_usage": "display",
    "notice": "display",
    "display_meta": "display",
    "session": "display",
    "stats": "display",
    "status": "display",
    "message.delta": "ordered_content",
    "delta": "ordered_content",
    "message": "ordered_content",
    "user": "ordered_content",
    "assistant": "ordered_content",
    "reasoning.delta": "ordered_content",
    "thinking": "ordered_content",
    "tool": "ordered_content",
    "tool.output.delta": "ordered_content",
    "run.started": "critical_control",
    "run.completed": "critical_control",
    "done": "critical_control",
    "error": "critical_control",
    "provider_completed": "critical_control",
    "evidence_frozen": "critical_control",
    "review_ready": "critical_control",
    "review": "critical_control",
    "command.claimed": "critical_control",
    "approval": "critical_control",
    "approval.requested": "critical_control",
    "approval.resolved": "critical_control",
    "settings": "critical_control",
    "settings_result": "critical_control",
    "settings.applied": "critical_control",
    "queue": "critical_control",
    "commands": "critical_control",
    "workspace.claimed": "critical_control",
    "workspace.released": "critical_control",
    "turn.restored": "critical_control",
}


def strict_safe_int(value: Any, *, minimum: int = 0, maximum: int = MAX_SAFE_INT) -> int:
    """Validate strict JSON safe integers, rejecting bool, float, and strings."""
    if type(value) is not int:
        raise ValueError("Expected strict integer value")
    if value < minimum or value > maximum:
        raise ValueError(f"Integer {value} outside range [{minimum}, {maximum}]")
    return value


def _validate_identifier(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _HEX32_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: expected 32 lowercase hex chars")
    return value


def _jcs_number(value: int | float) -> str:
    if type(value) is int:
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise ValueError("Integer outside IEEE-754 safe integer range for JCS")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite float is forbidden in JCS")
    if value == 0.0:
        return "0"
    if value.is_integer() and abs(value) <= MAX_SAFE_INT:
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)


def _jcs_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Lone surrogate is forbidden in JCS") from exc
    return json.dumps(value, ensure_ascii=False)


def canonical_jcs(value: Any, *, _depth: int = 0) -> str:
    """Serialize JSON value according to RFC 8785 (UTF-16 key sort, strict numbers)."""
    if _depth > 64:
        raise ValueError("JSON nesting exceeds JCS depth limit")
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_jcs(item, _depth=_depth + 1) for item in value) + "]"
    if isinstance(value, Mapping):
        items: list[tuple[bytes, str, Any]] = []
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("Object keys must be strings")
            _jcs_string(k)
            items.append((k.encode("utf-16-be"), k, v))
        items.sort(key=lambda entry: entry[0])
        parts = [
            f"{_jcs_string(k)}:{canonical_jcs(v, _depth=_depth + 1)}"
            for _, k, v in items
        ]
        return "{" + ",".join(parts) + "}"
    raise ValueError(f"Unsupported JCS type: {type(value).__name__}")


def canonical_jcs_bytes(value: Any) -> bytes:
    """Return UTF-8 bytes of RFC 8785 canonical JSON."""
    return canonical_jcs(value).encode("utf-8")


def semantic_batch_payload(batch: Mapping[str, Any]) -> dict[str, Any]:
    """Extract and validate the canonical semantic batch payload."""
    missing = [field for field in SEMANTIC_BATCH_FIELDS if field not in batch]
    if missing:
        raise ValueError("Missing semantic batch fields: " + ", ".join(missing))
    if batch["kind"] != "batch":
        raise ValueError("Invalid batch kind")
    if strict_safe_int(batch["schema_version"], minimum=2, maximum=2) != 2:
        raise ValueError("Unsupported semantic batch schema_version")
    sid = _validate_identifier(batch["session_id"], "session_id")
    epoch = batch["stream_epoch"]
    if not isinstance(epoch, str) or not 1 <= len(epoch) <= 128:
        raise ValueError("Invalid stream_epoch")
    from_rev = strict_safe_int(batch["from_revision"])
    to_rev = strict_safe_int(batch["to_revision"])
    from_cur = strict_safe_int(batch["from_cursor"])
    to_cur = strict_safe_int(batch["to_cursor"])
    if to_rev != from_rev + 1 or to_cur < from_cur:
        raise ValueError("Invalid semantic batch watermark progression")
    changes = batch["changes"]
    summary_patch = batch["summary_patch"]
    if not isinstance(changes, list) or not isinstance(summary_patch, dict):
        raise ValueError("Semantic batch changes must be list and summary_patch dict")
    return {
        "kind": "batch",
        "schema_version": 2,
        "session_id": sid,
        "stream_epoch": epoch,
        "from_revision": from_rev,
        "to_revision": to_rev,
        "from_cursor": from_cur,
        "to_cursor": to_cur,
        "changes": changes,
        "summary_patch": summary_patch,
    }


def semantic_batch_digest(batch: Mapping[str, Any]) -> str:
    """Compute SHA-256(JCS(semantic_payload)), excluding response-only fields."""
    payload = semantic_batch_payload(batch)
    return sha256(canonical_jcs_bytes(payload)).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_schema_dict(
    node: dict[str, Any], depth: int, pending: list[tuple[Any, int]]
) -> None:
    if len(node) > 64:
        raise ValueError("options_schema object exceeds 64 keys")
    for key, val in node.items():
        if not isinstance(key, str) or len(key) > 128:
            raise ValueError("Invalid options_schema key")
        if key == "enum" and isinstance(val, list) and len(val) > 200:
            raise ValueError("options_schema enum exceeds 200 entries")
        if isinstance(val, (dict, list)):
            pending.append((val, depth + 1))


def _validate_schema_list(
    node: list[Any], depth: int, pending: list[tuple[Any, int]]
) -> None:
    if len(node) > 200:
        raise ValueError("options_schema list exceeds 200 entries")
    for item in node:
        if isinstance(item, (dict, list)):
            pending.append((item, depth + 1))


def _validate_options_schema(schema: dict[str, Any]) -> dict[str, Any]:
    pending: list[tuple[Any, int]] = [(schema, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 6:
            raise ValueError("options_schema exceeds depth limit")
        if isinstance(node, dict):
            _validate_schema_dict(node, depth, pending)
        elif isinstance(node, list):
            _validate_schema_list(node, depth, pending)
    return schema


class FeatureState(_StrictModel):
    supported: bool
    available: bool
    reason_code: str | None = None


class ProviderCapabilities(_StrictModel):
    schema_version: Literal[1] = 1
    provider_instance_id: str = Field(pattern=HEX32_PATTERN)
    revision: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)
    provider_kind: Literal["pi", "codex", "claude"]
    provider_version: str = Field(min_length=1, max_length=128)
    structured_chat: FeatureState
    resume: FeatureState
    steer: FeatureState
    conversation_rollback: FeatureState
    conversation_fork: FeatureState
    context_import: FeatureState
    model_change: Literal["next_turn", "in_place", "restart", "unsupported"]
    effort_change: Literal["next_turn", "in_place", "restart", "unsupported"]
    options_schema: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_schema_bounds(self) -> ProviderCapabilities:
        _validate_options_schema(self.options_schema)
        return self


class ActionAvailability(_StrictModel):
    action: str = Field(min_length=1, max_length=64)
    supported: bool
    authorized: bool
    ready: bool
    reason_code: str | None = Field(default=None, max_length=64)
    remedy: Literal[
        "none",
        "select_node",
        "update_agent",
        "reconnect",
        "choose_supported_option",
        "resolve_current_run",
        "review_access",
    ] = "none"
    observed_revision: str = Field(min_length=1, max_length=128)


class NativeSelection(_StrictModel):
    model: str = Field(min_length=1, max_length=200)
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_strings(self) -> NativeSelection:
        if _CONTROL_RE.search(self.model):
            raise ValueError("Model name cannot contain control characters")
        return self


class StartSessionPayload(_StrictModel):
    action: Literal["start_session"]
    project_id: str = Field(min_length=1, max_length=128)
    cwd: str = Field(default=".", min_length=1, max_length=1024)
    selection: NativeSelection | None = None


class ResumeSessionPayload(_StrictModel):
    action: Literal["resume_session"]
    previous_session_id: str = Field(pattern=HEX32_PATTERN)
    new_session_id: str = Field(pattern=HEX32_PATTERN)
    expected_native_thread: str = Field(min_length=1, max_length=256)


class SendPayload(_StrictModel):
    action: Literal["send"]
    text: str = Field(max_length=65536)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)
    selection_mode: Literal["inherit_at_dispatch", "explicit"]
    selection: NativeSelection | None = None

    @model_validator(mode="after")
    def check_selection_mode(self) -> SendPayload:
        for att_id in self.attachment_ids:
            _validate_identifier(att_id, "attachment_id")
        if self.selection_mode == "explicit" and self.selection is None:
            raise ValueError("Explicit selection_mode requires selection")
        if self.selection_mode == "inherit_at_dispatch" and self.selection is not None:
            raise ValueError("inherit_at_dispatch forbids inline selection")
        if not self.text and not self.attachment_ids:
            raise ValueError("Send requires text or attachments")
        return self


class SteerPayload(_StrictModel):
    action: Literal["steer"]
    target_run_id: str = Field(min_length=1, max_length=128)
    provider_turn_id: str = Field(min_length=1, max_length=256)
    worker_epoch: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)
    text: str = Field(min_length=1, max_length=65536)


class InterruptPayload(_StrictModel):
    action: Literal["interrupt"]
    target_run_id: str = Field(min_length=1, max_length=128)
    provider_turn_id: str = Field(min_length=1, max_length=256)
    worker_epoch: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)


class ApprovalPayload(_StrictModel):
    action: Literal["answer_approval"]
    observed_request_id: str = Field(min_length=1, max_length=256)
    target_run_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)
    request_digest: str = Field(min_length=16, max_length=128)
    answer: JsonValue


class ChangeModelPayload(_StrictModel):
    action: Literal["change_model"]
    model: str = Field(min_length=1, max_length=200)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    expected_settings_revision: int = Field(strict=True, ge=0, le=MAX_SAFE_INT)


class ChangeOptionsPayload(_StrictModel):
    action: Literal["change_options"]
    options: dict[str, JsonValue]
    expected_settings_revision: int = Field(strict=True, ge=0, le=MAX_SAFE_INT)


class ForkSessionPayload(_StrictModel):
    action: Literal["fork_session"]
    source_session_id: str = Field(pattern=HEX32_PATTERN)
    source_run_id: str | None = Field(default=None, max_length=128)
    target_provider_instance_id: str = Field(pattern=HEX32_PATTERN)
    context_ref: str | None = Field(default=None, max_length=128)
    cwd: str = Field(default=".", min_length=1, max_length=1024)


class RollbackSessionPayload(_StrictModel):
    action: Literal["rollback_session"]
    checkpoint_ref: str = Field(min_length=1, max_length=128)
    expected_run_id: str | None = Field(default=None, max_length=128)
    expected_workspace_version: int = Field(strict=True, ge=0, le=MAX_SAFE_INT)
    rollback_mode: Literal["native_rewind", "replacement_session", "files_only"]


class StopSessionPayload(_StrictModel):
    action: Literal["stop_session"]
    worker_epoch: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)
    queue_policy: Literal["cancel_queued", "drain_only"] = "cancel_queued"


NativeAction = Annotated[
    StartSessionPayload
    | ResumeSessionPayload
    | SendPayload
    | SteerPayload
    | InterruptPayload
    | ApprovalPayload
    | ChangeModelPayload
    | ChangeOptionsPayload
    | ForkSessionPayload
    | RollbackSessionPayload
    | StopSessionPayload,
    Field(discriminator="action"),
]


class NativeCommand(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    session_id: str = Field(pattern=HEX32_PATTERN)
    provider_instance_id: str = Field(pattern=HEX32_PATTERN)
    payload: NativeAction


class NativeEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: Literal[1] = 1
    session_id: str = Field(pattern=HEX32_PATTERN)
    worker_epoch: int = Field(strict=True, ge=1, le=MAX_SAFE_INT)
    event_id: str | None = Field(default=None, min_length=1, max_length=256)
    event_seq: int | None = Field(default=None, strict=True, ge=1, le=MAX_SAFE_INT)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    receipt: str | None = Field(default=None, pattern=HEX32_PATTERN)
    kind: str = Field(min_length=1, max_length=96)
    body: dict[str, Any] = Field(default_factory=dict)


class ProviderCompletedBody(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    attempt_id: str = Field(min_length=1, max_length=128)
    execution_state: Literal["completed", "failed", "interrupted", "uncertain"]
    evidence_state: Literal[
        "capture_pending", "unavailable", "not_requested", "frozen", "partial"
    ]
    error_code: str | None = Field(default=None, max_length=64)


class EvidenceFrozenBody(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    attempt_id: str = Field(min_length=1, max_length=128)
    evidence_state: Literal["frozen", "partial", "unavailable"]
    after_ref: str | None = Field(default=None, max_length=64)
    before_ref: str | None = Field(default=None, max_length=64)
    capture_started_at: float | None = None
    capture_finished_at: float | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=64)


class ReviewReadyBody(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    attempt_id: str = Field(min_length=1, max_length=128)
    review_state: Literal["ready", "unavailable"]
    review_ref: str | None = Field(default=None, max_length=64)
    files_changed: int | None = Field(default=None, strict=True, ge=0, le=MAX_SAFE_INT)
    summary: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=64)


class CommandClaimedBody(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    attempt_id: str = Field(min_length=1, max_length=128)
    provider_instance_id: str | None = Field(default=None, max_length=64)
    workspace_version: int | None = Field(default=None, strict=True, ge=0, le=MAX_SAFE_INT)


class ApprovalRequestedBody(_StrictModel):
    request_id: str = Field(min_length=1, max_length=256)
    request_digest: str | None = Field(default=None, max_length=128)
    method: str = Field(min_length=1, max_length=128)
    receipt: str | None = Field(default=None, pattern=HEX32_PATTERN)


class ApprovalResolvedBody(_StrictModel):
    request_id: str = Field(min_length=1, max_length=256)
    decision: str = Field(min_length=1, max_length=64)
    receipt: str | None = Field(default=None, pattern=HEX32_PATTERN)


class SettingsAppliedBody(_StrictModel):
    receipt: str = Field(pattern=HEX32_PATTERN)
    status: Literal["applied", "partial", "rejected", "unknown"]
    settings_revision: int = Field(strict=True, ge=0, le=MAX_SAFE_INT)
    actual: dict[str, Any] = Field(default_factory=dict)


class WorkspaceClaimBody(_StrictModel):
    claim_id: str = Field(min_length=1, max_length=128)
    phase: Literal["baseline", "execute", "capture", "restore", "released", "unknown"]
    receipt: str = Field(pattern=HEX32_PATTERN)
    attempt_id: str = Field(min_length=1, max_length=128)


CRITICAL_BODY_MODELS: dict[str, type[BaseModel]] = {
    "provider_completed": ProviderCompletedBody,
    "evidence_frozen": EvidenceFrozenBody,
    "review_ready": ReviewReadyBody,
    "command.claimed": CommandClaimedBody,
    "approval.requested": ApprovalRequestedBody,
    "approval.resolved": ApprovalResolvedBody,
    "settings.applied": SettingsAppliedBody,
    "workspace.claimed": WorkspaceClaimBody,
    "workspace.released": WorkspaceClaimBody,
}


@dataclass(frozen=True)
class NormalizedEvent:
    """Normalized source record produced by envelope or legacy decoding."""

    stream_epoch: str
    source_start_cursor: int
    source_end_cursor: int
    kind: str
    category: Literal["display", "ordered_content", "critical_control"]
    event_id: str | None
    event_seq: int | None
    worker_epoch: int | None
    receipt: str | None
    run_id: str | None
    body: dict[str, Any]
    legacy_unknown: bool = False
    is_unknown_control: bool = False


def decode_raw_source_line(
    raw_line: bytes,
    *,
    stream_epoch: str,
    start_cursor: int,
    end_cursor: int,
) -> NormalizedEvent:
    """Decode one complete UTF-8 JSONL raw spool line into NormalizedEvent."""
    strict_safe_int(start_cursor)
    strict_safe_int(end_cursor)
    if end_cursor < start_cursor:
        raise ValueError("Source cursor moved backwards")
    parsed = json.loads(raw_line.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Raw event line must be a JSON object")
    if "schema_version" in parsed and "kind" in parsed:
        env = NativeEventEnvelope.model_validate(parsed)
        category = EVENT_CATEGORIES.get(env.kind)
        if category is None:
            return NormalizedEvent(
                stream_epoch=stream_epoch,
                source_start_cursor=start_cursor,
                source_end_cursor=end_cursor,
                kind=env.kind,
                category="critical_control",
                event_id=env.event_id,
                event_seq=env.event_seq,
                worker_epoch=env.worker_epoch,
                receipt=env.receipt,
                run_id=env.run_id,
                body=dict(env.body),
                legacy_unknown=False,
                is_unknown_control=True,
            )
        model_cls = CRITICAL_BODY_MODELS.get(env.kind)
        body = (
            model_cls.model_validate(env.body).model_dump()
            if model_cls is not None
            else dict(env.body)
        )
        return NormalizedEvent(
            stream_epoch=stream_epoch,
            source_start_cursor=start_cursor,
            source_end_cursor=end_cursor,
            kind=env.kind,
            category=category,
            event_id=env.event_id,
            event_seq=env.event_seq,
            worker_epoch=env.worker_epoch,
            receipt=env.receipt,
            run_id=env.run_id,
            body=body,
            legacy_unknown=False,
            is_unknown_control=False,
        )
    legacy_kind = parsed.get("type")
    if not isinstance(legacy_kind, str) or not legacy_kind:
        raise ValueError("Legacy event missing type")
    category = EVENT_CATEGORIES.get(legacy_kind)
    is_unknown = category is None
    receipt = parsed.get("receipt") if isinstance(parsed.get("receipt"), str) else None
    event_id = parsed.get("event_id") if isinstance(parsed.get("event_id"), str) else None
    return NormalizedEvent(
        stream_epoch=stream_epoch,
        source_start_cursor=start_cursor,
        source_end_cursor=end_cursor,
        kind=legacy_kind,
        category=category or "critical_control",
        event_id=event_id,
        event_seq=None,
        worker_epoch=None,
        receipt=receipt,
        run_id=parsed.get("attempt_id") if isinstance(parsed.get("attempt_id"), str) else None,
        body=parsed,
        legacy_unknown=True,
        is_unknown_control=is_unknown,
    )
