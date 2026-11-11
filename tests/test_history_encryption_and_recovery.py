"""No real data: encrypted receipt/tail recovery, key rotation and dry-run retention."""
import json
import sqlite3

import pytest

from hub.operation_history import PREFIX, seal, unseal, retention_plan, rewrite_task_ciphertext
from hub.keyring import rotate_key
from hub.mcp_tasks import TaskService
from hub.runtime import Runtime
from hub.store import Store
from tests import test_mcp_tasks

task_runtime = test_mcp_tasks.task_runtime
create_task = test_mcp_tasks.create_task


@pytest.mark.asyncio
async def test_complete_encrypts_result_tail_and_frozen_task_and_rekeys(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime, principal, tasks)
    plaintext = 'synthetic-private-source-content'
    runtime.complete({'id': identifier}, {'ok': True, 'data': {'exit_code': 0, 'command_ok': True, 'output': plaintext}})
    row = runtime.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    assert row['result'].startswith(PREFIX) and row['output'].startswith(PREFIX)
    assert plaintext not in row['result'] and plaintext not in row['output']
    frozen = runtime.store.one('SELECT * FROM mcp_tasks WHERE task_id=?', (identifier,))
    assert plaintext not in frozen['terminal_operation'] and plaintext not in frozen['terminal_result']
    assert frozen['terminal_result'].startswith(PREFIX)
    expected = tasks.get(identifier, principal)
    assert expected['result']['structuredContent']['output'] == plaintext
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        runtime.store.execute('UPDATE mcp_tasks SET terminal_result=? WHERE task_id=?', (seal(runtime.store, '{}'), identifier))
    # Equal plaintext can be re-encrypted; different or corrupt data stays immutable.
    with runtime.store.transaction():
        rewrite_task_ciphertext(runtime.store, identifier, 'terminal_result',
                                seal(runtime.store, unseal(runtime.store, frozen['terminal_result'])))
    directory = runtime.store.directory
    runtime.store.close()
    rotate_key(directory)
    reopened = Store(directory)
    try:
        restored = Runtime(reopened)
        assert restored.operation(identifier, principal)['output'] == plaintext
        assert TaskService(restored).get(identifier, principal) == expected
        assert restored.operation_result(reopened.one('SELECT * FROM operations WHERE id=?', (identifier,)))['data']['output'] == plaintext
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_live_tail_cannot_spoof_cipher_prefix_and_restarts(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime, principal, tasks)
    text = PREFIX + 'not-ciphertext'
    assert runtime.outputs.receive(identifier, {'snapshot': True, 'seq': 1, 'text': text})
    row = runtime.store.one('SELECT * FROM operation_output_tails WHERE operation_id=?', (identifier,))
    assert row['output'] == '' and row['sealed_output'].startswith(PREFIX)
    assert text not in row['sealed_output'] and row['output_chars'] == len(text)
    assert runtime.operation(identifier, principal)['output'] == text
    reopened = Store(runtime.store.directory)
    try:
        assert Runtime(reopened).operation(identifier, principal)['output'] == text
    finally:
        reopened.close()


def test_retention_plans_are_read_only_and_preserve_unknown(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        for state in ['succeeded', 'running', 'unknown', 'needs_review']:
            store.execute("INSERT INTO operations(id,actor,tool,args_summary,fingerprint,idem,state,result,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (state, 'fixture', 'exec', '{}', state, state, state, json.dumps({'ok': True}), 1, 1))
        store.audit('fixture', 'old-event')
        store.execute('UPDATE audit SET at=1')
        before = store.all('SELECT * FROM operations')
        plan = retention_plan(store, now=400 * 86400)
        assert [row['id'] for row in plan['operations']] == ['succeeded']
        assert plan['approval_required'] and not plan['automatic_delete_allowed']
        assert isinstance(plan['collaboration'], dict)
        assert plan['dry_run']
        assert store.all('SELECT * FROM operations') == before
        assert store.one('SELECT * FROM audit')
    finally:
        store.close()


@pytest.mark.parametrize('guarded', [False, True])
def test_json_media_index_accepts_encrypted_read_and_keeps_existing_expiry(tmp_path, guarded):
    store = Store(tmp_path / 'hub')
    try:
        runtime = Runtime(store)
        if guarded:
            runtime._purge_computer()
        else:
            store.execute("CREATE INDEX operations_media_expiry ON operations(json_extract(result,'$.data.computer_expires_at'))")
        store.close()
        store = Store(tmp_path / 'hub')
        runtime = Runtime(store)
        index = store.one("SELECT sql FROM sqlite_master WHERE type='index' AND name='operations_media_expiry'")
        assert bool(index) == guarded
        store.execute("INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,created,updated) VALUES ('media','fixture','read','{}','','running',1,1)")
        runtime.complete({'id': 'media'}, {'ok': True, 'data': {'text': 'private pixels',
            '_computer_content': [], 'computer_expires_at': 1}})
        row = store.one("SELECT * FROM operations WHERE id='media'")
        assert row['result'].startswith(PREFIX) and 'private pixels' not in row['result']
        runtime._purge_computer()
        value = runtime.operation_result(store.one("SELECT * FROM operations WHERE id='media'"))
        assert value['data']['media_expired'] and 'text' not in value['data']
    finally:
        store.close()
