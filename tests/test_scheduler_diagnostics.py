import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent.journal import Journal
from agent.telemetry import AgentTelemetry
from hub.diagnostics import Diagnostics, LABELS, event_detail, stage_label
from shared.scheduler_config import QUEUE_REASONS, safe_queue_detail
from shared.util import DevError


@pytest.mark.parametrize("value", [None, "", [], True, {"queue_reason": [], "lane": {}},
    {"queue_reason": "private command", "lane": "secret host"}])
def test_unknown_scheduler_diagnostics_fail_closed(value):
    assert safe_queue_detail(value) == {}


@pytest.mark.parametrize("reason", sorted(QUEUE_REASONS))
def test_known_queue_reason_roundtrips_through_journal_and_hub(tmp_path, reason):
    journal = Journal(tmp_path / "journal")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE operation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT,source TEXT,seq INTEGER,
        stage TEXT,at REAL,elapsed_ms INTEGER,detail TEXT,UNIQUE(operation_id,source,seq))""")
    class Store:
        lock = threading.RLock()
        def execute(self, sql, args=()):
            with self.lock, db:
                return db.execute(sql, args)
        def all(self, sql, args=()):
            return [dict(row) for row in db.execute(sql, args).fetchall()]
        def one(self, sql, args=()):
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None
    store = Store()
    store.db = db
    operation = dict(id="op", device_id="node", pending=True, state="running",
                     cancel_requested=False, accepted_at=1, transport_error=None,
                     attempts=1, created=time.time(), updated=time.time())
    principal = object()
    def authorized(identifier, who, _):
        assert who is principal
        if identifier != "op":
            raise DevError("FORBIDDEN", "not visible", 403)
        return dict(operation)
    runtime = SimpleNamespace(store=store, operation=authorized, online=lambda _: True)
    diagnostics = Diagnostics(runtime)
    try:
        telemetry = AgentTelemetry(journal)
        telemetry.record("op", "waiting_worker", queue_reason=reason, lane="remote",
                         command="private command", host="secret host", blocked_by=["private-op"])
        events = telemetry.snapshot("op")
        assert events[0]["detail"]["queue_reason"] == reason
        assert "command" not in events[0]["detail"] and "host" not in events[0]["detail"]
        diagnostics.ingest("op", events)
        trace = diagnostics.trace({"operation_id": "op", "after_event_id": 0, "limit": 50}, principal)
        assert trace["current"]["reason"] == QUEUE_REASONS[reason]
        assert trace["events"][0]["detail"] == {"queue_reason": reason, "lane": "remote"}
        assert trace["events"][0]["label"] == QUEUE_REASONS[reason]
        assert trace["events"][0]["blocked_by"] == []
        assert "private" not in json.dumps(trace) and "secret" not in json.dumps(trace)
        # Old agents and unknown future reasons retain a safe generic label.
        diagnostics.ingest("op", [{"stage": "waiting_worker", "seq": 2, "elapsed_ms": 1,
                                  "detail": {"queue_reason": "untrusted text", "lane": ["remote"]}}])
        again = diagnostics.trace({"operation_id": "op", "after_event_id": 0, "limit": 50}, principal)
        assert again["current"]["reason"] == LABELS["waiting_worker"]
        assert again["events"][-1]["detail"] == {}
        # An older retained wait cannot override a terminal/cancel/offline status.
        operation["cancel_requested"] = True
        assert "取消" in diagnostics.trace({"operation_id": "op", "after_event_id": 0, "limit": 50}, principal)["current"]["reason"]
        operation["pending"] = False
        assert "已结束" in diagnostics.trace({"operation_id": "op", "after_event_id": 0, "limit": 50}, principal)["current"]["reason"]
    finally:
        journal.db.close()
        db.close()


def test_scheduler_fields_do_not_expand_desktop_diagnostics(tmp_path):
    journal = Journal(tmp_path / "journal")
    try:
        telemetry = AgentTelemetry(journal)
        telemetry.record("op", "native_action", queue_reason="project_limit", lane="execution",
                         outcome="completed", command="private")
        assert telemetry.snapshot("op")[0]["detail"] == {"outcome": "completed"}
        assert event_detail("executing", {"queue_reason": "project_limit", "lane": "execution"}) == {}
        assert stage_label("executing", {"queue_reason": "project_limit"}) == LABELS["executing"]
    finally:
        journal.db.close()
