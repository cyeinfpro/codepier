import subprocess

import pytest

from agent import resource_pressure
from agent.resource_pressure import ResourceSampler


def test_linux_cpu_delta_memory_cgroup_and_io(monkeypatch):
    now = [1.0]
    sampler = ResourceSampler(lambda: now[0])
    values = {
        "/proc/stat": "cpu 100 0 50 850 0 0 0 0 0 0\n",
        "/proc/meminfo": "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n",
        "/proc/pressure/io": "some avg10=2.50 avg60=0.00 total=100\n",
        "/sys/fs/cgroup/memory.max": str(4 * 1024**3),
        "/sys/fs/cgroup/memory.current": str(1024**3),
    }
    monkeypatch.setattr(resource_pressure.sys, "platform", "linux")
    monkeypatch.setattr(sampler, "_read", lambda path: values[path])
    initial = sampler.sample()
    assert initial.cpu_busy is None
    assert initial.memory_total == 4 * 1024**3
    assert initial.memory_available == 3 * 1024**3
    now[0] = 4.0
    values["/proc/stat"] = "cpu 150 0 100 1050 0 0 0 0 0 0\n"
    result = sampler.sample()
    assert result.cpu_busy == pytest.approx(1 / 3)
    assert result.io_stall == .025
    assert result.valid(now[0])


@pytest.mark.parametrize("exception", [OSError("unavailable"), ValueError("invalid"), subprocess.TimeoutExpired("counter", .6)])
def test_collection_errors_are_explicit_and_reset_cpu_baseline(monkeypatch, exception):
    sampler = ResourceSampler(lambda: 1.0)
    sampler.previous = (0, (100, 50))
    monkeypatch.setattr(resource_pressure.sys, "platform", "darwin")
    def fail():
        raise exception
    monkeypatch.setattr(sampler, "_darwin", fail)
    result = sampler.sample()
    assert result.source == "unavailable"
    assert result.cpu_busy is None and result.memory_available is None
    assert sampler.previous is None


@pytest.mark.parametrize("ticks", [(90, 40), (120, 80), (100, 50)])
def test_counter_wrap_or_invalid_delta_is_not_reported_as_usage(monkeypatch, ticks):
    sampler = ResourceSampler(lambda: 1.0)
    sampler.previous = (0, (100, 50))
    monkeypatch.setattr(resource_pressure.sys, "platform", "win32")
    monkeypatch.setattr(sampler, "_windows", lambda: (ticks, 1024**3, 4 * 1024**3, None, "fixture"))
    assert sampler.sample().cpu_busy is None


def test_long_sampling_gap_does_not_claim_current_cpu(monkeypatch):
    sampler = ResourceSampler(lambda: 100.0)
    sampler.previous = (0, (100, 50))
    monkeypatch.setattr(resource_pressure.sys, "platform", "darwin")
    monkeypatch.setattr(sampler, "_darwin", lambda: ((200, 100), 1024**3, 4 * 1024**3, None, "fixture"))
    assert sampler.sample().cpu_busy is None


def test_unsupported_platform_is_unknown_not_fake_zero(monkeypatch):
    monkeypatch.setattr(resource_pressure.sys, "platform", "unsupported")
    result = ResourceSampler().sample()
    assert result.source == "unavailable" and result.cpu_busy is None


def linux_fixture(monkeypatch):
    now = [1.0]
    sampler = ResourceSampler(lambda: now[0])
    values = {
        "/proc/stat": "cpu 100 0 0 900 0 0 0 0\n",
        "/proc/meminfo": "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n",
        "/proc/pressure/io": "some avg10=0.00 avg60=0.00 total=100\n",
        "/proc/self/cgroup": "0::/\n",
        "/sys/fs/cgroup/memory.max": str(1024**3),
        "/sys/fs/cgroup/memory.current": str(1000 * 1024**2),
        "/sys/fs/cgroup/cpu.max": "max 100000",
    }

    def read(path):
        if path not in values:
            raise OSError("missing counter")
        return values[path]

    monkeypatch.setattr(resource_pressure.sys, "platform", "linux")
    monkeypatch.setattr(resource_pressure.os, "sched_getaffinity", lambda _: {0, 1}, raising=False)
    monkeypatch.setattr(sampler, "_read", read)
    return sampler, now, values


