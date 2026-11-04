"""Bound recovery payloads without forgetting mutation identities.

Recovery material is atomically retired only when no active/unacknowledged call
exists. A durable private queue separates that decision from slow filesystem
deletion. Failed deletion is retried; retired backups never become readable again.
"""
import json
import re
import stat
import time

DAY = 86400
DEFAULTS = {"call_days": 30, "max_calls": 20000, "backup_days": 30,
            "backups_per_path": 20, "max_backups": 2000,
            "checkpoint_days": 14, "max_checkpoints": 10}
IDENTIFIER = re.compile(r"[a-f0-9]{32}")


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("journal_retention contains unsupported settings")
    result = {**DEFAULTS, **value}
    if any(type(item) is not int or not 1 <= item <= 1000000 for item in result.values()):
        raise ValueError("journal_retention values must be positive bounded integers")
    return result


def _active(journal):
    # Call only while holding journal.lock.
    return journal.db.execute(
        "SELECT 1 FROM calls WHERE acked=0 OR status IN ('accepted','running','retryable') LIMIT 1").fetchone()


def _checkpoint_candidates(journal, now, settings):
    # No journal lock across directory enumeration/stat: slow mounts must not
    # block unrelated SQLite readers or new operation admission.
    archives = []
    for path in journal.checkpoints.glob("*.zip"):
        if not IDENTIFIER.fullmatch(path.stem):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            archives.append((info.st_mtime, path.name))
    archives.sort(reverse=True)
    return [name for index, (modified, name) in enumerate(archives)
            if index >= settings["max_checkpoints"] or modified < now-settings["checkpoint_days"]*DAY][:1000]


def _orphan_candidates(journal, now, settings):
    # add_backup writes its UUID file before inserting metadata; checkpoints
    # use UUID.part until publication. Crashes can strand either artifact.
    result = []
    for kind, directory, pattern, suffix, days in (
            ("orphan_backups", journal.backups, "*", "", settings["backup_days"]),
            ("checkpoint_parts", journal.checkpoints, "*.part", ".part", settings["checkpoint_days"])):
        for path in directory.glob(pattern):
            identifier = path.name[:-len(suffix)] if suffix else path.name
            if not IDENTIFIER.fullmatch(identifier):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime < now-days*DAY:
                if kind == "orphan_backups":
                    with journal.lock:
                        if journal.db.execute("SELECT 1 FROM backups WHERE id=?", (identifier,)).fetchone():
                            continue
                result.append((kind, identifier))
                if len(result) >= 1000:
                    return result
    return result


def _drain_cleanup(journal, removed):
    with journal.lock:
        pending = journal.db.execute(
            "SELECT kind,id FROM recovery_cleanup ORDER BY retired_at,kind,id LIMIT 1000").fetchall()
    for row in pending:
        kind, identifier = row["kind"], row["id"]
        if kind not in {"backups", "checkpoints", "orphan_backups", "checkpoint_parts"} or not IDENTIFIER.fullmatch(identifier):
            continue
        with journal.lock, journal.db:
            if _active(journal):
                return
            if kind == "orphan_backups" and journal.db.execute(
                    "SELECT 1 FROM backups WHERE id=?", (identifier,)).fetchone():
                journal.db.execute("DELETE FROM recovery_cleanup WHERE kind=? AND id=?", (kind, identifier))
                continue
        # This item was already retired atomically. A newly admitted request
        # cannot obtain its backup metadata; checkpoint UUIDs are never reused.
        backup = kind in {"backups", "orphan_backups"}
        directory = journal.backups if backup else journal.checkpoints
        suffix = "" if backup else ".part" if kind == "checkpoint_parts" else ".zip"
        path = directory / (identifier + suffix)
        path.unlink(missing_ok=True)
        with journal.lock, journal.db:
            journal.db.execute("DELETE FROM recovery_cleanup WHERE kind=? AND id=?", (kind, identifier))
        removed["backups" if backup else "checkpoints"] += 1


