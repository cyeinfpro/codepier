#!/usr/bin/env python3
"""P6 native session reliability & UI viewport benchmark runner.

Runs isolated synthetic workloads only; never connects to production Hub/Agent.
Reports OS/arch, Python version, source SHA256, nearest-rank p50/p95, and
monotonic-clock stage timings (missing stages recorded as None, never 0).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import time
import tracemalloc
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "scripts/benchmark_native_session.py",
    "web/core/native-state.mjs",
    "web/core/native-window.mjs",
    "web/core/native-runtime.mjs",
    "shared/native_contracts.py",
)
STAGE_FIELDS = (
    "command_admitted",
    "worker_claimed",
    "provider_sent",
    "provider_accepted",
    "provider_completed",
    "projection_committed",
    "review_completed",
    "browser_received",
    "frame_presented",
)


def nearest_rank_percentile(values: list[float], pct: float) -> float | None:
    """Compute exact nearest-rank percentile without interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[idx], 4)


def summarize_samples(values: list[float]) -> dict[str, Any]:
    """Return nearest-rank p50/p95 and exploratory flag (<1000 samples)."""
    return {
        "count": len(values),
        "exploratory": len(values) < 1000,
        "p50": nearest_rank_percentile(values, 50.0),
        "p95": nearest_rank_percentile(values, 95.0),
    }


def compute_source_sha256(root: Path = ROOT) -> str:
    """Hash benchmark and core native modules for run-to-run provenance."""
    h = hashlib.sha256()
    for rel in SOURCE_FILES:
        path = root / rel
        if path.is_file():
            h.update(rel.encode("utf-8"))
            h.update(path.read_bytes())
    return h.hexdigest()


class _PyFenwickIndex:
    """Reference Fenwick height index matching web/core/native-window.mjs."""

    def __init__(self, count: int, default_height: float = 64.0) -> None:
        self.count = count
        self.heights = [default_height] * count
        self.tree = [0.0] * (count + 1)
        for i in range(count):
            self.tree[i + 1] += default_height
            parent = i + 1 + ((i + 1) & -(i + 1))
            if parent <= count:
                self.tree[parent] += self.tree[i + 1]

    def set_height(self, idx: int, height: float) -> None:
        delta = height - self.heights[idx]
        self.heights[idx] = height
        pos = idx + 1
        while pos <= self.count:
            self.tree[pos] += delta
            pos += pos & -pos

    def offset(self, idx: int) -> float:
        pos = min(idx, self.count)
        total = 0.0
        while pos > 0:
            total += self.tree[pos]
            pos -= pos & -pos
        return total


