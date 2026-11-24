import asyncio
import json
from types import SimpleNamespace

import pytest

from hub.runtime import Connection, Runtime
from hub.store import Store


@pytest.mark.asyncio
async def test_detach_during_send_does_not_stop_other_device_deliveries(tmp_path, monkeypatch):
    store = Store(tmp_path / "hub")
    runtime = Runtime(store)
    sending, cold_started, cold_stopped = (asyncio.Event() for _ in range(3))
    never = asyncio.Event()

    class Socket:
        async def send_text(self, value):
            sending.set()
            await never.wait()

        async def close(self, code):
            pass

    connection = Connection(Socket(), SimpleNamespace(pack=json.dumps))
    batches = iter([[{"device_id": d, "id": d}] for d in ("hot", "cold")])

    async def select(*_):
        return next(batches, [])

    async def deliver(ids, *, device_id, selected):
        if device_id == "hot":
            await connection.send({"type": "call", "id": ids[0]})
        else:
            cold_started.set()
            try:
                await never.wait()
            finally:
                cold_stopped.set()

    monkeypatch.setattr(store, "run_hint_read", select)
    monkeypatch.setattr(runtime, "deliver_device", deliver)
    worker = asyncio.create_task(runtime.delivery_loop())
    progress = asyncio.create_task(cold_started.wait())
    try:
        await asyncio.wait_for(sending.wait(), 3)
        connection.close()  # The writer propagates cancellation to the in-flight send.
        await asyncio.wait({progress, worker}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert not worker.done()
        assert progress.done()
    finally:
        connection.close()
        progress.cancel()
        worker.cancel()
        await asyncio.gather(worker, progress, return_exceptions=True)
        store.close()
    assert worker.cancelled()
    assert cold_stopped.is_set()
