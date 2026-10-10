"""Deterministic wake races; no callback network, model, or new dispatcher."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from hub.collaboration.lifecycle import CollaborationLoops


def service(enabled=True):
    return SimpleNamespace(config=SimpleNamespace(enabled=enabled,events_enabled=enabled,collector_enabled=False),
                           health={},clock=lambda:1)


def configured_loop():
    loops = CollaborationLoops(service())
    loops.started = True
    loops._event_loop = asyncio.get_running_loop()
    loops._event_wake = asyncio.Event()
    return loops


@pytest.mark.asyncio
async def test_commit_during_tick_is_not_lost_or_parallel():
    loops = configured_loop()
    first, second, release = asyncio.Event(),asyncio.Event(),asyncio.Event()
    calls, active, peak = 0,0,0
    async def tick():
        nonlocal calls,active,peak
        calls += 1
        active += 1
        peak = max(peak,active)
        try:
            if calls==1:
                first.set()
                await release.wait()
            else:
                second.set()
        finally:
            active -= 1
    task = asyncio.create_task(loops.loop('events',tick,30))
    try:
        await first.wait()
        await asyncio.to_thread(loops.wake_events)
        release.set()
        await asyncio.wait_for(second.wait(),2)
        assert calls==2 and peak==1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_commit_while_sleeping_does_not_wait_for_fallback():
    loops = configured_loop()
    first, second = asyncio.Event(),asyncio.Event()
    calls = 0
    async def tick():
        nonlocal calls
        calls += 1
        (first if calls==1 else second).set()
    task = asyncio.create_task(loops.loop('events',tick,30))
    try:
        await first.wait()
        await asyncio.sleep(0)
        loops.wake_events()
        await asyncio.wait_for(second.wait(),2)
        assert calls==2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_fallback_still_checks_without_any_hint():
    loops = configured_loop()
    seen = asyncio.Event()
    count = 0
    async def tick():
        nonlocal count
        count += 1
        if count==2:
            seen.set()
    task = asyncio.create_task(loops.loop('events',tick,.01))
    try:
        await asyncio.wait_for(seen.wait(),2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_storm_is_coalesced_and_old_generation_cannot_wake_restart():
    loops = configured_loop()
    old = loops._event_wake
    calls = []
    loops._event_loop = SimpleNamespace(is_closed=lambda:False,
                                       call_soon_threadsafe=lambda *args:calls.append(args))
    for _ in range(10000):
        loops.wake_events()
    assert len(calls)==1
    await loops.stop()
    loops.started = True
    loops._event_wake = asyncio.Event()
    loops._deliver_event_wake(old)
    assert not old.is_set() and not loops._event_wake.is_set()
    assert loops.tasks==[]


@pytest.mark.asyncio
async def test_disabled_stopped_closed_or_closing_loop_is_noop():
    loops = CollaborationLoops(service(False))
    loops.wake_events()
    await loops.start()
    assert not loops.started and loops.tasks==[]
    loops = configured_loop()
    def closing(*args):
        raise RuntimeError('loop closing')
    loops._event_loop = SimpleNamespace(is_closed=lambda:False,call_soon_threadsafe=closing)
    loops.wake_events()
    assert loops._wake_scheduled is None
    loops._event_loop = SimpleNamespace(is_closed=lambda:True)
    loops.wake_events()
    await loops.stop()
    loops.wake_events()
    assert loops._event_loop is None


@pytest.mark.asyncio
async def test_failed_tick_still_accepts_a_commit_hint():
    loops = configured_loop()
    failed, recovered = asyncio.Event(),asyncio.Event()
    count = 0
    async def tick():
        nonlocal count
        count += 1
        if count==1:
            failed.set()
            raise ValueError('private body')
        recovered.set()
    task = asyncio.create_task(loops.loop('events',tick,30))
    try:
        await failed.wait()
        assert loops.service.health['events']['reason_code']=='ValueError'
        assert 'private body' not in str(loops.service.health)
        loops.wake_events()
        await asyncio.wait_for(recovered.wait(),2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
