"""Web SSH v1 byte framing and strict, bounded JSON control messages.

Decoding proves shape only.  Callers must compare every binding with the
authenticated attachment and current authority before forwarding bytes.
"""

from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import IntEnum
from typing import Any
from uuid import UUID

from .ssh_terminal_config import DEFAULT_CONFIG, TerminalConfig

PROTOCOL_VERSION = 1
UINT64_MAX = (1 << 64) - 1
HEADER = struct.Struct("!BB16s16sQQQI")
HEADER_SIZE = HEADER.size
_UUID_RE = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
_COUNTER_RE = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")


class TerminalProtocolError(ValueError):
    """A safe diagnostic code; never include input payloads or credentials."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FrameKind(IntEnum):
    INPUT = 1
    OUTPUT = 2


def canonical_uuid(value: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise TerminalProtocolError("INVALID_BINDING", "Expected a UUID binding")
    return UUID(value).hex


def checked_uint64(value: int, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= UINT64_MAX:
        raise TerminalProtocolError(
            "INVALID_COUNTER", "Counter is outside the unsigned 64-bit range"
        )
    return value


def parse_counter(value: str) -> int:
    """JSON counters must be decimal strings, including values below 2**53."""
    if not isinstance(value, str) or not _COUNTER_RE.fullmatch(value):
        raise TerminalProtocolError(
            "INVALID_COUNTER", "JSON counters must be canonical decimal strings"
        )
    return checked_uint64(int(value))


@dataclass(frozen=True)
class TerminalFrame:
    kind: FrameKind
    session_id: str
    runtime_generation: str
    stream_epoch: int
    writer_epoch: int
    sequence: int
    payload: bytes

    def __post_init__(self) -> None:
        if isinstance(self.kind, bool) or not isinstance(self.kind, int):
            raise TerminalProtocolError("INVALID_FRAME_KIND", "Unknown terminal frame kind")
        try:
            kind = FrameKind(self.kind)
        except (ValueError, TypeError) as exc:
            raise TerminalProtocolError(
                "INVALID_FRAME_KIND", "Unknown terminal frame kind"
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "session_id", canonical_uuid(self.session_id))
        object.__setattr__(self, "runtime_generation", canonical_uuid(self.runtime_generation))
        checked_uint64(self.stream_epoch)
        checked_uint64(self.writer_epoch)
        checked_uint64(self.sequence, positive=kind == FrameKind.INPUT)
        if not isinstance(self.payload, bytes) or not self.payload:
            raise TerminalProtocolError(
                "INVALID_PAYLOAD", "A terminal data frame requires non-empty bytes"
            )
        if kind == FrameKind.OUTPUT:
            checked_uint64(self.sequence + len(self.payload))

    @property
    def offset(self) -> int:
        return self.sequence

    @property
    def input_seq(self) -> int:
        return self.sequence

    @property
    def end_offset(self) -> int:
        return self.sequence + len(self.payload)

    @property
    def seq(self) -> int:
        return self.sequence


def _frame_limit(kind: FrameKind, config: TerminalConfig) -> int:
    return config.input_frame_bytes if kind == FrameKind.INPUT else config.frame_bytes


def encode_frame(frame: TerminalFrame, config: TerminalConfig | None = None) -> bytes:
    cfg = config or DEFAULT_CONFIG
    if len(frame.payload) > _frame_limit(frame.kind, cfg):
        raise TerminalProtocolError(
            "FRAME_TOO_LARGE", "Terminal data exceeds the negotiated frame limit"
        )
    return (
        HEADER.pack(
            PROTOCOL_VERSION,
            int(frame.kind),
            UUID(frame.session_id).bytes,
            UUID(frame.runtime_generation).bytes,
            frame.stream_epoch,
            frame.writer_epoch,
            frame.sequence,
            len(frame.payload),
        )
        + frame.payload
    )


def decode_frame(
    raw: bytes | bytearray | memoryview, config: TerminalConfig | None = None
) -> TerminalFrame:
    cfg = config or DEFAULT_CONFIG
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TerminalProtocolError("INVALID_FRAME", "Terminal data must be binary")
    if isinstance(raw, memoryview) and (raw.ndim != 1 or raw.itemsize != 1 or not raw.c_contiguous):
        raise TerminalProtocolError(
            "INVALID_FRAME", "Terminal data must be a contiguous byte buffer"
        )
    if len(raw) < HEADER_SIZE:
        raise TerminalProtocolError("TRUNCATED_FRAME", "Terminal data header is incomplete")
    if len(raw) > HEADER_SIZE + max(cfg.frame_bytes, cfg.input_frame_bytes):
        raise TerminalProtocolError(
            "FRAME_TOO_LARGE", "Terminal data exceeds the negotiated frame limit"
        )
    try:
        version, raw_kind, sid, generation, epoch, writer, sequence, length = HEADER.unpack_from(
            raw
        )
    except (struct.error, BufferError, TypeError) as exc:
        raise TerminalProtocolError("INVALID_FRAME", "Terminal data header is invalid") from exc
    if version != PROTOCOL_VERSION:
        raise TerminalProtocolError("PROTOCOL_MISMATCH", "Unsupported terminal protocol version")
    try:
        kind = FrameKind(raw_kind)
    except ValueError as exc:
        raise TerminalProtocolError("INVALID_FRAME_KIND", "Unknown terminal frame kind") from exc
    if not 0 < length <= _frame_limit(kind, cfg):
        raise TerminalProtocolError(
            "FRAME_TOO_LARGE", "Terminal data length is outside the negotiated limit"
        )
    if len(raw) != HEADER_SIZE + length:
        raise TerminalProtocolError(
            "FRAME_LENGTH_MISMATCH", "Terminal frame length does not match its header"
        )
    return TerminalFrame(
        kind,
        UUID(bytes=sid).hex,
        UUID(bytes=generation).hex,
        epoch,
        writer,
        sequence,
        bytes(raw[HEADER_SIZE:]),
    )


def validate_binding(
    frame: TerminalFrame,
    *,
    session_id: str,
    runtime_generation: str,
    stream_epoch: int,
    writer_epoch: int | None = None,
) -> None:
    if frame.session_id != canonical_uuid(session_id):
        raise TerminalProtocolError("SESSION_MISMATCH", "Frame belongs to another session")
    if frame.runtime_generation != canonical_uuid(runtime_generation):
        raise TerminalProtocolError("STALE_GENERATION", "Frame belongs to another Agent generation")
    if frame.stream_epoch != checked_uint64(stream_epoch):
        raise TerminalProtocolError("STALE_STREAM", "Frame belongs to another output stream")
    if writer_epoch is not None and frame.writer_epoch != checked_uint64(writer_epoch):
        raise TerminalProtocolError("STALE_WRITER", "Frame belongs to another writer")


Validator = Callable[[Any], None]


def _text(maximum: int, minimum: int = 1, pattern: str | None = None) -> Validator:
    compiled = re.compile(pattern) if pattern else None

    def validate(value: Any) -> None:
        if not isinstance(value, str) or not minimum <= len(value) <= maximum:
            raise TerminalProtocolError(
                "INVALID_CONTROL", "Control text has an invalid length or type"
            )
        if any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise TerminalProtocolError(
                "INVALID_CONTROL", "Control text contains control characters"
            )
        if compiled and not compiled.fullmatch(value):
            raise TerminalProtocolError("INVALID_CONTROL", "Control text has an invalid format")

    return validate


def _uuid(value: Any) -> None:
    canonical_uuid(value)


def _counter(value: Any) -> None:
    parse_counter(value)


def _enum(*choices: str) -> Validator:
    def validate(value: Any) -> None:
        if not isinstance(value, str) or value not in choices:
            raise TerminalProtocolError("INVALID_CONTROL", "Unknown control enum value")

    return validate


def _int(minimum: int, maximum: int) -> Validator:
    def validate(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise TerminalProtocolError("INVALID_CONTROL", "Control integer is outside its bounds")

    return validate


def _number(value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 2**53 - 1
        or not math.isfinite(value)
    ):
        raise TerminalProtocolError("INVALID_CONTROL", "Invalid control timestamp")


def _boolean(value: Any) -> None:
    if not isinstance(value, bool):
        raise TerminalProtocolError("INVALID_CONTROL", "Expected a control boolean")


def _nullable_exit(value: Any) -> None:
    if value is not None:
        _int(-(2**31), 2**31 - 1)(value)


def _auth_text(maximum: int) -> Validator:
    def validate(value: Any) -> None:
        if not isinstance(value, str) or len(value) > maximum:
            raise TerminalProtocolError("INVALID_CONTROL", "Authentication text exceeds its bound")
        if any(
            (ord(char) < 32 and char not in "\t\r\n")
            or ord(char) == 127
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        ):
            raise TerminalProtocolError(
                "INVALID_CONTROL", "Authentication text contains unsupported characters"
            )

    return validate


def _auth_prompts(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise TerminalProtocolError(
            "INVALID_CONTROL", "Authentication prompt count is outside its bound"
        )
    for prompt in value:
        if not isinstance(prompt, dict) or set(prompt) != {"text", "echo"}:
            raise TerminalProtocolError("INVALID_CONTROL", "Invalid authentication prompt fields")
        _auth_text(512)(prompt["text"])
        _boolean(prompt["echo"])


def _auth_responses(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise TerminalProtocolError(
            "INVALID_CONTROL", "Authentication response count is outside its bound"
        )
    for response in value:
        # Empty responses are valid for keyboard-interactive.  They remain
        # secrets and are never interpolated into validation errors.
        if not isinstance(response, str) or len(response) > 4096:
            raise TerminalProtocolError(
                "INVALID_CONTROL", "Authentication response exceeds its bound"
            )
        if any(0xD800 <= ord(char) <= 0xDFFF for char in response):
            raise TerminalProtocolError(
                "INVALID_CONTROL", "Authentication response is not valid Unicode"
            )


def _challenge_deadline(value: Any) -> None:
    _number(value)
    if not 0 < value <= 120:
        raise TerminalProtocolError(
            "INVALID_CONTROL", "Authentication deadline is outside its bound"
        )


_TOKEN = _text(1024, 16, r"[A-Za-z0-9._~\-]+")
_CODE = _text(80, 1, r"[A-Z][A-Z0-9_]*")
_STATE = _enum(
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
_RESUME = _enum("live", "redraw", "resume_unconsumed", "history_replay", "gap", "restricted")
_SCOPE = {"attachment_id": _uuid, "render_epoch": _counter, "stream_epoch": _counter}
_BINDING = {**_SCOPE, "runtime_generation": _uuid, "writer_epoch": _counter}


def _schemas(config: TerminalConfig) -> tuple[dict, dict]:
    dims = {
        "cols": _int(config.min_cols, config.max_cols),
        "rows": _int(config.min_rows, config.max_rows),
    }
    client = {
        "authenticate": (
            {"ticket": _TOKEN, "renderer_id": _uuid},
            {
                "protocol_version": _int(PROTOCOL_VERSION, PROTOCOL_VERSION),
                "runtime_generation": _uuid,
                "known_epoch": _counter,
                "rendered_offset": _counter,
            },
        ),
        "output_ack": ({**_SCOPE, "rendered_offset": _counter}, {"runtime_generation": _uuid}),
        "resize": (
            {
                "attachment_id": _uuid,
                "runtime_generation": _uuid,
                "stream_epoch": _counter,
                "writer_epoch": _counter,
                "resize_seq": _counter,
                **dims,
            },
            {"render_epoch": _counter},
        ),
        "takeover": ({"request_id": _uuid, "expected_writer_epoch": _counter}, {}),
        "detach": ({"request_id": _uuid}, {"reason": _text(64)}),
        "close": ({"request_id": _uuid}, {"reason": _text(64), "expected_generation": _uuid}),
        "ping": ({"nonce": _text(64)}, {"timestamp": _number}),
        "pong": ({"nonce": _text(64)}, {"timestamp": _number}),
        "auth_answer": ({"challenge_id": _uuid, "responses": _auth_responses}, {}),
    }
    state_optional = {
        **_BINDING,
        "session_id": _uuid,
        "reason_code": _CODE,
        "recoverability": _enum(
            "resume", "redraw", "new_session", "reauthenticate", "none", "unknown"
        ),
        "final_offset": _counter,
        "attachment_exit_status": _nullable_exit,
        "remote_session_state": _enum("unknown", "running", "missing", "ended"),
        "output_complete": _boolean,
        "resume_strategy": _RESUME,
    }
    server = {
        "attached": (
            {
                **_BINDING,
                "session_id": _uuid,
                "mode": _enum("direct", "tmux"),
                "resume_strategy": _RESUME,
                "state": _STATE,
                "start_offset": _counter,
            },
            {"output_end_offset": _counter, "next_input_seq": _counter, **dims},
        ),
        "input_ack": (
            {
                "writer_epoch": _counter,
                "input_seq": _counter,
                "status": _enum("claimed", "forwarded", "uncertain", "already_claimed"),
            },
            {**_SCOPE, "runtime_generation": _uuid},
        ),
        "replay_complete": ({**_SCOPE, "final_offset": _counter}, {"runtime_generation": _uuid}),
        "state": ({"state": _STATE}, state_optional),
        "error": (
            {"reason_code": _CODE},
            {**state_optional, "message": _text(512), "retryable": _boolean},
        ),
        "exit": ({"final_offset": _counter, "output_complete": _boolean}, state_optional),
        "history_gap": (
            {**_SCOPE, "available_start": _counter, "available_end": _counter},
            {"runtime_generation": _uuid, "resume_strategy": _RESUME},
        ),
        "auth_challenge": (
            {
                "challenge_id": _uuid,
                "prompts": _auth_prompts,
                "deadline_seconds": _challenge_deadline,
            },
            {
                "name": _auth_text(256),
                "instruction": _auth_text(1024),
                "session_id": _uuid,
                "runtime_generation": _uuid,
            },
        ),
        "ping": client["ping"],
        "pong": client["pong"],
    }
    return client, server


def validate_control(
    message: Any,
    *,
    direction: str = "client",
    config: TerminalConfig | None = None,
    allowed_types: Collection[str] | None = None,
) -> dict[str, Any]:
    cfg = config or DEFAULT_CONFIG
    if direction not in {"client", "server"}:
        raise ValueError("direction must be 'client' or 'server'")
    if not isinstance(message, dict) or len(message) > 24:
        raise TerminalProtocolError("INVALID_CONTROL", "Expected a bounded control object")
    message_type = message.get("type")
    schemas = _schemas(cfg)[direction == "server"]
    if not isinstance(message_type, str) or message_type not in schemas:
        raise TerminalProtocolError("UNKNOWN_CONTROL", "Unknown terminal control message")
    if allowed_types is not None and message_type not in allowed_types:
        raise TerminalProtocolError(
            "UNEXPECTED_CONTROL", "Control is not allowed in the current state"
        )
    required, optional = schemas[message_type]
    if not required.keys() <= message.keys():
        raise TerminalProtocolError("INVALID_CONTROL", "Required control fields are missing")
    if message.keys() - {"type"} - required.keys() - optional.keys():
        raise TerminalProtocolError("INVALID_CONTROL", "Unknown control fields are not allowed")
    for name, validator in {**optional, **required}.items():
        if name in message:
            validator(message[name])
    # Keep counter strings untouched: no silently lossy JS number conversion.
    return dict(message)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TerminalProtocolError("INVALID_CONTROL", "Duplicate control keys are not allowed")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise TerminalProtocolError("INVALID_CONTROL", "Non-finite JSON numbers are not allowed")


def parse_control(
    raw: str | bytes,
    *,
    direction: str = "client",
    config: TerminalConfig | None = None,
    allowed_types: Collection[str] | None = None,
) -> dict[str, Any]:
    cfg = config or DEFAULT_CONFIG
    if not isinstance(raw, (str, bytes)):
        raise TerminalProtocolError("INVALID_CONTROL", "Expected a JSON text message")
    try:
        if len(raw) > cfg.control_bytes:
            raise TerminalProtocolError(
                "CONTROL_TOO_LARGE", "Control message exceeds its byte limit"
            )
        encoded = raw.encode("utf-8", "strict") if isinstance(raw, str) else raw
        if len(encoded) > cfg.control_bytes:
            raise TerminalProtocolError(
                "CONTROL_TOO_LARGE", "Control message exceeds its byte limit"
            )
        text = encoded.decode("utf-8", "strict")
        message = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
    except TerminalProtocolError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise TerminalProtocolError(
            "INVALID_CONTROL", "Control message is not valid bounded UTF-8 JSON"
        ) from exc
    return validate_control(message, direction=direction, config=cfg, allowed_types=allowed_types)


def encode_control(
    message: dict[str, Any],
    *,
    direction: str = "server",
    config: TerminalConfig | None = None,
) -> str:
    cfg = config or DEFAULT_CONFIG
    checked = validate_control(message, direction=direction, config=cfg)
    encoded = json.dumps(checked, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > cfg.control_bytes:
        raise TerminalProtocolError("CONTROL_TOO_LARGE", "Control message exceeds its byte limit")
    return encoded
