"""Durable semantic projection, journal, keyset paging, and bounded views."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Mapping
import uuid

from shared.native_cli import identifier, public
from shared.native_contracts import (
    MAX_SAFE_INT,
    NormalizedEvent,
    canonical_jcs,
    decode_raw_source_line,
    semantic_batch_digest,
    strict_safe_int,
)
from shared.util import DevError

PROJECTION_SCHEMA_VERSION = 2
DEFAULT_WINDOW_LIMIT = 50
DEFAULT_BODY_INLINE_BYTES = 16384
DEFAULT_TOOL_OUTPUT_INLINE_BYTES = 4096
DEFAULT_SCAN_MAX_BYTES = 262144
DEFAULT_SCAN_MAX_LINES = 128
MAX_SINGLE_LINE_BYTES = 1048576
DEFAULT_HISTORY_ROW_BUDGET = 50
DEFAULT_HISTORY_BYTE_BUDGET = 131072
DEFAULT_BODY_CHUNK_BYTES = 65536
DEFAULT_OUTPUT_TAIL_BYTES = 32768
DEFAULT_JOURNAL_MAX_BATCHES = 500
DEFAULT_JOURNAL_MAX_BYTES = 8 * 1024 * 1024
BODY_CHUNK_STORE_BYTES = 65536

_TERMINAL_TOOL_STATES = frozenset(
    {
        "completed",
        "end",
        "succeeded",
        "ok",
        "error",
        "failed",
        "denied",
        "interrupted",
        "cancelled",
    }
)
_KNOWN_LEGACY_KINDS = frozenset(
    {
        "user",
        "delta",
        "text_delta",
        "message.delta",
        "message",
        "assistant",
        "reasoning",
        "reasoning.delta",
        "thinking",
        "tool",
        "tool.output.delta",
        "approval",
        "approval.requested",
        "approval.resolved",
        "settings",
        "settings_pending",
        "settings_result",
        "settings.applied",
        "commands",
        "command_result",
        "queue",
        "cancelled",
        "stats",
        "status",
        "error",
        "done",
        "review",
        "token_usage",
        "notice",
        "display_meta",
        "session",
    }
)
_CRITICAL_CONTROL_KINDS = frozenset(
    {
        "provider_completed",
        "evidence_frozen",
        "review_ready",
        "command.claimed",
        "workspace.claimed",
        "workspace.released",
        "turn.restored",
        "run.started",
        "run.completed",
    }
)


@dataclass(frozen=True)
class Watermark:
    """Authoritative projection watermark tying epoch, revision, and cursor."""

    epoch: str
    revision: int
    applied_cursor: int


@dataclass(frozen=True)
class ProjectionHead:
    """Loaded row from native_projection_heads."""

    session_id: str
    watermark: Watermark
    schema_version: int
    retained_from_revision: int
    summary: dict[str, Any]
    source_state: str
    blocked_cursor: int | None
    updated: float


@dataclass(frozen=True)
class ScanResult:
    """Complete JSONL lines scanned strictly on UTF-8/newline boundaries."""

    lines: list[NormalizedEvent]
    start_cursor: int
    end_cursor: int
    ingested_cursor: int
    error_code: str | None = None
    error_detail: dict[str, Any] | None = None
    has_more: bool = False


@dataclass
class StagedProjection:
    """Pure staged changes produced from one bounded read transaction."""

    session_id: str
    base: Watermark
    end_cursor: int
    items: list[dict[str, Any]] = field(default_factory=list)
    body_writes: list[dict[str, Any]] = field(default_factory=list)
    output_writes: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    summary_patch: dict[str, Any] = field(default_factory=dict)
    source_state: str = "ready"
    blocked_cursor: int | None = None
    has_more: bool = False


def _output_rows(db, sid: str, cursor: int, *, limit: int = -1, boundary: int = 2**63 - 1):
    return db.execute(
        """SELECT offset,data FROM output
        WHERE session=:sid AND offset >= COALESCE((
            SELECT MAX(offset) FROM output WHERE session=:sid AND offset<=:cursor
        ), 0) AND offset<:boundary AND offset+length(data)>:cursor
        ORDER BY offset LIMIT :limit""",
        {"sid": sid, "cursor": cursor, "boundary": boundary, "limit": limit},
    )


def ensure_projection_schema(db) -> None:
    """Create projection tables and indexes idempotently in native.sqlite3."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS native_projection_heads (
          session_id TEXT PRIMARY KEY,
          epoch TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          projection_revision INTEGER NOT NULL DEFAULT 0,
          applied_cursor INTEGER NOT NULL DEFAULT 0,
          retained_from_revision INTEGER NOT NULL DEFAULT 0,
          summary_json TEXT NOT NULL,
          source_state TEXT NOT NULL DEFAULT 'building',
          blocked_cursor INTEGER,
          updated REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS native_projection_items (
          session_id TEXT NOT NULL,
          epoch TEXT NOT NULL,
          item_key TEXT NOT NULL,
          first_cursor INTEGER NOT NULL,
          item_revision INTEGER NOT NULL,
          kind TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 0,
          pending INTEGER NOT NULL DEFAULT 0,
          summary_json TEXT NOT NULL,
          body_ref TEXT,
          PRIMARY KEY(session_id, epoch, item_key)
        );
        CREATE INDEX IF NOT EXISTS native_projection_items_order
          ON native_projection_items(session_id, epoch, first_cursor, item_key);
        CREATE INDEX IF NOT EXISTS native_projection_items_active
          ON native_projection_items(session_id, epoch, active, pending, first_cursor);

        CREATE TABLE IF NOT EXISTS native_projection_batches (
          session_id TEXT NOT NULL,
          epoch TEXT NOT NULL,
          from_revision INTEGER NOT NULL,
          to_revision INTEGER NOT NULL,
          from_cursor INTEGER NOT NULL,
          to_cursor INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          payload_bytes INTEGER NOT NULL,
          created REAL NOT NULL,
          PRIMARY KEY(session_id, epoch, to_revision)
        );

        CREATE TABLE IF NOT EXISTS native_projection_bodies (
          session_id TEXT NOT NULL,
          epoch TEXT NOT NULL,
          item_key TEXT NOT NULL,
          content_generation INTEGER NOT NULL,
          start_byte INTEGER NOT NULL,
          end_byte INTEGER NOT NULL,
          total_bytes INTEGER NOT NULL,
          content_revision INTEGER NOT NULL,
          data BLOB NOT NULL,
          PRIMARY KEY(session_id, epoch, item_key, content_generation, start_byte)
        );

        CREATE TABLE IF NOT EXISTS native_projection_outputs (
          session_id TEXT NOT NULL,
          epoch TEXT NOT NULL,
          item_key TEXT NOT NULL,
          output_epoch TEXT NOT NULL,
          start_offset INTEGER NOT NULL,
          end_offset INTEGER NOT NULL,
          data BLOB NOT NULL,
          running INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(session_id, epoch, item_key, output_epoch, start_offset)
        );

        CREATE TABLE IF NOT EXISTS native_projection_tombstones (
          session_id TEXT PRIMARY KEY,
          epoch TEXT NOT NULL,
          deleted INTEGER NOT NULL DEFAULT 0,
          last_transition_id TEXT NOT NULL,
          transition_ids_json TEXT NOT NULL DEFAULT '[]',
          source_epochs_json TEXT NOT NULL DEFAULT '[]',
          updated REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS native_projection_secrets (
          name TEXT PRIMARY KEY,
          secret_hex TEXT NOT NULL,
          created REAL NOT NULL
        );
        """
    )


def _ensure_signing_key(db, secret: bytes | None = None) -> bytes:
    if secret is not None:
        return secret
    row = db.execute(
        "SELECT secret_hex FROM native_projection_secrets WHERE name='token_hmac'"
    ).fetchone()
    if row:
        return bytes.fromhex(row["secret_hex"])
    key = secrets.token_bytes(32)
    db.execute(
        "INSERT OR IGNORE INTO native_projection_secrets(name,secret_hex,created) VALUES(?,?,?)",
        ("token_hmac", key.hex(), time.time()),
    )
    row = db.execute(
        "SELECT secret_hex FROM native_projection_secrets WHERE name='token_hmac'"
    ).fetchone()
    return bytes.fromhex(row["secret_hex"])


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise DevError("CLI_INVALID", "无效令牌", 400)
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except ValueError as exc:
        raise DevError("CLI_INVALID", "令牌编码无效", 400) from exc


def sign_opaque_token(payload: Mapping[str, Any], secret: bytes) -> str:
    """Create an HMAC-SHA256 signed opaque token."""
    raw = canonical_jcs(dict(payload)).encode("utf-8")
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64u_encode(raw)}.{_b64u_encode(sig)}"


def verify_opaque_token(token: str, secret: bytes) -> dict[str, Any]:
    """Verify and decode an HMAC-SHA256 signed opaque token."""
    if not isinstance(token, str) or "." not in token:
        raise DevError("CLI_INVALID", "令牌格式无效", 400)
    body_b64, sig_b64 = token.split(".", 1)
    raw = _b64u_decode(body_b64)
    sig = _b64u_decode(sig_b64)
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise DevError("CLI_INVALID", "令牌签名无效", 400)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DevError("CLI_INVALID", "令牌内容无效", 400) from exc
    if not isinstance(parsed, dict):
        raise DevError("CLI_INVALID", "令牌载荷无效", 400)
    return parsed


def make_resume_token(
    db,
    *,
    session_id: str,
    scope_key: str,
    epoch: str,
    revision: int,
    applied_cursor: int,
    secret: bytes | None = None,
) -> str:
    """Issue a signed resume token bound to scope, session, epoch, and watermarks."""
    key = _ensure_signing_key(db, secret)
    return sign_opaque_token(
        {
            "v": 2,
            "kind": "resume",
            "sid": identifier(session_id),
            "scope": scope_key,
            "epoch": epoch,
            "rev": strict_safe_int(revision),
            "cur": strict_safe_int(applied_cursor),
        },
        key,
    )


def parse_resume_token(
    db,
    token: str,
    *,
    session_id: str,
    scope_key: str,
    secret: bytes | None = None,
) -> Watermark:
    """Validate a signed resume token against session_id and scope_key."""
    key = _ensure_signing_key(db, secret)
    data = verify_opaque_token(token, key)
    if (
        data.get("v") != 2
        or data.get("kind") != "resume"
        or data.get("sid") != identifier(session_id)
        or data.get("scope") != scope_key
    ):
        raise DevError("CLI_INVALID", "恢复令牌作用域不匹配", 400)
    epoch = data.get("epoch")
    if not isinstance(epoch, str) or not epoch:
        raise DevError("CLI_INVALID", "恢复令牌世代无效", 400)
    try:
        rev = strict_safe_int(data.get("rev"))
        cur = strict_safe_int(data.get("cur"))
    except ValueError as exc:
        raise DevError("CLI_INVALID", "恢复令牌水位无效", 400) from exc
    return Watermark(epoch=epoch, revision=rev, applied_cursor=cur)


def make_history_token(
    db,
    *,
    session_id: str,
    scope_key: str,
    epoch: str,
    before_cursor: int,
    before_key: str,
    secret: bytes | None = None,
) -> str:
    """Issue a signed keyset paging token."""
    key = _ensure_signing_key(db, secret)
    return sign_opaque_token(
        {
            "v": 2,
            "kind": "history",
            "sid": identifier(session_id),
            "scope": scope_key,
            "epoch": epoch,
            "cur": strict_safe_int(before_cursor),
            "key": before_key,
        },
        key,
    )


def parse_history_token(
    db,
    token: str,
    *,
    session_id: str,
    scope_key: str,
    secret: bytes | None = None,
) -> tuple[str, int, str]:
    """Verify and unpack a signed keyset paging token."""
    key = _ensure_signing_key(db, secret)
    data = verify_opaque_token(token, key)
    if (
        data.get("v") != 2
        or data.get("kind") != "history"
        or data.get("sid") != identifier(session_id)
        or data.get("scope") != scope_key
    ):
        raise DevError("CLI_INVALID", "分页令牌作用域不匹配", 400)
    epoch = data.get("epoch")
    item_key = data.get("key")
    if not isinstance(epoch, str) or not epoch or not isinstance(item_key, str) or not item_key:
        raise DevError("CLI_INVALID", "分页令牌字段无效", 400)
    try:
        cur = strict_safe_int(data.get("cur"))
    except ValueError as exc:
        raise DevError("CLI_INVALID", "分页令牌游标无效", 400) from exc
    return epoch, cur, item_key


def read_head(db, session_id: str) -> ProjectionHead | None:
    """Read the projection head for a session."""
    row = db.execute(
        "SELECT * FROM native_projection_heads WHERE session_id=?",
        (identifier(session_id),),
    ).fetchone()
    if not row:
        return None
    summary = json.loads(row["summary_json"])
    return ProjectionHead(
        session_id=row["session_id"],
        watermark=Watermark(
            epoch=row["epoch"],
            revision=strict_safe_int(row["projection_revision"]),
            applied_cursor=strict_safe_int(row["applied_cursor"]),
        ),
        schema_version=strict_safe_int(row["schema_version"], minimum=1),
        retained_from_revision=strict_safe_int(row["retained_from_revision"]),
        summary=summary if isinstance(summary, dict) else {},
        source_state=row["source_state"],
        blocked_cursor=row["blocked_cursor"],
        updated=float(row["updated"]),
    )


def read_tombstone(db, session_id: str) -> dict[str, Any] | None:
    """Read persisted tombstone and retired epochs for a session."""
    row = db.execute(
        "SELECT * FROM native_projection_tombstones WHERE session_id=?",
        (identifier(session_id),),
    ).fetchone()
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "epoch": row["epoch"],
        "deleted": bool(row["deleted"]),
        "last_transition_id": row["last_transition_id"],
        "transition_ids": json.loads(row["transition_ids_json"]),
        "source_epochs": json.loads(row["source_epochs_json"]),
        "updated": float(row["updated"]),
    }


def _active_epoch_for_session(db, session_id: str, head: ProjectionHead | None) -> str:
    if head is not None:
        return head.watermark.epoch
    tomb = read_tombstone(db, session_id)
    if tomb is not None:
        return tomb["epoch"]
    return f"ep_{uuid.uuid4().hex[:16]}"


def assert_complete_line_boundary(db, session_id: str, cursor: int) -> None:
    """Ensure cursor is 0 or lands immediately after a newline byte in output."""
    strict_safe_int(cursor)
    if cursor == 0:
        return
    prev = db.execute(
        "SELECT offset,data FROM output WHERE session=? AND offset<? AND offset+length(data)>=? "
        "ORDER BY offset DESC LIMIT 1",
        (session_id, cursor, cursor),
    ).fetchone()
    if not prev or prev["data"][cursor - prev["offset"] - 1 : cursor - prev["offset"]] != b"\n":
        raise DevError("CLI_OFFSET", "事件游标不是已完成的事件边界", 409)


def _ingested_cursor(db, session_id: str) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(offset+length(data)),0) FROM output WHERE session=?",
        (session_id,),
    ).fetchone()
    return strict_safe_int(int(row[0]))


def _collect_raw_buffer(
    db, session_id: str, start_cursor: int, *, max_bytes: int
) -> tuple[bytearray, int, str | None, dict[str, Any] | None]:
    buffer = bytearray()
    cursor = start_cursor
    hard_cap = max(max_bytes, MAX_SINGLE_LINE_BYTES)
    for row in _output_rows(db, session_id, start_cursor):
        row_offset = strict_safe_int(int(row["offset"]))
        if row_offset > cursor:
            return buffer, cursor, "SOURCE_GAP", {"expected": cursor, "actual": row_offset}
        slice_start = max(0, cursor - row_offset)
        piece = row["data"][slice_start:]
        if not piece:
            continue
        buffer.extend(piece)
        cursor += len(piece)
        if b"\n" in buffer and len(buffer) >= max_bytes:
            break
        if len(buffer) > hard_cap:
            break
    return buffer, cursor, None, None


def scan_complete_jsonl_lines(
    db,
    session_id: str,
    start_cursor: int,
    *,
    max_bytes: int = DEFAULT_SCAN_MAX_BYTES,
    max_lines: int = DEFAULT_SCAN_MAX_LINES,
    stream_epoch: str = "ep_scan",
) -> ScanResult:
    """Scan complete UTF-8 JSONL lines without crossing incomplete lines or UTF-8."""
    sid = identifier(session_id)
    strict_safe_int(start_cursor)
    strict_safe_int(max_bytes, minimum=0)
    strict_safe_int(max_lines, minimum=1)
    ingested = _ingested_cursor(db, sid)
    if start_cursor > ingested:
        return ScanResult(
            [],
            start_cursor,
            start_cursor,
            ingested,
            "SOURCE_GAP",
            {"expected": start_cursor, "ingested": ingested},
        )
    buffer, scanned_to, gap_err, gap_detail = _collect_raw_buffer(
        db, sid, start_cursor, max_bytes=max_bytes
    )
    if gap_err and not buffer:
        return ScanResult([], start_cursor, start_cursor, ingested, gap_err, gap_detail)
    return _decode_scanned_buffer(
        buffer,
        start_cursor=start_cursor,
        ingested=ingested,
        scanned_to=scanned_to,
        max_bytes=max_bytes,
        max_lines=max_lines,
        stream_epoch=stream_epoch,
        gap_err=gap_err,
        gap_detail=gap_detail,
    )


def _decode_scanned_buffer(
    buffer: bytearray,
    *,
    start_cursor: int,
    ingested: int,
    scanned_to: int,
    max_bytes: int,
    max_lines: int,
    stream_epoch: str,
    gap_err: str | None,
    gap_detail: dict[str, Any] | None,
) -> ScanResult:
    lines: list[NormalizedEvent] = []
    pos = 0
    while True:
        nl = buffer.find(b"\n", pos)
        if nl < 0:
            break
        raw_line = bytes(buffer[pos:nl])
        line_end = start_cursor + nl + 1
        if lines and ((line_end - start_cursor) > max_bytes or len(lines) >= max_lines):
            return ScanResult(lines, start_cursor, start_cursor + pos, ingested, has_more=True)
        try:
            event = decode_raw_source_line(
                raw_line,
                stream_epoch=stream_epoch,
                start_cursor=start_cursor + pos,
                end_cursor=line_end,
            )
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            return ScanResult(
                lines,
                start_cursor,
                start_cursor + pos,
                ingested,
                "SOURCE_CORRUPT",
                {"cursor": start_cursor + pos, "end_cursor": line_end, "reason": str(exc)},
            )
        lines.append(event)
        pos = nl + 1
        if len(lines) >= max_lines or (pos >= max_bytes and max_bytes > 0):
            more = pos < len(buffer) or scanned_to < ingested
            return ScanResult(lines, start_cursor, start_cursor + pos, ingested, has_more=more)
    if not lines and len(buffer) > MAX_SINGLE_LINE_BYTES:
        return ScanResult(
            [],
            start_cursor,
            start_cursor,
            ingested,
            "SOURCE_CORRUPT",
            {"cursor": start_cursor, "reason": "line_too_large"},
        )
    if gap_err:
        return ScanResult(lines, start_cursor, start_cursor + pos, ingested, gap_err, gap_detail)
    return ScanResult(
        lines, start_cursor, start_cursor + pos, ingested, has_more=scanned_to < ingested
    )


def _normalize_kind(kind: str) -> str:
    if kind in {"delta", "text_delta", "message.delta", "message", "assistant"}:
        return "assistant"
    if kind in {"reasoning", "reasoning.delta", "thinking"}:
        return "reasoning"
    if kind in {"tool", "tool.output.delta"}:
        return "tool"
    if kind in {"approval", "approval.requested", "approval.resolved"}:
        return "approval"
    if kind in {"review", "review_ready"}:
        return "review"
    return kind


def _is_unknown_control(event: NormalizedEvent) -> bool:
    if not event.is_unknown_control:
        return False
    if event.legacy_unknown and event.kind in _KNOWN_LEGACY_KINDS:
        return False
    return True


def _resolve_claude_item_id(
    state: dict[str, Any], event: NormalizedEvent, body: Mapping[str, Any]
) -> str | None:
    item_id = body.get("item_id")
    if (
        state["provider"] != "claude"
        or event.kind != "message"
        or "block_index" in body
        or not isinstance(item_id, str)
        or not item_id.endswith(":0")
    ):
        return item_id if isinstance(item_id, str) else None
    prefix = item_id[:-1]
    text = body.get("text")
    matches = [
        it
        for it in state["items"].values()
        if it["kind"] == "assistant"
        and it["summary"].get("last_type") in {"delta", "message.delta"}
        and it["summary"].get("receipt") == event.receipt
        and str(it["summary"].get("item_id", "")).startswith(prefix)
        and it.get("full_text", "") == text
    ]
    if len(matches) == 1:
        return str(matches[0]["summary"]["item_id"])
    return item_id


def _compute_item_key(
    state: dict[str, Any], event: NormalizedEvent, body: Mapping[str, Any]
) -> tuple[str, str]:
    kind = _normalize_kind(event.kind)
    item_id = _resolve_claude_item_id(state, event, body)
    ident = item_id or body.get("request_id") or body.get("tool_id") or body.get("review_id") or ""
    receipt = event.receipt or (
        body.get("receipt") if isinstance(body.get("receipt"), str) else None
    )
    if not receipt and not ident:
        return kind, f"session:{kind}:@{event.source_start_cursor}"
    return kind, f"{receipt or 'session'}:{kind}:{ident}"


def _utf8_safe_prefix(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    end = max_bytes
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end].decode("utf-8", errors="replace")


def _build_item_body_payload(
    item: dict[str, Any], *, body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES
) -> dict[str, Any]:
    raw = item["full_text"].encode("utf-8")
    if len(raw) <= body_inline_bytes:
        return {"kind": "inline", "text": item["full_text"]}
    return {
        "kind": "ref",
        "body_ref": item["body_ref"],
        "content_generation": item["content_generation"],
        "content_revision": item["item_revision"],
        "byte_length": len(raw),
        "preview": _utf8_safe_prefix(raw, 1024),
        "partial": True,
    }


def _public_item(
    item: dict[str, Any], *, body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES
) -> dict[str, Any]:
    summary = item["summary"]
    out = {
        "key": item["item_key"],
        "first_cursor": item["first_cursor"],
        "item_revision": item["item_revision"],
        "kind": item["kind"],
        "status": summary.get("status", ""),
        "active": bool(item["active"]),
        "pending": bool(item["pending"]),
        "body": _build_item_body_payload(item, body_inline_bytes=body_inline_bytes),
    }
    for field_name in (
        "receipt",
        "parent_receipt",
        "item_id",
        "tool_id",
        "request_id",
        "name",
        "method",
        "is_error",
        "exit_code",
        "output_ref",
        "resolved",
        "resolution",
        "options",
        "details",
        "attachments",
        "steering",
    ):
        if field_name in summary and summary[field_name] is not None:
            out[field_name] = summary[field_name]
    return out


def _get_or_create_item(
    state: dict[str, Any], key: str, kind: str, event: NormalizedEvent
) -> tuple[dict[str, Any], bool]:
    existing = state["items"].get(key)
    if existing is not None:
        return existing, False
    item = {
        "item_key": key,
        "first_cursor": event.source_start_cursor,
        "item_revision": state["next_revision"],
        "kind": kind,
        "active": 0,
        "pending": 0,
        "body_ref": None,
        "content_generation": 1,
        "full_text": "",
        "output_bytes": b"",
        "output_epoch": None,
        "summary": {"status": "", "receipt": event.receipt},
    }
    state["items"][key] = item
    return item, True


def _extract_exit_code(body: Mapping[str, Any], text: str) -> int | None:
    if type(body.get("exit_code")) is int:
        return int(body["exit_code"])
    if type(body.get("exitCode")) is int:
        return int(body["exitCode"])
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                if type(parsed.get("exitCode")) is int:
                    return int(parsed["exitCode"])
                if type(parsed.get("exit_code")) is int:
                    return int(parsed["exit_code"])
        except ValueError:
            pass
    return None


def _handle_settings_meta(
    state: dict[str, Any], event: NormalizedEvent, body: dict[str, Any]
) -> None:
    summary = state["summary"]
    patch = state["summary_patch"]
    if event.kind == "settings":
        merged = {**summary.get("settings", {}), **{k: v for k, v in body.items() if k != "type"}}
        summary["settings"] = merged
        patch["settings"] = merged
        if "commands" in body:
            summary["commands"] = body["commands"]
            patch["commands"] = body["commands"]
        return
    if event.kind == "settings_pending":
        pending = None if body.get("cleared") else dict(body)
        summary["pending_settings"] = pending
        patch["pending_settings"] = pending
        return
    outcome = body.get("outcome") or body.get("status") or "unknown"
    res: dict[str, Any] = {"receipt": event.receipt or body.get("receipt"), "outcome": outcome}
    if isinstance(body.get("actual"), dict):
        summary["settings"] = {**summary.get("settings", {}), **body["actual"]}
        patch["settings"] = summary["settings"]
        res["actual"] = body["actual"]
    if type(body.get("settings_revision")) is int:
        res["settings_revision"] = body["settings_revision"]
    summary["settings_result"] = res
    patch["settings_result"] = res


def _handle_meta_event(state: dict[str, Any], event: NormalizedEvent, body: dict[str, Any]) -> None:
    if event.kind in {"settings", "settings_pending", "settings_result", "settings.applied"}:
        _handle_settings_meta(state, event, body)
        return
    summary = state["summary"]
    patch = state["summary_patch"]
    if event.kind == "commands":
        summary["commands"] = body.get("commands", [])
        patch["commands"] = summary["commands"]
    elif event.kind in {"command_result", "command.claimed"}:
        res = {k: v for k, v in body.items() if k != "type"}
        summary["command_result"] = res
        patch["command_result"] = res
    elif event.kind in {"stats", "token_usage"}:
        stats = body.get("stats") if isinstance(body.get("stats"), dict) else dict(body)
        summary["stats"] = stats
        patch["stats"] = stats
    elif event.kind in {"queue", "cancelled"}:
        summary["queue"] = body.get("queue", body)
        patch["queue"] = summary["queue"]
    elif event.kind == "status":
        text = body.get("text") or body.get("status") or ""
        summary["status_text"] = text
        patch["status_text"] = text


def _close_turn_items(state: dict[str, Any], receipt: str | None, resolution: str) -> None:
    for item in state["items"].values():
        if receipt and item["summary"].get("receipt") != receipt:
            continue
        changed = False
        if item["kind"] == "approval" and not item["summary"].get("resolved"):
            item["summary"]["resolved"] = True
            item["summary"]["resolution"] = resolution
            item["active"] = 0
            item["pending"] = 0
            changed = True
        elif item["active"]:
            item["active"] = 0
            changed = True
        if changed:
            item["item_revision"] = state["next_revision"]
            state["touched"][item["item_key"]] = ("upsert", None, item.get("item_revision"))


def _handle_control_event(
    state: dict[str, Any], event: NormalizedEvent, body: dict[str, Any]
) -> None:
    summary = state["summary"]
    patch = state["summary_patch"]
    if event.kind in {"done", "run.completed"}:
        receipt = event.receipt or body.get("receipt")
        status = str(body.get("status") or "completed")
        run = summary.setdefault("run", {})
        if not receipt or run.get("receipt") == receipt:
            run["receipt"] = None
            run["status"] = status
            run["phase"] = ""
            patch["run"] = dict(run)
        _close_turn_items(state, receipt, "closed")
    elif event.kind in _CRITICAL_CONTROL_KINDS:
        ctrl = summary.setdefault("control", {})
        ctrl[event.kind] = {
            "receipt": event.receipt,
            "run_id": event.run_id,
            "worker_epoch": event.worker_epoch,
            "body": dict(body),
            "cursor": event.source_end_cursor,
        }
        patch["control"] = {event.kind: ctrl[event.kind]}
        if event.kind == "review_ready":
            _handle_content_event(state, event, body)


def _apply_text_change(
    state: dict[str, Any],
    item: dict[str, Any],
    *,
    is_new: bool,
    mode: str,
    text: str,
    body_inline_bytes: int,
) -> None:
    base_rev = None if is_new else item["item_revision"]
    if mode == "append":
        item["full_text"] += text
        op = "upsert" if is_new else "append_text"
    else:
        if not is_new and item["summary"].get("body_externalized"):
            item["content_generation"] += 1
        item["full_text"] = text
        op = "upsert" if is_new else "replace_body"
    item["item_revision"] = state["next_revision"]
    raw_len = len(item["full_text"].encode("utf-8"))
    if raw_len > body_inline_bytes:
        item["summary"]["body_externalized"] = True
        item["body_ref"] = f"body:{state['epoch']}:{item['item_key']}:{item['content_generation']}"
        state["body_dirty"].add(item["item_key"])
        op = "upsert" if is_new else "replace_body"
    else:
        item["summary"]["body_externalized"] = False
        item["body_ref"] = None
    prev_entry = state["touched"].get(item["item_key"])
    if prev_entry and prev_entry[0] == "append_text" and op == "append_text":
        state["touched"][item["item_key"]] = ("append_text", prev_entry[1], prev_entry[2] + text)
    elif op == "append_text":
        state["touched"][item["item_key"]] = ("append_text", base_rev, text)
    else:
        state["touched"][item["item_key"]] = (op, base_rev, None)


def _update_phase(
    state: dict[str, Any], kind: str, event: NormalizedEvent, body: Mapping[str, Any]
) -> None:
    run = state["summary"].setdefault("run", {})
    phase_map = {"tool": "tool", "reasoning": "reasoning", "assistant": "reply", "user": "working"}
    if kind in phase_map:
        run["phase"] = phase_map[kind]
    if kind == "user" and not body.get("parent_receipt") and not body.get("steering"):
        run["receipt"] = event.receipt
        run["status"] = "running"
    state["summary_patch"]["run"] = dict(run)


def _apply_tool_output(
    state: dict[str, Any],
    item: dict[str, Any],
    event: NormalizedEvent,
    body: Mapping[str, Any],
    *,
    tool_inline_bytes: int,
) -> tuple[str, str]:
    status = str(body.get("status") or ("delta" if event.kind == "tool.output.delta" else ""))
    chunk_text = body.get("text") if isinstance(body.get("text"), str) else body.get("delta")
    chunk_str = chunk_text if isinstance(chunk_text, str) else ""
    running = 0 if status in _TERMINAL_TOOL_STATES else 1
    item["active"] = running
    is_delta = status == "delta" or event.kind == "tool.output.delta"
    if is_delta:
        item["output_bytes"] += chunk_str.encode("utf-8")
    elif chunk_str and not item["output_bytes"]:
        item["output_bytes"] = chunk_str.encode("utf-8")
    code = _extract_exit_code(body, chunk_str)
    if code is not None:
        item["summary"]["exit_code"] = code
    if len(item["output_bytes"]) > tool_inline_bytes or item["output_epoch"] is not None:
        if item["output_epoch"] is None:
            item["output_epoch"] = f"out_{uuid.uuid4().hex[:12]}"
        item["summary"]["output_ref"] = {
            "item_key": item["item_key"],
            "output_epoch": item["output_epoch"],
            "byte_length": len(item["output_bytes"]),
        }
        state["output_dirty"].add(item["item_key"])
        return ("replace" if not is_delta else "append"), ""
    return ("append" if is_delta else "replace"), chunk_str


def _apply_approval_fields(
    item: dict[str, Any], event: NormalizedEvent, body: Mapping[str, Any]
) -> None:
    resolved = bool(body.get("resolved")) or event.kind == "approval.resolved"
    item["summary"]["resolved"] = resolved
    if resolved:
        item["summary"]["resolution"] = body.get("resolution") or body.get("decision") or "resolved"
        item["active"] = 0
        item["pending"] = 0
    else:
        item["active"] = 1
        item["pending"] = 1
    for field_name in ("request_id", "method", "options", "details"):
        if field_name in body:
            item["summary"][field_name] = body[field_name]


def _handle_content_event(
    state: dict[str, Any], event: NormalizedEvent, body: dict[str, Any]
) -> None:
    kind, key = _compute_item_key(state, event, body)
    item, is_new = _get_or_create_item(state, key, kind, event)
    item["summary"]["last_type"] = event.kind
    for field_name in (
        "receipt",
        "parent_receipt",
        "item_id",
        "tool_id",
        "name",
        "attachments",
        "steering",
    ):
        if field_name in body and body[field_name] is not None:
            item["summary"][field_name] = body[field_name]
    if event.receipt and not item["summary"].get("receipt"):
        item["summary"]["receipt"] = event.receipt
    if "status" in body and isinstance(body["status"], str):
        item["summary"]["status"] = body["status"]
    item["summary"]["is_error"] = bool(
        body.get("isError") or body.get("is_error") or kind == "error"
    )
    _update_phase(state, kind, event, body)
    mode, text = _resolve_content_mode_and_text(state, item, kind, event, body)
    _apply_text_change(
        state,
        item,
        is_new=is_new,
        mode=mode,
        text=text,
        body_inline_bytes=state["body_inline_bytes"],
    )
    if kind == "review":
        state["summary"]["review"] = dict(body)
        state["summary_patch"]["review"] = dict(body)


def _resolve_content_mode_and_text(
    state: dict[str, Any],
    item: dict[str, Any],
    kind: str,
    event: NormalizedEvent,
    body: Mapping[str, Any],
) -> tuple[str, str]:
    if kind == "approval":
        _apply_approval_fields(item, event, body)
        fallback = (
            body.get("text") or body.get("message") or body.get("method") or item["full_text"]
        )
        return "replace", str(fallback or "")
    if kind == "tool":
        return _apply_tool_output(
            state, item, event, body, tool_inline_bytes=state["tool_inline_bytes"]
        )
    if event.kind in {
        "delta",
        "text_delta",
        "message.delta",
        "reasoning",
        "reasoning.delta",
        "thinking",
    }:
        delta = body.get("text") if body.get("text") is not None else body.get("delta", "")
        if kind == "reasoning":
            item["active"] = 0 if body.get("status") == "end" else 1
        else:
            item["active"] = 1
        return "append", str(delta or "")
    if kind == "assistant":
        item["active"] = 0
    if body.get("text") is not None:
        return "replace", str(body["text"])
    fallback = body.get("message") or body.get("title") or body.get("name") or item["full_text"]
    return "replace", str(fallback or "")


_META_KINDS = frozenset(
    {
        "settings",
        "settings_pending",
        "settings_result",
        "settings.applied",
        "commands",
        "command_result",
        "command.claimed",
        "stats",
        "token_usage",
        "queue",
        "cancelled",
        "status",
        "notice",
        "display_meta",
        "session",
    }
)


def _reduce_one_event(state: dict[str, Any], event: NormalizedEvent) -> None:
    body = event.body
    if event.kind in _META_KINDS:
        _handle_meta_event(state, event, body)
    elif event.kind in {"done", "run.completed"} or event.kind in _CRITICAL_CONTROL_KINDS:
        _handle_control_event(state, event, body)
    else:
        _handle_content_event(state, event, body)


def _hydrate_existing_item(row, body_bytes: bytes, output_bytes: bytes) -> dict[str, Any]:
    summary = json.loads(row["summary_json"])
    return {
        "item_key": row["item_key"],
        "first_cursor": strict_safe_int(row["first_cursor"]),
        "item_revision": strict_safe_int(row["item_revision"]),
        "kind": row["kind"],
        "active": int(row["active"]),
        "pending": int(row["pending"]),
        "body_ref": row["body_ref"],
        "content_generation": strict_safe_int(summary.get("content_generation", 1), minimum=1),
        "full_text": body_bytes.decode("utf-8", errors="replace")
        if row["body_ref"]
        else str(summary.get("inline_text", "")),
        "output_bytes": output_bytes,
        "output_epoch": (summary.get("output_ref") or {}).get("output_epoch"),
        "summary": summary,
    }


def _load_item_body_bytes(db, sid: str, epoch: str, item_key: str, generation: int) -> bytes:
    parts = [
        bytes(r["data"])
        for r in db.execute(
            "SELECT data FROM native_projection_bodies WHERE session_id=? AND epoch=? AND item_key=? AND content_generation=? ORDER BY start_byte",
            (sid, epoch, item_key, generation),
        )
    ]
    return b"".join(parts)


def _load_item_output_bytes(
    db, sid: str, epoch: str, item_key: str, output_epoch: str | None
) -> bytes:
    if not output_epoch:
        return b""
    parts = [
        bytes(r["data"])
        for r in db.execute(
            "SELECT data FROM native_projection_outputs WHERE session_id=? AND epoch=? AND item_key=? AND output_epoch=? ORDER BY start_offset",
            (sid, epoch, item_key, output_epoch),
        )
    ]
    return b"".join(parts)


def _load_relevant_items(db, sid: str, epoch: str) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? ORDER BY first_cursor, item_key",
        (sid, epoch),
    ).fetchall()
    for row in rows:
        summary = json.loads(row["summary_json"])
        gen = strict_safe_int(summary.get("content_generation", 1), minimum=1)
        body_bytes = (
            _load_item_body_bytes(db, sid, epoch, row["item_key"], gen) if row["body_ref"] else b""
        )
        out_ep = (summary.get("output_ref") or {}).get("output_epoch")
        out_bytes = (
            _load_item_output_bytes(db, sid, epoch, row["item_key"], out_ep) if out_ep else b""
        )
        items[row["item_key"]] = _hydrate_existing_item(row, body_bytes, out_bytes)
    return items


def _build_body_write_rows(item: dict[str, Any], revision: int) -> list[dict[str, Any]]:
    raw = item["full_text"].encode("utf-8")
    total = len(raw)
    rows = []
    for start in range(0, max(1, total), BODY_CHUNK_STORE_BYTES):
        chunk = raw[start : start + BODY_CHUNK_STORE_BYTES]
        rows.append(
            {
                "item_key": item["item_key"],
                "content_generation": item["content_generation"],
                "start_byte": start,
                "end_byte": start + len(chunk),
                "total_bytes": total,
                "content_revision": revision,
                "data": chunk,
            }
        )
    return rows


def _build_output_write_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item["output_bytes"]
    out_ep = item["output_epoch"]
    if not out_ep:
        return []
    rows = []
    for start in range(0, max(1, len(raw)), BODY_CHUNK_STORE_BYTES):
        chunk = raw[start : start + BODY_CHUNK_STORE_BYTES]
        rows.append(
            {
                "item_key": item["item_key"],
                "output_epoch": out_ep,
                "start_offset": start,
                "end_offset": start + len(chunk),
                "data": chunk,
                "running": int(item["active"]),
            }
        )
    return rows


def _build_staged_changes(
    state: dict[str, Any], *, body_inline_bytes: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    db_items: list[dict[str, Any]] = []
    body_writes: list[dict[str, Any]] = []
    output_writes: list[dict[str, Any]] = []
    for key, entry in state["touched"].items():
        op, base_rev, text_arg = entry
        item = state["items"][key]
        pub = _public_item(item, body_inline_bytes=body_inline_bytes)
        if op == "append_text":
            changes.append(
                {
                    "op": "append_text",
                    "key": key,
                    "base_item_revision": base_rev,
                    "item_revision": item["item_revision"],
                    "text": text_arg,
                }
            )
        elif op == "replace_body":
            changes.append(
                {
                    "op": "replace_body",
                    "key": key,
                    "base_item_revision": base_rev,
                    "item_revision": item["item_revision"],
                    "body": pub["body"],
                    "item": pub,
                }
            )
        else:
            changes.append(
                {"op": "upsert", "key": key, "item_revision": item["item_revision"], "item": pub}
            )
        stored_summary = dict(item["summary"])
        stored_summary["content_generation"] = item["content_generation"]
        stored_summary["inline_text"] = "" if item["body_ref"] else item["full_text"]
        db_items.append(
            {
                "item_key": key,
                "first_cursor": item["first_cursor"],
                "item_revision": item["item_revision"],
                "kind": item["kind"],
                "active": int(item["active"]),
                "pending": int(item["pending"]),
                "summary_json": json.dumps(stored_summary, ensure_ascii=False),
                "body_ref": item["body_ref"],
            }
        )
        if key in state["body_dirty"] and item["body_ref"]:
            body_writes.extend(_build_body_write_rows(item, item["item_revision"]))
        if key in state["output_dirty"] and item["output_epoch"]:
            output_writes.extend(_build_output_write_rows(item))
    return changes, db_items, body_writes, output_writes


def _session_summary_from_row(row) -> dict[str, Any]:
    if not row:
        return {}
    meta = public(row)
    return {
        "id": meta["id"],
        "title": meta["title"],
        "provider": meta["provider"],
        "process_status": meta["status"],
        "status": meta["status"],
        "error": meta["error"],
        "cwd": meta["cwd"],
        "mode": meta.get("mode", "terminal"),
    }


def prepare_projection_step(
    db,
    session_id: str,
    *,
    max_bytes: int = DEFAULT_SCAN_MAX_BYTES,
    max_lines: int = DEFAULT_SCAN_MAX_LINES,
    body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES,
    tool_inline_bytes: int = DEFAULT_TOOL_OUTPUT_INLINE_BYTES,
) -> StagedProjection | None:
    """Prepare one bounded incremental projection step in a single read transaction."""
    sid = identifier(session_id)
    db.execute("BEGIN")
    try:
        srow = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not srow or srow["status"] == "deleted" or srow["mode"] != "chat":
            db.commit()
            return None
        head = read_head(db, sid)
        epoch = _active_epoch_for_session(db, sid, head)
        base = head.watermark if head else Watermark(epoch=epoch, revision=0, applied_cursor=0)
        scan = scan_complete_jsonl_lines(
            db,
            sid,
            base.applied_cursor,
            max_bytes=max_bytes,
            max_lines=max_lines,
            stream_epoch=epoch,
        )
        staged = _stage_from_scan(
            db,
            srow,
            head,
            base,
            scan,
            body_inline_bytes=body_inline_bytes,
            tool_inline_bytes=tool_inline_bytes,
        )
        db.commit()
        return staged
    except BaseException:
        db.rollback()
        raise


def _stage_from_scan(
    db,
    srow,
    head: ProjectionHead | None,
    base: Watermark,
    scan: ScanResult,
    *,
    body_inline_bytes: int,
    tool_inline_bytes: int,
) -> StagedProjection | None:
    sid = srow["id"]
    summary = dict(head.summary) if head else {}
    session_meta = _session_summary_from_row(srow)
    summary_patch: dict[str, Any] = {}
    if summary.get("session") != session_meta:
        summary["session"] = session_meta
        summary_patch["session"] = session_meta
    state = {
        "provider": srow["provider"],
        "epoch": base.epoch,
        "next_revision": base.revision + 1,
        "items": _load_relevant_items(db, sid, base.epoch),
        "summary": summary,
        "summary_patch": summary_patch,
        "touched": {},
        "body_dirty": set(),
        "output_dirty": set(),
        "body_inline_bytes": body_inline_bytes,
        "tool_inline_bytes": tool_inline_bytes,
    }
    accepted_end = base.applied_cursor
    source_state = "ready"
    blocked_cursor: int | None = None
    for event in scan.lines:
        if _is_unknown_control(event):
            source_state = "blocked_control"
            blocked_cursor = event.source_start_cursor
            break
        _reduce_one_event(state, event)
        accepted_end = event.source_end_cursor
    if source_state == "ready" and scan.error_code in {"SOURCE_GAP", "SOURCE_CORRUPT"}:
        source_state = scan.error_code.lower()
        blocked_cursor = accepted_end
    if not state["touched"] and not state["summary_patch"] and accepted_end == base.applied_cursor:
        if head and head.source_state == source_state and head.blocked_cursor == blocked_cursor:
            return None
        if head is None and scan.ingested_cursor == 0 and source_state == "ready":
            pass
    changes, db_items, body_writes, output_writes = _build_staged_changes(
        state, body_inline_bytes=body_inline_bytes
    )
    return StagedProjection(
        session_id=sid,
        base=base,
        end_cursor=accepted_end,
        items=db_items,
        body_writes=body_writes,
        output_writes=output_writes,
        changes=changes,
        summary=state["summary"],
        summary_patch=state["summary_patch"],
        source_state=source_state,
        blocked_cursor=blocked_cursor,
        has_more=scan.has_more,
    )


def write_items(db, session_id: str, staged: StagedProjection, revision: int) -> None:
    """Persist updated semantic items and externalized bodies/outputs."""
    epoch = staged.base.epoch
    for item in staged.items:
        db.execute(
            """INSERT INTO native_projection_items(
                session_id,epoch,item_key,first_cursor,item_revision,kind,active,pending,summary_json,body_ref
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id,epoch,item_key) DO UPDATE SET
                item_revision=excluded.item_revision,
                active=excluded.active,
                pending=excluded.pending,
                summary_json=excluded.summary_json,
                body_ref=excluded.body_ref""",
            (
                session_id,
                epoch,
                item["item_key"],
                item["first_cursor"],
                revision,
                item["kind"],
                item["active"],
                item["pending"],
                item["summary_json"],
                item["body_ref"],
            ),
        )
    for b in staged.body_writes:
        db.execute(
            """INSERT INTO native_projection_bodies(
                session_id,epoch,item_key,content_generation,start_byte,end_byte,total_bytes,content_revision,data
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id,epoch,item_key,content_generation,start_byte) DO UPDATE SET
                end_byte=excluded.end_byte,
                total_bytes=excluded.total_bytes,
                content_revision=excluded.content_revision,
                data=excluded.data""",
            (
                session_id,
                epoch,
                b["item_key"],
                b["content_generation"],
                b["start_byte"],
                b["end_byte"],
                b["total_bytes"],
                revision,
                b["data"],
            ),
        )
    for o in staged.output_writes:
        db.execute(
            """INSERT INTO native_projection_outputs(
                session_id,epoch,item_key,output_epoch,start_offset,end_offset,data,running
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id,epoch,item_key,output_epoch,start_offset) DO UPDATE SET
                end_offset=excluded.end_offset,
                data=excluded.data,
                running=excluded.running""",
            (
                session_id,
                epoch,
                o["item_key"],
                o["output_epoch"],
                o["start_offset"],
                o["end_offset"],
                o["data"],
                o["running"],
            ),
        )


def insert_batch(db, session_id: str, staged: StagedProjection, revision: int) -> str:
    """Persist canonical JCS batch payload and SHA-256 digest in journal."""
    batch_obj = {
        "kind": "batch",
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "session_id": session_id,
        "stream_epoch": staged.base.epoch,
        "from_revision": staged.base.revision,
        "to_revision": revision,
        "from_cursor": staged.base.applied_cursor,
        "to_cursor": staged.end_cursor,
        "changes": staged.changes,
        "summary_patch": staged.summary_patch,
    }
    payload_digest = semantic_batch_digest(batch_obj)
    payload_json = canonical_jcs(batch_obj)
    payload_bytes = len(payload_json.encode("utf-8"))
    db.execute(
        """INSERT INTO native_projection_batches(
            session_id,epoch,from_revision,to_revision,from_cursor,to_cursor,payload_json,payload_digest,payload_bytes,created
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            staged.base.epoch,
            staged.base.revision,
            revision,
            staged.base.applied_cursor,
            staged.end_cursor,
            payload_json,
            payload_digest,
            payload_bytes,
            time.time(),
        ),
    )
    return payload_digest


