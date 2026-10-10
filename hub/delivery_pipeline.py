"""Bounded preparation pipeline with strictly ordered per-device sends."""
from __future__ import annotations

import asyncio
from collections import deque


async def ordered_delivery(identifiers, *, prepare, send, failed, slots, stopping, prefetch=2):
    """Overlap DB preparation with transport I/O; never reorder Agent admission.

    At most prefetch requests per device and the global slots limit retain a
    decrypted request. Cancelled producers settle their DB phase and release
    the same lease. Transport errors never replay a request here.
    """
    remaining = iter(identifiers)
    pending = deque()

    async def prepared(identifier):
        await slots.acquire()
        try:
            return identifier, await prepare(identifier)
        except BaseException:
            slots.release()
            raise

    def fill():
        while len(pending) < prefetch and not stopping():
            identifier = next(remaining, None)
            if identifier is None:
                break
            pending.append((identifier, asyncio.create_task(prepared(identifier))))

    try:
        fill()
        while pending and not stopping():
            identifier, task = pending.popleft()
            lease = None
            try:
                lease = await task
                if lease[1] is not None and not stopping():
                    await send(identifier, lease[1])
            except asyncio.CancelledError:
                # A task may have finished just as cancellation was delivered.
                if lease is None and task.done() and not task.cancelled() and task.exception() is None:
                    lease = task.result()
                raise
            except Exception as exc:
                await failed(identifier, exc)
            finally:
                if lease is not None:
                    slots.release()
            fill()
    finally:
        # No detached producer may retain a slot/request after this owner exits.
        tasks = [task for _, task in pending]
        for task in tasks:
            if not task.done():
                task.cancel()
        for value in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(value, tuple):
                slots.release()
