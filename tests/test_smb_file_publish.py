"""No-clobber publication on filesystems without hard links."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from shared import file_publish
from shared.file_publish import AtomicCreateUnsupported, publish_new_file


def no_hard_links(*args, **kwargs):
    raise OSError(errno.ENOTSUP, "Hard links are unsupported")


def test_native_exclusive_rename_keeps_complete_content_and_no_clobber(tmp_path, monkeypatch):
    source, destination = tmp_path / "stage", tmp_path / "target"
    source.write_bytes(b"complete\0file\xff")
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    result = publish_new_file(source, destination)
    assert result.method in {"rename_exclusive", "rename_no_replace"}
    assert result.source_retained is False
    assert not source.exists()
    assert destination.read_bytes() == b"complete\0file\xff"
    source.write_bytes(b"a competing writer")
    with pytest.raises(FileExistsError):
        publish_new_file(source, destination)
    assert destination.read_bytes() == b"complete\0file\xff"
    assert source.read_bytes() == b"a competing writer"


def test_hard_link_path_remains_supported(tmp_path):
    source, destination = tmp_path / "stage", tmp_path / "target"
    source.write_text("complete", encoding="utf-8")
    result = publish_new_file(source, destination)
    assert result.method == "hard_link"
    assert result.source_retained is True
    assert source.exists()
    assert destination.read_text(encoding="utf-8") == "complete"


@pytest.mark.parametrize(
    "error", [errno.EACCES, errno.EPERM, errno.EIO, errno.ETIMEDOUT],
    ids=["EACCES", "EPERM", "EIO", "ETIMEDOUT"],
)
def test_io_and_permission_failures_do_not_replay_with_rename(tmp_path, monkeypatch, error):
    source, destination = tmp_path / "stage", tmp_path / "target"
    source.write_text("complete", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError(error, "injected publication failure")

    def unexpected(*args, **kwargs):
        pytest.fail("Permission/network/I/O failures must not cause publication replay")

    monkeypatch.setattr(file_publish.os, "link", fail)
    monkeypatch.setattr(file_publish, "_native_rename_no_replace", unexpected)
    with pytest.raises(OSError) as caught:
        publish_new_file(source, destination)
    assert caught.value.errno == error
    assert not destination.exists()
    assert source.exists()


def test_unsupported_fs_never_falls_back_to_replace_or_partial_copy(tmp_path, monkeypatch):
    source, destination = tmp_path / "stage", tmp_path / "target"
    source.write_bytes(b"complete")
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    monkeypatch.setattr(file_publish, "_native_rename_no_replace", no_hard_links)
    with pytest.raises(AtomicCreateUnsupported):
        publish_new_file(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b"complete"


@pytest.mark.parametrize("platform,symbol,cwd,flags", [
    ("darwin", "renameatx_np", -2, 4),
    ("linux", "renameat2", -100, 1),
])
def test_native_binding_uses_exclusive_flag_and_pinned_fds(monkeypatch, platform, symbol, cwd, flags):
    calls = []

    class Function:
        def __call__(self, *args):
            calls.append(args)
            return 0

    function = Function()
    library = type("Library", (), {symbol: function})()
    monkeypatch.setattr(file_publish.sys, "platform", platform)
    monkeypatch.setattr(file_publish.ctypes, "CDLL", lambda *args, **kwargs: library)
    assert file_publish._native_rename_no_replace("stage", "target", src_dir_fd=7, dst_dir_fd=8)
    assert calls == [(7, b"stage", 8, b"target", flags)]
    file_publish._native_rename_no_replace("stage", "target")
    assert calls[-1] == (cwd, b"stage", cwd, b"target", flags)


def test_native_errno_is_preserved(monkeypatch):
    class Function:
        def __call__(self, *args):
            ctypes.set_errno(errno.EEXIST)
            return -1

    library = type("Library", (), {"renameat2": Function()})()
    monkeypatch.setattr(file_publish.sys, "platform", "linux")
    monkeypatch.setattr(file_publish.ctypes, "CDLL", lambda *args, **kwargs: library)
    with pytest.raises(FileExistsError):
        file_publish._native_rename_no_replace("stage", "target")


def test_native_missing_symbol_is_explicit(monkeypatch):
    monkeypatch.setattr(file_publish.sys, "platform", "linux")
    monkeypatch.setattr(file_publish.ctypes, "CDLL", lambda *args, **kwargs: object())
    with pytest.raises(OSError) as caught:
        file_publish._native_rename_no_replace("stage", "target")
    assert caught.value.errno == errno.ENOSYS


def test_native_rename_does_not_follow_existing_destination_symlink(tmp_path, monkeypatch):
    source, destination, other = tmp_path / "stage", tmp_path / "target", tmp_path / "other"
    source.write_bytes(b"ours")
    other.write_bytes(b"theirs")
    destination.symlink_to(other)
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    with pytest.raises(FileExistsError):
        publish_new_file(source, destination)
    assert destination.is_symlink()
    assert other.read_bytes() == b"theirs"
    assert source.read_bytes() == b"ours"


def test_native_rename_respects_directory_descriptors(tmp_path, monkeypatch):
    if os.name == "nt":
        # Windows has no dir_fd support; the portable import branch is tested
        # separately and must never emulate it through the process cwd.
        with pytest.raises(OSError) as caught:
            file_publish._native_rename_no_replace("stage", "target", src_dir_fd=1, dst_dir_fd=1)
        assert caught.value.errno == errno.ENOSYS
        return
    (tmp_path / "stage").write_bytes(b"complete")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(file_publish.os, "link", no_hard_links)
        publish_new_file("stage", "target", src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
    assert not (tmp_path / "stage").exists()
    assert (tmp_path / "target").read_bytes() == b"complete"


@pytest.mark.parametrize("compatibility", [False, True])
def test_concurrent_processes_publish_exactly_one_complete_file(tmp_path, compatibility):
    program = """
