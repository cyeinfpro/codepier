"""Durable candidate reads make progress while the Hub writer is occupied."""

import asyncio
import sqlite3
from threading import Event

import pytest

from hub.store import Store


@pytest.mark.asyncio
async def test_delivery_hint_reads_committed_state_without_waiting_for_writer(tmp_path):
    store = Store(tmp_path / "hub")
    writing = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    sql = "SELECT value FROM meta WHERE key='fixture-change'"
    store.execute("INSERT INTO meta(key,value) VALUES ('fixture-change','old')")

    def update():
        with store.transaction():
            store.db.execute("UPDATE meta SET value='new' WHERE key='fixture-change'")
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(5)

    writer = asyncio.create_task(store.run(update))
    try:
        await asyncio.wait_for(writing.wait(), 2)
        row = await asyncio.wait_for(store.run_hint_read(store.one, sql), 2)
        assert row["value"] == "old"
        release.set()
        await writer
        assert (await store.run_hint_read(store.one, sql))["value"] == "new"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await store.run_hint_read(store.execute, "DELETE FROM meta")
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        await store.aclose()


@pytest.mark.asyncio
async def test_change_during_delivery_hint_does_not_retry_or_break_snapshot(tmp_path):
    store = Store(tmp_path / "hub")
    other = Store(store.directory)
    try:
        def snapshot():
            sql = "SELECT count(*) AS n FROM meta WHERE key='fixture-change'"
            before = store.one(sql)["n"]
            other.execute("INSERT INTO meta(key,value) VALUES ('fixture-change','new')")
            return before, store.one(sql)["n"]

        assert await store.run_hint_read(snapshot) == (0, 0)
        assert store.one("SELECT value FROM meta WHERE key='fixture-change'")["value"] == "new"
        metrics = store.read_worker_metrics.snapshot()
        assert metrics["completed"] == 1
        assert metrics["read_retries"] == {"session_revision": 0, "data_version": 0}
    finally:
        await other.aclose()
        await store.aclose()
