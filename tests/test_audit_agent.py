"""Agent lifecycle fault injection, using only temporary directories/children."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.runner as runner
from agent.config import validate_config
from agent.journal import Journal
from agent.runner import Agent
from shared.crypto import SecureChannel, token
from shared.instance_lock import InstanceLock
from shared.util import atomic_json


@pytest.fixture
def local_agent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "config.json"
    atomic_json(path, {"hub_url": "http://127.0.0.1:9", "device_id": "fixture", "secret": token(),
                      "state_dir": str(tmp_path / "state"),
                      "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}], "tasks": {}})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


def request(root, id="operation"):
    return {"id": id, "tool": "fs_write", "project": {"root": str(root), "alias": "fixture", "mode": "write"},
            "args": {"project": "fixture", "path": "created.txt", "content": "once", "expected_sha256": "new",
                     "idempotency_key": "audit-operation"}}


async def eventually(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process group regression")
async def test_leader_exit_with_inherited_stdout_does_not_wait_task_timeout(local_agent):
    agent, root = local_agent
    agent.journal.start("descendant", {"tool": "tasks_run"})
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('leader-done')"
    result = await asyncio.wait_for(agent.run_process("descendant", [sys.executable, "-c", code], root, 25), 6)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert "leader-done" in result["output"] and not agent.processes


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process group regression")
async def test_completed_task_does_not_leave_background_children(local_agent):
    agent, root = local_agent
    agent.journal.start("background", {"tool": "tasks_run"})
    child = "import time,pathlib; time.sleep(.7); pathlib.Path('orphan-finished').write_text('unexpected')"
    code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    result = await agent.run_process("background", [sys.executable, "-c", code], root, 5)
    assert result["exit_code"] == 0
    await asyncio.sleep(.85)
    assert not (root / "orphan-finished").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["status", "update_output"])
async def test_storage_failure_reaps_child_and_process_registry(local_agent, monkeypatch, fault):
    agent, root = local_agent
    agent.journal.start("disk-full", {"tool": "tasks_run"})
    created = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        created.append(process)
        return process

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected disk full")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(agent.journal, fault, fail)
    with pytest.raises(sqlite3.OperationalError, match="injected disk full"):
        await asyncio.wait_for(agent.run_process("disk-full", [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"], root, 25, stream=True), 6)
    assert created and created[0].returncode is not None
    assert not agent.processes


@pytest.mark.asyncio
async def test_cancellation_during_spawn_retains_handle_until_child_reaped(local_agent, monkeypatch):
    agent, root = local_agent
    agent.journal.start("spawn", {"tool": "tasks_run"})
    created = []
    release = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        created.append(process)
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    operation = asyncio.create_task(agent.run_process("spawn", [sys.executable, "-c", "import time; time.sleep(30)"], root, 25))
    await eventually(lambda: created)
    operation.cancel()
    await asyncio.sleep(.01)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 6)
    assert created[0].returncode is not None and not agent.processes


@pytest.mark.asyncio
async def test_accepted_cancel_does_not_wait_for_project_lock(local_agent):
    agent, root = local_agent
    lock = asyncio.Lock()
    await lock.acquire()
    agent.project_locks[str(root)] = lock
    job = asyncio.create_task(agent.handle(request(root)))
    agent.jobs["operation"] = job
    try:
        await eventually(lambda: agent.journal.status("operation")["status"] == "accepted")
        agent.cancel_call("operation")
        await asyncio.wait_for(job, .5)
        assert agent.journal.status("operation")["result"]["error"]["code"] == "CANCELLED"
        assert not (root / "created.txt").exists()
    finally:
        lock.release()
        await job


@pytest.mark.asyncio
async def test_cancel_while_started_packet_sends_prevents_write(local_agent):
    agent, root = local_agent
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(packet):
        if packet["type"] == "started":
            started.set()
            await release.wait()
        return True

    agent.send = send
    job = asyncio.create_task(agent.handle(request(root)))
    agent.jobs["operation"] = job
    await started.wait()
    agent.cancel_call("operation")
    release.set()
    await job
    assert not (root / "created.txt").exists()
    assert agent.journal.status("operation")["result"]["error"]["code"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancelling_file_thread_preserves_project_lock_and_real_result(local_agent, monkeypatch):
    agent, root = local_agent
    entered, release = threading.Event(), threading.Event()
    original_call = agent.engine.call

    def delayed_call(*args):
        entered.set()
        assert release.wait(3)
        return original_call(*args)

    monkeypatch.setattr(agent.engine, "call", delayed_call)
    job = asyncio.create_task(agent.handle(request(root)))
    try:
        await eventually(entered.is_set)
        job.cancel()
        await asyncio.sleep(.02)
        assert not job.done() and agent.project_locks[str(root)].locked()
    finally:
        release.set()
        await job
    assert agent.journal.status("operation")["result"]["ok"]
    assert (root / "created.txt").read_text() == "once"


@pytest.mark.asyncio
async def test_stop_interrupts_connection_handshake(local_agent):
    agent, _ = local_agent
    entered = asyncio.Event()

    async def connect():
        entered.set()
        await asyncio.Event().wait()

    agent.connect_once = connect
    running = asyncio.create_task(agent.run())
    await entered.wait()
    await agent.stop()
    await asyncio.wait_for(running, .5)
    assert agent.instance_lock.file.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "deep-json"])
async def test_invalid_config_at_watcher_start_recovers(local_agent, monkeypatch, fault):
    agent, _ = local_agent
    new_config = copy.deepcopy(agent.config)
    new_config["name"] = "restored config"
    if fault == "missing":
        agent.config_path.unlink()
    else:
        agent.config_path.write_text('{"x":' + '[' * 10000 + '0' + ']' * 10000 + '}')
    sleep = asyncio.sleep

    async def quick_sleep(delay):
        await sleep(.01 if delay == 2 else delay)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
    watcher = asyncio.create_task(agent.watch_config())
    try:
        await sleep(.03)
        assert not watcher.done()
        atomic_json(agent.config_path, new_config)
        await eventually(lambda: agent.config.get("name") == "restored config")
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_send_invalidates_consumed_channel(local_agent):
    agent, _ = local_agent
    entered = asyncio.Event()

    class Socket:
        closed = False

        async def send(self, packet):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    socket = Socket()
    agent.socket = socket
    agent.channel = SecureChannel(token(), token(), "fixture", "agent")
    sending = asyncio.create_task(agent.send({"type": "heartbeat"}))
    await entered.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert socket.closed and agent.socket is None and agent.channel is None


@pytest.mark.asyncio
async def test_config_change_during_handshake_cannot_publish_stale_connection(local_agent, monkeypatch):
    agent, _ = local_agent
    challenge = token()
    hub_channel = SecureChannel(agent.config["secret"], challenge, "fixture", "hub")

    class Socket:
        receives = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, packet):
            assert hub_channel.unpack(packet)["type"] == "hello"

        async def recv(self):
            self.receives += 1
            if self.receives == 1:
                return json.dumps({"type": "challenge", "challenge": challenge})
            agent.config = {**agent.config, "hub_url": "http://127.0.0.1:10"}
            return hub_channel.pack({"type": "ready"})

        def __aiter__(self):
            raise AssertionError("stale connection entered reader")

    monkeypatch.setattr(runner.websockets, "connect", lambda *args, **kwargs: Socket())
    await agent.connect_once()
    assert agent.socket is None and agent.connection_number == 0


@pytest.mark.asyncio
async def test_malformed_request_gets_durable_failure(local_agent):
    agent, _ = local_agent
    await agent.handle({"id": "malformed", "tool": "fs_read"})
    assert agent.journal.status("malformed")["result"]["error"]["code"] == "INVALID_REQUEST"
    await agent.handle({"id": "malformed-tool", "tool": [], "project": {}, "args": {}})
    assert agent.journal.status("malformed-tool")["result"]["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_terminal_result_is_retried_after_transient_storage_failure(local_agent, monkeypatch):
    agent, root = local_agent
    original = agent.journal.finish
    attempts, sent = [], []

    def finish(id, result):
        attempts.append(copy.deepcopy(result))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("injected transient disk error")
        original(id, result)

    async def send(packet):
        sent.append(packet)
        return True

    monkeypatch.setattr(agent.journal, "finish", finish)
    agent.send = send
    await asyncio.wait_for(agent.handle(request(root)), 3)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert agent.journal.status("operation")["result"]["ok"]
    assert len(agent.journal.history(str(root), "created.txt")) == 1
    assert len([packet for packet in sent if packet["type"] == "result"]) == 1


@pytest.mark.asyncio
async def test_overlapping_projects_cannot_run_tasks_and_file_changes_together(local_agent):
    agent, root = local_agent
    child = root / "child"
    child.mkdir()
    parent_entered, child_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(id, *args):
        if id == "parent":
            parent_entered.set()
            await release.wait()
        else:
            child_entered.set()
        return {}

    agent.execute = execute
    parent_request = {"id": "parent", "tool": "tasks_run", "project": {"root": str(root), "alias": "fixture", "mode": "write"},
                      "args": {"project": "fixture", "task": "test", "idempotency_key": "audit-parent"}}
    parent_job = asyncio.create_task(agent.handle(parent_request))
    await parent_entered.wait()
    child_job = asyncio.create_task(agent.handle(request(child, "child")))
    try:
        await asyncio.sleep(.05)
        assert not child_entered.is_set()
    finally:
        release.set()
        await asyncio.gather(parent_job, child_job)
    assert child_entered.is_set() and not agent.active_roots


@pytest.mark.asyncio
async def test_read_slots_share_project_but_writer_excludes_reads(local_agent):
    agent, root = local_agent
    readers_ready = asyncio.Event()
    release_readers = asyncio.Event()
    entered = []

    async def reader(name):
        async with agent.project_slot(root, write=False):
            entered.append(name)
            if len(entered) == 2:
                readers_ready.set()
            await release_readers.wait()

    first = asyncio.create_task(reader("first"))
    second = asyncio.create_task(reader("second"))
    await asyncio.wait_for(readers_ready.wait(), 1)
    assert len(agent._active_slots) == 2

    writer_entered = asyncio.Event()

    async def writer():
        async with agent.project_slot(root, write=True):
            writer_entered.set()

    pending_writer = asyncio.create_task(writer())
    await asyncio.sleep(.05)
    assert not writer_entered.is_set()
    release_readers.set()
    await asyncio.gather(first, second)
    await asyncio.wait_for(writer_entered.wait(), 1)
    await pending_writer
    assert not agent.active_roots


@pytest.mark.parametrize("field,value", [
    ("state_dir", "relative-state"), ("name", "invalid\ud800"),
    ("device_id", "with space"), ("device_id", "encoded%2Fdevice"),
])
def test_invalid_config_cannot_replace_runtime_identity(local_agent, field, value):
    agent, _ = local_agent
    config = {**agent.config, field: value}
    with pytest.raises(ValueError):
        validate_config(config, agent.config_path)


@pytest.mark.parametrize("cwd", ["../outside", "/tmp", "folder\\child", "bad\x00path", ".ssh"])
def test_invalid_task_cwd_rejected_before_hot_reload(local_agent, cwd):
    agent, _ = local_agent
    config = {**agent.config, "tasks": {"test": {"command": ["python"], "cwd": cwd}}}
    with pytest.raises(ValueError, match="cwd"):
        validate_config(config, agent.config_path)


def test_init_does_not_persist_invalid_pairing(tmp_path):
    pairing = tmp_path / "pairing.json"
    pairing.write_text(json.dumps({"device_id": "fixture", "secret": "short", "hub_url": "http://localhost:9"}))
    config = tmp_path / "agent" / "config.json"
    result = subprocess.run([sys.executable, "-m", "agent", "--config", str(config), "init", "--pairing-file", str(pairing), "--allow", str(tmp_path)], capture_output=True, timeout=5)
    assert result.returncode != 0 and not config.exists()


def test_failed_agent_construction_releases_instance_lock(local_agent, monkeypatch, tmp_path):
    agent, _ = local_agent
    config = {**agent.config, "state_dir": str(tmp_path / "failed-state")}
    path = tmp_path / "failed-config.json"
    atomic_json(path, config)

    def fail(*args):
        raise sqlite3.OperationalError("cannot open journal")

    monkeypatch.setattr(runner, "Journal", fail)
    with pytest.raises(sqlite3.OperationalError):
        Agent(path)
    lock = InstanceLock(Path(config["state_dir"]) / ".agent.lock")
    lock.close()


def test_outbox_query_uses_pending_index(local_agent):
    agent, _ = local_agent
    plan = agent.journal.db.execute("EXPLAIN QUERY PLAN SELECT id,result FROM calls WHERE status IN ('done','interrupted') AND acked=0 ORDER BY at LIMIT 32").fetchall()
    assert any("calls_outbox" in row[3] for row in plan)
    assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_legacy_backup_schema_is_migrated_without_losing_history(tmp_path):
    state = tmp_path / "legacy-state"
    state.mkdir()
    with sqlite3.connect(state / "agent.sqlite3") as db:
        db.execute("CREATE TABLE backups (id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL, existed INTEGER NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL, at REAL NOT NULL)")
        db.execute("INSERT INTO backups VALUES ('old','/fixture','old.sh',0,'new','old-digest',1)")
    journal = Journal(state)
    try:
        assert journal.history("/fixture")[0]["before_mode"] is None
        journal.add_backup("new", "/fixture", "script.sh", b"script", "new-digest", before_mode=0o755)
        row, data = journal.backup("/fixture", "new")
        assert row["before_mode"] == 0o755 and data == b"script"
    finally:
        journal.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hang", "nonzero", "missing"])
async def test_windows_taskkill_is_bounded_and_has_direct_kill_fallback(monkeypatch, failure):
    class Process:
        pid = 12345
        returncode = None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                await asyncio.Event().wait()
            return self.returncode

    process, killer = Process(), Process()
    calls = []
    if failure == "nonzero":
        killer.returncode = 1

    async def spawn(*args, **kwargs):
        calls.append(args)
        if failure == "missing":
            raise FileNotFoundError("taskkill missing")
        return killer

    original_wait_for = asyncio.wait_for

    async def quick_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, min(timeout, .02))

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "wait_for", quick_wait_for)
    await Agent.kill_process(process)
    assert process.returncode is not None and calls
    if failure == "hang":
        assert killer.returncode is not None
    calls.clear()
    await Agent.kill_process(process, include_finished=True)
    assert not calls  # A finished Windows PID may have been reused.
