"""Numeric Store observation preserves nested transactions and serialization."""
import asyncio
import json
import sqlite3
from threading import Event

import pytest

from hub.store import Store
from hub.store_contract import TransactionAborted
from hub.store_metrics import TransactionMetrics


def test_transaction_metrics_are_bounded_and_snapshot_is_detached():
    metrics = TransactionMetrics(sample_limit=3)
    for duration in range(1, 11):
        metrics.observe(duration, 'committed')
    snapshot = metrics.snapshot()
    assert snapshot['count'] == snapshot['committed'] == 10
    assert snapshot['sample_count'] == 3
    assert snapshot['duration_ms'] == {'p50': 9, 'p95': 10, 'p99': 10}
    snapshot['duration_ms']['p99'] = 999
    assert metrics.snapshot()['duration_ms']['p99'] == 10
    with pytest.raises(ValueError):
        TransactionMetrics(sample_limit=None)


def test_only_outer_transaction_counts_and_caught_inner_error_rolls_back(tmp_path):
    store = Store(tmp_path / 'hub')
    callbacks = []
    try:
        before = store.stats_snapshot()['transactions']
        with store.transaction():
            store.execute("INSERT INTO meta VALUES ('metric-private-key','metric-private-value')")
            with store.transaction():
                assert store.one("SELECT value FROM meta WHERE key='metric-private-key'")
            store.after_commit(lambda: callbacks.append('committed'))
        after_commit = store.stats_snapshot()['transactions']
        assert after_commit['count'] == before['count'] + 1
        assert after_commit['committed'] == before['committed'] + 1
        assert callbacks == ['committed']
        with pytest.raises(TransactionAborted):
            with store.transaction():
                store.execute("INSERT INTO meta VALUES ('must-rollback','private-value')")
                try:
                    with store.transaction():
                        raise ValueError('private-error-not-a-label')
                except ValueError:
                    pass
                store.after_commit(lambda: callbacks.append('wrong'))
        after = store.stats_snapshot()
        assert after['transactions']['count'] == before['count'] + 2
        assert after['transactions']['rolled_back'] == before['rolled_back'] + 1
        assert store.one("SELECT value FROM meta WHERE key='must-rollback'") is None
        assert callbacks == ['committed']
        assert 'private' not in json.dumps(after)
        changes = store.one('SELECT total_changes() AS n')['n']
        assert store.stats_snapshot() == after
        assert store.one('SELECT total_changes() AS n')['n'] == changes
    finally:
        store.close()


def test_commit_failure_and_post_commit_callback_error_are_distinct(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        store.execute('CREATE TABLE metrics_parent (id INTEGER PRIMARY KEY)')
        store.execute('CREATE TABLE metrics_child (id INTEGER REFERENCES metrics_parent(id) DEFERRABLE INITIALLY DEFERRED)')
        before = store.stats_snapshot()['transactions']
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction():
                store.execute('INSERT INTO metrics_child VALUES (42)')
        failed = store.stats_snapshot()['transactions']
        assert failed['commit_failed'] == before['commit_failed'] + 1
        assert not store.all('SELECT * FROM metrics_child')

        def broken_callback():
            raise RuntimeError('private-callback-failure')
        with pytest.raises(RuntimeError, match='private-callback-failure'):
            with store.transaction():
                store.execute('INSERT INTO metrics_parent VALUES (42)')
                store.after_commit(broken_callback)
        finished = store.stats_snapshot()['transactions']
        assert finished['committed'] == failed['committed'] + 1
        assert finished['rolled_back'] == failed['rolled_back']
        assert finished['commit_failed'] == failed['commit_failed']
        assert store.one('SELECT id FROM metrics_parent')['id'] == 42
    finally:
        store.close()


def test_observer_failure_cannot_change_commit_or_after_commit_callbacks(tmp_path, monkeypatch):
    store = Store(tmp_path / 'hub')
    try:
        callbacks = []
        def failing_observer(duration, outcome):
            raise RuntimeError('telemetry-unavailable')
        monkeypatch.setattr(store.db, '_on_transaction', failing_observer)
        with store.transaction():
            store.execute("INSERT INTO meta VALUES ('observed-effect','committed')")
            store.after_commit(lambda: callbacks.append('committed'))
        assert store.one("SELECT value FROM meta WHERE key='observed-effect'")['value'] == 'committed'
        assert callbacks == ['committed']
    finally:
        store.close()


def test_concurrent_phases_keep_single_worker_and_count_rollback(tmp_path):
    store = Store(tmp_path / 'hub')
    started, release = Event(), Event()
    order = []
    before = store.stats_snapshot()['transactions']

    def first_phase():
        with store.transaction():
            store.execute("INSERT INTO meta VALUES ('first-phase','not-persisted')")
            started.set()
            assert release.wait(3)
            order.append('first')
            raise ValueError('rollback-phase')

    def second_phase():
        with store.transaction():
            assert store.one("SELECT value FROM meta WHERE key='first-phase'") is None
            store.execute("INSERT INTO meta VALUES ('second-phase','persisted')")
            order.append('second')

    async def run():
        first = asyncio.create_task(store.run(first_phase))
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(store.run(second_phase))
        await asyncio.sleep(0)
        busy = store.stats_snapshot()
        assert busy['worker']['running'] == 1 and busy['worker']['queued'] == 1
        release.set()
        with pytest.raises(ValueError, match='rollback-phase'):
            await first
        await second

    try:
        asyncio.run(run())
        after = store.stats_snapshot()
        assert order == ['first', 'second']
        assert after['worker']['completed'] == 2 and after['worker']['failed'] == 1
        assert after['worker']['running'] == after['worker']['queued'] == 0
        assert after['transactions']['count'] == before['count'] + 2
        assert after['transactions']['rolled_back'] == before['rolled_back'] + 1
        assert after['transactions']['committed'] == before['committed'] + 1
    finally:
        release.set()
        store.close()


def test_rejected_executor_submission_does_not_leave_a_phantom_queue(tmp_path):
    store = Store(tmp_path / 'hub')
    store.close()
    async def run():
        with pytest.raises(RuntimeError):
            await store.run(lambda: 'never-admitted')
    asyncio.run(run())
    stats = store.stats_snapshot()
    assert stats['worker']['rejected'] == 1
    assert stats['worker']['accepted'] == 0
    assert stats['worker']['queued'] == stats['worker']['running'] == 0
