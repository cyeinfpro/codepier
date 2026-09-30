"""Runtime recovery, connection fencing and fault-containment regressions."""
from __future__ import annotations

import asyncio
import gc
import json
import time
import weakref

import pytest
from fastapi import WebSocketDisconnect

from hub.runtime import Connection, Principal, Runtime
from hub.store import Store
from shared.crypto import SecureChannel, token
from shared.util import DevError


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "hub")
    from tests.legacy_iam_fixture import seed_owner
    seed_owner(store,'owner','owner')
    secret = token()
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','home',?,?)",
                  (store.encrypt(secret), time.time()))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES ('proj','Project','project','dev','/tmp/project','','write',1,?)",
                  (time.time(),))
    runtime = Runtime(store)
    runtime.wait_seconds = 0
    principal = Principal("panel:owner", "owner", {"read", "write", "execute"}, ["*"], admin=True)
    yield runtime, principal, secret
    store.close()


class Socket:
    def __init__(self, secret, hello=None, before_hello=None, after_ready=None):
        self.secret = secret
        self.hello = hello or {"type": "hello", "delivery_protocol": 2, "journal_id": "journal-one"}
        self.before_hello, self.after_ready = before_hello, after_ready
        self.incoming = asyncio.Queue()
        self.packets = []
        self.closed = False
        self.first = True

    async def accept(self):
        pass

    async def send_json(self, packet):
        self.channel = SecureChannel(self.secret, packet["challenge"], "dev", "agent")

    async def send_text(self, packet):
        data = self.channel.unpack(packet)
        self.packets.append(data)
        if data["type"] == "ready" and self.after_ready:
            self.after_ready()

    async def receive_text(self):
        if self.first:
            self.first = False
            if self.before_hello:
                self.before_hello()
            return self.channel.pack(self.hello)
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return self.channel.pack(item)

    async def close(self, **kwargs):
        self.closed = True
        self.incoming.put_nowait(None)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 1)


@pytest.mark.asyncio
async def test_failed_send_fences_already_waiting_sender():
    started, release = asyncio.Event(), asyncio.Event()

    class FailedSocket:
        calls = 0
        async def send_text(self, packet):
            self.calls += 1
            started.set()
            await release.wait()
            raise OSError("link lost")
        async def close(self, **kwargs):
            pass

    socket = FailedSocket()
    channel = SecureChannel(token(), token(), "dev", "hub")
    connection = Connection(socket, channel)
    first = asyncio.create_task(connection.send({"type": "one"}))
    await started.wait()
    second = asyncio.create_task(connection.send({"type": "two"}))
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(item, OSError) for item in outcomes)
    assert connection.unusable and socket.calls == channel.sent == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hello", "ready"])
@pytest.mark.parametrize("change", ["disabled", "rotated", "removed"])
async def test_credentials_revoked_during_handshake_cannot_register(runtime, phase, change):
    r, _, secret = runtime
    def revoke():
        if change == "disabled":
            r.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
        elif change == "rotated":
            r.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (r.store.encrypt(token()),))
        else:
            r.store.execute("DELETE FROM projects")
            r.store.execute("DELETE FROM devices")
    socket = Socket(secret, **{"before_hello" if phase == "hello" else "after_ready": revoke})
    await r.agent_socket(socket, "dev")
    assert socket.closed and not r.connections
    assert not r.store.one("SELECT id FROM audit WHERE action='device.connected'")


@pytest.mark.asyncio
@pytest.mark.parametrize("hello", [
    {"type": "hello"},
    {"type": "hello", "delivery_protocol": 1, "journal_id": "legacy"},
    {"type": "hello", "delivery_protocol": 2},
    {"type": "hello", "delivery_protocol": 2, "journal_id": []},
])
async def test_agent_without_durable_journal_protocol_is_not_admitted(runtime, hello):
    r, _, secret = runtime
    socket = Socket(secret, hello=hello)
    await r.agent_socket(socket, "dev")
    assert not r.connections and socket.closed and not socket.packets


@pytest.mark.asyncio
async def test_disabled_device_is_fenced_before_close_completes(runtime):
    r, p, secret = runtime
    op = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    r.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
    assert not r.online("dev")
    await r.deliver(op["operation_id"])
    assert not [packet for packet in socket.packets if packet["type"] == "call"]
    await r.disconnect_device("dev", "owner disabled")
    await reader
    assert not r.connections


@pytest.mark.asyncio
async def test_storage_failure_cannot_prevent_connection_teardown(runtime, monkeypatch):
    r, _, secret = runtime
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    def unavailable(*args, **kwargs):
        raise OSError("storage temporarily unavailable")
    monkeypatch.setattr(r.store, "execute", unavailable)
    await r.disconnect_device("dev", "owner disabled")
    await reader
    assert socket.closed and not r.connections


@pytest.mark.asyncio
async def test_authenticated_late_events_cannot_cross_key_rotation(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    r.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (r.store.encrypt(token()),))
    socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": {"ok": True, "data": {"content": "stale device"}}})
    await reader
    assert r.operation(receipt["operation_id"], p)["result"] is None


