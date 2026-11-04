"""Retention keeps SQLite critical sections separate from slow filesystem I/O."""
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import pytest

from agent.journal import Journal
from agent import journal_retention as retention
from shared.util import DevError


@pytest.fixture
def material(tmp_path):
    journal = Journal(tmp_path / "state")
    identifier = uuid.uuid4().hex
    journal.add_backup(identifier, "synthetic", "source.py", b"old", "after")
    old = time.time() - 40 * retention.DAY
    with journal.db:
        journal.db.execute("UPDATE backups SET at=?", (old,))
    checkpoint = journal.checkpoints / (uuid.uuid4().hex + ".zip")
    checkpoint.write_bytes(b"synthetic")
    os.utime(checkpoint, (old, old))
    yield journal, identifier, checkpoint
    journal.db.close()


def assert_reader_can_run(journal):
    completed = threading.Event()
    def read():
        journal.status("synthetic-missing")
        completed.set()
    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    assert completed.wait(1), "filesystem I/O retained the journal lock"
    worker.join()


def test_glob_stat_and_unlink_do_not_hold_journal_lock(material, monkeypatch):
    journal, identifier, checkpoint = material
    old_glob, old_stat, old_unlink = Path.glob, Path.lstat, Path.unlink
    observations = []
    def glob(path, *args, **kwargs):
        if path == journal.checkpoints:
            assert_reader_can_run(journal)
            observations.append("glob")
        return old_glob(path, *args, **kwargs)
    def lstat(path, *args, **kwargs):
        if path == checkpoint:
            assert_reader_can_run(journal)
            observations.append("stat")
        return old_stat(path, *args, **kwargs)
    def unlink(path, *args, **kwargs):
        if path.parent in {journal.backups, journal.checkpoints}:
            assert_reader_can_run(journal)
            observations.append("unlink")
        return old_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 1
    assert set(observations) == {"glob", "stat", "unlink"}


def test_failed_unlink_retains_durable_queue_and_retries(material, monkeypatch):
    journal, identifier, checkpoint = material
    backup = journal.backups / identifier
    original = Path.unlink
    def failing(path, *args, **kwargs):
        if path == backup:
            raise OSError("synthetic slow volume failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", failing)
    with pytest.raises(OSError, match="synthetic"):
        journal.prune()
    assert backup.exists() and checkpoint.exists()
    assert journal.db.execute("SELECT count(*) FROM recovery_cleanup").fetchone()[0] == 2
    with pytest.raises(DevError) as error:
        journal.backup("synthetic", identifier)
    assert error.value.code == "NOT_FOUND"
    monkeypatch.setattr(Path, "unlink", original)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 1
    assert not backup.exists() and not checkpoint.exists()
    assert journal.db.execute("SELECT count(*) FROM recovery_cleanup").fetchone()[0] == 0


def test_new_call_during_enumeration_stops_retirement(material, monkeypatch):
    journal, identifier, checkpoint = material
    original = retention._checkpoint_candidates
    def started(*args):
        result = original(*args)
        journal.start("new-call", {"tool": "fs_write"})
        return result
    monkeypatch.setattr(retention, "_checkpoint_candidates", started)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 0
    assert journal.backup("synthetic", identifier)[1] == b"old"
    assert checkpoint.exists()
    assert journal.db.execute("SELECT count(*) FROM recovery_cleanup").fetchone()[0] == 0


def test_new_unacked_call_defers_queued_file_deletion(material, monkeypatch):
    journal, identifier, checkpoint = material
    original = retention._drain_cleanup
    def started(*args):
        journal.start("new-call", {"tool": "fs_read"})
        return original(*args)
    monkeypatch.setattr(retention, "_drain_cleanup", started)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 0
    assert (journal.backups / identifier).exists() and checkpoint.exists()
    journal.finish("new-call", {"ok": True})
    assert journal.prune()["backups"] == 0
    journal.ack("new-call")
    monkeypatch.setattr(retention, "_drain_cleanup", original)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 1


