"""No-network invariants for bounded ordered delivery preparation."""
import asyncio

import pytest

from hub.delivery_pipeline import ordered_delivery


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