@pytest.mark.asyncio
async def test_reconnect_outbox_cannot_bypass_journal_epoch_fence(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    r.store.execute("UPDATE operations SET attempts=1,journal_id='lost-journal' WHERE id=?", (receipt["operation_id"],))
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": {"ok": True, "data": {"content": "foreign journal"}}})
    await until(lambda: any(item["type"] == "ack" for item in socket.packets))
    final = r.operation(receipt["operation_id"], p)
    assert final["state"] == "needs_review" and final["result"]["error"]["code"] == "JOURNAL_CHANGED"
    await socket.close()
    await reader


@pytest.mark.asyncio
async def test_final_result_is_immutable_even_with_stale_operation_snapshot(runtime):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    row = r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],))
    r.complete(row, {"ok": True, "data": {"content": "first result"}})
    r.complete(row, {"ok": True, "data": {"content": "duplicate result"}})
    assert r.operation(row["id"], p)["result"]["data"]["content"] == "first result"
    assert r.store.one("SELECT count(*) AS n FROM audit WHERE status='succeeded'")["n"] == 1


@pytest.mark.asyncio
async def test_completed_result_future_is_not_retained_by_expiry_timer(runtime):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    pending = weakref.ref(r.futures[receipt["operation_id"]])
    r.complete(r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],)), {"ok": True, "data": {"content": "large result"}})
    await asyncio.sleep(0)
    gc.collect()
    assert pending() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"ok": True, "data": None}, {"ok": False, "error": None}])
async def test_null_result_fields_remain_retrievable_on_idempotent_retry(runtime, result):
    r, p, _ = runtime
    args = {"project": "Project", "path": "README.md", "idempotency_key": "null-result-replay"}
    receipt = await r.invoke("fs_read", args, p)
    r.complete(r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],)), result)
    if result["ok"]:
        assert await r.invoke("fs_read", args, p) == {"operation_id": receipt["operation_id"]}
    else:
        with pytest.raises(DevError) as error:
            await r.invoke("fs_read", args, p)
        assert error.value.code == "REMOTE_ERROR"


@pytest.mark.asyncio
async def test_replayed_interrupted_result_is_acknowledged_without_reauditing(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("tasks_run", {"project": "Project", "task": "test", "idempotency_key": "interrupted-replay"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    packet = {"type": "result", "id": receipt["operation_id"], "result": {"ok": False, "error": {"code": "INTERRUPTED", "message": "local restart"}}}
    socket.incoming.put_nowait(packet)
    socket.incoming.put_nowait(packet)
    await until(lambda: len([item for item in socket.packets if item["type"] == "ack"]) == 2)
    await socket.close()
    await reader
    assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"
    assert r.store.one("SELECT count(*) AS n FROM audit WHERE status='needs_review'")["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, [], {"ok": True, "data": ["bad"]}, {"ok": False, "error": {"message": {"bad": "type"}}}])
async def test_malformed_result_does_not_poison_agent_outbox(runtime, result):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": result})
    await until(lambda: any(item["type"] == "ack" for item in socket.packets))
    assert r.online("dev")
    assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"
    await socket.close()
    await reader


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"tool": "fs_read", "args": {}, "project": []}, {"tool": "removed-tool", "args": {}, "project": {"root": "/tmp/project"}}])
async def test_invalid_durable_request_does_not_retry_forever(runtime, payload):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    r.store.execute("UPDATE operations SET payload=? WHERE id=?", (r.store.encrypt(json.dumps(payload)), receipt["operation_id"]))
    await r.deliver(receipt["operation_id"])
    assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reconnecting", "cancelling"])
async def test_legacy_pending_without_request_is_reconciled_on_start(runtime, state):
    r, p, _ = runtime
    args = {"project": "Project", "path": "README.md", "idempotency_key": "legacy-receipt"}
    receipt = await r.invoke("fs_read", args, p)
    r.store.execute("UPDATE operations SET state=?,payload=NULL WHERE id=?", (state, receipt["operation_id"]))
    await r.start()
    try:
        repeated = await r.invoke("fs_read", args, p)
        assert repeated["state"] == "needs_review" and not repeated["pending"] and repeated["next"] is None
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_scheduler_survives_temporary_storage_and_audit_failure(runtime, monkeypatch):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    original_all, original_audit = r.store.all, r.store.audit
    failed = asyncio.Event()
    def unavailable(*args, **kwargs):
        failed.set()
        raise OSError("injected storage unavailable")
    monkeypatch.setattr(r.store, "all", unavailable)
    monkeypatch.setattr(r.store, "audit", unavailable)
    r.worker = asyncio.create_task(r.delivery_loop())
    await failed.wait()
    await asyncio.sleep(0)
    assert not r.worker.done()
    monkeypatch.setattr(r.store, "all", original_all)
    monkeypatch.setattr(r.store, "audit", original_audit)
    r.store.execute("UPDATE operations SET deadline=1 WHERE id=?", (receipt["operation_id"],))
    r.wake.set()
    await until(lambda: not r.operation(receipt["operation_id"], p)["pending"])
    await r.stop()
