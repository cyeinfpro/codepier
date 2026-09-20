"""Filesystem fault and boundary regressions; all data lives in tmp_path."""
import errno
import contextlib
import json
import os
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.filesystem as filesystem
from agent.filesystem import FileEngine, MAX_FILE
from agent.journal import Journal
from shared.contracts import TOOLS
from shared.crypto import digest
from shared.util import DevError


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_bytes(b"original\n")
    config_path = tmp_path / "private" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")
    journal = Journal(tmp_path / "state")
    config = {"allowed_roots": [{"path": str(root), "writable": True}]}
    yield FileEngine(config, journal, config_path), {"root": str(root), "mode": "write"}, root
    journal.db.close()


def search_args(**overrides):
    return TOOLS["fs_search"].model(project="test", query="needle", **overrides).model_dump()


def move_args():
    return {"path": "src/app.py", "destination": "src/moved.py", "expected_sha256": digest(b"original\n")}


def test_replace_all_rejects_allocation_before_building_oversized_result(engine, monkeypatch):
    e, project, root = engine
    original = b"a" * MAX_FILE
    (root / "src/app.py").write_bytes(original)

    class NeverAllocateReplacement(str):
        def replace(self, *args, **kwargs):
            pytest.fail("replacement allocation happened before its byte budget check")

    monkeypatch.setattr(e, "text", lambda data: NeverAllocateReplacement(data.decode()))
    with pytest.raises(DevError, match="1 MiB"):
        e.edit(project, {"path": "src/app.py", "expected_sha256": digest(original),
                         "edits": [{"old_text": "a", "new_text": "b" * MAX_FILE, "replace_all": True}]})
    assert (root / "src/app.py").read_bytes() == original
    assert e.journal.history(str(root)) == []


