"""Bounded executor for decoupled after-snapshot capture and offline review builds."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import threading
import time
from typing import Any, Callable

from agent.coding_reviews import (
    ReviewStore,
    binding,
    capture,
    freeze_review_from_snapshots,
)
from agent.native_turn_store import JobClaim, NativeTurnStore, TurnKey, TurnRecord
from agent.workspace_ownership import WorkspaceOwnershipStore
from shared.native_cli import database, immediate
from shared.util import DevError


class NativeReviewWorker:
    """Runs capture_after and build_review jobs on dedicated SQLite connections."""

    def __init__(
        self,
        directory: str | Path,
        engine_factory: Callable[[str], tuple[Any, dict[str, Any]]],
        *,
        owner: str = "native-review-worker",
        owner_epoch: int = 1,
        max_workers: int = 2,
        turn_store: NativeTurnStore | None = None,
        claims: WorkspaceOwnershipStore | None = None,
    ):
        self.directory = Path(directory)
        self.engine_factory = engine_factory
        self.owner = owner
        self.owner_epoch = owner_epoch
        self.turn_store = turn_store or NativeTurnStore()
        self.claims = claims or WorkspaceOwnershipStore()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="native-review"
        )
        self._pump_lock = threading.Lock()
        self._pump_capacity = max_workers
        self._pumped: dict[str, Future] = {}

    def __enter__(self) -> NativeReviewWorker:
        return self

    def __exit__(self, *args) -> None:
        # Never auto-release durable capture barriers on worker exit or error.
        self._executor.shutdown(wait=True)

    def submit_capture(self, job_id: str) -> Future[dict[str, Any] | None]:
        return self._executor.submit(self.run_capture_job, job_id)

    def submit_review(self, job_id: str) -> Future[dict[str, Any] | None]:
        return self._executor.submit(self.run_review_job, job_id)

    def pump_due(self, *, now: float | None = None) -> tuple[str, ...]:
        """Dispatch due, durably authorized jobs without inflating the executor queue."""
        now_ts = time.time() if now is None else now
        with self._pump_lock:
            self._pumped = {key: task for key, task in self._pumped.items() if not task.done()}
            capacity = self._pump_capacity - len(self._pumped)
            if capacity <= 0:
                return ()
            rows = self._due_jobs(now_ts, capacity)
            dispatched: list[str] = []
            for row in rows:
                job_id = str(row["id"])
                if job_id in self._pumped:
                    continue
                execute = (
                    self.run_capture_job if row["kind"] == "capture_after" else self.run_review_job
                )
                self._pumped[job_id] = self._executor.submit(execute, job_id, now=now_ts)
                dispatched.append(job_id)
            return tuple(dispatched)

    def _due_jobs(self, now: float, limit: int) -> list:
        with closing(database(self.directory)) as conn:
            return conn.execute(
                """SELECT j.id, j.kind FROM native_review_jobs j
                   JOIN native_turns t USING (receipt,attempt_id)
                   JOIN sessions s ON s.id=t.session_id
                   WHERE j.status IN ('pending','blocked') AND j.available_at<=?
                     AND (j.recovery_deadline IS NULL OR j.recovery_deadline>?)
                     AND t.current_attempt=1 AND s.status!='deleted'
                     AND ((j.kind='build_review' AND t.evidence_state IN ('frozen','partial')
                           AND t.review_state='pending')
                       OR (j.kind='capture_after' AND t.evidence_state IN ('capture_pending','capturing')
                           AND t.execution_state IN ('completed','failed','interrupted')
                           AND EXISTS (SELECT 1 FROM native_workspace_claims c
                                        WHERE c.receipt=t.receipt AND c.attempt_id=t.attempt_id AND c.phase='capture')
                           AND NOT EXISTS (SELECT 1 FROM native_workspace_claims c
                                            WHERE c.receipt=t.receipt AND c.attempt_id=t.attempt_id
                                              AND c.phase IN ('unknown','released'))))
                   ORDER BY j.available_at,j.id LIMIT ?""",
                (now, now, limit),
            ).fetchall()

    def _load_matching_after_if_present(
        self, store: ReviewStore, project: dict[str, Any], turn: TurnRecord
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not turn.after_ref:
            return None, "REVIEW_UNAVAILABLE"
        try:
            return store.load_turn_snapshot(turn.after_ref, project, kind="turn_after"), None
        except DevError as exc:
            if exc.code == "REVIEW_UNAVAILABLE" and exc.details.get("cause") == "REVIEW_NOT_FOUND":
                return None, None
            return None, exc.code

    def _check_capture_preconditions(
        self, conn, turn: TurnRecord, claim: JobClaim, now_ts: float
    ) -> str | None:
        session = conn.execute(
            "SELECT workspace_version FROM sessions WHERE id=?", (turn.session_id,)
        ).fetchone()
        if session is not None and int(session["workspace_version"]) > turn.workspace_version:
            with immediate(conn):
                self.turn_store.fail_capture_job_tx(
                    conn,
                    TurnKey(turn.receipt, turn.attempt_id),
                    job_id=claim.id,
                    fence=claim.fence,
                    error_code="CAPTURE_WINDOW_CLOSED",
                    claims=self.claims,
                    now=now_ts,
                )
            return "CAPTURE_WINDOW_CLOSED"
        with immediate(conn):
            rows = conn.execute(
                """SELECT phase FROM native_workspace_claims
                   WHERE receipt=? AND attempt_id=?""",
                (turn.receipt, turn.attempt_id),
            ).fetchall()
            phases = {row["phase"] for row in rows}
            owned = "capture" in phases and not (phases & {"unknown", "released"})
            drained = owned and self.claims.close_child_admission_tx(
                conn, turn.receipt, turn.attempt_id, now=now_ts
            )
        if not owned:
            self.turn_store.block_job(
                conn,
                claim.id,
                fence=claim.fence,
                reason="CAPTURE_BARRIER_UNVERIFIED",
                retry_after=5.0,
                now=now_ts,
            )
            return "CAPTURE_BARRIER_UNVERIFIED"
        if not drained:
            self.turn_store.block_job(
                conn,
                claim.id,
                fence=claim.fence,
                reason="CHILD_WRITERS_ACTIVE",
                retry_after=1.0,
                now=now_ts,
            )
            return "CHILD_WRITERS_ACTIVE"
        return None

    def _publish_after_snapshot(
        self,
        conn,
        engine,
        project: dict[str, Any],
        turn: TurnRecord,
        claim: JobClaim,
        now_ts: float,
    ) -> dict[str, Any] | None:
        store = ReviewStore(engine)
        after, error_code = self._load_matching_after_if_present(store, project, turn)
        if error_code is not None:
            with immediate(conn):
                self.turn_store.fail_capture_job_tx(
                    conn,
                    TurnKey(turn.receipt, turn.attempt_id),
                    job_id=claim.id,
                    fence=claim.fence,
                    error_code=error_code,
                    claims=self.claims,
                    now=now_ts,
                )
            return None
        if after is not None:
            return after
        try:
            captured = capture(engine, project)
            expected_binding = binding(engine, project)
            return store.save_turn_snapshot(
                captured,
                ref=turn.after_ref,
                receipt=turn.receipt,
                attempt_id=turn.attempt_id,
                role="after",
                expected_binding=expected_binding,
            )
        except DevError as exc:
            if exc.code == "REVIEW_QUOTA":
                self.turn_store.block_job(
                    conn,
                    claim.id,
                    fence=claim.fence,
                    reason="REVIEW_QUOTA",
                    retry_after=5.0,
                    now=now_ts,
                )
                return None
            with immediate(conn):
                self.turn_store.fail_capture_job_tx(
                    conn,
                    TurnKey(turn.receipt, turn.attempt_id),
                    job_id=claim.id,
                    fence=claim.fence,
                    error_code=exc.code,
                    claims=self.claims,
                    now=now_ts,
                )
            return None

    def run_capture_job(self, job_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Execute one capture_after job on its own SQLite connection."""
        now_ts = time.time() if now is None else now
        with closing(database(self.directory)) as conn:
            claim = self.turn_store.claim_job(
                conn,
                job_id,
                owner=self.owner,
                owner_epoch=self.owner_epoch,
                now=now_ts,
            )
            if claim is None:
                return None
            key = TurnKey(claim.receipt, claim.attempt_id)
            turn = self.turn_store.get_current_tx(conn, key)
            blocked = self._check_capture_preconditions(conn, turn, claim, now_ts)
            if blocked is not None:
                return {"status": "blocked", "reason": blocked}
            engine, project = self.engine_factory(turn.session_id)
            after = self._publish_after_snapshot(conn, engine, project, turn, claim, now_ts)
            if after is None:
                return None
            with immediate(conn):
                return self.turn_store.commit_frozen_evidence_tx(
                    conn,
                    key,
                    job_id=claim.id,
                    fence=claim.fence,
                    after_ref=turn.after_ref,
                    capture_started_at=after["started"],
                    capture_finished_at=after["finished"],
                    coverage=after["coverage"],
                    claims=self.claims,
                    now=now_ts,
                )

    def run_review_job(self, job_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Execute one build_review job strictly from frozen before/after snapshots."""
        now_ts = time.time() if now is None else now
        with closing(database(self.directory)) as conn:
            claim = self.turn_store.claim_job(
                conn,
                job_id,
                owner=self.owner,
                owner_epoch=self.owner_epoch,
                now=now_ts,
            )
            if claim is None:
                return None
            key = TurnKey(claim.receipt, claim.attempt_id)
            turn = self.turn_store.get_current_tx(conn, key)
            engine, project = self.engine_factory(turn.session_id)
            try:
                summary = freeze_review_from_snapshots(
                    engine,
                    project,
                    before_ref=turn.before_ref,
                    after_ref=turn.after_ref,
                    finalize=True,
                )
            except DevError as exc:
                with immediate(conn):
                    return self.turn_store.commit_review_result_tx(
                        conn,
                        key,
                        job_id=claim.id,
                        fence=claim.fence,
                        review_state="unavailable",
                        error_code=exc.code,
                        now=now_ts,
                    )
            with immediate(conn):
                return self.turn_store.commit_review_result_tx(
                    conn,
                    key,
                    job_id=claim.id,
                    fence=claim.fence,
                    review_state="ready",
                    review_ref=summary["review_ref"],
                    files_changed=summary["summary"]["files"],
                    summary=summary["summary"],
                    now=now_ts,
                )
