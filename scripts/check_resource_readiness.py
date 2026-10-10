#!/usr/bin/env python3
"""Read-only bounded runner prerequisite; never change Agent admission or retry tests."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.resource_pressure import ResourceSampler
from agent.scheduler import Pressure


def healthy(sample, now):
    # Same conservative growth prerequisites as the real adaptive scheduler.
    # These observations are never supplied to the Agent or its heartbeat.
    return (isinstance(sample, Pressure) and sample.valid(now)
            and sample.cpu_busy is not None and sample.cpu_busy <= .65
            and sample.memory_available >= 1024**3
            and sample.memory_available / sample.memory_total >= .20
            and (sample.io_stall or 0) < .10)


def wait_ready(sampler, *, timeout=300, stable_seconds=15, interval=3,
               clock=time.monotonic, sleep=time.sleep, emit=print):
    if not (0 < stable_seconds < timeout <= 600 and 0 < interval <= 5):
        raise ValueError("Readiness requires a bounded positive stable window and timeout")
    started = clock()
    stable_since = None
    samples = 0
    while True:
        sample = sampler.sample()
        now = clock()
        samples += 1
        ready = healthy(sample, now)
        stable_since = (now if stable_since is None else stable_since) if ready else None
        stable = 0 if stable_since is None else now - stable_since
        event = {"elapsed_seconds": round(now - started, 3), "samples": samples,
                 "stable_seconds": round(stable, 3), "healthy": ready,
                 "source": sample.source, "cpu_busy": sample.cpu_busy,
                 "memory_available": sample.memory_available,
                 "memory_total": sample.memory_total, "io_stall": sample.io_stall,
                 "reason": "observing_stable_window" if ready else "waiting_for_real_os_health"}
        # Deadline is checked before success: late samples do not extend the budget.
        if now - started >= timeout:
            emit(json.dumps({**event, "state": "failed", "reason": "runner_prerequisite_timeout"}, sort_keys=True))
            return False
        if stable >= stable_seconds:
            emit(json.dumps({**event, "state": "ready"}, sort_keys=True))
            return True
        emit(json.dumps({**event, "state": "waiting"}, sort_keys=True))
        sleep(min(interval, timeout - (now - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--stable-seconds", type=float, default=15)
    args = parser.parse_args()
    return 0 if wait_ready(ResourceSampler(), timeout=args.timeout,
                           stable_seconds=args.stable_seconds,
                           emit=lambda value: print(value, flush=True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
