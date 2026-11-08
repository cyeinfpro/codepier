"""Synthetic knowledge-map storage, isolation, source binding and automatic reads."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core_files import CoreFiles
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent import project_knowledge as knowledge
from shared.crypto import digest
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    config_path = private / "config.json"
    config_path.write_text("{}")
    journal = Journal(tmp_path / "state")
    config = {"allowed_roots": [{"path": str(root), "writable": True}], "tasks": {}}
    engine = FileEngine(config, journal, config_path)
    project = {"id": "p", "alias": "P", "root": str(root), "mode": "write"}
    yield engine, project, root
    journal.db.close()


def build(workspace, **args):
    engine, project, _ = workspace
    return knowledge.build(engine, project, {"max_files": 2000, "entries": [], **args})


def query(workspace, **args):
    engine, project, _ = workspace
    return knowledge.query(engine, project, {"query": "", "paths": [], "limit": 8,
                                            "offset": 0, "max_chars": 6000, **args})


def test_absent_read_does_not_initialize_storage(workspace):
    engine, project, _ = workspace
    before = engine.journal.db.total_changes
    assert query(workspace)["state"] == "missing"
    assert knowledge.auto_context(engine, project) is None
    assert engine.journal.db.total_changes == before
    assert engine.journal.db.execute("SELECT name FROM sqlite_master WHERE name='project_knowledge'").fetchone() is None


def test_build_and_query_are_structural_without_source_bodies(workspace):
    engine, project, root = workspace
    (root / "service.py").write_text('import helpers\n\ndef handle():\n    return "SOURCE_BODY_MUST_NOT_BE_INDEXED"\n')
    (root / "helpers.py").write_text("def helper():\n    pass\n")
    result = build(workspace)
    assert result["entries_count"] == 2 and result["changed_entries"] == 2
    selected = query(workspace, paths=["service.py"])
    entry = selected["entries"][0]
    assert entry["symbols"] == ["handle"] and entry["related_paths"] == ["helpers.py"]
    assert "SOURCE_BODY_MUST_NOT_BE_INDEXED" not in json.dumps(selected)
    assert selected["trust"] == knowledge.TRUST
    assert not list(root.glob("*knowledge*"))
    assert engine.call("project_knowledge_query", project, {})["state"] == "ready"


def test_annotation_is_bound_to_source_and_survives_unchanged_refresh(workspace):
    _, _, root = workspace
    source = b"def send(): pass\n"
    (root / "transport.py").write_bytes(source)
    build(workspace)
    result = build(workspace, entries=[{"path": "transport.py", "expected_sha256": digest(source),
                                       "summary": "Deliver durable receipts without replay.", "related_paths": []}])
    assert result["reused_entries"] == 1
    assert query(workspace)["entries"][0]["origin"] == "agent"
    assert build(workspace)["revision"] == result["revision"]
    assert query(workspace)["entries"][0]["summary"] == "Deliver durable receipts without replay."
    with pytest.raises(DevError, match="变化"):
        build(workspace, entries=[{"path": "transport.py", "expected_sha256": "0" * 64, "summary": "wrong"}])
    assert query(workspace)["revision"] == result["revision"]


def test_stale_and_removed_content_is_never_replayed(workspace):
    _, _, root = workspace
    path = root / "worker.py"
    path.write_text("def original(): pass\n")
    build(workspace, entries=[{"path": "worker.py", "expected_sha256": digest(path.read_bytes()),
                              "summary": "OLD_ANNOTATION", "related_paths": []}])
    path.write_text("def replacement(): pass\n")
    result = query(workspace, paths=["worker.py"])
    assert result["entries"][0]["state"] == "stale" and result["refresh_required"]
    assert "OLD_ANNOTATION" not in json.dumps(result) and "original" not in json.dumps(result)
    build(workspace)
    entry = query(workspace)["entries"][0]
    assert entry["state"] == "current" and entry["origin"] == "structure"
    path.unlink()
    result = query(workspace)
    assert result["entries"][0]["state"] == "missing" and "summary" not in result["entries"][0]
    assert build(workspace)["entries_count"] == 0


def test_sensitive_paths_links_and_generated_files_are_excluded(workspace, tmp_path):
    _, _, root = workspace
    (root / ".env").write_text("TOP_SECRET")
    (root / "credentials.json").write_text('{"secret": "TOP_SECRET"}')
    (root / "ordinary.py").write_text('api_key = "sk-12345678901234567890"\ndef safe(): pass\n')
    (root / "README.md").write_text("# Title\n# password=TOP_SECRET_123456\n")
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("function built(){}")
    (root / "link.py").symlink_to(root / "ordinary.py")
    (root / "hard.py").write_text("def hard(): pass\n")
    os.link(root / "hard.py", tmp_path / "hard-other.py")
    build(workspace)
    result = query(workspace, limit=40)
    value = json.dumps(result)
    assert "TOP_SECRET" not in value and "sk-1234" not in value
    assert {e["path"] for e in result["entries"]} == {"ordinary.py", "README.md"}
    with pytest.raises(DevError):
        query(workspace, paths=[".env"])
    with pytest.raises(DevError):
        query(workspace, paths=["../outside.py"])


def test_annotation_rejects_credentials_without_replacing_map(workspace):
    _, _, root = workspace
    (root / "a.py").write_text("pass\n")
    original = build(workspace)
    with pytest.raises(DevError) as error:
        build(workspace, entries=[{"path": "a.py", "expected_sha256": digest(b"pass\n"),
                                  "summary": "password=very-secret-password"}])
    assert error.value.code == "KNOWLEDGE_SENSITIVE_TEXT"
    assert query(workspace)["revision"] == original["revision"]


def test_current_policy_and_link_changes_hide_retained_entries(workspace):
    engine, _, root = workspace
    (root / "a.py").write_text("def a(): pass\n")
    build(workspace)
    original = root / "a.py"
    original.rename(root / "moved.py")
    original.symlink_to(root / "moved.py")
    assert query(workspace)["entries"] == []
    engine.config["allowed_roots"] = []
    with pytest.raises(DevError) as error:
        query(workspace)
    assert error.value.code == "ROOT_NOT_ALLOWED"


def test_project_identity_and_worktree_roots_are_isolated(workspace, tmp_path):
    engine, project, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    assert knowledge.query(engine, {**project, "id": "other"}, {})["state"] == "missing"
    other = tmp_path / "worktree"
    other.mkdir()
    (other / "a.py").write_text("pass\n")
    engine.config["allowed_roots"].append({"path": str(other), "writable": True})
    separate = {**project, "root": str(other)}
    assert knowledge.query(engine, separate, {})["state"] == "missing"
    knowledge.build(engine, separate, {})
    assert knowledge.query(engine, separate, {})["state"] == "ready"
    assert query(workspace)["state"] == "ready"


def test_read_only_project_can_query_but_cannot_generate(workspace):
    engine, project, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    readonly = {**project, "mode": "read"}
    assert knowledge.query(engine, readonly, {})["state"] == "ready"
    with pytest.raises(DevError) as error:
        knowledge.build(engine, readonly, {})
    assert error.value.code == "READ_ONLY"


def test_revision_compare_and_swap_rejects_stale_writer(workspace):
    _, _, root = workspace
    (root / "a.py").write_text("pass\n")
    result = build(workspace)
    with pytest.raises(DevError) as error:
        build(workspace, expected_revision="new")
    assert error.value.code == "KNOWLEDGE_CONFLICT"
    (root / "b.py").write_text("pass\n")
    build(workspace, expected_revision=result["revision"])
    with pytest.raises(DevError):
        build(workspace, expected_revision=result["revision"])


def test_file_budget_does_not_replace_old_map(workspace):
    _, _, root = workspace
    (root / "a.py").write_text("pass\n")
    revision = build(workspace)["revision"]
    (root / "b.py").write_text("pass\n")
    with pytest.raises(DevError) as error:
        build(workspace, max_files=1)
    assert error.value.code == "KNOWLEDGE_SCAN_LIMIT"
    assert query(workspace)["revision"] == revision


def test_query_pages_and_full_json_budget(workspace):
    _, _, root = workspace
    for i in range(18):
        (root / f"entry_{i:02d}.py").write_text(f"def symbol_{i:02d}(): pass\n")
    build(workspace)
    seen, offset = [], 0
    while True:
        result = query(workspace, offset=offset, limit=5, max_chars=1000)
        assert len(knowledge.encoded(result)) <= 1000
        seen.extend(entry["path"] for entry in result["entries"])
        if result["next_offset"] is None:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert len(seen) == 18 and len(set(seen)) == 18
    tiny = query(workspace, max_chars=512)
    assert len(knowledge.encoded(tiny)) <= 512


def test_directory_only_reader_does_not_construct_journal(workspace, monkeypatch):
    engine, project, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    readonly = FileEngine(engine.config, SimpleNamespace(directory=engine.journal.directory), engine.config_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Journal construction is forbidden in a read")
    monkeypatch.setattr(Journal, "__init__", forbidden)
    assert knowledge.auto_context(readonly, project)["state"] == "ready"


def test_open_and_file_read_include_actual_reference_data(workspace):
    engine, project, root = workspace
    (root / "worker.py").write_text("def process_receipt(): pass\n")
    (root / "README.md").write_text("# Receipts\n")
    build(workspace)
    from shared.contracts import TOOLS
    args = TOOLS["project_context"].model.model_validate({"project": "P"}).model_dump()
    result = engine.call("project_context", project, args)
    assert result["project_knowledge"]["entries"]
    args = TOOLS["read"].model.model_validate({"project": "P", "path": "worker.py"}).model_dump()
    result = CoreFiles(engine).call("read", project, args)
    assert result["project_knowledge"]["entries"][0]["symbols"] == ["process_receipt"]


def test_storage_limit_rolls_back_update(workspace, monkeypatch):
    _, _, root = workspace
    (root / "a.py").write_text("pass\n")
    original = build(workspace)
    (root / "a.py").write_text("def changed(): pass\n")
    monkeypatch.setattr(knowledge, "STORE_BYTES", 1)
    with pytest.raises(DevError) as error:
        build(workspace)
    assert error.value.code == "KNOWLEDGE_STORAGE_LIMIT"
    assert query(workspace)["revision"] == original["revision"]


def test_concurrent_revision_change_does_not_overwrite(workspace, monkeypatch):
    engine, _, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    original = knowledge.structural
    def competing(*args):
        with engine.journal.lock, engine.journal.db:
            engine.journal.db.execute("UPDATE project_knowledge SET revision=?", ("f" * 64,))
        return original(*args)
    monkeypatch.setattr(knowledge, "structural", competing)
    (root / "a.py").write_text("def newer(): pass\n")
    with pytest.raises(DevError) as error:
        build(workspace)
    assert error.value.code == "KNOWLEDGE_CONFLICT"
    assert query(workspace)["revision"] == "f" * 64


def test_no_match_does_not_claim_whole_repository_freshness(workspace):
    _, _, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    (root / "new.py").write_text("def entirely_new(): pass\n")
    result = query(workspace, query="entirely_new")
    assert result["entries"] == []
    assert "no whole-repository freshness claim" in result["freshness"]


def test_automatic_context_falls_back_for_chinese_task(workspace):
    engine, project, root = workspace
    (root / "README.md").write_text("# Application architecture\n")
    build(workspace)
    assert query(workspace, query="请修复授权问题")["entries"] == []
    result = knowledge.auto_context(engine, project, query="请修复授权问题")
    assert result["entries"][0]["path"] == "README.md"
    assert len(knowledge.encoded(result)) <= 3500


def test_corrupt_map_read_is_unavailable_and_explicit_build_repairs(workspace):
    engine, project, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.execute("UPDATE project_knowledge SET payload=?", ('{"version":1,"entries":[42]}',))
    with pytest.raises(DevError) as error:
        query(workspace)
    assert error.value.code == "KNOWLEDGE_CORRUPT"
    assert knowledge.auto_context(engine, project)["state"] == "unavailable"
    assert build(workspace)["entries_count"] == 1
    assert query(workspace)["entries"][0]["state"] == "current"


def test_context_identity_changes_when_map_or_selected_source_changes(workspace):
    engine, project, root = workspace
    project.update(allow_tasks=False, _coding_owner="fixture-owner", _coding_device="fixture-device")
    (root / "a.py").write_text("def before(): pass\n")
    from shared.contracts import TOOLS
    args = TOOLS["open_workspace"].model.model_validate({"project": "P"}).model_dump()
    empty = engine.call("open_workspace", project, args)
    build(workspace)
    first = engine.call("open_workspace", project, {**args, "context_id": empty["context_id"]})
    assert not first["context_unchanged"] and first["context"]["project_knowledge"]["entries"]
    (root / "a.py").write_text("def after(): pass\n")
    changed = engine.call("open_workspace", project, {**args, "context_id": first["context_id"]})
    assert not changed["context_unchanged"]
    assert changed["context"]["project_knowledge"]["entries"][0]["state"] == "stale"


@pytest.mark.asyncio
async def test_isolated_read_worker_returns_real_reference_without_mutation(workspace):
    engine, project, root = workspace
    (root / "a.py").write_text("def value(): pass\n")
    build(workspace)
    from agent.workspace_probe import probe
    before = engine.journal.db.total_changes
    result = await probe(engine, "project_knowledge_query", project, {"query": "", "paths": [], "max_chars": 2000})
    assert result["entries"][0]["symbols"] == ["value"]
    assert engine.journal.db.total_changes == before


@pytest.mark.parametrize("payload", [b"not-a-text-payload", "x" * 1025])
def test_non_text_and_oversized_map_can_be_rebuilt(workspace, monkeypatch, payload):
    engine, project, root = workspace
    (root / "a.py").write_text("pass\n")
    build(workspace)
    monkeypatch.setattr(knowledge, "MAP_BYTES", 1024)
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.execute("UPDATE project_knowledge SET payload=?", (payload,))
    assert knowledge.auto_context(engine, project)["state"] == "unavailable"
    with pytest.raises(DevError) as error:
        query(workspace)
    assert error.value.code == "KNOWLEDGE_CORRUPT"
    assert build(workspace)["entries_count"] == 1
    assert query(workspace)["entries"][0]["state"] == "current"
