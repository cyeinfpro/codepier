"""Runner settling is a prerequisite, never fabricated product health or a test retry."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
from agent.scheduler import Pressure
from scripts.check_resource_readiness import healthy, wait_ready


def sample(now, cpu=.2):
    return Pressure(now, cpu, 2 * 1024**3, 4 * 1024**3, 0, "test_os")


@pytest.mark.parametrize("change", [
    {"cpu_busy": None}, {"cpu_busy": .66}, {"cpu_busy": float("nan")},
    {"memory_available": 512 * 1024**2}, {"memory_total": 16 * 1024**3},
    {"io_stall": .1}, {"measured_at": 0},
])
def test_unhealthy_or_unavailable_observations_never_pass(change):
    assert not healthy(replace(sample(20), **change), 20)


def exercise(values, timeout=20):
    now = [0.0]
    events = []
    class Sampler:
        def sample(self):
            value = values[min(int(now[0]), len(values)-1)]
            return sample(now[0], value)
    result = wait_ready(Sampler(), timeout=timeout, stable_seconds=3, interval=1,
        clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds),
        emit=lambda text: events.append(json.loads(text)))
    return result, events


def test_requires_continuous_health_and_resets_on_pressure():
    result, events = exercise([None, .2, .2, .9, .2, .2, .2, .2])
    assert result and events[-1]["elapsed_seconds"] == 7
    assert events[3]["stable_seconds"] == 0
    assert events[-1]["stable_seconds"] == 3


def test_prerequisite_timeout_is_failure_not_skip_or_retry():
    result, events = exercise([.9], timeout=5)
    assert result is False
    assert events[-1]["state"] == "failed"
    assert events[-1]["reason"] == "runner_prerequisite_timeout"
    assert events[-1]["elapsed_seconds"] == 5
    assert len(events) == 6


def test_resource_ci_waits_before_original_test_without_threshold_changes():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    title = "- name: Wait for genuine resource-runner readiness"
    section = workflow.split(title)[1].split("- name:")[0]
    assert "if: matrix.shard == 4" in section
    assert "check_resource_readiness.py --timeout 300 --stable-seconds 15" in section
    assert workflow.index(title) < workflow.index("- name: Exhaustive deterministic shard")
    assert "mdutil" not in section and "continue-on-error" not in section
