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
