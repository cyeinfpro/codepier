"""Bounded DB queue measurement does not change Store cancellation semantics."""
import asyncio
from contextvars import ContextVar
import json
from threading import Event

import pytest

from hub.store import Store
from hub.worker_metrics import WorkerMetrics, percentiles


def test_worker_metrics_are_bounded_and_contain_no_query_or_identity():
    metrics = WorkerMetrics(sample_limit=3)
    for _ in range(10):
        started = metrics.start(metrics.submit())
        metrics.finish(started)
    snapshot = metrics.snapshot()
    assert snapshot['submitted'] == snapshot['completed'] == 10
    assert snapshot['sample_count'] == 3
    assert snapshot['queued'] == snapshot['running'] == 0
    assert set(snapshot['queue_ms']) == {'p50', 'p95', 'p99'}
    assert percentiles([]) == {'p50': None, 'p95': None, 'p99': None}


def test_small_samples_use_nearest_rank_without_hiding_the_tail():
    assert percentiles([1, 100]) == {'p50':1, 'p95':100, 'p99':100}
    assert percentiles(list(range(1, 101))) == {'p50':50, 'p95':95, 'p99':99}
    metrics = WorkerMetrics()
    metrics.start(metrics.submit())
    snapshot = metrics.snapshot()
    assert snapshot['percentile_method'] == 'nearest_rank'
    assert snapshot['sample_count'] == 1 and snapshot['phase_sample_count'] == 0
    assert snapshot['phase_ms']['p95'] is None


def test_store_measures_success_failure_and_preserves_context(tmp_path):
    store = Store(tmp_path/'hub')
    marker = ContextVar('fixture', default='absent')
    async def run():
        marker.set('fixture-secret-not-a-metric')
        assert await store.run(marker.get) == 'fixture-secret-not-a-metric'
        with pytest.raises(ValueError, match='fixture-error'):
            await store.run(lambda: (_ for _ in ()).throw(ValueError('fixture-error')))
    try:
        asyncio.run(run())
        value = store.worker_metrics.snapshot()
        assert value['submitted'] == value['completed'] == 2
        assert value['failed'] == 1
        assert 'fixture-secret' not in json.dumps(value)
        assert 'fixture-error' not in json.dumps(value)
    finally:
        store.close()


def test_cancelled_waiter_still_settles_the_original_db_phase(tmp_path):
    store = Store(tmp_path/'hub')
    started, release = Event(), Event()
    effects = []
    def phase():
        started.set()
        assert release.wait(3)
        effects.append('one')
    async def run():
        task = asyncio.create_task(store.run(phase))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        second = asyncio.create_task(store.run(lambda: effects.append('two')))
        await asyncio.sleep(0)
        value = store.worker_metrics.snapshot()
        assert value['running'] == 1 and value['queued'] == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await second
    try:
        asyncio.run(run())
        assert effects == ['one', 'two']
        value = store.worker_metrics.snapshot()
        assert value['submitted'] == value['completed'] == 2
        assert value['queued'] == value['running'] == 0
    finally:
        release.set()
        store.close()