import errno, os, sys
from pathlib import Path
from shared import file_publish
stage, target, payload = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].encode() * 4096
stage.write_bytes(payload)
def unsupported(*args, **kwargs):
    raise OSError(errno.ENOTSUP, 'unsupported')
file_publish.os.link = unsupported
print('ready', flush=True)
sys.stdin.readline()
try:
    file_publish.publish_new_file(stage, target)
except FileExistsError:
    print('conflict')
else:
    print('published')
"""
    if compatibility:
        program = program.replace("file_publish.os.link = unsupported",
            "file_publish.os.link = unsupported\nfile_publish._native_rename_no_replace = unsupported")
        program = program.replace("publish_new_file(stage, target)",
                                  "publish_new_file(stage, target, allow_exclusive_copy=True)")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(tmp_path / f"stage-{i}"), str(tmp_path / "target"), str(i)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=root, env=env,
        )
        for i in range(6)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = []
        for process in processes:
            output, error = process.communicate(timeout=20)
            assert process.returncode == 0, error
            results.append(output.strip())
        assert results.count("published") == 1
        assert results.count("conflict") == 5
        winner = results.index("published")
        assert (tmp_path / "target").read_bytes() == str(winner).encode() * 4096
        assert (tmp_path / f"stage-{winner}").exists() is compatibility
        for index in range(6):
            if index != winner:
                assert (tmp_path / f"stage-{index}").read_bytes() == str(index).encode() * 4096
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@pytest.fixture
def engine(tmp_path):
    from agent.filesystem import FileEngine
    from agent.journal import Journal

    root = tmp_path / "workspace"
    root.mkdir()
    config_path = tmp_path / "private" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    journal = Journal(tmp_path / "state")
    instance = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}]}, journal, config_path)
    yield instance, {"root": str(root), "mode": "write"}, root
    journal.db.close()


def test_engine_create_replace_restore_and_move_without_hard_links(engine, monkeypatch):
    from shared.crypto import digest

    instance, project, root = engine
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    first = instance.mutate(project, "source.txt", "new", b"first\n")
    assert first["publication_method"] in {"rename_exclusive", "rename_no_replace"}
    assert (root / "source.txt").read_bytes() == b"first\n"
    changed = instance.mutate(project, "source.txt", digest(b"first\n"), b"second\n")
    assert changed["publication_method"] == "replace"
    assert instance.journal.backup(str(root), changed["backup_id"])[1] == b"first\n"
    (root / "source.txt").chmod(0o751)
    moved = instance.move(project, {"path": "source.txt", "destination": "moved.txt", "expected_sha256": digest(b"second\n")})
    assert not (root / "source.txt").exists()
    assert (root / "moved.txt").read_bytes() == b"second\n"
    assert stat.S_IMODE((root / "moved.txt").stat().st_mode) == 0o751
    instance.call("history_restore", project, {"backup_id": moved["backup_id"], "expected_sha256": "new"})
    assert (root / "source.txt").read_bytes() == b"second\n"
    assert stat.S_IMODE((root / "source.txt").stat().st_mode) == 0o751
    assert not list(root.glob(".rd-*"))


def test_engine_competing_create_is_sha_conflict_and_keeps_other_file(engine, monkeypatch):
    from shared.util import DevError

    instance, project, root = engine
    native = file_publish._native_rename_no_replace
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)

    def race(source, destination, **kwargs):
        Path(destination).write_bytes(b"concurrent writer")
        return native(source, destination, **kwargs)

    monkeypatch.setattr(file_publish, "_native_rename_no_replace", race)
    with pytest.raises(DevError) as caught:
        instance.mutate(project, "target.txt", "new", b"ours")
    assert caught.value.code == "SHA_CONFLICT"
    assert (root / "target.txt").read_bytes() == b"concurrent writer"
    assert not list(root.glob(".rd-*"))


@pytest.mark.parametrize("change_target", [False, True])
def test_move_fallback_preserves_source_changes_and_foreign_destination(engine, monkeypatch, change_target):
    from shared.crypto import digest
    from shared.util import DevError

    instance, project, root = engine
    (root / "source.txt").write_bytes(b"before")
    native = file_publish._native_rename_no_replace
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)

    def race(source, destination, **kwargs):
        result = native(source, destination, **kwargs)
        (root / "source.txt").write_bytes(b"local source")
        if change_target:
            replacement = root / "editor.tmp"
            replacement.write_bytes(b"local target")
            os.replace(replacement, destination)
        return result

    monkeypatch.setattr(file_publish, "_native_rename_no_replace", race)
    with pytest.raises(DevError) as caught:
        instance.move(project, {"path": "source.txt", "destination": "target.txt", "expected_sha256": digest(b"before")})
    assert caught.value.code == "SHA_CONFLICT"
    assert (root / "source.txt").read_bytes() == b"local source"
    if change_target:
        assert (root / "target.txt").read_bytes() == b"local target"
    else:
        assert not (root / "target.txt").exists()
    assert not list(root.glob(".rd-*"))


def test_move_uncertain_source_delete_keeps_last_copy_and_backup(engine, monkeypatch):
    from shared.crypto import digest
    from shared.util import DevError

    instance, project, root = engine
    source = root / "source.txt"
    source.write_bytes(b"only complete copy")
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    unlink = Path.unlink

    def lost_ack(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == source:
            raise OSError(errno.EIO, "server deleted source but reply was lost")
        return result

    monkeypatch.setattr(Path, "unlink", lost_ack)
    with pytest.raises(DevError) as caught:
        instance.move(project, {"path": "source.txt", "destination": "target.txt", "expected_sha256": digest(b"only complete copy")})
    assert caught.value.code == "WRITE_OUTCOME_UNCONFIRMED"
    assert caught.value.details["retryable"] is False
    assert not source.exists()
    assert (root / "target.txt").read_bytes() == b"only complete copy"
    assert instance.journal.backup(str(root), caught.value.details["backup_id"])[1] == b"only complete copy"
    assert not list(root.glob(".rd-*"))


def test_uncertain_publish_ack_is_not_replayed_or_deleted(engine, monkeypatch):
    from shared.util import DevError

    instance, project, root = engine
    native = file_publish._native_rename_no_replace
    calls = []
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)

    def lost_ack(source, destination, **kwargs):
        calls.append(1)
        native(source, destination, **kwargs)
        raise OSError(errno.ETIMEDOUT, "server published but reply was lost")

    monkeypatch.setattr(file_publish, "_native_rename_no_replace", lost_ack)
    with pytest.raises(DevError) as caught:
        instance.mutate(project, "target.txt", "new", b"complete")
    assert caught.value.code == "WRITE_OUTCOME_UNCONFIRMED"
    assert caught.value.details["retryable"] is False
    assert caught.value.details["path"] == "target.txt"
    assert (root / "target.txt").read_bytes() == b"complete"
    assert calls == [1]
    assert not list(root.glob(".rd-*"))


def test_unsupported_atomic_publication_uses_explicit_compatibility(engine, monkeypatch):

    instance, project, root = engine
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    monkeypatch.setattr(file_publish, "_native_rename_no_replace", no_hard_links)
    receipt = instance.mutate(project, "target.txt", "new", b"complete")
    assert receipt["publication_method"] == "exclusive_create"
    assert receipt["atomic"] is False
    assert receipt["partial_content_visible"] is True
    assert (root / "target.txt").read_bytes() == b"complete"
    assert not list(root.glob(".rd-*"))


def test_network_read_failure_is_classified_without_partial_content(engine, monkeypatch):
    from shared.util import DevError

    instance, project, root = engine
    (root / "source.txt").write_bytes(b"complete")

    def unavailable(*args, **kwargs):
        raise OSError(getattr(errno, "ESTALE", errno.EIO), "share lost")

    monkeypatch.setattr(os, "read", unavailable)
    with pytest.raises(DevError) as caught:
        instance.read(project, {"path": "source.txt"})
    assert caught.value.code == "FILESYSTEM_UNAVAILABLE"


def test_directory_fsync_unsupported_remains_an_explicit_durability_failure(engine, monkeypatch):
    import agent.filesystem as filesystem
    from shared.util import DevError

    instance, project, root = engine
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    monkeypatch.setattr(filesystem, "fsync_directory", no_hard_links)
    with pytest.raises(DevError) as caught:
        instance.mutate(project, "target.txt", "new", b"complete")
    assert caught.value.code == "DURABILITY_UNCONFIRMED"
    assert caught.value.details["durability"] == "unconfirmed"
    assert (root / "target.txt").read_bytes() == b"complete"
    assert not list(root.glob(".rd-*"))


@pytest.mark.parametrize("mode", ["read_only", "unauthorized"])
def test_smb_fallback_never_bypasses_path_grants(engine, monkeypatch, mode):
    from shared.util import DevError

    instance, project, root = engine
    if mode == "read_only":
        project["mode"] = "read"
    else:
        instance.config["allowed_roots"] = []

    def forbidden(*args, **kwargs):
        pytest.fail("Publication was reached before validating authorization")

    monkeypatch.setattr(file_publish.os, "link", forbidden)
    with pytest.raises(DevError) as caught:
        instance.mutate(project, "target.txt", "new", b"complete")
    assert caught.value.code == ("READ_ONLY" if mode == "read_only" else "ROOT_NOT_ALLOWED")
    assert not (root / "target.txt").exists()


def test_native_import_uses_anchored_exclusive_rename_on_smb(engine, monkeypatch):
    from agent.incoming_artifacts import import_artifact

    instance, project, root = engine
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    receipt = import_artifact(instance, project, {
        "path": "incoming/photo.bin",
        "file": {"download_url": "https://files.oaiusercontent.com/fixture", "size": 9},
    }, stream=[b"complete\0"])
    assert receipt["created"] is True and receipt["overwritten"] is False
    assert receipt["publication_method"] in {"rename_exclusive", "rename_no_replace"}
    assert (root / "incoming/photo.bin").read_bytes() == b"complete\0"
    assert not list(root.rglob(".rd-*"))


def test_import_competing_target_is_not_overwritten_by_smb_fallback(engine, monkeypatch):
    from agent.incoming_artifacts import import_artifact
    from shared.util import DevError

    instance, project, root = engine
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)

    def concurrent_stream():
        yield b"complete"
        (root / "target.bin").write_bytes(b"other writer")

    with pytest.raises(DevError) as caught:
        import_artifact(instance, project, {
            "path": "target.bin", "file": {"download_url": "https://files.oaiusercontent.com/fixture", "size": 8},
        }, stream=concurrent_stream())
    assert caught.value.code == "ARTIFACT_DESTINATION_EXISTS"
    assert (root / "target.bin").read_bytes() == b"other writer"
    assert not list(root.rglob(".rd-*"))

@pytest.fixture
def exclusive_only(monkeypatch):
    monkeypatch.setattr(file_publish.os, "link", no_hard_links)
    monkeypatch.setattr(file_publish, "_native_rename_no_replace", no_hard_links)


def test_exclusive_copy_retains_stage_and_reports_non_atomic(tmp_path, exclusive_only):
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    receipt = publish_new_file(stage, target, allow_exclusive_copy=True)
    assert receipt.method == "exclusive_create"
    assert not receipt.atomic and receipt.source_retained
    assert stage.read_bytes() == target.read_bytes()
    assert receipt.identity == (target.stat().st_dev, target.stat().st_ino)


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_exclusive_copy_refuses_every_existing_target(tmp_path, exclusive_only, kind):
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"new")
    if kind == "file":
        target.write_bytes(b"old")
    elif kind == "symlink":
        target.symlink_to(stage)
    else:
        target.mkdir()
    with pytest.raises(FileExistsError):
        publish_new_file(stage, target, allow_exclusive_copy=True)
    assert stage.read_bytes() == b"new"
    if kind == "file":
        assert target.read_bytes() == b"old"


def test_exclusive_copy_blocks_symlink_parent(tmp_path, exclusive_only):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    stage = tmp_path / "stage"
    stage.write_bytes(b"new")
    with pytest.raises(OSError):
        publish_new_file(stage, tmp_path / "alias" / "target", allow_exclusive_copy=True)
    assert not (tmp_path / "real" / "target").exists()


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EIO, errno.EACCES])
def test_copy_failure_keeps_target_and_no_replay(tmp_path, monkeypatch, exclusive_only, failure):
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"new")
    def fail(fd):
        raise OSError(failure, "injected fsync failure")
    monkeypatch.setattr(file_publish.os, "fsync", fail)
    with pytest.raises(file_publish.ExclusiveCreateUnconfirmed):
        publish_new_file(stage, target, allow_exclusive_copy=True)
    assert target.read_bytes() == b"new"
    assert stage.read_bytes() == b"new"
    with pytest.raises(FileExistsError):
        publish_new_file(stage, target, allow_exclusive_copy=True)


def test_copy_failure_never_unlinks_foreign_replacement(tmp_path, monkeypatch, exclusive_only):
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"new")
    original = file_publish.os.fsync
    def replace(fd):
        original(fd)
        target.unlink()
        target.write_bytes(b"foreign")
    monkeypatch.setattr(file_publish.os, "fsync", replace)
    with pytest.raises(file_publish.ExclusiveCreateUnconfirmed):
        publish_new_file(stage, target, allow_exclusive_copy=True)
    assert target.read_bytes() == b"foreign"


def test_exclusive_move_verifies_new_inode(engine, exclusive_only):
    instance, project, root = engine
    receipt = instance.mutate(project, "source.txt", "new", b"complete")
    moved = instance.move(project, {"path": "source.txt", "destination": "target.txt",
                                   "expected_sha256": receipt["sha256"]})
    assert moved["atomic"] is False
    assert not (root / "source.txt").exists()
    assert (root / "target.txt").read_bytes() == b"complete"


def test_exclusive_import_reports_semantics(engine, exclusive_only):
    from agent.incoming_artifacts import import_artifact
    instance, project, root = engine
    receipt = import_artifact(instance, project, {
        "path": "incoming/data.bin",
        "file": {"download_url": "https://files.oaiusercontent.com/fixture", "size": 9},
    }, stream=[b"complete" + bytes([0])])
    assert receipt["atomic"] is False
    assert receipt["partial_content_visible"] is True
    assert (root / "incoming/data.bin").read_bytes() == b"complete" + bytes([0])

def test_exclusive_copy_exposes_partial_content_before_completion(tmp_path, monkeypatch, exclusive_only):
    stage, target = tmp_path / "stage", tmp_path / "target"
    payload = b"a" * (1024 * 1024 + 4)
    stage.write_bytes(payload)
    original_read = os.read
    observations = []
    def observe(fd, count):
        observations.append(target.read_bytes())
        return original_read(fd, count)
    monkeypatch.setattr(file_publish.os, "read", observe)
    result = publish_new_file(stage, target, allow_exclusive_copy=True)
    assert observations == [b"", payload[:1024 * 1024]]
    assert not result.atomic
    assert target.read_bytes() == payload


def test_exclusive_copy_crash_leaves_partial_target_and_retry_conflicts(tmp_path):
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"x" * (2 * 1024 * 1024))
    program = """
