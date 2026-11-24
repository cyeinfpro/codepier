"""Versioned native protocol fixture replay and fault-injection tests."""

from __future__ import annotations

from pathlib import Path
import pytest

from tests.native_support import NativeFixtureReplayer, Utf8LineChunker, validate_manifest

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "native"


def _event_matches(actual: dict, expected: dict) -> bool:
    return all(actual.get(k) == v for k, v in expected.items())


def _wire_matches(actual: dict, expected: dict) -> bool:
    return all(actual.get(k) == v for k, v in expected.items())


@pytest.mark.parametrize(
    "fixture_rel",
    [
        "pi/v1/basic_turn",
        "codex/v1/approval_and_next_turn_settings",
        "claude/v1/stream_and_block_repair",
    ],
)
def test_versioned_fixture_replay_and_invariants(fixture_rel: str) -> None:
    replayer = NativeFixtureReplayer(FIXTURES_ROOT / fixture_rel)
    report = replayer.replay()
    assert report.invariants_ok, report.violations
    for expected in replayer.wire_out_expected:
        assert any(_wire_matches(w, expected) for w in report.wire_out), (expected, report.wire_out)
    for expected in replayer.events_expected:
        assert any(_event_matches(e, expected) for e in report.events), (expected, report.events)


@pytest.mark.parametrize("chunk_size", [1, 3, 7])
def test_arbitrary_utf8_byte_chunk_splitting_and_duplicate_frames(chunk_size: int) -> None:
    replayer = NativeFixtureReplayer(FIXTURES_ROOT / "pi/v1/basic_turn")
    report = replayer.replay(
        chunk_size=chunk_size,
        duplicate_wire_indices={8, 9},
        inject_stale_settings_readback=True,
    )
    assert report.invariants_ok, report.violations
    assert report.receipt_states["11111111111111111111111111111111"] == "completed"


def test_utf8_line_chunker_splits_multibyte_characters_cleanly() -> None:
    chunker = Utf8LineChunker()
    raw = '{"text":"中文🚀测试"}\n{"text":"second"}\n'.encode("utf-8")
    parsed = []
    for i in range(0, len(raw), 2):
        parsed.extend(chunker.feed(raw[i : i + 2]))
    parsed.extend(chunker.finish())
    assert parsed == [{"text": "中文🚀测试"}, {"text": "second"}]


def test_codex_delayed_settings_and_control_delta_interleaving() -> None:
    replayer = NativeFixtureReplayer(FIXTURES_ROOT / "codex/v1/approval_and_next_turn_settings")
    report = replayer.replay(
        chunk_size=5,
        interleave_control_frames=[
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": 42}}}
        ],
    )
    assert report.invariants_ok, report.violations
    assert any(e.get("type") == "stats" for e in report.events)


def test_claude_stream_block_repair_and_duplicate_result_dedup() -> None:
    replayer = NativeFixtureReplayer(FIXTURES_ROOT / "claude/v1/stream_and_block_repair")
    report = replayer.replay(chunk_size=4, duplicate_wire_indices={10, 12})
    assert report.invariants_ok, report.violations
    messages = [e for e in report.events if e.get("type") == "message"]
    assert len(messages) == 2
    assert all(m["text"] == "Part 1 + repaired" and m["block_index"] == 0 and m["item_id"] == "msg-c1:0" for m in messages)
    done_events = [e for e in report.events if e.get("type") == "done"]
    assert len(done_events) == 1


def test_manifest_sanitization_rejects_private_markers() -> None:
    with pytest.raises(ValueError, match="Unsanitized marker"):
        validate_manifest(
            {
                "provider": "pi",
                "protocol_version": "v1",
                "schema_version": 2,
                "source": "synthetic",
                "sanitization_version": "1.0",
                "cli_hash": "h",
                "harness_hash": "r",
                "cwd": "/Users/private/repo",
            }
        )
