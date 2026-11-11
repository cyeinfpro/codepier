"""Slow filesystem resolution must not block the Agent's event loop."""
import asyncio
import threading
import pytest

import agent.runner as runner
from agent.resource_queue import ResourceQueue
from agent.runner import Agent


def slot_agent():
    agent = object.__new__(Agent)
    agent.resources = ResourceQueue()
    agent.slot_owners = {}
    agent._waiting_writes = []
    agent._active_slots = {}
    agent.active_roots = set()
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_slow_project_claim_resolution_keeps_loop_live_and_cancellation_owned(tmp_path, monkeypatch, cancel):
    agent = slot_agent()
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    original = runner.canonical_path
    observed_threads = []

    def slow_path(path):
        observed_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(2):
            raise AssertionError('The event loop could not release a slow path resolver')
        return original(path)

    monkeypatch.setattr(runner, 'canonical_path', slow_path)
    admitted = asyncio.Event()

    async def acquire():
        async with agent.project_slot(tmp_path):
            admitted.set()

    work = asyncio.create_task(acquire())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .5)
        assert not admitted.is_set()
        assert observed_threads and observed_threads[0] != loop_thread
        assert not agent._active_slots and not agent.active_roots
        if cancel:
            work.cancel()
            await asyncio.sleep(0)
            assert not work.done()  # The resolver is settled before cancellation escapes.
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await work
            assert not admitted.is_set()
        else:
            await work
            assert admitted.is_set()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
    assert not agent.resources.active and not agent.resources.waiting
    assert not agent._active_slots and not agent.active_roots
    assert not agent._waiting_writes and not agent.slot_owners


@pytest.mark.asyncio
async def test_project_path_aliases_still_share_one_resource_claim(tmp_path):
    agent = slot_agent()
    alias = tmp_path / 'child' / '..'
    (tmp_path / 'child').mkdir()
    entered = asyncio.Event()

    async def second():
        async with agent.project_slot(alias):
            entered.set()

    async with agent.project_slot(tmp_path):
        work = asyncio.create_task(second())
        try:
            async with asyncio.timeout(2):
                while not agent.resources.waiting:
                    await asyncio.sleep(0)
            assert not entered.is_set()
        finally:
            if work.done():
                await work
    await asyncio.wait_for(work, 2)
    assert entered.is_set() and not agent.resources.active