def prune(journal, *, now=None, **settings):
    settings = validate(settings)
    now = time.time() if now is None else now
    removed = {"calls": 0, "backups": 0, "checkpoints": 0, "cancellations": 0}
    expired = json.dumps({"ok": False, "error": {"code": "RESULT_EXPIRED",
        "message": "操作已完成并确认；详细结果超过保留期，绝不会重新执行此操作"}})
    with journal.lock, journal.db:
        journal.db.execute("CREATE TABLE IF NOT EXISTS recovery_cleanup "
                           "(kind TEXT NOT NULL,id TEXT NOT NULL,retired_at REAL NOT NULL,PRIMARY KEY(kind,id))")
        rows = journal.db.execute(
            "SELECT id FROM calls WHERE acked=1 AND status IN ('done','interrupted') "
            "AND result_expired=0 AND (at<? OR id IN "
            "(SELECT id FROM calls WHERE acked=1 AND status IN ('done','interrupted') "
            "AND result_expired=0 ORDER BY at DESC LIMIT -1 OFFSET ?)) "
            "ORDER BY at LIMIT 1000", (now-settings["call_days"]*DAY, settings["max_calls"])).fetchall()
        for row in rows:
            journal.db.execute("UPDATE calls SET result=?,output='',output_seq=0,result_expired=1 WHERE id=?",
                               (expired, row["id"]))
        removed["calls"] = len(rows)
        removed["cancellations"] = journal.db.execute(
            "DELETE FROM cancellations WHERE at<? AND id IN "
            "(SELECT id FROM calls WHERE result_expired=1 AND acked=1)",
            (now-7*DAY,)).rowcount
        if journal.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_stages'").fetchone():
            for row in rows:
                journal.db.execute("DELETE FROM execution_stages WHERE operation_id=?", (row["id"],))
        if _active(journal):
            return removed

    checkpoints = _checkpoint_candidates(journal, now, settings)
    orphans = _orphan_candidates(journal, now, settings)
    with journal.lock, journal.db:
        # Admission could have happened during filesystem enumeration.
        if _active(journal):
            return removed
        stale = journal.db.execute(
            "SELECT id FROM (SELECT id,at,ROW_NUMBER() OVER (PARTITION BY root,path ORDER BY at DESC) AS n,"
            "ROW_NUMBER() OVER (ORDER BY at DESC) AS total FROM backups) "
            "WHERE at<? OR n>? OR total>? LIMIT 1000",
            (now-settings["backup_days"]*DAY, settings["backups_per_path"], settings["max_backups"])).fetchall()
        for row in stale:
            identifier = row["id"]
            if not IDENTIFIER.fullmatch(identifier):
                continue
            journal.db.execute("INSERT OR IGNORE INTO recovery_cleanup VALUES ('backups',?,?)", (identifier, now))
            journal.db.execute("DELETE FROM backups WHERE id=?", (identifier,))
        for name in checkpoints:
            journal.db.execute("INSERT OR IGNORE INTO recovery_cleanup VALUES ('checkpoints',?,?)", (name[:-4], now))
        for kind, identifier in orphans:
            # Recheck after enumeration: a writer may have installed metadata.
            if kind == "orphan_backups" and journal.db.execute(
                    "SELECT 1 FROM backups WHERE id=?", (identifier,)).fetchone():
                continue
            # A stale normal backup may already have been retired above.
            if kind == "orphan_backups" and journal.db.execute(
                    "SELECT 1 FROM recovery_cleanup WHERE kind='backups' AND id=?", (identifier,)).fetchone():
                continue
            journal.db.execute("INSERT OR IGNORE INTO recovery_cleanup VALUES (?,?,?)", (kind, identifier, now))
    # An unlink/commit error leaves the durable item queued. Missing files count
    # as completed cleanup, allowing recovery after a crash between unlink/commit.
    _drain_cleanup(journal, removed)
    return removed
