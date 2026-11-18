#!/usr/bin/env python3
"""Replay a simultaneous five-directory fs_tree burst on a disposable loopback stack.

Run the same pair of harness files on --repository before / after. Output belongs
to a new .work directory below a local temporary directory. Only summary.json is
suitable to share; the fixture creates throwaway credentials, a DB and logs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
import uuid
from pathlib import Path

if __package__:
    from . import benchmark_hub_agent as bench
else:
    import benchmark_hub_agent as bench

COUNT = 5
TIMEOUT = 90
HUB = ("hub_received", "dispatched", "hub_completed")
AGENT = ("accepted", "executing", "persisting", "result_ready")


def stage_times(sample):
    """Surface only same-clock intervals for one completed, unambiguous attempt."""
    operation = sample.get("operation", {})
    result = {"attempts": operation.get("attempts"), "trace_complete": False}
    if operation.get("attempts") != 1 or operation.get("state") != "succeeded":
        return result
    for source, allowed, key in (
        ("hub", HUB, "at"),
        ("agent", AGENT, "elapsed_ms"),
    ):
        events = sorted(
            (
                e
                for e in sample.get("events", [])
                if e["source"] == source and e["stage"] in allowed
            ),
            key=lambda e: e["seq"],
        )
        observed = tuple(e["stage"] for e in events)
        expected = tuple(stage for stage in allowed if stage in observed)
        if observed != expected or observed[:2] != allowed[:2] or len(observed) < 3:
            return result
        times = {e["stage"]: e[key] for e in events}
        values = list(times.values())
        if any(v is None for v in values) or values != sorted(values):
            return result
        result[f"{source}_{key}"] = times
        first, second, last = values[0], values[1], values[-1]
        if source == "hub":
            result["hub_first_dispatch_ms"] = round((second - first) * 1000, 3)
            result["hub_after_dispatch_ms"] = round((last - second) * 1000, 3)
        else:
            result["agent_wait_ms"] = second - first
            # The fast path can attach the trace before recording result_ready.
            # Keep the measured endpoint, so a persisting sample stays distinct.
            result[f"agent_to_{observed[-1]}_ms"] = last - first
    return {**result, "trace_complete": True}


async def read_tree(client, database, index):
    path = f"probe-{index}"
    sample = {"directory": index, "ok": False}
    key = uuid.uuid4().hex
    began = time.monotonic()
    try:
        data, failed = await bench.rpc(
            client,
            "project_query",
            {
                "operation": "tree",
                "project": "ProjectAlpha",
                "path": path,
                "depth": 1,
                "options": {"format": "structured"},
                "idempotency_key": key,
            },
        )
        sample["operation_id"] = data.get("operation_id") or data.get("id")
        identifier, result, failed, polls, state = await bench.finish_original(
            client, data, failed, began + TIMEOUT
        )
        sample.update(operation_id=identifier, polls=polls, state=state)
        if failed:
            bench.record_failure(sample, result, state)
        else:
            entries = result.get("entries", [])
            sample["ok"] = (
                len(entries) == 1
                and isinstance(entries[0], dict)
                and entries[0].get("path") == f"{path}/marker.txt"
                and entries[0].get("type") == "file"
            )
            if not sample["ok"]:
                sample["error"] = "tree_content_mismatch"
    except Exception as exc:
        sample["latency_ms"] = round((time.monotonic() - began) * 1000, 3)
        # Never reissue a tree call, even though this is a read-only fixture.
        await bench.recover_sample(client, database, key, sample, TIMEOUT, exc)
    else:
        sample["latency_ms"] = round((time.monotonic() - began) * 1000, 3)
    return sample


async def bursts(stack, rounds, samples):
    database = stack.hubdir / "hub.sqlite3"
    for index in range(COUNT):
        directory = stack.projectalpha / f"probe-{index}"
        directory.mkdir()
        (directory / "marker.txt").write_bytes(b"x\n")
    async with bench.measurement_client(stack, COUNT, TIMEOUT) as client:
        await bench.warmup(client, TIMEOUT)
        measured = 0.0
        for number in range(rounds):
            start = time.monotonic()
            batch = await asyncio.gather(*(read_tree(client, database, i) for i in range(COUNT)))
            measured += time.monotonic() - start
            bench.enrich(database, batch)
            for row in batch:
                row.update(round=number + 1, **stage_times(row))
                row.pop("operation", None)
                row.pop("events", None)
                samples.append(row)
            if any(not s.get("ok") or not s["trace_complete"] for s in batch):
                break
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--variant", choices=("before", "after"), required=True)
    parser.add_argument("--preflight-seconds", type=float, default=5)
    args = parser.parse_args()
    output, repo = args.output.resolve(), args.repository.resolve()
    roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    if not any(
        output.is_relative_to(root) and ".work" in output.relative_to(root).parts[:-1]
        for root in roots
    ):
        parser.error("output must be a new subdirectory of .work in a local temporary directory")
    if not (repo / ".git").exists() or output.exists() or not 1 <= args.rounds <= 32:
        parser.error("expected a Git checkout, new output directory, and 1..32 rounds")
    if not 0 <= args.preflight_seconds <= 60:
        parser.error("expected 0..60 preflight seconds")
    output.mkdir(mode=0o700, parents=True)
    metadata = {
        "variant": args.variant,
        "rounds": args.rounds,
        "repository": bench.git_identity(repo),
        "host_context": args.host_context,
        "platform": platform.platform(),
        "python": sys.version,
        "cpu_count": os.cpu_count(),
        "harness_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in map(Path, (__file__, bench.__file__))
        },
        "started_at": time.time(),
        "samples": [],
    }
    metadata["load_samples"], overloaded = bench.preflight(args.preflight_seconds, 1.0)
    sys.path.insert(0, str(repo))
    if overloaded:
        metadata["result"] = "blocked_by_host_load"
    else:
        try:
            from tests.support import running_stack

            with running_stack(output / "fixture") as stack:
                metadata["admission_before"] = bench.prepare_admission(
                    lambda **kw: bench.observe_admission(stack, **kw),
                    COUNT,
                    profile="cold",
                    timeout=10,
                )
                seconds = asyncio.run(bursts(stack, args.rounds, metadata["samples"]))
            rows = metadata["samples"]
            metadata["successes"] = sum(s["ok"] for s in rows)
            metadata["throughput_success_per_second"] = metadata["successes"] / seconds
            for key in ("hub_first_dispatch_ms", "agent_wait_ms", "latency_ms"):
                metadata[key] = bench.percentiles([s.get(key) for s in rows if s["ok"]])
            metadata["result"] = (
                "completed"
                if len(rows) == COUNT * args.rounds
                and all(s["ok"] and s["trace_complete"] for s in rows)
                else "incomplete"
            )
        except Exception as exc:
            metadata.update(result="failed", error=type(exc).__name__)
    metadata["ended_at"] = time.time()
    metadata["loadavg_end"] = list(os.getloadavg())
    metadata["repository_end"] = bench.git_identity(repo)
    if metadata["repository"] != metadata["repository_end"]:
        metadata["result"] = "source_changed_during_run"
    (output / "summary.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(metadata["result"], output / "summary.json")
    return 0 if metadata["result"] == "completed" else 2 if overloaded else 1


if __name__ == "__main__":
    raise SystemExit(main())
