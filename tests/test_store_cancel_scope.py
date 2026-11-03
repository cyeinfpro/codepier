"""An ASGI cancel scope must drain a DB phase without busy-waiting or replay."""
from __future__ import annotations

import asyncio
import threading

import anyio
import pytest

from hub.store import Store


@pytest.mark.parametrize('worker_fails', [False, True], ids=['commit', 'error'])
@pytest.mark.parametrize('direct_cancels', [0, 2], ids=['scope-only', 'mixed-cancel'])
def test_level_cancellation_drains_once_without_spinning(
        tmp_path, monkeypatch, worker_fails, direct_cancels):
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
            shield_calls.append(future)
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
                            await store.run(phase)
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
