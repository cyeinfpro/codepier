from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent import lsp_navigation, telemetry
from agent.filesystem import FileEngine
from agent.journal import Journal
from shared.contracts import Search, freeze_schema
from shared.unified_contracts import translate
from shared.util import DevError


def test_search_cursor_ties_result_order_to_scope(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "hello.py"
    path.write_text("# needle\n", encoding="utf-8")
    fs = FileEngine(
        {"allowed_roots": [{"path": str(root), "writable": False, "allow_tasks": True}]},
        SimpleNamespace(directory=tmp_path / "journal"),
        tmp_path / "agent.json",
    )
    project = {"root": str(root), "mode": "read"}
    args = {"query": "needle"}
    monkeypatch.setattr(fs, "git_candidates", lambda *a, **kw: ([(path, path.name, path.stat())], False))
    first = fs.search(project, args)
    assert first["scan_scope"] == "git_index"
    monkeypatch.setattr(fs, "git_candidates", lambda *a, **kw: None)
    cursor = {"resume_after_path": path.name}
    assert fs.search(project, {**args, **cursor})["scan_scope"] == "filesystem_walk"
    with pytest.raises(DevError) as exc:
        fs.search(project, {**args, **cursor, "resume_scan_scope": first["scan_scope"]})
    assert exc.value.status == 409
    assert exc.value.code == "SEARCH_SCAN_SCOPE_CHANGED"
    assert exc.value.details["scan_scope"] == "filesystem_walk"
    assert fs.search(
        project, {**args, **cursor, "resume_scan_scope": "filesystem_walk"}
    )["matches"] == []
    plan = translate(
        "query",
        {
            "action": "search",
            "project": "Fixture",
            "args": {**args, **cursor, "resume_scan_scope": first["scan_scope"]},
        },
    )
    options = plan.args["options"]
    typed = Search.model_validate(
        {"project": plan.args["project"], "query": plan.args["query"], **options}
    )
    assert typed.resume_scan_scope == "git_index"
    assert "resume_scan_scope" not in Search.model_validate(
        {"project": "Fixture", **args}
    ).model_dump()


def test_frozen_schema_blocks_inplace_union():
    schema = freeze_schema({"nested": {"existing": 1}})
    nested = schema["nested"]
    with pytest.raises(TypeError, match="read-only"):
        nested |= {"unwanted": 2}
    assert schema == {"nested": {"existing": 1}}
    assert nested | {"copy_only": 2} == {"existing": 1, "copy_only": 2}


def test_telemetry_counts_overflow_and_failed_flush(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal")
    monkeypatch.setattr(telemetry, "_PENDING_DB_LIMIT", 2)
    recorder = telemetry.AgentTelemetry(journal, deferred_persist=True)
    try:
        for identifier in ("first", "second", "overflow"):
            assert recorder.record(identifier, "executing")
        assert recorder.dropped == 1
        assert len(recorder._pending_db) == 2
        assert recorder.flush() == 2
        assert recorder.dropped == 1
        assert recorder.record("next", "executing")
        with journal.lock, journal.db:
            journal.db.execute("DROP TABLE execution_stages")
        assert recorder.flush() == 0
        assert recorder.errors == 1
        assert recorder.dropped == 2
        assert not recorder.has_pending()
    finally:
        journal.db.close()


@pytest.mark.asyncio
async def test_lsp_reports_wait_only_until_lock_acquired(monkeypatch):
    pool = lsp_navigation.LspPool(max_instances=1)
    process = object()

    async def close():
        pass

    async def spawn(entry, *_):
        entry.peer = SimpleNamespace(process=process, is_alive=lambda: True, close=close)
        return entry.peer

    monkeypatch.setattr(pool, "_spawn_peer", spawn)
    stages = []

    def phase(identifier, stage, **detail):
        stages.append((identifier, stage, detail))

    agent = SimpleNamespace(processes={}, phase=phase)
    key = ("/fixture", "python")
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def first(_):
        entered.set()
        await finish.wait()
        return "first"

    async def second(_):
        return "second"

    one = asyncio.create_task(pool.run_query(agent, "one", {}, "/fixture", key, {}, first))
    two = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            two = asyncio.create_task(pool.run_query(agent, "two", {}, "/fixture", key, {}, second))
            while not stages:
                await asyncio.sleep(0)
            assert stages == [
                ("two", "waiting_resource", {"queue_reason": "lsp_busy", "resource_kinds": ["service"]})
            ]
            finish.set()
            assert await one == "first"
            assert await two == "second"
            assert stages[-1] == ("two", "executing", {})
    finally:
        finish.set()
        await asyncio.gather(one, *(task for task in [two] if task), return_exceptions=True)
        await pool.shutdown_all()
