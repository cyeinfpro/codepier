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


class AgentTelemetry:
    def __init__(self, journal, *, process_diagnostics=False):
        self.journal = journal
        self.process_diagnostics = process_diagnostics
        self.starts = OrderedDict()
        self._last = {}
        self.errors = 0
        self._process_pending = OrderedDict()
        self._process_lock = threading.Lock()
        with journal.lock, journal.db:
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS execution_stages (operation_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(operation_id,seq))"
            )

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
            elapsed = max(0, round((now - self.starts.get(identifier, now)) * 1000))
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
        try:
            with self.journal.lock, self.journal.db:
                if stage.startswith("waiting_") and self._last.get(identifier) == signature:
                    return False
                start = self.starts.setdefault(identifier, time.monotonic())
                self.starts.move_to_end(identifier)
                seq = self.journal.db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM execution_stages WHERE operation_id=?",
                    (identifier,),
                ).fetchone()[0]
                self.journal.db.execute(
                    "INSERT INTO execution_stages VALUES (?,?,?,?,?)",
                    (
                        identifier,
                        seq,
                        stage,
                        max(0, round((time.monotonic() - start) * 1000))
                        if _elapsed_ms is None
                        else _elapsed_ms,
                        json.dumps(allowed),
                    ),
                )
                self.journal.db.execute(
                    "DELETE FROM execution_stages WHERE operation_id=? AND seq<?",
                    (identifier, seq - 31),
                )
            self._last[identifier] = signature
            if stage == "result_ready":
                self.starts.pop(identifier, None)
                self._last.pop(identifier, None)
            while len(self.starts) > 4096:
                expired, _ = self.starts.popitem(last=False)
                self._last.pop(expired, None)
            return True
        except (sqlite3.Error, OSError):
            self.errors += 1
            return False

    def snapshot(self, identifier):
        self._flush_process(identifier)
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
