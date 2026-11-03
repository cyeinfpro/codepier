"""A single DB worker with fair, internal control-event precedence.

Each lane is FIFO. At most eight control phases precede a waiting normal phase;
an executing phase is never preempted. Admission and shutdown retain Executor's
existing contract: accepted work drains, rather than silently losing writes.
This bounds priority bursts, not the total number of pending application calls.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from threading import Lock


CONTROL_BURST = 8


class StoreWorker(Executor):
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hub-sqlite')
        self._lock = Lock()
        self._normal = deque()
        self._control = deque()
        self._draining = False
        self._closing = False
        self._control_burst = 0

    def submit(self, fn, /, *args, **kwargs):
        return self._submit(self._normal, fn, args, kwargs)

    def submit_control(self, fn, /, *args, **kwargs):
        return self._submit(self._control, fn, args, kwargs)

    def _submit(self, lane, fn, args, kwargs):
        with self._lock:
            if self._closing:
                raise RuntimeError('cannot schedule new futures after shutdown')
            future = Future()
            lane.append((future, fn, args, kwargs))
            if not self._draining:
                self._draining = True
                try:
                    # One finite drain job uses the standard executor's thread
                    # lifecycle and interpreter shutdown. No permanent waiter.
                    self._pool.submit(self._drain)
                except BaseException:
                    lane.pop()
                    self._draining = False
                    raise
            return future

    def _drain(self):
        while True:
            with self._lock:
                if self._control and (not self._normal or self._control_burst < CONTROL_BURST):
                    future, fn, args, kwargs = self._control.popleft()
                    self._control_burst = min(CONTROL_BURST, self._control_burst + 1)
                elif self._normal:
                    future, fn, args, kwargs = self._normal.popleft()
                    self._control_burst = 0
                else:
                    # A concurrent submit cannot miss the transition to idle.
                    self._draining = False
                    self._control_burst = 0
                    return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)
                del result
            # Do not retain the last phase's arguments/results while idle.
            del future, fn, args, kwargs

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            self._closing = True
            pending = []
            if cancel_futures:
                pending = [item[0] for item in (*self._normal, *self._control)]
                self._normal.clear()
                self._control.clear()
        # Future callbacks may submit work; never call them under our lock.
        for future in pending:
            future.cancel()
        # Never cancel the dispatcher: it owns all already accepted phases.
        self._pool.shutdown(wait=wait, cancel_futures=False)
