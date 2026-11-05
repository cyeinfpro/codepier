from pathlib import Path
import json
import shutil
import subprocess

import pytest

from agent.run_directories import (
    cleanup_run_directories, finish_run_directory, prepare_run_directory, run_environment,
)
from agent.snapshots import SnapshotManager, _clone_file
from shared.util import DevError


@pytest.fixture
def project(tmp_path):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("original")
    return root


@pytest.fixture
def manager(tmp_path):
    return SnapshotManager(tmp_path.resolve() / "state")


@pytest.fixture
def clone(monkeypatch):
    # Unit tests exercise lifecycle independent of the host filesystem.
    monkeypatch.setattr("agent.snapshots._clone_file", shutil.copy2)


def test_run_lifecycle_and_active_ttl(project):
    run = prepare_run_directory(project, "op-1")
    assert run_environment(run) == {"CODEPIER_RUN_DIR": str(run)}
    assert cleanup_run_directories(project, 0, now=1e20) == []
    finish_run_directory(run)
    assert cleanup_run_directories(project, 0, now=1e20) == ["op-1"]
    assert not run.exists()


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", ".", "", "x/y", "x\\y"])
def test_run_rejects_invalid_ids(project, name):
    with pytest.raises(DevError):
        prepare_run_directory(project, name)


def test_run_rejects_symlink_and_reuse(project, tmp_path):
    (project / ".codepier").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DevError):
        prepare_run_directory(project, "op")
    (project / ".codepier").unlink()
    prepare_run_directory(project, "op")
    with pytest.raises(DevError, match="already exists"):
        prepare_run_directory(project, "op")


def test_git_exclude_preserves_existing(project):
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    exclude = project / ".git" / "info" / "exclude"
    exclude.write_text("user-entry")
    prepare_run_directory(project, "first")
    prepare_run_directory(project, "second")
    assert exclude.read_text() == "user-entry\n/.codepier/runs/\n"
    assert subprocess.run(["git", "-C", str(project), "check-ignore", ".codepier/runs/first"], capture_output=True).returncode == 0


def test_snapshot_maps_cwd_excludes_metadata_and_success_cleanup(project, manager, clone):
    (project / ".git").write_text("gitdir: /outside/metadata")
    (project / ".codepier").mkdir()
    (project / ".codepier" / "private").write_text("excluded")
    snap = manager.create(project, "op", "src")
    assert snap.cwd == snap.root / "src"
    assert (snap.cwd / "main.py").read_text() == "original"
    assert not (snap.root / ".git").exists()
    assert not (snap.root / ".codepier").exists()
    (snap.cwd / "main.py").write_text("changed")
    assert (project / "src" / "main.py").read_text() == "original"
    assert manager.cleanup(0, now=1e20) == []
    manager.finish(snap, success=True)
    assert not snap.root.parent.exists()


def test_failed_snapshot_ttl(project, manager, clone):
    snap = manager.create(project, "failed")
    manager.finish(snap, success=False)
    assert manager.cleanup(3600, now=0) == []
    assert manager.cleanup(0, now=1e20) == ["failed"]


@pytest.mark.parametrize("cwd", ["../escape", "/tmp", ".git"])
def test_cwd_containment(project, manager, cwd):
    with pytest.raises(DevError):
        manager.create(project, "op", cwd)


def test_clone_failure_does_not_run_or_fallback(project, manager, monkeypatch):
    def unsupported(*args):
        raise DevError("SNAPSHOT_UNSUPPORTED", "no reflink")
    monkeypatch.setattr("agent.snapshots._clone_file", unsupported)
    with pytest.raises(DevError) as caught:
        manager.create(project, "unsupported")
    assert caught.value.code == "SNAPSHOT_UNSUPPORTED"
    assert not (manager.parent / "unsupported").exists()


@pytest.mark.parametrize("kind", ["absolute", "escape", "dangling", "metadata"])
def test_unsafe_symlinks_rejected(project, manager, kind):
    targets = {"absolute": str(project / "src"), "escape": "../outside", "dangling": "missing", "metadata": ".git"}
    (project / ".git").mkdir()
    (project / "link").symlink_to(targets[kind])
    with pytest.raises(DevError):
        manager.create(project, "unsafe")


