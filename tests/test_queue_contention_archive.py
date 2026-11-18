"""Synthetic cold archive and queue diagnostic regressions."""
from contextlib import closing
import json
import time

import pytest

from hub.operation_archive import archive_payloads, restore_payloads
from hub.operation_history import result, seal, unseal
from hub.operation_output import OperationOutput
from hub.keyring import rotate_key
from hub.queue_contention import baseline_window_report, queue_contention_report
from hub.store import Store
from tests.test_reliability import runtime


def insert(store, identifier='old', *, state='succeeded', updated=1, output='secret tail'):
    store.execute("""INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,
        result,output,created,updated,accepted_at) VALUES (?,'fixture','read','{}',?,?,?, ?,1,?,1)""",
        (identifier, identifier, state, seal(store, json.dumps({'value': 'compressible '*5000})),
         seal(store, output), updated))


def test_archive_roundtrip_rotation_and_restore(tmp_path):
    store = Store(tmp_path)
    insert(store)
    insert(store, 'active', state='running')
    insert(store, 'review', state='needs_review')
    insert(store, 'recent', updated=time.time())
    before = store.one("SELECT * FROM operations WHERE id='old'")
    expected = result(store, before)
    assert archive_payloads(store)['operations'] == 1
    assert store.one('SELECT count(*) AS n FROM operation_payload_archives')['n'] == 0
    assert archive_payloads(store, dry_run=False)['operations'] == 1
    archived = store.one("SELECT * FROM operations WHERE id='old'")
    assert archived['created'] == before['created'] and archived['updated'] == before['updated']
    assert result(store, archived) == expected
    assert OperationOutput(store).read(archived) == ('secret tail', 11)
    assert OperationOutput(store).read(archived, include_body=False) == (None, 11)
    assert archive_payloads(store, dry_run=False)['operations'] == 0
    body = store.one('SELECT body FROM operation_payload_archives')['body']
    assert len(body) < len(before['result']) and 'compressible' not in body
    store.close()
    rotate_key(tmp_path)
    store = Store(tmp_path)
    try:
        archived = store.one("SELECT * FROM operations WHERE id='old'")
        assert result(store, archived) == expected
        assert restore_payloads(store, dry_run=False)['operations'] == 1
        restored = store.one("SELECT * FROM operations WHERE id='old'")
        assert result(store, restored) == expected and unseal(store, restored['output']) == 'secret tail'
        assert restored['updated'] == before['updated']
    finally:
        store.close()


