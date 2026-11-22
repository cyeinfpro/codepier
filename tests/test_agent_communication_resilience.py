"""Focused communication isolation faults; no remote services or credentials."""

from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import agent.runner as runner
from agent.communication import MetadataSampler
from agent.runner import Agent
from shared.crypto import SecureChannel
from shared.util import atomic_json


async def eventually(predicate, seconds=2):
    loop = asyncio.get_running_loop()
    until = loop.time() + seconds
    while not predicate():
        assert loop.time() < until, "Expected condition did not become true"
        await asyncio.sleep(0.005)


class Build:
    component = "agent"
    catalog_sha256 = "c" * 64

    def __init__(self):
        self.loaded = {
            "version": "test",
            "source_sha256": "a" * 64,
            "errors": [],
            "instance_id": "f" * 32,
            "pid": 1,
        }
        self.value = {
            "component": self.component,
            "runtime": copy.deepcopy(self.loaded),
            "disk": {"version": "test", "source_sha256": "a" * 64, "errors": []},
            "restart_required": False,
            "source_verified": True,
            "catalog_sha256": self.catalog_sha256,
        }

    def describe(self):
        return copy.deepcopy(self.value)


def sampler():
    return MetadataSampler(
        Build(),
        SimpleNamespace(
            describe=lambda: {
                "control_ready": True,
                "update_ready": True,
                "status": "ready",
            }
        ),
    )


@pytest.fixture
def local_agent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "config.json"
    atomic_json(
        path,
        {
            "hub_url": "http://127.0.0.1:9",
            "device_id": "fixture",
            "secret": "synthetic-communication-test-secret-0123456789",
            "state_dir": str(tmp_path / "state"),
            "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
        },
    )
    instance = Agent(path)
    yield instance
    instance.journal.db.close()
    instance.instance_lock.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Requires a POSIX blocking FIFO")
@pytest.mark.parametrize("shutdown", ["close", "timeout"])
async def test_metadata_worker_reaps_kernel_blocked_read(local_agent, tmp_path, shutdown):
    metadata = local_agent.metadata
    metadata.read_timeout = 0.75 if shutdown == "timeout" else 10.0
    fifo, entered = tmp_path / "blocked", tmp_path / "entered"
    os.mkfifo(fifo)
    child_code = """
import os, pathlib, sys
sys.stdin.buffer.read()
pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))
# No writer will open the FIFO. This OS read path cannot be released by asyncio.
with open(sys.argv[1], "rb") as stream:
    stream.read(1)
"""
    metadata._worker_command = lambda: (
        sys.executable,
        "-I",
        "-B",
        "-c",
        child_code,
        str(fifo),
        str(entered),
    )
    first = asyncio.create_task(metadata.refresh())
    second = None
    try:
        await eventually(entered.is_file)
        worker, owner = metadata._worker, metadata._refresh_task
        assert worker.returncode is None
        assert int(entered.read_text()) == worker.pid
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(metadata.refresh())
        await asyncio.sleep(0)
        assert metadata._refresh_task is owner and metadata._worker is worker
        assert metadata.snapshot()["build"]["observation_state"] == "pending"
        if shutdown == "close":
            await asyncio.wait_for(metadata.close(), 2)
        await asyncio.wait_for(second, 2)
        assert worker.returncode is not None
        assert metadata._worker is None
        assert metadata.snapshot()["build"]["source_verified"] is False
        assert metadata.observation()["error_type"] == (
            "TimeoutError" if shutdown == "timeout" else None
        )
    finally:
        await metadata.close()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)


@pytest.mark.asyncio
async def test_metadata_worker_reads_real_installation_without_replacing_runtime(local_agent):
    metadata = local_agent.metadata
    try:
        observed = await metadata.refresh()
        assert observed["build"]["runtime"] == local_agent.build.loaded
        assert observed["build"]["observation_state"] == "fresh", metadata.observation()
        assert observed["management"]["layout"] == "external"
        assert metadata._worker is None
    finally:
        await metadata.close()