def test_relative_internal_symlink_supported(project, manager, clone):
    (project / "link").symlink_to("src")
    snap = manager.create(project, "safe")
    assert (snap.root / "link").is_symlink()
    assert (snap.root / "link" / "main.py").read_text() == "original"


@pytest.mark.parametrize(("name", "content"), [
    ("editable.pth", "/original/project"),
    ("exec.pth", "import external_finder"),
    ("escape.pth", "../../outside"),
    ("package.egg-link", "/original"),
    ("__editable___finder.py", "MAPPING = {}"),
    ("direct_url.json", json.dumps({"dir_info": {"editable": True}})),
])
def test_editable_escapes_rejected(project, manager, name, content):
    (project / name).write_text(content)
    with pytest.raises(DevError) as caught:
        manager.create(project, "unsafe")
    assert caught.value.code == "SNAPSHOT_UNSAFE"


def test_artifact_extraction_and_containment(project, manager, clone):
    snap = manager.create(project, "op")
    run = prepare_run_directory(project, "op")
    (snap.root / "reports").mkdir()
    (snap.root / "reports" / "report.txt").write_text("ok")
    assert manager.extract_artifacts(snap, ["reports"], run) == ["reports"]
    assert (run / "reports" / "report.txt").read_text() == "ok"
    with pytest.raises(DevError):
        manager.extract_artifacts(snap, ["../escape"], run)
    (snap.root / "link").symlink_to("src/main.py")
    with pytest.raises(DevError):
        manager.extract_artifacts(snap, ["link"], run)
    with pytest.raises(DevError):
        manager.extract_artifacts(snap, ["reports"], run)


def test_artifact_directory_symlink_rejected(project, manager, clone):
    snap = manager.create(project, "op")
    run = prepare_run_directory(project, "op")
    (snap.root / "reports").mkdir()
    (snap.root / "reports" / "link").symlink_to("../src")
    with pytest.raises(DevError):
        manager.extract_artifacts(snap, ["reports"], run)


def test_cleanup_ignores_unowned_and_symlink_entries(project, manager, tmp_path):
    other = tmp_path.resolve() / "other"
    other.mkdir()
    (other / "keep").write_text("safe")
    (manager.parent / "link").symlink_to(other, target_is_directory=True)
    unmanaged = manager.parent / "unmanaged"
    unmanaged.mkdir()
    assert manager.cleanup(0, now=1e20) == []
    assert (other / "keep").exists()


def test_real_clone_is_cow_or_explicitly_unsupported(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"original" * 100)
    try:
        _clone_file(str(source), str(destination))
    except DevError as error:
        assert error.code == "SNAPSHOT_UNSUPPORTED"
        pytest.skip("Host filesystem does not support reflink")
    destination.write_bytes(b"changed")
    assert source.read_bytes() == b"original" * 100


@pytest.mark.parametrize("error_kind", ["unsupported", "missing", "timeout"])
def test_linux_clone_never_falls_back(monkeypatch, tmp_path, error_kind):
    monkeypatch.setattr("agent.snapshots.sys.platform", "linux")
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        if error_kind == "missing":
            raise FileNotFoundError("cp")
        if error_kind == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setattr("agent.snapshots.subprocess.run", run)
    with pytest.raises(DevError) as caught:
        _clone_file(str(tmp_path / "source"), str(tmp_path / "destination"))
    assert caught.value.code == "SNAPSHOT_UNSUPPORTED"
    assert len(commands) == 1
    assert "--reflink=always" in commands[0]


def test_original_venv_shebang_rejected(project, manager):
    script = project / "pytest"
    script.write_text("#!" + str(project / ".venv/bin/python") + "\n")
    script.chmod(0o755)
    with pytest.raises(DevError, match="original project"):
        manager.create(project, "unsafe")


def test_malformed_editable_metadata_fails_closed(project, manager):
    (project / "direct_url.json").write_text('{"dir_info": null}')
    with pytest.raises(DevError):
        manager.create(project, "unsafe")