def test_archive_transaction_rolls_back_and_live_tails_are_excluded(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        insert(store)
        insert(store, 'tail')
        store.execute("INSERT INTO operation_output_tails(operation_id,output_seq,output) VALUES ('tail',1,'live')")
        original = store.execute
        def fail(sql, args=()):
            if sql.startswith('UPDATE operations SET result=?,output=?'):
                raise RuntimeError('synthetic failure')
            return original(sql, args)
        monkeypatch.setattr(store, 'execute', fail)
        with pytest.raises(RuntimeError, match='synthetic'):
            archive_payloads(store, dry_run=False)
        assert store.one('SELECT count(*) AS n FROM operation_payload_archives')['n'] == 0
        monkeypatch.setattr(store, 'execute', original)
        assert archive_payloads(store, dry_run=False)['operations'] == 1
    finally:
        store.close()


def test_diagnostics_convoy_and_thresholds(tmp_path):
    store = Store(tmp_path)
    now = 100000.0
    def event(identifier, stage, at, seq, **detail):
        store.execute("""INSERT INTO operation_events(operation_id,source,seq,stage,at,detail)
            VALUES (?,'agent',?,?,?,?)""", (identifier, seq, stage, at, json.dumps(detail)))
    try:
        insert(store, 'root', state='running', updated=now)
        event('root', 'accepted', now-500, 1)
        event('root', 'executing', now-499, 2)
        insert(store, 'middle', state='queued', updated=now)
        event('middle', 'accepted', now-200, 1)
        event('middle', 'waiting_resource', now-199, 2, blocked_by=['root'], lane='read')
        for index in range(6):
            identifier = 'wait'+str(index)
            insert(store, identifier, state='queued', updated=now)
            event(identifier, 'accepted', now-100, 1)
            event(identifier, 'waiting_resource', now-99, 2, blocked_by=['middle'], lane='read')
        report = queue_contention_report(store, now=now)
        assert report['operations'] == 8
        assert report['waits_over_10s'] == 7
        assert report['convoy_ratio'] == pytest.approx(0.75)
        assert report['root_blockers'][0]['operation_id'] == 'root'
        assert report['root_blockers'][0]['blocked_operations'] == 7
        assert len(report['alerts']['operations_over_60s']) == 7
        assert report['alerts']['projects_over_5'][0]['waiting_operations'] == 7
        assert report['alerts']['holders_over_5'][0]['blocked_operations'] == 7
        assert 'compressible' not in json.dumps(report)
    finally:
        store.close()


def test_missing_archive_key_is_not_regenerated(tmp_path):
    store = Store(tmp_path)
    insert(store)
    archive_payloads(store, dry_run=False)
    store.close()
    (tmp_path / 'master.key').unlink()
    with pytest.raises(RuntimeError, match='master.key is missing'):
        Store(tmp_path)
    assert not (tmp_path / 'master.key').exists()


def test_restore_preserves_later_result_redaction(tmp_path):
    store = Store(tmp_path)
    try:
        insert(store)
        archive_payloads(store, dry_run=False)
        store.execute("UPDATE operations SET result=? WHERE id='old'", (seal(store, '{"redacted":true}'),))
        restore_payloads(store, dry_run=False)
        assert result(store, store.one("SELECT * FROM operations WHERE id='old'")) == {'redacted': True}
    finally:
        store.close()


def test_offline_cli_backup_is_readable_and_running_hub_is_rejected(tmp_path, monkeypatch):
    from scripts.archive_operation_payloads import main
    from shared.instance_lock import InstanceLock
    store = Store(tmp_path)
    insert(store)
    store.close()
    monkeypatch.setattr('sys.argv', ['archive', '--data', str(tmp_path), '--apply'])
    with InstanceLock(tmp_path / '.hub.lock'):
        with pytest.raises(RuntimeError):
            main()
    main()
    backups = list((tmp_path / '.payload-backups').iterdir())
    assert len(backups) == 1
    backup = Store(backups[0])
    try:
        assert result(backup, backup.one("SELECT * FROM operations WHERE id='old'"))['value'].startswith('compressible')
        assert backup.one('SELECT count(*) AS n FROM operation_payload_archives')['n'] == 0
    finally:
        backup.close()


def test_archive_skips_media_and_validates_bounds(tmp_path):
    store = Store(tmp_path)
    try:
        insert(store)
        store.execute("UPDATE operations SET media_expires_at=1 WHERE id='old'")
        assert archive_payloads(store, dry_run=False)['operations'] == 0
        for arguments in ({'days': 29}, {'batch': 501}, {'dry_run': 1}, {'now': float('nan')}):
            with pytest.raises(ValueError):
                archive_payloads(store, **arguments)
    finally:
        store.close()


def test_baseline_keeps_window_cohorts_and_uses_first_same_attempt_evidence(tmp_path):
    now = 100000.0
    with closing(Store(tmp_path)) as store:
        def operation(identifier, tool, state, created, updated, *, device="device-1"):
            store.execute(
                """INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,
                   fingerprint,state,created,updated) VALUES (?,?,?,'admin',?,'{}',?,?,?,?)""",
                (identifier, device, "project-1", tool, identifier, state, created, updated),
            )

        def event(identifier, source, seq, stage, at, elapsed=None, **detail):
            store.execute(
                """INSERT INTO operation_events(operation_id,source,seq,stage,at,elapsed_ms,detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (identifier, source, seq, stage, at, elapsed, json.dumps(detail)),
            )

        # Created before the window, finished inside it. A dispatch retry and
        # pre-execution Agent restart must each contribute just one sample.
        operation("read", "fs_tree", "succeeded", 100, now - 30)
        event("read", "hub", 1, "hub_received", 100, baseline_connection="online",
              baseline_admission="adaptive")
        event("read", "hub", 2, "dispatched", 102)
        event("read", "hub", 3, "dispatched", now - 40)
        event("read", "hub", 4, "hub_completed", now - 30)
        event("read", "agent", 1, "accepted", 105, 15)
        event("read", "agent", 2, "accepted", now - 41, 3)
        # Out-of-order receipt arrival cannot replace Agent's sequence/elapsed.
        event("read", "agent", 3, "executing", now - 42, 63)
        operation("cancel", "write", "cancelled", now - 20, now - 19)
        event("cancel", "hub", 1, "hub_completed", now - 19, baseline_timeout=False)
        operation("timed-out", "exec", "failed", now - 18, now - 17)
        event("timed-out", "hub", 1, "hub_received", now - 18,
              baseline_connection="offline", baseline_admission="legacy_or_warming")
        event("timed-out", "hub", 2, "hub_completed", now - 17, baseline_timeout=True)
        operation("legacy-error", "exec", "failed", now - 16, now - 15)
        operation("waiting", "exec", "queued", 200, 205)
        operation("expired-window", "exec", "failed", 100, now - 86400)
        operation("other-device", "exec", "succeeded", now - 14, now - 13, device="device-2")
        report = baseline_window_report(store, now=now, device_id="device-1", project_id="project-1")
    assert report["total"]["admitted"] == 3
    assert report["total"]["completed"] == 4
    assert report["total"]["successful_per_second"] == pytest.approx(1 / 86400)
    assert report["total"]["terminal_by_state"]["cancelled"] == 1
    assert report["total"]["timeouts_observed"] == report["total"]["timeout_classification_unknown"] == 1
    assert report["total"]["queued_now"] == 1
    assert report["total"]["oldest_queued_age_seconds"] == now - 200
    latencies = report["by_workload"]["short_read"]["success_latency_ms"]
    assert latencies["hub_first_dispatch"] == {"n": 1, "p50": 2000, "p95": 2000, "p99": 2000}
    assert latencies["agent_to_executing"]["p95"] == 60
    assert latencies["hub_to_completion"]["p99"] == (now - 130) * 1000
    assert report["by_workload"]["file_write"]["success_latency_ms"]["hub_first_dispatch"]["p95"] is None
    conditions = {(r["workload"], r["connection"], r["admission"]): r for r in report["by_admission_condition"]}
    assert conditions["short_read", "online", "adaptive"]["completed"] == 1
    assert conditions["task_or_exec", "offline", "legacy_or_warming"]["timeouts_observed"] == 1
    assert conditions["task_or_exec", "unknown", "unknown"]["timeout_classification_unknown"] == 1
    assert "admission_rejections" in report["not_observed"]


def test_baseline_cli_reads_runtime_trace_without_counting_idempotent_replay(runtime, capsys, monkeypatch):
    from scripts.queue_contention_report import main

    r, principal = runtime
    project = r.store.one("SELECT * FROM projects WHERE id='proj'")
    args = {"path": "private-path", "idempotency_key": "private-key"}
    identifier, _ = r._admit_operation("fs_read", args, project, principal)
    r._admit_operation("fs_read", args, project, principal)
    timeout = {"ok": False, "error": {"code": "QUEUE_EXPIRED", "message": "private timeout"}}
    r.complete({"id": identifier}, timeout)
    r.complete({"id": identifier}, timeout)
    monkeypatch.setattr("sys.argv", [
        "report", "--database", str(r.store.state_directory / "hub.sqlite3"),
        "--baseline", "--device", "dev", "--project", "proj",
    ])
    main()
    output = capsys.readouterr().out
    assert "private" not in output and identifier not in output
    report = json.loads(output)
    assert report["total"]["admitted"] == report["total"]["completed"] == 1
    assert report["total"]["timeouts_observed"] == 1
    assert report["by_admission_condition"][0]["connection"] == "offline"
    assert report["by_admission_condition"][0]["admission"] == "legacy_or_warming"
