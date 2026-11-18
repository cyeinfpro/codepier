"""Bounded preparation pipeline with strictly ordered per-device sends."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import time

from anyio import CancelScope


def _ignore(stage, identifier, started):
    return None


class _Identifiers:
    """One bounded lookahead on a full chunk; never delay a ready send."""

    def __init__(self, identifiers, refill, chunk_size):
        self._refill = refill
        self._chunk_size = chunk_size
        self._accept(identifiers)
        self.read = None

    def _accept(self, batch):
        # None means yield to the scheduler (budget reached or lookup failed).
        # An empty snapshot may become stale while earlier work is sent.
        if batch is None:
            self._refill = None
        self._items = deque(batch or ())
        self._full_chunk = len(self._items) >= self._chunk_size

    async def next(self, *, idle):
        while True:
            if self._items:
                identifier = self._items.popleft()
                if not self._items and self._full_chunk and self._refill is not None:
                    self.read = asyncio.create_task(self._refill(), name="device-refill")
                return identifier
            if self._refill is None:
                return None
            if self.read is not None:
                if not idle and not self.read.done():
                    return None
                self._accept(await asyncio.shield(self.read))
                self.read = None
                continue
            if not idle:
                # A short chunk (or an empty speculative result) already saw
                # the queue tail. Do not query once per remaining send.
                return None
            more = await self._refill()
            self._accept(more)
            if not more:
                return None


@dataclass(slots=True)
class _Preparation:
    identifier: object
    task: asyncio.Task | None = None
    packet: object = None
    ready_at: float = 0.0
    has_slot: bool = False


async def _prepare_entry(entry, slots, prepare, observe, release):
    waited = time.monotonic()
    await slots.acquire()
    entry.has_slot = True
    try:
        observe("slot_wait", entry.identifier, waited)
        entry.packet = await prepare(entry.identifier)
        entry.ready_at = time.monotonic()
        if entry.packet is None:
            release(entry)
    except BaseException:
        release(entry)
        raise


async def _drain_preparations(pending, read, release):
    """Settle admitted Store phases before the owner exits."""
    tasks = []
    for entry in pending:
        if entry.task.done():
            release(entry)
        else:
            entry.task.cancel()
        tasks.append(entry.task)
    if read is not None:
        read.cancel()
        tasks.append(read)
    if tasks:
        drained = asyncio.gather(*tasks, return_exceptions=True)
        # Follow Store's shielding convention for both ASGI scope cancellation
        # and repeat calls to asyncio.Task.cancel().
        try:
            await asyncio.shield(drained)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                while not drained.done():
                    try:
                        await asyncio.shield(drained)
                    except asyncio.CancelledError:
                        continue
            raise
        finally:
            # A producer may finish successfully while handling its
            # cancellation. Discard that result as well.
            for entry in pending:
                release(entry)


async def ordered_delivery(
    identifiers,
    *,
    prepare,
    send,
    failed,
    slots,
    stopping,
    prefetch=2,
    refill=None,
    chunk_size=1,
    observe=_ignore,
    discard=None,
):
    """Overlap DB preparation with transport I/O; never reorder Agent admission.

    At most prefetch requests per device and the global slots limit retain a
    decrypted request. Cancelled producers settle their DB phase and release
    the same lease. Transport errors never replay a request here. Refill
    returns the next due identifiers for the device in order, or None to
    return to the scheduler after the selected work drains. A full chunk
    overlaps the lookup of the next chunk with its final sends. observe
    receives monotonic start marks for slot waits and send turns. discard,
    when given, reports when a prepared packet stops holding its slot.
    """
    source = _Identifiers(identifiers, refill, chunk_size)
    pending = deque()

    def release(entry):
        # A completed Task holds its return value. Keep the packet on the
        # entry so it can be dropped when returning the global slot.
        had_packet = entry.packet is not None
        entry.packet = None
        if entry.has_slot:
            entry.has_slot = False
            slots.release()
        if had_packet and discard is not None:
            discard(entry.identifier)

    async def fill():
        while len(pending) < prefetch and not stopping():
            identifier = await source.next(idle=not pending)
            if identifier is None or stopping():
                break
            entry = _Preparation(identifier)
            entry.task = asyncio.create_task(
                _prepare_entry(entry, slots, prepare, observe, release)
            )
            pending.append(entry)

    try:
        await fill()
        while pending and not stopping():
            entry = pending[0]
            try:
                # Cancel the producer from the common drain path. Its own
                # cancellation handling cannot make this owner send a packet.
                await asyncio.shield(entry.task)
                observe("send_turn", entry.identifier, entry.ready_at)
                if entry.packet is not None and not stopping():
                    await send(entry.identifier, entry.packet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await failed(entry.identifier, exc)
            finally:
                if entry.task.done():
                    pending.popleft()
                    release(entry)
            await fill()
    finally:
        if pending or source.read is not None:
            await _drain_preparations(pending, source.read, release)
