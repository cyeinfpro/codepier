"""Real SQLite completion preserves uncertain writes as reviewable receipts."""
import time
from types import SimpleNamespace

import pytest

from hub.operation_output import OperationOutput
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError


@pytest.fixture
def completion_runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    runtime = Runtime.__new__(Runtime)
    runtime.store = store
    runtime.outputs = OperationOutput(store)
    runtime.integrations = SimpleNamespace(complete=lambda *args: None)
    runtime.diagnostics = SimpleNamespace(ingest=lambda *args: None, record=lambda *args: None,
        connection=SimpleNamespace(completed=lambda *args, **kwargs: None, write_errors=0))
    runtime.collaboration = SimpleNamespace(events=None)
    runtime.notify_operation = lambda *args: None
    runtime.publish = lambda *args: None
    runtime.on_loop = lambda *args: None
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',
                  ('fixture-device', 'fixture', store.encrypt('synthetic-secret'), time.time()))
    yield runtime
    store.close()


def insert_operation(runtime, tool):
    now = time.time()
    runtime.store.execute('''INSERT INTO operations
        (id,device_id,actor,tool,args_summary,fingerprint,idem,state,created,updated)
        VALUES (?,?,?,?,?,?,?,'running',?,?)''',
        ('fixture-operation', 'fixture-device', 'panel:fixture', tool, '{}', 'fixture-fingerprint',
         'fixture-idempotency-key', now, now))
    return runtime.store.one('SELECT * FROM operations WHERE id=?', ('fixture-operation',))


@pytest.mark.parametrize('code,state', [
    ('WRITE_OUTCOME_UNCONFIRMED', 'needs_review'),
    ('STAGING_CLEANUP_UNCONFIRMED', 'needs_review'),
    ('DURABILITY_UNCONFIRMED', 'needs_review'),
    ('NOT_FOUND', 'failed'), ('READ_ONLY', 'failed'),
    ('ATOMIC_CREATE_UNSUPPORTED', 'failed'), ('FILESYSTEM_UNAVAILABLE', 'failed'),
])
def test_file_receipt_separates_uncertain_effect_from_definite_failure(completion_runtime, code, state):
    runtime = completion_runtime; op = insert_operation(runtime, 'fs_move')
    error = {'code': code, 'message': 'fixture', 'path': 'src/a.py', 'retryable': False}
    runtime.complete(op, {'ok': False, 'error': error})
    row = runtime.store.one('SELECT * FROM operations WHERE id=?', (op['id'],))
    assert row['state'] == state
    assert runtime.operation_result(row)['error'] == error
    # A second receipt observation does not convert uncertainty into success.
    runtime.complete(op, {'ok': False, 'error': error})
    assert runtime.store.one('SELECT state FROM operations WHERE id=?', (op['id'],))['state'] == state


@pytest.mark.parametrize('tool,data,state', [
    ('agent_roots_set', {'state': 'needs_review', 'outcome_confirmed': False}, 'needs_review'),
    ('agent_roots_set', {'state': 'pending_reload', 'outcome_confirmed': False}, 'needs_review'),
    ('agent_roots_set', {'state': 'pending_reload', 'outcome_confirmed': True}, 'succeeded'),
    ('agent_roots_set', {'state': 'applied', 'outcome_confirmed': True}, 'succeeded'),
    ('agent_roots_get', {'state': 'needs_review', 'outcome_confirmed': False}, 'succeeded'),
])
def test_only_directory_save_uncertainty_changes_success_receipt_state(completion_runtime, tool, data, state):
    runtime = completion_runtime; op = insert_operation(runtime, tool)
    runtime.complete(op, {'ok': True, 'data': data})
    row = runtime.store.one('SELECT * FROM operations WHERE id=?', (op['id'],))
    assert row['state'] == state and runtime.operation_result(row)['data'] == data


def test_synchronous_tool_error_keeps_whitelisted_file_recovery_detail():
    with pytest.raises(DevError) as caught:
        Runtime.unwrap('original-operation', {'ok': False, 'error': {
            'code': 'WRITE_OUTCOME_UNCONFIRMED', 'message': 'fixture', 'retryable': False,
            'path': 'src/a.py', 'backup_id': 'a'*32, 'expected_sha256': 'b'*64,
            'publication': 'unconfirmed', 'secret': 'never-return'}}, tool='write')
    error = caught.value
    assert error.code == 'WRITE_OUTCOME_UNCONFIRMED'
    assert error.details['operation_id'] == 'original-operation'
    assert error.details['path'] == 'src/a.py' and error.details['backup_id'] == 'a'*32
    assert error.details['expected_sha256'] == 'b'*64 and error.details['retryable'] is False
    assert 'secret' not in error.details
