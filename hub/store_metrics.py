"""Bounded, process-local transaction aggregates for internal diagnosis.

No query, argument, project, user, path, callback, or exception is retained.
Transaction outcomes are measured at SQLite completion, before after-commit
callbacks; this collector is never an authorization cache.
"""
from collections import deque
from math import isfinite
from threading import Lock
import time

from hub.worker_metrics import percentiles


class TransactionMetrics:
    OUTCOMES = frozenset({'committed', 'rolled_back', 'commit_failed', 'rollback_failed'})

    def __init__(self, sample_limit=2048):
        if type(sample_limit) is not int or not 1 <= sample_limit <= 8192:
            raise ValueError('Transaction sample limit must be within 1..8192')
        self._lock = Lock()
        self._durations = deque(maxlen=sample_limit)
        self._outcomes = dict.fromkeys(self.OUTCOMES, 0)
        self._count = 0
        self._total_ms = self._max_ms = 0.0
        self.since = time.time()

    def observe(self, duration_ms, outcome):
        if outcome not in self.OUTCOMES or not isfinite(duration_ms) or duration_ms < 0:
            raise ValueError('Invalid numeric transaction observation')
        with self._lock:
            self._count += 1
            self._outcomes[outcome] += 1
            self._durations.append(duration_ms)
            self._total_ms += duration_ms
            self._max_ms = max(self._max_ms, duration_ms)

    def snapshot(self):
        with self._lock:
            return {'scope': 'hub_process', 'since': self.since,
                    'count': self._count, **self._outcomes,
                    'sample_limit': self._durations.maxlen,
                    'sample_count': len(self._durations), 'percentile_method': 'nearest_rank',
                    'duration_ms': percentiles(self._durations),
                    'total_duration_ms': round(self._total_ms, 3),
                    'max_duration_ms': round(self._max_ms, 3),
                    'measurement': 'outer_sqlite_transaction_before_after_commit_callbacks'}


class LockMetrics:
    """Bounded outer lock waits/holds, independent of DB worker queue time."""
    def __init__(self, sample_limit=2048):
        if type(sample_limit) is not int or not 1 <= sample_limit <= 8192:
            raise ValueError('Lock sample limit must be within 1..8192')
        self._lock = Lock()
        self._wait = deque(maxlen=sample_limit)
        self._held = deque(maxlen=sample_limit)
        self._count = 0
        self._total_wait = self._total_held = self._max_wait = self._max_held = 0.0
        self.since = time.time()

    def observe(self, wait_ms, held_ms):
        if not all(isfinite(value) and value >= 0 for value in (wait_ms, held_ms)):
            raise ValueError('Invalid numeric lock observation')
        with self._lock:
            self._count += 1
            self._wait.append(wait_ms)
            self._held.append(held_ms)
            self._total_wait += wait_ms
            self._total_held += held_ms
            self._max_wait = max(self._max_wait, wait_ms)
            self._max_held = max(self._max_held, held_ms)

    def snapshot(self):
        with self._lock:
            return {'scope': 'hub_process', 'since': self.since, 'count': self._count,
                    'sample_limit': self._wait.maxlen, 'sample_count': len(self._wait),
                    'percentile_method': 'nearest_rank',
                    'wait_ms': percentiles(self._wait), 'held_ms': percentiles(self._held),
                    'total_wait_ms': round(self._total_wait, 3),
                    'total_held_ms': round(self._total_held, 3),
                    'max_wait_ms': round(self._max_wait, 3),
                    'max_held_ms': round(self._max_held, 3),
                    'measurement': 'outer_store_lock_completed_acquisitions'}
