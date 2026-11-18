"""Bounded, secret-free in-process DB worker timings; never an authorization cache."""

from __future__ import annotations

from collections import deque
from math import ceil
from threading import Lock
import time


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    return {
        name: round(values[min(len(values) - 1, ceil(len(values) * fraction) - 1)], 3)
        for name, fraction in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]
    }


class WorkerMetrics:
    """Numeric aggregates only: no SQL, parameters, labels, actors, or paths."""

    def __init__(self, sample_limit=2048):
        if type(sample_limit) is not int or not 1 <= sample_limit <= 8192:
            raise ValueError("Worker sample limit must be within 1..8192")
        self._lock = Lock()
        self._queue = deque(maxlen=sample_limit)
        self._run = deque(maxlen=sample_limit)
        self.submitted = self.started = self.completed = self.failed = self.rejected = 0
        self.read_retries = {"session_revision": 0, "data_version": 0}
        self.read_fence_ms = deque(maxlen=sample_limit)
        self.peak_pending = 0
        self.running_since = None
        self.since = time.time()

    def submit(self):
        with self._lock:
            self.submitted += 1
            self.peak_pending = max(
                self.peak_pending, self.submitted - self.completed - self.rejected
            )
        return time.monotonic()

    def reject(self):
        # The executor rejected submission before any phase was admitted.
        with self._lock:
            self.rejected += 1

    def start(self, submitted):
        now = time.monotonic()
        with self._lock:
            if self.started == self.completed:
                self.running_since = now
            self.started += 1
            self._queue.append(max(0, (now - submitted) * 1000))
        return now

    def finish(self, started, *, failed=False):
        with self._lock:
            self.completed += 1
            if self.started == self.completed:
                self.running_since = None
            self.failed += bool(failed)
            self._run.append(max(0, (time.monotonic() - started) * 1000))

    def read_retry(self, revision_changed, data_changed):
        with self._lock:
            self.read_retries["session_revision"] += int(revision_changed)
            self.read_retries["data_version"] += int(data_changed)

    def read_fenced(self, started):
        with self._lock:
            self.read_fence_ms.append((time.monotonic() - started) * 1000)

    def responsive(self, *, max_queue, max_running_seconds):
        with self._lock:
            queued = self.submitted - self.started - self.rejected
            running = self.running_since
            return queued < max_queue and (
                running is None or time.monotonic() - running < max_running_seconds
            )

    def snapshot(self):
        with self._lock:
            return {
                "scope": "hub_process",
                "since": self.since,
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "rejected": self.rejected,
                "accepted": self.submitted - self.rejected,
                "queued": self.submitted - self.started - self.rejected,
                "running": self.started - self.completed,
                "peak_pending": self.peak_pending,
                "sample_limit": self._queue.maxlen,
                "sample_count": len(self._queue),
                "phase_sample_count": len(self._run),
                "percentile_method": "nearest_rank",
                "queue_ms": percentiles(self._queue),
                "phase_ms": percentiles(self._run),
                "read_retries": dict(self.read_retries),
                "read_fence_ms": percentiles(self.read_fence_ms),
            }