def test_cgroup_reclaimable_cache_does_not_latch_memory_pause(monkeypatch):
    from agent.scheduler import AdaptiveCapacity
    sampler, now, values = linux_fixture(monkeypatch)
    values["/sys/fs/cgroup/memory.stat"] = (
        f"inactive_file {700 * 1024**2}\nfile {800 * 1024**2}\nshmem 0\n"
        f"file_dirty {20 * 1024**2}\nfile_writeback {10 * 1024**2}\n")
    sample = sampler.sample()
    assert sample.memory_available == 694 * 1024**2
    capacity = AdaptiveCapacity({}, lambda: now[0])
    capacity.update(sample)
    assert not capacity.memory_paused
    values["/sys/fs/cgroup/memory.stat"] = (
        "inactive_file 0\nfile 0\nshmem 0\nfile_dirty 0\nfile_writeback 0\n")
    sample = sampler.sample()
    assert sample.memory_available == 24 * 1024**2
    capacity.update(sample)
    assert capacity.memory_paused


def test_cgroup_does_not_count_shmem_dirty_or_writeback_as_free(monkeypatch):
    sampler, _, values = linux_fixture(monkeypatch)
    values["/sys/fs/cgroup/memory.stat"] = (
        f"inactive_file {700 * 1024**2}\nfile {800 * 1024**2}\nshmem {700 * 1024**2}\n"
        f"file_dirty {70 * 1024**2}\nfile_writeback {30 * 1024**2}\n")
    assert sampler.sample().memory_available == 24 * 1024**2


@pytest.mark.parametrize("quota", ["100000 100000", "50000 100000"])
def test_cgroup_cpu_uses_quota_instead_of_host_cpu_count(monkeypatch, quota):
    sampler, now, values = linux_fixture(monkeypatch)
    values["/sys/fs/cgroup/cpu.max"] = quota
    values["/sys/fs/cgroup/cpu.stat"] = "usage_usec 1000000\n"
    assert sampler.sample().cpu_busy is None
    now[0] += 2
    values["/proc/stat"] = "cpu 101 0 0 1099 0 0 0 0\n"
    usage = 2000000 if quota.startswith("50000") else 3000000
    values["/sys/fs/cgroup/cpu.stat"] = f"usage_usec {usage}\n"
    assert sampler.sample().cpu_busy == 1.0


def test_linux_cpu_affinity_detects_one_saturated_allowed_cpu(monkeypatch):
    sampler, now, values = linux_fixture(monkeypatch)
    monkeypatch.setattr(resource_pressure.os, "sched_getaffinity", lambda _: {1})
    values["/proc/stat"] = (
        "cpu 100 0 0 900 0 0 0 0\ncpu0 50 0 0 450 0 0 0 0\ncpu1 50 0 0 450 0 0 0 0\n")
    assert sampler.sample().cpu_busy is None
    now[0] += 2
    values["/proc/stat"] = (
        "cpu 101 0 0 1099 0 0 0 0\ncpu0 50 0 0 649 0 0 0 0\ncpu1 51 0 0 450 0 0 0 0\n")
    assert sampler.sample().cpu_busy == 1.0


def test_nested_cgroup_respects_tighter_parent_memory_and_cpu(monkeypatch):
    sampler, now, values = linux_fixture(monkeypatch)
    values["/proc/self/cgroup"] = "0::/slice/agent\n"
    for group in ("/sys/fs/cgroup/slice", "/sys/fs/cgroup/slice/agent"):
        values[group + "/memory.max"] = "max"
        values[group + "/memory.current"] = "0"
        values[group + "/cpu.max"] = "max 100000"
    values["/sys/fs/cgroup/slice/memory.max"] = str(512 * 1024**2)
    values["/sys/fs/cgroup/slice/memory.current"] = str(256 * 1024**2)
    values["/sys/fs/cgroup/slice/cpu.max"] = "50000 100000"
    values["/sys/fs/cgroup/slice/cpu.stat"] = "usage_usec 0\n"
    # The root has no tighter competing memory pressure.
    values["/sys/fs/cgroup/memory.current"] = "0"
    assert sampler.sample().memory_total == 512 * 1024**2
    now[0] += 2
    values["/proc/stat"] = "cpu 101 0 0 1099 0 0 0 0\n"
    values["/sys/fs/cgroup/slice/cpu.stat"] = "usage_usec 1000000\n"
    sample = sampler.sample()
    assert sample.memory_available == 256 * 1024**2
    assert sample.cpu_busy == 1.0


def test_cgroup_cpu_reset_does_not_fabricate_saturation(monkeypatch):
    sampler, now, values = linux_fixture(monkeypatch)
    values["/sys/fs/cgroup/cpu.max"] = "50000 100000"
    values["/sys/fs/cgroup/cpu.stat"] = "usage_usec 9000000\n"
    sampler.sample()
    now[0] += 2
    values["/proc/stat"] = "cpu 101 0 0 1099 0 0 0 0\n"
    values["/sys/fs/cgroup/cpu.stat"] = "usage_usec 0\n"
    assert sampler.sample().cpu_busy == pytest.approx(.005)