import os, sys
from shared.file_publish import _exclusive_create_copy
original = os.read
calls = 0
def crash(fd, count):
    global calls
    calls += 1
    if calls == 2:
        os._exit(71)
    return original(fd, count)
os.read = crash
_exclusive_create_copy(sys.argv[1], sys.argv[2])
"""
    result = subprocess.run([sys.executable, "-c", program, str(stage), str(target)],
                            cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert result.returncode == 71
    assert target.read_bytes() == b"x" * (1024 * 1024)
    assert stage.stat().st_size == 2 * 1024 * 1024
    with pytest.raises(FileExistsError):
        file_publish._exclusive_create_copy(stage, target)


def test_exclusive_copy_keeps_pinned_parent_after_swap(tmp_path, monkeypatch, exclusive_only):
    parent, outside = tmp_path / "parent", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    stage = parent / "stage"
    stage.write_bytes(b"payload")
    original_read = os.read
    def swap(fd, count):
        parent.rename(tmp_path / "moved")
        parent.symlink_to(outside, target_is_directory=True)
        return original_read(fd, count)
    monkeypatch.setattr(file_publish.os, "read", swap)
    with pytest.raises(file_publish.ExclusiveCreateUnconfirmed):
        publish_new_file(stage, parent / "target", allow_exclusive_copy=True)
    assert not (outside / "target").exists()
    assert (tmp_path / "moved" / "target").read_bytes() == b"payload"


def test_exclusive_failure_engine_receipt_keeps_recovery_fields(engine, monkeypatch, exclusive_only):
    from shared.util import DevError
    from shared.filesystem_errors import safe_filesystem_error_detail
    instance, project, root = engine
    original = file_publish._exclusive_create_copy
    def inject(*args, **kwargs):
        real_fsync = os.fsync
        def failed(fd):
            raise OSError(errno.ENOSPC, "injected")
        monkeypatch.setattr(os, "fsync", failed)
        try:
            return original(*args, **kwargs)
        finally:
            monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(file_publish, "_exclusive_create_copy", inject)
    with pytest.raises(DevError) as caught:
        instance.mutate(project, "target.txt", "new", b"complete")
    assert caught.value.code == "WRITE_OUTCOME_UNCONFIRMED"
    detail = safe_filesystem_error_detail("write", caught.value.code, caught.value.details)
    assert detail["atomic"] is False and detail["partial_content_visible"] is True
    assert detail["mutation_started"] is True and detail["retryable"] is False
    assert detail["path"] == "target.txt" and len(detail["expected_sha256"]) == 64
    assert len(detail["backup_id"]) == 32
    assert (root / "target.txt").read_bytes() == b"complete"
    assert not list(root.glob(".rd-*"))
