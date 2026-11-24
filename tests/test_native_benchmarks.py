"""Tests for the P6 native session benchmark runner."""

from __future__ import annotations

import json

from scripts import benchmark_native_session as bench


def test_nearest_rank_percentiles_and_exploratory_flag() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 500.0]
    assert bench.nearest_rank_percentile(values, 50.0) == 30.0
    assert bench.nearest_rank_percentile(values, 95.0) == 500.0
    assert bench.nearest_rank_percentile([], 50.0) is None
    summary = bench.summarize_samples(values)
    assert summary["exploratory"] is True
    assert summary["p50"] == 30.0
    assert summary["p95"] == 500.0


def test_cold_open_v2_stays_bounded_while_v1_scales_with_history() -> None:
    cold = bench.benchmark_cold_open(sizes=(1000, 10000), page_size=50)
    assert cold["1000"]["v2_snapshot"]["replayed_events"] == 50
    assert cold["10000"]["v2_snapshot"]["replayed_events"] == 50
    assert cold["10000"]["v1_replay"]["replayed_events"] == 10000
    assert cold["10000"]["v2_snapshot"]["request_bytes"] < cold["10000"]["v1_replay"]["request_bytes"]


def test_slow_review_gate_does_not_block_provider_completed_or_projection() -> None:
    res = bench.benchmark_slow_review_gate(iterations=3, review_gate_ms=15.0)
    assert res["provider_to_projection_ms"]["p95"] < 5.0
    assert res["provider_to_review_ms"]["p50"] >= 14.0
    stages = res["stage_timestamps_ns_sample"]
    assert set(stages) == set(bench.STAGE_FIELDS)
    # Unobserved client stages must stay None, never fabricated as 0.
    assert stages["browser_received"] is None
    assert stages["frame_presented"] is None


def test_benchmark_runner_and_cli_output(tmp_path) -> None:
    out_file = tmp_path / "bench.json"
    code = bench.main(["--quick", "--output", str(out_file)])
    assert code == 0
    report = json.loads(out_file.read_text(encoding="utf-8"))
    prov = report["provenance"]
    assert prov["os"] and prov["arch"] and prov["python_version"]
    assert len(prov["source_sha256"]) == 64
    assert report["dirty_fenwick_delta"]["updated_nodes_per_delta"] <= 1.0
    assert report["reconnect_catchup"]["dropped_duplicate_bytes"] > 0
