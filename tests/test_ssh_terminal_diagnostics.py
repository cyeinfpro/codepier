"""Measurements are bounded numeric data, never a second terminal journal."""

import json
from types import SimpleNamespace

import pytest

from hub.ssh_terminal.diagnostics import (
    BUCKETS_MS, COUNTERS, HISTOGRAMS, MAX_COUNTER, TerminalMetrics, safe_metadata, session_snapshot,
)


def test_metrics_have_fixed_names_bounded_values_and_cumulative_histograms():
    metrics = TerminalMetrics()
    metrics.increment("connect_total", MAX_COUNTER)
    metrics.increment("connect_total")
    for value in (0, 5, 15, 999999, float("nan"), float("inf"), -1, True, "secret"):
        metrics.observe("agent_control_rtt_ms", value)
    result = metrics.snapshot()
    assert set(result["counters"]) == set(COUNTERS)
    assert set(result["histograms"]) == set(HISTOGRAMS)
    assert result["counters"]["connect_total"] == MAX_COUNTER
    histogram = result["histograms"]["agent_control_rtt_ms"]
    assert histogram["count"] == 4 and histogram["overflow"] == 1
    assert [item["le_ms"] for item in histogram["buckets"]] == list(BUCKETS_MS)
    assert histogram["buckets"][0]["count"] == 1
    assert histogram["buckets"][1]["count"] == 2
    assert histogram["buckets"][-1]["count"] == 3
    with pytest.raises(KeyError):
        metrics.increment("session-id-is-not-a-label")
    assert "secret" not in json.dumps(result, allow_nan=False)


def test_diagnostic_metadata_accepts_only_fixed_numeric_stages_and_versions():
    metadata = safe_metadata({
        "stage_timings_ms": {"ssh_connect": 15, "pty_open": 0, "attach": float("nan"),
                             "hub_prepare": "password-secret", "command": "never-keep"},
        "agent_version": "1.33.37", "hub_version": "invalid secret", "protocol_version": True,
        "connected_at": 1000, "disconnected_at": float("inf"), "reconnect_count": -1,
        "recover_count": 1, "backpressure_count": "2", "backpressure_duration_ms": 100,
        "password": "never-keep", "output": "never-keep",
    })
    assert metadata == {"stage_timings_ms": {"ssh_connect": 15, "pty_open": 0}, "agent_version": "1.33.37",
                        "connected_at": 1000, "recover_count": 1, "backpressure_count": 2,
                        "backpressure_duration_ms": 100}


def test_session_projection_never_exposes_other_sessions_or_unmeasured_buffers():
    secret = "synthetic-secret-do-not-expose"
    session = SimpleNamespace(row={"id": "a" * 32, "runtime_generation": "b" * 32,
                                  "state": "detached", "mode": "direct", "reason_code": secret, "created": 10,
                                  "metadata": json.dumps({"host": secret, "username": secret,
                                                          "stage_timings_ms": {"hub_prepare": 7}})},
                              attachment=None, metrics=TerminalMetrics())
    agent = SimpleNamespace(closed=False, generation="b" * 32,
                            queue=SimpleNamespace(queued_bytes=123456, waiting_bytes=654321))
    status = {"ssh_connected": True, "password": secret, "terminal_output": secret,
              "stage_timings_ms": {"ssh_connect": 25, "unknown": secret},
              "ring_bytes": 0, "output_outstanding_bytes": 0, "bytes_in": 0, "bytes_out": 0}
    measured = session_snapshot(session, agent=agent, status=status, link_status="connected",
                                rtt_ms=0, link_reason=None, hub_version="1.33.37")
    assert measured["stages_ms"] == {"hub_prepare": 7, "ssh_connect": 25}
    assert measured["bytes"] == {"input": "0", "output": "0"}
    assert measured["links"]["agent"]["control_rtt_ms"] == 0
    assert measured["metrics"]["gauges"]["agent_queue_bytes"] is None
    assert measured["metrics"]["gauges"]["ssh_ring_bytes"] == 0
    assert secret not in json.dumps(measured)
    missing = session_snapshot(session, agent=agent, status={}, link_status="unconfirmed",
                               rtt_ms=None, link_reason="AGENT_RESPONSE_UNKNOWN", hub_version="1.33.37")
    assert missing["bytes"] == {"input": None, "output": None}
    assert missing["links"]["ssh"]["connected"] is None
    assert missing["metrics"]["gauges"]["ssh_ring_bytes"] is None
    assert missing["metrics"]["gauges"]["ssh_outstanding_bytes"] is None
    assert missing["links"]["agent"]["control_rtt_ms"] is None
