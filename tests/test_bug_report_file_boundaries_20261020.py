"""Exercise the existing server-side boundaries named by report items 7 and 10."""
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from agent.filesystem import FileEngine
from agent.journal import Journal
from agent.native_cli import NativeCLI
from shared.native_cli import ATTACHMENT_CAP
from shared.util import DevError


@pytest.fixture
def native(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    config_path = private / "config.json"
    config_path.write_text("{}")
    config = {"allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
              "shell": {"enabled": True, "projects": ["*"], "inherit_env": False, "env": {}}}
    journal = Journal(tmp_path / "state")
    engine = FileEngine(config, journal, config_path)
    obj = NativeCLI(SimpleNamespace(state_dir=tmp_path / "state", engine=engine, config=config))
    project = {"id": "fixture-project", "alias": "fixture", "device_id": "fixture-device",
               "root": str(root), "mode": "write", "allow_tasks": True,
               "_native_owner": "user:owner", "_native_space": "fixture-space"}
    yield obj, project, root
    journal.db.close()


@pytest.mark.parametrize("cwd", ["../outside", "/etc", "C:/Windows", "..\\outside", ".git", "\x00"])
def test_native_start_checks_cwd_on_server_before_spawning(native, monkeypatch, cwd):
    obj, project, root = native
    calls = []
    monkeypatch.setattr("agent.native_cli.subprocess.Popen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(DevError):
        obj.action("start", project, {"id": uuid.uuid4().hex, "cli": "pi", "cwd": cwd})
    assert not calls


def test_native_cwd_rejects_symlink_escape(native, monkeypatch):
    obj, project, root = native
    (root / "escape").symlink_to(root.parent, target_is_directory=True)
    calls = []
    monkeypatch.setattr("agent.native_cli.subprocess.Popen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(DevError, match="符号链接"):
        obj.action("start", project, {"id": uuid.uuid4().hex, "cli": "pi", "cwd": "escape"})
    assert not calls


def test_encoded_dots_in_json_stay_a_literal_project_filename(native):
    obj, _, root = native
    folder = root / "%2e%2e"
    folder.mkdir()
    assert obj.agent.engine.path(root, "%2e%2e") == folder
    assert root in obj.agent.engine.path(root, "%2e%2e/file.txt").parents


def upload_args(raw, **overrides):
    return {"file": uuid.uuid4().hex, "name": "fixture.txt", "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), **overrides}


def test_filename_cannot_escape_spool_and_other_owner_cannot_adopt_upload(native):
    obj, project, root = native
    raw = b"private fixture source"
    args = upload_args(raw, name="../../fixture.txt")
    obj.action("upload_begin", project, args)
    with pytest.raises(DevError) as denied:
        obj.action("upload_finish", {**project, "_native_owner": "user:other"}, {"file": args["file"]})
    assert denied.value.code == "CLI_OWNER_DENIED"
    result = obj.action("upload_chunk", project, {"file": args["file"], "offset": 0,
                        "data": base64.b64encode(raw).decode(), "sha256": args["sha256"]})
    path = Path(result["path"])
    assert path.parent == obj.directory / "uploads"
    assert path.name == args["file"] + ".txt"
    assert result["ready"] and path.read_bytes() == raw
    assert not list(root.iterdir())


@pytest.mark.parametrize("size", [0, -1, ATTACHMENT_CAP + 1, True])
def test_invalid_upload_size_is_rejected_without_spool_file(native, size):
    obj, project, _ = native
    args = upload_args(b"x", size=size)
    with pytest.raises(ValueError, match="20 MiB"):
        obj.action("upload_begin", project, args)
    with obj.connect_db() as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert not list((obj.directory / "uploads").glob("*"))


def test_full_file_hash_mismatch_never_marks_attachment_ready(native):
    obj, project, _ = native
    raw = b"content whose expected file hash is wrong"
    args = upload_args(raw, sha256="0" * 64)
    obj.action("upload_begin", project, args)
    with pytest.raises(ValueError, match="Attachment SHA256 mismatch"):
        obj.action("upload_chunk", project, {"file": args["file"], "offset": 0,
                   "data": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()})
    with obj.connect_db() as db:
        assert db.execute("SELECT ready FROM attachments WHERE id=?", (args["file"],)).fetchone()[0] == 0
