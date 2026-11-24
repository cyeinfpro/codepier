"""Shared support and deterministic fixture replayer for native protocol suites."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from agent.chat_worker import Protocol
from agent.claude_protocol import ClaudeProtocol

REQUIRED_MANIFEST_FIELDS = {
    "provider",
    "protocol_version",
    "schema_version",
    "source",
    "sanitization_version",
    "cli_hash",
    "harness_hash",
}
PRIVATE_PATH_MARKERS = ("/Users/", "/home/", "C:\\Users\\", "sk-", "Bearer ")


@dataclass(frozen=True)
class ReplayReport:
    """Result produced by NativeFixtureReplayer for one fixture directory."""

    provider: str
    protocol_version: str
    wire_out: list[dict[str, Any]]
    events: list[dict[str, Any]]
    finished: list[tuple[str, str]]
    receipt_states: dict[str, str]
    prompt_counts: dict[str, int]
    content_sha256: str
    invariants_ok: bool
    violations: list[str]


class Utf8LineChunker:
    """Incremental UTF-8 JSONL decoder supporting arbitrary byte-chunk splits."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buf.extend(chunk)
        decoded: list[dict[str, Any]] = []
        while b"\n" in self._buf:
            raw_line, _, rest = self._buf.partition(b"\n")
            self._buf[:] = rest
            if raw_line.strip():
                decoded.append(json.loads(raw_line.decode("utf-8")))
        return decoded

    def finish(self) -> list[dict[str, Any]]:
        if not self._buf.strip():
            self._buf.clear()
            return []
        line = bytes(self._buf)
        self._buf.clear()
        return [json.loads(line.decode("utf-8"))]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse non-empty lines from a JSONL file."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate versioned fixture manifest metadata and sanitization guards."""
    missing = REQUIRED_MANIFEST_FIELDS - set(manifest)
    if missing:
        raise ValueError(f"Manifest missing required fields: {sorted(missing)}")
    if manifest["source"] not in {"synthetic", "real-capture"}:
        raise ValueError(f"Invalid fixture source: {manifest['source']}")
    raw_text = json.dumps(manifest, ensure_ascii=False)
    for marker in PRIVATE_PATH_MARKERS:
        if marker in raw_text:
            raise ValueError(f"Unsanitized marker found in manifest: {marker}")


def _make_protocol(provider: str, sent: list[dict[str, Any]], events: list[dict[str, Any]], finished: list[tuple[str, str]], receipt_states: dict[str, str]):
    row = {"cwd": "/fixture/project", "chat_settings": "{}", "native_thread": ""}

    def finish(receipt: str, state: str) -> None:
        finished.append((receipt, state))
        receipt_states[receipt] = state

    def persist(_thread: str) -> None:
        return None

    cls = ClaudeProtocol if provider == "claude" else Protocol
    proto = cls(provider, row, sent.append, lambda k, **v: None, finish, persist)

    def emit(kind: str, **values: Any) -> None:
        receipt = values.get("receipt", proto.active)
        events.append({"type": kind, "receipt": receipt, **values})

    proto.emit = emit
    return proto


def _apply_step(
    proto: Protocol,
    step: dict[str, Any],
    *,
    prompt_counts: dict[str, int],
    terminal_receipts: set[str],
    violations: list[str],
) -> None:
    op = step.get("op")
    if op == "start":
        proto.start()
        return
    if op == "prompt":
        receipt = step["receipt"]
        if receipt in terminal_receipts:
            violations.append(f"post_terminal_resurrection:{receipt}")
        prompt_counts[receipt] = prompt_counts.get(receipt, 0) + 1
        proto.prompt(receipt, step["payload"])
        return
    if op == "settings":
        proto.change_settings(step["receipt"], step["payload"])
        return
    if op == "answer":
        if proto.active and step.get("run_receipt") and step["run_receipt"] != proto.active:
            violations.append(f"cross_run_approval:{step['run_receipt']}!={proto.active}")
        proto.answer(step["request_id"], step["answer"])
        return
    if op == "wire":
        frame = step["frame"]
        proto.receive(frame)
        return
    raise ValueError(f"Unsupported replay step op: {op}")


def _feed_wire_chunks(
    proto: Protocol,
    wire_frame: dict[str, Any],
    chunk_size: int,
    duplicate: bool,
) -> None:
    raw = (json.dumps(wire_frame, ensure_ascii=False) + "\n").encode("utf-8")
    decoder = Utf8LineChunker()
    for idx in range(0, len(raw), max(1, chunk_size)):
        for parsed in decoder.feed(raw[idx : idx + max(1, chunk_size)]):
            proto.receive(parsed)
            if duplicate:
                proto.receive(parsed)
    for parsed in decoder.finish():
        proto.receive(parsed)
        if duplicate:
            proto.receive(parsed)


