"""A comparison cannot hide missing trace/operations or emit raw evidence."""

import json

from scripts.report_hub_benchmark_pair import safe_report


def archived_run(path, variant, *, skip_stage=False, duplicate=False):
    duration = 100 if variant == "before" else 99
    started = 10 if variant == "before" else 400
    repository = {
        "commit": ("a" if variant == "before" else "b") * 40,
        "tracked_diff_and_untracked_source_sha256": "0" * 64,
    }
    manifest = {
        "variant": variant,
        "repository": repository,
        "repository_end": repository,
        "source_unchanged": True,
        "result": "completed",
        "disconnect": {
            "passed": True,
            "exercised": True,
            "operation_rows": 1,
            "side_effect_count": 1,
            "original_operation_id": "private-original-receipt",
        },
        "harness_sha256": "c" * 64,
        "started_at": started,
        "ended_at": started + duration,
        "cpu_count": 8,
        "python": "3.13",
        "platform": "test",
        "host_context": "private-host-note",
        "preflight": [{"loadavg": [2, 2, 2]}],
        "mode": "open_loop",
        "admission_profile": "ready",
        "readiness_timeout_seconds": 120,
        "request_timeout_seconds": 60,
        "open_loop": {
            "arrival_rates_per_second": [10],
            "burst": 0,
            "max_in_flight": 32,
            "steady_duration_seconds": None,
            "planned_operations": 1000,
        },
        "workloads": ["read"],
    }
    cell = path / "read-open-10"
    cell.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(manifest))
    (cell / "summary.json").write_text(
        json.dumps(
            {
                "mode": "open_loop",
                "workload": "read",
                "arrival_rate_per_second": 10,
                "duration_seconds": duration,
                "scheduled": 1000,
                "succeeded": 1000,
                "errors": 0,
                "unresolved": 0,
                "admission_stopped": False,
                "resource_sampling_complete": True,
            }
        )
    )
    (cell / "resources.jsonl").write_text(
        json.dumps({"complete": True, "loadavg": [2, 2, 2]}) + "\n"
    )
    rows = [
        {
            "sequence": i,
            "kind": "read",
            "ok": True,
            "latency_ms": 50 if variant == "before" else 30,
            "hub_first_dispatch_ms": 20 if variant == "before" else 8,
            "agent_queue_ms": 2,
            "operation_id": f"private-receipt-{i if not duplicate else 0}",
            "send_lag_ms": 0.2,
        }
        for i in range(1000)
    ]
    if skip_stage:
        rows[-1].pop("hub_first_dispatch_ms")
    (cell / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path, manifest


def test_pair_shares_only_aggregates_and_marks_threshold_as_one_observation(tmp_path):
    before, _ = archived_run(tmp_path / "a", "before")
    after, _ = archived_run(tmp_path / "b", "after")
    report = safe_report(before, after)
    assert report["status"] == "single_pair"
    read = report["cells"][0]["observed_short_read"]
    assert read["status"] == "observed_met"
    assert read["first_dispatch_p95_reduction_pct"] == 60
    assert "private-" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)


def test_partial_stage_evidence_and_duplicate_operations_prevent_observed_met(tmp_path):
    before, _ = archived_run(tmp_path / "a", "before")
    after, _ = archived_run(tmp_path / "b", "after", skip_stage=True, duplicate=True)
    report = safe_report(before, after)
    assert report["status"] == "diagnostic"
    case = report["cells"][0]
    assert case["issues"] == ["duplicate_operation_id", "incomplete_success_stages"]
    assert case["after"]["by_kind"]["read"]["hub_first_dispatch_ms"]["count"] == 999
    assert case["observed_short_read"]["status"] == "diagnostic"


def test_disallows_pairing_different_fixed_arrival_or_deadline_parameters(tmp_path):
    before, _ = archived_run(tmp_path / "a", "before")
    after, manifest = archived_run(tmp_path / "b", "after")
    manifest["open_loop"]["max_in_flight"] = 48
    manifest["request_timeout_seconds"] = 10
    (after / "manifest.json").write_text(json.dumps(manifest))
    report = safe_report(before, after)
    assert report["status"] == "incompatible"
    assert set(report["mismatched_fields"]) == {"open_loop", "request_timeout_seconds"}
    assert "cells" not in report
