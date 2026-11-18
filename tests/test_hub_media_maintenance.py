"""Expired media cleanup yields between durable, independently committed slices."""

import asyncio
import json
import threading

import pytest

from hub.operation_history import seal
from hub.runtime import MAINTENANCE_MEDIA_BATCH, MAINTENANCE_MEDIA_LIMIT, Runtime
from hub.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_cleanup_yields_to_control_and_resumes_from_persisted_expiry(tmp_path, interrupt):
    store = Store(tmp_path / "hub")
    runtime = Runtime(store)
    expired = json.dumps({"ok": True, "data": {"text": "pixels", "computer_expires_at": 1}})
    live = json.dumps({"ok": True, "data": {"text": "current", "computer_expires_at": 10**12}})
    total = MAINTENANCE_MEDIA_LIMIT + 1
    sealed = seal(store, expired)
    with store.transaction() as db:
        db.executemany(
            """INSERT INTO operations
               (id,actor,tool,args_summary,fingerprint,idem,state,created,updated,result,media_expires_at)
               VALUES (?, 'fixture', 'read', '{}', '', ?, 'succeeded',1,1,?,?)""",
            [(str(i), str(i), sealed, 1) for i in range(total)]
            + [("legacy", "legacy", expired, None), ("live", "live", seal(store, live), 10**12)],
        )

    def due(store):
        return store.one("SELECT count(*) AS n FROM operations WHERE media_expires_at=1")["n"]

    loop = asyncio.get_running_loop()
    first_slice = asyncio.Event()
    release = threading.Event()
    original = runtime._purge_computer

    def observe_slice(now, *, legacy):
        count = original(now, legacy=legacy)
        if not legacy and not release.is_set():
            loop.call_soon_threadsafe(first_slice.set)
            assert release.wait(5)
        return count

    runtime._purge_computer = observe_slice
    cleanup = asyncio.create_task(runtime._run_maintenance())
    try:
        await asyncio.wait_for(first_slice.wait(), 5)
        receipt = asyncio.create_task(store.run_control(due, store))
        await asyncio.sleep(0)  # Submit the control phase before the worker is released.
        if interrupt:
            cleanup.cancel()
        release.set()
        assert await asyncio.wait_for(receipt, 5) == total - MAINTENANCE_MEDIA_BATCH
        if interrupt:
            with pytest.raises(asyncio.CancelledError):
                await cleanup
            assert due(store) == total - MAINTENANCE_MEDIA_BATCH
        else:
            await cleanup
            assert due(store) == 1  # Per-cycle budget is preserved.
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
        store.close()

    reopened = Store(tmp_path / "hub")
    try:
        runtime = Runtime(reopened)
        await runtime._run_maintenance()
        assert due(reopened) == 0
        rows = reopened.all("SELECT * FROM operations")
        assert len(rows) == total + 2
        for row in rows:
            assert row["state"] == "succeeded"
            assert row["idem"] == row["id"]
            data = runtime.operation_result(row)["data"]
            if row["id"] == "live":
                assert data["text"] == "current"
            else:
                assert data["media_expired"] and "text" not in data
    finally:
        reopened.close()
