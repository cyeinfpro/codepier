"""Bounded, secret-free in-process DB worker timings; never an authorization cache."""
from __future__ import annotations

from collections import deque
from math import ceil
from threading import Lock
import time


def percentiles(values):
    values = sorted(values)
    if not values:
        return {'p50': None, 'p95': None, 'p99': None}
    return {name: round(values[min(len(values)-1, ceil(len(values)*fraction)-1)], 3)
            for name, fraction in [('p50', .50), ('p95', .95), ('p99', .99)]}


class WorkerMetrics:
    """Numeric aggregates only: no SQL, parameters, labels, actors, or paths."""
    def __init__(self, sample_limit=2048):
        self._lock = Lock()
        self._queue = deque(maxlen=sample_limit)
        self._run = deque(maxlen=sample_limit)
        self.submitted = self.started = self.completed = self.failed = 0
        self.peak_pending = 0
        self.since = time.time()

    def submit(self):
        with self._lock:
            self.submitted += 1
            self.peak_pending = max(self.peak_pending, self.submitted-self.completed)
        return time.monotonic()

    def start(self, submitted):
        now = time.monotonic()
        with self._lock:
            self.started += 1
            self._queue.append(max(0, (now-submitted)*1000))
        return now

    def finish(self, started, *, failed=False):
        with self._lock:
            self.completed += 1
            self.failed += bool(failed)
            self._run.append(max(0, (time.monotonic()-started)*1000))

    def snapshot(self):
        with self._lock:
            return {'scope': 'hub_process', 'since': self.since,
                    'submitted': self.submitted, 'completed': self.completed,
                    'failed': self.failed, 'queued': self.submitted-self.started,
                    'running': self.started-self.completed, 'peak_pending': self.peak_pending,
                    'sample_limit': self._queue.maxlen, 'sample_count': len(self._queue),
                    'phase_sample_count': len(self._run), 'percentile_method': 'nearest_rank',
                    'queue_ms': percentiles(self._queue), 'phase_ms': percentiles(self._run)}
