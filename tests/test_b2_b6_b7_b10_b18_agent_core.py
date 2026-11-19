"""Targeted anti-regression tests for B2, B6, B7, B10, B15 hooks, B16, and B18."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import threading
import time
import uuid

import pytest

from agent.journal import Journal
from agent.resource_queue import Claim, ResourceQueue, claims_for
from agent.scheduler import Pressure, ProjectScheduler
from agent import workspace_probe
from shared.crypto import SecureChannel, token
from shared.scheduler_config import is_read_only_exec, validate_scheduler
from shared.tool_protocol import advertisement
from shared.util import DevError
from tests.test_core_tools import agent, request


@pytest.mark.asyncio
async def test_agent_journal_uses_wal_full_and_never_blocks_event_loop(agent, monkeypatch):
    instance, project, root = agent
    db = instance.journal.db
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert db.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY

    # Seed two accepted calls before simulating a slow disk thread holding journal.lock.
    active_id = uuid.uuid4().hex
    pending_id = uuid.uuid4().hex
    instance.journal.start(active_id, {"tool": "read", "project": project, "args": {"path": "a"}})
    instance.journal.start(
        pending_id, {"tool": "read", "project": project, "args": {"path": "b"}}
    )
    instance.jobs[active_id] = asyncio.current_task()

    locked = threading.Event()
    release = threading.Event()

    def hold_journal_lock():
        with instance.journal.lock:
            locked.set()
            release.wait(5)

    locker = threading.Thread(target=hold_journal_lock, daemon=True)
    locker.start()
    try:
        assert await asyncio.to_thread(locked.wait, 2)
        t0 = time.monotonic()
        instance.phase(active_id, "waiting_worker", queue_reason="lane_capacity", lane="read")
        instance._mark_running(active_id)
        assert instance.journal.status(active_id)["status"] == "running"
        assert instance.job_counts()["running_jobs"] == 1
        assert instance.cancel_pending_call(pending_id) is True
        assert instance.journal.is_cancelled(pending_id) is True
        assert len(instance.telemetry.snapshot(active_id)) >= 1
        assert time.monotonic() - t0 < 0.02
    finally:
        instance.jobs.pop(active_id, None)
        release.set()
        locker.join(timeout=2)

    if instance._journal_persist_task is not None:
        await asyncio.wait_for(instance._journal_persist_task, 2)

    # Full handle() path: single telemetry.snapshot on persist, no trace:* leak, durable receipt.
    (root / "hello.txt").write_text("hello wal full\n", encoding="utf-8")
    call = request(project, "read", path="hello.txt")
    persisting_snapshots = 0
    real_snapshot = instance.telemetry.snapshot
    real_finish = instance.journal.finish
    persisted_with_count = []

    def tracked_snapshot(identifier):
        nonlocal persisting_snapshots
        res = real_snapshot(identifier)
        if identifier == call["id"] and any(row["stage"] == "persisting" for row in res):
            persisting_snapshots += 1
        return res

    def tracked_finish(identifier, result):
        if identifier == call["id"]:
            persisted_with_count.append(persisting_snapshots)
        return real_finish(identifier, result)

    monkeypatch.setattr(instance.telemetry, "snapshot", tracked_snapshot)
    monkeypatch.setattr(instance.journal, "finish", tracked_finish)

    await instance.handle(call)

    # persisting phase calls snapshot once (not twice at L1450-1451) before journal.finish().
    assert persisted_with_count == [1]
    assert call["id"] not in instance.pending_output
    assert ("trace:" + call["id"]) not in instance.pending_output

    if instance._journal_persist_task is not None:
        await asyncio.wait_for(instance._journal_persist_task, 2)

    with closing(sqlite3.connect(instance.state_dir / "agent.sqlite3")) as raw_db:
        row = raw_db.execute(
            "SELECT status,result FROM calls WHERE id=?", (call["id"],)
        ).fetchone()
    assert row is not None and row[0] == "done"
    assert json.loads(row[1])["ok"] is True

    recovered = Journal(instance.state_dir)
    try:
        assert recovered.status(call["id"])["status"] == "done"
        assert recovered.status(call["id"])["result"]["ok"] is True
    finally:
        recovered.db.close()


@pytest.mark.asyncio
async def test_trace_key_cleaned_on_cancel_before_start(agent):
    instance, project, _ = agent
    await instance.scheduler.reconfigure(
        {"adaptive": False, "minimum": 1, "initial": 1, "maximum": 1}
    )
    call = request(project, "exec", command="true", idempotency_key="cancel-trace-cleanup")
    call_id = call["id"]
    instance.pending_output["trace:" + call_id] = {"type": "trace", "id": call_id, "trace": []}

    # Hold the single execution slot so call stays at "accepted" until cancelled.
    async with instance.scheduler.slot("holder-exec", "other-proj", "execution"):
        task = asyncio.create_task(instance.handle(call))
        try:
            async with asyncio.timeout(2):
                while not instance.scheduler.waiting and not task.done():
                    await asyncio.sleep(0.001)
            assert not task.done(), "operation finished before entering the admission queue"
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    assert call_id not in instance.pending_output
    assert ("trace:" + call_id) not in instance.pending_output


@pytest.mark.asyncio
async def test_fail_fast_rejects_capacity_wait_and_max_queue_ms_preserves_admitted():
    # B6: lane_capacity without resource blockers must immediately raise RESOURCE_BUSY.
    sched = ProjectScheduler({"adaptive": False, "read_limit": 1})
    async with sched.slot("holder-read", "P1", "read"):
        with pytest.raises(DevError) as caught:
            async with asyncio.timeout(1):
                async with sched.slot(
                    "ff-lane",
                    "P2",
                    "read",
                    wait_policy="fail_fast",
                    initial_wait_seconds=0.02,
                ):
                    pytest.fail("should not acquire full read lane")
        assert caught.value.code == "RESOURCE_BUSY"
        assert caught.value.status == 409
        assert caught.value.details["reason"] == "lane_capacity"
        assert caught.value.details["blocked_by"] == []

    # B6: project_limit without resource blockers.
    sched_proj = ProjectScheduler(
        {"adaptive": False, "read_limit": 4, "project_limits": {"P1": 1}}
    )
    async with sched_proj.slot("holder-proj", "P1", "read"):
        with pytest.raises(DevError) as caught_proj:
            async with asyncio.timeout(1):
                async with sched_proj.slot(
                    "ff-proj",
                    "P1",
                    "read",
                    wait_policy="fail_fast",
                    initial_wait_seconds=0.02,
                ):
                    pytest.fail("should not bypass project_limit")
        assert caught_proj.value.code == "RESOURCE_BUSY"
        assert caught_proj.value.details["reason"] == "project_limit"
        assert caught_proj.value.details["blocked_by"] == []

    # B6: resource_pressure (memory_paused) on execution lane.
    sched_press = ProjectScheduler({"adaptive": False, "initial": 2})
    await sched_press.update_pressure(Pressure(time.monotonic(), None, 1, 1024**3))
    with pytest.raises(DevError) as caught_press:
        async with asyncio.timeout(1):
            async with sched_press.slot(
                "ff-press",
                "P1",
                "execution",
                wait_policy="fail_fast",
                initial_wait_seconds=0.02,
            ):
                pytest.fail("should not enter memory-paused execution lane")
    assert caught_press.value.code == "RESOURCE_BUSY"
    assert caught_press.value.details["reason"] == "resource_pressure"
    assert caught_press.value.details["blocked_by"] == []

    # B6: remote_target_limit without resource blockers.
    sched_remote = ProjectScheduler(
        {"adaptive": False, "remote_limit": 4, "remote_target_limit": 1}
    )
    async with sched_remote.slot("holder-remote", "P1", "remote", "host.example:22"):
        with pytest.raises(DevError) as caught_remote:
            async with asyncio.timeout(1):
                async with sched_remote.slot(
                    "ff-remote",
                    "P2",
                    "remote",
                    "host.example:22",
                    wait_policy="fail_fast",
                    initial_wait_seconds=0.02,
                ):
                    pytest.fail("should not bypass remote_target_limit")
        assert caught_remote.value.code == "RESOURCE_BUSY"
        assert caught_remote.value.details["reason"] == "remote_target_limit"
        assert caught_remote.value.details["blocked_by"] == []

    # B7: max_queue_ms timeout cleans waiter when not admitted, but preserves admitted item on race.
    queue = ResourceQueue()
    sched_b7 = ProjectScheduler({"adaptive": False, "read_limit": 1}, resources=queue)
    async with sched_b7.slot("busy-read", "P1", "read"):
        with pytest.raises(DevError) as expired:
            async with sched_b7.slot("queued-read", "P2", "read", max_queue_ms=15):
                pytest.fail("should time out while busy")
        assert expired.value.code == "RESOURCE_BUSY"
        assert not sched_b7.waiting
        assert not queue.waiting

    # Same-tick race: event.set() happens while waiter is waiting for condition after TimeoutError.
    entered_race = asyncio.Event()
    holder_task_release = asyncio.Event()
    holder_acquired = asyncio.Event()

    async def hold_slot():
        async with sched_b7.slot("holder-race", "P1", "read"):
            holder_acquired.set()
            await holder_task_release.wait()

    holder = asyncio.create_task(hold_slot())
    await asyncio.wait_for(holder_acquired.wait(), 1)

    async def racing_waiter():
        async with sched_b7.slot("raced-waiter", "P2", "read", max_queue_ms=20):
            assert any(entry.operation_id == "raced-waiter" for entry in sched_b7.active.values())
            entered_race.set()

    waiter = asyncio.create_task(racing_waiter())
    await asyncio.sleep(0.005)
    async with sched_b7.condition:
        # Hold condition across max_queue_ms=20ms timeout so TimeoutError hits condition wait.
        await asyncio.sleep(0.04)
        # Move holder out of active and grant raced-waiter in the same critical section.
        holder_item = next(
            entry for entry in sched_b7.active.values() if entry.operation_id == "holder-race"
        )
        sched_b7.active.pop(holder_item.token, None)
        sched_b7._drain_all()
        assert sched_b7._events[
            next(
                entry.token
                for entry in sched_b7.active.values()
                if entry.operation_id == "raced-waiter"
            )
        ].is_set()

    await asyncio.wait_for(waiter, 1)
    assert entered_race.is_set()
    holder_task_release.set()
    await asyncio.wait_for(holder, 1)
    assert not sched_b7.active and not sched_b7.waiting


@pytest.mark.asyncio
async def test_read_probe_single_root_phase_and_fs_search_not_starved_by_exec(agent, monkeypatch):
    instance, project, root = agent
    (root / "sample.txt").write_text("needle line\nother line\n", encoding="utf-8")

    # 1. ReadPool.limit binds to scheduler read_limit=8, and PROBE_TIMEOUT is aligned to 30s.
    assert validate_scheduler({})["read_limit"] == 8
    assert workspace_probe.PROBE_TIMEOUT == 30
    assert workspace_probe.ReadPool().limit == 8
    bound_pool = workspace_probe.ReadPool(scheduler=instance.scheduler)
    assert bound_pool.limit == instance.scheduler.config["read_limit"]
    await instance.scheduler.reconfigure({"read_limit": 5})
    assert bound_pool.limit == 5
    await instance.scheduler.reconfigure({"read_limit": 8})
    assert bound_pool.limit == 8

    # Dynamic probe timeout tracks remaining operation budget capped at 30s.
    t20 = instance._probe_timeout(time.time() + 20)
    assert 18.0 <= t20 <= 20.5
    assert instance._probe_timeout(time.time() + 120) == 30.0

    # 2. Single "root" phase RPC per read (plus no duplicate integrations.project calls).
    phases_seen = []
    real_probe = workspace_probe.probe

    async def spy_probe(engine, tool, proj, args, *, phase="execute", **kwargs):
        phases_seen.append((tool, phase))
        return await real_probe(engine, tool, proj, args, phase=phase, **kwargs)

    monkeypatch.setattr(workspace_probe, "probe", spy_probe)
    read_call = request(project, "read", path="sample.txt")
    await instance.handle(read_call)
    assert instance.journal.status(read_call["id"])["result"]["ok"]
    assert phases_seen == [("read", "admission"), ("read", "root"), ("read", "execute")]
    assert [p for _, p in phases_seen].count("root") == 1

    # Direct ReadWorker without admission also has exactly ("root", "execute").
    non_admit_phases = []
    worker = workspace_probe.ReadWorker(admission=False)
    monkeypatch.setattr(
        worker,
        "process",
        type(
            "Proc",
            (),
            {
                "stdin": type(
                    "In", (), {"write": lambda *a: None, "drain": staticmethod(lambda: asyncio.sleep(0))}
                )(),
                "stdout": type(
                    "Out",
                    (),
                    {
                        "readline": staticmethod(
                            lambda: asyncio.sleep(
                                0, result=json.dumps({"ok": True, "data": {"x": 1}}).encode()
                            )
                        )
                    },
                )(),
                "wait": staticmethod(lambda: asyncio.sleep(0)),
                "returncode": 0,
            },
        )(),
    )
    for p in ("root", "execute"):
        res = await worker.request({"phase": p, "tool": "read", "args": {}})
        non_admit_phases.append(p)
        assert res["ok"]
    assert non_admit_phases == ["root", "execute"]
    with pytest.raises(DevError):
        await worker.request({"phase": "root", "tool": "read", "args": {}})

    # Check single integrations.project call post-queue when workspace_id is used.
    proj_calls = []

    def fake_project(proj, args):
        proj_calls.append(args.get("workspace_id"))
        return proj

    monkeypatch.setattr(instance.integrations, "project", fake_project)
    ws_call = request(project, "read", path="sample.txt", workspace_id="a" * 32)
    await instance.handle(ws_call)
    # One pre-queue resolution + one post-queue recheck (never duplicate post-queue).
    assert len(proj_calls) == 2

    # 3. fs_search runs in < 1s even when execution lane is saturated AND when memory_paused.
    (root / "exec_dir").mkdir(exist_ok=True)
    search_dir = root / "search_dir"
    search_dir.mkdir(exist_ok=True)
    (search_dir / "target.txt").write_text("needle line\nother line\n", encoding="utf-8")
    await instance.scheduler.reconfigure(
        {"adaptive": False, "minimum": 1, "initial": 1, "maximum": 1, "read_limit": 4}
    )
    exec_entered = asyncio.Event()
    exec_release = asyncio.Event()
    real_execute = instance.execute

    async def fake_execute(identifier, tool, current, args):
        if tool == "exec":
            exec_entered.set()
            await exec_release.wait()
            return {"exit_code": 0, "output": "ok"}
        return await real_execute(identifier, tool, current, args)

    monkeypatch.setattr(instance, "execute", fake_execute)
    busy_exec = asyncio.create_task(
        instance.handle(
            request(
                project,
                "exec",
                command="sleep 10",
                cwd="exec_dir",
                idempotency_key="busy-exec-1",
            )
        )
    )
    try:
        await asyncio.wait_for(exec_entered.wait(), 1)
        search_call = request(project, "fs_search", query="needle", path="search_dir")
        t0 = time.monotonic()
        await asyncio.wait_for(instance.handle(search_call), 1.0)
        assert time.monotonic() - t0 < 1.0
        search_res = instance.journal.status(search_call["id"])["result"]
        assert search_res["ok"] and search_res["data"]["matches"]
    finally:
        exec_release.set()
        await asyncio.wait_for(busy_exec, 2)
        monkeypatch.setattr(instance, "execute", real_execute)

    # Also verify fs_search works under memory_paused (execution limit == 0).
    await instance.scheduler.update_pressure(Pressure(time.monotonic(), None, 1, 1024**3))
    assert instance.scheduler.capacity.memory_paused
    search_paused = request(project, "fs_search", query="needle", path="search_dir")
    await asyncio.wait_for(instance.handle(search_paused), 1.0)
    assert instance.journal.status(search_paused["id"])["result"]["ok"]


@pytest.mark.asyncio
async def test_scheduler_single_project_slot_borrowing_and_reservation():
    # Read lane: single active project borrows full read_limit=4 when uncontended.
    sched = ProjectScheduler({"adaptive": False, "initial": 2, "maximum": 4, "read_limit": 4})
    async with sched.slot("r1", "A", "read"):
        async with sched.slot("r2", "A", "read"):
            async with sched.slot("r3", "A", "read"):
                async with sched.slot("r4", "A", "read"):
                    assert sched.snapshot("A")["lanes"]["read"]["running"] == 4

    # Execution lane with limit=2: borrows full 2 when uncontended; reserves 1 if B is waiting.
    async with sched.slot("e1", "A", "execution"):
        async with sched.slot("e2", "A", "execution"):
            assert sched.snapshot("A")["lanes"]["execution"]["running"] == 2

    # When project B is waiting on read lane, A leaves 1 slot (max 3 of 4).
    entered = []
    release = asyncio.Event()

    async def hold_read(op_id, proj):
        async with sched.slot(op_id, proj, "read"):
            entered.append((op_id, proj))
            await release.wait()

    t1 = asyncio.create_task(hold_read("a1", "A"))
    t2 = asyncio.create_task(hold_read("a2", "A"))
    t3 = asyncio.create_task(hold_read("a3", "A"))
    await asyncio.sleep(0)
    # Hold a 4th slot? First put B in waiting before a4 arrives by saturating or using condition.
    async with sched.condition:
        from agent.scheduler import Ticket

        b_ticket = Ticket(object(), "b-wait", "B", "read", "")
        sched.waiting.append(b_ticket)
        sched._events[b_ticket.token] = asyncio.Event()
        # Now with B waiting on read lane, another A item sees project_limit at 3!
        a4_item = Ticket(object(), "a4", "A", "read", "")
        assert sched._reason(a4_item) == "project_limit"
        sched.waiting.remove(b_ticket)
        sched._events.pop(b_ticket.token, None)
        # Without B waiting, A can borrow slot 4!
        assert sched._reason(a4_item) is None
    release.set()
    await asyncio.gather(t1, t2, t3)


@pytest.mark.asyncio
async def test_exec_read_only_shared_mode_non_exclusive_under_strict_semantics(agent):
    instance, project, root = agent
    # Redline/contract: validate_scheduler({}) keeps "strict" default!
    assert validate_scheduler({})["resource_semantics"] == "strict"
    assert instance.scheduler.config["resource_semantics"] == "strict"

    assert is_read_only_exec("exec", {"read_only": True}) is True
    assert is_read_only_exec("exec", {"options": {"read_only": True}}) is True
    assert is_read_only_exec("exec", {"resources": [{"kind": "path", "name": ".", "mode": "read"}]}) is True
    assert is_read_only_exec("exec", {"command": "ls"}) is False

    ro_claims = claims_for(
        instance.engine,
        "exec",
        project,
        {"command": "ls", "cwd": ".", "read_only": True, "resources": []},
        root,
    )
    assert ro_claims == [
        Claim("agent", "path", ro_claims[0].name, False, "process", False)
    ]

    # Under strict semantics, read_only=True exec and concurrent file read do not block each other!
    (root / "doc.txt").write_text("concurrent read ok\n", encoding="utf-8")
    exec_entered = asyncio.Event()
    exec_release = asyncio.Event()
    real_execute = instance.execute

    async def fake_execute(identifier, tool, current, args):
        if tool == "exec":
            exec_entered.set()
            await exec_release.wait()
            return {"exit_code": 0, "output": "ok"}
        return await real_execute(identifier, tool, current, args)

    instance.execute = fake_execute
    ro_exec_call = request(project, "exec", command="git status", idempotency_key="ro-exec-1")
    ro_exec_call["args"]["read_only"] = True
    exec_task = asyncio.create_task(instance.handle(ro_exec_call))
    try:
        await asyncio.wait_for(exec_entered.wait(), 1)
        read_call = request(project, "read", path="doc.txt")
        await asyncio.wait_for(instance.handle(read_call), 5)
        assert instance.journal.status(read_call["id"])["result"]["ok"]
    finally:
        exec_release.set()
        await asyncio.wait_for(exec_task, 2)
        instance.execute = real_execute


@pytest.mark.asyncio
async def test_ws_recv_loop_nonblocking_and_batches_acks(agent, monkeypatch):
    instance, project, _ = agent

    # Seed 3 un-acked finished calls + 1 active running call + 1 pending call.
    ack_ids = [uuid.uuid4().hex for _ in range(3)]
    for aid in ack_ids:
        instance.journal.start(aid, {"tool": "read", "project": project, "args": {"path": aid}})
        instance.journal.finish(aid, {"ok": True, "data": {"id": aid}})

    running_id = uuid.uuid4().hex
    instance.journal.start(running_id, {"tool": "exec", "project": project, "args": {}})
    instance.journal.mark_running(running_id)
    instance.jobs[running_id] = asyncio.create_task(asyncio.sleep(5))

    pending_id = uuid.uuid4().hex
    instance.journal.start(pending_id, {"tool": "read", "project": project, "args": {"path": "p"}})

    # Hold acks_batch behind a threading.Event so any inline await in WS recv loop would stall!
    batch_release = threading.Event()
    batched_calls = []
    real_acks_batch = instance.journal.acks_batch

    def slow_acks_batch(ids):
        batched_calls.append(list(ids))
        batch_release.wait(5)
        return real_acks_batch(ids)

    monkeypatch.setattr(instance.journal, "acks_batch", slow_acks_batch)

    status_replies = []
    real_report_status = instance.report_status

    async def capture_report_status(identifier):
        status_replies.append(identifier)
        return await real_report_status(identifier)

    monkeypatch.setattr(instance, "report_status", capture_report_status)

    # Use real SecureChannel + >64 KiB payload (B16) to verify async crypto offload too!
    secret = token()
    challenge = token()
    device_id = instance.config["device_id"]
    instance.config["secret"] = secret
    hub_channel = SecureChannel(secret, challenge, device_id, "hub")

    large_note = "x" * 70000
    frames = [
        {"type": "ack", "id": ack_ids[0]},
        {"type": "ack", "id": ack_ids[1]},
        {"type": "ack", "id": ack_ids[2]},
        {"type": "probe", "id": running_id},
        {"type": "call", "id": running_id, "tool": "exec", "project": project, "args": {}},
        {"type": "cancel_pending", "id": pending_id},
        {"type": "going_away", "reconnect_window_seconds": 5, "note": large_note},
    ]

    sent_packets = []
    ready_payload = {
        "type": "ready",
        **advertisement(instance.build.catalog_sha256),
        "scheduler_config": {"config": {}, "revision": __import__("hashlib").sha256(b"{}").hexdigest()},
    }

    class EncryptedSocket:
        def __init__(self):
            self.handshake = [
                json.dumps({"type": "challenge", "challenge": challenge}),
            ]
            self.frame_iter = iter(frames)
            self.recv_start = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def recv(self):
            if self.handshake:
                return self.handshake.pop(0)
            return hub_channel.pack(ready_payload)

        async def send(self, packet):
            msg = hub_channel.unpack(packet)
            sent_packets.append(msg)
            if msg.get("type") == "hello":
                ready_payload["channel_binding"] = dict(msg["channel_binding"])

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.recv_start is None:
                self.recv_start = time.monotonic()
            try:
                frame = next(self.frame_iter)
            except StopIteration:
                # Release acks_batch only AFTER all recv frames were pumped through the loop!
                assert time.monotonic() - self.recv_start < 0.5
                batch_release.set()
                raise StopAsyncIteration from None
            return hub_channel.pack(frame)

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr("agent.runner.websockets.connect", lambda *a, **kw: EncryptedSocket())
    monkeypatch.setattr(instance, "heartbeat", idle)
    monkeypatch.setattr(instance.native, "sync", idle)

    t0 = time.monotonic()
    try:
        await asyncio.wait_for(instance.connect_once(), 3)
    finally:
        batch_release.set()
        job = instance.jobs.pop(running_id, None)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    assert time.monotonic() - t0 < 2.0
    assert status_replies == [running_id, running_id]
    assert instance.journal.is_cancelled(pending_id)
    assert instance.reconnect_window == 5
    # Verify all 3 ACKs were coalesced in batch and persisted to SQLite!
    assert sum(len(b) for b in batched_calls) == 3
    assert len(batched_calls) == 1
    with closing(sqlite3.connect(instance.state_dir / "agent.sqlite3")) as raw_db:
        rows = raw_db.execute(
            f"SELECT acked FROM calls WHERE id IN ({','.join('?' for _ in ack_ids)})", ack_ids
        ).fetchall()
    assert [r[0] for r in rows] == [1, 1, 1]

    # Verify Agent.send() also offloads >64 KiB encryption cleanly via pack_channel.
    send_ch_agent = SecureChannel(secret, challenge, device_id, "agent")
    send_ch_hub = SecureChannel(secret, challenge, device_id, "hub")
    outbound = []

    class OutSocket:
        async def send(self, packet):
            outbound.append(send_ch_hub.unpack(packet))

        async def close(self, *a, **kw):
            pass

    instance.socket, instance.channel = OutSocket(), send_ch_agent
    assert await instance.send({"type": "large_test", "blob": "y" * 70000}) is True
    assert outbound and len(outbound[0]["blob"]) == 70000
    instance.socket = instance.channel = None


@pytest.mark.asyncio
async def test_agent_memory_pressure_and_shutdown_invoke_lsp_hooks(agent, monkeypatch):
    from agent import lsp_navigation

    instance, project, _ = agent
    evicted = []
    shut_down = []

    async def fake_evict(ag=None, *, keep=0):
        evicted.append((ag, keep))
        return 1

    async def fake_shutdown(ag=None):
        shut_down.append(ag)
        return 1

    monkeypatch.setattr(lsp_navigation, "evict_for_memory_pressure", fake_evict)
    monkeypatch.setattr(lsp_navigation, "shutdown_all", fake_shutdown)

    # Trigger one iteration of watch_scheduler under memory pressure.
    monkeypatch.setattr(
        instance.resource_sampler,
        "sample",
        lambda: Pressure(time.monotonic(), 0.95, 64 * 1024**2, 1024**3),
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        instance.stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    await instance.watch_scheduler()
    await stopper
    assert evicted == [(instance, 0)]

    # Reset stop_event and verify DEVICE_ACTIONS lifecycle handoff triggers shutdown_all.
    from shared.tool_protocol import wire_version

    instance.stop_event.clear()
    monkeypatch.setattr(instance.lifecycle, "prepare", lambda *a: asyncio.sleep(0, result={"ok": True}))
    call = {
        "id": uuid.uuid4().hex,
        "tool": "agent_restart",
        "tool_contract_version": wire_version("agent_restart"),
        "project": project,
        "args": {},
    }
    await instance.handle(call)
    assert shut_down == [instance]


@pytest.mark.asyncio
async def test_mutation_start_is_durable_before_started_and_execution(agent, monkeypatch, tmp_path):
    instance, project, _ = agent
    call = request(project, "exec", command="unused", read_only=True, idempotency_key=uuid.uuid4().hex)
    identifier = call["id"]
    received = threading.Event()
    release = threading.Event()
    original_mark = instance.journal.mark_running_db
    events = []
    original_send = instance.send
    restart_dir = tmp_path / "restart-at-execution"
    restart_dir.mkdir()

    def hold_start(identifier):
        received.set()
        if not release.wait(5):
            raise TimeoutError("start persistence test did not release")
        return original_mark(identifier)

    async def send(message):
        if message.get("type") == "started" and message.get("id") == identifier:
            events.append("started")
        return await original_send(message)

    async def execute(*args, **kwargs):
        # Simulate restarting at the first externally visible action, without
        # performing the requested shell command or using a live system.
        with closing(sqlite3.connect(restart_dir / "agent.sqlite3")) as output:
            instance.journal.db.backup(output)
        with closing(Journal(restart_dir).db) as recovered:
            row = recovered.execute("SELECT status FROM calls WHERE id=?", (identifier,)).fetchone()
            assert row["status"] == "interrupted"
        events.append("executed")
        return {}

    monkeypatch.setattr(instance.journal, "mark_running_db", hold_start)
    monkeypatch.setattr(instance, "send", send)
    monkeypatch.setattr(instance, "execute", execute)
    task = asyncio.create_task(instance.handle(call))
    try:
        assert await asyncio.to_thread(received.wait, 2)
        assert events == []
        assert instance.journal.fast_status(identifier)["status"] == "accepted"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert events == ["started", "executed"]


def test_resource_claims_keep_local_task_and_explicit_resource_policy(agent):
    instance, project, root = agent
    instance.engine.config["tasks"] = {"exclusive": {"allow_read_concurrency": False}}
    declared = {"read_only": True, "cwd": "."}
    for mode in ("write", "read_stable"):
        args = {**declared, "resources": [{"kind": "path", "name": ".", "mode": mode}]}
        claim, = claims_for(instance.engine, "exec", project, args, root)
        assert claim.write is (mode == "write")
        assert claim.stable is (mode == "read_stable")
        assert not is_read_only_exec("exec", args, config=instance.engine.config)
    args = {**declared, "task": "exclusive"}
    claim, = claims_for(instance.engine, "exec", project, args, root)
    assert claim.write and claim.stable
    assert not is_read_only_exec("exec", args, config=instance.engine.config)


@pytest.mark.asyncio
async def test_admitted_read_retains_own_execution_timeout(agent, monkeypatch):
    instance, project, root = agent
    identifier = uuid.uuid4().hex
    args = {"path": "unused"}
    state = {}

    async def probe(engine, tool, project, args, *, worker, expected_root, timeout_seconds=None):
        state["timeout"] = workspace_probe.effective_probe_timeout(timeout_seconds)
        return {}

    monkeypatch.setattr(workspace_probe, "probe", probe)
    async with instance._read_execution_worker(identifier, "read", args, root, existing=object()):
        instance._read_worker_deadlines = {identifier: time.time() - 1}
        assert await instance.execute(identifier, "read", project, args) == {}
    assert state["timeout"] == workspace_probe.PROBE_TIMEOUT


@pytest.mark.asyncio
async def test_terminal_cache_bound_and_retention_leave_durable_tombstones(agent, monkeypatch):
    from agent import journal

    instance, project, _ = agent
    monkeypatch.setattr(journal, "TERMINAL_CACHE_LIMIT", 2)
    identifiers = [uuid.uuid4().hex for _ in range(3)]
    for identifier in identifiers:
        instance.journal.start(identifier, request(project, "exec", command="unused", idempotency_key=identifier))
        instance.journal.finish(identifier, {"ok": True, "data": {}})
        instance.journal.ack(identifier)

    cold, _, newest = identifiers
    assert len(instance.journal.terminal_statuses) == 2
    assert instance.journal.fast_status(cold)["status"] == "missing"
    assert instance.journal.status(cold)["status"] == "done"
    await instance._cancel_pending_frame(cold)
    assert not instance.journal.is_cancelled(cold)

    pruned = instance.journal.prune(now=time.time() + 365 * 86400)
    assert pruned["calls"] == 3
    assert not instance.journal.terminal_statuses
    assert instance.journal.fast_status(newest)["status"] == "missing"
    assert instance.journal.status(newest)["result"]["error"]["code"] == "RESULT_EXPIRED"


@pytest.mark.asyncio
async def test_mutation_that_expires_during_durable_start_never_begins(agent, monkeypatch):
    from types import SimpleNamespace

    from agent import runner

    instance, project, _ = agent
    call = request(project, "exec", command="unused", idempotency_key=uuid.uuid4().hex)
    identifier = call["id"]
    wall = [time.time()]
    call["not_after"] = wall[0] + 30
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=lambda: wall[0], monotonic=time.monotonic))

    original_mark = instance.journal.mark_running_db
    original_send = instance.send
    started = []
    executed = []

    def mark_then_expire(identifier):
        changed = original_mark(identifier)
        assert changed
        wall[0] = call["not_after"] + 1
        return changed

    async def send(message):
        if message.get("type") == "started" and message.get("id") == identifier:
            started.append(identifier)
        return await original_send(message)

    async def execute(*args, **kwargs):
        executed.append(identifier)
        return {}

    monkeypatch.setattr(instance.journal, "mark_running_db", mark_then_expire)
    monkeypatch.setattr(instance, "send", send)
    monkeypatch.setattr(instance, "execute", execute)
    await asyncio.wait_for(instance.handle(call), 3)
    assert not started and not executed
    receipt = instance.journal.status(identifier)
    assert receipt["status"] == "done"
    assert receipt["result"]["error"]["code"] == "QUEUE_EXPIRED"
