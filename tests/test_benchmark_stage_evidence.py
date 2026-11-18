"""The benchmark compares stage pairs within one clock and handling attempt."""

import pytest

from scripts.benchmark_hub_agent import _trace_intervals


def hub(seq, stage, at):
    return dict(source="hub", seq=seq, stage=stage, at=at, elapsed_ms=None)


def agent(seq, stage, elapsed_ms):
    # Hub's snapshot ingestion can see every stage at the same wall time.
    return dict(source="agent", seq=seq, stage=stage, at=16384, elapsed_ms=elapsed_ms)


def measured(*events, attempts=1):
    result = {}
    _trace_intervals(result, {"created": 8.0, "attempts": attempts}, list(events))
    return result


def test_measure_first_dispatch_by_sequence_and_report_separate_clock_domains():
    measurements = measured(
        hub(4, "hub_completed", 12.0),
        hub(2, "dispatched", 10.0),
        hub(3, "dispatched", 9.25),  # Hub clock adjusted before retry.
        hub(1, "hub_received", 9.0),
        agent(3, "executing", 40),
        agent(1, "accepted", 6),
        agent(2, "waiting_worker", 12),
        attempts=2,
    )
    assert measurements == {
        "hub_queue_ms": 2000.0,
        "hub_first_dispatch_ms": 1000.0,
        "hub_after_dispatch_ms": 2000.0,
        "agent_queue_unavailable": "multiple_attempts",
    }
    result = measured(agent(3, "executing", 40), agent(1, "accepted", 6))
    assert result["agent_queue_ms"] == 34


def test_pruned_or_backwards_hub_clock_is_never_zero_latency_or_first_send():
    for events, reason in (
        ([hub(4, "dispatched", 10)], "missing_received_or_dispatch"),
        ([hub(2, "dispatched", 10), hub(3, "hub_received", 9)], "stage_order"),
        ([hub(1, "hub_received", 11), hub(2, "dispatched", 10)], "clock_order"),
    ):
        result = measured(*events)
        assert "hub_first_dispatch_ms" not in result
        assert result["hub_first_dispatch_unavailable"] == reason
    assert "hub_queue_ms" not in measured(hub(4, "dispatched", 10))


@pytest.mark.parametrize(
    "events,reason",
    [
        (
            [agent(3, "waiting_worker", 99), agent(4, "executing", 100)],
            "missing_accepted_or_executing",
        ),
        (
            [agent(1, "accepted", 0), agent(2, "accepted", 0), agent(3, "executing", 5)],
            "multiple_attempts",
        ),
        ([agent(1, "accepted", 11), agent(2, "executing", 10)], "clock_order"),
        ([agent(2, "accepted", 5), agent(1, "executing", 10)], "stage_order"),
    ],
)
def test_missing_reset_or_inverted_agent_clock_is_never_a_short_wait(events, reason):
    result = measured(*events)
    assert "agent_queue_ms" not in result
    assert result["agent_queue_unavailable"] == reason
