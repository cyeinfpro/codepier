"""Diagnostic stages are durable metadata, never command output or credentials."""
from __future__ import annotations
import json
import sqlite3
import time
from shared.computer_diagnostics import COMPUTER_STAGES, safe_detail

STAGES = {'accepted', 'waiting_project', 'waiting_worker', 'executing', 'persisting', 'result_ready'} | set(COMPUTER_STAGES)

class AgentTelemetry:
    def __init__(self, journal):
        self.journal = journal
        self.starts = {}
        self.errors = 0
        with journal.lock, journal.db:
            journal.db.execute('CREATE TABLE IF NOT EXISTS execution_stages (operation_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(operation_id,seq))')

    def record(self, identifier, stage, **detail):
        if stage not in STAGES:
            return
        start = self.starts.setdefault(identifier, time.monotonic())
        allowed = {k: v for k, v in detail.items() if k in {'blocked_by', 'mode'}}
        if stage in COMPUTER_STAGES:
            allowed = safe_detail(detail)
        try:
            with self.journal.lock, self.journal.db:
                seq = self.journal.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM execution_stages WHERE operation_id=?', (identifier,)).fetchone()[0]
                self.journal.db.execute('INSERT INTO execution_stages VALUES (?,?,?,?,?)',
                    (identifier, seq, stage, max(0, round((time.monotonic()-start)*1000)), json.dumps(allowed)))
                self.journal.db.execute('DELETE FROM execution_stages WHERE operation_id=? AND seq<?', (identifier, seq-31))
        except (sqlite3.Error, OSError):
            self.errors += 1

    def snapshot(self, identifier):
        try:
            with self.journal.lock:
                rows = self.journal.db.execute('SELECT seq,stage,elapsed_ms,detail FROM execution_stages WHERE operation_id=? ORDER BY seq', (identifier,)).fetchall()
            return [{'seq': r[0], 'stage': r[1], 'elapsed_ms': r[2], 'detail': json.loads(r[3])} for r in rows]
        except (sqlite3.Error, OSError):
            self.errors += 1
            return []