def test_retirement_transaction_failure_never_unlinks(material):
    journal, identifier, checkpoint = material
    with journal.db:
        journal.db.execute("CREATE TRIGGER fail_retire BEFORE DELETE ON backups "
                           "BEGIN SELECT RAISE(ABORT,'synthetic retirement failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        journal.prune()
    assert journal.backup("synthetic", identifier)[1] == b"old"
    assert checkpoint.exists()
    assert journal.db.execute("SELECT count(*) FROM recovery_cleanup").fetchone()[0] == 0


def orphan(journal, *, partial=False, old=True):
    path = (journal.checkpoints if partial else journal.backups) / (
        uuid.uuid4().hex + (".part" if partial else ""))
    path.write_bytes(b"synthetic orphan, never inspected")
    if old:
        at = time.time() - 40 * retention.DAY
        os.utime(path, (at, at))
    return path


def test_expired_orphans_removed_but_fresh_and_unrecognized_files_survive(material):
    journal, _, _ = material
    old_backup, old_part = orphan(journal), orphan(journal, partial=True)
    fresh_backup, fresh_part = orphan(journal, old=False), orphan(journal, partial=True, old=False)
    unexpected = journal.backups / "not-a-uuid"
    unexpected.write_bytes(b"keep")
    old = time.time() - 40 * retention.DAY
    os.utime(unexpected, (old, old))
    result = journal.prune()
    assert result["backups"] == 2 and result["checkpoints"] == 2
    assert not old_backup.exists() and not old_part.exists()
    assert fresh_backup.exists() and fresh_part.exists() and unexpected.exists()


def test_active_and_unacked_calls_protect_orphan_artifacts(material):
    journal, _, _ = material
    backup, part = orphan(journal), orphan(journal, partial=True)
    journal.start("active-orphan", {"tool": "fs_write"})
    journal.prune()
    assert backup.exists() and part.exists()
    journal.finish("active-orphan", {"ok": True})
    journal.prune()
    assert backup.exists() and part.exists()
    journal.ack("active-orphan")
    journal.prune()
    assert not backup.exists() and not part.exists()


def test_orphan_metadata_added_during_scan_is_rechecked(material, monkeypatch):
    journal, _, _ = material
    backup = orphan(journal)
    original = retention._orphan_candidates
    def new_metadata(*args):
        result = original(*args)
        journal.add_backup(backup.name, "new-owner", "new.py", None, "after")
        return result
    monkeypatch.setattr(retention, "_orphan_candidates", new_metadata)
    journal.prune()
    assert backup.exists()
    assert journal.db.execute("SELECT 1 FROM backups WHERE id=?", (backup.name,)).fetchone()
    assert not journal.db.execute("SELECT 1 FROM recovery_cleanup WHERE id=?", (backup.name,)).fetchone()


def test_orphan_unlink_failure_stays_queued_for_retry(material, monkeypatch):
    journal, _, _ = material
    part = orphan(journal, partial=True)
    original = Path.unlink
    def failed(path, *args, **kwargs):
        if path == part:
            raise OSError("synthetic partial cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", failed)
    with pytest.raises(OSError, match="partial"):
        journal.prune()
    assert part.exists()
    assert journal.db.execute("SELECT 1 FROM recovery_cleanup WHERE kind='checkpoint_parts' AND id=?",
                              (part.stem,)).fetchone()
    monkeypatch.setattr(Path, "unlink", original)
    journal.prune()
    assert not part.exists()


def test_missing_queued_file_is_recoverable_after_unlink_commit_crash(material, monkeypatch):
    journal, identifier, checkpoint = material
    original = retention._drain_cleanup
    monkeypatch.setattr(retention, "_drain_cleanup", lambda *args: None)
    journal.prune()
    (journal.backups / identifier).unlink()
    checkpoint.unlink()
    monkeypatch.setattr(retention, "_drain_cleanup", original)
    result = journal.prune()
    assert result["backups"] == result["checkpoints"] == 1
    assert journal.db.execute("SELECT count(*) FROM recovery_cleanup").fetchone()[0] == 0