def test_edit_counts_utf8_bytes_before_replacement(engine):
    e, project, root = engine
    original = b"a" * (MAX_FILE // 2)
    (root / "src/app.py").write_bytes(original)
    with pytest.raises(DevError, match="1 MiB"):
        e.edit(project, {"path": "src/app.py", "expected_sha256": digest(original),
                         "edits": [{"old_text": "a", "new_text": "中", "replace_all": True}]})
    assert (root / "src/app.py").read_bytes() == original


@pytest.mark.parametrize("content", ["contains\x00nul", "unpaired\ud800"])
@pytest.mark.parametrize("tool", ["fs_preview", "fs_write"])
def test_invalid_text_is_rejected_before_preview_or_write(engine, content, tool):
    e, project, root = engine
    with pytest.raises(DevError) as exc:
        e.call(tool, project, {"path": "src/new.py", "content": content, "expected_sha256": "new"})
    assert exc.value.code == "INVALID_CONTENT"
    assert not (root / "src/new.py").exists()
    assert e.journal.history(str(root)) == []


def test_direct_mutation_rejects_non_utf8_before_backup(engine):
    e, project, root = engine
    with pytest.raises(DevError) as exc:
        e.mutate(project, "src/app.py", digest(b"original\n"), b"\xff")
    assert exc.value.code == "ENCODING"
    assert (root / "src/app.py").read_bytes() == b"original\n"
    assert e.journal.history(str(root)) == []


def test_read_rejects_mixed_snapshot_from_in_place_writer(engine, monkeypatch):
    e, project, root = engine
    target = root / "src/app.py"
    target.write_bytes(b"a" * 200000)
    real_read = os.read
    first = True

    def changing_read(fd, amount):
        nonlocal first
        block = real_read(fd, amount)
        if first:
            first = False
            target.write_bytes(b"b" * 200000)
        return block

    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(DevError) as exc:
        e.read(project, {"path": "src/app.py"})
    assert exc.value.code == "FILE_CHANGED"


def test_move_disk_failure_does_not_publish_partial_destination(engine, monkeypatch):
    e, project, root = engine
    real_fsync = os.fsync
    calls = 0

    def full_disk(fd):
        nonlocal calls
        if stat.S_ISREG(os.fstat(fd).st_mode):
            calls += 1
        if calls == 2:  # The first flush persists the pre-image backup.
            raise OSError(errno.ENOSPC, "disk full")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(OSError, match="disk full"):
        e.move(project, move_args())
    assert (root / "src/app.py").read_bytes() == b"original\n"
    assert not (root / "src/moved.py").exists()
    assert not list((root / "src").glob(".rd-*"))
    backup = e.journal.history(str(root))[0]
    assert e.journal.backup(str(root), backup["id"])[1] == b"original\n"


def test_move_source_conflict_preserves_concurrent_local_destination(engine, monkeypatch):
    e, project, root = engine
    real_fsync = os.fsync
    calls = 0

    def concurrent_save(fd):
        nonlocal calls
        if stat.S_ISREG(os.fstat(fd).st_mode):
            calls += 1
        real_fsync(fd)
        if calls == 2 and stat.S_ISREG(os.fstat(fd).st_mode):
            (root / "src/app.py").write_bytes(b"local source")
            (root / "src/moved.py").write_bytes(b"local destination")

    monkeypatch.setattr(os, "fsync", concurrent_save)
    with pytest.raises(DevError) as exc:
        e.move(project, move_args())
    assert exc.value.code == "SHA_CONFLICT"
    assert (root / "src/app.py").read_bytes() == b"local source"
    assert (root / "src/moved.py").read_bytes() == b"local destination"
    assert not list((root / "src").glob(".rd-*"))


@pytest.mark.parametrize("source_changes", [False, True])
@pytest.mark.parametrize("replace_inode", [False, True])
def test_move_preserves_source_and_target_if_destination_changes_after_publish(engine, monkeypatch, source_changes, replace_inode):
    e, project, root = engine
    real_link = os.link

    def concurrent_save(src, dest, *args, **kwargs):
        real_link(src, dest, *args, **kwargs)
        if source_changes:
            (root / "src/app.py").write_bytes(b"local source")
        if replace_inode:
            replacement = root / "src/editor.tmp"
            replacement.write_bytes(b"local destination")
            os.replace(replacement, dest)
        else:
            Path(dest).write_bytes(b"local destination")

    monkeypatch.setattr(os, "link", concurrent_save)
    with pytest.raises(DevError) as exc:
        e.move(project, move_args())
    assert exc.value.code == ("SHA_CONFLICT" if source_changes else "DESTINATION_CHANGED")
    assert (root / "src/app.py").read_bytes() == (b"local source" if source_changes else b"original\n")
    assert (root / "src/moved.py").read_bytes() == b"local destination"
    assert (root / "src/moved.py").stat().st_nlink == 1


def test_move_preserves_executable_mode_and_publishes_full_file(engine, monkeypatch):
    e, project, root = engine
    source = root / "src/app.py"
    source.chmod(0o751)
    real_link = os.link

    def inspect_publication(src, dest, *args, **kwargs):
        assert Path(src).read_bytes() == b"original\n"
        assert not Path(dest).exists()
        return real_link(src, dest, *args, **kwargs)

    monkeypatch.setattr(os, "link", inspect_publication)
    e.move(project, move_args())
    assert not source.exists()
    assert (root / "src/moved.py").read_bytes() == b"original\n"
    assert stat.S_IMODE((root / "src/moved.py").stat().st_mode) == 0o751
    assert (root / "src/moved.py").stat().st_nlink == 1


def test_overlapping_project_roots_cannot_both_win_same_sha(engine, monkeypatch):
    e, project, root = engine
    entered_replace = threading.Event()
    release_replace = threading.Event()
    second_started = threading.Event()
    second_done = threading.Event()
    real_replace = os.replace

    def pause_first_publication(src, dest):
        if Path(dest) == root / "src/app.py" and not entered_replace.is_set():
            entered_replace.set()
            assert release_replace.wait(3)
        return real_replace(src, dest)

    def second_write():
        second_started.set()
        try:
            return e.mutate({**project, "root": str(root / "src")}, "app.py", digest(b"original\n"), b"second")
        finally:
            second_done.set()

    monkeypatch.setattr(os, "replace", pause_first_publication)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(e.mutate, project, "src/app.py", digest(b"original\n"), b"first")
        assert entered_replace.wait(3)
        second = workers.submit(second_write)
        try:
            assert second_started.wait(3)
            assert not second_done.wait(0.1)
        finally:
            release_replace.set()
        assert first.result(timeout=3)["sha256"] == digest(b"first")
        with pytest.raises(DevError) as exc:
            second.result(timeout=3)
    assert exc.value.code == "SHA_CONFLICT"
    assert (root / "src/app.py").read_bytes() == b"first"


def test_mkdir_cannot_create_missing_readonly_ancestors(engine):
    e, project, root = engine
    readonly = root / "absent"
    writable = readonly / "allowed"
    e.config["allowed_roots"].extend([
        {"path": str(readonly), "writable": False},
        {"path": str(writable), "writable": True},
    ])
    with pytest.raises(DevError) as exc:
        e.call("fs_mkdir", project, {"path": "absent/allowed/new"})
    assert exc.value.code == "READ_ONLY"
    assert not readonly.exists()


def test_deep_search_and_checkpoint_do_not_silently_stop_at_100_levels(engine):
    e, project, root = engine
    deep = root.joinpath(*(["d"] * 105))
    deep.mkdir(parents=True)
    source = deep / "deep.py"
    source.write_bytes(b"needle")
    rel = source.relative_to(root).as_posix()
    result = e.search(project, search_args())
    assert not result["truncated"]
    assert [match["path"] for match in result["matches"]] == [rel]
    checkpoint = e.checkpoint(project, {"label": "deep source"})
    with zipfile.ZipFile(checkpoint["local_archive"]) as archive:
        assert archive.read("files/" + rel) == b"needle"


def test_directory_only_and_excluded_trees_cannot_bypass_scan_budget(engine, monkeypatch):
    e, project, root = engine
    for index in range(12):
        (root / f"empty{index}").mkdir()
    monkeypatch.setattr(filesystem, "MAX_SCAN_ENTRIES", 5)
    result = e.search(project, search_args(file_glob="*.unmatched"))
    assert result["truncated"] and result["scan_error"] == "SCAN_LIMIT"
    tree = e.tree(project, TOOLS["fs_tree"].model(project="test").model_dump())
    assert tree["truncated"] and tree["next_offset"] is None
    with pytest.raises(DevError) as exc:
        e.checkpoint(project, {"label": "limited"})
    assert exc.value.code == "CHECKPOINT_LIMIT"
    assert not list(e.journal.checkpoints.iterdir())


def test_unreadable_directory_is_reported_and_checkpoint_is_not_published(engine, monkeypatch):
    e, project, root = engine
    real_scandir = os.scandir

    def denied(path):
        if Path(path) == root / "src":
            raise PermissionError("directory unavailable")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    result = e.search(project, search_args())
    assert result["truncated"] and result["scan_error"] == "SCAN_FAILED"
    with pytest.raises(DevError) as exc:
        e.checkpoint(project, {"label": "unreadable source"})
    assert exc.value.code == "SCAN_FAILED"
    assert not list(e.journal.checkpoints.iterdir())


def test_checkpoint_preserves_executable_mode(engine):
    e, project, root = engine
    (root / "src/app.py").chmod(0o755)
    result = e.checkpoint(project, {"label": "permissions"})
    with zipfile.ZipFile(result["local_archive"]) as archive:
        member = archive.getinfo("files/src/app.py")
        assert stat.S_IMODE(member.external_attr >> 16) == 0o755
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["files"][0]["mode"] == 0o755


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_history_restore_preserves_deleted_or_moved_executable_mode(engine, operation):
    e, project, root = engine
    source = root / "src/app.py"
    source.chmod(0o751)
    if operation == "move":
        changed = e.move(project, move_args())
    else:
        changed = e.mutate(project, "src/app.py", digest(b"original\n"), None)
    e.call("history_restore", project, {"backup_id": changed["backup_id"], "expected_sha256": "new"})
    assert source.read_bytes() == b"original\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o751


def test_history_restore_applies_mode_even_if_contents_already_match(engine):
    e, project, root = engine
    source = root / "src/app.py"
    source.chmod(0o751)
    deleted = e.mutate(project, "src/app.py", digest(b"original\n"), None)
    source.write_bytes(b"original\n")
    source.chmod(0o644)
    restored = e.call("history_restore", project, {"backup_id": deleted["backup_id"],
                                                  "expected_sha256": digest(b"original\n")})
    assert stat.S_IMODE(source.stat().st_mode) == 0o751
    assert restored["backup_id"]
    e.call("history_restore", project, {"backup_id": restored["backup_id"], "expected_sha256": restored["sha256"]})
    assert stat.S_IMODE(source.stat().st_mode) == 0o644


def test_read_many_bounds_json_expansion_even_for_first_item(engine):
    e, project, root = engine
    (root / "controls.txt").write_bytes(b"\x01" * MAX_FILE)
    result = e.call("fs_read_many", project, {"paths": ["controls.txt", "src/app.py"], "max_lines": 1000})
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 2 * MAX_FILE
    assert result["files"][0]["error"] == "BATCH_ITEM_TOO_LARGE"
    assert result["files"][1]["content"] == "original\n"


def test_diff_reordered_small_file_is_bounded_before_sequence_matching(monkeypatch):
    lines = [f"{number}\n" for number in range(20000)]
    before = "".join(lines).encode()
    after = "".join(lines[index ^ 1] for index in range(len(lines))).encode()

    def never_match(*args, **kwargs):
        pytest.fail("quadratic diff was started without checking its work budget")

    monkeypatch.setattr(filesystem.difflib, "SequenceMatcher", never_match)
    result = FileEngine.diff("reordered.txt", before, after)
    assert result["diff_truncated"] and result["line_counts_approximate"]


def test_diff_large_file_small_edit_keeps_precise_line_numbers():
    before = [f"line {index}\n" for index in range(50000)]
    after = before[:]
    after[40000] = "changed\n"
    result = FileEngine.diff("large.txt", "".join(before).encode(), "".join(after).encode())
    assert not result["diff_truncated"]
    assert result["added_lines"] == result["removed_lines"] == 1
    assert "@@ -39998,7 +39998,7 @@" in result["diff"]
    assert "-line 40000\n+changed\n" in result["diff"]


@pytest.mark.parametrize("before,after,header", [(None, b"new\n", "@@ -0,0 +1,1 @@"),
                                               (b"old\n", None, "@@ -1,1 +0,0 @@")])
def test_diff_empty_ranges_use_valid_unified_diff_headers(before, after, header):
    assert header in FileEngine.diff("file.txt", before, after)["diff"]


def test_move_failed_source_deletion_rolls_back_only_unchanged_destination(engine, monkeypatch):
    e, project, root = engine
    real_unlink = Path.unlink

    def locked_source(path, *args, **kwargs):
        if path == root / "src/app.py":
            raise PermissionError("source is locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_source)
    with pytest.raises(PermissionError, match="source is locked"):
        e.move(project, move_args())
    assert (root / "src/app.py").read_bytes() == b"original\n"
    assert not (root / "src/moved.py").exists()
    assert not list((root / "src").glob(".rd-*"))


def test_write_directory_flush_failure_requires_review_and_keeps_backup(engine, monkeypatch):
    e, project, root = engine

    def fail_directory_flush(directory):
        raise OSError(errno.EIO, "directory flush failed")

    monkeypatch.setattr(filesystem, "fsync_directory", fail_directory_flush)
    with pytest.raises(DevError) as exc:
        e.mutate(project, "src/app.py", digest(b"original\n"), b"new content")
    assert exc.value.code == "DURABILITY_UNCONFIRMED"
    assert (root / "src/app.py").read_bytes() == b"new content"
    backup = e.journal.history(str(root))[0]
    assert e.journal.backup(str(root), backup["id"])[1] == b"original\n"


def test_move_persists_destination_before_source_deletion(engine, monkeypatch):
    e, project, root = engine
    (root / "target").mkdir()
    observed = []

    def record_sync(directory):
        observed.append((directory, (root / "src/app.py").exists(), (root / "target/moved.py").exists()))

    monkeypatch.setattr(filesystem, "fsync_directory", record_sync)
    e.move(project, {**move_args(), "destination": "target/moved.py"})
    assert observed[0] == (root / "target", True, True)
    assert observed[1] == (root / "src", False, True)


def test_checkpoint_file_flush_failure_does_not_publish_archive(engine, monkeypatch):
    e, project, root = engine

    def fail_flush(fd):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "fsync", fail_flush)
    with pytest.raises(OSError, match="disk full"):
        e.checkpoint(project, {"label": "failed flush"})
    assert not list(e.journal.checkpoints.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte filenames")
def test_unsupported_filename_does_not_break_json_or_checkpoint(engine, monkeypatch):
    e, project, root = engine
    (root / "colon:name.txt").write_bytes(b"needle")
    (root / "good.txt").write_bytes(b"needle")
    try:
        with open(os.fsencode(root) + b"/invalid-\xff.txt", "wb") as stream:
            stream.write(b"needle")
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        # APFS rejects these names; simulate Linux's surrogateescaped DirEntry.
        real_scandir = os.scandir
        entry = SimpleNamespace(path=str(root / "invalid-\udcff.txt"),
                                stat=lambda **kwargs: (root / "good.txt").stat())

        @contextlib.contextmanager
        def with_invalid_name(path):
            with real_scandir(path) as entries:
                yield iter([*entries, entry]) if Path(path) == root else entries

        monkeypatch.setattr(os, "scandir", with_invalid_name)
    tree = e.tree(project, TOOLS["fs_tree"].model(project="test").model_dump())
    assert not tree["truncated"]
    json.dumps(tree, ensure_ascii=False).encode("utf-8")
    result = e.search(project, search_args())
    assert [match["path"] for match in result["matches"]] == ["good.txt"]
    checkpoint = e.checkpoint(project, {"label": "portable names"})
    with zipfile.ZipFile(checkpoint["local_archive"]) as archive:
        assert archive.read("files/good.txt") == b"needle"
        assert all("invalid" not in name and "colon:" not in name for name in archive.namelist())
