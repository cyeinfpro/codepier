"""Bounded, secret-free Hub delivery stage timings for first-delivery analysis.

Monotonic, in-process aggregates only: no SQL, payloads, actors, paths or
project names, and nothing is written to SQLite on the delivery path. Slow
samples carry the opaque operation ID so an instance administrator can open
the durable trace of that same operation. A restart discards these numbers;
they are evidence for a measurement window, never an authorization input.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from math import isfinite
from threading import Lock
import time

from hub.worker_metrics import percentiles

# Ordered as one operation passes through the Hub before its packet is written.
STAGES = (
    "candidate_wait",  # due (max(created,next_attempt)) -> selected by loop/refill
    "batch_wait",  # selected -> first local attempt to acquire a global slot
    "slot_wait",  # global decrypted-request slot (delivery_slots)
    "device_lock_wait",  # per-device dispatch lock before the DB phase
    "prepare_queue",  # DB writer queue before the preparation phase ran
    "prepare_run",  # preparation phase on the DB writer
    "send_turn",  # prepared -> its turn in the strictly ordered device send
    "send_lock_wait",  # connection send lock (another packet on the socket)
    "fence_queue",  # control-lane queue before the final authority fence ran
    "fence_run",  # final current-authority fence while holding the send lock
    "send",  # pack and socket write after the fence
    "total",  # due -> sent, settled or failed
)
STAGE_NAMES = frozenset(STAGES)
# A first call, a retried call, recovery probes, cancellation and offline
# backoff have different meanings and must never share one percentile.
KINDS = (
    "first_call",
    "retry_call",
    "probe",
    "cancel",
    "cancel_pending",
    "offline_backoff",
    "settled",
    "fenced",
    "failed",
)
SLOW_MS = 1000.0
CONNECTION_EVENTS = ("connected", "replaced", "disconnected")


def classify(prepared, *, attempts=None, offline=False):
    """Name what a preparation produced without inspecting request contents."""
    if prepared is None:
        return "offline_backoff" if offline else "settled"
    types = {packet.get("type") for packet in prepared[2]}
    for kind in ("cancel", "cancel_pending", "probe"):
        if kind in types:
            return kind
    if "call" in types:
        return "first_call" if attempts == 0 else "retry_call"
    return "settled"


class DeliveryTiming:
    """One operation's stage durations within one delivery attempt."""

    __slots__ = ("identifier", "selected", "stages", "kind", "attempts", "payload_bytes")

    def __init__(self, identifier, *, due=None, attempts=None, now=None):
        self.identifier = identifier
        self.selected = time.monotonic()
        self.stages = {}
        self.kind = None
        self.attempts = attempts
        self.payload_bytes = 0
        if due is not None:
            wall = time.time() if now is None else now
            self.stages["candidate_wait"] = max(0.0, (wall - due) * 1000)

    def add(self, stage, started, ended=None):
        if stage not in STAGE_NAMES:
            return
        ended = time.monotonic() if ended is None else ended
        self.stages[stage] = self.stages.get(stage, 0.0) + max(0.0, (ended - started) * 1000)


def timed_phase(timing, queue_stage, run_stage, function, /, *args, **kwargs):
    """Wrap one synchronous Store phase so worker queue and run time are split."""
    submitted = time.monotonic()

    def phase():
        started = time.monotonic()
        timing.add(queue_stage, submitted, started)
        try:
            return function(*args, **kwargs)
        finally:
            timing.add(run_stage, started)

    return phase


def _summary(values):
    return {**percentiles(values), "max": round(max(values), 3) if values else None}


