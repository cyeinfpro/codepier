"""One file-privacy policy across reads, scans, artifacts and saved search hits."""
import json
import zipfile

import pytest

from agent.artifacts import Artifacts
from agent.filesystem import FileEngine, protected
from agent.journal import Journal
from agent.searches import Searches
from shared.contracts import TOOLS
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    config = private / "config.json"
    config.write_text("{}")
    journal = Journal(tmp_path / "state")
    engine = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}]}, journal, config)
    project = {"id": "fixture-project", "root": str(root), "mode": "write"}
    try:
        yield engine, project, root
    finally:
        journal.db.close()


PRIVATE_PATHS = [
    ".codex/auth.json", "nested/credentials.json", "secrets.json",
    ".claude/.credentials.json", ".codex/sessions/fixture.jsonl",
    "nested/.codex/archived_sessions/fixture.jsonl",
    ".claude/projects/fixture/transcript.jsonl",
]


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_runtime_paths_are_not_regular_workspace_files(workspace, path):
    engine, project, root = workspace
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SYNTHETIC_PRIVATE_SENTINEL")
    with pytest.raises(DevError) as caught:
        engine.read(project, {"path": path})
    assert caught.value.code == "PROTECTED_PATH"
    assert protected(path)
    with pytest.raises(DevError):
        Artifacts(engine).register("1" * 32, project, {"path": path, "name": ""})
    assert not list((engine.journal.directory / "artifacts").glob("*.bin"))


def test_tree_search_checkpoint_share_private_path_exclusions(workspace):
    engine, project, root = workspace
    for name in PRIVATE_PATHS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SYNTHETIC_PRIVATE_SENTINEL")
    for name in (".codex/skills/review/SKILL.md", ".claude/skills/review/SKILL.md"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SAFE_SKILL_SOURCE")
        assert engine.read(project, {"path": name})["content"] == "SAFE_SKILL_SOURCE"
    tree = engine.tree(project, {"path": ".", "depth": 10, "offset": 0, "limit": 100})
    assert not any(row["path"] in PRIVATE_PATHS for row in tree["entries"])
    args = TOOLS["fs_search"].model(project="fixture-project", query="SYNTHETIC_PRIVATE_SENTINEL").model_dump()
    assert engine.search(project, args)["matches"] == []
    checkpoint = engine.checkpoint(project, {"label": "synthetic privacy"})
    with zipfile.ZipFile(checkpoint["local_archive"]) as archive:
        assert all("SYNTHETIC_PRIVATE_SENTINEL" not in archive.read(name).decode() for name in archive.namelist())


def test_saved_search_cannot_replay_now_protected_paths(workspace):
    engine, project, root = workspace
    searches = Searches(engine)
    # Historical cache fixture, made before the filename policy was tightened.
    args = TOOLS["searches_start"].model(project="fixture-project", query="SYNTHETIC_PRIVATE_SENTINEL").model_dump()
    searches.start("2" * 32, project, args)
    payload = {"path": ".codex/auth.json", "sha256": "0" * 64, "snippet": "SYNTHETIC_PRIVATE_SENTINEL"}
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.execute("INSERT INTO search_hits VALUES(?,?,?)", ("2" * 32, 1, json.dumps(payload)))
        engine.journal.db.execute("UPDATE search_sessions SET result_count=1 WHERE id=?", ("2" * 32,))
    result = searches.get(project, {"search_id": "2" * 32, "cursor": 0, "limit": 10})
    assert result["results"] == []
    assert result["cursor"] == 1 and not result["has_more"]


@pytest.mark.parametrize("path", ["auth.example.json", "credentials.sample.json", "secrets.template.json",
                                  "schemas/auth.schema.json", ".codex/skills/auth/SKILL.md",
                                  ".claude/skills/projects/SKILL.md", "src/sessions/history.json"])
def test_source_examples_and_skill_definitions_remain_readable(workspace, path):
    engine, project, root = workspace
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SAFE_EXAMPLE")
    assert not protected(path)
    assert engine.read(project, {"path": path})["content"] == "SAFE_EXAMPLE"


def test_registered_private_snapshot_cannot_be_downloaded_after_policy_upgrade(workspace):
    import hashlib
    import time
    engine, project, root = workspace
    artifacts = Artifacts(engine)
    identifier = "3" * 32
    binary = b"SYNTHETIC_PRIVATE_SENTINEL"
    sha = hashlib.sha256(binary).hexdigest()
    (artifacts.directory / (identifier + ".bin")).write_bytes(binary)
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.execute("INSERT INTO artifact_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            (identifier, str(root), ".codex/auth.json", "private.json", len(binary), sha,
             json.dumps([sha]), time.time(), time.time() + 60))
    with pytest.raises(DevError) as caught:
        artifacts.chunk(project, identifier, 0)
    assert caught.value.code == "PROTECTED_PATH"
    assert (artifacts.directory / (identifier + ".bin")).read_bytes() == binary
