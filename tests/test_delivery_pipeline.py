"""No-network invariants for bounded ordered delivery preparation."""
import asyncio
from types import SimpleNamespace

import pytest

from hub.delivery_metrics import DeliveryMetrics
from hub.delivery_pipeline import ordered_delivery
from hub.device_delivery import DeviceDelivery
from shared.config import RuntimeConfig


def test_prepares_next_request_during_send_without_reordering():
    async def run():
        slots = asyncio.Semaphore(2)
        second_ready = asyncio.Event()
        sent, prepared = [], []
        async def prepare(identifier):
            prepared.append(identifier)
            if identifier == 1:
                second_ready.set()
            return identifier
        async def send(identifier, value):
            if identifier == 0:
                await asyncio.wait_for(second_ready.wait(), 3)
            assert value == identifier
            sent.append(identifier)
        async def failed(identifier, error):
            raise AssertionError((identifier, error))
        await ordered_delivery(range(8), prepare=prepare, send=send, failed=failed,
                               slots=slots, stopping=lambda: False)
        assert sent == prepared == list(range(8))
        assert slots._value == 2
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['prepare', 'send'])
def test_failure_is_reported_once_without_automatic_replay(stage):
    async def run():
        slots = asyncio.Semaphore(2)
        preparations, sends, errors = [], [], []
        async def prepare(identifier):
            preparations.append(identifier)
            if stage == 'prepare' and identifier == 1:
                raise OSError('fixture')
            return identifier
        async def send(identifier, value):
            sends.append(identifier)
            if stage == 'send' and identifier == 1:
                raise OSError('fixture')
        async def failed(identifier, error):
            errors.append((identifier, type(error).__name__))
        await ordered_delivery(range(4), prepare=prepare, send=send, failed=failed,
                               slots=slots, stopping=lambda: False)
        assert preparations == list(range(4))
        assert errors == [(1, 'OSError')]
        assert sends == ([0, 2, 3] if stage == 'prepare' else list(range(4)))
        assert slots._value == 2
    asyncio.run(run())


@pytest.mark.parametrize('cancel_stage', ['prepare', 'send'])
def test_cancellation_drains_all_prepared_leases(cancel_stage):
    async def run():
        slots = asyncio.Semaphore(2)
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def prepare(identifier):
            if cancel_stage == 'prepare' and identifier == 0:
                entered.set()
                await blocker.wait()
            return identifier
        async def send(identifier, value):
            entered.set()
            await blocker.wait()
        async def failed(identifier, error):
            raise AssertionError('Cancellation must propagate')
        task = asyncio.create_task(ordered_delivery(range(20), prepare=prepare, send=send,
            failed=failed, slots=slots, stopping=lambda: False))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slots._value == 2
    asyncio.run(run())


def test_multiple_devices_share_global_decrypted_request_limit():
    async def run():
        slots = asyncio.Semaphore(3)
        full = asyncio.Event()
        release = asyncio.Event()
        active = peak = 0
        async def prepare(identifier):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                full.set()
            return identifier
        async def send(identifier, value):
            nonlocal active
            await release.wait()
            active -= 1
        async def failed(identifier, error):
            raise AssertionError((identifier, error))
        tasks = [asyncio.create_task(ordered_delivery(range(5), prepare=prepare, send=send,
            failed=failed, slots=slots, stopping=lambda: False)) for _ in range(4)]
        await asyncio.wait_for(full.wait(), 3)
        assert peak == 3
        release.set()
        await asyncio.gather(*tasks)
        assert peak <= 3 and active == 0 and slots._value == 3
    asyncio.run(run())


def test_refill_continues_in_order_and_empty_refill_ends_source():
    async def run():
        slots = asyncio.Semaphore(2)
        sent, refills, observed = [], [], []
        batches = [[3, 4], [5]]
        async def refill():
            refills.append(len(sent))
            return batches.pop(0) if batches else []
        async def prepare(identifier):
            return identifier
        async def send(identifier, value):
            sent.append(identifier)
        async def failed(identifier, error):
            raise AssertionError((identifier, error))
        def observe(stage, identifier, started):
            observed.append((stage, identifier))
        await ordered_delivery([0, 1, 2], prepare=prepare, send=send, failed=failed,
                               slots=slots, stopping=lambda: False, refill=refill,
                               chunk_size=2, observe=observe)
        assert sent == [0, 1, 2, 3, 4, 5]
        assert len(refills) == 3 and refills[-1] == 6 and batches == []
        assert [identifier for stage, identifier in observed if stage == 'slot_wait'] == sent
        assert [identifier for stage, identifier in observed if stage == 'send_turn'] == sent
        assert slots._value == 2
    asyncio.run(run())


