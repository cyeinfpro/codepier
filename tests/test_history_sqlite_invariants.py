"""Pure SQLite frozen receipts, including interrupted synthetic maintenance."""
import os
import sqlite3
import subprocess
import sys

import pytest

from hub.operation_history import rewrite_task_ciphertext, seal, unseal
from tests import test_mcp_tasks

task_runtime = test_mcp_tasks.task_runtime


async def frozen(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await test_mcp_tasks.create_task(runtime, principal, tasks)
    runtime.complete({'id': identifier}, {'ok': True, 'data': {
        'output': 'synthetic frozen output', 'exit_code': 0, 'command_ok': True}})
    return runtime, identifier, tasks.get(identifier, principal)


def strict_schema(store):
    rows = store.all("SELECT sql FROM sqlite_master WHERE name IN "
                     "('mcp_tasks_terminal_immutable','mcp_tasks_terminal_result_immutable')")
    assert len(rows) == 2
    assert all('history_same_body' not in row['sql'] and 'AND NOT (' not in row['sql'] for row in rows)


@pytest.mark.asyncio
async def test_raw_sqlite_state_updates_and_frozen_receipt_guards(task_runtime):
    runtime, identifier, expected = await frozen(task_runtime)
    store = runtime.store
    with sqlite3.connect(store.state_directory/'hub.sqlite3') as raw:
        raw.execute("UPDATE operations SET state='unknown',transport_error='synthetic' WHERE id=?", (identifier,))
        raw.execute("UPDATE operations SET state='succeeded' WHERE id=?", (identifier,))
        for column in ('terminal_operation', 'terminal_result'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                raw.execute(f'UPDATE mcp_tasks SET {column}=? WHERE task_id=?', ('{}', identifier))
    assert task_runtime[2].get(identifier, task_runtime[1]) == expected
    strict_schema(store)


@pytest.mark.asyncio
async def test_authenticated_rewrite_rejects_changes_and_blocks_other_writers(task_runtime, monkeypatch):
    runtime, identifier, _ = await frozen(task_runtime)
    store = runtime.store
    original = store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result']
    encrypted = seal(store, unseal(store, original))
    with store.lock, store.db:
        with pytest.raises(RuntimeError, match='explicit writer transaction'):
            rewrite_task_ciphertext(store, identifier, 'terminal_result', encrypted)
    with store.transaction():
        with pytest.raises(ValueError, match='preserve'):
            rewrite_task_ciphertext(store, identifier, 'terminal_result', seal(store, '{}'))
    execute = store.execute
    observed = []
    def update(sql, args=()):
        if sql.startswith('UPDATE mcp_tasks SET terminal_result='):
            # Even this connection cannot alter the body under the one-pair exception.
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                with store.savepoint():
                    execute('UPDATE mcp_tasks SET terminal_result=? WHERE task_id=?',
                            (seal(store, '{}'), identifier))
            with sqlite3.connect(store.state_directory/'hub.sqlite3', timeout=0) as other:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    other.execute('UPDATE mcp_tasks SET terminal_result=? WHERE task_id=?', ('{}', identifier))
            observed.append(True)
        return execute(sql, args)
    monkeypatch.setattr(store, 'execute', update)
    with store.transaction():
        rewrite_task_ciphertext(store, identifier, 'terminal_result', encrypted)
    assert observed == [True]
    assert store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result'] == encrypted
    strict_schema(store)


@pytest.mark.asyncio
async def test_failed_cipher_rewrite_restores_schema_and_original_bytes(task_runtime, monkeypatch):
    runtime, identifier, _ = await frozen(task_runtime)
    store = runtime.store
    original = store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result']
    execute = store.execute
    def fail(sql, args=()):
        if sql.startswith('UPDATE mcp_tasks SET terminal_result='):
            raise RuntimeError('synthetic write failure')
        return execute(sql, args)
    monkeypatch.setattr(store, 'execute', fail)
    with pytest.raises(RuntimeError, match='synthetic write failure'):
        with store.transaction():
            rewrite_task_ciphertext(store, identifier, 'terminal_result', seal(store, unseal(store, original)))
    assert store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result'] == original
    strict_schema(store)


@pytest.mark.asyncio
async def test_process_crash_rolls_back_guard_exception_and_data(task_runtime):
    runtime, identifier, _ = await frozen(task_runtime)
    store = runtime.store
    original = store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result']
    script = """
import os,sys
from hub.store import Store
from hub.operation_history import rewrite_task_ciphertext,seal,unseal
s=Store(sys.argv[1])
identifier=sys.argv[2]
old=s.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?',(identifier,))['terminal_result']
execute=s.execute
def crash(sql,args=()):
    if sql.startswith('UPDATE mcp_tasks SET terminal_result='):
        os._exit(71)
    return execute(sql,args)
s.execute=crash
with s.transaction(immediate=True):
    rewrite_task_ciphertext(s,identifier,'terminal_result',seal(s,unseal(s,old)))
"""
    completed = subprocess.run([sys.executable, '-c', script, str(store.directory), identifier],
                               env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=15)
    assert completed.returncode == 71
    assert store.one('SELECT terminal_result FROM mcp_tasks WHERE task_id=?', (identifier,))['terminal_result'] == original
    strict_schema(store)
