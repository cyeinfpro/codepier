"""Tests for hub.native_projection durability, watermarks, digests, and reducer parity."""

from __future__ import annotations

from contextlib import closing
import json
from types import SimpleNamespace
import uuid

import pytest

from hub.native_projection import (
    ProjectionHead,
    Watermark,
    clear_or_delete_projection_tx,
    commit_projection_step,
    ensure_projection_schema,
    prepare_projection_step,
    read_head,
    read_item_body_chunk,
    read_snapshot,
    read_tombstone,
    record_session_metadata_step,
    scan_complete_jsonl_lines,
    verify_shadow_parity,
)
import hub.native_projection as proj_mod
from shared.native_cli import database, public
from shared.native_contracts import MAX_SAFE_INT, semantic_batch_digest, strict_safe_int
from shared.util import DevError
from tests.test_chat_sse import service


def _insert_raw(db, sid: str, offset: int, data: bytes) -> None:
    db.execute("INSERT INTO output VALUES (?,?,?)", (sid, offset, data))
    cur = db.execute("SELECT COALESCE(MAX(offset+length(data)),0) FROM output WHERE session=?", (sid,)).fetchone()[0]
    db.execute("UPDATE sessions SET size=? WHERE id=?", (cur, sid))


def _run_all_steps(db, sid: str, **kwargs) -> int:
    count = 0
    while True:
        staged = prepare_projection_step(db, sid, **kwargs)
        if staged is None:
            break
        assert commit_projection_step(db, sid, staged) is True
        count += 1
    return count


def test_schema_tables_and_indexes_created(service):
    obj, _, _ = service
    with closing(database(obj.directory)) as db:
        ensure_projection_schema(db)
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
    assert {
        "native_projection_heads",
        "native_projection_items",
        "native_projection_batches",
        "native_projection_bodies",
        "native_projection_outputs",
        "native_projection_tombstones",
    } <= tables
    assert {"native_projection_items_order", "native_projection_items_active"} <= indexes


def test_split_utf8_and_half_jsonl_line_stop_applied_cursor(service):
    obj, _, sid = service
    line1 = (json.dumps({"type": "user", "receipt": "r1", "text": "hello"}, ensure_ascii=False) + "\n").encode()
    line2 = (json.dumps({"type": "delta", "receipt": "r1", "item_id": "m1", "text": "你好世界🚀"}, ensure_ascii=False) + "\n").encode()
    split_utf8 = line2.index("你".encode()) + 1
    part1 = line1 + line2[:split_utf8]
    part2 = line2[split_utf8:]

    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, part1)

    with closing(database(obj.directory)) as db:
        scan = scan_complete_jsonl_lines(db, sid, 0)
        assert len(scan.lines) == 1
        assert scan.end_cursor == len(line1)
        assert scan.ingested_cursor == len(part1)
        assert scan.error_code is None
        _run_all_steps(db, sid)
        snap = read_snapshot(db, sid, scope_key="sc_test")
        assert snap["applied_cursor"] == len(line1)
        assert snap["sync"]["ingested_cursor"] == len(part1)
        assert snap["sync"]["lag_bytes"] == split_utf8
        assert snap["sync"]["projection_state"] == "syncing"

    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, len(part1), part2)

    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        snap = read_snapshot(db, sid, scope_key="sc_test")
        assert snap["applied_cursor"] == len(line1) + len(line2)
        assert snap["sync"]["projection_state"] == "ready"
        assistant = next(it for it in snap["items"] if it["kind"] == "assistant")
        assert assistant["body"] == {"kind": "inline", "text": "你好世界🚀"}


