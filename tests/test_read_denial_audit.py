"""Read snapshots never write, but their final denial audits remain durable."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError


@pytest.fixture
def denial(tmp_path, monkeypatch):
    store = Store(tmp_path / "hub")
    def reject(*args, **kwargs):
        raise DevError("FORBIDDEN", "synthetic denied fixture", 403)
    monkeypatch.setattr("hub.runtime.require_role", reject)
    def call():
        return Runtime.authorize(SimpleNamespace(store=store),
                                 SimpleNamespace(actor="synthetic"), "write", project_id="fixture")
    yield store, call
    store.close()


@pytest.mark.asyncio
async def test_direct_read_denial_is_audited_before_original_error(denial):
    store, call = denial
    with pytest.raises(DevError, match="synthetic denied fixture"):
        await store.run_read(call)
    rows = store.all("SELECT action,target,status FROM audit")
    assert rows == [{"action": "role.denied", "target": "fixture", "status": "denied"}]


@pytest.mark.asyncio
async def test_caught_denials_only_audit_final_retry_after_writer_fence(denial):
    store, call = denial
    attempts = []
    def filtered_read():
        attempts.append(1)
        try:
            call()
        except DevError:
            pass
        if len(attempts) < 4:
            # Synthetic concurrent security revision, without writing on reader.
            store.session_revision += 1
        return {"visible": []}
    assert await asyncio.wait_for(store.run_read(filtered_read), 3) == {"visible": []}
    assert len(attempts) == 4
    assert store.one("SELECT COUNT(*) AS n FROM audit")["n"] == 1


@pytest.mark.asyncio
async def test_normal_reader_never_submits_writer_job(denial, monkeypatch):
    store, _ = denial
    def unexpected(*args, **kwargs):
        raise AssertionError("pure read submitted a writer job")
    monkeypatch.setattr(store._executor, "submit", unexpected)
    assert await store.run_read(store.one, "SELECT 1 AS n") == {"n": 1}
    assert store.one("SELECT COUNT(*) AS n FROM audit")["n"] == 0


@pytest.mark.asyncio
async def test_cancelled_denial_drains_writer_audit_before_return(denial, monkeypatch):
    store, call = denial
    entered, release = threading.Event(), threading.Event()
    original = store.audit
    def audit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "audit", audit)
    task = asyncio.create_task(store.run_read(call))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.one("SELECT COUNT(*) AS n FROM audit")["n"] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