def test_refill_error_drains_prepared_leases():
    async def run():
        slots = asyncio.Semaphore(2)
        async def refill():
            raise OSError('fixture')
        async def prepare(identifier):
            return identifier
        async def send(identifier, value):
            return None
        async def failed(identifier, error):
            raise AssertionError((identifier, error))
        with pytest.raises(OSError):
            await ordered_delivery([0], prepare=prepare, send=send, failed=failed,
                                   slots=slots, stopping=lambda: False, refill=refill)
        assert slots._value == 2
    asyncio.run(run())


@pytest.mark.asyncio
async def test_slow_full_chunk_refill_does_not_block_selected_sends():
    lookup_started = asyncio.Event()
    lookup_release = asyncio.Event()
    slots = asyncio.Semaphore(2)
    sent = []
    queries = 0

    async def refill():
        nonlocal queries
        queries += 1
        if queries == 1:
            lookup_started.set()
            await lookup_release.wait()
            return [2]
        return []

    async def prepare(identifier):
        return identifier

    async def send(identifier, value):
        if identifier == 0:
            await lookup_started.wait()
        elif identifier == 1:
            lookup_release.set()
        sent.append(value)

    async def failed(identifier, error):
        raise AssertionError((identifier, error))

    await asyncio.wait_for(ordered_delivery(
        [0, 1], prepare=prepare, send=send, failed=failed, slots=slots,
        stopping=lambda: False, refill=refill, chunk_size=2,
    ), 3)
    assert sent == [0, 1, 2] and queries == 2 and slots._value == 2


@pytest.mark.asyncio
async def test_short_chunk_queries_at_idle_and_returns_unavailable_refill():
    sent = []
    queried_after = []
    slots = asyncio.Semaphore(8)

    async def refill():
        queried_after.append(tuple(sent))
        return None

    async def prepare(identifier):
        return identifier

    async def send(identifier, value):
        sent.append(value)

    async def failed(identifier, error):
        raise AssertionError((identifier, error))

    await ordered_delivery(
        [0, 1, 2], prepare=prepare, send=send, failed=failed, slots=slots,
        stopping=lambda: False, prefetch=8, refill=refill, chunk_size=16,
    )
    assert queried_after == [(0, 1, 2)] and slots._value == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", [0, 1])
async def test_repeat_cancel_joins_producer_before_returning_its_slot(producer):
    prepare_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    slots = asyncio.Semaphore(2)

    async def prepare(identifier):
        if identifier == producer:
            prepare_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
        return identifier

    async def send(identifier, value):
        assert identifier == 0 and producer == 1
        await asyncio.Future()

    async def failed(identifier, error):
        raise AssertionError((identifier, error))

    task = asyncio.create_task(ordered_delivery(
        [0, 1], prepare=prepare, send=send, failed=failed, slots=slots,
        stopping=lambda: False,
    ))
    await asyncio.wait_for(prepare_started.wait(), 3)
    task.cancel()
    try:
        await asyncio.wait_for(cleanup_started.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and slots._value == 1
    finally:
        cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert slots._value == 2


@pytest.mark.asyncio
async def test_stopped_device_releases_prepared_payload_measurements():
    prepared_second = asyncio.Event()
    sending = asyncio.Event()
    metrics = DeliveryMetrics()
    slots = asyncio.Semaphore(2)

    async def prepare(identifier, *, device_id, timing):
        timing.payload_bytes = 10 * (identifier + 1)
        if identifier == 1:
            prepared_second.set()
        return device_id, None, [{"type": "call"}]

    async def send(identifier, payload, *, timing):
        await prepared_second.wait()
        sending.set()
        await asyncio.Future()

    async def failed(identifier, error):
        raise AssertionError((identifier, error))

    runtime = SimpleNamespace(
        delivery_config=RuntimeConfig(10, 5, 1),
        delivery_metrics=metrics,
        delivery_slots=slots,
        delivery_prefetch=2,
        stopping=False,
        _prepare_for_send=prepare,
        _send_prepared=send,
        _delivery_failed=failed,
    )
    task = asyncio.create_task(DeviceDelivery(runtime, [0, 1], "fixture").run())
    try:
        await asyncio.wait_for(sending.wait(), 3)
        assert metrics.snapshot()["prepared_payloads"]["bytes"] == 30
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert metrics.snapshot()["prepared_payloads"]["bytes"] == 0
    assert slots._value == 2


def test_stopping_does_not_send_already_prepared_next_request():
    async def run():
        slots = asyncio.Semaphore(2)
        stopped = False
        sent = []
        async def prepare(identifier):
            return identifier
        async def send(identifier, value):
            nonlocal stopped
            sent.append(identifier)
            stopped = True
        async def failed(identifier, error):
            raise AssertionError((identifier, error))
        await ordered_delivery(range(5), prepare=prepare, send=send, failed=failed,
                               slots=slots, stopping=lambda: stopped)
        assert sent == [0]
        assert slots._value == 2
    asyncio.run(run())