@pytest.mark.asyncio
async def test_metadata_states_preserve_identity_and_do_not_churn_wire_timestamps():
    metadata = sampler()

    async def sample():
        return {
            "build": metadata.build.describe(),
            "management": metadata.lifecycle.describe(),
            "device_actions": ["agent_restart", "agent_uninstall", "agent_update"],
        }

    metadata._read = sample
    clock = [10.0]
    metadata.clock = lambda: clock[0]
    try:
        initial = metadata.snapshot()
        assert initial["build"]["observation_state"] == "pending"
        assert initial["build"]["source_verified"] is False
        first = await metadata.refresh()
        assert first["build"]["observation_state"] == "fresh"
        assert first["device_actions"] == ["agent_restart", "agent_uninstall", "agent_update"]
        clock[0] += 1
        second = await metadata.refresh()
        assert first == second
        assert not {"checked_at", "sample_started_at", "age_seconds"} & set(second["build"])
        second["build"]["runtime"]["instance_id"] = "caller-mutated"
        assert metadata.snapshot()["build"]["runtime"]["instance_id"] == "f" * 32
        clock[0] += metadata.stale_after + 1
        stale = metadata.snapshot()
        assert stale["build"]["observation_state"] == "stale"
        assert stale["build"]["source_verified"] is False and stale["device_actions"] == []
        assert stale["build"]["disk"] == first["build"]["disk"]
        metadata.build.value["disk"]["source_sha256"] = "b" * 64
        metadata.build.value["restart_required"] = True
        changed = await metadata.refresh()
        assert changed["build"]["source_verified"] is True
        assert changed["build"]["restart_required"] is True
        assert changed["build"]["runtime"] == first["build"]["runtime"]

        def fail():
            raise OSError("synthetic-private-filesystem-detail")

        metadata.lifecycle.describe = fail
        failed = await metadata.refresh()
        assert failed["build"]["observation_state"] == "failed"
        assert failed["build"]["source_verified"] is False
        assert failed["build"]["restart_required"] is True
        assert failed["build"]["disk"] == changed["build"]["disk"]
        assert "synthetic-private" not in repr(failed)
        assert metadata.observation()["error_type"] == "OSError"
    finally:
        await metadata.close()


@pytest.mark.asyncio
async def test_heartbeat_reads_memory_and_never_awaits_outbox(local_agent, monkeypatch):
    instance = local_agent
    packets = []

    def forbidden(*args):
        raise AssertionError("Heartbeat accessed synchronous metadata")

    async def forbidden_outbox(*args, **kwargs):
        raise AssertionError("Heartbeat awaited the outbox")

    async def send(body):
        packets.append(body)
        if len(packets) >= 3:
            instance.stop_event.set()
        return True

    monkeypatch.setattr(instance.build, "describe", forbidden)
    monkeypatch.setattr(instance.lifecycle, "describe", forbidden)
    monkeypatch.setattr(instance.lifecycle, "actions", forbidden)
    monkeypatch.setattr(instance, "report_outbox", forbidden_outbox)
    monkeypatch.setattr(instance, "send", send)
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    await asyncio.wait_for(instance.heartbeat(), 1)
    assert len(packets) == 3
    assert all(packet["build"]["observation_state"] == "pending" for packet in packets)


class Socket:
    def __init__(self):
        self.packets = []
        self.closes = 0

    async def send(self, body):
        self.packets.append(body)

    async def close(self):
        self.closes += 1


class Channel:
    def __init__(self):
        self.packed = []

    def pack(self, body):
        self.packed.append(body)
        return body


@pytest.mark.asyncio
async def test_expected_socket_is_checked_after_send_lock_and_before_pack(local_agent):
    instance = local_agent
    old, new = Socket(), Socket()
    old_channel, new_channel = Channel(), Channel()
    instance.socket, instance.channel = old, old_channel
    await instance.send_lock.acquire()
    sending = asyncio.create_task(
        instance.send({"type": "result", "id": "original"}, expected_socket=old)
    )
    try:
        await eventually(instance.send_lock.control_waiting)
        instance.socket, instance.channel = new, new_channel
    finally:
        instance.send_lock.release()
    assert await sending is False
    assert not old.packets and not new.packets
    assert not old_channel.packed and not new_channel.packed


