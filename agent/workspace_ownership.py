"""Durable workspace ownership and child-writer drain gate backed by native.sqlite3."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Sequence

from agent.resource_queue import Claim, ResourceQueue, canonical_path
from shared.native_cli import identifier, immediate, process_exists, worker_present
from shared.util import DevError

LIVE_CLAIM_PHASES = ("baseline", "execute", "capture", "restore", "unknown")
STRICT_BARRIER_PHASES = frozenset({"capture", "restore", "unknown"})


class WorkspaceConflictError(DevError):
    """Raised when a workspace claim conflicts with an active or unknown durable claim."""


def _turn_identity(turn_or_key: Any) -> tuple[str, str]:
    if isinstance(turn_or_key, tuple) and len(turn_or_key) == 2:
        return identifier(turn_or_key[0]), str(turn_or_key[1])
    if isinstance(turn_or_key, Mapping):
        return identifier(turn_or_key["receipt"]), str(turn_or_key["attempt_id"])
    return identifier(turn_or_key.receipt), str(turn_or_key.attempt_id)


def _normalize_claim(claim: Claim) -> Claim:
    name = claim.name
    if claim.kind == "path" and claim.namespace == "agent":
        name = canonical_path(name)
    return Claim(
        namespace=claim.namespace,
        kind=claim.kind,
        name=name,
        write=claim.write,
        holder=claim.holder,
        stable=claim.stable,
        strict_barrier=claim.strict_barrier,
    )


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        namespace=row["namespace"],
        kind=row["resource_kind"],
        name=row["canonical_name"],
        write=bool(row["write_mode"]),
        holder=row["holder_kind"],
        stable=bool(row["stable"]),
        strict_barrier=row["phase"] in STRICT_BARRIER_PHASES,
    )


def _serialize_identity(identity: str | Mapping[str, Any]) -> str:
    if isinstance(identity, Mapping):
        return json.dumps(dict(identity), ensure_ascii=False, separators=(",", ":"))
    return str(identity or "")


def _parse_identity(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


class WorkspaceOwnershipStore:
    """Durable mirror and admission boundary for Claim-based workspace ownership."""

    def active_claims_tx(
        self, conn: sqlite3.Connection, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all live or unknown durable workspace claims."""
        if namespace is None:
            rows = conn.execute(
                """SELECT * FROM native_workspace_claims
                   WHERE phase IN ('baseline','execute','capture','restore','unknown')
                   ORDER BY updated_at ASC, claim_id ASC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM native_workspace_claims
                   WHERE namespace=? AND phase IN ('baseline','execute','capture','restore','unknown')
                   ORDER BY updated_at ASC, claim_id ASC""",
                (namespace,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _find_conflicts(
        self,
        conn: sqlite3.Connection,
        normalized: list[Claim],
        *,
        phase: str,
        receipt: str,
        attempt_id: str,
    ) -> tuple[list[str] | None, list[dict[str, Any]]]:
        rows = conn.execute(
            """SELECT * FROM native_workspace_claims
               WHERE phase IN ('baseline','execute','capture','restore','unknown')"""
        ).fetchall()
        same_turn: list[sqlite3.Row] = []
        conflicts: list[dict[str, Any]] = []
        incoming = [
            Claim(
                c.namespace,
                c.kind,
                c.name,
                c.write,
                c.holder,
                c.stable,
                strict_barrier=c.strict_barrier or phase in STRICT_BARRIER_PHASES,
            )
            for c in normalized
        ]
        for row in rows:
            if row["receipt"] == receipt and row["attempt_id"] == attempt_id:
                same_turn.append(row)
                continue
            existing = _row_to_claim(row)
            if any(claim.conflicts(existing, relaxed=False) for claim in incoming):
                conflicts.append(
                    {
                        "claim_id": row["claim_id"],
                        "phase": row["phase"],
                        "receipt": row["receipt"],
                        "attempt_id": row["attempt_id"],
                        "canonical_name": row["canonical_name"],
                    }
                )
        if same_turn and all(r["phase"] == phase for r in same_turn):
            existing_set = {
                (r["namespace"], r["resource_kind"], r["canonical_name"], r["write_mode"])
                for r in same_turn
            }
            incoming_set = {
                (c.namespace, c.kind, c.name, int(c.write)) for c in incoming
            }
            if existing_set == incoming_set:
                return [r["claim_id"] for r in same_turn], conflicts
        return None, conflicts

    def acquire_claim_tx(
        self,
        conn: sqlite3.Connection,
        *,
        claims: Sequence[Claim],
        phase: str,
        receipt: str,
        attempt_id: str,
        owner_epoch: int,
        workspace_version: int,
        process_identity: str | Mapping[str, Any] = "",
        now: float | None = None,
    ) -> list[str]:
        """Atomically check conflicts and record a complete set of workspace claims."""
        if not conn.in_transaction:
            raise RuntimeError("acquire_claim_tx requires an active transaction")
        if phase not in ("baseline", "execute", "capture", "restore"):
            raise ValueError(f"Invalid acquire phase: {phase}")
        receipt = identifier(receipt)
        if not attempt_id:
            raise ValueError("Invalid attempt_id")
        now_ts = time.time() if now is None else now
        normalized = [_normalize_claim(c) for c in claims]
        existing_ids, conflicts = self._find_conflicts(
            conn, normalized, phase=phase, receipt=receipt, attempt_id=attempt_id
        )
        if conflicts:
            raise WorkspaceConflictError(
                "工作区存在冲突的资源或屏障占用",
                409,
                conflicts=conflicts,
            )
        if existing_ids is not None:
            return existing_ids

        identity_str = _serialize_identity(process_identity)
        claim_ids: list[str] = []
        for idx, claim in enumerate(normalized):
            claim_id = f"{receipt}:{attempt_id}:{idx}"
            conn.execute(
                """INSERT INTO native_workspace_claims(
                     claim_id, namespace, resource_kind, canonical_name,
                     write_mode, stable, holder_kind, phase, receipt,
                     attempt_id, owner_epoch, workspace_version,
                     process_identity, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim_id,
                    claim.namespace,
                    claim.kind,
                    claim.name,
                    int(claim.write),
                    int(claim.stable),
                    claim.holder,
                    phase,
                    receipt,
                    attempt_id,
                    owner_epoch,
                    workspace_version,
                    identity_str,
                    now_ts,
                ),
            )
            claim_ids.append(claim_id)
        return claim_ids

    def transition_tx(
        self,
        conn: sqlite3.Connection,
        turn_or_key: Any,
        *,
        source: str = "execute",
        target: str = "capture",
        owner_epoch: int | None = None,
        now: float | None = None,
    ) -> int:
        """Atomically transition a live claim phase without releasing the barrier."""
        if not conn.in_transaction:
            raise RuntimeError("transition_tx requires an active transaction")
        receipt, attempt_id = _turn_identity(turn_or_key)
        now_ts = time.time() if now is None else now
        params: list[Any] = [target, now_ts, receipt, attempt_id, source]
        epoch_clause = ""
        if owner_epoch is not None:
            epoch_clause = " AND owner_epoch=?"
            params.append(owner_epoch)
        cursor = conn.execute(
            f"""UPDATE native_workspace_claims
                SET phase=?, updated_at=?
                WHERE receipt=? AND attempt_id=? AND phase=?{epoch_clause}""",
            params,
        )
        if cursor.rowcount > 0:
            return int(cursor.rowcount)
        already = conn.execute(
            """SELECT COUNT(*) FROM native_workspace_claims
               WHERE receipt=? AND attempt_id=? AND phase=?""",
            (receipt, attempt_id, target),
        ).fetchone()[0]
        if already > 0:
            return int(already)
        raise DevError(
            "CLAIM_TRANSITION_FAILED",
            f"无法将工作区声明从 {source} 转换到 {target}",
            409,
            receipt=receipt,
            attempt_id=attempt_id,
        )

    def register_child_writer_tx(
        self,
        conn: sqlite3.Connection,
        *,
        parent_receipt: str,
        parent_attempt_id: str,
        writer_id: str,
        process_identity: str | Mapping[str, Any] = "",
        now: float | None = None,
    ) -> bool:
        """Register a delegated child writer unless parent writer admission is closed."""
        if not conn.in_transaction:
            raise RuntimeError("register_child_writer_tx requires an active transaction")
        parent_receipt = identifier(parent_receipt)
        if not parent_attempt_id or not writer_id:
            raise ValueError("Invalid parent_attempt_id or writer_id")
        gate = conn.execute(
            """SELECT admission_closed FROM native_writer_gates
               WHERE receipt=? AND attempt_id=?""",
            (parent_receipt, parent_attempt_id),
        ).fetchone()
        if gate is not None and int(gate["admission_closed"]) == 1:
            return False
        parent_live = conn.execute(
            """SELECT 1 FROM native_workspace_claims
               WHERE receipt=? AND attempt_id=? AND phase IN ('baseline','execute','capture')
               LIMIT 1""",
            (parent_receipt, parent_attempt_id),
        ).fetchone()
        if parent_live is None:
            raise DevError("PARENT_CLAIM_REQUIRED", "父任务未持有活跃工作区声明", 409)
        now_ts = time.time() if now is None else now
        conn.execute(
            """INSERT OR IGNORE INTO native_child_writers(
                 parent_receipt, parent_attempt_id, writer_id, status,
                 process_identity, created_at, updated_at
               ) VALUES (?,?,?,'active',?,?,?)""",
            (
                parent_receipt,
                parent_attempt_id,
                writer_id,
                _serialize_identity(process_identity),
                now_ts,
                now_ts,
            ),
        )
        return True

    def close_child_admission_tx(
        self,
        conn: sqlite3.Connection,
        parent_receipt: str,
        parent_attempt_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Atomically close child writer admission and return whether all writers are drained."""
        if not conn.in_transaction:
            raise RuntimeError("close_child_admission_tx requires an active transaction")
        parent_receipt = identifier(parent_receipt)
        now_ts = time.time() if now is None else now
        conn.execute(
            """INSERT INTO native_writer_gates(receipt, attempt_id, admission_closed, updated_at)
               VALUES (?,?,1,?)
               ON CONFLICT(receipt, attempt_id) DO UPDATE SET
                 admission_closed=1, updated_at=excluded.updated_at""",
            (parent_receipt, parent_attempt_id, now_ts),
        )
        return self.are_writers_drained_tx(conn, parent_receipt, parent_attempt_id)

    def finish_child_writer_tx(
        self,
        conn: sqlite3.Connection,
        *,
        parent_receipt: str,
        parent_attempt_id: str,
        writer_id: str,
        status: str = "finished",
        now: float | None = None,
    ) -> bool:
        """Mark a delegated child writer settled and return whether all writers are drained."""
        if not conn.in_transaction:
            raise RuntimeError("finish_child_writer_tx requires an active transaction")
        if status not in ("finished", "failed", "aborted"):
            raise ValueError(f"Invalid child writer status: {status}")
        parent_receipt = identifier(parent_receipt)
        now_ts = time.time() if now is None else now
        conn.execute(
            """UPDATE native_child_writers SET status=?, updated_at=?
               WHERE parent_receipt=? AND parent_attempt_id=? AND writer_id=?""",
            (status, now_ts, parent_receipt, parent_attempt_id, writer_id),
        )
        return self.are_writers_drained_tx(conn, parent_receipt, parent_attempt_id)

    def are_writers_drained_tx(
        self, conn: sqlite3.Connection, parent_receipt: str, parent_attempt_id: str
    ) -> bool:
        """Return True iff no active delegated child writers remain for this turn."""
        parent_receipt = identifier(parent_receipt)
        count = conn.execute(
            """SELECT COUNT(*) FROM native_child_writers
               WHERE parent_receipt=? AND parent_attempt_id=? AND status='active'""",
            (parent_receipt, parent_attempt_id),
        ).fetchone()[0]
        return int(count) == 0

    def release_or_drain_tx(
        self,
        conn: sqlite3.Connection,
        turn_or_key: Any,
        *,
        now: float | None = None,
    ) -> str:
        """Close child admission and release claims only once all managed writers drained."""
        if not conn.in_transaction:
            raise RuntimeError("release_or_drain_tx requires an active transaction")
        receipt, attempt_id = _turn_identity(turn_or_key)
        now_ts = time.time() if now is None else now
        if not self.close_child_admission_tx(conn, receipt, attempt_id, now=now_ts):
            return "draining"
        conn.execute(
            """UPDATE native_workspace_claims SET phase='released', updated_at=?
               WHERE receipt=? AND attempt_id=? AND phase!='released'""",
            (now_ts, receipt, attempt_id),
        )
        return "released"

    def _holder_info(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[str | None, list[int]]:
        parsed = _parse_identity(row["process_identity"])
        sid = parsed.get("session_id")
        pids: list[int] = []
        raw_pid = parsed.get("pid")
        if isinstance(raw_pid, int) and raw_pid > 0:
            pids.append(raw_pid)
        turn = conn.execute(
            "SELECT session_id FROM native_turns WHERE receipt=? AND attempt_id=?",
            (row["receipt"], row["attempt_id"]),
        ).fetchone()
        if turn is not None:
            sid = sid or turn["session_id"]
        if sid:
            session = conn.execute(
                "SELECT worker_pid, child_pid FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if session is not None:
                for col in ("worker_pid", "child_pid"):
                    if isinstance(session[col], int) and session[col] > 0:
                        pids.append(session[col])
        return sid, pids

    def _has_incomplete_restore(self, conn: sqlite3.Connection, receipt: str, attempt_id: str) -> bool:
        row = conn.execute(
            """SELECT 1 FROM native_restore_journals
               WHERE receipt=? AND attempt_id=? AND stage != 'completed' LIMIT 1""",
            (receipt, attempt_id),
        ).fetchone()
        return row is not None

    def _classify_claim(
        self, directory: str | Path, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> str:
        sid, pids = self._holder_info(conn, row)
        if sid and worker_present(directory, sid):
            return "keep"
        if any(process_exists(pid) for pid in pids):
            return "keep"
        if not sid or not pids:
            return "unknown"
        if not self.are_writers_drained_tx(conn, row["receipt"], row["attempt_id"]):
            return "unknown"
        if row["phase"] == "unknown":
            return "unknown"
        if row["phase"] == "capture":
            # Once the provider is terminal, the persisted barrier belongs to
            # the evidence job, not the process that originally created it.
            pending = conn.execute(
                """SELECT 1 FROM native_turns WHERE receipt=? AND attempt_id=?
                   AND evidence_state IN ('capture_pending','capturing')""",
                (row["receipt"], row["attempt_id"]),
            ).fetchone()
            if pending is not None:
                return "keep"
            return "unknown"
        if row["phase"] == "restore" and self._has_incomplete_restore(
            conn, row["receipt"], row["attempt_id"]
        ):
            return "unknown"
        return "released"

    def _reconcile_tx(
        self, directory: str | Path, conn: sqlite3.Connection, now_ts: float
    ) -> dict[str, list[str]]:
        report: dict[str, list[str]] = {"kept": [], "unknown": [], "released": []}
        rows = conn.execute(
            """SELECT * FROM native_workspace_claims
               WHERE phase IN ('baseline','execute','capture','restore','unknown')"""
        ).fetchall()
        for row in rows:
            decision = self._classify_claim(directory, conn, row)
            claim_id = row["claim_id"]
            if decision == "keep":
                report["kept"].append(claim_id)
                continue
            new_phase = "unknown" if decision == "unknown" else "released"
            conn.execute(
                "UPDATE native_workspace_claims SET phase=?, updated_at=? WHERE claim_id=?",
                (new_phase, now_ts, claim_id),
            )
            report["unknown" if decision == "unknown" else "released"].append(claim_id)
        return report

    def reconcile_claims(
        self,
        directory: str | Path,
        conn: sqlite3.Connection,
        *,
        now: float | None = None,
    ) -> dict[str, list[str]]:
        """Verify WorkerLock and process liveness; mark unprovable holders unknown."""
        now_ts = time.time() if now is None else now
        if conn.in_transaction:
            return self._reconcile_tx(directory, conn, now_ts)
        with immediate(conn):
            return self._reconcile_tx(directory, conn, now_ts)

    def sync_to_resource_queue(
        self, conn: sqlite3.Connection, queue: ResourceQueue
    ) -> list[Any]:
        """Hydrate live and unknown durable claims into an in-memory ResourceQueue."""
        rows = conn.execute(
            """SELECT * FROM native_workspace_claims
               WHERE phase IN ('baseline','execute','capture','restore','unknown')
               ORDER BY updated_at ASC, claim_id ASC"""
        ).fetchall()
        grouped: dict[tuple[str, str], list[Claim]] = {}
        for row in rows:
            grouped.setdefault((row["receipt"], row["attempt_id"]), []).append(_row_to_claim(row))
        tokens: list[Any] = []
        for (receipt, attempt_id), claim_list in grouped.items():
            op_id = f"native:{receipt}:{attempt_id}"
            token = queue.try_reserve(op_id, claim_list, strict=True)
            if token is not None:
                tokens.append(token)
        return tokens
