"""An ASGI cancel scope must drain a DB phase without busy-waiting or replay."""
from __future__ import annotations

import asyncio
import gc
import sqlite3
import threading

import anyio
import pytest

from hub.store import Store


@pytest.mark.parametrize('worker_fails', [False, True], ids=['commit', 'error'])
@pytest.mark.parametrize('direct_cancels', [0, 2], ids=['scope-only', 'mixed-cancel'])
@pytest.mark.parametrize('lane', ['run', 'run_control'], ids=['normal', 'control'])
def test_level_cancellation_drains_once_without_spinning(
        tmp_path, monkeypatch, worker_fails, direct_cancels, lane):
    store = Store(tmp_path / 'store')
    release = threading.Event()
    real_shield = asyncio.shield
    shield_calls = []
    effects, cancellations, unhandled = [], [], []

    async def exercise():
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        started, draining, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        admission = asyncio.Lock()
        owner_tasks = []

        def counted_shield(future):
            # Do not retain the Future: an unconsumed worker exception must be
            # observable by the loop's exception handler after the owner exits.
            shield_calls.append(None)
            if len(shield_calls) >= 2:
                draining.set()
            return real_shield(future)

        monkeypatch.setattr(asyncio, 'shield', counted_shield)

        def phase():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError('Test did not release the admitted database phase')
            effects.append('executed')
            if worker_fails:
                raise ValueError('Synthetic failure after cancellation')
            store.execute("INSERT INTO meta(key,value) VALUES ('cancel-scope-proof','once')")
            return 'committed'

        async def owner(*, task_status=anyio.TASK_STATUS_IGNORED):
            owner_tasks.append(asyncio.current_task())
            try:
                with anyio.CancelScope() as scope:
                    task_status.started(scope)
                    async with admission:
                        try:
                            await getattr(store, lane)(phase)
                        except asyncio.CancelledError:
                            cancellations.append('cancelled')
                            raise
            finally:
                finished.set()

        try:
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    scope = await tasks.start(owner)
                    await started.wait()
                    scope.cancel()
                    await draining.wait()
                    # Direct asyncio cancellation can also arrive while the
                    # AnyIO parent scope remains cancelled. Neither may cancel
                    # the worker, release admission early, or resubmit a write.
                    for _ in range(direct_cancels):
                        owner_tasks[0].cancel()
                        await asyncio.sleep(0)
                    # Count await attempts, not elapsed time or CPU speed.
                    # A cancelled AnyIO scope otherwise cancels every new await.
                    for _ in range(40):
                        await asyncio.sleep(0)
                    calls_while_blocked = len(shield_calls)
                    held_until_settled = admission.locked() and not finished.is_set()
                    effects_before_release = list(effects)
                    release.set()
                    await finished.wait()
                await asyncio.sleep(0)
                gc.collect()
                await asyncio.sleep(0)
            assert held_until_settled
            assert effects_before_release == []
            assert effects == ['executed']
            assert cancellations == ['cancelled']
            assert not admission.locked()
            assert not unhandled
            assert calls_while_blocked <= 3 + direct_cancels, (
                f'Cancelled DB waiter busy-looped {calls_while_blocked} times')
            row = store.one("SELECT value FROM meta WHERE key='cancel-scope-proof'")
            assert (row['value'] if row else None) == (None if worker_fails else 'once')
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

    try:
        anyio.run(exercise, backend='asyncio')
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize('close_fails', [False, True], ids=['closed', 'close-error'])
@pytest.mark.parametrize('direct_cancels', [0, 2], ids=['scope-only', 'mixed-cancel'])
def test_level_cancellation_of_close_drains_both_lanes_without_spinning(
        tmp_path, monkeypatch, close_fails, direct_cancels):
    store = Store(tmp_path / 'store')
    release = threading.Event()
    real_close, real_shutdown, real_shield = store.close, store._executor.shutdown, asyncio.shield
    writes, close_calls, close_effects, cancellations, unhandled = [], [], [], [], []
    shield_calls = []

    async def exercise():
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        started, closing, draining, finished = (asyncio.Event() for _ in range(4))
        admission = asyncio.Lock()
        owner_tasks, workers = [], []

        def hold_transaction():
            with store.transaction():
                loop.call_soon_threadsafe(started.set)
                assert release.wait(3), 'Test did not release the active database transaction'

        def write(lane):
            store.execute('INSERT INTO meta(key,value) VALUES (?,?)', (lane, 'once'))
            writes.append(lane)

        def observed_shutdown(wait=True, *, cancel_futures=False):
            # Observe the actual stop-admission boundary, then wait for the real
            # worker to drain both accepted queues before SQLite can be closed.
            real_shutdown(wait=False, cancel_futures=cancel_futures)
            loop.call_soon_threadsafe(closing.set)
            if wait:
                real_shutdown(wait=True, cancel_futures=False)

        def observed_close():
            close_calls.append('close')
            real_close()
            close_effects.append('closed')
            if close_fails:
                raise ValueError('Synthetic close failure after database shutdown')

        def counted_shield(future):
            if owner_tasks and asyncio.current_task() is owner_tasks[0]:
                shield_calls.append(None)
                if len(shield_calls) >= 2:
                    draining.set()
            return real_shield(future)

        async def owner(*, task_status=anyio.TASK_STATUS_IGNORED):
            owner_tasks.append(asyncio.current_task())
            try:
                with anyio.CancelScope() as scope:
                    task_status.started(scope)
                    async with admission:
                        try:
                            await store.aclose()
                        except asyncio.CancelledError:
                            cancellations.append('cancelled')
                            raise
            finally:
                finished.set()

        monkeypatch.setattr(store._executor, 'shutdown', observed_shutdown)
        monkeypatch.setattr(store, 'close', observed_close)
        monkeypatch.setattr(asyncio, 'shield', counted_shield)
        try:
            with anyio.fail_after(5):
                workers.append(asyncio.create_task(store.run(hold_transaction)))
                await started.wait()
                for lane in (store.run, store.run_control):
                    workers.append(asyncio.create_task(lane(write, lane.__name__)))
                while store.stats_snapshot()['worker']['queued'] < 2:
                    await asyncio.sleep(0)
                async with anyio.create_task_group() as tasks:
                    scope = await tasks.start(owner)
                    await closing.wait()
                    for lane in (store.run, store.run_control):
                        with pytest.raises(RuntimeError, match='shutdown'):
                            await lane(lambda: 'not admitted')
                    scope.cancel()
                    await draining.wait()
                    for _ in range(direct_cancels):
                        owner_tasks[0].cancel()
                        await asyncio.sleep(0)
                    for _ in range(40):
                        await asyncio.sleep(0)
                    calls_while_blocked = len(shield_calls)
                    held_until_settled = admission.locked() and not finished.is_set()
                    effects_before_release = (list(writes), list(close_effects))
                    release.set()
                    await finished.wait()
                assert await asyncio.gather(*workers) == [None, None, None]
                gc.collect()
                await asyncio.sleep(0)
            assert held_until_settled
            assert effects_before_release == ([], [])
            assert sorted(writes) == ['run', 'run_control']
            assert close_calls == ['close'] and close_effects == ['closed']
            assert cancellations == ['cancelled']
            assert not admission.locked()
            assert not unhandled
            assert calls_while_blocked <= 3 + direct_cancels, (
                f'Cancelled close waiter busy-looped {calls_while_blocked} times')
            with sqlite3.connect(store.state_directory / 'hub.sqlite3') as reader:
                rows = reader.execute(
                    "SELECT key,value FROM meta WHERE key IN ('run','run_control') ORDER BY key").fetchall()
            assert rows == [('run', 'once'), ('run_control', 'once')]
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                store.one('SELECT 1')
            metrics = store.stats_snapshot()['worker']
            assert metrics['completed'] == 3 and metrics['rejected'] == 2
            assert metrics['queued'] == metrics['running'] == metrics['failed'] == 0
        finally:
            release.set()
            await asyncio.gather(*workers, return_exceptions=True)
            monkeypatch.setattr(store._executor, 'shutdown', real_shutdown)
            loop.set_exception_handler(previous_handler)

    try:
        anyio.run(exercise, backend='asyncio')
    finally:
        release.set()
        real_close()