class DeliveryMetrics:
    """Numeric delivery aggregates; bounded memory, safe to read from any thread."""

    def __init__(self, sample_limit=1024, slow_limit=32, slow_ms=SLOW_MS):
        if type(sample_limit) is not int or not 1 <= sample_limit <= 8192:
            raise ValueError("Delivery sample limit must be within 1..8192")
        if type(slow_limit) is not int or not 0 <= slow_limit <= 32:
            raise ValueError("Delivery slow sample limit must be within 0..32")
        if not isfinite(slow_ms) or slow_ms < 0:
            raise ValueError("Delivery slow threshold must be finite and nonnegative")
        self._lock = Lock()
        self._limit = sample_limit
        self._stages = {}
        self._counts = dict.fromkeys(KINDS, 0)
        self._slow = deque(maxlen=slow_limit)
        self.slow_ms = slow_ms
        self._rounds = deque(maxlen=sample_limit)
        self._select_ms = deque(maxlen=sample_limit)
        self._batch_items = deque(maxlen=sample_limit)
        self._batch_age = deque(maxlen=sample_limit)
        self._active = Counter()
        self._maintenance = deque(maxlen=sample_limit)
        self._maintenance_queue = deque(maxlen=sample_limit)
        self._maintenance_run = deque(maxlen=sample_limit)
        self._loop_lag = deque(maxlen=sample_limit)
        self._prepared = self._prepared_peak = 0
        self._prepared_bytes = self._prepared_peak_bytes = 0
        self._connections = dict.fromkeys(CONNECTION_EVENTS, 0)
        self.refill_errors = 0
        self.since = time.time()

    def finish(self, timing):
        kind = timing.kind if timing.kind in self._counts else "settled"
        timing.stages["total"] = timing.stages.get("candidate_wait", 0.0) + max(
            0.0, (time.monotonic() - timing.selected) * 1000
        )
        with self._lock:
            self._counts[kind] += 1
            for stage, value in timing.stages.items():
                if stage not in STAGE_NAMES:
                    continue
                key = (kind, stage)
                if key not in self._stages:
                    self._stages[key] = deque(maxlen=self._limit)
                self._stages[key].append(value)
            if self._slow.maxlen and timing.stages["total"] >= self.slow_ms:
                self._slow.append(
                    {
                        "operation_id": timing.identifier,
                        "kind": kind,
                        "at": round(time.time(), 3),
                        "stages_ms": {
                            stage: round(value, 3)
                            for stage, value in timing.stages.items()
                            if stage in STAGE_NAMES
                        },
                    }
                )

    def round(self, candidates, *, elapsed_ms=None):
        with self._lock:
            self._rounds.append(candidates)
            if elapsed_ms is not None:
                self._select_ms.append(elapsed_ms)

    def start_batch(self):
        started = time.monotonic()
        with self._lock:
            self._active[started] += 1
        return started

    def batch(self, items, age_ms, *, started=None):
        with self._lock:
            self._batch_items.append(items)
            self._batch_age.append(age_ms)
            if started is not None:
                self._active[started] -= 1
                if not self._active[started]:
                    del self._active[started]

    def maintenance(self, elapsed_ms, *, queue_ms=None, run_ms=None):
        with self._lock:
            self._maintenance.append(elapsed_ms)
            if queue_ms is not None:
                self._maintenance_queue.append(queue_ms)
            if run_ms is not None:
                self._maintenance_run.append(run_ms)

    def retain_payload(self, size):
        # Original UTF-8 request length, measured once at decryption. Counts
        # completed preparations still held for ordered send, not Python RSS.
        with self._lock:
            self._prepared += 1
            self._prepared_bytes += size
            self._prepared_peak = max(self._prepared_peak, self._prepared)
            self._prepared_peak_bytes = max(self._prepared_peak_bytes, self._prepared_bytes)

    def release_payload(self, size):
        with self._lock:
            self._prepared -= 1
            self._prepared_bytes -= size

    def connection(self, kind):
        if kind in CONNECTION_EVENTS:
            with self._lock:
                self._connections[kind] += 1

    def loop_lag(self, lag_ms):
        with self._lock:
            self._loop_lag.append(max(0.0, lag_ms))

    def refill_error(self):
        with self._lock:
            self.refill_errors += 1

    def snapshot(self):
        # This runs on the diagnostics reader; sorting hundreds of thousands
        # of samples while holding the lock would stall the delivery loop.
        with self._lock:
            stages = {key: tuple(values) for key, values in self._stages.items()}
            counts = dict(self._counts)
            connections = dict(self._connections)
            rounds, select_ms = tuple(self._rounds), tuple(self._select_ms)
            items, age = tuple(self._batch_items), tuple(self._batch_age)
            now = time.monotonic()
            oldest_active_ms = (now - min(self._active)) * 1000 if self._active else None
            active = self._active.total()
            maintenance = tuple(self._maintenance)
            maintenance_queue = tuple(self._maintenance_queue)
            maintenance_run = tuple(self._maintenance_run)
            loop_lag, refill_errors = tuple(self._loop_lag), self.refill_errors
            slow = [{**sample, "stages_ms": dict(sample["stages_ms"])} for sample in self._slow]
            prepared = {
                "measurement": "prepared_request_utf8_bytes_before_slot_release",
                "count": self._prepared,
                "peak_count": self._prepared_peak,
                "bytes": self._prepared_bytes,
                "peak_bytes": self._prepared_peak_bytes,
            }
        summaries = {}
        for kind in KINDS:
            for stage in STAGES:
                values = stages.get((kind, stage))
                if values:
                    summaries.setdefault(kind, {})[stage] = {
                        **_summary(values),
                        "count": len(values),
                    }
        return {
            "scope": "hub_process",
            "since": self.since,
            "clock": "monotonic",
            "candidate_wait_clock": "persisted_due_to_selection_wall_time",
            "percentile_method": "nearest_rank",
            "sample_limit": self._limit,
            "slow_threshold_ms": self.slow_ms,
            "counts": counts,
            "connection_events": connections,
            "stages_ms": summaries,
            "rounds": {
                "count": len(rounds),
                "candidates": _summary(rounds),
                "select_ms": _summary(select_ms),
            },
            "device_batches": {
                "count": len(items),
                "items": _summary(items),
                "age_ms": _summary(age),
                "active": active,
                "oldest_active_ms": round(oldest_active_ms, 3) if active else None,
            },
            "prepared_payloads": prepared,
            "maintenance_ms": _summary(maintenance),
            "maintenance_queue_ms": _summary(maintenance_queue),
            "maintenance_run_ms": _summary(maintenance_run),
            "loop_lag_ms": _summary(loop_lag),
            "refill_errors": refill_errors,
            "slow_samples": slow,
        }


async def monitor_loop_lag(metrics, stopping, interval=0.25):
    """Sample event-loop scheduling delay; long lag explains waits outside DB/locks."""
    while not stopping():
        started = time.monotonic()
        await asyncio.sleep(interval)
        metrics.loop_lag((time.monotonic() - started - interval) * 1000)
