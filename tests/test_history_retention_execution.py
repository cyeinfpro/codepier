"""Retention runs exclusively on isolated synthetic temporary databases."""
import json
import sqlite3

import pytest

from shared.util import DevError
from hub.operation_history import encrypt_legacy_history, prune_history, unseal, PREFIX
from hub.runtime import Runtime
from hub.store import Store
from hub.keyring import rotate_key
from hub.mcp_tasks import TaskService
from tests import test_mcp_tasks

task_runtime = test_mcp_tasks.task_runtime
NOW = 500 * 86400


def insert(store, identifier, state='succeeded', updated=1):
    store.execute("""INSERT INTO operations
        (id,actor,tool,args_summary,fingerprint,idem,state,result,output,created,updated)
        VALUES (?,'fixture','exec','{}',?,?,?,?,'synthetic output',1,?)""",
        (identifier, 'fingerprint-'+identifier, 'idem-'+identifier, state,
         json.dumps({'ok': True, 'data': {'output': 'synthetic output'}}), updated))


def test_expiry_preserves_identity_replay_timestamps_and_unresolved(tmp_path):
    store = Store(tmp_path/'hub')
    try:
        for state in ('succeeded', 'failed', 'cancelled', 'running', 'unknown', 'needs_review'):
            insert(store, state, state)
        insert(store, 'recent', updated=NOW)
        store.audit('fixture', 'synthetic')
        store.execute('UPDATE audit SET at=1')
        before = store.all('SELECT * FROM operations ORDER BY id')
        plan = prune_history(store, now=NOW, collaboration=False)
        assert plan['operation_bodies'] == 3 and plan['audit'] == 1
        assert store.all('SELECT * FROM operations ORDER BY id') == before
        assert prune_history(store, now=NOW, dry_run=False, collaboration=False)['operation_bodies'] == 3
        for row in store.all('SELECT * FROM operations'):
            assert row['fingerprint'] == 'fingerprint-'+row['id']
            assert row['idem'] == 'idem-'+row['id']
            if row['id'] in ('succeeded', 'failed', 'cancelled'):
                assert row['updated'] == 1 and row['history_pruned_at'] == NOW
                assert row['output'] == '' and row['result'].startswith(PREFIX)
                assert Runtime(store).operation_result(row)['history_expired']
            else:
                assert row['history_pruned_at'] is None and row['output'] == 'synthetic output'
        assert not store.all('SELECT * FROM audit')
        assert prune_history(store, now=NOW, dry_run=False, collaboration=False)['operation_bodies'] == 0
    finally:
        store.close()


