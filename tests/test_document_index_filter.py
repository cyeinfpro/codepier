"""Synthetic checks for bounded bootstrap document discovery."""
import hashlib

import pytest

from agent.context import DOC_INDEX_MAX_BYTES, index_document, project_context
from agent.filesystem import FileEngine
from agent.journal import Journal


@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-dev.txt", "docs/requirements-extra.txt",
    "poetry.lock", "docs/package-lock.json", "MANIFEST.sha256", "docs/build.log",
])
def test_generated_files_are_not_document_candidates(path):
    assert not index_document(path)


@pytest.mark.parametrize("path", [
    "README.md", "docs/LOCKING.md", "docs/requirements.md", "AGENTS.md",
    "docs/DESIGN_2026-11-04.md", "package.json", "pyproject.toml",
])
def test_current_and_historical_guides_remain_discoverable(path):
    assert index_document(path)


@pytest.mark.parametrize("detail", ["minimal", "full"])
def test_document_filter_keeps_explicit_reads_and_large_document_sha(tmp_path, detail):
    root = tmp_path / "project"
    root.mkdir()
    docs = root / "docs"
    docs.mkdir()
    (root / "README.md").write_text("Read me\n")
    lock = root / "requirements.txt"
    lock.write_text("generated locked dependencies\n" * 2000)
    large = docs / "GUIDE.md"
    large.write_text("guide line\n" * (DOC_INDEX_MAX_BYTES // 10 + 1))
    state = tmp_path / "state"
    journal = Journal(state)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    engine = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}]},
                        journal, config_path)
    project = {"id": "fixture", "alias": "Fixture", "root": str(root), "mode": "write"}
    try:
        result = project_context(engine, project, {
            "context_detail": detail, "include_skills": False, "max_files": 8,
            "max_chars": 12000,
        })
        indexed = {row["path"]: row for row in result["documents"]}
        assert "requirements.txt" not in indexed
        assert "requirements.txt" not in result["remaining_documents"]
        assert indexed["docs/GUIDE.md"]["next"] == ("read" if detail == "minimal" else "fs_read")
        assert "offset/limit" in indexed["docs/GUIDE.md"]["read_hint"]
        assert "read_hint" not in indexed["README.md"]
        assert indexed["docs/GUIDE.md"]["sha256"] == hashlib.sha256(large.read_bytes()).hexdigest()
        # Filtering discovery must not hide files or alter ordinary read permissions.
        assert engine.read(project, {"path": "requirements.txt", "max_lines": 1})["content"]
        assert lock.exists()
        assert any(row["path"] == "requirements.txt" for row in result["root_entries"])
    finally:
        journal.db.close()