def _prune_journal(
    db,
    session_id: str,
    epoch: str,
    *,
    max_batches: int,
    max_bytes: int,
) -> int:
    rows = db.execute(
        "SELECT to_revision, payload_bytes FROM native_projection_batches WHERE session_id=? AND epoch=? ORDER BY to_revision DESC",
        (session_id, epoch),
    ).fetchall()
    kept_bytes = 0
    cut_revision = 0
    for idx, row in enumerate(rows):
        kept_bytes += int(row["payload_bytes"])
        if idx >= max_batches or kept_bytes > max_bytes:
            cut_revision = int(row["to_revision"])
            break
    if cut_revision:
        db.execute(
            "DELETE FROM native_projection_batches WHERE session_id=? AND epoch=? AND to_revision<=?",
            (session_id, epoch, cut_revision),
        )
        return cut_revision + 1
    return 0


def update_head(
    db,
    session_id: str,
    staged: StagedProjection,
    revision: int,
    *,
    retained_from_revision: int,
) -> None:
    """Update or insert native_projection_heads atomically."""
    db.execute(
        """INSERT INTO native_projection_heads(
            session_id,epoch,schema_version,projection_revision,applied_cursor,retained_from_revision,summary_json,source_state,blocked_cursor,updated
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            epoch=excluded.epoch,
            schema_version=excluded.schema_version,
            projection_revision=excluded.projection_revision,
            applied_cursor=excluded.applied_cursor,
            retained_from_revision=MAX(native_projection_heads.retained_from_revision, excluded.retained_from_revision),
            summary_json=excluded.summary_json,
            source_state=excluded.source_state,
            blocked_cursor=excluded.blocked_cursor,
            updated=excluded.updated""",
        (
            session_id,
            staged.base.epoch,
            PROJECTION_SCHEMA_VERSION,
            revision,
            staged.end_cursor,
            retained_from_revision,
            json.dumps(staged.summary, ensure_ascii=False),
            staged.source_state,
            staged.blocked_cursor,
            time.time(),
        ),
    )


