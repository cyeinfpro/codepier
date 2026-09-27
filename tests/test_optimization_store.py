"""Behavioral regressions for the September 2026 optimization review."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import sqlite3
import threading
import time

import pytest

from hub.store import Store
from hub.store_contract import TransactionAborted
from shared.config import ConfigurationError, RuntimeConfig, env_csv, env_float, env_int


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "hub")
    yield instance
    instance.close()


def test_raw_connection_and_cursor_require_current_thread_lock(store):
    with pytest.raises(RuntimeError, match="current thread"):
        store.db.execute("SELECT 1")
    with store.lock:
        cursor = store.db.execute("SELECT 1 AS value")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.db.execute, "SELECT 1")
            with pytest.raises(RuntimeError, match="current thread"):
                future.result(timeout=2)
    with pytest.raises(RuntimeError, match="current thread"):
        cursor.fetchone()
    with store.lock:
        assert cursor.fetchone()["value"] == 1
    assert not hasattr(cursor, "connection")


def test_execute_metadata_survives_transaction_but_fetch_does_not(store):
    cursor = store.execute("INSERT INTO meta VALUES ('metadata','value')")
    assert cursor.rowcount == 1
    assert isinstance(cursor.lastrowid, int)
    with pytest.raises(RuntimeError):
        store.execute("SELECT * FROM meta").fetchall()


def test_nested_execute_and_audit_do_not_commit_outer_transaction(store):
    callbacks = []
    with pytest.raises(ValueError, match="after writes"):
        with store.lock, store.db:
            store.db.execute("BEGIN IMMEDIATE")
            store.execute("INSERT INTO meta VALUES ('rollback','value')")
            store.audit("test", "rollback.audit")
            store.after_commit(lambda: callbacks.append("published"))
            # A second SQLite connection must not observe an early commit.
            with sqlite3.connect(store.directory / "hub.sqlite3") as observer:
                assert observer.execute("SELECT value FROM meta WHERE key='rollback'").fetchone() is None
            raise ValueError("after writes")
    assert store.one("SELECT * FROM meta WHERE key='rollback'") is None
    assert store.one("SELECT * FROM audit WHERE action='rollback.audit'") is None
    assert callbacks == []


def test_caught_inner_failure_makes_outer_transaction_rollback_only(store):
    with pytest.raises(TransactionAborted):
        with store.lock, store.db:
            store.execute("INSERT INTO meta VALUES ('outer','one')")
            try:
                with store.db:
                    store.execute("INSERT INTO meta VALUES ('inner','two')")
                    raise ValueError("caught by the application")
            except ValueError:
                pass
            store.execute("INSERT INTO meta VALUES ('after','three')")
    assert not store.all("SELECT * FROM meta WHERE key IN ('outer','inner','after')")


def test_commit_callback_runs_once_after_outer_commit(store):
    observations = []
    with store.lock, store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.execute("INSERT INTO meta VALUES ('committed','yes')")
        with store.db:
            store.after_commit(lambda: observations.append(store.one("SELECT value FROM meta WHERE key='committed'")["value"]))
        assert observations == []
    assert observations == ["yes"]


def test_transaction_helpers_fail_without_context(store):
    from hub.access import bump_grant_revision
    with pytest.raises(RuntimeError):
        bump_grant_revision(store, "does-not-exist")
    with store.lock:
        with pytest.raises(RuntimeError, match="enclosing"):
            bump_grant_revision(store, "does-not-exist")
        with store.db:
            with pytest.raises(RuntimeError, match="implicitly commit"):
                store.db.executescript("SELECT 1;")


@pytest.mark.asyncio
async def test_database_job_does_not_block_event_loop_and_copies_context(store):
    context = ContextVar("review_request")
    context.set("request-context")
    ticks = []
    def slow_job():
        time.sleep(.08)
        return context.get(), threading.get_ident()
    task = asyncio.create_task(store.run(slow_job))
    while not task.done():
        ticks.append(time.monotonic())
        await asyncio.sleep(.005)
    value, thread = await task
    assert value == "request-context"
    assert thread != threading.get_ident()
    assert len(ticks) >= 3


@pytest.mark.asyncio
async def test_cancelled_database_phase_drains_before_releasing_admission(store):
    entered, release = threading.Event(), threading.Event()
    admission = asyncio.Lock()
    def write_job():
        entered.set()
        assert release.wait(5)
        store.execute("INSERT INTO meta VALUES ('cancel-durable','yes')")
    async def admitted():
        async with admission:
            await store.run(write_job)
    first = asyncio.create_task(admitted())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(.01)
        assert admission.locked()
        assert not first.done()
        first.cancel()  # Repeated cancellation cannot detach the DB worker.
        await asyncio.sleep(0)
        assert admission.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not admission.locked()
    assert store.one("SELECT value FROM meta WHERE key='cancel-durable'")["value"] == "yes"


@pytest.mark.asyncio
async def test_await_and_close_reject_holding_thread_lock(store):
    with store.lock:
        with pytest.raises(RuntimeError, match="holding Store.lock"):
            await store.run(lambda: None)
        with pytest.raises(RuntimeError, match="Release Store.lock"):
            store.close()


@pytest.mark.asyncio
async def test_worker_publish_is_bounded_and_delivered_on_queue_owner(store):
    from hub.runtime import Runtime
    runtime = Runtime(store)
    queue = asyncio.Queue(maxsize=2)
    runtime.watchers.add(queue)
    waiting = asyncio.create_task(queue.get())
    await asyncio.sleep(0)
    await store.run(runtime.publish, "review", {"value": 1})
    assert (await asyncio.wait_for(waiting, 1))["data"] == {"value": 1}
    for index in range(10):
        await store.run(runtime.publish, "review", {"value": index})
    await asyncio.sleep(0)
    assert queue.qsize() == 2
    assert (await queue.get())["data"]["value"] == 8
    assert (await queue.get())["data"]["value"] == 9
    await runtime.stop()


@pytest.mark.parametrize("value", ["", "nan", "inf", "-inf", "secret-value", "-1", "101"])
def test_float_configuration_is_finite_bounded_and_redacted(monkeypatch, value):
    monkeypatch.setenv("REVIEW_NUMBER", value)
    with pytest.raises(ConfigurationError) as caught:
        env_float("REVIEW_NUMBER", 5, 0, 100)
    assert "REVIEW_NUMBER" in str(caught.value)
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("value", ["", "text", "1.2", "0", "65536"])
def test_port_configuration_is_validated(monkeypatch, value):
    monkeypatch.setenv("REVIEW_PORT", value)
    with pytest.raises(ConfigurationError, match="REVIEW_PORT"):
        env_int("REVIEW_PORT", 8765, 1, 65535)


def test_origin_csv_trims_deduplicates_and_omits_empty_entries(monkeypatch):
    monkeypatch.setenv("REVIEW_ORIGINS", " https://one.test, ,https://two.test,https://one.test ")
    assert env_csv("REVIEW_ORIGINS") == ("https://one.test", "https://two.test")


def test_runtime_configuration_uses_contract_wait_budget(monkeypatch):
    from shared.contracts import OPERATION_WAIT_SECONDS
    monkeypatch.setenv("HUB_CALL_WAIT_SECONDS", str(OPERATION_WAIT_SECONDS))
    assert RuntimeConfig.from_env().wait_seconds == OPERATION_WAIT_SECONDS
    monkeypatch.setenv("HUB_CALL_WAIT_SECONDS", str(OPERATION_WAIT_SECONDS + 1))
    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_env()


@pytest.mark.parametrize("setting,value", [("HUB_PORT", "bad-port"), ("TZ", "Unknown/Timezone"), ("HUB_QUEUE_TTL_SECONDS", "NaN")])
def test_invalid_startup_config_does_not_create_state(tmp_path, monkeypatch, setting, value):
    from hub.app import create_app
    monkeypatch.setenv(setting, value)
    directory = tmp_path / "invalid-hub"
    with pytest.raises(ConfigurationError, match=setting):
        create_app(str(directory))
    assert not directory.exists()


def test_mutating_catalog_is_computed_after_all_registrations():
    from shared.contracts import MUTATING, READ_WITH_SCOPE, TOOLS
    from shared.computer_contracts import COMPUTER_READ_TOOLS
    expected = {name for name, tool in TOOLS.items() if tool.scope != "read" and name not in COMPUTER_READ_TOOLS and name not in READ_WITH_SCOPE}
    assert MUTATING == expected


def test_process_catalog_contains_only_registered_process_tools():
    from shared.contracts import PROCESS_TOOLS, TOOLS
    assert PROCESS_TOOLS == {'tasks_run', 'shell_exec', 'ssh_exec', 'exec',
                             'validation_run', 'lsp_query', 'worktrees_create', 'worktrees_remove'}
    assert PROCESS_TOOLS <= TOOLS.keys()
