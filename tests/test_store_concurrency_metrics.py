"""Bounded lock diagnosis and authorized project SQL query plans."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from hub import iam
from hub.store import Store
from hub.store_contract import OwnedRLock


def test_store_lock_observes_outermost_hold_without_sql_or_sensitive_labels(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        before = store.stats_snapshot()['lock']['count']
        with store.lock:
            with store.lock:
                store.one("SELECT 'private fixture path' AS value")
        after = store.stats_snapshot()
        assert after['lock']['count'] == before + 1
        assert after['lock']['sample_count'] <= after['lock']['sample_limit']
        assert after['lock']['wait_ms']['p50'] >= 0
        assert after['lock']['held_ms']['p50'] >= 0
        assert 'private fixture path' not in json.dumps(after)
        assert store.stats_snapshot() == after
    finally:
        store.close()


def test_lock_metrics_are_bounded_detached_and_observer_cannot_break_lock():
    from hub.store_metrics import LockMetrics
    metrics = LockMetrics(sample_limit=3)
    for index in range(1, 11):
        metrics.observe(index, index * 2)
    snapshot = metrics.snapshot()
    assert snapshot['count'] == 10 and snapshot['sample_count'] == 3
    assert snapshot['wait_ms'] == {'p50': 9, 'p95': 10, 'p99': 10}
    assert snapshot['held_ms'] == {'p50': 18, 'p95': 20, 'p99': 20}
    snapshot['held_ms']['p99'] = -1
    assert metrics.snapshot()['held_ms']['p99'] == 20
    with pytest.raises(ValueError):
        LockMetrics(sample_limit=0)

    def failed_observer(*_):
        raise ValueError('fixture measurement unavailable')
    lock = OwnedRLock(on_release=failed_observer)
    with lock:
        assert lock.owned
    assert not lock.owned
    with lock:
        assert lock.owned


def test_contention_reports_one_sample_per_owner_and_retains_thread_ownership(tmp_path):
    store = Store(tmp_path / 'hub')
    attempted, entered = Event(), Event()
    try:
        before = store.stats_snapshot()['lock']['count']
        def contender():
            attempted.set()
            with store.lock:
                entered.set()
                with store.lock:
                    assert store.lock.owned
                assert store.one('SELECT 1 AS value')['value'] == 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.lock:
                future = executor.submit(contender)
                assert attempted.wait(2)
                assert not entered.wait(.02)
            future.result(timeout=2)
        measured = store.stats_snapshot()['lock']
        assert measured['count'] == before + 2
        assert measured['max_wait_ms'] >= 10
        assert measured['max_held_ms'] >= 10
        assert not store.lock.owned
    finally:
        store.close()


def test_project_indexes_bound_authorized_history_and_active_count_work(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        definitions = store.all("SELECT sql FROM sqlite_master WHERE name LIKE 'operations_project_%'")
        assert len(definitions) == 2
    finally:
        store.close()
    with sqlite3.connect(tmp_path / 'fixture.sqlite3') as db:
        db.execute('''CREATE TABLE operations(id TEXT PRIMARY KEY,space_id TEXT,project_id TEXT,
            created REAL,state TEXT,owner_user_id TEXT,visibility TEXT,grant_id TEXT,payload TEXT)''')
        db.execute('CREATE INDEX legacy_space ON operations(space_id)')
        db.executemany('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)', (
            (str(index), 'team', 'target' if index % 101 == 0 else 'unrelated', float(index),
             'running' if index % 3 else 'succeeded', 'owner' if index % 2 else 'other',
             'space' if index % 5 else 'private', 'grant' if index % 7 else 'different', 'x' * 512)
            for index in range(10100)))
        # Same live privacy predicate used by list_operations/operation_rows;
        # both user-owned and grant-scoped callers retain their restrictions.
        queries = []
        for caller in (SimpleNamespace(space_id='team', grant_id=None, instance_admin=False, user_id='owner'),
                       SimpleNamespace(space_id='team', grant_id='grant', instance_admin=False)):
            clause, values = iam.private_sql(caller)
            queries.append(('SELECT id,project_id,state,created FROM operations WHERE ' + clause +
                            ' AND project_id=? AND created<? ORDER BY created DESC LIMIT 20', (*values, 'target', 9500)))
            queries.append(('SELECT count(*) FROM operations WHERE ' + clause +
                            " AND project_id=? AND state IN ('queued','running','reconnecting','cancelling')",
                            (*values, 'target')))

        def measure(sql, args):
            instructions = [0]
            def progress():
                instructions[0] += 1
                return 0
            db.set_progress_handler(progress, 100)
            try:
                rows = db.execute(sql, args).fetchall()
            finally:
                db.set_progress_handler(None, 0)
            plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, args)]
            return rows, instructions[0], plan

        before = [measure(*query) for query in queries]
        for definition in definitions:
            db.execute(definition['sql'])
        after = [measure(*query) for query in queries]
        for old, new in zip(before, after, strict=True):
            assert old[0] == new[0]
            assert new[1] < old[1] / 5
            assert any('operations_project_' in part for part in new[2])
