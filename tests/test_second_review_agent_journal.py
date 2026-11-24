"""Agent second-review regressions in disposable files and journal state."""

import asyncio
import sqlite3
import time
import uuid
from collections import deque
from contextlib import closing
import subprocess
import threading

import pytest

from agent.journal import Journal, MAX_INTERRUPTED_READS
from agent import workspace_probe
from shared.util import DevError
from tests.test_core_tools import agent, request


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_target", [False, True])
@pytest.mark.parametrize("deadline_case", ["expires_during_auth", "valid", "legacy_without_header"])
async def test_vps_handler_rechecks_start_deadline_before_dispatch(
    agent, monkeypatch, saved_target, deadline_case
):
    from types import SimpleNamespace

    instance, project, _ = agent
    instance.config["shell"].update(enabled=True, projects=[project["alias"]])
    monkeypatch.setattr("agent.project_runtime.selected", lambda *_: {"mode": "native_compat"})
    inline_call = request(
        project,
        "ssh_exec",
        host="fixture.invalid",
        username="fixture",
        password="fixture-only-not-used",
        command="true",
        timeout_seconds=10,
        idempotency_key=uuid.uuid4().hex,
    )
    call = inline_call
    if saved_target:
        call = request(
            project,
            "exec",
            target="vps:" + uuid.uuid4().hex,
            command="true",
            timeout_seconds=10,
            idempotency_key=uuid.uuid4().hex,
        )
        # Simulate the already authenticated Hub delivery, not public arguments.
        call["core_ssh"] = inline_call["args"]
    deadline = time.time() + 600
    if deadline_case != "legacy_without_header":
        call["not_after"] = deadline

    connected = []
    dispatched = []

    async def fake_transport(**kwargs):
        connected.append(kwargs["operation_id"])
        assert instance.journal.durable_status(call["id"]) == "running"
        after_auth = kwargs.get("require_authority")
        if deadline_case == "expires_during_auth":
            # Advance only the runner's wall clock while the authenticated
            # transport is about to send. The original journal keeps real time.
            with monkeypatch.context() as clock:
                clock.setattr(
                    "agent.runner.time",
                    SimpleNamespace(time=lambda: deadline + 1),
                )
                if after_auth is not None:
                    after_auth()
        elif after_auth is not None:
            after_auth()
        dispatched.append(kwargs["operation_id"])
        return {"exit_code": 0, "output": "", "transport_runtime": "asyncssh"}

    monkeypatch.setattr("agent.vps_exec.execute_vps_command", fake_transport)
    await instance.handle(call)
    receipt = instance.journal.status(call["id"])
    assert receipt["status"] == "done"
    assert connected == [call["id"]]
    if deadline_case == "expires_during_auth":
        assert not dispatched, "remote command entered send window after the start deadline"
        assert receipt["result"]["error"]["code"] == "QUEUE_EXPIRED"
    else:
        assert receipt["result"]["ok"]
        runtime = receipt["result"]["data"]["transport_runtime"]
        assert runtime["engine"] == "asyncssh"
        assert runtime["mode"] == "native_ssh_transport"
        assert runtime.get("target") == (call["args"]["target"] if saved_target else None)
        assert runtime["grants_authority"] is False
        assert dispatched == [call["id"]]

    # A repeated delivery returns the saved receipt without a second connection.
    await instance.handle(call)
    assert instance.journal.status(call["id"])["result"] == receipt["result"]
    assert connected == [call["id"]]


@pytest.mark.asyncio
async def test_real_write_is_not_replayed_from_durable_start_receipt(agent, tmp_path, monkeypatch):
    instance, project, root = agent
    operation = request(
        project,
        "fs_write",
        path="once.txt",
        content="actual file write",
        expected_sha256="new",
        idempotency_key=uuid.uuid4().hex,
    )
    identifier = operation["id"]
    restart = tmp_path / "journal-restart"
    restart.mkdir()
    original_send = instance.send
    frames = []

    async def send(message):
        frames.append(message)
        if message.get("type") == "started" and message.get("id") == identifier:
            assert not (root / "once.txt").exists()
            assert instance.journal.fast_status(identifier)["status"] == "running"
            with (
                instance.journal.lock,
                closing(sqlite3.connect(restart / "agent.sqlite3")) as clone,
            ):
                instance.journal.db.backup(clone)
        return await original_send(message)

    monkeypatch.setattr(instance, "send", send)
    await instance.handle(operation)
    assert (root / "once.txt").read_text() == "actual file write"
    known_good = instance.journal
    recovered = Journal(restart)
    try:
        instance.journal = recovered
        (root / "once.txt").write_text("preserve newer bytes")
        await instance.handle(operation)
        assert recovered.status(identifier)["result"]["error"]["code"] == "INTERRUPTED"
        assert frames[-1]["type"] == "result"
        assert frames[-1]["result"]["error"]["code"] == "INTERRUPTED"
        assert (root / "once.txt").read_text() == "preserve newer bytes"
    finally:
        instance.journal = known_good
        recovered.db.close()
    if instance._journal_persist_task is not None:
        await asyncio.wait_for(instance._journal_persist_task, 3)


@pytest.mark.asyncio
async def test_persist_retries_failed_suffix_in_order_and_survives_reopen(agent, monkeypatch):
    instance, _, _ = agent
    actual = instance.journal.cancel_db
    first, retry, last = (uuid.uuid4().hex for _ in range(3))
    seen = []

    def cancel(identifier, at):
        seen.append(identifier)
        if identifier == retry and seen.count(retry) == 1:
            raise sqlite3.OperationalError("injected transient test failure")
        actual(identifier, at)

    monkeypatch.setattr(instance.journal, "cancel_db", cancel)
    for identifier in (first, retry, last):
        instance.cancel_call(identifier)
    await asyncio.wait_for(instance._journal_persist_task, 4)
    assert seen == [first, retry, retry, last]
    assert instance._journal_persist_queue == deque()
    with closing(Journal(instance.journal.directory).db) as recovered:
        assert {r[0] for r in recovered.execute("SELECT id FROM cancellations")} == {
            first,
            retry,
            last,
        }


