"""One DB thread drains trusted Agent receipts without starving other phases."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import sqlite3
import subprocess
import sys
import threading

import pytest

from hub.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'hub')
    yield instance
    instance.close()


async def queued(store, count):
    async with asyncio.timeout(5):
        while store.stats_snapshot()['worker']['queued'] < count:
            await asyncio.sleep(0)


async def blocked_worker(store):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10), 'fixture did not release the DB worker'

    task = asyncio.create_task(store.run(hold))
    assert await asyncio.to_thread(entered.wait, 5)
    return task, release


@pytest.mark.asyncio
async def test_control_receipt_passes_existing_normal_backlog(store):
    first, release = await blocked_worker(store)
    order = []
    tasks = [first]
    try:
        tasks.extend(asyncio.create_task(store.run(order.append, ('normal', index))) for index in range(12))
        await queued(store, 12)
        # The fallback makes the previous FIFO implementation reproduce the
        # observed receipt delay, rather than failing only on a missing method.
        control = getattr(store, 'run_control', store.run)
        tasks.append(asyncio.create_task(control(order.append, ('control', 0))))
        await queued(store, 13)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert order[0] == ('control', 0)
    assert [value for kind, value in order if kind == 'normal'] == list(range(12))


@pytest.mark.asyncio
async def test_control_burst_is_bounded_and_both_lanes_are_fifo(store):
    first, release = await blocked_worker(store)
    order = []
    tasks = [first]
    try:
        tasks.extend(asyncio.create_task(store.run(order.append, ('normal', index))) for index in range(4))
        await queued(store, 4)
        control = getattr(store, 'run_control', store.run)
        tasks.extend(asyncio.create_task(control(order.append, ('control', index))) for index in range(25))
        await queued(store, 29)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert order[:9] == [('control', index) for index in range(8)] + [('normal', 0)]
    assert [value for kind, value in order if kind == 'control'] == list(range(25))
    assert [value for kind, value in order if kind == 'normal'] == list(range(4))
    assert [order.index(('normal', index)) for index in range(4)] == [8, 17, 26, 28]


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_while', ['queued', 'running'])
async def test_repeated_control_cancellation_waits_for_accepted_write(store, cancel_while):
    first, release_first = await blocked_worker(store)
    entered, release_write = threading.Event(), threading.Event()

    def write():
        entered.set()
        assert release_write.wait(10)
        store.execute("INSERT INTO meta VALUES ('control-cancel','durable')")

    control = asyncio.create_task(store.run_control(write))
    try:
        await queued(store, 1)
        if cancel_while == 'running':
            release_first.set()
            assert await asyncio.to_thread(entered.wait, 5)
        control.cancel()
        await asyncio.sleep(0)
        control.cancel()
        await asyncio.sleep(0)
        assert not control.done()
        assert not release_write.is_set()
    finally:
        release_first.set()
        release_write.set()
        await first
    with pytest.raises(asyncio.CancelledError):
        await control
    assert store.one("SELECT value FROM meta WHERE key='control-cancel'")['value'] == 'durable'
    metrics = store.stats_snapshot()['worker']
    assert metrics['running'] == metrics['queued'] == metrics['failed'] == 0
    assert metrics['completed'] == 2


def test_control_and_normal_use_one_thread_and_keep_each_loop_context(store):
    context = ContextVar('control-review-context')

    async def caller(value):
        context.set(value)
        return await asyncio.gather(*(
            (store.run_control if index % 2 else store.run)(lambda: (context.get(), threading.get_ident()))
            for index in range(12)))

    with ThreadPoolExecutor(max_workers=2) as callers:
        left = callers.submit(asyncio.run, caller('left'))
        right = callers.submit(asyncio.run, caller('right'))
        a, b = left.result(timeout=10), right.result(timeout=10)
    assert {value for value, _ in a} == {'left'}
    assert {value for value, _ in b} == {'right'}
    assert len({thread for _, thread in a + b}) == 1
    assert threading.get_ident() not in {thread for _, thread in a + b}
    assert store.stats_snapshot()['worker']['completed'] == 24


@pytest.mark.asyncio
async def test_failed_control_phase_does_not_strand_either_lane(store):
    def fail():
        with store.transaction():
            store.execute("INSERT INTO meta VALUES ('control-failed','not committed')")
            raise ValueError('synthetic phase error')

    results = await asyncio.gather(store.run_control(fail), store.run(lambda: 'normal'),
                                   store.run_control(lambda: 'control'), return_exceptions=True)
    assert isinstance(results[0], ValueError)
    assert results[1:] == ['normal', 'control']
    assert store.one("SELECT * FROM meta WHERE key='control-failed'") is None
    metrics = store.stats_snapshot()['worker']
    assert metrics['completed'] == 3 and metrics['failed'] == 1
    assert metrics['queued'] == metrics['running'] == 0


@pytest.mark.asyncio
async def test_close_drains_both_lanes_even_when_close_awaiter_is_cancelled(store, monkeypatch):
    first, release = await blocked_worker(store)
    tasks = [first]
    closing = threading.Event()
    original = store._executor.shutdown

    def observe_shutdown(wait=True, *, cancel_futures=False):
        original(wait=False, cancel_futures=cancel_futures)
        closing.set()
        if wait:
            original(wait=True, cancel_futures=False)

    monkeypatch.setattr(store._executor, 'shutdown', observe_shutdown)
    close = None
    try:
        for lane in (store.run, store.run_control):
            for index in range(3):
                tasks.append(asyncio.create_task(lane(store.execute,
                    'INSERT INTO meta VALUES (?,?)', (f'{lane.__name__}-{index}', 'durable'))))
        await queued(store, 6)
        close = asyncio.create_task(store.aclose())
        assert await asyncio.to_thread(closing.wait, 5)
        for lane in (store.run, store.run_control):
            with pytest.raises(RuntimeError, match='shutdown'):
                await lane(lambda: 'not admitted')
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        await asyncio.gather(*tasks)
        if close is not None:
            outcome = (await asyncio.gather(close, return_exceptions=True))[0]
    assert isinstance(outcome, asyncio.CancelledError)
    with sqlite3.connect(store.state_directory / 'hub.sqlite3') as reader:
        assert reader.execute("SELECT count(*) FROM meta WHERE value='durable'").fetchone()[0] == 6
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        store.one('SELECT 1')
    metrics = store.stats_snapshot()['worker']
    assert metrics['completed'] == 7 and metrics['rejected'] == 2
    assert metrics['queued'] == metrics['running'] == 0


def test_interpreter_exit_drains_accepted_lanes_without_a_permanent_waiter(tmp_path):
    target = tmp_path / 'exit-result.txt'
    program = '''
import sys, time
from pathlib import Path
from hub.store_worker import StoreWorker
worker = StoreWorker()
target = Path(sys.argv[1])
worker.submit(time.sleep, .05)
def append(value):
    with target.open('a') as stream:
        stream.write(value + '\\n')
for index in range(12):
    worker.submit(append, 'normal-' + str(index))
    worker.submit_control(append, 'control-' + str(index))
# Intentionally rely on the normal ThreadPoolExecutor interpreter-exit drain.
'''
    result = subprocess.run([sys.executable, '-c', program, str(target)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    values = target.read_text().splitlines()
    assert len(values) == 24 and len(set(values)) == 24
    assert [item for item in values if item.startswith('normal-')] == [f'normal-{i}' for i in range(12)]
    assert [item for item in values if item.startswith('control-')] == [f'control-{i}' for i in range(12)]


@pytest.mark.asyncio
async def test_control_rejects_awaiting_while_holding_store_lock(store):
    with store.lock:
        with pytest.raises(RuntimeError, match='holding Store.lock'):
            await store.run_control(lambda: None)