def commit_projection_step(
    db,
    session_id: str,
    staged: StagedProjection,
    *,
    max_journal_batches: int = DEFAULT_JOURNAL_MAX_BATCHES,
    max_journal_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
) -> bool:
    """Commit one staged projection step atomically with Watermark CAS."""
    sid = identifier(session_id)
    db.execute("BEGIN IMMEDIATE")
    try:
        current = read_head(db, sid)
        tomb = read_tombstone(db, sid)
        if tomb and tomb["deleted"]:
            db.rollback()
            return False
        if current is None:
            expected_epoch = tomb["epoch"] if tomb else staged.base.epoch
            if staged.base != Watermark(expected_epoch, 0, 0):
                db.rollback()
                return False
        elif current.watermark != staged.base:
            db.rollback()
            return False
        if staged.end_cursor < staged.base.applied_cursor:
            raise ValueError("projection cursor moved backwards")
        assert_complete_line_boundary(db, sid, staged.end_cursor)
        revision = staged.base.revision + 1
        write_items(db, sid, staged, revision)
        insert_batch(db, sid, staged, revision)
        pruned_from = _prune_journal(
            db, sid, staged.base.epoch, max_batches=max_journal_batches, max_bytes=max_journal_bytes
        )
        retained = max(current.retained_from_revision if current else 0, pruned_from)
        update_head(db, sid, staged, revision, retained_from_revision=retained)
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise


