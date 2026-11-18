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
    monkeypatch.setattr("agent.snapshots._clone_directory", lambda *_: False)


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
    monkeypatch.setattr("agent.snapshots._clone_directory", lambda *_: False)
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


def test_snapshot_relocates_contained_python_entrypoint_without_touching_source(project, manager, clone):
    bin_dir = project / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_bytes(b"interpreter")
    entry = bin_dir / "pip"
    original = b"#!" + bytes(bin_dir / "python") + b" -I\nprint('fixture')\n"
    entry.write_bytes(original)
    entry.chmod(0o755)
    snap = manager.create(project, "relocate")
    cloned = snap.root / ".venv/bin/pip"
    assert cloned.read_bytes() == original.replace(bytes(project), bytes(snap.root), 1)
    assert cloned.stat().st_mode & 0o111 and entry.read_bytes() == original


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
        assert source.read_bytes() == b"original" * 100
        # Explicit lack of reflink support is a valid fail-closed outcome,
        # not a skipped contract. Never substitute a full-copy fallback.
        assert not destination.exists() or destination.stat().st_size == 0
        return
    assert destination.read_bytes() == b"original" * 100
    destination.write_bytes(b"changed")
    assert source.read_bytes() == b"original" * 100


@pytest.mark.parametrize("err", ["EOPNOTSUPP", "ENOSYS", "EINTR"])
def test_linux_clone_never_falls_back(monkeypatch, tmp_path, err):
    import errno
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr("agent.snapshots.sys.platform", "linux")
    calls = []
    def ioctl(destination, request, source):
        calls.append(request)
        raise OSError(getattr(errno, err), err)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"original")
    with pytest.raises(DevError) as caught:
        _clone_file(str(source), str(destination))
    assert caught.value.code == "SNAPSHOT_UNSUPPORTED"
    assert calls == [0x40049409]
    assert source.read_bytes() == b"original"
    assert not destination.exists() or destination.stat().st_size == 0


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


@pytest.mark.parametrize("kind", ["run", "snapshot"])
def test_concurrent_ttl_cleanup_of_same_entry(project, manager, clone, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    if kind == "run":
        entry = prepare_run_directory(project, "expired")
        finish_run_directory(entry)
        cleanup = lambda: cleanup_run_directories(project, 0, now=1e20)
    else:
        snapshot = manager.create(project, "expired")
        manager.finish(snapshot, success=False)
        entry = snapshot.root.parent
        cleanup = lambda: manager.cleanup(0, now=1e20)
    original = shutil.rmtree
    barrier, removed, lock = threading.Barrier(2), threading.Event(), threading.Lock()
    calls = 0
    def ordered_remove(path, *args, **kwargs):
        nonlocal calls
        if Path(path) != entry:
            return original(path, *args, **kwargs)
        with lock:
            calls += 1
            first = calls == 1
        barrier.wait(timeout=3)
        if first:
            try:
                return original(path, *args, **kwargs)
            finally:
                removed.set()
        assert removed.wait(3)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", ordered_remove)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cleanup) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert not entry.exists()
    assert sorted(results, key=len) == [[], ["expired"]]


@pytest.mark.parametrize("kind", ["run", "snapshot"])
@pytest.mark.parametrize("failure", ["permission", "missing_child", "symlink"])
def test_ttl_cleanup_preserves_io_errors_and_symlink_rejection(project, manager, clone, monkeypatch, kind, failure):
    if kind == "run":
        entry = prepare_run_directory(project, "expired")
        finish_run_directory(entry)
        cleanup = lambda: cleanup_run_directories(project, 0, now=1e20)
    else:
        snapshot = manager.create(project, "expired")
        manager.finish(snapshot, success=False)
        entry = snapshot.root.parent
        cleanup = lambda: manager.cleanup(0, now=1e20)
    original = shutil.rmtree
    target = project.parent / "protected-target"
    target.mkdir()
    (target / "keep").write_text("keep")
    def fail(path, *args, **kwargs):
        if failure == "permission":
            raise PermissionError("synthetic denied deletion")
        if failure == "missing_child":
            raise FileNotFoundError("synthetic child disappeared; entry still exists")
        original(path)
        Path(path).symlink_to(target, target_is_directory=True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", fail)
    with pytest.raises(OSError):
        cleanup()
    assert (target / "keep").read_text() == "keep"


def test_snapshot_excludes_nested_generated_and_credentials(project, manager, clone):
    extras = project / "src" / ".codepier-test-runs"
    extras.mkdir(parents=True, exist_ok=True)
    (extras / "test.log").write_text("discardable data")
    (project / "src" / ".env").write_text("FIXTURE_PRIVATE=1")
    (project / "src" / "source.py").write_text("value = 42")
    snapshot = manager.create(project, "a" * 32)
    assert (snapshot.root / "src" / "source.py").read_text() == "value = 42"
    assert not (snapshot.root / "src" / ".env").exists()
    assert not (snapshot.root / "src" / ".codepier-test-runs").exists()
