"""Synthetic NAS semantics: no replay after unconfirmed saves, bounded metadata I/O."""
import errno
import json
import os
import zipfile
from pathlib import Path

import pytest

import agent.filesystem as filesystem
from agent.filesystem import FileEngine
from agent.journal import Journal
from shared.crypto import digest
from shared.util import DevError


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.txt").write_bytes(b"original\r\n")
    journal = Journal(tmp_path / "state")
    e = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}]},
                   journal, tmp_path / "private" / "config.json")
    yield e, {"root": str(root), "mode": "write"}, root
    journal.db.close()


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_mutation_verifies_named_result_and_qualifies_atomicity(engine, operation):
    e, project, root = engine
    path = "new.txt" if operation == "create" else "source.txt"
    before = "new" if operation == "create" else digest(b"original\r\n")
    after = None if operation == "delete" else b"updated\r\n"
    receipt = e.mutate(project, path, before, after)
    assert receipt["content_verified"] is True
    assert receipt["atomicity_scope"] == "filesystem_primitive"
    assert receipt["continuous_visibility_guaranteed"] is False
    assert (root / path).read_bytes() == after if after is not None else not (root / path).exists()


@pytest.mark.parametrize("failure", ["enoent", "eio", "wrong_bytes", "same_bytes_new_inode", "symlink"])
def test_published_but_unconfirmed_write_is_never_replayed(engine, monkeypatch, failure):
    e, project, root = engine
    target = root / "source.txt"
    real_replace, real_open, real_read = os.replace, os.open, os.read
    published = False
    replacements = 0
    reads = 0

    def replacing(src, dest, *args, **kwargs):
        nonlocal published, replacements
        replacements += 1
        real_replace(src, dest, *args, **kwargs)
        published = True
        if failure == "wrong_bytes":
            target.write_bytes(b"external edit")
        elif failure == "symlink":
            other = root / "other.txt"
            other.write_bytes(b"updated\r\n")
            target.unlink()
            target.symlink_to(other)

    def opening(path, *args, **kwargs):
        if published and Path(path) == target and failure in {"enoent", "eio"}:
            raise OSError(errno.ENOENT if failure == "enoent" else errno.EIO, "synthetic NAS failure")
        return real_open(path, *args, **kwargs)

    def reading(fd, amount):
        nonlocal reads
        data = real_read(fd, amount)
        if published and failure == "same_bytes_new_inode":
            reads += 1
            if reads == 1:
                other = root / "competitor"
                other.write_bytes(b"updated\r\n")
                real_replace(other, target)
        return data

    monkeypatch.setattr(filesystem.os, "replace", replacing)
    monkeypatch.setattr(filesystem.os, "open", opening)
    monkeypatch.setattr(filesystem.os, "read", reading)
    with pytest.raises(DevError) as caught:
        e.mutate(project, "source.txt", digest(b"original\r\n"), b"updated\r\n")
    error = caught.value
    assert error.code == "WRITE_OUTCOME_UNCONFIRMED"
    assert error.details["retryable"] is False and error.details["mutation_started"] is True
    assert error.details["expected_sha256"] == digest(b"updated\r\n")
    assert replacements == 1
    assert e.journal.backup(str(root), error.details["backup_id"])[1] == b"original\r\n"


def test_delete_recreated_during_directory_sync_requires_review(engine, monkeypatch):
    e, project, root = engine
    def sync(directory, **kwargs):
        (root / "source.txt").write_bytes(b"another writer")
    monkeypatch.setattr(e, "sync_directory", sync)
    with pytest.raises(DevError) as caught:
        e.mutate(project, "source.txt", digest(b"original\r\n"), None)
    assert caught.value.code == "WRITE_OUTCOME_UNCONFIRMED"
    assert (root / "source.txt").read_bytes() == b"another writer"


def test_excluded_names_are_filtered_before_stat_but_still_count_toward_budget(engine, monkeypatch):
    e, project, root = engine
    excluded = {".build", ".git", ".env", "excluded"}
    for name in excluded:
        (root / name).mkdir()
    real_scandir = os.scandir
    stats = []

    class Entry:
        def __init__(self, item):
            self.item, self.path = item, item.path
        def stat(self, **kwargs):
            stats.append(self.item.name)
            assert self.item.name not in excluded
            return self.item.stat(**kwargs)

    class Scan:
        def __init__(self, directory):
            self.iterator = real_scandir(directory)
        def __enter__(self):
            return (Entry(item) for item in self.iterator)
        def __exit__(self, *args):
            self.iterator.close()

    monkeypatch.setattr(filesystem.os, "scandir", Scan)
    assert [rel for _, rel, _ in e.walk(root, root, exclude=lambda rel: rel == "excluded")] == ["source.txt"]
    assert stats == ["source.txt"]
    monkeypatch.setattr(filesystem, "MAX_SCAN_ENTRIES", 1)
    with pytest.raises(DevError) as caught:
        list(e.walk(root, root, exclude=lambda rel: rel == "excluded"))
    assert caught.value.code == "SCAN_LIMIT"


@pytest.mark.parametrize("directory", [".build", ".next", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".turbo"])
def test_build_outputs_are_not_scanned_or_checkpointed_but_explicit_reads_work(engine, directory):
    e, project, root = engine
    generated = root / directory
    generated.mkdir()
    (generated / "cache.txt").write_bytes(b"generated")
    assert [rel for _, rel, _ in e.walk(root, root)] == ["source.txt"]
    assert e.read(project, {"path": directory + "/cache.txt"})["content"] == "generated"
    receipt = e.checkpoint(project, {"label": "source only"})
    with zipfile.ZipFile(receipt["local_archive"]) as archive:
        assert "files/source.txt" in archive.namelist()
        assert not any(name.startswith("files/" + directory + "/") for name in archive.namelist())


def test_tracked_generated_files_are_excluded_from_git_search(engine):
    import subprocess
    e, project, root = engine
    generated = root / ".build"
    generated.mkdir()
    (generated / "cache.txt").write_bytes(b"generated")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "--", ".build", "source.txt"],
                   check=True, capture_output=True)
    rows, capped = e.git_candidates(root, root)
    assert not capped and [rel for _, rel, _ in rows] == ["source.txt"]


def test_invalid_generated_name_never_enters_checkpoint_manifest(engine, monkeypatch):
    e, project, root = engine
    real_scandir = os.scandir

    class InvalidEntry:
        path = str(root / "invalid\udcff.tsbuildinfo")
        def stat(self, **kwargs):
            pytest.fail("nonportable filename reached metadata I/O")

    class Scan:
        def __init__(self, directory):
            self.iterator = real_scandir(directory)
        def __enter__(self):
            return iter([InvalidEntry(), *self.iterator])
        def __exit__(self, *args):
            self.iterator.close()

    monkeypatch.setattr(filesystem.os, "scandir", Scan)
    receipt = e.checkpoint(project, {"label": "portable"})
    with zipfile.ZipFile(receipt["local_archive"]) as archive:
        assert "files/source.txt" in archive.namelist()
        assert json.loads(archive.read("manifest.json"))["skipped"] == []