@pytest.mark.asyncio
async def test_late_outbox_failure_only_closes_old_socket_and_worker_continues(local_agent, caplog):
    instance = local_agent
    old, new = Socket(), Socket()
    entered, release, retried = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def outbox():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            raise sqlite3.OperationalError("synthetic-private-result")
        retried.set()
        return []

    instance.journal.outbox = outbox
    instance.socket, instance.channel = old, Channel()
    task = asyncio.create_task(instance.outbox_sender())
    try:
        await eventually(entered.is_set)
        instance.socket, instance.channel = new, Channel()
        instance._outbox_wake.set()
        release.set()
        await eventually(retried.is_set)
        assert old.closes == 1 and new.closes == 0
        assert not task.done()
        assert "OperationalError" in caplog.text
        assert "synthetic-private-result" not in caplog.text
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_outbox_settles_real_read_before_returning(local_agent):
    instance = local_agent
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read():
        entered.set()
        assert release.wait(3)
        finished.set()
        return []

    instance.journal.outbox = read
    instance.socket, instance.channel = Socket(), Channel()
    task = asyncio.create_task(instance.outbox_sender())
    try:
        await eventually(entered.is_set)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set()


@pytest.mark.parametrize("phase", ["queued", "packing"])
def test_crypto_worker_timeout_releases_gate_and_fences_late_packet(
    local_agent, monkeypatch, phase
):
    """Use the actual asyncio default executor and real encrypted channels."""
    instance = local_agent
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    secret = "synthetic-crypto-isolation-secret-0123456789"
    old, new = Socket(), Socket()
    old_channel = SecureChannel(secret, "a" * 40, "fixture", "agent")
    new_channel = SecureChannel(secret, "b" * 40, "fixture", "agent")
    new_hub = SecureChannel(secret, "b" * 40, "fixture", "hub")
    original = old_channel._pack_sequence

    def blocking_work():
        entered.set()
        assert release.wait(3)
        finished.set()

    def blocked_pack(body, sequence):
        entered.set()
        assert release.wait(3)
        try:
            return original(body, sequence)
        finally:
            finished.set()

    async def scenario():
        loop = asyncio.get_running_loop()
        # This isolated asyncio.run owns and drains its default executor.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupying = None
        if phase == "queued":
            occupying = loop.run_in_executor(None, blocking_work)
            await eventually(entered.is_set)
        else:
            monkeypatch.setattr(old_channel, "_pack_sequence", blocked_pack)
        instance.socket, instance.channel = old, old_channel
        sending = asyncio.create_task(
            instance.send(
                {"type": "result", "id": "original", "result": {"data": "x" * (128 * 1024)}},
                expected_socket=old,
            )
        )
        try:
            await eventually(entered.is_set)
            assert await asyncio.wait_for(sending, 1) is False
            assert old.closes == 1 and not old.packets
            assert not instance.send_lock.locked()
            # The old worker is still blocked. A small current-generation
            # heartbeat must not depend on that worker or its discarded key.
            instance.socket, instance.channel = new, new_channel
            assert await asyncio.wait_for(
                instance.send({"type": "heartbeat"}, expected_socket=new), 1
            )
            assert new_hub.unpack(new.packets[0]) == {"type": "heartbeat"}
            assert not finished.is_set()
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)
            if occupying is not None:
                await occupying
            await eventually(finished.is_set)
        assert not old.packets and len(new.packets) == 1
        assert old_channel.sent == 0 and new_channel.sent == 1

    monkeypatch.setattr(runner, "PACK_TIMEOUT_SECONDS", 0.05)
    asyncio.run(scenario())
