"""Regression tests for B5 (tree pagination), B13 (search truncation/cursor), and B15 (LspPool)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.filesystem as fs_mod
from agent import lsp_navigation
from agent.filesystem import FileEngine
from agent.runner import Agent
from shared.util import DevError


def resident_count(pool):
    """Live language-server peers currently held by a pool (test diagnostic)."""
    return sum(
        1 for entry in pool._entries.values() if entry.peer is not None and entry.peer.is_alive()
    )

FAKE_LSP_SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"


def _make_engine(tmp_path: Path, root: Path, *, writable: bool = False) -> FileEngine:
    return FileEngine(
        {"allowed_roots": [{"path": str(root), "writable": writable, "allow_tasks": True}]},
        SimpleNamespace(directory=tmp_path / "journal"),
        tmp_path / "agent-config.json",
    )


def _make_lsp_agent(
    tmp_path: Path,
    root: Path,
    marker: Path,
    *,
    mode: str = "normal",
    timeout_seconds: int = 5,
    pool: lsp_navigation.LspPool | None = None,
):
    config = {
        "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
        "integrations": {
            "language_servers": {
                "python": {
                    "enabled": True,
                    "projects": ["*"],
                    "command": [
                        sys.executable,
                        str(FAKE_LSP_SERVER),
                        "--marker",
                        str(marker),
                        "--mode",
                        mode,
                    ],
                    "timeout_seconds": timeout_seconds,
                }
            }
        },
    }
    engine = FileEngine(config, SimpleNamespace(directory=tmp_path / "journal"), tmp_path / "agent.json")
    agent = SimpleNamespace(
        config=config,
        engine=engine,
        processes={},
        kill_process=Agent.kill_process,
    )
    if pool is not None:
        agent.lsp_pool = pool
    return agent


def test_tree_depth1_paginates_past_per_dir_limit(tmp_path):
    root = tmp_path / "project"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for i in range(120):
        (docs / f"doc_{i:03d}.md").write_text(f"# doc {i}\n", encoding="utf-8")

    engine = _make_engine(tmp_path, root)
    project = {"root": str(root), "alias": "Fixture", "mode": "read"}

    collected = []
    offset = 0
    pages = 0
    while True:
        pages += 1
        res = engine.tree(
            project,
            {"path": "docs", "depth": 1, "offset": offset, "limit": 40, "per_dir": 40},
        )
        assert res["per_dir_capped"] is False
        assert res["next_hint"] is None
        assert "omitted" not in res
        assert not (res["truncated"] and res["next_offset"] is None)
        collected.extend(entry["path"] for entry in res["entries"])
        if not res["page_truncated"]:
            assert res["truncated"] is False
            assert res["next_offset"] is None
            break
        assert res["truncated"] is True
        assert res["next_offset"] == offset + len(res["entries"])
        offset = res["next_offset"]

    assert pages == 3
    assert len(collected) == 120
    assert len(set(collected)) == 120
    assert set(collected) == {f"docs/doc_{i:03d}.md" for i in range(120)}


def test_tree_depth_gt1_splits_per_dir_capped_and_page_truncated_with_next_hint(tmp_path):
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    for i in range(20):
        (root / "docs" / f"d_{i:02d}.md").write_text("doc\n", encoding="utf-8")
    for i in range(5):
        (root / "src" / f"s_{i:02d}.py").write_text("pass\n", encoding="utf-8")

    engine = _make_engine(tmp_path, root)
    project = {"root": str(root), "alias": "Fixture", "mode": "read"}

    capped = engine.tree(
        project,
        {"path": ".", "depth": 2, "offset": 0, "limit": 100, "per_dir": 5},
    )
    assert capped["per_dir_capped"] is True
    assert capped["page_truncated"] is False
    assert capped["truncated"] is True
    assert capped["next_offset"] is None
    assert capped["omitted"] == {"docs": 15}
    assert isinstance(capped["next_hint"], str)
    assert "depth=1" in capped["next_hint"]
    assert "docs" in capped["next_hint"]

    paged = engine.tree(
        project,
        {"path": ".", "depth": 2, "offset": 0, "limit": 5, "per_dir": 15},
    )
    assert paged["page_truncated"] is True
    assert paged["truncated"] is True
    assert paged["next_offset"] == 5

    # Verify git_index depth=1 pagination also bypasses per_dir=40 cleanly.
    for i in range(20, 60):
        (root / "docs" / f"d_{i:02d}.md").write_text("doc\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=CodePier Test",
            "-c",
            "user.email=codepier@example.invalid",
            "commit",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    git_paths = []
    offset = 0
    while True:
        page = engine.tree(project, {"path": "docs", "depth": 1, "offset": offset, "limit": 25})
        assert page["scan_scope"] == "git_index"
        assert page["per_dir_capped"] is False
        assert not (page["truncated"] and page["next_offset"] is None)
        git_paths.extend(e["path"] for e in page["entries"])
        if not page["page_truncated"]:
            assert page["truncated"] is False
            break
        offset = page["next_offset"]
    assert len(git_paths) == 60
    assert len(set(git_paths)) == 60


def test_search_skipped_binary_never_sets_truncated_or_phantom_next_offset(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\x00\x01\x02\x03" * 256)
    (root / "oversized.txt").write_bytes(b"a" * (fs_mod.MAX_FILE + 128))
    (root / "hits.py").write_text(
        "needle one\nother\nneedle two\nneedle three\n", encoding="utf-8"
    )

    engine = _make_engine(tmp_path, root)
    project = {"root": str(root), "alias": "Fixture", "mode": "read"}

    res = engine.search(
        project,
        {
            "path": ".",
            "query": "needle",
            "file_glob": "*",
            "case_sensitive": False,
            "offset": 0,
            "limit": 50,
        },
    )
    assert len(res["matches"]) == 3
    assert res["skipped_files"] == 2
    assert res["skipped_reasons"] == {"BINARY_FILE": 1, "FILE_TOO_LARGE": 1}
    assert res["scan_status"] == "matches"
    assert res["truncated"] is False
    assert res["next_offset"] is None
    assert res["resume_after_path"] is None

    # Trigger scan_limited (32 * MAX_FILE scan budget) and verify resume_after_path advances cleanly.
    big_dir = root / "big"
    big_dir.mkdir()
    for i in range(10):
        body = ("padding " * 120) + (f"\ntarget_hit_{i}\n" if i in (0, 7, 9) else "\n")
        (big_dir / f"f{i:02d}.txt").write_text(body, encoding="utf-8")

    monkeypatch.setattr(fs_mod, "MAX_FILE", 128)  # 32 * 128 = 4096 bytes scan budget
    collected = []
    cursor = None
    seen_cursors = set()
    for _ in range(15):
        page = engine.search(
            project,
            {
                "path": "big",
                "query": "target_hit_",
                "file_glob": "*",
                "case_sensitive": False,
                "offset": 0,
                "limit": 50,
                "resume_after_path": cursor,
            },
        )
        collected.extend(m["path"] for m in page["matches"])
        if page["scan_status"] == "scan_limited":
            assert page["truncated"] is True
            assert page["next_offset"] is None
            assert isinstance(page["resume_after_path"], str) and page["resume_after_path"]
            assert page["resume_after_path"] not in seen_cursors
            seen_cursors.add(page["resume_after_path"])
            cursor = page["resume_after_path"]
        else:
            assert page["truncated"] is False
            assert page["next_offset"] is None
            assert page["resume_after_path"] is None
            break
    else:
        raise AssertionError("search pagination with resume_after_path did not terminate")

    assert collected == ["big/f00.txt", "big/f07.txt", "big/f09.txt"]


def test_search_git_index_resume_after_path_and_page_limited(tmp_path, monkeypatch):
    root = tmp_path / "project"
    sub = root / "pkg"
    sub.mkdir(parents=True)
    for i in range(8):
        text = ("padding " * 120) + f"\nmatch_line\nmatch_line\n"
        (sub / f"m{i:02d}.py").write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=CodePier Test",
            "-c",
            "user.email=codepier@example.invalid",
            "commit",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    # Include an untracked file sorting before m00.py to verify unified sorted ordering.
    (sub / "a_extra.py").write_text(("padding " * 120) + "\nmatch_line\n", encoding="utf-8")

    engine = _make_engine(tmp_path, root)
    project = {"root": str(root), "alias": "Fixture", "mode": "read"}

    # Match pagination (page_limited) must still use next_offset.
    p1 = engine.search(
        project,
        {
            "path": "pkg",
            "query": "match_line",
            "file_glob": "*",
            "case_sensitive": False,
            "offset": 0,
            "limit": 3,
        },
    )
    assert p1["scan_scope"] == "git_index"
    assert p1["scan_status"] == "page_limited"
    assert p1["truncated"] is True
    assert p1["next_offset"] == 3
    assert p1["resume_after_path"] is None

    # Now trigger scan_limited under git_index and resume cleanly across all files.
    monkeypatch.setattr(fs_mod, "MAX_FILE", 128)
    all_paths = []
    cursor = None
    for _ in range(15):
        page = engine.search(
            project,
            {
                "path": "pkg",
                "query": "match_line",
                "file_glob": "*",
                "case_sensitive": False,
                "offset": 0,
                "limit": 50,
                "resume_after_path": cursor,
            },
        )
        assert page["scan_scope"] == "git_index"
        all_paths.extend(m["path"] for m in page["matches"])
        if page["scan_status"] == "scan_limited":
            assert page["next_offset"] is None
            cursor = page["resume_after_path"]
            assert cursor
        else:
            assert page["truncated"] is False
            break
    else:
        raise AssertionError("git_index resume_after_path did not terminate")

    assert all_paths == ["pkg/a_extra.py"] + [f"pkg/m{i:02d}.py" for i in range(8) for _ in range(2)]


@pytest.mark.asyncio
async def test_lsp_pool_reuses_warm_server_across_queries_and_evicts_on_idle(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text("def fixture():\n    return 1\n", encoding="utf-8")
    (root / "helper.py").write_text("def caller():\n    return 2\n", encoding="utf-8")
    marker = tmp_path / "marker.json"

    pool = lsp_navigation.LspPool(idle_ttl=0.25, max_instances=3)
    agent = _make_lsp_agent(tmp_path, root, marker, pool=pool)
    project = {"root": str(root), "alias": "Fixture", "id": "proj-1", "allow_tasks": True}

    try:
        r1 = await lsp_navigation.query(
            agent,
            "lsp-op-1",
            project,
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        assert r1["source_current"] is True
        assert len(r1["items"]) == 1
        m1 = json.loads(marker.read_text())
        pid1 = m1["pid"]
        assert m1["requests"].count("initialize") == 1
        assert m1["requests"].count("textDocument/didOpen") == 1
        assert resident_count(pool) == 1
        assert not agent.processes

        t0 = time.monotonic()
        r2 = await lsp_navigation.query(
            agent,
            "lsp-op-2",
            project,
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        warm_elapsed = time.monotonic() - t0
        assert r2["source_current"] is True
        assert len(r2["items"]) == 1
        m2 = json.loads(marker.read_text())
        assert m2["pid"] == pid1
        assert m2["requests"].count("initialize") == 1
        assert m2["requests"].count("textDocument/didOpen") == 1
        assert pool.hits == 1
        assert pool.misses == 1
        assert warm_elapsed < 1.0

        # Wait past idle_ttl and assert background reaper shuts down the server cleanly.
        m_idle = {}
        for _ in range(60):
            m_idle = json.loads(marker.read_text())
            if resident_count(pool) == 0 and m_idle.get("state") in {"shutdown", "exited"}:
                break
            await asyncio.sleep(0.05)
        assert resident_count(pool) == 0
        assert m_idle["state"] in {"shutdown", "exited"}
    finally:
        await lsp_navigation.shutdown_all(agent)


@pytest.mark.asyncio
async def test_lsp_pool_incremental_sync_lru_memory_pressure_and_crash_respawn(tmp_path):
    roots = [tmp_path / f"p{i}" for i in range(3)]
    markers = [tmp_path / f"marker{i}.json" for i in range(3)]
    for root in roots:
        root.mkdir()
        (root / "source.py").write_text("def fixture():\n    return 1\n", encoding="utf-8")
        (root / "helper.py").write_text("def caller():\n    return 2\n", encoding="utf-8")

    # 1. Incremental document sync (didChange + didChangeWatchedFiles) preserves source_current
    pool = lsp_navigation.LspPool(idle_ttl=60.0, max_instances=2)
    config = {
        "allowed_roots": [{"path": str(tmp_path), "writable": True, "allow_tasks": True}],
        "integrations": {
            "language_servers": {
                "python": {
                    "enabled": True,
                    "projects": ["*"],
                    "command": [
                        sys.executable,
                        str(FAKE_LSP_SERVER),
                        "--marker",
                        str(markers[0]),
                    ],
                    "timeout_seconds": 5,
                }
            }
        },
    }
    engine = FileEngine(config, SimpleNamespace(directory=tmp_path / "journal"), tmp_path / "agent.json")
    agent = SimpleNamespace(
        config=config,
        engine=engine,
        processes={},
        kill_process=Agent.kill_process,
        lsp_pool=pool,
    )
    projects = [
        {"root": str(roots[i]), "alias": f"P{i}", "id": f"p{i}", "allow_tasks": True}
        for i in range(3)
    ]

    try:
        r1 = await lsp_navigation.query(
            agent,
            "sync-1",
            projects[0],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        sha1 = r1["source_sha256"]
        pid0 = json.loads(markers[0].read_text())["pid"]

        (roots[0] / "source.py").write_text("def fixture():\n    return 99\n", encoding="utf-8")
        r2 = await lsp_navigation.query(
            agent,
            "sync-2",
            projects[0],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        assert r2["source_sha256"] != sha1
        assert r2["source_current"] is True
        m_sync = json.loads(markers[0].read_text())
        assert m_sync["pid"] == pid0
        assert "textDocument/didChange" in m_sync["requests"]
        assert "workspace/didChangeWatchedFiles" in m_sync["requests"]

        # 2. Concurrent queries on same (root, language) reuse the single server cleanly.
        c1, c2 = await asyncio.gather(
            lsp_navigation.query(
                agent,
                "conc-1",
                projects[0],
                {"action": "hover", "path": "source.py", "line": 1, "column": 1, "limit": 50},
            ),
            lsp_navigation.query(
                agent,
                "conc-2",
                projects[0],
                {"action": "definition", "path": "source.py", "line": 1, "column": 1, "limit": 50},
            ),
        )
        assert c1["source_current"] is True and c2["source_current"] is True
        assert json.loads(markers[0].read_text())["pid"] == pid0

        # 3. LRU eviction at max_instances=2
        await lsp_navigation.query(
            agent,
            "lru-1",
            projects[1],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        assert resident_count(pool) == 2
        await lsp_navigation.query(
            agent,
            "lru-2",
            projects[2],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        assert resident_count(pool) == 2
        assert not any(key[:2] == (str(roots[0].resolve()), "python") for key in pool._entries)

        # 4. Memory-pressure eviction hook
        evicted = await lsp_navigation.evict_for_memory_pressure(agent)
        assert evicted == 2
        assert resident_count(pool) == 0

        # 5. Crash recovery + clean respawn
        await lsp_navigation.query(
            agent,
            "crash-1",
            projects[0],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        entry = next(
            value for key, value in pool._entries.items()
            if key[:2] == (str(roots[0].resolve()), "python")
        )
        dead_proc = entry.peer.process
        dead_pid = dead_proc.pid
        if os.name == "nt":
            dead_proc.kill()
        else:
            os.kill(dead_pid, signal.SIGKILL)
        await dead_proc.wait()

        recovered = await lsp_navigation.query(
            agent,
            "crash-2",
            projects[0],
            {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
        )
        assert recovered["source_current"] is True
        new_pid = json.loads(markers[0].read_text())["pid"]
        assert new_pid != dead_pid

        # 6. Disconnect and unresponsive timeout evict broken servers cleanly
        for bad_mode, expected_code in (("disconnect", "LSP_DISCONNECTED"), ("timeout", "LSP_TIMEOUT")):
            bad_agent = _make_lsp_agent(
                tmp_path,
                roots[0],
                markers[1],
                mode=bad_mode,
                timeout_seconds=1,
                pool=lsp_navigation.LspPool(idle_ttl=60.0, max_instances=2),
            )
            try:
                with pytest.raises(DevError) as exc_info:
                    await lsp_navigation.query(
                        bad_agent,
                        f"bad-{bad_mode}",
                        projects[0],
                        {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50},
                    )
                assert exc_info.value.code == expected_code
                assert resident_count(bad_agent.lsp_pool) == 0
            finally:
                await lsp_navigation.shutdown_all(bad_agent)
    finally:
        closed = await lsp_navigation.shutdown_all(agent)
        assert closed >= 0
        assert resident_count(pool) == 0
        assert not agent.processes


@pytest.mark.asyncio
async def test_lsp_pool_never_reuses_peer_for_recreated_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text("def fixture():\n    return 1\n")
    marker = tmp_path / "marker.json"
    pool = lsp_navigation.LspPool()
    agent = _make_lsp_agent(tmp_path, root, marker, pool=pool)
    project = {"root": str(root), "alias": "Fixture", "id": "proj-1", "allow_tasks": True}
    args = {"action": "symbols", "path": "source.py", "line": 1, "column": 1, "limit": 50}
    try:
        await lsp_navigation.query(agent, "old", project, args)
        first = json.loads(marker.read_text())["pid"]
        root.rename(tmp_path / "old")
        root.mkdir()
        (root / "source.py").write_text("def fixture():\n    return 2\n")
        await lsp_navigation.query(agent, "new", project, args)
        assert json.loads(marker.read_text())["pid"] != first
        assert pool.hits == 0
    finally:
        await pool.shutdown_all()
