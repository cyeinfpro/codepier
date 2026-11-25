"""Isolated acceptance regressions for Agent audit issues AR05/07/08/10."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import knowledge_refresh as refresh, lsp_navigation as lsp
from agent.core_files import CoreFiles
from agent.filesystem import FileEngine
from agent.journal import Journal
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    journal = Journal(tmp_path / "state")
    config = {"allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}]}
    engine = FileEngine(config, journal, tmp_path / "private" / "config.json")
    project = {"id": "fixture", "root": str(root), "mode": "write", "allow_tasks": True,
               "_coding_owner": "synthetic-principal", "_coding_device": "fixture-device"}
    yield engine, project, root
    journal.db.close()
    with refresh._LOCK:
        cache_keys = {key for cache in (refresh._CACHES, refresh._BUILDS) for key in cache
                      if key[0] == str(journal.directory)}
        for cache_key in cache_keys:
            refresh._CACHES.pop(cache_key, None)
            refresh._BUILDS.pop(cache_key, None)


@pytest.mark.asyncio
async def test_ar05_only_uri_history_controls_unversioned_diagnostics():
    peer = lsp.Peer(SimpleNamespace(), "fixture", {}, Path("."), {})

    async def notify(*_args):
        return None

    peer.notify = notify

    async def sync(uri, sha="source-a"):
        return await peer.sync_document(uri=uri, source="pass", sha=sha, language_id="python")

    async def publish(uri, expected=None):
        report = {"uri": uri, "diagnostics": []}
        if expected is not None:
            report["version"] = expected
        peer._accept_diagnostics(report)

    first_version = await sync("file:///first")
    for i in range(70):
        uri = f"file:///fixture/{i}"
        version = await sync(uri)
        await publish(uri)
        assert await peer.wait_diagnostics(uri, version, 0) is not None
    reopened = await sync("file:///first")
    await publish("file:///first")
    await publish("file:///first", first_version)
    assert await peer.wait_diagnostics("file:///first", reopened, 0) is None
    await publish("file:///first", reopened)
    assert await peer.wait_diagnostics("file:///first", reopened, 0) is not None
    edited = await sync("file:///first", "source-b")
    await publish("file:///first")
    await publish("file:///first", reopened)
    assert await peer.wait_diagnostics("file:///first", edited, 0) is None
    await publish("file:///first", edited)
    assert await peer.wait_diagnostics("file:///first", edited, 0) is not None


@pytest.mark.asyncio
async def test_ar05_bounded_history_retires_peer_before_a_new_epoch(monkeypatch):
    monkeypatch.setattr(lsp, "MAX_TRACKED_DOCUMENT_URIS", 2)
    pool = lsp.LspPool()
    entry = lsp._PoolEntry((".", "python", 0, 0), "fixture", lambda: 0)
    events = []

    async def close():
        events.append("closed")

    entry.peer = SimpleNamespace(opened_uris={"file:///a", "file:///b"}, is_alive=lambda: True, close=close)

    async def spawn(entry, *_args):
        events.append("spawned")
        entry.peer = SimpleNamespace(opened_uris=set())

    monkeypatch.setattr(pool, "_spawn_peer", spawn)
    reused = await pool._ensure_query_peer(entry, {}, "fixture", {}, Path("."), {}, "fixture")
    assert reused is False and events == ["closed", "spawned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_ar08_slow_preflight_keeps_loop_responsive_and_never_starts_after_revocation(
    workspace, monkeypatch, cancel,
):
    engine, project, root = workspace
    (root / "source.py").write_text("def fixture(): pass\n")
    engine.config["integrations"] = {"language_servers": {
        "python": {"command": ["inert-fixture"], "projects": ["*"], "timeout_seconds": 5},
    }}
    agent = SimpleNamespace(config=engine.config, engine=engine)
    started = threading.Event()
    release = threading.Event()
    original = engine.read_bytes
    executed = []

    def slow_read(*args, **kwargs):
        started.set()
        assert release.wait(5), "controlled test disk did not complete"
        return original(*args, **kwargs)

    class InertPool:
        async def run_query(self, *_args, admission=None):
            await admission()
            executed.append(True)

    monkeypatch.setattr(engine, "read_bytes", slow_read)
    task = asyncio.create_task(lsp.query(
        agent, "fixture", project, {"action": "symbols", "path": "source.py"}, pool=InertPool(),
    ))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        timer = asyncio.Event()
        asyncio.get_running_loop().call_later(0.01, timer.set)
        # A real blocked worker still owns the file when the asyncio timer fires.
        await asyncio.wait_for(timer.wait(), 0.4)
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancel must settle the owning worker before returning"
        else:
            engine.config["allowed_roots"][0]["allow_tasks"] = False
    finally:
        release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(DevError) as denied:
            await task
        assert denied.value.code == "TASKS_DISABLED"
    assert not executed


def _build(engine, project, **args):
    result = refresh.build(engine, project, {"batch_size": 2, **args})
    batches = 1
    while result["state"] == "building":
        opts = dict(result["continuation"])
        opts.pop("idempotency_key", None)
        result = refresh.build(engine, project, opts)
        batches += 1
        assert batches < 20
    return result, batches


def test_ar07_continuations_preserve_all_signatures_and_restart_discards_incomplete_cache(
    workspace, monkeypatch,
):
    engine, project, root = workspace
    for i in range(7):
        (root / f"f{i}.py").write_text(f"def fixture_{i}(): pass\n")
    original = engine.read_bytes
    reads = []

    def read(path, **kwargs):
        reads.append(path.name)
        return original(path, **kwargs)

    monkeypatch.setattr(engine, "read_bytes", read)
    initial, batches = _build(engine, project)
    assert initial["entries_count"] == 7 and batches == 4 and len(reads) == 7
    reads.clear()
    for _ in range(2):
        stable, batches = _build(engine, project, incremental=True)
        assert stable["refresh"]["metadata_reused"] == 7 and batches == 4 and not reads

    (root / "f0.py").write_text("def updated(): pass\n")
    (root / "f1.py").rename(root / "moved.py")
    (root / "f6.py").unlink()
    changed, _ = _build(engine, project, incremental=True)
    assert changed["entries_count"] == 6
    assert changed["refresh"]["metadata_reused"] == 4
    assert sorted(reads) == ["f0.py", "moved.py"]
    reads.clear()

    # Simulate process restart during a durable rebuild. It may finish, but it
    # must not publish a partial hint set based only on its later batches.
    pending = refresh.build(engine, project, {"incremental": True, "batch_size": 2})
    assert pending["state"] == "building"
    with refresh._LOCK:
        key = next(key for key in refresh._BUILDS if key[0] == str(engine.journal.directory))
        refresh._CACHES.pop(key, None)
        refresh._BUILDS.pop(key)
    opts = dict(pending["continuation"])
    opts.pop("idempotency_key", None)
    resumed, _ = _build(engine, project, **opts)
    assert resumed["state"] == "ready" and resumed["refresh"]["mode"] == "full"
    assert key not in refresh._CACHES
    reads.clear()
    restored, _ = _build(engine, project, incremental=True)
    assert restored["refresh"]["mode"] == "full" and len(reads) == 6


def test_ar10_streamed_paging_retains_splitlines_exact_utf8_and_current_sha(workspace):
    engine, project, root = workspace
    # 64 KiB boundary falls on CRLF, then a Unicode separator split across chunks.
    data = ("x" * 65535 + "\r\n" + "y" * 65534 + "\u2028" + "\U0001f319\u0085"
            + "a\v\f\x1c\x1d\x1e\u2029\r\nb\rc\nlast").encode()
    path = root / "data.txt"
    path.write_bytes(data)
    reader = CoreFiles(engine)
    reader.output_bytes = 80 * 1024
    args = {"path": "data.txt", "offset": 1, "limit": 3, "expected_sha256": ""}
    output = []
    expected_lines = data.decode().splitlines(keepends=True)
    while True:
        part = reader.read_one(root, args)
        assert part["total_lines"] == len(expected_lines)
        assert part["sha256"] == hashlib.sha256(data).hexdigest()
        output.append(part["content"])
        if part["next_offset"] is None:
            break
        args.update(offset=part["next_offset"], expected_sha256=part["sha256"])
    assert "".join(output).encode() == data
    before = path.stat()
    path.write_bytes(b"q" + data[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(DevError) as stale:
        reader.read_one(root, {**args, "offset": 1})
    assert stale.value.code == "SHA_CONFLICT"


def test_ar10_pinned_many_pages_read_source_only_on_creation(workspace, monkeypatch):
    engine, project, root = workspace
    data = b"line" + b"\n".join([b"x" * 16] * 16384) + b"\n"
    (root / "data.txt").write_bytes(data)
    reader = CoreFiles(engine)
    before = engine.read_chunks
    counts = []

    def count(path, consume, **kwargs):
        size = before(path, consume, **kwargs)
        counts.append(size)
        return size

    monkeypatch.setattr(engine, "read_chunks", count)
    args = {"path": "data.txt", "offset": 1, "limit": 512, "expected_sha256": "",
            "options": {"read_snapshot": "create", "include_knowledge": False}}
    pages = []
    sha = hashlib.sha256(data).hexdigest()
    while True:
        result = reader.call("read", project, args)
        assert result["sha256"] == sha
        pages.append(result["content"])
        if result["next_offset"] is None:
            break
        args["offset"] = result["next_offset"]
        args["options"]["read_snapshot"] = result["read_snapshot"]
    assert len(pages) == 32 and counts == [len(data)] and "".join(pages).encode() == data

    args = {"path": "data.txt", "offset": 1, "limit": 512, "expected_sha256": sha}
    tracemalloc.start()
    try:
        assert reader.read_one(root, args)["total_lines"] == 16384
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 400_000, peak


def test_patch_case_detection_uses_entries_not_symlink_targets(monkeypatch):
    from types import SimpleNamespace

    from agent import batch_patch

    inode = SimpleNamespace(st_dev=4, st_ino=9)

    class Entry:
        def lstat(self):
            return inode

    class CaseFoldedDirectory:
        def is_dir(self):
            return True

        def __truediv__(self, name):
            assert name in {"Dangling", "dANGLING"}
            return Entry()

    monkeypatch.setattr(batch_patch.os, "listdir", lambda _parent: ["Dangling"])
    assert batch_patch._case_sensitive_directory(CaseFoldedDirectory()) is False
