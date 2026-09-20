"""Regression for queued overlapping writers: later writers cannot block earlier ones."""
import asyncio
import pytest
from tests.test_audit_agent import local_agent


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True])
async def test_queued_writers_finish_fifo_after_shared_reader(local_agent, nested):
    agent, root = local_agent
    nested_root = root / 'nested'
    nested_root.mkdir()
    events = []
    tasks = []
    async def writer(name, path):
        async with agent.project_slot(path, write=True):
            events.append(name)
            await asyncio.sleep(0)
    try:
        async with agent.project_slot(root, write=False):
            tasks.append(asyncio.create_task(writer('first', root)))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(writer('second', nested_root if nested else root)))
            await asyncio.sleep(0)
            assert len(agent._waiting_writes) == 2 and not events
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert events == ['first', 'second']
        assert not agent._active_slots and not agent._waiting_writes
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_first_writer_does_not_strand_next_writer_or_readers(local_agent):
    agent, root = local_agent
    tasks, events = [], []
    async def enter(name, write):
        async with agent.project_slot(root, write=write):
            events.append(name)
    try:
        async with agent.project_slot(root, write=False):
            tasks.append(asyncio.create_task(enter('cancelled', True)))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(enter('writer', True)))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(enter('reader', False)))
            await asyncio.sleep(0)
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*tasks[1:]), 1)
        assert events == ['writer', 'reader']
        assert not agent._active_slots and not agent._waiting_writes
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
