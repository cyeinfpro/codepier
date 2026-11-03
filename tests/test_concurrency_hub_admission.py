"""Metadata reserves and bounded diagnostic writes under sustained contention."""
import json
from types import SimpleNamespace

import pytest

from hub.admission_policy import Budget, check_admission, usage
from hub.diagnostics import Diagnostics, event_detail
from hub.store import Store
from shared.util import DevError


def empty_counts(**changes):
    return dict.fromkeys(("global_count", "global_bytes", "node_count", "node_bytes",
                          "project_count", "project_bytes", "node_reads", "project_reads", "controls"), 0) | changes


def test_minimal_open_uses_bounded_read_reserve_without_baseline():
    budget = Budget(paused=True, reason="pressure")
    check_admission(budget, empty_counts(node_count=64, project_count=32),
                    "open_workspace", 100, args={"context_detail": "minimal"})
    for args in ({"capture_baseline": True}, {"context_detail": "documents"}):
        with pytest.raises(DevError) as error:
            check_admission(budget, empty_counts(), "open_workspace", 100, args=args)
        assert error.value.code == "DEVICE_BUSY"
    with pytest.raises(DevError):
        check_admission(budget, empty_counts(node_count=512), "open_workspace", 100, args={})


def test_durable_usage_counts_only_minimal_observations(tmp_path):
    store = Store(tmp_path)
    try:
        summaries = [{}, {"context_detail": "minimal"}, {"capture_baseline": True},
                     {"context_detail": "documents"}]
        for number, summary in enumerate(summaries):
            store.execute("""INSERT INTO operations
                (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated)
                VALUES(?,'node','project','actor','open_workspace',?,'f','queued',0,0)""",
                          (str(number), json.dumps(summary)))
        counts = usage(store, "node", "project")
        assert counts["global_count"] == 4
        assert counts["node_reads"] == counts["project_reads"] == 2
    finally:
        store.close()


def test_resource_wait_details_are_bounded_and_do_not_disclose_resource_names():
    detail = event_detail("waiting_resource", {
        "queue_reason": "resource_conflict", "lane": "read",
        "resource_kinds": ["path", "service", "/private/file", "path"],
        "blocked_count": 3, "queue_age_ms": 1234,
        "path": "/private/file", "command": "private command",
    })
    assert detail == {"queue_reason": "resource_conflict", "lane": "read",
                      "resource_kinds": ["path", "service"],
                      "blocked_count": 3, "queue_age_ms": 1234}
    assert event_detail("waiting_resource", {"blocked_count": True, "queue_age_ms": -1}) == {}


def test_trace_batch_commits_once_and_duplicate_replays_do_not_write(tmp_path):
    store = Store(tmp_path)
    diagnostics = Diagnostics(SimpleNamespace(store=store))
    statements = []
    with store.lock:
        store.db.set_trace_callback(statements.append)
    trace = [{"stage": "waiting_resource", "seq": n, "elapsed_ms": n,
              "detail": {"blocked_by": ["other"], "queue_reason": "resource_conflict"}}
             for n in range(1, 33)]
    try:
        diagnostics.ingest("operation", trace)
        assert sum(sql == "COMMIT" for sql in statements) == 1
        assert sum(sql.startswith("DELETE FROM operation_events") for sql in statements) == 1
        statements.clear()
        diagnostics.ingest("operation", trace)
        assert not any(sql.startswith(("INSERT", "DELETE", "UPDATE")) for sql in statements)
        assert store.one("SELECT COUNT(*) AS n FROM operation_events")["n"] == 32
        for n in range(33, 234):
            diagnostics.record("operation", "executing", source="agent", seq=n, elapsed_ms=n)
        assert store.one("SELECT COUNT(*) AS n FROM operation_events")["n"] == 200
        assert store.one("SELECT MIN(seq) AS first FROM operation_events")["first"] == 34
    finally:
        store.close()
