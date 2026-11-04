"""Synthetic regression cases for audit P3/P4/P5/P6/P14."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

from agent.channel_binding import new_binding, verify_binding
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent.runner import Agent
from agent.runtime_config import _replace_config, read_json
from shared.contracts import TOOLS
from shared.crypto import SecureChannel
from shared.util import DevError


@pytest.fixture
def journal(tmp_path):
    value = Journal(tmp_path / "state")
    yield value
    value.db.close()


def test_replayed_ready_and_pinned_downgrade_rejected(journal):
    secret, challenge, device = "s" * 48, "c" * 48, "fixture"
    first = new_binding()
    hub = SecureChannel(secret, challenge, device, "hub")
    recorded = hub.pack({"type": "ready", "channel_binding": first})
    # A repeated Hub challenge alone passes AEAD; a new Agent nonce must not.
    replay = SecureChannel(secret, challenge, device, "agent").unpack(recorded)
    with pytest.raises(ValueError, match="replay"):
        verify_binding(journal, new_binding(), replay)
    with pytest.raises(ValueError, match="Upgrade Hub before Agent"):
        verify_binding(journal, first, {"type": "ready"})
    assert verify_binding(journal, first, replay)
    assert journal.meta("hub_channel_binding") == "1"
    with pytest.raises(ValueError, match="downgrade"):
        verify_binding(journal, new_binding(), {"type": "ready"})
    second = Journal(journal.directory)
    try:
        with pytest.raises(ValueError, match="downgrade"):
            verify_binding(second, new_binding(), {"type": "ready"})
    finally:
        second.db.close()


@pytest.mark.parametrize("binding", [{}, {"version": True, "agent_nonce": "x" * 64},
    {"version": 1, "agent_nonce": "中" * 64}, [], "legacy"])
def test_malformed_binding_is_not_legacy(journal, binding):
    with pytest.raises(ValueError):
        verify_binding(journal, new_binding(), {"channel_binding": binding})


def test_retention_compacts_acked_results_without_reexecuting_writes(journal):
    request = {"tool": "fs_write", "args": {"content": "synthetic"}}
    for identifier in ("acked", "unacked", "running"):
        journal.start(identifier, request)
        journal.mark_running(identifier)
        if identifier != "running":
            journal.finish(identifier, {"ok": True, "output": "x" * 131072})
    journal.ack("acked")
    with journal.db:
        journal.db.execute("UPDATE calls SET at=?", (time.time() - 40 * 86400,))
    counts = journal.prune()
    assert counts["calls"] == 1
    assert journal.status("acked")["result"]["error"]["code"] == "RESULT_EXPIRED"
    assert journal.start("acked", request)["error"]["code"] == "RESULT_EXPIRED"
    assert journal.status("unacked")["result"]["ok"]
    assert journal.status("running")["status"] == "running"
    assert journal.prune()["calls"] == 0
    with pytest.raises(DevError) as error:
        journal.start("acked", {"tool": "fs_write", "args": {}})
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_backup_and_checkpoint_quotas_preserve_newest_and_active_receipts(journal):
    identifiers = []
    for index in range(25):
        identifier = uuid.uuid4().hex
        identifiers.append(identifier)
        journal.add_backup(identifier, "fixture", "a.txt", b"before", "after")
        with journal.db:
            journal.db.execute("UPDATE backups SET at=? WHERE id=?", (time.time() - 25 + index, identifier))
    for index in range(12):
        path = journal.checkpoints / (uuid.uuid4().hex + ".zip")
        path.write_bytes(b"synthetic")
        os.utime(path, (time.time() - 12 + index,) * 2)
    journal.start("in-flight", {"tool": "fs_write"})
    assert journal.prune()["backups"] == 0
    journal.finish("in-flight", {"ok": True})
    assert journal.prune()["backups"] == 0
    journal.ack("in-flight")
    counts = journal.prune()
    assert counts["backups"] == 5 and counts["checkpoints"] == 2
    assert not (journal.backups / identifiers[0]).exists()
    assert (journal.backups / identifiers[-1]).exists()
    assert len(journal.history("fixture")) == 20


def test_private_config_aliases_and_legacy_backups_blocked(tmp_path, journal):
    root = tmp_path / "project"
    folder = root / "ops"
    folder.mkdir(parents=True)
    config = folder / "config.json"
    config.write_text("{}")
    engine = FileEngine({"allowed_roots": [{"path": str(root)}]}, journal, config)
    for name in ("config.json", "CONFIG.JSON", "config.json.runtime-backup-old",
                 "CONFIG.JSON.RUNTIME-temp"):
        with pytest.raises(DevError) as error:
            engine.path(root, "ops/" + name)
        assert error.value.code == "PROTECTED_PATH"
    assert engine.path(root, "ops/readme.txt") == folder / "readme.txt"


def test_new_config_backups_are_private_and_bounded(tmp_path):
    path = tmp_path / "config.json"
    state = tmp_path / "private-state"
    value = {"secret": "synthetic-only", "state_dir": str(state), "revision": 0}
    path.write_text(json.dumps(value))
    for revision in range(12):
        _, raw, info = read_json(path)
        value["revision"] = revision + 1
        ref = _replace_config(path, raw, info, json.dumps(value).encode(), hashlib.sha256(raw).hexdigest())
        assert (path.parent / ref).parent == state / "config-backups"
    assert len(list((state / "config-backups").iterdir())) == 10
    assert not list(tmp_path.glob("config.json.runtime-backup-*"))


def test_git_search_respects_ignores_and_guards(tmp_path, journal):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    (root / "generated" / "ignored.txt").write_text("needle")
    for directory in (".venv-review", ".work"):
        (root / directory).mkdir()
        (root / directory / "noise").write_text("needle")
    (root / "source.py").write_text("needle")
    (root / ".env").write_text("needle")
    engine = FileEngine({"allowed_roots": [{"path": str(root)}]}, journal, tmp_path / "private" / "config.json")
    args = TOOLS["fs_search"].model(project="fixture", query="needle").model_dump()
    result = engine.search({"root": str(root)}, args)
    assert result["scan_scope"] == "git_index"
    assert [row["path"] for row in result["matches"]] == ["source.py"]
    assert not result["truncated"]
    # Explicit content paths still pass the same protected-path checks.
    with pytest.raises(DevError):
        engine.path(root, ".venv-review/noise")


@pytest.mark.asyncio
async def test_slow_output_commit_does_not_block_loop_and_final_output_persists(journal, tmp_path, monkeypatch):
    agent = Agent.__new__(Agent)
    agent.journal = journal
    agent.processes = {}
    agent.cancelled = set()
    agent.stop_event = asyncio.Event()
    agent.output_wake = asyncio.Event()
    agent.pending_output = {}
    journal.start("stream", {"tool": "exec"})
    journal.mark_running("stream")
    loop_thread = threading.get_ident()
    original = journal.update_output
    writes = []
    def delayed(*args):
        assert threading.get_ident() != loop_thread
        time.sleep(.15)
        writes.append(args)
        original(*args)
    monkeypatch.setattr(journal, "update_output", delayed)
    ticks = 0
    async def pulse():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(.01)
    timer = asyncio.create_task(pulse())
    try:
        result = await agent._run_process_account("stream",
            [sys.executable, "-c", "import time; print('first',flush=True); time.sleep(.3); print('final',flush=True)"],
            tmp_path, 5, stream=True, environment_override=dict(os.environ))
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
    assert result["exit_code"] == 0
    assert ticks >= 10
    assert "final" in journal.status("stream")["output"]
    assert len(writes) == 2


def test_incremental_search_uses_git_and_hides_newly_private_cached_hits(tmp_path, journal):
    from agent.searches import Searches
    root = tmp_path / "indexed"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "noise").write_text("needle")
    (root / "source.py").write_text("needle")
    cfg = root / "ops" / "config.json"
    cfg.parent.mkdir()
    engine = FileEngine({"allowed_roots": [{"path": str(root)}]}, journal, cfg)
    project = {"root": str(root), "id": "fixture"}
    searches = Searches(engine)
    args = TOOLS["searches_start"].model(project="fixture", query="needle").model_dump()
    started = searches.start("search", project, args)
    assert started["scan_scope"] == "git_index"
    page_args = {"search_id": "search", "cursor": 0, "limit": 10}
    page = searches.get(project, page_args)
    assert [row["path"] for row in page["results"]] == ["source.py"]
    # Simulate a cache written before the configuration-backup policy upgrade.
    old = {"path": "ops/config.json.runtime-backup-old", "sha256": "0" * 64,
           "line": 1, "kind": "text", "snippet": "synthetic private value"}
    with journal.db:
        journal.db.execute("INSERT INTO search_hits VALUES (?,?,?)", ("search", 2, json.dumps(old)))
        journal.db.execute("UPDATE search_sessions SET result_count=2 WHERE id='search'")
    assert all(row["path"] == "source.py" for row in searches.get(project, page_args)["results"])


@pytest.mark.asyncio
async def test_status_and_outbox_reads_wait_off_loop(journal, monkeypatch):
    from types import SimpleNamespace
    agent = Agent.__new__(Agent)
    agent.journal = journal
    agent.pending_output = {}
    agent.output_wake = asyncio.Event()
    agent.telemetry = SimpleNamespace(snapshot=lambda identifier: [])
    sent = []
    async def send(packet):
        sent.append(packet)
        return True
    agent.send = send
    loop_thread = threading.get_ident()
    original = journal.status
    def slow_status(identifier):
        assert threading.get_ident() != loop_thread
        time.sleep(.05)
        return original(identifier)
    monkeypatch.setattr(journal, "status", slow_status)
    await agent.report_status("missing")
    def off_loop_outbox():
        assert threading.get_ident() != loop_thread
        return []
    monkeypatch.setattr(journal, "outbox", off_loop_outbox)
    await agent.report_outbox()
    assert sent[-1]["status"] == "missing"
