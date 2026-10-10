import asyncio
from contextlib import AsyncExitStack

import pytest

from agent.scheduler import AdaptiveCapacity, Pressure, ProjectScheduler, validate_scheduler
from shared.util import DevError


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def pressure(clock, **kwargs):
    return Pressure(clock(), cpu_busy=kwargs.get("cpu", .3),
                    memory_available=kwargs.get("available", 8 * 1024**3),
                    memory_total=16 * 1024**3, io_stall=kwargs.get("io", 0))


def test_capacity_grows_gradually_after_healthy_cooldown():
    clock = Clock()
    capacity = AdaptiveCapacity({"initial": 8, "maximum": 12}, clock)
    for moment in (0, 5, 10):
        clock.now = moment
        assert capacity.update(pressure(clock)) == 8
    clock.now = 15
    assert capacity.update(pressure(clock)) == 9
    clock.now = 16
    assert capacity.update(pressure(clock)) == 9


def test_pressure_reduces_capacity_without_below_minimum():
    clock = Clock()
    capacity = AdaptiveCapacity({"initial": 8, "maximum": 12}, clock)
    assert capacity.update(pressure(clock, cpu=.95)) == 6
    for _ in range(10):
        capacity.update(pressure(clock, cpu=.95))
    assert capacity.limit == 2


@pytest.mark.parametrize("bad", [
    None, Pressure(float("nan")), Pressure(-100, .2, 100, 200),
    Pressure(0, float("nan"), 100, 200), Pressure(0, 1.5, 100, 200),
    Pressure(0, .5, -1, 200), Pressure(0, .5, 300, 200),
    Pressure(0, .5, True, 200), Pressure(0, .5, 100, 200, float("inf")),
])
def test_invalid_metrics_degrade_safely(bad):
    capacity = AdaptiveCapacity({"initial": 8, "maximum": 12}, Clock())
    assert capacity.update(bad) == 4
    assert capacity.reason == "metrics_unavailable"


def test_critical_memory_pauses_new_work_and_recovers_slowly():
    clock = Clock()
    capacity = AdaptiveCapacity({"initial": 8, "maximum": 12}, clock)
    assert capacity.update(pressure(clock, available=128 * 1024**2)) == 0
    assert capacity.reason == "memory_critical"
    for moment in (5, 10):
        clock.now = moment
        assert capacity.update(pressure(clock)) == 0
    clock.now = 15
    assert capacity.update(pressure(clock)) == 2


@pytest.mark.parametrize("config", [
    {"adaptive": "true"}, {"maximum": True}, {"minimum": 9, "maximum": 8},
    {"maximum": 65}, {"minimum": 0}, {"unknown": 1},
    {"project_limits": {"a": False}}, {"project_limits": {"a": 65}},
    {"project_limits": []},
])
def test_configuration_rejects_invalid_values(config):
    with pytest.raises(ValueError):
        validate_scheduler(config)


async def admitted(scheduler, name, project, started, release, lane="execution", target=""):
    async with scheduler.slot(name, project, lane, target):
        started.set()
        await release.wait()


def test_other_project_has_reserved_capacity_and_no_duplicate_admission():
    async def run():
        scheduler = ProjectScheduler({"adaptive": False, "initial": 4})
        release = asyncio.Event()
        started = [asyncio.Event() for _ in range(5)]
        tasks = [asyncio.create_task(admitted(scheduler, str(i), "A", started[i], release)) for i in range(4)]
        try:
            for event in started[:3]:
                await asyncio.wait_for(event.wait(), 1)
            await asyncio.sleep(0)
            assert not started[3].is_set()
            tasks.append(asyncio.create_task(admitted(scheduler, "B1", "B", started[4], release)))
            await asyncio.wait_for(started[4].wait(), 1)
            assert scheduler.snapshot()["running"] == 4
            assert scheduler.snapshot("A")["queued"] == 1
            assert scheduler.snapshot("B")["running"] == 1
            assert scheduler.snapshot("B")["queued"] == 0
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert scheduler.snapshot()["running"] == 0
        assert scheduler.snapshot()["queued"] == 0
        assert not scheduler.turn
    asyncio.run(run())


