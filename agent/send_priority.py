"""Two-class WebSocket send gate: control receipts never queue behind bulk.

The Agent shares one authenticated socket for operation receipts
(accepted/started/status/result/trace/heartbeat/native_reply) and for bulk
background traffic (native transcript backfill, artifact chunks). A plain FIFO
lock lets a burst of 256 KiB transcript frames delay an ``accepted`` receipt by
seconds; the Hub then redelivers the call and records late acceptance.

This gate keeps FIFO order *within* each class, hands the lock directly to the
next waiter (no barging), and always serves waiting control frames first. A
frame already being written is never interrupted, so the worst-case control
delay is one bulk frame. Bulk senders are additionally windowed by their own
protocol (see ``agent.native_sync_flow``), so bulk cannot starve either.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager

BULK_TYPES = frozenset({"native_sync", "artifact_chunk"})


def is_bulk(body) -> bool:
    return isinstance(body, dict) and body.get("type") in BULK_TYPES


class PrioritySendLock:
    def __init__(self):
        self._locked = False
        self._control: deque[asyncio.Future] = deque()
        self._bulk: deque[asyncio.Future] = deque()

    def locked(self) -> bool:
        return self._locked

    def control_waiting(self) -> bool:
        return any(not waiter.done() for waiter in self._control)

    @asynccontextmanager
    async def hold(self, bulk: bool = False):
        await self.acquire(bulk)
        try:
            yield
        finally:
            self.release()

    # Compatibility with ``async with lock:`` users (control priority).
    async def __aenter__(self):
        await self.acquire(False)
        return self

    async def __aexit__(self, *exc):
        self.release()
        return False

    async def acquire(self, bulk: bool = False) -> bool:
        if (
            not self._locked
            and not self.control_waiting()
            and (not bulk or not self._pending(self._bulk))
        ):
            self._locked = True
            return True
        queue = self._bulk if bulk else self._control
        waiter = asyncio.get_running_loop().create_future()
        queue.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            # Ownership may already have been handed to us; pass it on.
            if waiter.done() and not waiter.cancelled():
                self.release()
            raise
        finally:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
        return True

    @staticmethod
    def _pending(queue) -> bool:
        return any(not waiter.done() for waiter in queue)

    def release(self) -> None:
        if not self._locked:
            raise RuntimeError("PrioritySendLock released while not held")
        for queue in (self._control, self._bulk):
            while queue:
                waiter = queue.popleft()
                if not waiter.done():
                    # Direct hand-off: the lock stays held for the next owner.
                    waiter.set_result(True)
                    return
        self._locked = False
