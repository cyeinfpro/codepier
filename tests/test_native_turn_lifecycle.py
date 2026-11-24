"""Deterministic fault-injection tests for native turn lifecycle, barriers, and restore."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

import agent.coding_reviews as reviews
from agent.chat_reviews import TurnReviews
from agent.native_cli import NativeCLI
from agent.filesystem import FileEngine
from agent.native_review_worker import NativeReviewWorker
from agent.native_turn_store import NativeTurnStore, StaleOwnerError, StateConflictError, TurnKey
from agent.resource_queue import Claim, ResourceQueue, canonical_path
from agent.turn_restore import TurnRestoreCoordinator, create_hidden_git_checkpoint
from agent.workspace_ownership import WorkspaceConflictError, WorkspaceOwnershipStore
from shared.native_cli import (
    WorkerLock,
    append_control_tx,
    append_spool_bytes_tx,
    database,
    immediate,
    retained_bytes,
)
from shared.util import DevError


def _hex32() -> str:
    return uuid.uuid4().hex


def _make_env(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    native_dir = state_dir / "native-cli"
    native_dir.mkdir()
    config_path = tmp_path / "config.json"
    config = {"allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}]}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    engine = FileEngine(config, SimpleNamespace(directory=state_dir), config_path)
    sid = _hex32()
    project = {
        "id": "proj1",
        "alias": "proj1",
        "root": str(root),
        "device_id": "dev1",
        "mode": "write",
        "allow_tasks": True,
        "_coding_owner": f"native:{sid}",
        "_coding_device": "dev1",
    }
    return SimpleNamespace(
        root=root,
        state_dir=state_dir,
        native_dir=native_dir,
        config_path=config_path,
        engine=engine,
        project=project,
        sid=sid,
    )


def _insert_session(conn: sqlite3.Connection, env, *, now: float = 1000.0) -> None:
    with immediate(conn):
        conn.execute(
            """INSERT INTO sessions(
                 id, project_id, device_id, root, cwd, provider, title, status,
                 created, updated, size, heartbeat, argv, mode
               ) VALUES (?,?,?,?,?,?,?,'running',?,?,0,?,'[]','chat')""",
            (
                env.sid,
                env.project["id"],
                env.project["device_id"],
                str(env.root),
                str(env.root),
                "codex",
                "Test Session",
                now,
                now,
                now,
            ),
        )


def _insert_command(conn: sqlite3.Connection, sid: str, receipt: str, *, state: str = "queued") -> None:
    with immediate(conn):
        conn.execute(
            "INSERT INTO commands(id, session, kind, payload, state, created) VALUES (?,?,?,?,?,?)",
            (receipt, sid, "chat_prompt", '{"text":"hi"}', state, 1000.0),
        )


def _setup_ready_turn(
    env,
    conn: sqlite3.Connection,
    *,
    receipt: str | None = None,
    attempt_id: str = "att-1",
    owner_epoch: int = 1,
    now: float = 1000.0,
    evidence_requested: bool = True,
):
    receipt = receipt or _hex32()
    _insert_command(conn, env.sid, receipt, state="claimed")
    store = NativeTurnStore()
    claims = WorkspaceOwnershipStore()
    claim = Claim("agent", "path", str(env.root), True, "process")
    with immediate(conn):
        claims.acquire_claim_tx(
            conn,
            claims=[claim],
            phase="execute",
            receipt=receipt,
            attempt_id=attempt_id,
            owner_epoch=owner_epoch,
            workspace_version=0,
            process_identity={"session_id": env.sid, "pid": os.getpid()},
            now=now,
        )
        turn = store.begin_turn_tx(
            conn,
            receipt=receipt,
            attempt_id=attempt_id,
            session_id=env.sid,
            provider_instance_id=_hex32(),
            workspace_key=canonical_path(env.root),
            workspace_version=0,
            owner_epoch=owner_epoch,
            evidence_requested=evidence_requested,
            now=now,
        )
    if evidence_requested:
        tr = TurnReviews(env.native_dir, {"id": env.sid, "project_id": "proj1", "device_id": "dev1", "root": str(env.root)}, env.config_path)
        prep = tr.prepare_turn_baseline(receipt, attempt_id, before_ref=turn.before_ref)
        assert prep["status"] == "baseline_ready"
        with immediate(conn):
            turn = store.record_baseline_tx(
                conn,
                TurnKey(receipt, attempt_id),
                owner_epoch=owner_epoch,
                expected_state_version=turn.state_version,
                status="baseline_ready",
                now=now,
            )
    return store, claims, turn


def test_v4_to_v5_migration_and_retained_bytes(tmp_path, monkeypatch):
    native_dir = tmp_path / "native"
    native_dir.mkdir(mode=0o700)
    db_path = native_dir / "native.sqlite3"
    v4 = sqlite3.connect(db_path)
    v4.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL,
          root TEXT NOT NULL, cwd TEXT NOT NULL, provider TEXT NOT NULL, title TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          size INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, error TEXT NOT NULL DEFAULT '',
          heartbeat REAL NOT NULL DEFAULT 0, lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          argv TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE output (session TEXT NOT NULL, offset INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(session,offset));
        CREATE TABLE commands (id TEXT PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL);
        CREATE TABLE attachments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, root TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, sha TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL);
        PRAGMA user_version=4;
        """
    )
    before_schema = v4.execute(
        "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
    ).fetchall()
    v4.close()

    import shared.native_cli as native

    # Fail after all V5 DDL has executed but before committing or setting the version.
    with monkeypatch.context() as fault:
        fault.setattr(
            native, "_V5_SCHEMA_SQL",
            native._V5_SCHEMA_SQL + "\nCREATE TABLE native_injected_failure(;\n",
        )
        with pytest.raises(sqlite3.OperationalError):
            database(native_dir)
    with closing(sqlite3.connect(db_path)) as reopened:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 4
        assert reopened.execute(
            "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall() == before_schema

    with closing(database(native_dir)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert {"provider_instance_id", "worker_epoch", "workspace_version"} <= cols
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "native_turns",
            "native_review_jobs",
            "native_control_events",
            "native_workspace_claims",
            "native_restore_journals",
        } <= tables

        sid, receipt = _hex32(), _hex32()
        with immediate(conn):
            conn.execute(
                """INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,size,argv)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, "p", "d", "/r", "/r", "codex", "t", "running", 1.0, 1.0, 0, "[]"),
            )
            append_control_tx(
                conn,
                event_id="evt-1",
                session_id=sid,
                receipt=receipt,
                attempt_id="a1",
                worker_epoch=1,
                kind="provider_completed",
                payload={"receipt": receipt, "attempt_id": "a1", "execution_state": "completed", "evidence_state": "not_requested"},
                now=2.0,
            )
        assert retained_bytes(conn) > 0


def test_before_publish_crash_reconciles_same_ref_and_missing_file_is_unavailable(tmp_path):
    env = _make_env(tmp_path)
    (env.root / "app.py").write_text("print(1)\n", encoding="utf-8")
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store = NativeTurnStore()
        receipt = _hex32()
        with immediate(conn):
            turn = store.begin_turn_tx(
                conn,
                receipt=receipt,
                attempt_id="att-1",
                session_id=env.sid,
                provider_instance_id=_hex32(),
                workspace_key=canonical_path(env.root),
                workspace_version=0,
                owner_epoch=1,
                now=1000.0,
            )
        tr = TurnReviews(env.native_dir, {"id": env.sid, "project_id": "proj1", "device_id": "dev1", "root": str(env.root)}, env.config_path)
        prep = tr.prepare_turn_baseline(receipt, "att-1", before_ref=turn.before_ref)
        assert prep["status"] == "baseline_ready"
        # Crash before DB commit of baseline_ready -> later workspace edit occurs.
        (env.root / "app.py").write_text("print(2)\n", encoding="utf-8")
        review_store = reviews.ReviewStore(env.engine)
        actions = store.recover_session_turns(
            conn, env.sid, store=review_store, project=env.project, now=1005.0
        )
        assert actions[0]["action"] == "reconciled_before_ref"
        recovered = store.get_current_tx(conn, TurnKey(receipt, "att-1"))
        assert recovered.evidence_state == "baseline_ready"
        snap = review_store.load_turn_snapshot(recovered.before_ref, env.project, kind="baseline")
        assert snap["files"]["app.py"]["text"] == "print(1)\n"
        # Missing fixed file raises REVIEW_UNAVAILABLE instead of fabricating from workspace.
        (review_store.directory / f"{recovered.before_ref}.json").unlink()
        with pytest.raises(DevError) as exc:
            review_store.load_turn_snapshot(recovered.before_ref, env.project, kind="baseline")
        assert exc.value.code == "REVIEW_UNAVAILABLE"


def test_no_stdin_before_baseline_commit_and_write_started_crash_is_uncertain(tmp_path):
    env = _make_env(tmp_path)
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store = NativeTurnStore()
        receipt = _hex32()
        with immediate(conn):
            turn = store.begin_turn_tx(
                conn,
                receipt=receipt,
                attempt_id="att-1",
                session_id=env.sid,
                provider_instance_id=_hex32(),
                workspace_key=canonical_path(env.root),
                workspace_version=0,
                owner_epoch=1,
                now=1000.0,
            )
        # Baseline still pending -> must forbid prompt write.
        with pytest.raises(StateConflictError) as exc:
            with immediate(conn):
                store.mark_write_started_tx(
                    conn,
                    TurnKey(receipt, "att-1"),
                    owner_epoch=1,
                    expected_state_version=turn.state_version,
                    now=1001.0,
                )
        assert exc.value.code == "BASELINE_NOT_COMMITTED"

        # Commit baseline_unavailable, then start write and crash before accepted.
        with immediate(conn):
            turn = store.record_baseline_tx(
                conn,
                TurnKey(receipt, "att-1"),
                owner_epoch=1,
                expected_state_version=turn.state_version,
                status="baseline_unavailable",
                error_code="REVIEW_QUOTA",
                now=1002.0,
            )
            turn = store.mark_write_started_tx(
                conn,
                TurnKey(receipt, "att-1"),
                owner_epoch=1,
                expected_state_version=turn.state_version,
                now=1003.0,
            )
        actions = store.recover_session_turns(conn, env.sid, now=1004.0)
        assert actions[0]["action"] == "marked_uncertain"
        recovered = store.get_current_tx(conn, TurnKey(receipt, "att-1"))
        assert recovered.execution_state == "uncertain"
        assert recovered.send_phase == "uncertain"
        # Re-using the same receipt to replay is forbidden.
        with pytest.raises(StateConflictError):
            with immediate(conn):
                store.begin_turn_tx(
                    conn,
                    receipt=receipt,
                    attempt_id="att-2",
                    session_id=env.sid,
                    provider_instance_id=_hex32(),
                    workspace_key=canonical_path(env.root),
                    workspace_version=0,
                    owner_epoch=1,
                    now=1005.0,
                )


def test_terminal_commit_atomic_rollback_and_interleaved_spool_append(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    (env.root / "f.txt").write_text("v1", encoding="utf-8")
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store, claims, turn = _setup_ready_turn(env, conn)
        key = TurnKey(turn.receipt, turn.attempt_id)
        with immediate(conn):
            turn = store.mark_write_started_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, now=1001.0)
            turn = store.mark_accepted_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, native_turn_id="nt-1", now=1002.0)

        # Inject fault inside append_control_tx -> entire terminal tx must roll back.
        import agent.native_turn_store as nts

        original_append = nts.append_control_tx

        def failing_append(*args, **kwargs):
            raise RuntimeError("injected control append failure")

        monkeypatch.setattr(nts, "append_control_tx", failing_append)
        with pytest.raises(RuntimeError, match="injected control append failure"):
            store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1003.0)

        monkeypatch.setattr(nts, "append_control_tx", original_append)
        mid = store.get_current_tx(conn, key)
        assert mid.execution_state == "running"
        assert mid.terminal_event_id is None
        assert conn.execute("SELECT COUNT(*) FROM native_review_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM native_control_events").fetchone()[0] == 0
        assert conn.execute("SELECT size FROM sessions WHERE id=?", (env.sid,)).fetchone()[0] == 0

        # Interleaved token flush + terminal control append share persisted tail without overlap.
        with immediate(conn):
            t_start, t_end = append_spool_bytes_tx(conn, env.sid, b'{"type":"delta","text":"hi"}\n', now=1004.0)
        res = store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1005.0)
        c_start, c_end = res["spool_range"]
        assert c_start == t_end and c_end > c_start
        # Subsequent token flush re-reads persisted tail size cleanly.
        with immediate(conn):
            t2_start, t2_end = append_spool_bytes_tx(conn, env.sid, b'{"type":"delta","text":"next"}\n', now=1006.0)
        assert t2_start == c_end
        assert conn.execute("SELECT size FROM sessions WHERE id=?", (env.sid,)).fetchone()[0] == t2_end
        # Replay persist_provider_terminal returns original event idempotently.
        replay = store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1007.0)
        assert replay["event_id"] == res["event_id"]
        assert conn.execute("SELECT COUNT(*) FROM native_control_events").fetchone()[0] == 1


def test_capture_barrier_blocks_next_turn_and_reuses_published_after_outside_build_review(tmp_path):
    env = _make_env(tmp_path)
    (env.root / "file.txt").write_text("before\n", encoding="utf-8")
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store, claims, turn = _setup_ready_turn(env, conn)
        key = TurnKey(turn.receipt, turn.attempt_id)
        with immediate(conn):
            turn = store.mark_write_started_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, now=1001.0)
            turn = store.mark_accepted_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, now=1002.0)
        (env.root / "file.txt").write_text("during-turn\n", encoding="utf-8")
        store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1003.0)

        # Next turn cannot acquire overlapping claim while capture barrier is held.
        with pytest.raises(WorkspaceConflictError):
            with immediate(conn):
                claims.acquire_claim_tx(
                    conn,
                    claims=[Claim("agent", "path", str(env.root), True, "process")],
                    phase="execute",
                    receipt=_hex32(),
                    attempt_id="next-1",
                    owner_epoch=1,
                    workspace_version=0,
                    now=1004.0,
                )

        # Simulate crash AFTER publishing after_ref snapshot, BEFORE commit_frozen_evidence_tx.
        tr = TurnReviews(env.native_dir, {"id": env.sid, "project_id": "proj1", "device_id": "dev1", "root": str(env.root)}, env.config_path)
        tr.capture_turn_after(turn.receipt, turn.attempt_id, after_ref=turn.after_ref)
        # Workspace mutates later, before worker commits!
        (env.root / "file.txt").write_text("later-unrelated-edit\n", encoding="utf-8")

    # Worker runs capture_after on its own connection and reuses already published after_ref!
    with NativeReviewWorker(env.native_dir, lambda _sid: (env.engine, env.project)) as worker:
        cap_res = worker.run_capture_job(f"capture:{turn.receipt}:{turn.attempt_id}", now=1005.0)
        assert cap_res["evidence_state"] == "frozen"

    # Now barrier is released -> next turn can start even BEFORE build_review runs!
    with closing(database(env.native_dir)) as conn:
        with immediate(conn):
            next_ids = claims.acquire_claim_tx(
                conn,
                claims=[Claim("agent", "path", str(env.root), True, "process")],
                phase="execute",
                receipt=_hex32(),
                attempt_id="next-2",
                owner_epoch=1,
                workspace_version=0,
                now=1006.0,
            )
        assert len(next_ids) == 1

    # build_review runs completely outside capture barrier and ignores later workspace edits.
    (env.root / "file.txt").write_text("even-later-edit\n", encoding="utf-8")
    with NativeReviewWorker(env.native_dir, lambda _sid: (env.engine, env.project)) as worker:
        rev_res = worker.run_review_job(f"review:{turn.receipt}:{turn.attempt_id}", now=1007.0)
        assert rev_res["review_state"] == "ready"
        detail = reviews.read_review(env.engine, env.project, {"review_ref": rev_res["review_ref"], "path": "file.txt"})
        assert "-before" in detail["diff"]
        assert "+during-turn" in detail["diff"]
        assert "later-unrelated-edit" not in detail["diff"]
        assert "even-later-edit" not in detail["diff"]


def test_agent_reconcile_dispatches_only_durable_eligible_evidence_jobs(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    (env.root / "x.txt").write_text("baseline", encoding="utf-8")
    now = time.time()
    monkeypatch.setattr("agent.workspace_ownership.process_exists", lambda _pid: False)
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env, now=now)
        store, claims, turn = _setup_ready_turn(env, conn, now=now)
        key = TurnKey(turn.receipt, turn.attempt_id)
        (env.root / "x.txt").write_text("completed", encoding="utf-8")
        store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=now)
        capture_id = f"capture:{turn.receipt}:{turn.attempt_id}"
        review_id = f"review:{turn.receipt}:{turn.attempt_id}"

    cli = NativeCLI(SimpleNamespace(state_dir=env.state_dir, engine=env.engine, config={}))
    with cli.review_worker as worker:
        # Startup's actual reconcile boundary must queue the original pending capture.
        worker._pumped[capture_id].result(timeout=10)
        with closing(database(env.native_dir)) as conn:
            assert store.get_current_tx(conn, key).evidence_state == "frozen"
            assert conn.execute("SELECT attempts FROM native_review_jobs WHERE id=?", (capture_id,)).fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM native_control_events WHERE kind='provider_completed'").fetchone()[0] == 1

        # The same bounded dispatcher used during an idle connected Agent handles
        # the review job from frozen evidence, independent of the capture barrier.
        assert worker.pump_due() == (review_id,)
        worker._pumped[review_id].result(timeout=10)
        assert worker.pump_due() == ()
        with closing(database(env.native_dir)) as conn:
            assert store.get_current_tx(conn, key).review_state == "ready"
            assert conn.execute("SELECT attempts FROM native_review_jobs WHERE id=?", (review_id,)).fetchone()[0] == 1

            _, _, second = _setup_ready_turn(env, conn, now=now + 1.0)
            second_key = TurnKey(second.receipt, second.attempt_id)
            store.persist_provider_terminal(
                conn, second_key, "completed", owner_epoch=1, claims=claims, now=now + 1.0
            )
            second_id = f"capture:{second.receipt}:{second.attempt_id}"
            with immediate(conn):
                conn.execute(
                    "UPDATE native_workspace_claims SET phase='unknown' WHERE receipt=?",
                    (second.receipt,),
                )

        # No positive ownership proof means no fresh workspace sampling.
        cli.reconcile_runtime(now=now + 2.0)
        assert worker.pump_due(now=now + 2.0) == ()
        with closing(database(env.native_dir)) as conn:
            assert conn.execute("SELECT attempts FROM native_review_jobs WHERE id=?", (second_id,)).fetchone()[0] == 0
            assert store.get_current_tx(conn, second_key).evidence_state == "capture_pending"


def test_competing_job_workers_fenced_and_three_tier_cancellation(tmp_path):
    env = _make_env(tmp_path)
    (env.root / "x.txt").write_text("a\n", encoding="utf-8")
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store, claims, turn = _setup_ready_turn(env, conn)
        key = TurnKey(turn.receipt, turn.attempt_id)
        with immediate(conn):
            turn = store.mark_write_started_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, now=1001.0)
            turn = store.mark_accepted_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, native_turn_id="nt-1", now=1002.0)

        # Wrong turn_id / epoch interrupt rejected.
        with immediate(conn):
            assert not store.mark_interrupt_requested_tx(conn, key, owner_epoch=99, now=1002.1)
            assert not store.mark_interrupt_requested_tx(conn, key, owner_epoch=1, expected_native_turn_id="nt-other", now=1002.1)
            assert not store.cancel_queued_tx(conn, key, owner_epoch=1, now=1002.2)

        store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1003.0)
        job_id = f"capture:{turn.receipt}:{turn.attempt_id}"
        c1 = store.claim_job(conn, job_id, owner="w1", owner_epoch=1, lease_seconds=5.0, now=1004.0)
        assert c1 is not None and c1.fence == 1
        assert store.claim_job(conn, job_id, owner="w2", owner_epoch=1, now=1005.0) is None
        # Lease expires and prior holder is dead -> reclaim increments fence to 2.
        with immediate(conn):
            assert store.reclaim_stale_job_tx(conn, job_id, now=1010.0, holder_alive=False)
        c2 = store.claim_job(conn, job_id, owner="w2", owner_epoch=1, now=1010.0)
        assert c2 is not None and c2.fence == 2

        # Stale worker w1 (fence=1) is rejected; w2 (fence=2) succeeds.
        with pytest.raises(StaleOwnerError):
            with immediate(conn):
                store.commit_frozen_evidence_tx(
                    conn, key, job_id=job_id, fence=1, after_ref=turn.after_ref,
                    capture_started_at=1004.0, capture_finished_at=1004.5,
                    coverage={"complete": True, "skipped": []}, claims=claims, now=1011.0,
                )
        with immediate(conn):
            frozen = store.commit_frozen_evidence_tx(
                conn, key, job_id=job_id, fence=2, after_ref=turn.after_ref,
                capture_started_at=1010.0, capture_finished_at=1010.5,
                coverage={"complete": True, "skipped": []}, claims=claims, now=1011.0,
            )
            # Tier 3: cancelling build_review keeps execution_state=completed and evidence intact.
            assert store.cancel_review_job_tx(conn, key, now=1012.0)
        final_turn = store.get_current_tx(conn, key)
        assert final_turn.execution_state == "completed"
        assert final_turn.evidence_state == "frozen"
        assert final_turn.review_state == "cancelled"
        assert frozen["review_job_id"] == f"review:{turn.receipt}:{turn.attempt_id}"


def test_child_writer_drain_gate_and_reconcile_claims(tmp_path):
    env = _make_env(tmp_path)
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        store, claims, turn = _setup_ready_turn(env, conn)
        key = TurnKey(turn.receipt, turn.attempt_id)
        with immediate(conn):
            assert claims.register_child_writer_tx(
                conn, parent_receipt=turn.receipt, parent_attempt_id=turn.attempt_id, writer_id="child-1", now=1001.0
            )
            turn = store.mark_write_started_tx(conn, key, owner_epoch=1, expected_state_version=turn.state_version, now=1002.0)
        store.persist_provider_terminal(conn, key, "completed", owner_epoch=1, claims=claims, now=1003.0)

        # Capture worker sees active child writer -> blocks without releasing barrier.
        with NativeReviewWorker(env.native_dir, lambda _sid: (env.engine, env.project)) as worker:
            res = worker.run_capture_job(f"capture:{turn.receipt}:{turn.attempt_id}", now=1004.0)
            assert res == {"status": "blocked", "reason": "CHILD_WRITERS_ACTIVE"}

        # Admission is now closed -> late child registration rejected; finishing child-1 drains gate.
        with immediate(conn):
            assert not claims.register_child_writer_tx(
                conn, parent_receipt=turn.receipt, parent_attempt_id=turn.attempt_id, writer_id="child-2", now=1004.5
            )
            assert claims.finish_child_writer_tx(
                conn, parent_receipt=turn.receipt, parent_attempt_id=turn.attempt_id, writer_id="child-1", now=1005.0
            )
        assert claims.active_claims_tx(conn)  # Still held in capture until capture commits!

        # Reconcile with live WorkerLock keeps claim; unknown holder becomes 'unknown', not released.
        with WorkerLock(env.native_dir, env.sid):
            rep_live = claims.reconcile_claims(env.native_dir, conn, now=1006.0)
            assert rep_live["kept"]
        with immediate(conn):
            claims.acquire_claim_tx(
                conn,
                claims=[Claim("agent", "path", str(tmp_path / "other"), True, "process")],
                phase="execute",
                receipt=_hex32(),
                attempt_id="unprovable",
                owner_epoch=1,
                workspace_version=0,
                process_identity="",
                now=1007.0,
            )
        rep = claims.reconcile_claims(env.native_dir, conn, now=1008.0)
        assert any(cid.endswith(":0") for cid in rep["unknown"])


def test_resource_queue_strict_capture_barrier_symlink_and_disjoint_worktrees(tmp_path):
    real_ws = tmp_path / "ws"
    real_ws.mkdir()
    sub = real_ws / "sub"
    sub.mkdir()
    alias = tmp_path / "ws-link"
    os.symlink(real_ws, alias)
    other_wt = tmp_path / "wt2"
    other_wt.mkdir()

    clock = [0.0]
    queue = ResourceQueue(semantics="relaxed", clock=lambda: clock[0], bypass_seconds=1)
    cap_token = queue.try_reserve(
        "capture-1",
        [Claim("agent", "path", canonical_path(real_ws), True, "process", True, strict_barrier=True)],
        strict=True,
    )
    assert cap_token is not None

    # Disjoint worktree proceeds in parallel immediately.
    wt2_token = queue.try_reserve(
        "wt2-op",
        [Claim("agent", "path", canonical_path(other_wt), True, "process")],
    )
    assert wt2_token is not None

    # Symlink alias & child directory read (even bounded=True) cannot cross strict capture barrier.
    clock[0] = 100.0
    for target in (alias, sub):
        blocked_tok = object()
        queue.enqueue(
            blocked_tok,
            f"read-{target.name}",
            [Claim("agent", "path", canonical_path(target), False, "file")],
            bounded=True,
        )
        assert queue.blocked(blocked_tok)
        queue.discard(blocked_tok)


def test_hub_offline_pin_retention_and_bounded_lease_expiry(tmp_path):
    env = _make_env(tmp_path)
    (env.root / "m.txt").write_text("hello", encoding="utf-8")
    base_now = time.time()
    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env, now=base_now)
        store, claims, turn = _setup_ready_turn(env, conn, now=base_now)
        rstore = reviews.ReviewStore(env.engine)
        # Even after BASELINE_TTL (24h) expires while Hub is offline, active pin protects before_ref.
        after_25h = base_now + reviews.BASELINE_TTL + 3600.0
        with rstore.lock():
            kept = list(rstore._records(now=after_25h))
        assert any(p.stem == turn.before_ref for p, _, _ in kept)

        # After MAX_PIN_LEASE_SECONDS, recovery transitions expired pin before GC cleans it up.
        after_8d = base_now + reviews.MAX_PIN_LEASE_SECONDS + 3600.0
        actions = store.recover_session_turns(
            conn, env.sid, store=rstore, project=env.project, claims=claims, now=after_8d
        )
        assert actions[0]["action"] == "expired_baseline_pin"
        with rstore.lock():
            remaining = list(rstore._records(now=after_8d))
        assert remaining == []


def test_joint_restore_partial_failure_holds_barrier_and_hidden_git_checkpoint(tmp_path):
    env = _make_env(tmp_path)
    (env.root / "a.txt").write_text("cur-a", encoding="utf-8")
    (env.root / "b.txt").write_text("cur-b", encoding="utf-8")
    sha_a = __import__("hashlib").sha256(b"cur-a").hexdigest()
    sha_b = __import__("hashlib").sha256(b"cur-b").hexdigest()

    # Hidden Git ref checkpoint does not touch user index or HEAD.
    subprocess.run(["git", "init"], cwd=env.root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=env.root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=env.root, check=True)
    subprocess.run(["git", "add", "a.txt"], cwd=env.root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=env.root, check=True, capture_output=True)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.root, check=True, capture_output=True, text=True).stdout.strip()
    ckpt = create_hidden_git_checkpoint(env.root, checkpoint_id="cp1")
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.root, check=True, capture_output=True, text=True).stdout.strip()
    assert head_before == head_after and ckpt["ref"] == "refs/codepier/checkpoints/cp1"
    # b.txt was untracked in user index, so user index still doesn't stage b.txt.
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=env.root, check=True, capture_output=True, text=True).stdout.strip()
    assert staged == ""

    with closing(database(env.native_dir)) as conn:
        _insert_session(conn, env)
        claims = WorkspaceOwnershipStore()
        coord = TurnRestoreCoordinator()
        receipt = _hex32()
        prep = coord.prepare_restore(
            conn,
            session_id=env.sid,
            receipt=receipt,
            attempt_id="rst-1",
            checkpoint_ref=ckpt["ref"],
            mode="native_rewind",
            target_files=[
                {"path": "a.txt", "expected_current_sha256": sha_a, "target_content": "old-a"},
                {"path": "b.txt", "expected_current_sha256": sha_b, "target_content": "old-b"},
            ],
            expected_workspace_version=0,
            owner_epoch=1,
            claims=claims,
            workspace_claims=[Claim("agent", "path", str(env.root), True, "process")],
            now=1000.0,
        )
        rid = prep["restore_id"]
        with pytest.raises(DevError) as missing_callback:
            coord.apply_provider_step(conn, rid, now=1001.0)
        assert missing_callback.value.code == "RESTORE_UNSUPPORTED"
        assert conn.execute(
            "SELECT stage FROM native_restore_journals WHERE restore_id=?", (rid,)
        ).fetchone()["stage"] == "prepared"

        rewind_calls = []

        def confirm_rewind(_journal):
            rewind_calls.append(rid)
            with closing(database(env.native_dir)) as other:
                with pytest.raises(DevError) as duplicate:
                    coord.apply_provider_step(other, rid, provider_fn=confirm_rewind, now=1001.0)
                assert duplicate.value.code == "RESTORE_INVALID_STAGE"
            return {"ok": True, "native_thread_id": "thr-1"}

        failed_id = coord.prepare_restore(
            conn,
            session_id=env.sid,
            receipt=_hex32(),
            attempt_id="rst-failed",
            checkpoint_ref=ckpt["ref"],
            mode="native_rewind",
            target_files=[],
            expected_workspace_version=0,
            owner_epoch=1,
            claims=claims,
            workspace_claims=[],
            now=1000.0,
        )["restore_id"]
        with pytest.raises(DevError) as rejected:
            coord.apply_provider_step(
                conn, failed_id, provider_fn=lambda _j: {"ok": False}, now=1001.0
            )
        assert rejected.value.code == "PROVIDER_REWIND_UNCONFIRMED"
        failed = conn.execute(
            "SELECT stage, error_code FROM native_restore_journals WHERE restore_id=?", (failed_id,)
        ).fetchone()
        assert tuple(failed) == ("uncertain", "PROVIDER_REWIND_UNCONFIRMED")
        with pytest.raises(DevError) as blocked:
            coord.restore_files_checked(conn, failed_id, root=env.root)
        assert blocked.value.code == "RESTORE_INVALID_STAGE"
        assert (env.root / "a.txt").read_bytes() == b"cur-a"

        coord.apply_provider_step(conn, rid, provider_fn=confirm_rewind, now=1001.0)
        assert rewind_calls == [rid]

        # Inject write failure on 2nd file -> partial state, barrier remains held!
        calls = []

        def flaky_writer(path: Path, data: bytes) -> None:
            calls.append(path.name)
            if path.name == "b.txt":
                raise OSError("disk full")
            path.write_bytes(data)

        partial = coord.restore_files_checked(conn, rid, root=env.root, file_writer=flaky_writer, now=1002.0)
        assert partial["stage"] == "partial" and partial["barrier_held"]
        assert [f["status"] for f in partial["files"]] == ["restored", "failed"]
        with pytest.raises(DevError) as exc:
            coord.commit_runtime_binding(conn, rid, claims=claims, now=1003.0)
        assert exc.value.code == "RESTORE_INCOMPLETE"
        with pytest.raises(WorkspaceConflictError):
            with immediate(conn):
                claims.acquire_claim_tx(
                    conn,
                    claims=[Claim("agent", "path", str(env.root), True, "process")],
                    phase="execute",
                    receipt=_hex32(),
                    attempt_id="blocked-after-partial",
                    owner_epoch=1,
                    workspace_version=0,
                    now=1004.0,
                )
