"""Synthetic cold archive and queue diagnostic regressions."""
import json
import time

import pytest

from hub.operation_archive import archive_payloads, restore_payloads
from hub.operation_history import result, seal, unseal
from hub.operation_output import OperationOutput
from hub.keyring import rotate_key
from hub.queue_contention import queue_contention_report
from hub.store import Store


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
