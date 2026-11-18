"""One device's delivery task: ordered sends, bounded refills, stage timings."""

from __future__ import annotations

import asyncio
import time

from hub.delivery_metrics import DeliveryTiming, classify
from hub.delivery_pipeline import ordered_delivery

# A device task refills in configured chunks until it yields to the scheduler.
DELIVERY_TASK_LIMIT = 128


class DeviceDelivery:
    """Owns no authority: every row is re-prepared and re-fenced by Runtime."""

    def __init__(self, runtime, ids, device_id=None, selected=()):
        self.runtime = runtime
        self.ids = list(ids)
        self.device_id = device_id
        self.metrics = runtime.delivery_metrics
        self.seen = set(self.ids)
        self.timings = {row["id"]: self._new_timing(row) for row in selected}
        self.retained = {}

    @staticmethod
    def _new_timing(row):
        return DeliveryTiming(row["id"], due=row.get("due"), attempts=row.get("attempts"))

    def timing(self, identifier):
        current = self.timings.get(identifier)
        if current is None:
            current = self.timings[identifier] = DeliveryTiming(identifier)
        return current

    def observe(self, stage, identifier, mark):
        # For direct calls without selection metadata, the slot wait opens the
        # timing. A later mark must not re-create a timing for a settled row.
        current = self.timing(identifier) if stage == "slot_wait" else self.timings.get(identifier)
        if current is not None:
            if stage == "slot_wait":
                current.add("batch_wait", current.selected, mark)
            current.add(stage, mark)

    def _settle(self, identifier, kind=None):
        current = self.timings.pop(identifier, None) or DeliveryTiming(identifier)
        current.kind = kind or current.kind
        self.metrics.finish(current)

    def discard(self, identifier):
        self.metrics.release_payload(self.retained.pop(identifier))

    async def prepare(self, identifier):
        current = self.timing(identifier)
        prepared = await self.runtime._prepare_for_send(
            identifier, device_id=self.device_id, timing=current
        )
        current.kind = current.kind or classify(prepared, attempts=current.attempts)
        if prepared is None:
            self._settle(identifier)
        else:
            self.retained[identifier] = current.payload_bytes
            self.metrics.retain_payload(current.payload_bytes)
        return prepared

    async def send(self, identifier, prepared):
        current = self.timing(identifier)
        sent = await self.runtime._send_prepared(identifier, prepared, timing=current)
        self._settle(identifier, None if sent else "fenced")

    async def failed(self, identifier, exc):
        self._settle(identifier, "failed")
        await self.runtime._delivery_failed(identifier, exc)

    async def refill(self):
        runtime = self.runtime
        if runtime.stopping or len(self.seen) >= DELIVERY_TASK_LIMIT:
            return None
        try:
            found = await runtime.store.run_hint_read(
                runtime._device_delivery_refill, self.device_id, tuple(self.seen)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable rows remain due; a later scheduler round retries.
            self.metrics.refill_error()
            return None
        if found is None:
            # A previously handled row now has a due cancellation. The
            # scheduler can select it again after this task gives up ownership.
            return None
        fresh = [row for row in found if row["id"] not in self.seen]
        fresh = fresh[: DELIVERY_TASK_LIMIT - len(self.seen)]
        for row in fresh:
            self.timings[row["id"]] = self._new_timing(row)
            self.seen.add(row["id"])
        return [row["id"] for row in fresh]

    async def run(self):
        started = self.metrics.start_batch()
        runtime = self.runtime
        try:
            config = runtime.delivery_config
            await ordered_delivery(
                self.ids,
                prepare=self.prepare,
                send=self.send,
                failed=self.failed,
                slots=runtime.delivery_slots,
                stopping=lambda: runtime.stopping,
                prefetch=runtime.delivery_prefetch,
                refill=self.refill if config.rolling_for(self.device_id) else None,
                chunk_size=config.delivery_device_chunk,
                observe=self.observe,
                discard=self.discard,
            )
        finally:
            self.metrics.batch(len(self.seen), (time.monotonic() - started) * 1000, started=started)
