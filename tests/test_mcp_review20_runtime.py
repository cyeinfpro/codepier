"""Focused regressions for defensive Hub review fixes."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from hub.runtime import Connection
from hub.store import _classify_write
from shared.contracts import TOOLS
from tests.test_core_tools import agent, request  # noqa: F401 (fixture)
from tests.test_delivery_concurrency import delivery  # noqa: F401 (fixture)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("INSERT INTO unknown_table (id) VALUES (?)", (True, False)),
        ("DELETE FROM unknown_table WHERE id=?", (True, False)),
        ("UPDATE unknown_table SET value=?", (True, False)),
        ("REPLACE INTO unknown_table (id) VALUES (?)", (True, False)),
        ("UPDATE room_permission_policies SET updated=?", (True, False)),
        ("DELETE FROM conversation_project_revocations WHERE id=?", (True, False)),
        ("UPDATE oauth_clients SET updated=?", (True, False)),
        ("UPDATE devices SET future_auth_column=?", (True, False)),
        ("UPDATE devices SET name=?, info=?, last_seen=?", (False, False)),
        ("UPDATE operations SET updated=?", (False, False)),
        ("INSERT INTO device_presence (device_id) VALUES (?)", (False, False)),
        ("UPDATE meta SET value=?", (False, True)),
    ],
)
def test_write_classification_is_conservative_and_keeps_reviewed_hot_paths(sql, expected):
    assert _classify_write(sql) == expected


@pytest.mark.asyncio
async def test_submit_authorization_lookup_runs_off_event_loop(delivery, monkeypatch):
    e = delivery
    loop_thread = threading.get_ident()
    observed = []

    def authorize(device_id):
        observed.append((device_id, threading.get_ident()))
        return False

    monkeypatch.setattr(e.runtime, "online", authorize)
    args = TOOLS["read"].model.model_validate({"project": "hot", "path": "README.md"}).model_dump()
    project = e.runtime.project("hot", e.principal)
    observed.clear()
    await e.runtime.dispatch("read", args, project, e.principal)
    assert observed and all(device_id == "hot" and worker != loop_thread for device_id, worker in observed)


@pytest.mark.asyncio
async def test_control_reply_waits_for_capacity_and_retains_channel(monkeypatch):
    monkeypatch.setattr("hub.runtime._SEND_QUEUE_LIMIT", 1)
    started, release = asyncio.Event(), asyncio.Event()
    ack_started, release_ack = asyncio.Event(), asyncio.Event()
    ack_two_sent = asyncio.Event()
    sent = []

    async def send_text(wire):
        payload = json.loads(wire)
        sent.append(payload["id"])
        if payload["id"] == "data-first":
            started.set()
            await release.wait()
        elif payload["id"] == "ack-1":
            ack_started.set()
            await release_ack.wait()
        elif payload["id"] == "ack-2":
            ack_two_sent.set()

    connection = Connection(SimpleNamespace(send_text=send_text), SimpleNamespace(pack=json.dumps))
    data = asyncio.create_task(connection.send({"type": "call", "id": "data-first"}))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert connection.send_nowait({"type": "ack", "id": "ack-1"})
        waiting = asyncio.create_task(connection.enqueue_reply({"type": "ack", "id": "ack-2"}))
        await asyncio.sleep(0)
        assert not waiting.done()
        assert not connection.unusable
        release.set()
        await asyncio.wait_for(ack_started.wait(), 2)
        # Dequeueing ack-1 makes space, but its socket write remains stalled:
        # the receive loop must already be able to proceed.
        assert await asyncio.wait_for(waiting, 2)
        assert not release_ack.is_set()
        assert sent == ["data-first", "ack-1"]
        assert not connection.unusable
        release_ack.set()
        assert await asyncio.wait_for(data, 2)
        await asyncio.wait_for(ack_two_sent.wait(), 2)
        assert sent == ["data-first", "ack-1", "ack-2"]
    finally:
        release.set()
        release_ack.set()
        connection.close()

@pytest.mark.asyncio
async def test_agent_retains_cancel_delivered_before_its_call(agent, monkeypatch):
    instance, project, _root = agent
    call = request(project, "read", path="README.md")
    frames = []

    async def capture(frame):
        frames.append(frame)

    async def must_not_execute(*_args, **_kwargs):
        pytest.fail("cancelled call reached execution")

    monkeypatch.setattr(instance, "send", capture)
    monkeypatch.setattr(instance, "execute", must_not_execute)
    instance.cancel_call(call["id"])
    await asyncio.wait_for(instance.handle(call), 2)
    assert instance.journal.is_cancelled(call["id"])
    assert not any(frame.get("type") == "started" for frame in frames)
    assert instance.journal.status(call["id"])["result"]["error"]["code"] == "CANCELLED"

@pytest.mark.asyncio
async def test_worker_snapshot_neither_publishes_nor_consumes_authority_cache(delivery):
    e = delivery
    connection = e.runtime.connections["hot"]
    # A snapshot is evaluated against its own data, not another thread's cache.
    before = (e.store.session_revision, "hot", False)
    connection._auth_cache = before
    assert await e.store.run_read(
        e.runtime.connection_authorized, "hot", connection, allow_cache=True
    )
    assert connection._auth_cache is before
    assert e.runtime.connection_authorized("hot", connection)
    assert connection._auth_cache == (e.store.session_revision, "hot", True)