def test_lagging_projector_declares_real_applied_cursor_not_session_size(service):
    obj, _, sid = service
    l1 = b'{"type":"user","receipt":"r1","text":"one"}\n'
    l2 = b'{"type":"message","receipt":"r1","item_id":"m1","text":"two"}\n'
    l3 = b'{"type":"message","receipt":"r1","item_id":"m2","text":"three"}\n'
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, l1 + l2 + l3)

    with closing(database(obj.directory)) as db:
        staged = prepare_projection_step(db, sid, max_lines=1)
        assert staged is not None
        assert commit_projection_step(db, sid, staged) is True
        snap = read_snapshot(db, sid, scope_key="sc_test")
        assert snap["applied_cursor"] == len(l1)
        assert snap["sync"]["ingested_cursor"] == len(l1 + l2 + l3)
        assert snap["sync"]["lag_bytes"] == len(l2 + l3)
        assert len(snap["items"]) == 1
        assert _run_all_steps(db, sid, max_lines=1) == 2
        snap2 = read_snapshot(db, sid, scope_key="sc_test")
        assert snap2["applied_cursor"] == len(l1 + l2 + l3)
        assert len(snap2["items"]) == 3


def test_source_gap_and_source_corrupt_do_not_advance_applied_cursor(service):
    obj, _, sid = service
    valid = b'{"type":"user","receipt":"r1","text":"ok"}\n'
    corrupt = b'{"type":"delta","bad":\xff}\n'
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, valid)
        _insert_raw(db, sid, len(valid), corrupt)

    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        head = read_head(db, sid)
        assert head is not None
        assert head.watermark.applied_cursor == len(valid)
        assert head.source_state == "source_corrupt"
        assert head.blocked_cursor == len(valid)

    # Gap test in fresh session
    sid2 = uuid.uuid4().hex
    with closing(database(obj.directory)) as db, db:
        db.execute(
            "INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid2, "project", "device", "/tmp", "/tmp", "codex", "Gap", "running", 1, 1, "chat"),
        )
        _insert_raw(db, sid2, 64, valid)

    with closing(database(obj.directory)) as db:
        scan = scan_complete_jsonl_lines(db, sid2, 0)
        assert scan.error_code == "SOURCE_GAP"
        _run_all_steps(db, sid2)
        head2 = read_head(db, sid2)
        assert head2 is not None
        assert head2.watermark.applied_cursor == 0
        assert head2.source_state == "source_gap"


def test_unknown_critical_control_blocks_without_advancing_cursor(service):
    obj, _, sid = service
    l1 = b'{"type":"user","receipt":"r1","text":"hi"}\n'
    unknown_ctrl = json.dumps(
        {
            "schema_version": 1,
            "event_id": "e1",
            "event_seq": 1,
            "session_id": sid,
            "worker_epoch": 1,
            "kind": "future.critical.gate",
            "body": {"must_halt": True},
        }
    ).encode() + b"\n"
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, l1 + unknown_ctrl)

    with closing(database(obj.directory)) as db:
        assert _run_all_steps(db, sid) == 1
        head = read_head(db, sid)
        assert head is not None
        assert head.watermark.applied_cursor == len(l1)
        assert head.source_state == "blocked_control"
        assert head.blocked_cursor == len(l1)
        # Re-running does not loop or bump revision
        assert prepare_projection_step(db, sid) is None


def test_commit_transaction_atomicity_and_watermark_cas(service, monkeypatch):
    obj, _, sid = service
    l1 = b'{"type":"user","receipt":"r1","text":"first"}\n'
    l2 = b'{"type":"message","receipt":"r1","item_id":"m1","text":"second"}\n'
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, l1)

    with closing(database(obj.directory)) as db:
        staged = prepare_projection_step(db, sid)
        assert staged is not None
        orig_update_head = proj_mod.update_head

        def crash_head(*args, **kwargs):
            raise RuntimeError("injected crash before head")

        monkeypatch.setattr(proj_mod, "update_head", crash_head)
        with pytest.raises(RuntimeError, match="injected crash"):
            commit_projection_step(db, sid, staged)
        assert read_head(db, sid) is None
        assert db.execute("SELECT COUNT(*) FROM native_projection_items WHERE session_id=?", (sid,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM native_projection_batches WHERE session_id=?", (sid,)).fetchone()[0] == 0

        monkeypatch.setattr(proj_mod, "update_head", orig_update_head)
        assert commit_projection_step(db, sid, staged) is True

    # CAS mismatch test
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, len(l1), l2)
    with closing(database(obj.directory)) as db:
        s_a = prepare_projection_step(db, sid)
        s_b = prepare_projection_step(db, sid)
        assert s_a is not None and s_b is not None
        assert commit_projection_step(db, sid, s_a) is True
        assert commit_projection_step(db, sid, s_b) is False


def test_strict_safe_int_and_jcs_digest_verification(service):
    for bad in (True, False, 1.5, "1", -1, MAX_SAFE_INT + 1):
        with pytest.raises(ValueError):
            strict_safe_int(bad)
    assert strict_safe_int(0) == 0
    assert strict_safe_int(MAX_SAFE_INT) == MAX_SAFE_INT

    obj, _, sid = service
    ev = json.dumps({"type": "user", "receipt": "r1", "text": "emoji 🚀 中文"}, ensure_ascii=False).encode() + b"\n"
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, ev)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        row = db.execute("SELECT payload_json, payload_digest FROM native_projection_batches WHERE session_id=?", (sid,)).fetchone()
        assert row is not None
        assert row["payload_digest"] == semantic_batch_digest(json.loads(row["payload_json"]))


