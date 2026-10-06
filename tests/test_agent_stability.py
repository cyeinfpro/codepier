"""Regression coverage for media cleanup starving the Agent event loop."""
import asyncio
import json
import sqlite3
import threading

import pytest

from agent.computer import Computer
from agent.journal import Journal
from agent.service_watchdog import is_supervised
from shared.computer_media import purge_database


@pytest.mark.parametrize('table', ['calls', 'operations'])
def test_cleanup_cost_does_not_grow_with_file_history(tmp_path, table):
    db = sqlite3.connect(tmp_path / 'history.sqlite')
    db.row_factory = sqlite3.Row
    try:
        db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, tool TEXT, result TEXT)')
        file_result = json.dumps({'ok': True, 'data': {'text': 'file contents ' * 1000}})
        db.executemany(f'INSERT INTO {table} VALUES (?,?,?)',
                       [(str(i), 'read', file_result) for i in range(3000)])
        # The one-time index migration is allowed to scan legacy history.
        assert purge_database(db, table, now=10) == 0
        for name, tool, expiry in [('desktop', 'computer_observe', 5), ('browser', 'browser_snapshot', 5),
                                   ('core', 'read', 5), ('future', 'read', 20)]:
            db.execute(f'INSERT INTO {table} VALUES (?,?,?)', (name, tool,
                json.dumps({'ok': True, 'data': {'text': 'private', 'computer_expires_at': expiry}})))
        ticks = 0
        def budget():
            nonlocal ticks
            ticks += 1
            return ticks > 10
        # Fail if a recurring sweep goes back to parsing/scanning file history.
        db.set_progress_handler(budget, 100)
        assert purge_database(db, table, now=10) == 3
        assert purge_database(db, table, now=10) == 0
        db.set_progress_handler(None, 0)
        assert db.execute(f'SELECT result FROM {table} WHERE id=?', ('0',)).fetchone()[0] == file_result
        for name in ['desktop', 'browser', 'core']:
            data = json.loads(db.execute(f'SELECT result FROM {table} WHERE id=?', (name,)).fetchone()[0])['data']
            assert data['media_expired'] and 'text' not in data
        assert purge_database(db, table, now=21) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_slow_cleanup_does_not_hold_journal_or_stop_lease_guard(tmp_path, monkeypatch):
    journal = Journal(tmp_path / 'state')
    manager = Computer(lambda: {}, tmp_path, journal=journal)
    started = threading.Event()
    release = threading.Event()
    checked = asyncio.Event()
    original_sleep = asyncio.sleep
    def slow_cleanup(db, table):
        started.set()
        assert release.wait(5)
    monkeypatch.setattr('agent.computer.purge_database', slow_cleanup)
    async def tick(delay):
        await original_sleep(.001)
    monkeypatch.setattr('agent.computer.asyncio.sleep', tick)
    def revoked(session):
        if started.is_set():
            checked.set()
        return False
    monkeypatch.setattr(manager, 'revoked', revoked)
    manager.session = {'fixture': True}
    manager.start_guard()
    try:
        await asyncio.wait_for(checked.wait(), 2)
        assert journal.lock.acquire(blocking=False)
        journal.lock.release()
        # WAL reader remains usable while the worker owns its write transaction.
        assert journal.status('missing')['status'] == 'missing'
        first = manager.media_cleanup
        await original_sleep(.04)
        assert manager.media_cleanup is first  # no overlapping cleanup workers
    finally:
        release.set()
        manager.session = None
        await manager.close()
        journal.db.close()


@pytest.mark.parametrize('service,enabled,expected', [
    ('com.codepier.agent', '', True), ('com.liangchanghua.remote-dev-agent', '', True),
    ('', '1', True), ('unrelated.app', '', False), ('', '', False),
])
def test_existing_service_enables_watchdog(monkeypatch, service, enabled, expected):
    monkeypatch.setenv('XPC_SERVICE_NAME', service)
    monkeypatch.setenv('CODEPIER_SUPERVISED', enabled)
    assert is_supervised() is expected