def _check_prompt_and_resurrection(
    invariants: dict[str, Any],
    prompt_counts: dict[str, int],
    events: list[dict[str, Any]],
    violations: list[str],
) -> None:
    max_prompts = invariants.get("max_prompts_per_receipt", 1)
    for receipt, count in prompt_counts.items():
        if count > max_prompts:
            violations.append(f"prompt_count_exceeded:{receipt}:{count}>{max_prompts}")
    if not invariants.get("no_post_terminal_resurrection", True):
        return
    seen_done: set[str] = set()
    for ev in events:
        rec = ev.get("receipt")
        if not rec:
            continue
        if ev.get("type") in {"delta", "message", "reasoning", "tool"} and rec in seen_done:
            violations.append(f"post_terminal_event:{rec}:{ev.get('type')}")
        if ev.get("type") == "done":
            seen_done.add(rec)


def _check_cross_run_approval(
    invariants: dict[str, Any],
    events: list[dict[str, Any]],
    violations: list[str],
) -> None:
    if not invariants.get("no_cross_run_approval", True):
        return
    active_receipt: str | None = None
    pending_owners: dict[str, str | None] = {}
    for ev in events:
        kind = ev.get("type")
        if kind == "user" and ev.get("receipt"):
            active_receipt = ev["receipt"]
        elif kind == "approval":
            req_id = str(ev.get("request_id", ""))
            if not ev.get("resolved"):
                pending_owners[req_id] = active_receipt
            elif req_id in pending_owners and pending_owners[req_id] != active_receipt:
                violations.append(f"cross_run_approval_resolved:{req_id}")
        elif kind == "done":
            active_receipt = None


def _check_invariants(
    invariants: dict[str, Any],
    *,
    prompt_counts: dict[str, int],
    events: list[dict[str, Any]],
    finished: list[tuple[str, str]],
    violations: list[str],
) -> str:
    _check_prompt_and_resurrection(invariants, prompt_counts, events, violations)
    _check_cross_run_approval(invariants, events, violations)

    text_chunks = [
        ev.get("text", "")
        for ev in events
        if ev.get("type") in {"delta", "message"} and isinstance(ev.get("text"), str)
    ]
    digest = hashlib.sha256("".join(text_chunks).encode("utf-8")).hexdigest()
    expected_digest = invariants.get("content_sha256")
    if expected_digest and digest != expected_digest:
        violations.append(f"content_sha256_mismatch:{digest}!={expected_digest}")

    actual = {rec: st for rec, st in finished}
    for rec, expected_st in invariants.get("expected_terminal_states", {}).items():
        if actual.get(rec) != expected_st:
            violations.append(f"terminal_state_mismatch:{rec}:{actual.get(rec)}!={expected_st}")
    return digest


class NativeFixtureReplayer:
    """Deterministic replayer with byte-chunk, duplicate, settings-race, and interleave faults."""

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = Path(fixture_dir)
        self.manifest = json.loads((self.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        validate_manifest(self.manifest)
        self.wire_in = load_jsonl(self.fixture_dir / "wire.in.jsonl")
        self.wire_out_expected = load_jsonl(self.fixture_dir / "wire.out.expected.jsonl")
        self.events_expected = load_jsonl(self.fixture_dir / "events.expected.jsonl")
        self.invariants = json.loads(
            (self.fixture_dir / "invariants.json").read_text(encoding="utf-8")
        )

    def replay(
        self,
        *,
        chunk_size: int = 0,
        duplicate_wire_indices: set[int] | None = None,
        inject_stale_settings_readback: bool = False,
        interleave_control_frames: list[dict[str, Any]] | None = None,
    ) -> ReplayReport:
        provider = self.manifest["provider"]
        sent: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        finished: list[tuple[str, str]] = []
        receipt_states: dict[str, str] = {}
        prompt_counts: dict[str, int] = {}
        violations: list[str] = []
        terminal_receipts: set[str] = set()
        dup_indices = duplicate_wire_indices or set()
        proto = _make_protocol(provider, sent, events, finished, receipt_states)

        for idx, step in enumerate(self.wire_in):
            for rec, st in finished:
                if st in {"completed", "error", "interrupted"}:
                    terminal_receipts.add(rec)
            if step.get("op") == "wire" and chunk_size > 0:
                _feed_wire_chunks(proto, step["frame"], chunk_size, idx in dup_indices)
            else:
                _apply_step(
                    proto,
                    step,
                    prompt_counts=prompt_counts,
                    terminal_receipts=terminal_receipts,
                    violations=violations,
                )
                if step.get("op") == "wire" and idx in dup_indices:
                    proto.receive(step["frame"])
            if inject_stale_settings_readback and step.get("op") == "settings" and provider == "pi":
                proto.receive(
                    {
                        "type": "response",
                        "id": "stale-readback-0",
                        "success": True,
                        "data": {"model": {"provider": "pi", "id": "stale"}, "thinkingLevel": "low"},
                    }
                )
            if interleave_control_frames and idx == len(self.wire_in) // 2:
                for ctrl in interleave_control_frames:
                    proto.receive(ctrl)

        digest = _check_invariants(
            self.invariants,
            prompt_counts=prompt_counts,
            events=events,
            finished=finished,
            violations=violations,
        )
        return ReplayReport(
            provider=provider,
            protocol_version=self.manifest["protocol_version"],
            wire_out=sent,
            events=events,
            finished=finished,
            receipt_states=receipt_states,
            prompt_counts=prompt_counts,
            content_sha256=digest,
            invariants_ok=not violations,
            violations=violations,
        )
