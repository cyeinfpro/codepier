"""Repeated cancellation cannot release file claims before a thread commits."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent.resource_queue import Claim, ResourceQueue
from agent.runner import Agent


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['write', 'artifacts_register', 'fixture_engine_call'])
async def test_repeated_cancel_preserves_claim_and_actual_result(monkeypatch, tool):
    import agent.runner as runner
    started = threading.Event()
    finish = threading.Event()
    committed = []
    def call(*args):
        started.set()
        if not finish.wait(5):
            raise RuntimeError('test worker was not released')
        committed.append('committed')
        return {'committed': True}
    engine = SimpleNamespace(call=call)
    agent = SimpleNamespace(engine=engine, artifacts=SimpleNamespace(register=call),
                            _committed_io=Agent._committed_io)
    monkeypatch.setattr(runner.workspace_probe, 'eligible', lambda *args: False)
    monkeypatch.setattr(runner, 'CoreFiles', lambda engine: SimpleNamespace(call=call))
    queue = ResourceQueue(semantics='relaxed')
    claim = Claim('agent', 'path', '/synthetic/file', True)
    async def owner():
        async with queue.slot('writer', [claim], lambda _: None):
            return await Agent.execute(agent, 'writer', tool, {}, {})
    async def follower():
        async with queue.slot('follower', [claim], lambda _: None):
            assert committed == ['committed']
            return True
    task = asyncio.create_task(owner())
    other = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        other = asyncio.create_task(follower())
        await asyncio.sleep(0)
        for _ in range(4):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert not other.done()
            assert [entry[0] for entry in queue.active.values()] == ['writer']
        finish.set()
        assert await asyncio.wait_for(task, 2) == {'committed': True}
        assert await asyncio.wait_for(other, 2)
        assert not queue.active and not queue.waiting
    finally:
        finish.set()
        await asyncio.gather(task, *([other] if other else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancel_preserves_worker_error():
    started, finish = threading.Event(), threading.Event()
    def fail():
        started.set()
        finish.wait(5)
        raise ValueError('synthetic commit failure')
    task = asyncio.create_task(Agent._committed_io(fail))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        finish.set()
        with pytest.raises(ValueError, match='synthetic commit failure'):
            await task
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
