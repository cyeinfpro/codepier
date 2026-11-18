#!/usr/bin/env python3
"""Print a shareable comparison of two stopped benchmark_hub_agent runs.

Usage: python scripts/report_hub_benchmark_pair.py BEFORE_OUTPUT AFTER_OUTPUT
Exit 2 indicates incompatible or missing manifests. Exit 0 includes diagnostics.
Formal qualification requires the repeated ABBA runs in docs/PERFORMANCE_BENCHMARKS.md.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics

if __package__:
    from . import benchmark_hub_agent as benchmark
else:
    import benchmark_hub_agent as benchmark

WORKLOADS = {"read", "read_write", "long_short"}
KINDS = ("read", "write", "long")
STAGES = ("hub_first_dispatch_ms", "agent_queue_ms", "latency_ms")
# Compare known host context verbatim in memory. Never put host text, arbitrary
# manifest keys, operation IDs, filesystem paths or input error messages in output.
COMMON = (
    "harness_sha256",
    "mode",
    "python",
    "platform",
    "cpu_count",
    "workloads",
    "request_timeout_seconds",
    "admission_profile",
    "readiness_timeout_seconds",
    "allow_noisy",
    "max_load_per_cpu",
)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def identity(m):
    src = m["repository"]
    sha = src["commit"]
    diff = src["tracked_diff_and_untracked_source_sha256"]
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", sha) or not re.fullmatch(
        r"[a-f0-9]{64}", diff
    ):
        raise ValueError("invalid_source_identity")
    return {"commit": sha, "diff_sha256": diff}


def plan(m):
    names = m["workloads"]
    if not names or not set(names) <= WORKLOADS or len(set(names)) != len(names):
        raise ValueError("invalid_workloads")
    if m["mode"] == "closed_loop":
        counts, steps = m["concurrency"], m["operations_per_worker"]
        if (
            not counts
            or any(type(c) is not int or c not in {1, 8, 16, 32, 64, 128} for c in counts)
            or len(set(counts)) != len(counts)
            or type(steps) is not int
            or not 1 <= steps <= 32
        ):
            raise ValueError("invalid_closed_plan")
        return {f"{name}-{n}": (name, n, n * steps) for name in names for n in counts}
    if m["mode"] != "open_loop":
        raise ValueError("invalid_mode")
    p = m["open_loop"]
    rates = p["arrival_rates_per_second"]
    burst, duration, ops = p["burst"], p["steady_duration_seconds"], p["planned_operations"]
    if (
        not rates
        or len(set(rates)) != len(rates)
        or any(not number(r) or not 0.1 <= r <= 1000 for r in rates)
        or type(burst) is not int
        or not 0 <= burst <= 1024
        or not number(p["max_in_flight"])
        or not 1 <= p["max_in_flight"] <= 256
        or (ops is None) == (duration is None)
        or (ops is not None and (type(ops) is not int or not max(1, burst) <= ops <= 100000))
        or (duration is not None and (not number(duration) or not 1 <= duration <= 3600))
    ):
        raise ValueError("invalid_open_plan")
    scheduled = {
        rate: len(benchmark.arrival_schedule(rate, operations=ops, duration=duration, burst=burst))
        for rate in rates
    }
    cells = {
        f"{name}-open-{rate:g}": (name, rate, count)
        for name in names
        for rate, count in scheduled.items()
    }
    if len(cells) != len(rates) * len(names):
        raise ValueError("invalid_open_plan")
    return cells


def manifest_issues(m, variant):
    issues = set()
    if m["variant"] != variant:
        issues.add("wrong_variant")
    if m.get("source_unchanged") is not True or m.get("repository_end") != m["repository"]:
        issues.add("source_not_frozen")
    d = m.get("disconnect", {})
    if not (
        d.get("passed") is True
        and d.get("exercised") is True
        and d.get("operation_rows") == d.get("side_effect_count") == 1
    ):
        issues.add("disconnect_not_verified")
    if m.get("result") != "completed":
        issues.add("run_not_completed_cleanly")
    if m.get("allow_noisy") or m.get("overloaded"):
        issues.add("noisy_run")
    if not m.get("host_context"):
        issues.add("host_context_missing")
    if not m.get("request_timeout_seconds"):
        issues.add("request_timeout_unrecorded")
    if m.get("admission_profile") != "ready":
        issues.add("admission_not_stabilized")
    preflight = m.get("preflight", [])
    if not preflight or not all(r.get("loadavg") and number(r["loadavg"][0]) for r in preflight):
        issues.add("preflight_measurements_missing")
    elif max(r["loadavg"][0] for r in preflight) / m["cpu_count"] > 1:
        issues.add("host_overloaded")
    return issues


def cell(directory, summary, dims, mode, cpu):
    name, level, expected = dims
    open_loop = mode == "open_loop"
    if summary.get("arrival_rate_per_second" if open_loop else "concurrency") != level:
        raise ValueError("cell_mismatch")
    issues = set()
    counts = {kind: Counter() for kind in KINDS}
    observations = {kind: {key: [] for key in STAGES} for kind in KINDS}
    lags, terminal_offsets = [], []
    all_latencies = {kind: [] for kind in KINDS}
    unique, operations = set(), set()
    # Fixed filenames only; 'directory' is constructed from validated plan values.
    for sample in rows(directory / "samples.jsonl"):
        if open_loop:
            seq = sample["sequence"]
            in_plan = type(seq) is int and 0 <= seq < expected
            expected_kind = benchmark.open_loop_kind(name, seq)
            key = seq
        else:
            worker, step = sample["worker"], sample["step"]
            in_plan = (
                type(worker) is type(step) is int
                and 0 <= worker < level
                and 0 <= step < expected // level
            )
            expected_kind = benchmark.closed_loop_kind(name, worker, step)
            key = (worker, step)
        kind = sample["kind"]
        if not in_plan or kind != expected_kind or key in unique:
            issues.add("sample_does_not_match_plan")
        result = benchmark.open_loop_outcome(sample)
        counts[kind][result] += 1
        unique.add(key)
        if number(sample.get("latency_ms")):
            all_latencies[kind].append(sample["latency_ms"])
        op_id = sample.get("operation_id")
        if op_id is not None:
            if op_id in operations:
                issues.add("duplicate_operation_id")
            operations.add(op_id)
        if (
            open_loop
            and sample.get("phase") == "burst"
            and number(sample.get("completed_offset_ms"))
        ):
            terminal_offsets.append(sample["completed_offset_ms"])
        if result == "succeeded":
            if not op_id:
                issues.add("missing_operation_id")
            for key in STAGES:
                if number(sample.get(key)):
                    observations[kind][key].append(sample[key])
                else:
                    issues.add("incomplete_success_stages")
            if open_loop and number(sample.get("send_lag_ms")):
                lags.append(sample["send_lag_ms"])
    n = sum(c.total() for c in counts.values())
    successes = sum(c["succeeded"] for c in counts.values())
    if (
        n != expected
        or len(unique) != expected
        or n != summary.get("scheduled", summary.get("samples"))
        or successes != summary.get("succeeded")
        or n - successes != summary.get("errors")
    ):
        issues.add("incomplete_or_inconsistent_samples")
    if (
        n != successes
        or summary.get("unresolved") != 0
        or summary.get("admission_stopped") is not False
    ):
        issues.add("operations_not_all_successful")
    send_lag = benchmark.percentiles(lags)
    if open_loop and (len(lags) != successes or (lags and send_lag["p99"] > 5)):
        issues.add("client_lag_or_missing_measurements")
    loads = []
    for row in rows(directory / "resources.jsonl"):
        if (
            row.get("complete") is not True
            or not row.get("loadavg")
            or not number(row["loadavg"][0])
        ):
            issues.add("resource_samples_missing")
        else:
            loads.append(row["loadavg"][0] / cpu)
    if not loads or summary.get("resource_sampling_complete") is not True:
        issues.add("resource_samples_missing")
    if loads and max(loads) > 1:
        issues.add("host_overloaded")
    seconds = summary["duration_seconds"]
    if not number(seconds) or not seconds:
        raise ValueError("invalid_duration")

    def metric(kind, c):
        return {
            "samples": c.total(),
            **{
                key: c[key]
                for key in (
                    "succeeded",
                    "rejected",
                    "timeout",
                    "failed",
                    "client_dropped",
                    "not_sent_after_stop",
                )
            },
            "success_per_second": c["succeeded"] / seconds,
            **{
                key: {**benchmark.percentiles(v), "count": len(v)}
                for key, v in observations[kind].items()
            },
            "latency_all_ms": benchmark.percentiles(all_latencies[kind]),
            "hub_first_dispatch_max_ms": max(
                observations[kind]["hub_first_dispatch_ms"], default=None
            ),
            "p99_exploratory": c["succeeded"] < 1000,
        }

    return {
        "workload": name,
        "level": level,
        "planned": expected,
        "observed": n,
        "success_per_second": successes / seconds,
        "by_kind": {k: metric(k, c) for k, c in counts.items() if c},
        "median_load_per_cpu": round(statistics.median(loads), 4) if loads else None,
        "max_load_per_cpu": round(max(loads), 4) if loads else None,
        **({"send_lag_ms": send_lag} if open_loop else {}),
        **({"burst_terminal_ms_max": round(max(terminal_offsets), 3)} if terminal_offsets else {}),
    }, issues


def safe_report(before, after):
    manifests = [read_json(path / "manifest.json") for path in (before, after)]
    a, b = manifests
    plans = [plan(m) for m in manifests]
    ids = [identity(m) for m in manifests]
    keys = COMMON + (
        ("concurrency", "operations_per_worker") if a["mode"] == "closed_loop" else ("open_loop",)
    )
    mismatch = [key for key in keys if a.get(key) != b.get(key)]
    if (
        mismatch
        or plans[0] != plans[1]
        or not re.fullmatch(r"[a-f0-9]{64}", a["harness_sha256"])
        or not number(a["cpu_count"])
        or not a["cpu_count"]
    ):
        return {"status": "incompatible", "mismatched_fields": mismatch}
    issues = set.union(
        *(manifest_issues(m, var) for m, var in zip(manifests, ("before", "after"), strict=True))
    )
    if a.get("host_context") != b.get("host_context"):
        issues.add("host_activity_different")
    if not all(
        number(m.get("started_at"))
        and number(m.get("ended_at"))
        and m["ended_at"] >= m["started_at"]
        for m in manifests
    ):
        issues.add("run_times_incomplete")
    elif max(a["started_at"], b["started_at"]) < min(a["ended_at"], b["ended_at"]):
        issues.add("variants_overlapped")
    if ids[0] == ids[1]:
        issues.add("identical_sources")
    cards = []
    for label, dims in plans[0].items():
        values = []
        local_issues = set()
        for directory, m in zip((before, after), manifests, strict=True):
            try:
                run_cell = directory / label
                summary = read_json(run_cell / "summary.json")
                if summary.get("mode") != m["mode"] or summary.get("workload") != dims[0]:
                    raise ValueError("cell_mismatch")
                data, reasons = cell(run_cell, summary, dims, m["mode"], m["cpu_count"])
                local_issues.update(reasons)
                values.append(data)
            except (OSError, KeyError, TypeError, ValueError, AttributeError):
                values.append(None)
                local_issues.add("cell_evidence_missing_or_invalid")
        loads = [
            v["median_load_per_cpu"] for v in values if v and v["median_load_per_cpu"] is not None
        ]
        if len(loads) == 2 and abs(loads[1] - loads[0]) > 0.2:
            local_issues.add("host_load_differs")
        card = {
            "case": label,
            "before": values[0],
            "after": values[1],
            "issues": sorted(local_issues),
        }
        reads = [v["by_kind"].get("read") if v else None for v in values]
        if all(reads):
            short_a, short_b = reads
            hub = [r["hub_first_dispatch_ms"]["p95"] for r in reads]
            e2e = [r["latency_ms"]["p95"] for r in reads]
            if hub[0] and hub[1] is not None and None not in e2e:
                reduction = 1 - hub[1] / hub[0]
                card["observed_short_read"] = {
                    "first_dispatch_p95_reduction_pct": round(reduction * 100, 2),
                    "meets_numeric_targets_in_this_pair": (
                        reduction >= 0.5
                        and e2e[1] < e2e[0]
                        and short_b["success_per_second"] >= short_a["success_per_second"]
                        and values[1]["success_per_second"] >= values[0]["success_per_second"]
                        and all(
                            short_b[k] / short_b["samples"] <= short_a[k] / short_a["samples"]
                            for k in ("rejected", "timeout")
                        )
                    ),
                }
                if min(r["succeeded"] for r in reads) < 1000:
                    local_issues.add("short_read_sample_size_exploratory")
        card["issues"] = sorted(local_issues)
        cards.append(card)
    # Only open-loop arrivals measure the stated fixed-arrival-load target.
    if a["mode"] == "closed_loop":
        issues.add("closed_loop_not_fixed_arrival_rate")
    if any(card["issues"] for card in cards):
        issues.add("one_or_more_cells_diagnostic")
    for card in cards:
        observation = card.get("observed_short_read")
        if observation:
            observation["status"] = (
                "diagnostic"
                if issues
                else (
                    "observed_met"
                    if observation["meets_numeric_targets_in_this_pair"]
                    else "observed_missed"
                )
            )
    return {
        "status": "diagnostic" if issues else "single_pair",
        "mode": a["mode"],
        "scope": "Single loopback fixture pair. Formal qualification requires repeated ABBA runs.",
        "before_source": ids[0],
        "after_source": ids[1],
        "harness_sha256": a["harness_sha256"],
        "issues": sorted(issues),
        "cells": cards,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        report = safe_report(args.before, args.after)
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        report = {"status": "incompatible", "reason": "manifest_missing_or_invalid"}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if report["status"] == "incompatible" else 0


if __name__ == "__main__":
    raise SystemExit(main())