def test_reducer_claude_repair_settings_approvals_controls_and_shadow_parity(service):
    obj, _, sid = service
    with closing(database(obj.directory)) as db, db:
        db.execute("UPDATE sessions SET provider='claude' WHERE id=?", (sid,))
    events = [
        {"type": "user", "receipt": "r1", "text": "check repo"},
        {"type": "reasoning", "receipt": "r1", "item_id": "th1", "text": "thinking..."},
        {"type": "delta", "receipt": "r1", "item_id": "msg1:2", "text": "partial reply"},
        {"type": "message", "receipt": "r1", "item_id": "msg1:0", "text": "partial reply"},
        {"type": "approval", "receipt": "r1", "request_id": "req1", "method": "confirm", "text": "allow?"},
        {"type": "settings", "model": "claude-sonnet", "effort": "high"},
        {"type": "settings_result", "receipt": "s1", "outcome": "partial", "actual": {"model": "claude-sonnet"}},
        {"type": "commands", "commands": [{"name": "compact"}]},
        {"type": "queue", "queue": [{"receipt": "r2", "text": "queued"}]},
        {"type": "stats", "stats": {"tokens": 42}},
        {"type": "review", "receipt": "r1", "status": "ready", "summary": "1 file"},
        {
            "schema_version": 1,
            "event_id": "ev_pc",
            "event_seq": 1,
            "session_id": sid,
            "worker_epoch": 3,
            "receipt": "a" * 32,
            "run_id": "run1",
            "kind": "provider_completed",
            "body": {
                "receipt": "a" * 32,
                "attempt_id": "att1",
                "execution_state": "completed",
                "evidence_state": "frozen",
            },
        },
        {"type": "done", "receipt": "r1", "status": "completed"},
    ]
    raw = b"".join(json.dumps(e, ensure_ascii=False).encode() + b"\n" for e in events)
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, raw)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        snap = read_snapshot(db, sid, scope_key="sc_1")
        assistant = [it for it in snap["items"] if it["kind"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["key"] == "r1:assistant:msg1:2"
        assert snap["settings_result"]["outcome"] == "partial"
        assert snap["commands"] == [{"name": "compact"}]
        assert snap["stats"] == {"tokens": 42}
        assert snap["control"]["provider_completed"]["worker_epoch"] == 3
        assert snap["control"]["provider_completed"]["body"]["execution_state"] == "completed"
        # Approval closed by done
        approval = next(it for it in snap["items"] if it["kind"] == "approval")
        assert approval["resolved"] is True
        assert approval["pending"] is False
        parity = verify_shadow_parity(events, snap, provider="claude")
        assert parity == {"ok": True, "diffs": []}


def test_large_body_externalization_utf8_chunk_slicing_and_generation_expiry(service):
    obj, _, sid = service
    big_text = ("你好世界A" * 5000)  # > 16 KiB with multi-byte UTF-8
    e1 = json.dumps({"type": "message", "receipt": "r1", "item_id": "m1", "text": big_text}, ensure_ascii=False).encode() + b"\n"
    e2 = json.dumps({"type": "message", "receipt": "r1", "item_id": "m1", "text": big_text + "UPDATED"}, ensure_ascii=False).encode() + b"\n"
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, e1)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        snap = read_snapshot(db, sid, scope_key="sc_1")
        item = snap["items"][0]
        assert item["body"]["kind"] == "ref"
        assert item["body"]["byte_length"] == len(big_text.encode("utf-8"))
        assert item["body"]["partial"] is True
        gen1 = item["body"]["content_generation"]
        c1 = read_item_body_chunk(db, sid, item["key"], generation=gen1, start_byte=0, max_bytes=10)
        assert c1["has_more"] is True
        c2 = read_item_body_chunk(db, sid, item["key"], generation=gen1, start_byte=c1["end_byte"], max_bytes=100000)
        assert c1["text"] + c2["text"] == big_text
        assert c2["has_more"] is False

    # Replace body bumps generation and expires gen1
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, len(e1), e2)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        snap2 = read_snapshot(db, sid, scope_key="sc_1")
        gen2 = snap2["items"][0]["body"]["content_generation"]
        assert gen2 == gen1 + 1
        with pytest.raises(DevError) as expired:
            read_item_body_chunk(db, sid, item["key"], generation=gen1, start_byte=0)
        assert expired.value.code == "BODY_REVISION_EXPIRED"