def test_prune_does_not_forget_pending_memory_fence(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal")
    try:
        original_time = time.time()
        monkeypatch.setattr("agent.journal.time.time", lambda: original_time - 40 * 86400)
        journal.start("retired", {"tool": "write"})
        journal.cancel("retired")
        journal.finish("retired", {"ok": True})
        journal.ack("retired")
        journal.cancel_memory("awaiting-disk")
        assert journal.prune(now=original_time)["cancellations"] == 1
        assert not journal.is_cancelled("retired")
        assert journal.is_cancelled("awaiting-disk")
    finally:
        journal.db.close()


@pytest.mark.asyncio
async def test_recovered_reader_commits_each_restart_attempt(agent):
    instance, project, _ = agent
    known_good = instance.journal
    identifier = uuid.uuid4().hex
    payload = {"tool": "read", "project": project, "args": {"path": "file.txt"}}
    with known_good.lock, known_good.db:
        known_good.start(identifier, payload)
    try:
        for _ in range(MAX_INTERRUPTED_READS):
            instance.journal = Journal(known_good.directory)
            assert instance.journal.start(identifier, payload) is None
            await instance._begin_operation(identifier, "read", payload["args"])
            assert instance.journal.fast_status(identifier)["status"] == "running"
            assert instance.journal.durable_status(identifier) == "running"
            instance.journal.db.close()
        instance.journal = Journal(known_good.directory)
        assert instance.journal.start(identifier, payload)["error"]["code"] == "AGENT_RESTART_LIMIT"
        instance.journal.db.close()
    finally:
        instance.journal = known_good


@pytest.mark.asyncio
async def test_expired_read_root_does_not_kill_healthy_pooled_worker(agent):
    instance, project, root = agent
    (root / "probe.txt").write_text("ok")
    pool = workspace_probe.ReadPool()
    args = {"path": "probe.txt"}
    try:
        with pytest.raises(DevError) as err:
            async with workspace_probe.ReadWorker(admission=True, pool=pool) as worker:
                await workspace_probe.probe(
                    instance.engine, "read", project, args, phase="admission", worker=worker
                )
                process = worker.process
                await instance._operation_root(
                    "read",
                    project,
                    args,
                    worker=worker,
                    expected_root=root,
                    deadline=time.time() - 1,
                )
        assert err.value.code == "QUEUE_EXPIRED"
        assert process.returncode is None
        assert pool.idle[0][0] is process
    finally:
        await pool.close()


def test_search_scan_budget_and_page_cursors_are_relative_to_current_anchor(agent, monkeypatch):
    instance, project, root = agent
    first = root / "a.txt"
    first.write_text("needle\nneedle\n")
    second = root / "b.txt"
    second.write_text("needle\n")
    actual = instance.engine._search_entries

    def cap_after_first(*a, **kw):
        entries, scope, _capped, cursor = actual(*a, **kw)
        return list(entries)[:1], scope, True, cursor

    monkeypatch.setattr(instance.engine, "_search_entries", cap_after_first)
    first_page = instance.engine.search(project, {"query": "needle", "offset": 2})
    assert first_page["scan_status"] == "scan_limited"
    assert first_page["resume_after_path"] == "a.txt"
    assert first_page["next_offset"] == 0
    monkeypatch.setattr(instance.engine, "_search_entries", actual)
    second_page = instance.engine.search(
        project,
        {
            "query": "needle",
            "resume_after_path": first_page["resume_after_path"],
            "resume_scan_scope": first_page["scan_scope"],
            "offset": first_page["next_offset"],
        },
    )
    assert [m["path"] for m in second_page["matches"]] == ["b.txt"]


@pytest.mark.asyncio
async def test_pending_revocation_frame_waits_for_durable_fence(agent, monkeypatch):
    instance, _, _ = agent
    identifier = uuid.uuid4().hex
    actual = instance.journal.cancel_db
    entered = threading.Event()
    release = threading.Event()

    def cancel(*args):
        entered.set()
        if not release.wait(2):
            raise RuntimeError("missing test release")
        actual(*args)

    monkeypatch.setattr(instance.journal, "cancel_db", cancel)
    pending = asyncio.create_task(instance._cancel_pending_frame(identifier))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert instance.journal.is_cancelled(identifier)
        assert not pending.done()
    finally:
        release.set()
        await asyncio.wait_for(pending, 3)
    with closing(Journal(instance.journal.directory).db) as reopened:
        assert reopened.execute("SELECT id FROM cancellations WHERE id=?", (identifier,)).fetchone()


def test_cold_terminal_not_revived_by_synchronous_cancel_pending(agent):
    instance, project, _ = agent
    identifier = uuid.uuid4().hex
    instance.journal.start(identifier, {"tool": "read", "project": project})
    instance.journal.finish(identifier, {"ok": True})
    instance.journal.forget_retired_terminals([identifier])
    assert instance.journal.fast_status(identifier)["status"] == "missing"
    assert not instance.cancel_pending_call(identifier)
    assert not instance.journal.is_cancelled(identifier)


def test_git_tree_depth_none_lists_tracked_leaf(agent):
    instance, project, root = agent
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "tracked.py").write_text("pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=5)
    subprocess.run(["git", "-C", str(root), "add", "one/two/tracked.py"], check=True, timeout=5)
    assert "one/two/tracked.py" in str(
        instance.engine.tree(project, {"depth": None, "scope": "git"})
    )
