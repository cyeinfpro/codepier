"""Durable native turn lifecycle, job fencing, and recovery store."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time
from typing import Any, Literal, Mapping
import uuid

from agent.coding_reviews import MAX_PIN_LEASE_SECONDS, ReviewStore
from agent.workspace_ownership import WorkspaceOwnershipStore
from shared.native_cli import append_control_tx, identifier, immediate
from shared.native_contracts import (
    EvidenceFrozenBody,
    ProviderCompletedBody,
    ReviewReadyBody,
    canonical_jcs,
)
from shared.util import DevError

EXECUTION_STATES = frozenset(
    {"preparing", "running", "completed", "failed", "interrupted", "uncertain", "cancelled"}
)
SEND_PHASES = frozenset({"not_sent", "write_started", "accepted", "uncertain"})
EVIDENCE_STATES = frozenset(
    {
        "baseline_pending",
        "baseline_ready",
        "baseline_unavailable",
        "capture_pending",
        "capturing",
        "frozen",
        "partial",
        "unavailable",
        "not_requested",
    }
)
REVIEW_STATES = frozenset(
    {"pending", "building", "ready", "unavailable", "not_requested", "cancelled"}
)
LEGACY_COMMAND_STATES = {
    "completed": "completed",
    "failed": "error",
    "interrupted": "interrupted",
    "uncertain": "uncertain",
    "cancelled": "cancelled",
}


class StaleOwnerError(DevError):
    """Raised when an owner_epoch or job fence no longer matches the persisted row."""


class StateConflictError(DevError):
    """Raised when a CAS update fails on state_version or terminal state."""


@dataclass(frozen=True)
class TurnKey:
    receipt: str
    attempt_id: str


@dataclass(frozen=True)
class TurnRecord:
    receipt: str
    attempt_id: str
    session_id: str
    current_attempt: int
    retry_of_receipt: str | None
    retry_of_attempt: str | None
    provider_instance_id: str
    native_thread_id: str | None
    native_turn_id: str | None
    workspace_key: str
    workspace_version: int
    owner_epoch: int
    state_version: int
    send_phase: str
    execution_state: str
    evidence_state: str
    review_state: str
    before_ref: str | None
    after_ref: str | None
    review_ref: str | None
    terminal_event_id: str | None
    terminal_at: float | None
    capture_started_at: float | None
    capture_finished_at: float | None
    coverage_json: str | None
    error_code: str | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TurnRecord:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class JobClaim:
    id: str
    receipt: str
    attempt_id: str
    kind: str
    fence: int
    owner: str
    owner_epoch: int
    attempts: int


def _validate_hex32_opt(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return identifier(value)


def _require_tx(conn: sqlite3.Connection, name: str) -> None:
    if not conn.in_transaction:
        raise RuntimeError(f"{name} requires an active SQLite transaction")


class NativeTurnStore:
    """Transactional state store for native turns, review jobs, and recovery."""

    def get_current_tx(
        self, conn: sqlite3.Connection, key: TurnKey | str
    ) -> TurnRecord:
        if isinstance(key, TurnKey):
            row = conn.execute(
                "SELECT * FROM native_turns WHERE receipt=? AND attempt_id=?",
                (identifier(key.receipt), key.attempt_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM native_turns WHERE receipt=? AND current_attempt=1",
                (identifier(key),),
            ).fetchone()
        if row is None:
            raise DevError("TURN_NOT_FOUND", "原生轮次记录不存在", 404)
        return TurnRecord.from_row(row)

    def require_owner_tx(self, turn: TurnRecord, owner_epoch: int) -> None:
        if turn.owner_epoch != owner_epoch:
            raise StaleOwnerError(
                "STALE_OWNER_EPOCH",
                "执行所有权世代已过期，拒绝迟到写入",
                409,
                expected_epoch=turn.owner_epoch,
                actual_epoch=owner_epoch,
            )

    def begin_turn_tx(
        self,
        conn: sqlite3.Connection,
        *,
        receipt: str,
        attempt_id: str,
        session_id: str,
        provider_instance_id: str,
        workspace_key: str,
        workspace_version: int,
        owner_epoch: int,
        before_ref: str | None = None,
        after_ref: str | None = None,
        evidence_requested: bool = True,
        retry_of_receipt: str | None = None,
        retry_of_attempt: str | None = None,
        now: float | None = None,
    ) -> TurnRecord:
        """Persist a new native turn attempt with pre-allocated fixed evidence refs."""
        _require_tx(conn, "begin_turn_tx")
        receipt = identifier(receipt)
        session_id = identifier(session_id)
        if not attempt_id:
            raise ValueError("attempt_id is required")
        existing = conn.execute(
            "SELECT * FROM native_turns WHERE receipt=? AND attempt_id=?",
            (receipt, attempt_id),
        ).fetchone()
        if existing is not None:
            record = TurnRecord.from_row(existing)
            if record.session_id != session_id or record.workspace_key != workspace_key:
                raise StateConflictError("TURN_CONFLICT", "轮次标识已被冲突上下文占用", 409)
            return record
        prior_current = conn.execute(
            "SELECT * FROM native_turns WHERE receipt=? AND current_attempt=1",
            (receipt,),
        ).fetchone()
        if prior_current is not None:
            raise StateConflictError(
                "RECEIPT_ALREADY_ACTIVE",
                "同一收据已存在执行记录；新执行必须使用新收据并关联 retry_of_receipt",
                409,
            )
        now_ts = time.time() if now is None else now
        b_ref = _validate_hex32_opt(before_ref, "before_ref") or (
            uuid.uuid4().hex if evidence_requested else None
        )
        a_ref = _validate_hex32_opt(after_ref, "after_ref") or (
            uuid.uuid4().hex if evidence_requested else None
        )
        evidence_state = "baseline_pending" if evidence_requested else "not_requested"
        review_state = "pending" if evidence_requested else "not_requested"
        conn.execute(
            """INSERT INTO native_turns(
                 receipt, attempt_id, session_id, current_attempt,
                 retry_of_receipt, retry_of_attempt, provider_instance_id,
                 workspace_key, workspace_version, owner_epoch, state_version,
                 send_phase, execution_state, evidence_state, review_state,
                 before_ref, after_ref, created_at, updated_at
               ) VALUES (?,?,?,1,?,?,?,?,?,?,0,'not_sent','preparing',?,?,?,?,?,?)""",
            (
                receipt,
                attempt_id,
                session_id,
                _validate_hex32_opt(retry_of_receipt, "retry_of_receipt"),
                retry_of_attempt,
                provider_instance_id,
                workspace_key,
                workspace_version,
                owner_epoch,
                evidence_state,
                review_state,
                b_ref,
                a_ref,
                now_ts,
                now_ts,
            ),
        )
        return self.get_current_tx(conn, TurnKey(receipt, attempt_id))

    def record_baseline_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        owner_epoch: int,
        expected_state_version: int,
        status: Literal["baseline_ready", "baseline_unavailable"],
        before_ref: str | None = None,
        error_code: str | None = None,
        now: float | None = None,
    ) -> TurnRecord:
        """Persist baseline capture outcome before any prompt bytes may be sent."""
        _require_tx(conn, "record_baseline_tx")
        if status not in ("baseline_ready", "baseline_unavailable"):
            raise ValueError(f"Unsupported baseline status: {status}")
        now_ts = time.time() if now is None else now
        b_ref = _validate_hex32_opt(before_ref, "before_ref")
        review_state = "pending" if status == "baseline_ready" else "unavailable"
        cursor = conn.execute(
            """UPDATE native_turns
               SET evidence_state=?,
                   review_state=?,
                   before_ref=COALESCE(?, before_ref),
                   error_code=?,
                   state_version=state_version+1,
                   updated_at=?
               WHERE receipt=? AND attempt_id=? AND owner_epoch=?
                 AND state_version=? AND send_phase='not_sent'""",
            (
                status,
                review_state,
                b_ref,
                error_code,
                now_ts,
                identifier(key.receipt),
                key.attempt_id,
                owner_epoch,
                expected_state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflictError("BASELINE_CAS_FAILED", "基线状态更新版本冲突", 409)
        return self.get_current_tx(conn, key)

    def mark_write_started_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        owner_epoch: int,
        expected_state_version: int,
        now: float | None = None,
    ) -> TurnRecord:
        """Transition send_phase to write_started only after baseline preparation settled."""
        _require_tx(conn, "mark_write_started_tx")
        turn = self.get_current_tx(conn, key)
        self.require_owner_tx(turn, owner_epoch)
        if turn.evidence_state == "baseline_pending":
            raise StateConflictError(
                "BASELINE_NOT_COMMITTED",
                "基线准备尚未持久化完成，禁止写出 prompt 字节",
                409,
            )
        if turn.send_phase != "not_sent" or turn.execution_state not in ("preparing", "running"):
            raise StateConflictError(
                "INVALID_SEND_TRANSITION",
                f"当前状态 {turn.execution_state}/{turn.send_phase} 不允许启动发送",
                409,
            )
        now_ts = time.time() if now is None else now
        cursor = conn.execute(
            """UPDATE native_turns
               SET send_phase='write_started', execution_state='running',
                   state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND owner_epoch=?
                 AND state_version=? AND send_phase='not_sent'""",
            (now_ts, turn.receipt, turn.attempt_id, owner_epoch, expected_state_version),
        )
        if cursor.rowcount != 1:
            raise StateConflictError("WRITE_STARTED_CAS_FAILED", "发送阶段状态更新冲突", 409)
        return self.get_current_tx(conn, key)

    def mark_accepted_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        owner_epoch: int,
        expected_state_version: int,
        native_thread_id: str | None = None,
        native_turn_id: str | None = None,
        now: float | None = None,
    ) -> TurnRecord:
        """Record explicit provider acceptance confirmation."""
        _require_tx(conn, "mark_accepted_tx")
        now_ts = time.time() if now is None else now
        cursor = conn.execute(
            """UPDATE native_turns
               SET send_phase='accepted',
                   execution_state='running',
                   native_thread_id=COALESCE(?, native_thread_id),
                   native_turn_id=COALESCE(?, native_turn_id),
                   state_version=state_version+1,
                   updated_at=?
               WHERE receipt=? AND attempt_id=? AND owner_epoch=?
                 AND state_version=? AND send_phase IN ('write_started','accepted')""",
            (
                native_thread_id,
                native_turn_id,
                now_ts,
                identifier(key.receipt),
                key.attempt_id,
                owner_epoch,
                expected_state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflictError("ACCEPTED_CAS_FAILED", "确认接收阶段状态更新冲突", 409)
        return self.get_current_tx(conn, key)

    def update_legacy_command_tx(
        self, conn: sqlite3.Connection, receipt: str, outcome: str
    ) -> None:
        """Transition legacy commands row from active state without overwriting terminal receipts."""
        _require_tx(conn, "update_legacy_command_tx")
        receipt = identifier(receipt)
        target_state = LEGACY_COMMAND_STATES.get(outcome)
        if target_state is None:
            raise ValueError(f"Unsupported legacy outcome: {outcome}")
        row = conn.execute("SELECT state FROM commands WHERE id=?", (receipt,)).fetchone()
        if row is None:
            return
        if row["state"] == target_state:
            return
        if row["state"] not in ("queued", "claimed"):
            raise StateConflictError(
                "LEGACY_COMMAND_TERMINAL",
                f"旧命令 {receipt} 已处于终态 {row['state']}",
                409,
            )
        conn.execute("UPDATE commands SET state=? WHERE id=?", (target_state, receipt))

    def enqueue_job_tx(
        self,
        conn: sqlite3.Connection,
        turn: TurnRecord,
        *,
        kind: Literal["capture_after", "build_review"],
        job_id: str,
        now: float,
        recovery_deadline: float | None = None,
    ) -> str:
        """Idempotently enqueue a narrow capture_after or build_review job."""
        _require_tx(conn, "enqueue_job_tx")
        if kind not in ("capture_after", "build_review"):
            raise ValueError(f"Invalid job kind: {kind}")
        conn.execute(
            """INSERT OR IGNORE INTO native_review_jobs(
                 id, receipt, attempt_id, kind, status, fence,
                 available_at, recovery_deadline, created_at, updated_at
               ) VALUES (?,?,?,?,'pending',0,?,?,?,?)""",
            (
                job_id,
                turn.receipt,
                turn.attempt_id,
                kind,
                now,
                recovery_deadline,
                now,
                now,
            ),
        )
        return job_id

    def _existing_terminal(self, conn: sqlite3.Connection, turn: TurnRecord) -> dict[str, Any]:
        row = conn.execute(
            "SELECT spool_start, spool_end FROM native_control_events WHERE event_id=?",
            (turn.terminal_event_id,),
        ).fetchone()
        return {
            "event_id": turn.terminal_event_id,
            "execution_state": turn.execution_state,
            "evidence_state": turn.evidence_state,
            "spool_range": (int(row["spool_start"]), int(row["spool_end"])) if row else None,
        }

    def persist_provider_terminal(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        outcome: Literal["completed", "failed", "interrupted", "uncertain"],
        *,
        owner_epoch: int,
        claims: WorkspaceOwnershipStore,
        now: float | None = None,
        error_code: str | None = None,
        wire_event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically commit provider outcome, legacy command state, barrier transition, job, and control event."""
        if outcome not in ("completed", "failed", "interrupted", "uncertain"):
            raise ValueError(f"Invalid terminal outcome: {outcome}")
        now_ts = time.time() if now is None else now
        with immediate(conn):
            turn = self.get_current_tx(conn, key)
            self.require_owner_tx(turn, owner_epoch)
            if turn.terminal_event_id is not None:
                return self._existing_terminal(conn, turn)

            event_id = f"provider-terminal:{turn.receipt}:{turn.attempt_id}"
            needs_capture = turn.evidence_state == "baseline_ready"
            evidence = (
                "capture_pending"
                if needs_capture
                else "not_requested"
                if turn.evidence_state == "not_requested"
                else "unavailable"
            )
            cursor = conn.execute(
                """UPDATE native_turns
                   SET execution_state=?,
                       evidence_state=?,
                       terminal_event_id=?,
                       terminal_at=?,
                       error_code=COALESCE(?, error_code),
                       state_version=state_version+1,
                       updated_at=?
                   WHERE receipt=? AND attempt_id=? AND owner_epoch=?
                     AND state_version=? AND terminal_event_id IS NULL""",
                (
                    outcome,
                    evidence,
                    event_id,
                    now_ts,
                    error_code,
                    now_ts,
                    turn.receipt,
                    turn.attempt_id,
                    owner_epoch,
                    turn.state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("TERMINAL_CAS_FAILED", "终态提交版本冲突", 409)
            self.update_legacy_command_tx(conn, turn.receipt, outcome)
            if needs_capture:
                claims.transition_tx(
                    conn,
                    turn,
                    source="execute",
                    target="capture",
                    owner_epoch=owner_epoch,
                    now=now_ts,
                )
                self.enqueue_job_tx(
                    conn,
                    turn,
                    kind="capture_after",
                    job_id=f"capture:{turn.receipt}:{turn.attempt_id}",
                    now=now_ts,
                )
            else:
                claims.release_or_drain_tx(conn, turn, now=now_ts)

            body = ProviderCompletedBody(
                receipt=turn.receipt,
                attempt_id=turn.attempt_id,
                execution_state=outcome,
                evidence_state=evidence,
                error_code=error_code or turn.error_code,
            ).model_dump()
            spool_range = append_control_tx(
                conn,
                event_id=event_id,
                session_id=turn.session_id,
                receipt=turn.receipt,
                attempt_id=turn.attempt_id,
                worker_epoch=owner_epoch,
                kind="provider_completed",
                payload=body,
                wire_event=wire_event,
                now=now_ts,
            )
        return {
            "event_id": event_id,
            "execution_state": outcome,
            "evidence_state": evidence,
            "spool_range": spool_range,
        }

    def claim_job(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        owner: str,
        owner_epoch: int,
        lease_seconds: float = 30.0,
        now: float | None = None,
    ) -> JobClaim | None:
        """Atomically claim a claimable job and increment its fence."""
        now_ts = time.time() if now is None else now
        with immediate(conn):
            row = conn.execute(
                "SELECT * FROM native_review_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending" and float(row["available_at"]) <= now_ts:
                claimable = True
            elif (
                row["status"] == "blocked"
                and float(row["available_at"]) <= now_ts
                and (row["recovery_deadline"] is None or float(row["recovery_deadline"]) > now_ts)
            ):
                claimable = True
            else:
                claimable = False
            if not claimable:
                return None
            new_fence = int(row["fence"]) + 1
            cursor = conn.execute(
                """UPDATE native_review_jobs
                   SET status='running', fence=?, owner=?, owner_epoch=?,
                       lease_until=?, attempts=attempts+1, claimed_at=?, updated_at=?
                   WHERE id=? AND fence=? AND status IN ('pending','blocked')""",
                (
                    new_fence,
                    owner,
                    owner_epoch,
                    now_ts + lease_seconds,
                    now_ts,
                    now_ts,
                    job_id,
                    row["fence"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM native_review_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return JobClaim(
                id=updated["id"],
                receipt=updated["receipt"],
                attempt_id=updated["attempt_id"],
                kind=updated["kind"],
                fence=int(updated["fence"]),
                owner=updated["owner"],
                owner_epoch=int(updated["owner_epoch"]),
                attempts=int(updated["attempts"]),
            )

    def renew_job_lease(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        fence: int,
        lease_seconds: float = 30.0,
        now: float | None = None,
    ) -> bool:
        """Renew an active job lease matching the exact fence."""
        now_ts = time.time() if now is None else now
        with immediate(conn):
            cursor = conn.execute(
                """UPDATE native_review_jobs
                   SET lease_until=?, updated_at=?
                   WHERE id=? AND fence=? AND status='running'""",
                (now_ts + lease_seconds, now_ts, job_id, fence),
            )
            return cursor.rowcount == 1

    def block_job(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        fence: int,
        reason: str,
        retry_after: float = 5.0,
        recovery_deadline: float | None = None,
        now: float | None = None,
    ) -> bool:
        """Transition a running job to blocked while preserving barrier and recovery window."""
        now_ts = time.time() if now is None else now
        with immediate(conn):
            cursor = conn.execute(
                """UPDATE native_review_jobs
                   SET status='blocked', blocked_reason=?, last_error=?,
                       available_at=?, recovery_deadline=COALESCE(recovery_deadline, ?),
                       updated_at=?
                   WHERE id=? AND fence=? AND status='running'""",
                (
                    reason,
                    reason,
                    now_ts + retry_after,
                    recovery_deadline,
                    now_ts,
                    job_id,
                    fence,
                ),
            )
            return cursor.rowcount == 1

    def reclaim_stale_job_tx(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        now: float | None = None,
        holder_alive: bool = False,
    ) -> bool:
        """Return an expired running job to pending only if the prior holder is dead."""
        _require_tx(conn, "reclaim_stale_job_tx")
        if holder_alive:
            return False
        now_ts = time.time() if now is None else now
        cursor = conn.execute(
            """UPDATE native_review_jobs
               SET status='pending', available_at=?, updated_at=?
               WHERE id=? AND status='running' AND lease_until IS NOT NULL AND lease_until <= ?""",
            (now_ts, now_ts, job_id, now_ts),
        )
        return cursor.rowcount == 1

    def commit_frozen_evidence_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        job_id: str,
        fence: int,
        after_ref: str,
        capture_started_at: float,
        capture_finished_at: float,
        coverage: Mapping[str, Any],
        claims: WorkspaceOwnershipStore,
        now: float | None = None,
        background_writers_unknown: bool = False,
    ) -> dict[str, Any]:
        """Atomically persist frozen after evidence, release barrier, enqueue review, and emit event."""
        _require_tx(conn, "commit_frozen_evidence_tx")
        now_ts = time.time() if now is None else now
        after_ref = identifier(after_ref)
        turn = self.get_current_tx(conn, key)
        job_cursor = conn.execute(
            """UPDATE native_review_jobs SET status='succeeded', updated_at=?
               WHERE id=? AND receipt=? AND attempt_id=? AND kind='capture_after'
                 AND fence=? AND status='running'""",
            (now_ts, job_id, turn.receipt, turn.attempt_id, fence),
        )
        if job_cursor.rowcount != 1:
            raise StaleOwnerError("STALE_JOB_FENCE", "采集任务 fence 已过期", 409)

        complete = bool(coverage.get("complete")) and not background_writers_unknown
        evidence_state: Literal["frozen", "partial"] = "frozen" if complete else "partial"
        coverage_dict = dict(coverage)
        if background_writers_unknown:
            coverage_dict["background_writers_unknown"] = True
        conn.execute(
            """UPDATE native_turns
               SET after_ref=?, capture_started_at=?, capture_finished_at=?,
                   coverage_json=?, evidence_state=?,
                   state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND state_version=?""",
            (
                after_ref,
                capture_started_at,
                capture_finished_at,
                canonical_jcs(coverage_dict),
                evidence_state,
                now_ts,
                turn.receipt,
                turn.attempt_id,
                turn.state_version,
            ),
        )
        claims.release_or_drain_tx(conn, turn, now=now_ts)
        review_job_id = self.enqueue_job_tx(
            conn,
            turn,
            kind="build_review",
            job_id=f"review:{turn.receipt}:{turn.attempt_id}",
            now=now_ts,
        )
        event_id = f"evidence-frozen:{turn.receipt}:{turn.attempt_id}"
        body = EvidenceFrozenBody(
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            evidence_state=evidence_state,
            after_ref=after_ref,
            before_ref=turn.before_ref,
            capture_started_at=capture_started_at,
            capture_finished_at=capture_finished_at,
            coverage=coverage_dict,
        ).model_dump()
        spool_range = append_control_tx(
            conn,
            event_id=event_id,
            session_id=turn.session_id,
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            worker_epoch=turn.owner_epoch,
            kind="evidence_frozen",
            payload=body,
            now=now_ts,
        )
        return {
            "event_id": event_id,
            "evidence_state": evidence_state,
            "after_ref": after_ref,
            "review_job_id": review_job_id,
            "spool_range": spool_range,
        }

    def fail_capture_job_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        job_id: str,
        fence: int,
        error_code: str,
        claims: WorkspaceOwnershipStore,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Persist terminal capture failure and release barrier only after writer checks."""
        _require_tx(conn, "fail_capture_job_tx")
        now_ts = time.time() if now is None else now
        turn = self.get_current_tx(conn, key)
        job_cursor = conn.execute(
            """UPDATE native_review_jobs SET status='failed', last_error=?, updated_at=?
               WHERE id=? AND receipt=? AND attempt_id=? AND kind='capture_after'
                 AND fence=? AND status='running'""",
            (error_code, now_ts, job_id, turn.receipt, turn.attempt_id, fence),
        )
        if job_cursor.rowcount != 1:
            raise StaleOwnerError("STALE_JOB_FENCE", "采集任务 fence 已过期", 409)
        conn.execute(
            """UPDATE native_turns
               SET evidence_state='unavailable', review_state='unavailable',
                   error_code=?, state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND state_version=?""",
            (error_code, now_ts, turn.receipt, turn.attempt_id, turn.state_version),
        )
        drain_status = claims.release_or_drain_tx(conn, turn, now=now_ts)
        event_id = f"evidence-frozen:{turn.receipt}:{turn.attempt_id}"
        body = EvidenceFrozenBody(
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            evidence_state="unavailable",
            before_ref=turn.before_ref,
            error_code=error_code,
        ).model_dump()
        spool_range = append_control_tx(
            conn,
            event_id=event_id,
            session_id=turn.session_id,
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            worker_epoch=turn.owner_epoch,
            kind="evidence_frozen",
            payload=body,
            now=now_ts,
        )
        return {
            "event_id": event_id,
            "evidence_state": "unavailable",
            "drain_status": drain_status,
            "spool_range": spool_range,
        }

    def commit_review_result_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        job_id: str,
        fence: int,
        review_state: Literal["ready", "unavailable"],
        review_ref: str | None = None,
        files_changed: int | None = None,
        summary: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Commit offline build_review result outside the workspace barrier."""
        _require_tx(conn, "commit_review_result_tx")
        if review_state not in ("ready", "unavailable"):
            raise ValueError(f"Unsupported review_state: {review_state}")
        now_ts = time.time() if now is None else now
        turn = self.get_current_tx(conn, key)
        job_status = "succeeded" if review_state == "ready" else "failed"
        job_cursor = conn.execute(
            """UPDATE native_review_jobs
               SET status=?, last_error=?, updated_at=?
               WHERE id=? AND receipt=? AND attempt_id=? AND kind='build_review'
                 AND fence=? AND status='running'""",
            (job_status, error_code, now_ts, job_id, turn.receipt, turn.attempt_id, fence),
        )
        if job_cursor.rowcount != 1:
            raise StaleOwnerError("STALE_JOB_FENCE", "审阅构建任务 fence 已过期", 409)
        r_ref = _validate_hex32_opt(review_ref, "review_ref")
        conn.execute(
            """UPDATE native_turns
               SET review_state=?, review_ref=?, error_code=COALESCE(?, error_code),
                   state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND state_version=?""",
            (
                review_state,
                r_ref,
                error_code,
                now_ts,
                turn.receipt,
                turn.attempt_id,
                turn.state_version,
            ),
        )
        event_id = f"review-ready:{turn.receipt}:{turn.attempt_id}"
        body = ReviewReadyBody(
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            review_state=review_state,
            review_ref=r_ref,
            files_changed=files_changed,
            summary=dict(summary or {}),
            error_code=error_code,
        ).model_dump()
        spool_range = append_control_tx(
            conn,
            event_id=event_id,
            session_id=turn.session_id,
            receipt=turn.receipt,
            attempt_id=turn.attempt_id,
            worker_epoch=turn.owner_epoch,
            kind="review_ready",
            payload=body,
            now=now_ts,
        )
        return {
            "event_id": event_id,
            "review_state": review_state,
            "review_ref": r_ref,
            "spool_range": spool_range,
        }

    def cancel_queued_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        owner_epoch: int,
        now: float | None = None,
    ) -> bool:
        """Tier 1: cancel only if preparing and send_phase == not_sent."""
        _require_tx(conn, "cancel_queued_tx")
        now_ts = time.time() if now is None else now
        cursor = conn.execute(
            """UPDATE native_turns
               SET execution_state='cancelled',
                   evidence_state=CASE
                     WHEN evidence_state='not_requested' THEN 'not_requested'
                     ELSE 'unavailable'
                   END,
                   review_state=CASE
                     WHEN review_state='not_requested' THEN 'not_requested'
                     ELSE 'cancelled'
                   END,
                   error_code='CANCELLED_QUEUED',
                   state_version=state_version+1,
                   updated_at=?
               WHERE receipt=? AND attempt_id=? AND owner_epoch=?
                 AND execution_state='preparing' AND send_phase='not_sent'""",
            (now_ts, identifier(key.receipt), key.attempt_id, owner_epoch),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "UPDATE commands SET state='cancelled' WHERE id=? AND state='queued'",
            (identifier(key.receipt),),
        )
        return True

    def mark_interrupt_requested_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        owner_epoch: int,
        expected_native_turn_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Tier 2: record interrupt request for active attempt without faking terminal stop."""
        _require_tx(conn, "mark_interrupt_requested_tx")
        turn = self.get_current_tx(conn, key)
        if turn.owner_epoch != owner_epoch:
            return False
        if expected_native_turn_id is not None and turn.native_turn_id != expected_native_turn_id:
            return False
        if turn.execution_state not in ("running", "preparing") or turn.send_phase == "not_sent":
            return False
        now_ts = time.time() if now is None else now
        cursor = conn.execute(
            """UPDATE native_turns
               SET error_code='INTERRUPT_REQUESTED',
                   state_version=state_version+1,
                   updated_at=?
               WHERE receipt=? AND attempt_id=? AND owner_epoch=? AND state_version=?""",
            (now_ts, turn.receipt, turn.attempt_id, owner_epoch, turn.state_version),
        )
        return cursor.rowcount == 1

    def cancel_review_job_tx(
        self,
        conn: sqlite3.Connection,
        key: TurnKey,
        *,
        now: float | None = None,
    ) -> bool:
        """Tier 3: cancel only build_review without altering execution_state or deleting evidence."""
        _require_tx(conn, "cancel_review_job_tx")
        now_ts = time.time() if now is None else now
        turn = self.get_current_tx(conn, key)
        cursor = conn.execute(
            """UPDATE native_review_jobs
               SET status='cancelled', updated_at=?
               WHERE receipt=? AND attempt_id=? AND kind='build_review'
                 AND status IN ('pending','blocked')""",
            (now_ts, turn.receipt, turn.attempt_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            """UPDATE native_turns
               SET review_state='cancelled', state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND state_version=?""",
            (now_ts, turn.receipt, turn.attempt_id, turn.state_version),
        )
        return True

    def _recover_before_publish(
        self,
        conn: sqlite3.Connection,
        turn: TurnRecord,
        *,
        store: ReviewStore | None,
        project: dict[str, Any] | None,
        now_ts: float,
    ) -> dict[str, Any]:
        if store is not None and project is not None and turn.before_ref:
            try:
                store.load_turn_snapshot(turn.before_ref, project, kind="baseline")
                conn.execute(
                    """UPDATE native_turns
                       SET evidence_state='baseline_ready', state_version=state_version+1, updated_at=?
                       WHERE receipt=? AND attempt_id=? AND state_version=?""",
                    (now_ts, turn.receipt, turn.attempt_id, turn.state_version),
                )
                return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "reconciled_before_ref"}
            except DevError:
                pass
        return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "keep_baseline_pending"}

    def _recover_write_started(
        self, conn: sqlite3.Connection, turn: TurnRecord, now_ts: float
    ) -> dict[str, Any]:
        conn.execute(
            """UPDATE native_turns
               SET send_phase='uncertain', execution_state='uncertain',
                   error_code='UNCERTAIN_SEND', state_version=state_version+1, updated_at=?
               WHERE receipt=? AND attempt_id=? AND state_version=?""",
            (now_ts, turn.receipt, turn.attempt_id, turn.state_version),
        )
        self.update_legacy_command_tx(conn, turn.receipt, "uncertain")
        return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "marked_uncertain"}

    def _recover_pending_capture(
        self,
        conn: sqlite3.Connection,
        turn: TurnRecord,
        *,
        store: ReviewStore | None,
        project: dict[str, Any] | None,
        claims: WorkspaceOwnershipStore | None,
        current_workspace_version: int | None,
        now_ts: float,
        max_pin_lease_seconds: float,
    ) -> dict[str, Any]:
        lease_expired = (now_ts - turn.created_at) > max_pin_lease_seconds
        version_advanced = (
            current_workspace_version is not None
            and current_workspace_version > turn.workspace_version
        )
        job = conn.execute(
            """SELECT * FROM native_review_jobs
               WHERE receipt=? AND attempt_id=? AND kind='capture_after'""",
            (turn.receipt, turn.attempt_id),
        ).fetchone()
        # Absence of a session worker cannot prove the review worker is dead:
        # the Agent process may own this job. Never replace a running fence
        # without independent holder verification.
        if job is not None and job["status"] == "running":
            return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "capture_holder_unverified"}
        if claims is not None:
            phases = {
                row["phase"]
                for row in conn.execute(
                    "SELECT phase FROM native_workspace_claims WHERE receipt=? AND attempt_id=?",
                    (turn.receipt, turn.attempt_id),
                )
            }
            if "capture" not in phases or phases & {"unknown", "released"}:
                return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "capture_barrier_unverified"}
        if store is not None and project is not None and turn.after_ref:
            try:
                after = store.load_turn_snapshot(turn.after_ref, project, kind="turn_after")
                if job is not None and job["status"] in ("pending", "blocked") and claims is not None:
                    new_fence = int(job["fence"]) + 1
                    conn.execute(
                        "UPDATE native_review_jobs SET status='running', fence=? WHERE id=?",
                        (new_fence, job["id"]),
                    )
                    self.commit_frozen_evidence_tx(
                        conn,
                        TurnKey(turn.receipt, turn.attempt_id),
                        job_id=job["id"],
                        fence=new_fence,
                        after_ref=turn.after_ref,
                        capture_started_at=after["started"],
                        capture_finished_at=after["finished"],
                        coverage=after["coverage"],
                        claims=claims,
                        now=now_ts,
                    )
                    return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "committed_published_after"}
            except DevError:
                pass

        if lease_expired or version_advanced:
            reason = "PIN_LEASE_EXPIRED" if lease_expired else "CAPTURE_WINDOW_CLOSED"
            conn.execute(
                """UPDATE native_turns
                   SET evidence_state='unavailable', review_state='unavailable',
                       error_code=?, state_version=state_version+1, updated_at=?
                   WHERE receipt=? AND attempt_id=? AND state_version=?""",
                (reason, now_ts, turn.receipt, turn.attempt_id, turn.state_version),
            )
            conn.execute(
                """UPDATE native_review_jobs SET status='failed', last_error=?, updated_at=?
                   WHERE receipt=? AND attempt_id=? AND kind='capture_after'
                     AND status IN ('pending','running','blocked')""",
                (reason, now_ts, turn.receipt, turn.attempt_id),
            )
            if claims is not None:
                claims.release_or_drain_tx(conn, turn, now=now_ts)
            return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "expired_capture_window"}

        return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "resume_capture_job"}

    def _recover_one_turn(
        self,
        conn: sqlite3.Connection,
        turn: TurnRecord,
        *,
        store: ReviewStore | None,
        project: dict[str, Any] | None,
        claims: WorkspaceOwnershipStore | None,
        current_workspace_version: int | None,
        now_ts: float,
        max_pin_lease_seconds: float,
    ) -> dict[str, Any] | None:
        if turn.execution_state == "preparing" and turn.send_phase == "not_sent" and turn.evidence_state == "baseline_pending":
            return self._recover_before_publish(conn, turn, store=store, project=project, now_ts=now_ts)
        if turn.send_phase == "write_started" and turn.terminal_event_id is None:
            return self._recover_write_started(conn, turn, now_ts)
        if turn.evidence_state in ("capture_pending", "capturing"):
            return self._recover_pending_capture(
                conn,
                turn,
                store=store,
                project=project,
                claims=claims,
                current_workspace_version=current_workspace_version,
                now_ts=now_ts,
                max_pin_lease_seconds=max_pin_lease_seconds,
            )
        if turn.evidence_state == "baseline_ready" and (now_ts - turn.created_at) > max_pin_lease_seconds:
            conn.execute(
                """UPDATE native_turns
                   SET evidence_state='unavailable', review_state='unavailable',
                       error_code='PIN_LEASE_EXPIRED', state_version=state_version+1, updated_at=?
                   WHERE receipt=? AND attempt_id=? AND state_version=?""",
                (now_ts, turn.receipt, turn.attempt_id, turn.state_version),
            )
            if claims is not None:
                claims.release_or_drain_tx(conn, turn, now=now_ts)
            return {"receipt": turn.receipt, "attempt_id": turn.attempt_id, "action": "expired_baseline_pin"}
        return None

    def recover_session_turns(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        store: ReviewStore | None = None,
        project: dict[str, Any] | None = None,
        claims: WorkspaceOwnershipStore | None = None,
        current_workspace_version: int | None = None,
        now: float | None = None,
        max_pin_lease_seconds: float = MAX_PIN_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Reconcile interrupted session turns per B3.2 without replaying uncertain sends."""
        session_id = identifier(session_id)
        now_ts = time.time() if now is None else now
        actions: list[dict[str, Any]] = []
        with immediate(conn):
            rows = conn.execute(
                "SELECT * FROM native_turns WHERE session_id=? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            for row in rows:
                result = self._recover_one_turn(
                    conn,
                    TurnRecord.from_row(row),
                    store=store,
                    project=project,
                    claims=claims,
                    current_workspace_version=current_workspace_version,
                    now_ts=now_ts,
                    max_pin_lease_seconds=max_pin_lease_seconds,
                )
                if result is not None:
                    actions.append(result)
        return actions