def test_metadata_only_revision_bump_and_clear_tombstone_idempotency(service):
    obj, _, sid = service
    l1 = b'{"type":"user","receipt":"r1","text":"hi"}\n'
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, l1)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        h1 = read_head(db, sid)
        assert record_session_metadata_step(db, sid, {"session": {"title": "Renamed", "process_status": "exited"}}) is True
        h2 = read_head(db, sid)
        assert h2.watermark.revision == h1.watermark.revision + 1
        assert h2.watermark.applied_cursor == h1.watermark.applied_cursor
        assert record_session_metadata_step(db, sid, {"session": {"title": "Renamed", "process_status": "exited"}}) is False

        # Idempotent clear_or_delete_projection_tx per transition_id
        with db:
            db.execute("DELETE FROM output WHERE session=?", (sid,))
            ep_a = clear_or_delete_projection_tx(db, sid, transition_id="tx-clear-1", deleted=False)
            ep_b = clear_or_delete_projection_tx(db, sid, transition_id="tx-clear-1", deleted=False)
        assert ep_a == ep_b
        assert ep_a != h2.watermark.epoch
        tomb = read_tombstone(db, sid)
        assert tomb is not None
        assert h2.watermark.epoch in tomb["source_epochs"]

    # Late old-epoch chunk rejected by _persist_sync_detailed
    with closing(database(obj.directory)) as db:
        row = public(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    res = obj._persist_sync_detailed(
        "device",
        {
            "type": "native_sync",
            "sessions": [row],
            "chunks": [{"id": sid, "offset": 0, "stream_epoch": h2.watermark.epoch, "data": "e30K"}],
        },
    )
    assert res["offsets"][sid] == 0
    with closing(database(obj.directory)) as db:
        assert db.execute("SELECT COUNT(*) FROM output WHERE session=?", (sid,)).fetchone()[0] == 0


def test_worker_epoch_change_preserves_stream_epoch(service):
    obj, _, sid = service
    e1 = json.dumps(
        {
            "schema_version": 1,
            "event_id": "e1",
            "event_seq": 1,
            "session_id": sid,
            "worker_epoch": 1,
            "kind": "user",
            "body": {"text": "turn 1"},
        }
    ).encode() + b"\n"
    e2 = json.dumps(
        {
            "schema_version": 1,
            "event_id": "e2",
            "event_seq": 2,
            "session_id": sid,
            "worker_epoch": 2,
            "kind": "assistant",
            "body": {"item_id": "m1", "text": "reply after worker restart"},
        }
    ).encode() + b"\n"
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, 0, e1)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        ep1 = read_head(db, sid).watermark.epoch
    with closing(database(obj.directory)) as db, db:
        _insert_raw(db, sid, len(e1), e2)
    with closing(database(obj.directory)) as db:
        _run_all_steps(db, sid)
        ep2 = read_head(db, sid).watermark.epoch
        assert ep2 == ep1
