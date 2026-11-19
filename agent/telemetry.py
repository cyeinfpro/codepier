"""Diagnostic stages are durable metadata, never command output or credentials."""

from __future__ import annotations
import json
import sqlite3
import time
import threading
from collections import OrderedDict
from shared.computer_diagnostics import COMPUTER_STAGES, safe_detail
from shared.scheduler_config import safe_queue_detail
from shared.ssh_diagnostics import SSH_STAGES, safe_ssh_detail
from shared.process_diagnostics import PROCESS_STAGES, safe_process_detail

STAGES = (
    {
        "accepted",
        "waiting_project",
        "waiting_resource",
        "waiting_worker",
        "bypassed",
        "executing",
        "persisting",
        "result_ready",
    }
    | set(COMPUTER_STAGES)
    | set(SSH_STAGES)
    | set(PROCESS_STAGES)
)


_PENDING_DB_LIMIT = 8192


class AgentTelemetry:
    def __init__(self, journal, *, process_diagnostics=False, deferred_persist=False):
        self.journal = journal
        self.process_diagnostics = process_diagnostics
        self.deferred_persist = bool(deferred_persist)
        self.starts = OrderedDict()
        self._last = {}
        self.errors = 0
        self._process_pending = OrderedDict()
        self._process_lock = threading.Lock()
        self._mem_lock = threading.Lock()
        self._stages: OrderedDict[str, list[dict]] = OrderedDict()
        self._next_seq: dict[str, int] = {}
        self._pending_db: list[tuple[str, int, str, int, str]] = []
        self._dropped = 0
        with journal.lock, journal.db:
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS execution_stages (operation_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(operation_id,seq))"
            )
            for row in journal.db.execute(
                "SELECT operation_id,seq,stage,elapsed_ms,detail FROM execution_stages "
                "WHERE operation_id IN (SELECT id FROM calls WHERE status IN ('accepted','running','retryable')) "
                "ORDER BY operation_id,seq"
            ):
                op_id = row[0]
                stages = self._stages.setdefault(op_id, [])
                stages.append(
                    {
                        "seq": row[1],
                        "stage": row[2],
                        "elapsed_ms": row[3],
                        "detail": json.loads(row[4]),
                    }
                )
                if len(stages) > 32:
                    del stages[:-32]
                self._next_seq[op_id] = row[1]

    def abandon(self, identifier):
        with self._mem_lock:
            self.starts.pop(identifier, None)
            self._last.pop(identifier, None)

    def process_event(self, identifier, stage, **detail):
        """Bounded, best-effort memory only: never delay a workload on disk I/O."""
        if (
            not self.process_diagnostics
            or stage not in PROCESS_STAGES
            or not self._process_lock.acquire(blocking=False)
        ):
            return
        try:
            now = time.monotonic()
            if not self._mem_lock.acquire(blocking=False):
                return
            try:
                start = self.starts.get(identifier, now)
            finally:
                self._mem_lock.release()
            elapsed = max(0, round((now - start) * 1000))
            pending = self._process_pending.setdefault(identifier, [])
            if len(pending) < 16:
                pending.append((stage, elapsed, safe_process_detail(detail)))
            self._process_pending.move_to_end(identifier)
            while len(self._process_pending) > 256:
                self._process_pending.popitem(last=False)
        finally:
            self._process_lock.release()

    def _flush_process(self, identifier):
        # Called only by existing telemetry readers/writers, never the child path.
        if not self.process_diagnostics:
            return
        with self._process_lock:
            pending = self._process_pending.pop(identifier, [])
        for stage, elapsed, detail in pending:
            self.record(identifier, stage, _elapsed_ms=elapsed, _flush=False, **detail)

    def _record_memory_locked(
        self,
        identifier,
        stage,
        seq,
        elapsed,
        allowed,
        signature,
        now,
        *,
        queue_persist: bool,
    ):
        self.starts.setdefault(identifier, now)
        self.starts.move_to_end(identifier)
        self._next_seq[identifier] = seq
        entry = {"seq": seq, "stage": stage, "elapsed_ms": elapsed, "detail": allowed}
        stages = self._stages.setdefault(identifier, [])
        stages.append(entry)
        if len(stages) > 32:
            del stages[:-32]
        self._stages.move_to_end(identifier)
        self._last[identifier] = signature
        if queue_persist:
            if len(self._pending_db) < _PENDING_DB_LIMIT:
                self._pending_db.append((identifier, seq, stage, elapsed, json.dumps(allowed)))
            else:
                self._dropped += 1
        if stage == "result_ready":
            self.starts.pop(identifier, None)
            self._last.pop(identifier, None)
        while len(self.starts) > 4096:
            expired, _ = self.starts.popitem(last=False)
            self._last.pop(expired, None)
        while len(self._stages) > 4096:
            expired, _ = self._stages.popitem(last=False)
            self._next_seq.pop(expired, None)

    def _record_sync(self, identifier, stage, allowed, signature, _elapsed_ms):
        with self._mem_lock:
            if stage.startswith("waiting_") and self._last.get(identifier) == signature:
                return False
            start = self.starts.get(identifier)
        now = time.monotonic()
        elapsed = (
            max(0, round((now - (now if start is None else start)) * 1000))
            if _elapsed_ms is None
            else _elapsed_ms
        )
        payload = json.dumps(allowed)
        try:
            with self.journal.lock, self.journal.db:
                seq = self.journal.db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM execution_stages WHERE operation_id=?",
                    (identifier,),
                ).fetchone()[0]
                self.journal.db.execute(
                    "INSERT INTO execution_stages VALUES (?,?,?,?,?)",
                    (identifier, seq, stage, elapsed, payload),
                )
                self.journal.db.execute(
                    "DELETE FROM execution_stages WHERE operation_id=? AND seq<?",
                    (identifier, seq - 31),
                )
        except (sqlite3.Error, OSError):
            self.errors += 1
            return False
        with self._mem_lock:
            self._record_memory_locked(
                identifier,
                stage,
                seq,
                elapsed,
                allowed,
                signature,
                now,
                queue_persist=False,
            )
        return True

    def record(self, identifier, stage, *, _elapsed_ms=None, _flush=True, **detail):
        if stage not in STAGES:
            return False
        if _flush:
            self._flush_process(identifier)
        allowed = {k: v for k, v in detail.items() if k in {"blocked_by", "mode"}}
        if stage in PROCESS_STAGES:
            allowed = safe_process_detail(detail)
        elif stage in COMPUTER_STAGES:
            allowed = safe_detail(detail)
        elif stage in SSH_STAGES:
            allowed = safe_ssh_detail(detail)
        elif stage in {"waiting_worker", "waiting_resource", "bypassed"}:
            allowed.update(safe_queue_detail(detail))
        # A wait is a transition, not a periodic heartbeat. Its original elapsed
        # time remains durable; reason/blocker changes get a new event. Excluding
        # queue age keeps harmless scheduler drains from writing the same wait.
        signature = (
            stage,
            json.dumps({k: v for k, v in allowed.items() if k != "queue_age_ms"}, sort_keys=True),
        )
        if not self.deferred_persist:
            return self._record_sync(identifier, stage, allowed, signature, _elapsed_ms)
        now = time.monotonic()
        with self._mem_lock:
            if stage.startswith("waiting_") and self._last.get(identifier) == signature:
                return False
            start = self.starts.get(identifier)
            seq = self._next_seq.get(identifier, 0) + 1
            elapsed = (
                max(0, round((now - (now if start is None else start)) * 1000))
                if _elapsed_ms is None
                else _elapsed_ms
            )
            self._record_memory_locked(
                identifier,
                stage,
                seq,
                elapsed,
                allowed,
                signature,
                now,
                queue_persist=True,
            )
            return True

    @property
    def dropped(self) -> int:
        """Lifetime count of deferred records lost before a durable commit."""
        with self._mem_lock:
            return self._dropped

    def has_pending(self) -> bool:
        with self._mem_lock:
            return bool(self._pending_db)

    def flush(self) -> int:
        with self._mem_lock:
            if not self._pending_db:
                return 0
            batch = self._pending_db[:]
            self._pending_db.clear()
        affected: dict[str, int] = {}
        for op_id, seq, _, _, _ in batch:
            if seq > affected.get(op_id, 0):
                affected[op_id] = seq
        try:
            with self.journal.lock, self.journal.db:
                self.journal.db.executemany(
                    "INSERT OR REPLACE INTO execution_stages VALUES (?,?,?,?,?)", batch
                )
                for op_id, max_seq in affected.items():
                    self.journal.db.execute(
                        "DELETE FROM execution_stages WHERE operation_id=? AND seq<?",
                        (op_id, max_seq - 31),
                    )
            return len(batch)
        except (sqlite3.Error, OSError):
            with self._mem_lock:
                for op_id in affected:
                    self._last.pop(op_id, None)
                self._dropped += len(batch)
                self.errors += 1
            return 0

    def has_cached(self, identifier) -> bool:
        if not self.deferred_persist:
            return False
        with self._mem_lock:
            return identifier in self._stages

    def snapshot(self, identifier):
        self._flush_process(identifier)
        if self.deferred_persist:
            with self._mem_lock:
                cached = self._stages.get(identifier)
                if cached is not None:
                    return [
                        {
                            "seq": item["seq"],
                            "stage": item["stage"],
                            "elapsed_ms": item["elapsed_ms"],
                            "detail": dict(item["detail"]),
                        }
                        for item in cached
                    ]
        try:
            with self.journal.lock:
                rows = self.journal.db.execute(
                    "SELECT seq,stage,elapsed_ms,detail FROM execution_stages WHERE operation_id=? ORDER BY seq",
                    (identifier,),
                ).fetchall()
            return [
                {"seq": r[0], "stage": r[1], "elapsed_ms": r[2], "detail": json.loads(r[3])}
                for r in rows
            ]
        except (sqlite3.Error, OSError):
            self.errors += 1
            return []