def test_cancel_timeout_and_exception_release_all_state():
    async def run():
        scheduler = ProjectScheduler({"adaptive": False, "initial": 2})
        async with scheduler.slot("first", "A"):
            queued = asyncio.create_task(scheduler.slot("second", "A").__aenter__())
            await asyncio.sleep(0)
            assert scheduler.snapshot()["queued"] == 1
            queued.cancel()
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert scheduler.snapshot()["queued"] == 0
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.01):
                    async with scheduler.slot("expired", "A"):
                        pytest.fail("must remain queued")
            assert scheduler.snapshot()["queued"] == 0
        with pytest.raises(RuntimeError):
            async with scheduler.slot("throws", "A"):
                raise RuntimeError("owned operation failed")
        assert scheduler.snapshot()["running"] == 0
        assert not scheduler.turn
    asyncio.run(run())


def test_remote_target_backlog_does_not_block_other_target_or_local_reads():
    async def run():
        scheduler = ProjectScheduler({"remote_target_limit": 1})
        release = asyncio.Event()
        first, blocked, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            tasks.append(asyncio.create_task(admitted(scheduler, "r1", "A", first, release, "remote", "host-one")))
            await first.wait()
            tasks.append(asyncio.create_task(admitted(scheduler, "r2", "A", blocked, release, "remote", "host-one")))
            tasks.append(asyncio.create_task(admitted(scheduler, "r3", "A", other, release, "remote", "host-two")))
            await asyncio.wait_for(other.wait(), 1)
            assert not blocked.is_set()
            async with scheduler.slot("read", "B", "read"):
                async with scheduler.slot("local", "B"):
                    assert scheduler.snapshot()["running"] == 4
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert not scheduler.active and not scheduler.waiting
    asyncio.run(run())


def test_shrinking_limit_drains_active_work_without_cancelling_it():
    async def run():
        clock = Clock()
        scheduler = ProjectScheduler({"initial": 8, "maximum": 12}, clock)
        async with AsyncExitStack() as stack:
            for i in range(6):
                await stack.enter_async_context(scheduler.slot(str(i), "A"))
            await scheduler.update_pressure(pressure(clock, available=128 * 1024**2))
            assert scheduler.snapshot()["running"] == 6
            assert scheduler.snapshot()["lanes"]["execution"]["capacity"] == 0
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.01):
                    async with scheduler.slot("blocked", "B"):
                        pytest.fail("critical memory must pause admission")
            assert scheduler.snapshot()["running"] == 6
            async with scheduler.slot("health-read", "B", "read"):
                assert scheduler.snapshot()["running"] == 7
        assert not scheduler.active
    asyncio.run(run())


def test_project_override_is_independent_but_does_not_bypass_node_limit():
    async def run():
        scheduler = ProjectScheduler({"initial": 8, "project_limits": {"A": 1, "B": 6}})
        async with scheduler.slot("a1", "A"):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.01):
                    async with scheduler.slot("a2", "A"):
                        pytest.fail("project cap")
            async with AsyncExitStack() as stack:
                for i in range(6):
                    await stack.enter_async_context(scheduler.slot(str(i), "B"))
                assert scheduler.snapshot()["running"] == 7
                async with scheduler.slot("c1", "C"):
                    assert scheduler.snapshot()["running"] == 8
    asyncio.run(run())


def test_close_wakes_waiters_but_does_not_cancel_owned_running_work():
    async def run():
        scheduler = ProjectScheduler({"initial": 2})
        async with scheduler.slot("a1", "A"):
            queued = asyncio.create_task(scheduler.slot("a2", "A").__aenter__())
            await asyncio.sleep(0)
            await scheduler.close()
            with pytest.raises(DevError, match="Agent"):
                await queued
            assert scheduler.snapshot()["running"] == 1
        with pytest.raises(DevError):
            async with scheduler.slot("later", "B"):
                pytest.fail("closed")
        assert not scheduler.active and not scheduler.waiting
    asyncio.run(run())


