"""Atomic non-waiting admission: fairness and cleanup, not relaxed budgets."""
import asyncio

import pytest

from agent.resource_queue import Claim, ResourceQueue
from agent.scheduler import ProjectScheduler
from shared.util import DevError


@pytest.mark.asyncio
async def test_resource_try_respects_conflicting_waiter_and_duplicate():
    queue = ResourceQueue()
    read = [Claim('agent', 'path', '/project/file', False)]
    write = [Claim('agent', 'path', '/project', True)]
    token = queue.try_reserve('first', read)
    assert token is not None
    assert queue.try_reserve('first', read) is None
    queued = asyncio.Event()
    async def writer():
        async with queue.slot('writer', write, lambda _: queued.set()):
            pass
    task = asyncio.create_task(writer())
    await asyncio.wait_for(queued.wait(), 1)
    assert queue.try_reserve('late-read', read) is None
    unrelated = queue.try_reserve('other', [Claim('agent', 'path', '/other', False)])
    assert unrelated is not None
    await queue.release(unrelated)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await queue.release(token)
    assert not queue.active and not queue.waiting


@pytest.mark.asyncio
async def test_resource_try_condition_busy_has_no_side_effect():
    queue = ResourceQueue()
    async with queue.condition:
        assert queue.try_reserve('busy', []) is None
    assert not queue.active and not queue.waiting


@pytest.mark.asyncio
async def test_scheduler_conversion_failure_is_atomic_and_fair():
    scheduler = ProjectScheduler()
    waiting = asyncio.Event()
    async with scheduler.slot('preflight', 'a', 'read') as original:
        # Hold the existing condition to model another current owner.
        async with scheduler.condition:
            assert scheduler.try_reserve('preflight', 'b', 'read', replace=original) is None
        assert scheduler.active[original.token] is original
        # An older eligible ticket must win the same lane/project turn.
        from agent.scheduler import Ticket
        older = Ticket(object(), 'older', 'b', 'read', '')
        scheduler.waiting.append(older)
        before = (scheduler.sequence, dict(scheduler.turn))
        assert scheduler.try_reserve('preflight', 'b', 'read', replace=original) is None
        assert (scheduler.sequence, dict(scheduler.turn)) == before
        assert scheduler.active[original.token] is original
        assert scheduler.waiting == [older]
        scheduler.waiting.remove(older)
        converted = scheduler.try_reserve('preflight', 'b', 'execution', replace=original)
        assert converted is not None and converted.token is original.token
        assert scheduler.try_reserve('preflight', 'b', 'read', replace=None) is None
        assert scheduler.try_reserve('preflight', 'b', 'read', replace=original) is None
        waiting.set()
    assert waiting.is_set()
    assert not scheduler.active and not scheduler.waiting


@pytest.mark.asyncio
async def test_scheduler_conversion_wakes_read_waiter_before_execution_finishes():
    scheduler = ProjectScheduler()
    scheduler.config['read_limit'] = 3
    acquired = asyncio.Event()
    finish = asyncio.Event()
    async def reader():
        async with scheduler.slot('reader', 'b', 'read'):
            acquired.set()
            await finish.wait()
    async with scheduler.slot('preflight', 'a', 'read') as original:
        async with scheduler.slot('b-holder', 'b', 'read'), scheduler.slot('c-holder', 'c', 'read'):
            task = asyncio.create_task(reader())
            while not scheduler.waiting:
                await asyncio.sleep(0)
            # Existing fairness favors a zero-count execution lane over b's
            # second reader; once converted the reader must wake immediately.
            converted = scheduler.try_reserve('preflight', 'a', 'execution', replace=original)
            assert converted is not None
            await asyncio.wait_for(acquired.wait(), 1)
            assert scheduler.active[original.token].lane == 'execution'
            finish.set()
            await task
    assert not scheduler.active and not scheduler.waiting


@pytest.mark.asyncio
async def test_scheduler_try_observes_current_capacity_shutdown_and_project_cap():
    scheduler = ProjectScheduler()
    async with scheduler.slot('preflight', 'a', 'read') as original:
        before = (scheduler.sequence, dict(scheduler.turn))
        scheduler.capacity.limit = 0
        assert scheduler.try_reserve('preflight', 'a', 'execution', replace=original) is None
        assert (scheduler.sequence, dict(scheduler.turn)) == before
        scheduler.capacity.limit = 8
        scheduler.config['project_limits']['b'] = 1
        async with scheduler.slot('holder', 'b', 'execution'):
            assert scheduler.try_reserve('preflight', 'b', 'execution', replace=original) is None
        await scheduler.close()
        assert scheduler.try_reserve('preflight', 'a', 'execution', replace=original) is None
    assert not scheduler.active


@pytest.mark.asyncio
async def test_scheduler_try_preserves_multi_project_selected_order():
    from agent.scheduler import Ticket
    scheduler = ProjectScheduler()
    async with scheduler.slot('preflight', 'a', 'read') as original:
        # Original _selected fairness favors project b over a with an active reader.
        async with scheduler.slot('a-reader', 'a', 'read'):
            older = Ticket(object(), 'b-queued', 'b', 'read', '')
            scheduler.waiting.append(older)
            assert scheduler.try_reserve('preflight', 'a', 'read', replace=original) is None
            assert scheduler._selected() is older
            scheduler.waiting.remove(older)
    assert not scheduler.active and not scheduler.waiting


@pytest.mark.asyncio
async def test_scheduler_close_drains_conversion_notifications():
    scheduler = ProjectScheduler()
    async with scheduler.slot('preflight', 'a', 'read') as original:
        assert scheduler.try_reserve('preflight', 'a', 'execution', replace=original)
        assert scheduler._notifications
        await scheduler.close()
        assert not scheduler._notifications
        assert scheduler.try_reserve('new', 'b', 'read', replace=original) is None
    assert not scheduler.active