def record_session_metadata_step(
    db,
    session_id: str,
    summary_patch: Mapping[str, Any],
    *,
    max_journal_batches: int = DEFAULT_JOURNAL_MAX_BATCHES,
    max_journal_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
) -> bool:
    """Increment projection_revision with from_cursor == to_cursor for metadata changes."""
    sid = identifier(session_id)
    if not summary_patch:
        return False
    db.execute("BEGIN IMMEDIATE")
    try:
        head = read_head(db, sid)
        if head is None:
            srow = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not srow or srow["status"] == "deleted" or srow["mode"] != "chat":
                db.rollback()
                return False
            epoch = _active_epoch_for_session(db, sid, None)
            base = Watermark(epoch=epoch, revision=0, applied_cursor=0)
            summary = {"session": _session_summary_from_row(srow)}
            retained_base = 0
            source_state, blocked_cursor = "building", None
        else:
            base = head.watermark
            summary = dict(head.summary)
            retained_base = head.retained_from_revision
            source_state, blocked_cursor = head.source_state, head.blocked_cursor
        changed_patch = {k: v for k, v in summary_patch.items() if summary.get(k) != v}
        if not changed_patch and head is not None:
            db.rollback()
            return False
        summary.update(changed_patch)
        staged = StagedProjection(
            session_id=sid,
            base=base,
            end_cursor=base.applied_cursor,
            changes=[],
            summary=summary,
            summary_patch=changed_patch,
            source_state=source_state,
            blocked_cursor=blocked_cursor,
        )
        revision = base.revision + 1
        insert_batch(db, sid, staged, revision)
        pruned_from = _prune_journal(
            db, sid, base.epoch, max_batches=max_journal_batches, max_bytes=max_journal_bytes
        )
        update_head(
            db, sid, staged, revision, retained_from_revision=max(retained_base, pruned_from)
        )
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise


def clear_or_delete_projection_tx(
    db,
    session_id: str,
    *,
    transition_id: str,
    deleted: bool = False,
) -> str:
    """Rotate stream_epoch idempotently per transition_id and invalidate old projection rows."""
    sid = identifier(session_id)
    now = time.time()
    tomb = read_tombstone(db, sid)
    head = read_head(db, sid)
    if tomb and transition_id and transition_id in tomb["transition_ids"]:
        if deleted and not tomb["deleted"]:
            _purge_session_projection_data(db, sid, keep_tombstone=True)
            db.execute(
                "UPDATE native_projection_tombstones SET deleted=1, updated=? WHERE session_id=?",
                (now, sid),
            )
        return tomb["epoch"]
    old_epochs = list(tomb["source_epochs"]) if tomb else []
    for candidate in (tomb["epoch"] if tomb else None, head.watermark.epoch if head else None):
        if candidate and candidate not in old_epochs:
            old_epochs.append(candidate)
    new_epoch = f"ep_{uuid.uuid4().hex[:16]}"
    transitions = (list(tomb["transition_ids"]) if tomb else []) + (
        [transition_id] if transition_id else []
    )
    _purge_session_projection_data(db, sid, keep_tombstone=True)
    db.execute(
        """INSERT INTO native_projection_tombstones(
            session_id,epoch,deleted,last_transition_id,transition_ids_json,source_epochs_json,updated
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            epoch=excluded.epoch,
            deleted=MAX(native_projection_tombstones.deleted, excluded.deleted),
            last_transition_id=excluded.last_transition_id,
            transition_ids_json=excluded.transition_ids_json,
            source_epochs_json=excluded.source_epochs_json,
            updated=excluded.updated""",
        (
            sid,
            new_epoch,
            1 if deleted else 0,
            transition_id,
            json.dumps(transitions[-64:]),
            json.dumps(old_epochs[-64:]),
            now,
        ),
    )
    if not deleted:
        srow = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        summary = {"session": _session_summary_from_row(srow)} if srow else {}
        db.execute(
            """INSERT INTO native_projection_heads(
                session_id,epoch,schema_version,projection_revision,applied_cursor,retained_from_revision,summary_json,source_state,blocked_cursor,updated
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                new_epoch,
                PROJECTION_SCHEMA_VERSION,
                0,
                0,
                0,
                json.dumps(summary, ensure_ascii=False),
                "ready",
                None,
                now,
            ),
        )
    return new_epoch


def _purge_session_projection_data(db, sid: str, *, keep_tombstone: bool = True) -> None:
    for table in (
        "native_projection_heads",
        "native_projection_items",
        "native_projection_batches",
        "native_projection_bodies",
        "native_projection_outputs",
    ):
        db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
    if not keep_tombstone:
        db.execute("DELETE FROM native_projection_tombstones WHERE session_id=?", (sid,))


def _decode_db_item(row, *, body_inline_bytes: int) -> dict[str, Any]:
    summary = json.loads(row["summary_json"])
    if row["body_ref"]:
        body = {
            "kind": "ref",
            "body_ref": row["body_ref"],
            "content_generation": strict_safe_int(summary.get("content_generation", 1), minimum=1),
            "content_revision": strict_safe_int(row["item_revision"]),
            "byte_length": strict_safe_int(summary.get("byte_length", 0))
            if "byte_length" in summary
            else len(str(summary.get("inline_text", "")).encode("utf-8")),
            "preview": str(summary.get("preview", "")),
            "partial": True,
        }
    else:
        text = str(summary.get("inline_text", ""))
        raw = text.encode("utf-8")
        if len(raw) > body_inline_bytes:
            body = {
                "kind": "ref",
                "body_ref": f"body:{row['epoch']}:{row['item_key']}:{summary.get('content_generation', 1)}",
                "content_generation": strict_safe_int(
                    summary.get("content_generation", 1), minimum=1
                ),
                "content_revision": strict_safe_int(row["item_revision"]),
                "byte_length": len(raw),
                "preview": _utf8_safe_prefix(raw, 1024),
                "partial": True,
            }
        else:
            body = {"kind": "inline", "text": text}
    out = {
        "key": row["item_key"],
        "first_cursor": strict_safe_int(row["first_cursor"]),
        "item_revision": strict_safe_int(row["item_revision"]),
        "kind": row["kind"],
        "status": summary.get("status", ""),
        "active": bool(row["active"]),
        "pending": bool(row["pending"]),
        "body": body,
    }
    for field_name in (
        "receipt",
        "parent_receipt",
        "item_id",
        "tool_id",
        "request_id",
        "name",
        "method",
        "is_error",
        "exit_code",
        "output_ref",
        "resolved",
        "resolution",
        "options",
        "details",
        "attachments",
        "steering",
    ):
        if field_name in summary and summary[field_name] is not None:
            out[field_name] = summary[field_name]
    return out


def _enrich_body_ref_meta(db, sid: str, epoch: str, item: dict[str, Any]) -> None:
    if item["body"].get("kind") != "ref":
        return
    gen = item["body"]["content_generation"]
    row = db.execute(
        "SELECT total_bytes, data FROM native_projection_bodies WHERE session_id=? AND epoch=? AND item_key=? AND content_generation=? ORDER BY start_byte LIMIT 1",
        (sid, epoch, item["key"], gen),
    ).fetchone()
    if row:
        item["body"]["byte_length"] = strict_safe_int(row["total_bytes"])
        item["body"]["preview"] = _utf8_safe_prefix(bytes(row["data"]), 1024)


def read_snapshot(
    db,
    session_id: str,
    *,
    scope_key: str,
    window_limit: int = DEFAULT_WINDOW_LIMIT,
    body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES,
    secret: bytes | None = None,
) -> dict[str, Any]:
    """Read bounded snapshot and exact watermarks within one BEGIN read tx."""
    sid = identifier(session_id)
    strict_safe_int(window_limit, minimum=1, maximum=500)
    db.execute("BEGIN")
    try:
        srow = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not srow or srow["status"] == "deleted":
            raise DevError("CLI_NOT_FOUND", "未找到会话", 404)
        if srow["mode"] != "chat":
            raise DevError(
                "CLI_TERMINAL_SESSION",
                "此记录属于旧终端历史，不能作为聊天对话回放；原记录仍保留",
                409,
            )
        head = read_head(db, sid)
        ingested = _ingested_cursor(db, sid)
        if head is None:
            epoch = _active_epoch_for_session(db, sid, None)
            snap = _empty_building_snapshot(
                db, sid, scope_key, epoch, srow, ingested, secret=secret
            )
            db.commit()
            return snap
        result = _assemble_snapshot(
            db,
            sid,
            srow,
            head,
            ingested,
            scope_key=scope_key,
            window_limit=window_limit,
            body_inline_bytes=body_inline_bytes,
            secret=secret,
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def _empty_building_snapshot(
    db, sid: str, scope_key: str, epoch: str, srow, ingested: int, *, secret: bytes | None
) -> dict[str, Any]:
    session_meta = _session_summary_from_row(srow)
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "scope_key": scope_key,
        "session_id": sid,
        "stream_epoch": epoch,
        "projection_revision": 0,
        "applied_cursor": 0,
        "retained_from_revision": 0,
        "resume_token": make_resume_token(
            db,
            session_id=sid,
            scope_key=scope_key,
            epoch=epoch,
            revision=0,
            applied_cursor=0,
            secret=secret,
        ),
        "sync": {
            "projection_state": "building",
            "source_status": session_meta.get("status", ""),
            "ingested_cursor": ingested,
            "lag_bytes": ingested,
        },
        "session": session_meta,
        "run": {"receipt": None, "status": "idle", "phase": ""},
        "items": [],
        "active_keys": [],
        "pending": {"count": 0, "items": [], "next_cursor": None},
        "history": {"has_more": False, "before": None},
    }


def _assemble_snapshot(
    db,
    sid: str,
    srow,
    head: ProjectionHead,
    ingested: int,
    *,
    scope_key: str,
    window_limit: int,
    body_inline_bytes: int,
    secret: bytes | None,
) -> dict[str, Any]:
    epoch = head.watermark.epoch
    window_rows = db.execute(
        "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? ORDER BY first_cursor DESC, item_key DESC LIMIT ?",
        (sid, epoch, window_limit),
    ).fetchall()
    active_rows = db.execute(
        "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? AND (active=1 OR pending=1) ORDER BY first_cursor, item_key LIMIT 200",
        (sid, epoch),
    ).fetchall()
    by_key = {r["item_key"]: r for r in reversed(window_rows)}
    for r in active_rows:
        by_key.setdefault(r["item_key"], r)
    ordered_rows = sorted(by_key.values(), key=lambda r: (int(r["first_cursor"]), r["item_key"]))
    items = []
    for r in ordered_rows:
        item = _decode_db_item(r, body_inline_bytes=body_inline_bytes)
        _enrich_body_ref_meta(db, sid, epoch, item)
        items.append(item)
    oldest = window_rows[-1] if window_rows else None
    has_more = False
    before_tok = None
    if oldest is not None:
        earlier = db.execute(
            "SELECT 1 FROM native_projection_items WHERE session_id=? AND epoch=? AND (first_cursor<? OR (first_cursor=? AND item_key<?)) LIMIT 1",
            (sid, epoch, oldest["first_cursor"], oldest["first_cursor"], oldest["item_key"]),
        ).fetchone()
        if earlier:
            has_more = True
            before_tok = make_history_token(
                db,
                session_id=sid,
                scope_key=scope_key,
                epoch=epoch,
                before_cursor=int(oldest["first_cursor"]),
                before_key=oldest["item_key"],
                secret=secret,
            )
    return _format_snapshot_dict(
        db,
        sid,
        srow,
        head,
        ingested,
        items,
        has_more,
        before_tok,
        scope_key=scope_key,
        secret=secret,
    )


def _format_snapshot_dict(
    db,
    sid: str,
    srow,
    head: ProjectionHead,
    ingested: int,
    items: list[dict[str, Any]],
    has_more: bool,
    before_tok: str | None,
    *,
    scope_key: str,
    secret: bytes | None,
) -> dict[str, Any]:
    session_meta = {**_session_summary_from_row(srow), **head.summary.get("session", {})}
    session_meta["process_status"] = srow["status"]
    session_meta["status"] = srow["status"]
    session_meta["title"] = srow["title"]
    proj_state = head.source_state
    if proj_state == "ready" and ingested > head.watermark.applied_cursor:
        proj_state = "syncing"
    pending_items = [it for it in items if it["pending"]]
    return {
        "schema_version": head.schema_version,
        "scope_key": scope_key,
        "session_id": sid,
        "stream_epoch": head.watermark.epoch,
        "projection_revision": head.watermark.revision,
        "applied_cursor": head.watermark.applied_cursor,
        "retained_from_revision": head.retained_from_revision,
        "blocked_cursor": head.blocked_cursor,
        "resume_token": make_resume_token(
            db,
            session_id=sid,
            scope_key=scope_key,
            epoch=head.watermark.epoch,
            revision=head.watermark.revision,
            applied_cursor=head.watermark.applied_cursor,
            secret=secret,
        ),
        "sync": {
            "projection_state": proj_state,
            "source_status": srow["status"],
            "ingested_cursor": ingested,
            "lag_bytes": max(0, ingested - head.watermark.applied_cursor),
        },
        "session": session_meta,
        "run": head.summary.get("run", {"receipt": None, "status": "idle", "phase": ""}),
        "settings": head.summary.get("settings"),
        "pending_settings": head.summary.get("pending_settings"),
        "settings_result": head.summary.get("settings_result"),
        "commands": head.summary.get("commands"),
        "command_result": head.summary.get("command_result"),
        "queue": head.summary.get("queue"),
        "stats": head.summary.get("stats"),
        "review": head.summary.get("review"),
        "control": head.summary.get("control"),
        "items": items,
        "active_keys": [it["key"] for it in items if it["active"]],
        "pending": {"count": len(pending_items), "items": pending_items[:50], "next_cursor": None},
        "history": {"has_more": has_more, "before": before_tok},
    }


def read_history_page(
    db,
    session_id: str,
    *,
    epoch: str,
    before_cursor: int,
    before_key: str,
    row_budget: int = DEFAULT_HISTORY_ROW_BUDGET,
    byte_budget: int = DEFAULT_HISTORY_BYTE_BUDGET,
    scope_key: str = "",
    body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES,
    secret: bytes | None = None,
) -> dict[str, Any]:
    """Read one older history page using keyset seek capped by rows and UTF-8 bytes."""
    sid = identifier(session_id)
    strict_safe_int(before_cursor)
    strict_safe_int(row_budget, minimum=1, maximum=500)
    strict_safe_int(byte_budget, minimum=1, maximum=4 * 1024 * 1024)
    db.execute("BEGIN")
    try:
        head = read_head(db, sid)
        if head is None:
            raise DevError("HISTORY_UNAVAILABLE", "投影尚未就绪", 409)
        if head.watermark.epoch != epoch:
            raise DevError("EPOCH_CHANGED", "会话视图世代已变更", 409)
        rows = db.execute(
            """SELECT * FROM native_projection_items
            WHERE session_id=? AND epoch=? AND (first_cursor<? OR (first_cursor=? AND item_key<?))
            ORDER BY first_cursor DESC, item_key DESC LIMIT ?""",
            (sid, epoch, before_cursor, before_cursor, before_key, row_budget + 1),
        ).fetchall()
        page_desc, has_more = _bound_history_page(
            db,
            sid,
            epoch,
            rows,
            row_budget=row_budget,
            byte_budget=byte_budget,
            body_inline_bytes=body_inline_bytes,
        )
        ordered = list(reversed(page_desc))
        next_before = None
        if has_more and ordered:
            next_before = make_history_token(
                db,
                session_id=sid,
                scope_key=scope_key,
                epoch=epoch,
                before_cursor=ordered[0]["first_cursor"],
                before_key=ordered[0]["key"],
                secret=secret,
            )
        db.commit()
        return {
            "session_id": sid,
            "stream_epoch": epoch,
            "page_revision": head.watermark.revision,
            "page_applied_cursor": head.watermark.applied_cursor,
            "items": ordered,
            "has_more": has_more,
            "before": next_before,
        }
    except BaseException:
        db.rollback()
        raise


def _bound_history_page(
    db,
    sid: str,
    epoch: str,
    rows: list,
    *,
    row_budget: int,
    byte_budget: int,
    body_inline_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    selected: list[dict[str, Any]] = []
    used_bytes = 0
    has_more = len(rows) > row_budget
    for row in rows[:row_budget]:
        item = _decode_db_item(row, body_inline_bytes=body_inline_bytes)
        _enrich_body_ref_meta(db, sid, epoch, item)
        encoded_len = len(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if selected and used_bytes + encoded_len > byte_budget:
            has_more = True
            break
        selected.append(item)
        used_bytes += encoded_len
    return selected, has_more


def _align_utf8_bounds(raw: bytes, start_in_buf: int, end_in_buf: int) -> tuple[int, int]:
    s = start_in_buf
    while s < len(raw) and (raw[s] & 0xC0) == 0x80:
        s += 1
    e = min(end_in_buf, len(raw))
    if e < len(raw):
        while e > s and (raw[e] & 0xC0) == 0x80:
            e -= 1
    return s, e


def read_item_body_chunk(
    db,
    session_id: str,
    item_key: str,
    *,
    generation: int,
    start_byte: int = 0,
    max_bytes: int = DEFAULT_BODY_CHUNK_BYTES,
) -> dict[str, Any]:
    """Read one UTF-8 codepoint-safe chunk of an externalized item body."""
    sid = identifier(session_id)
    strict_safe_int(generation, minimum=1)
    strict_safe_int(start_byte)
    strict_safe_int(max_bytes, minimum=1, maximum=1024 * 1024)
    db.execute("BEGIN")
    try:
        tomb = read_tombstone(db, sid)
        if tomb and tomb["deleted"]:
            raise DevError("HISTORY_UNAVAILABLE", "会话历史已删除", 410)
        head = read_head(db, sid)
        if head is None:
            raise DevError("HISTORY_UNAVAILABLE", "会话历史不可用", 404)
        epoch = head.watermark.epoch
        item_row = db.execute(
            "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? AND item_key=?",
            (sid, epoch, item_key),
        ).fetchone()
        if not item_row:
            raise DevError("HISTORY_UNAVAILABLE", "未找到消息正文", 404)
        summary = json.loads(item_row["summary_json"])
        current_gen = strict_safe_int(summary.get("content_generation", 1), minimum=1)
        if generation != current_gen:
            raise DevError("BODY_REVISION_EXPIRED", "正文版本已更新，请重新读取条目", 409)
        raw = _load_item_body_bytes(db, sid, epoch, item_key, generation)
        if not raw and not item_row["body_ref"]:
            raw = str(summary.get("inline_text", "")).encode("utf-8")
        total = len(raw)
        if start_byte > total:
            raise DevError("CLI_OFFSET", "正文偏移超出范围", 409)
        s, e = _align_utf8_bounds(raw, start_byte, min(total, start_byte + max_bytes))
        db.commit()
        return {
            "session_id": sid,
            "stream_epoch": epoch,
            "item_key": item_key,
            "content_generation": generation,
            "content_revision": strict_safe_int(item_row["item_revision"]),
            "start_byte": s,
            "end_byte": e,
            "total_bytes": total,
            "has_more": e < total,
            "text": raw[s:e].decode("utf-8"),
        }
    except BaseException:
        db.rollback()
        raise


def read_tool_output_view(
    db,
    session_id: str,
    item_key: str,
    *,
    after_offset: int | None = None,
    tail_bytes: int = DEFAULT_OUTPUT_TAIL_BYTES,
) -> dict[str, Any]:
    """Read bounded tool stdout as either replace-tail or contiguous append."""
    sid = identifier(session_id)
    if after_offset is not None:
        strict_safe_int(after_offset)
    strict_safe_int(tail_bytes, minimum=1, maximum=1024 * 1024)
    db.execute("BEGIN")
    try:
        head = read_head(db, sid)
        if head is None:
            raise DevError("HISTORY_UNAVAILABLE", "工具输出尚不可用", 404)
        epoch = head.watermark.epoch
        item_row = db.execute(
            "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? AND item_key=?",
            (sid, epoch, item_key),
        ).fetchone()
        if not item_row:
            raise DevError("CLI_NOT_FOUND", "未找到工具条目", 404)
        summary = json.loads(item_row["summary_json"])
        out_ref = summary.get("output_ref") or {}
        out_epoch = str(out_ref.get("output_epoch") or "")
        rows = (
            db.execute(
                "SELECT start_offset,end_offset,data,running FROM native_projection_outputs WHERE session_id=? AND epoch=? AND item_key=? AND output_epoch=? ORDER BY start_offset",
                (sid, epoch, item_key, out_epoch),
            ).fetchall()
            if out_epoch
            else []
        )
        raw = (
            b"".join(bytes(r["data"]) for r in rows)
            if rows
            else str(summary.get("inline_text", "")).encode("utf-8")
        )
        min_offset = int(rows[0]["start_offset"]) if rows else 0
        total = min_offset + len(raw)
        running = bool(item_row["active"])
        res = _slice_tool_output(
            raw,
            min_offset=min_offset,
            total=total,
            after_offset=after_offset,
            tail_bytes=tail_bytes,
            output_epoch=out_epoch or f"{epoch}:{item_key}",
            running=running,
        )
        db.commit()
        return res
    except BaseException:
        db.rollback()
        raise


def _slice_tool_output(
    raw: bytes,
    *,
    min_offset: int,
    total: int,
    after_offset: int | None,
    tail_bytes: int,
    output_epoch: str,
    running: bool,
) -> dict[str, Any]:
    if after_offset is None or after_offset < min_offset or after_offset > total:
        raw_start = max(0, len(raw) - tail_bytes)
        s, e = _align_utf8_bounds(raw, raw_start, len(raw))
        from_off = min_offset + s
        return {
            "kind": "replace",
            "output_epoch": output_epoch,
            "from_offset": from_off,
            "to_offset": min_offset + e,
            "total_bytes": total,
            "truncated_before": from_off > 0,
            "running": running,
            "text": raw[s:e].decode("utf-8"),
        }
    rel_start = after_offset - min_offset
    s, e = _align_utf8_bounds(raw, rel_start, min(len(raw), rel_start + tail_bytes))
    return {
        "kind": "append",
        "output_epoch": output_epoch,
        "from_offset": min_offset + s,
        "to_offset": min_offset + e,
        "total_bytes": total,
        "truncated_before": False,
        "running": running,
        "text": raw[s:e].decode("utf-8"),
    }


def read_item_detail(
    db, session_id: str, item_key: str, *, body_inline_bytes: int = DEFAULT_BODY_INLINE_BYTES
) -> dict[str, Any]:
    """Read a single versioned semantic item in one BEGIN read transaction."""
    sid = identifier(session_id)
    db.execute("BEGIN")
    try:
        head = read_head(db, sid)
        if head is None:
            raise DevError("CLI_NOT_FOUND", "未找到会话投影", 404)
        row = db.execute(
            "SELECT * FROM native_projection_items WHERE session_id=? AND epoch=? AND item_key=?",
            (sid, head.watermark.epoch, item_key),
        ).fetchone()
        if not row:
            raise DevError("CLI_NOT_FOUND", "未找到条目", 404)
        item = _decode_db_item(row, body_inline_bytes=body_inline_bytes)
        _enrich_body_ref_meta(db, sid, head.watermark.epoch, item)
        db.commit()
        return {
            "stream_epoch": head.watermark.epoch,
            "projection_revision": head.watermark.revision,
            "item": item,
        }
    except BaseException:
        db.rollback()
        raise


def verify_shadow_parity(
    v1_events: list[Mapping[str, Any]], snapshot: Mapping[str, Any], *, provider: str = "codex"
) -> dict[str, Any]:
    """Compare v1 chatEvent semantics against a v2 projection snapshot without transcript logging."""
    state = {
        "provider": provider,
        "epoch": snapshot.get("stream_epoch", ""),
        "next_revision": 1,
        "items": {},
        "summary": {},
        "summary_patch": {},
        "touched": {},
        "body_dirty": set(),
        "output_dirty": set(),
        "body_inline_bytes": MAX_SAFE_INT,
        "tool_inline_bytes": MAX_SAFE_INT,
    }
    for idx, raw_event in enumerate(v1_events):
        line = json.dumps(raw_event, ensure_ascii=False).encode("utf-8")
        ev = decode_raw_source_line(
            line, stream_epoch=state["epoch"], start_cursor=idx * 100, end_cursor=(idx + 1) * 100
        )
        if not _is_unknown_control(ev):
            _reduce_one_event(state, ev)
    expected = sorted(state["items"].values(), key=lambda it: (it["first_cursor"], it["item_key"]))
    actual = list(snapshot.get("items", []))
    diffs = _diff_shadow_items(expected, actual)
    for field_name in ("settings", "stats", "commands", "queue", "review"):
        if state["summary"].get(field_name) != snapshot.get(field_name):
            diffs.append(f"summary:{field_name}")
    return {"ok": not diffs, "diffs": diffs}


def _diff_shadow_items(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> list[str]:
    diffs: list[str] = []
    if [it["item_key"] for it in expected] != [it["key"] for it in actual]:
        diffs.append("order_mismatch")
        return diffs
    for exp, act in zip(expected, actual, strict=True):
        if exp["kind"] != act["kind"]:
            diffs.append(f"kind:{exp['item_key']}")
        if bool(exp["summary"].get("resolved")) != bool(act.get("resolved")):
            diffs.append(f"resolved:{exp['item_key']}")
        act_body = act.get("body", {})
        if act_body.get("kind") == "inline" and act_body.get("text") != exp["full_text"]:
            diffs.append(f"text:{exp['item_key']}")
        elif act_body.get("kind") == "ref" and act_body.get("byte_length") != len(
            exp["full_text"].encode("utf-8")
        ):
            diffs.append(f"byte_length:{exp['item_key']}")
    return diffs