@pytest.mark.parametrize("adaptive", [True, False])
@pytest.mark.parametrize("total_mib", [256, 512, 1024])
def test_small_memory_budgets_can_recover_after_drain(total_mib, adaptive):
    clock = Clock()
    capacity = AdaptiveCapacity({"adaptive": adaptive, "initial": 4}, clock)
    total = total_mib * 1024**2
    assert capacity.update(Pressure(clock(), .2, 8 * 1024**2, total)) == 0
    for moment in (5, 10):
        clock.now = moment
        assert capacity.update(Pressure(clock(), .2, total // 3, total)) == 0
    clock.now = 15
    assert capacity.update(Pressure(clock(), .2, total // 3, total)) == (2 if adaptive else 4)
    assert not capacity.memory_paused


@pytest.mark.parametrize("overlap", [False, True])
def test_effective_project_union_preserves_both_bounded_sources(overlap):
    from shared.scheduler_config import effective_scheduler, validate_scheduler_overrides
    local = {"project_limits": {f"local-{i}": 2 for i in range(256)}}
    requested = {"project_limits": {f"panel-{i}": 6 for i in range(256)}}
    if overlap:
        requested["project_limits"].pop("panel-0")
        requested["project_limits"]["local-0"] = 64
    effective = effective_scheduler(local, requested)
    assert len(effective["project_limits"]) == (511 if overlap else 512)
    assert all(effective["project_limits"][key] == 2 for key in local["project_limits"])
    scheduler = ProjectScheduler(effective)
    asyncio.run(scheduler.reconfigure(effective))
    assert scheduler.config["project_limits"] == effective["project_limits"]
    with pytest.raises(ValueError):
        validate_scheduler_overrides({"project_limits": {str(i): 1 for i in range(257)}})
    with pytest.raises(ValueError):
        effective_scheduler({"project_limits": {str(i): 1 for i in range(257)}}, {})


def test_close_wins_when_waiter_becomes_eligible_before_it_resumes():
    async def run():
        scheduler = ProjectScheduler({"minimum": 1, "maximum": 1, "initial": 1})
        async with scheduler.slot("active", "A"):
            waiter = asyncio.create_task(scheduler.slot("waiting", "B").__aenter__())
            await asyncio.sleep(0)
            assert scheduler.waiting
            await scheduler.close()
        with pytest.raises(DevError):
            await waiter
        assert not scheduler.waiting and not scheduler.active
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_kind", ["unavailable", "neutral"])
async def test_raised_minimum_applies_immediately_and_survives_nonhealthy_metrics(sample_kind):
    clock = Clock()
    scheduler = ProjectScheduler({"initial": 2, "maximum": 12}, clock)
    await scheduler.reconfigure({"minimum": 8, "initial": 8, "maximum": 12})
    assert scheduler.capacity.limit == 8
    for moment in (3, 6, 30):
        clock.now = moment
        sample = None if sample_kind == "unavailable" else pressure(
            clock, cpu=.75, available=3 * 1024**3)
        await scheduler.update_pressure(sample)
        assert scheduler.capacity.limit == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("adaptive", [False, True])
async def test_raised_minimum_does_not_release_memory_emergency(adaptive):
    clock = Clock()
    scheduler = ProjectScheduler({"initial": 2, "maximum": 12}, clock)
    await scheduler.update_pressure(pressure(clock, available=128 * 1024**2))
    await scheduler.reconfigure({"adaptive": adaptive, "minimum": 8, "initial": 10, "maximum": 12})
    assert scheduler.capacity.memory_paused and scheduler.capacity.limit == 0
    await scheduler.update_pressure(None)
    assert scheduler.capacity.memory_paused and scheduler.capacity.limit == 0
    for moment in (5, 10, 15):
        clock.now = moment
        await scheduler.update_pressure(pressure(clock))
    assert not scheduler.capacity.memory_paused
    assert scheduler.capacity.limit == (8 if adaptive else 10)


@pytest.mark.asyncio
async def test_fixed_reconfigure_keeps_requested_initial_above_minimum():
    scheduler = ProjectScheduler({"initial": 2, "maximum": 12})
    await scheduler.reconfigure({"adaptive": False, "minimum": 8, "initial": 10, "maximum": 12})
    assert scheduler.capacity.limit == 10
