import json
import os
from pathlib import Path

import pytest

from agent.filesystem import FileEngine
from agent.journal import Journal
from shared.contracts import TOOLS


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    config_path = private / "config.json"
    config_path.write_text("{}")
    journal = Journal(tmp_path / "state")
    config = {"allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}], "tasks": {
        "test": {"projects": ["P"], "description": "Run tests", "env": {"SECRET": "never-return-this-value"}}}}
    engine = FileEngine(config, journal, config_path)
    project = {"id": "p", "alias": "P", "root": str(root), "mode": "write", "allow_tasks": True}
    yield engine, project, root
    journal.db.close()


def context(workspace, **args):
    engine, project, _ = workspace
    return engine.call("project_context", project, TOOLS["project_context"].model.model_validate({"project": "P", **args}).model_dump())


def test_bootstrap_is_bounded_and_does_not_execute_documents(workspace):
    _, _, root = workspace
    (root / "AGENTS.md").write_text("Project instructions\n" + "x" * 5000)
    (root / "README.md").write_text("README")
    (root / "package.json").write_text(json.dumps({"scripts": {"install": "touch SHOULD_NOT_EXIST"}}))
    result = context(workspace, max_chars=1000)
    assert result["budget"]["preview_chars"] == 1000 and result["truncated"]
    assert "README.md" in result["remaining_documents"]
    doc = result["documents"][0]
    assert len(doc["sha256"]) == 64 and doc["next_start_line"] == 2
    assert not (root / "SHOULD_NOT_EXIST").exists()
    assert "never-return-this-value" not in json.dumps(result)
    assert "NOT a full repository scan" in result["scope"]


def test_skills_index_discloses_summaries_not_entire_documents(workspace):
    _, _, root = workspace
    for directory in ("skills/review", ".agents/skills/release", ".claude/skills/test"):
        path = root / directory
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text("---\nname: test\ndescription: Useful skill\n---\nINTERNAL_FULL_BODY\n")
    result = context(workspace)
    assert len(result["skills"]) == 3
    assert all(s["description"] == "Useful skill" and len(s["sha256"]) == 64 for s in result["skills"])
    assert "INTERNAL_FULL_BODY" not in json.dumps(result)
    assert context(workspace, include_skills=False)["skills"] == []


def test_context_preserves_symlink_and_secret_exclusions(workspace, tmp_path):
    _, _, root = workspace
    secret = tmp_path / "outside.txt"
    secret.write_text("DO_NOT_LEAK")
    (root / "AGENTS.md").symlink_to(secret)
    (root / "skills").symlink_to(tmp_path, target_is_directory=True)
    (root / ".env").write_text("DO_NOT_LEAK")
    result = context(workspace)
    assert "DO_NOT_LEAK" not in json.dumps(result)
    assert result["warnings"] and result["truncated"]


def test_context_marks_index_limits_and_lists_omitted_documents(workspace):
    _, _, root = workspace
    (root / "README.md").write_text("readme")
    docs = root / "docs"
    docs.mkdir()
    for i in range(70):
        (docs / f"{i:02d}.md").write_text("documentation")
    skills = root / "skills"
    for i in range(23):
        path = skills / str(i)
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text("description: skill")
    result = context(workspace, max_files=1)
    assert len(result["documents"]) == 1 and result["remaining_documents"] and result["truncated"]
    assert len(result["skills"]) == 20