def test_cleanup_rolls_back_all_bodies_when_audit_delete_fails(tmp_path):
    store = Store(tmp_path/'hub')
    try:
        insert(store, 'old')
        store.audit('fixture', 'old')
        store.execute('UPDATE audit SET at=1')
        store.execute("""CREATE TRIGGER stop_audit BEFORE DELETE ON audit
            BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
            prune_history(store, now=NOW, dry_run=False, collaboration=False)
        assert store.one("SELECT * FROM operations WHERE id='old'")['history_pruned_at'] is None
        assert store.one('SELECT * FROM audit')
    finally:
        store.close()


@pytest.mark.asyncio
async def test_frozen_legacy_snapshot_migrates_losslessly_and_remains_protected(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await test_mcp_tasks.create_task(runtime, principal, tasks)
    body = json.dumps({'ok': True, 'data': {'output': 'private legacy', 'exit_code': 0, 'command_ok': True}})
    snapshot = json.dumps({'operation_id': identifier, 'state': 'succeeded',
                          'result': body, 'error': None, 'updated': 1})
    runtime.store.execute('UPDATE mcp_tasks SET terminal_operation=? WHERE task_id=?', (snapshot, identifier))
    runtime.store.execute("UPDATE operations SET result=?,output='private legacy',state='succeeded',updated=1 WHERE id=?",
                          (body, identifier))
    expected = tasks.get(identifier, principal)
    assert encrypt_legacy_history(runtime.store)['mcp_tasks.terminal_operation'] == 1
    row = runtime.store.one('SELECT * FROM mcp_tasks WHERE task_id=?', (identifier,))
    assert row['terminal_operation'].startswith(PREFIX)
    assert unseal(runtime.store, row['terminal_operation']) == snapshot
    assert tasks.get(identifier, principal) == expected
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        runtime.store.execute('UPDATE mcp_tasks SET terminal_operation=? WHERE task_id=?', ('{}', identifier))
    plan = prune_history(runtime.store, now=NOW, dry_run=False, collaboration=False)
    assert plan['operation_bodies'] == 0 and plan['protected_by_reference'] == 1
    assert 'frozen_mcp_task' in plan['reference_reasons']
    assert sum(encrypt_legacy_history(runtime.store).values()) == 0
    directory = runtime.store.directory
    runtime.store.close()
    rotate_key(directory)
    rotate_key(directory)
    reopened = Store(directory)
    try:
        assert TaskService(Runtime(reopened)).get(identifier, principal) == expected
    finally:
        reopened.close()


def test_batch_limit_and_live_legacy_tail_migration(tmp_path):
    store = Store(tmp_path/'hub')
    try:
        for identifier in ('a', 'b', 'c'):
            insert(store, identifier)
        assert prune_history(store, now=NOW, batch=1, dry_run=False, collaboration=False)['operation_bodies'] == 1
        insert(store, 'live', 'running')
        store.execute("INSERT INTO operation_output_tails(operation_id,output_seq,output) VALUES ('live',0,'old tail')")
        encrypt_legacy_history(store)
        tail = store.one("SELECT * FROM operation_output_tails WHERE operation_id='live'")
        assert tail['output'] == '' and tail['output_chars'] == 8
        assert unseal(store, tail['sealed_output']) == 'old tail'
        assert prune_history(store, now=NOW, dry_run=False, collaboration=False)['operation_bodies'] == 2
        assert store.one("SELECT * FROM operations WHERE id='live'")['history_pruned_at'] is None
    finally:
        store.close()


def test_legacy_media_migration_preserves_existing_short_ttl(tmp_path):
    store = Store(tmp_path/'hub')
    try:
        insert(store, 'media')
        store.execute("""UPDATE operations SET tool='read',result=? WHERE id='media'""",
                      (json.dumps({'ok': True, 'data': {'computer_expires_at': 1,
                        '_computer_content': [], 'text': 'expired private pixels'}}),))
        encrypt_legacy_history(store)
        assert store.one("SELECT media_expires_at FROM operations WHERE id='media'")['media_expires_at'] == 1
        Runtime(store)._purge_computer()
        body = Runtime(store).operation_result(store.one("SELECT * FROM operations WHERE id='media'"))
        assert body['data']['media_expired'] and 'text' not in body['data']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unresolved_audit_and_explicit_artifact_reference_are_preserved(task_runtime):
    store = task_runtime[0].store
    try:
        insert(store, 'unresolved', 'unknown')
        insert(store, 'published')
        store.audit('fixture', 'event', 'unresolved')
        store.execute('UPDATE audit SET at=1')
        store.execute("""INSERT INTO artifacts
            (id,actor,root,name,bytes,sha256,created,expires,source_operation_id,project_id,device_id,space_id,owner_user_id)
            SELECT 'file','fixture','root','file',1,'digest',1,2,'published',id,device_id,space_id,owner_user_id FROM projects LIMIT 1""")
        result = prune_history(store, now=NOW, dry_run=False, collaboration=False)
        assert result['operation_bodies'] == 0 and result['protected_by_reference'] == 1
        assert store.one('SELECT * FROM audit')
    finally:
        store.close()


def test_combined_entry_runs_only_synthetic_data_and_checks_minimum_windows(tmp_path):
    store = Store(tmp_path/'hub')
    try:
        insert(store, 'old')
        with pytest.raises(ValueError):
            prune_history(store, now=NOW, operation_days=1, dry_run=False)
        with pytest.raises(ValueError):
            prune_history(store, now=NOW, audit_days=1, dry_run=False)
        report = prune_history(store, now=NOW, dry_run=False)
        assert report['operation_bodies'] == 1
        assert 'collaboration' in report
        assert not any(report['collaboration'].values())
    finally:
        store.close()


@pytest.mark.asyncio
async def test_expired_idempotency_receipt_never_dispatches_again(task_runtime):
    runtime, principal, _ = task_runtime
    args = {'project': 'Fixture', 'command': 'printf synthetic', 'yield_seconds': 0,
            'idempotency_key': 'synthetic-retention-replay'}
    original = await runtime.invoke('exec', args, principal)
    identifier = original['operation_id']
    runtime.complete({'id': identifier}, {'ok': True, 'data': {'output': 'done', 'exit_code': 0, 'command_ok': True}})
    runtime.store.execute('UPDATE operations SET updated=1 WHERE id=?', (identifier,))
    assert prune_history(runtime.store, now=NOW, dry_run=False, collaboration=False)['operation_bodies'] == 1
    with pytest.raises(DevError) as expired:
        await runtime.invoke('exec', args, principal)
    assert expired.value.code == 'HISTORY_EXPIRED'
    assert expired.value.details['operation_id'] == identifier
    assert expired.value.details['retryable'] is False
    assert runtime.store.one('SELECT count(*) AS n FROM operations')['n'] == 1