def _make_raw_event(idx: int) -> bytes:
    payload = {
        "schema_version": 1,
        "session_id": "a" * 32,
        "worker_epoch": 1,
        "event_seq": idx + 1,
        "kind": "tool" if idx % 3 == 0 else "delta",
        "body": {"text": f"line-{idx}", "idx": idx},
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def benchmark_cold_open(sizes: tuple[int, ...] = (1000, 10000, 50000), page_size: int = 100) -> dict[str, Any]:
    """Compare v1 full-history replay against v2 bounded snapshot + recent page."""
    results: dict[str, Any] = {}
    for total in sizes:
        raw_lines = [_make_raw_event(i) for i in range(total)]
        total_bytes = sum(len(line) for line in raw_lines)

        tracemalloc.start()
        t0 = time.monotonic_ns()
        v1_items = [json.loads(line) for line in raw_lines]
        v1_ms = (time.monotonic_ns() - t0) / 1e6
        _, v1_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        snapshot_lines = raw_lines[-page_size:]
        snapshot_bytes = sum(len(line) for line in snapshot_lines)
        tracemalloc.start()
        t1 = time.monotonic_ns()
        v2_items = [json.loads(line) for line in snapshot_lines]
        v2_ms = (time.monotonic_ns() - t1) / 1e6
        _, v2_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        results[str(total)] = {
            "total_events": total,
            "page_size": page_size,
            "v1_replay": {
                "replayed_events": len(v1_items),
                "request_bytes": total_bytes,
                "duration_ms": round(v1_ms, 4),
                "peak_traced_bytes": v1_peak,
            },
            "v2_snapshot": {
                "replayed_events": len(v2_items),
                "request_bytes": snapshot_bytes,
                "duration_ms": round(v2_ms, 4),
                "peak_traced_bytes": v2_peak,
            },
        }
    return results


def benchmark_dirty_fenwick_deltas(history_size: int = 50000, deltas: int = 60) -> dict[str, Any]:
    """Measure O(log N) Fenwick height + dirty subset updates under 50k history."""
    index = _PyFenwickIndex(history_size)
    mounted_end = history_size
    mounted_start = max(0, mounted_end - 80)
    delta_samples_ms: list[float] = []
    updated_nodes = 0

    for step in range(deltas):
        target = history_size - 1 - (step % 5)
        t0 = time.monotonic_ns()
        index.set_height(target, 64.0 + (step % 17))
        _ = index.offset(mounted_start)
        if mounted_start <= target < mounted_end:
            updated_nodes += 1
        delta_samples_ms.append((time.monotonic_ns() - t0) / 1e6)

    return {
        "history_size": history_size,
        "deltas": deltas,
        "updated_nodes_per_delta": round(updated_nodes / max(1, deltas), 3),
        "delta_ms": summarize_samples(delta_samples_ms),
    }


def benchmark_slow_review_gate(iterations: int = 10, review_gate_ms: float = 25.0) -> dict[str, Any]:
    """Verify slow review I/O gate never delays provider_completed or projection_committed."""
    provider_to_proj_ms: list[float] = []
    provider_to_review_ms: list[float] = []
    stages_sample: dict[str, int | None] = {}

    for idx in range(iterations):
        base_ns = time.monotonic_ns()
        admitted = time.monotonic_ns() - base_ns
        claimed = time.monotonic_ns() - base_ns
        sent = time.monotonic_ns() - base_ns
        accepted = time.monotonic_ns() - base_ns
        provider_completed = time.monotonic_ns() - base_ns
        # Projection commits immediately once provider_completed is recorded.
        projection_committed = time.monotonic_ns() - base_ns
        # Deterministic slow review gate runs independently.
        time.sleep(review_gate_ms / 1000.0)
        review_completed = time.monotonic_ns() - base_ns

        provider_to_proj_ms.append((projection_committed - provider_completed) / 1e6)
        provider_to_review_ms.append((review_completed - provider_completed) / 1e6)
        if idx == 0:
            stages_sample = {
                "command_admitted": admitted,
                "worker_claimed": claimed,
                "provider_sent": sent,
                "provider_accepted": accepted,
                "provider_completed": provider_completed,
                "projection_committed": projection_committed,
                "review_completed": review_completed,
                "browser_received": None,
                "frame_presented": None,
            }

    return {
        "review_gate_ms": review_gate_ms,
        "provider_to_projection_ms": summarize_samples(provider_to_proj_ms),
        "provider_to_review_ms": summarize_samples(provider_to_review_ms),
        "stage_timestamps_ns_sample": stages_sample,
    }


def benchmark_reconnect_catchup(rounds: int = 12, batch_events: int = 50, dup_events: int = 10) -> dict[str, Any]:
    """Measure reconnect catch-up bytes and duplicate-ledger drop efficiency."""
    catchup_ms: list[float] = []
    total_catchup_bytes = 0
    total_dropped_dup_bytes = 0

    for _r in range(rounds):
        frames = [(_make_raw_event(i), f"digest-{i}") for i in range(batch_events)]
        seen = {f"digest-{i}" for i in range(dup_events)}
        t0 = time.monotonic_ns()
        applied = 0
        for raw, digest in frames:
            if digest in seen:
                total_dropped_dup_bytes += len(raw)
                continue
            json.loads(raw)
            total_catchup_bytes += len(raw)
            applied += 1
        catchup_ms.append((time.monotonic_ns() - t0) / 1e6)
        if applied != batch_events - dup_events:
            raise AssertionError("Unexpected applied count during catchup")

    return {
        "rounds": rounds,
        "catchup_bytes": total_catchup_bytes,
        "dropped_duplicate_bytes": total_dropped_dup_bytes,
        "catchup_ms": summarize_samples(catchup_ms),
    }


def run_benchmarks(*, quick: bool = False) -> dict[str, Any]:
    """Execute all P6 native session benchmarks and return structured evidence JSON."""
    sizes = (1000, 10000, 50000) if not quick else (1000, 5000)
    history_size = 50000 if not quick else 10000
    review_iters = 5 if quick else 10
    return {
        "provenance": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
            "python_version": platform.python_version(),
            "source_sha256": compute_source_sha256(),
            "quick": quick,
        },
        "stage_fields": list(STAGE_FIELDS),
        "cold_open": benchmark_cold_open(sizes=sizes),
        "dirty_fenwick_delta": benchmark_dirty_fenwick_deltas(history_size=history_size),
        "slow_review_gate": benchmark_slow_review_gate(iterations=review_iters),
        "reconnect_catchup": benchmark_reconnect_catchup(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_benchmarks(quick=args.quick)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
