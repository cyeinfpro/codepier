"""Private native-terminal spool. No terminal content belongs in operation/audit logs."""

from __future__ import annotations
from contextlib import contextmanager
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from shared.native_contracts import canonical_jcs

LIVE = {"starting", "running", "stopping", "orphaned"}
SESSION_QUOTA = 128 * 1024 * 1024
TOTAL_QUOTA = 1024 * 1024 * 1024
ATTACHMENT_CAP = 20 * 1024 * 1024
MAX_CONTROL_EVENT_BYTES = 256 * 1024


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid native session/receipt identifier")
    return value


def _migrate_v4(db):
    db.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL,
      root TEXT NOT NULL, cwd TEXT NOT NULL, provider TEXT NOT NULL, title TEXT NOT NULL,
      status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
      size INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, error TEXT NOT NULL DEFAULT '',
      heartbeat REAL NOT NULL DEFAULT 0, lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
      argv TEXT NOT NULL DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS output (session TEXT NOT NULL, offset INTEGER NOT NULL, data BLOB NOT NULL,
      PRIMARY KEY(session,offset));
    CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, root TEXT NOT NULL,
      name TEXT NOT NULL, size INTEGER NOT NULL, sha TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0,
      ready INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL);
    """)
    columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    for name, declaration in {
        "worker_pid": "INTEGER NOT NULL DEFAULT 0",
        "child_pid": "INTEGER NOT NULL DEFAULT 0",
        "launch_signature": "TEXT NOT NULL DEFAULT ''",
        "mode": "TEXT NOT NULL DEFAULT 'terminal'",
        "native_thread": "TEXT NOT NULL DEFAULT ''",
        "chat_settings": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN " + name + " " + declaration)
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_files (session TEXT, file TEXT, PRIMARY KEY(session,file))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS native_commands_pending ON commands(session,state)")
    db.execute("CREATE INDEX IF NOT EXISTS native_attachments_project ON attachments(project_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS native_security (
        kind TEXT NOT NULL CHECK(kind IN ('session','file')), id TEXT NOT NULL,
        owner TEXT NOT NULL, space TEXT NOT NULL, PRIMARY KEY(kind,id))""")
    db.execute("PRAGMA user_version=4")
    db.commit()


def _migrate_v5(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        for name, declaration in {
            "provider_instance_id": "TEXT NOT NULL DEFAULT ''",
            "worker_epoch": "INTEGER NOT NULL DEFAULT 1",
            "workspace_version": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN " + name + " " + declaration)
        # executescript() implicitly commits a pending transaction. Run each
        # complete statement on this connection to keep ALTERs, DDL and version atomic.
        statement = ""
        for line in _V5_SCHEMA_SQL.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError("Incomplete v5 migration statement")
        db.execute("PRAGMA user_version=5")
        db.commit()
    except BaseException:
        db.rollback()
        raise


_V5_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS native_turns (
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  current_attempt INTEGER NOT NULL CHECK (current_attempt IN (0,1)),
  retry_of_receipt TEXT,
  retry_of_attempt TEXT,
  provider_instance_id TEXT NOT NULL,
  native_thread_id TEXT,
  native_turn_id TEXT,
  workspace_key TEXT NOT NULL,
  workspace_version INTEGER NOT NULL,
  owner_epoch INTEGER NOT NULL,
  state_version INTEGER NOT NULL DEFAULT 0,
  send_phase TEXT NOT NULL CHECK (send_phase IN ('not_sent','write_started','accepted','uncertain')),
  execution_state TEXT NOT NULL CHECK (
    execution_state IN ('preparing','running','completed','failed','interrupted','uncertain','cancelled')
  ),
  evidence_state TEXT NOT NULL CHECK (
    evidence_state IN (
      'baseline_pending','baseline_ready','baseline_unavailable',
      'capture_pending','capturing','frozen','partial','unavailable','not_requested'
    )
  ),
  review_state TEXT NOT NULL CHECK (
    review_state IN ('pending','building','ready','unavailable','not_requested','cancelled')
  ),
  before_ref TEXT,
  after_ref TEXT,
  review_ref TEXT,
  terminal_event_id TEXT UNIQUE,
  terminal_at REAL,
  capture_started_at REAL,
  capture_finished_at REAL,
  coverage_json TEXT,
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (receipt, attempt_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS native_turn_one_current
  ON native_turns(receipt) WHERE current_attempt = 1;
CREATE INDEX IF NOT EXISTS native_turn_recovery
  ON native_turns(session_id, execution_state, evidence_state);

CREATE TABLE IF NOT EXISTS native_review_jobs (
  id TEXT PRIMARY KEY,
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('capture_after','build_review')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','blocked','succeeded','failed','cancelled')),
  fence INTEGER NOT NULL DEFAULT 0,
  owner TEXT,
  owner_epoch INTEGER,
  lease_until REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at REAL NOT NULL,
  claimed_at REAL,
  last_error TEXT,
  blocked_reason TEXT,
  recovery_deadline REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (receipt, attempt_id, kind)
);
CREATE INDEX IF NOT EXISTS native_review_claimable
  ON native_review_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS native_control_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  spool_start INTEGER NOT NULL,
  spool_end INTEGER NOT NULL,
  appended_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS native_control_by_session
  ON native_control_events(session_id, seq);

CREATE TABLE IF NOT EXISTS native_workspace_claims (
  claim_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  write_mode INTEGER NOT NULL CHECK (write_mode IN (0,1)),
  stable INTEGER NOT NULL CHECK (stable IN (0,1)),
  holder_kind TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('baseline','execute','capture','restore','released','unknown')),
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  owner_epoch INTEGER NOT NULL,
  workspace_version INTEGER NOT NULL,
  process_identity TEXT,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS native_workspace_live_claims
  ON native_workspace_claims(namespace, resource_kind, phase);

CREATE TABLE IF NOT EXISTS native_child_writers (
  parent_receipt TEXT NOT NULL,
  parent_attempt_id TEXT NOT NULL,
  writer_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','finished','failed','aborted')),
  process_identity TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (parent_receipt, parent_attempt_id, writer_id)
);

CREATE TABLE IF NOT EXISTS native_writer_gates (
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  admission_closed INTEGER NOT NULL DEFAULT 0 CHECK (admission_closed IN (0,1)),
  updated_at REAL NOT NULL,
  PRIMARY KEY (receipt, attempt_id)
);

CREATE TABLE IF NOT EXISTS native_restore_journals (
  restore_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  receipt TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  checkpoint_ref TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('native_rewind','replacement_session','files_only','unsupported')),
  stage TEXT NOT NULL CHECK (
    stage IN (
      'prepared','provider_rewind_applied','replacement_thread_prepared',
      'files_restored','runtime_binding_committed','completed','partial','uncertain'
    )
  ),
  target_provider_instance_id TEXT NOT NULL DEFAULT '',
  target_native_thread_id TEXT,
  replacement_session_id TEXT,
  expected_workspace_version INTEGER NOT NULL DEFAULT 0,
  new_workspace_version INTEGER,
  plan_json TEXT NOT NULL,
  results_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS native_restore_by_session
  ON native_restore_journals(session_id, stage);
"""


def database(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Native spool cannot be a symlink")
    os.chmod(directory, 0o700)
    path = directory / "native.sqlite3"
    if path.is_symlink():
        raise ValueError("Native database cannot be a symlink")
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA secure_delete=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 4:
        _migrate_v4(db)
        version = 4
    if version < 5:
        _migrate_v5(db)
    os.chmod(path, 0o600)
    return db


def process_exists(pid):
    """Conservative observation only; never signal a potentially reused PID."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is NOT a liveness query on Windows; non-console
        # signals map to TerminateProcess there. Observe a process HANDLE.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Unknown/access denied stays busy.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WorkerLock:
    """Cross-process ownership of one session, independent of Agent transport."""

    def __init__(self, directory, sid):
        folder = Path(directory) / "locks"
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise ValueError("Unsafe worker lock directory")
        self.fd = os.open(
            folder / (identifier(sid) + ".lock"),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if os.name == "nt":
                import msvcrt

                # A one-byte CRT lock is released when the fd/process closes.
                # Locking is nonblocking, so a stopped worker never blocks Hub IO.
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def worker_present(directory, sid):
    try:
        with WorkerLock(directory, sid):
            return False
    except (BlockingIOError, PermissionError, OSError):
        # An unreadable/contended lock is not evidence that the worker is dead.
        return True


@contextmanager
def immediate(conn: sqlite3.Connection):
    """Acquire an immediate SQLite write transaction without nesting."""
    if conn.in_transaction:
        raise RuntimeError("unexpected nested native transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def append_spool_bytes_tx(
    conn: sqlite3.Connection,
    session_id: str,
    raw: bytes,
    *,
    now: float,
) -> tuple[int, int]:
    """Append raw bytes inside an active transaction, re-reading persisted tail size."""
    if not conn.in_transaction:
        raise RuntimeError("append_spool_bytes_tx requires an active transaction")
    session_id = identifier(session_id)
    row = conn.execute("SELECT size FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown native session: {session_id}")
    offset = int(row["size"])
    if not raw:
        return offset, offset
    for start in range(0, len(raw), 65536):
        conn.execute(
            "INSERT INTO output(session, offset, data) VALUES (?,?,?)",
            (session_id, offset + start, raw[start : start + 65536]),
        )
    end_offset = offset + len(raw)
    conn.execute(
        "UPDATE sessions SET size=?, updated=? WHERE id=?",
        (end_offset, now, session_id),
    )
    return offset, end_offset


def _serialize_wire_event(
    *,
    event_id: str,
    schema_version: int,
    session_id: str,
    receipt: str,
    attempt_id: str,
    worker_epoch: int,
    kind: str,
    payload: Mapping[str, Any],
    wire_event: Mapping[str, Any] | None,
) -> bytes:
    if wire_event is None:
        envelope: dict[str, Any] = {
            "schema_version": schema_version,
            "session_id": session_id,
            "worker_epoch": worker_epoch,
            "event_id": event_id,
            "run_id": attempt_id,
            "receipt": receipt,
            "kind": kind,
            "body": dict(payload),
        }
    else:
        envelope = dict(wire_event)
    raw = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_CONTROL_EVENT_BYTES:
        raise ValueError("Control event exceeds bounded wire event size")
    return raw


def append_control_tx(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    schema_version: int = 1,
    session_id: str,
    receipt: str,
    attempt_id: str,
    worker_epoch: int,
    kind: str,
    payload: Mapping[str, Any],
    wire_event: Mapping[str, Any] | None = None,
    now: float,
) -> tuple[int, int]:
    """Idempotently append a control fact and its spool wire line inside one transaction."""
    if not conn.in_transaction:
        raise RuntimeError("append_control_tx requires an active transaction")
    session_id = identifier(session_id)
    receipt = identifier(receipt)
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("Invalid control event_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("Invalid control attempt_id")
    if not isinstance(kind, str) or not kind:
        raise ValueError("Invalid control event kind")
    payload_json = canonical_jcs(dict(payload))
    if len(payload_json.encode("utf-8")) > MAX_CONTROL_EVENT_BYTES:
        raise ValueError("Control payload exceeds bounded size")

    existing = conn.execute(
        """SELECT session_id, receipt, attempt_id, kind, payload_json, spool_start, spool_end
           FROM native_control_events WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if existing is not None:
        existing_key = (
            existing["session_id"],
            existing["receipt"],
            existing["attempt_id"],
            existing["kind"],
            existing["payload_json"],
        )
        if existing_key != (session_id, receipt, attempt_id, kind, payload_json):
            raise ValueError(f"Conflicting control event for event_id={event_id}")
        return int(existing["spool_start"]), int(existing["spool_end"])

    raw = _serialize_wire_event(
        event_id=event_id,
        schema_version=schema_version,
        session_id=session_id,
        receipt=receipt,
        attempt_id=attempt_id,
        worker_epoch=worker_epoch,
        kind=kind,
        payload=payload,
        wire_event=wire_event,
    )
    spool_start, spool_end = append_spool_bytes_tx(conn, session_id, raw, now=now)
    conn.execute(
        """INSERT INTO native_control_events(
             event_id, schema_version, session_id, receipt, attempt_id,
             kind, payload_json, spool_start, spool_end, appended_at, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            schema_version,
            session_id,
            receipt,
            attempt_id,
            kind,
            payload_json,
            spool_start,
            spool_end,
            now,
            now,
        ),
    )
    return spool_start, spool_end


def retained_bytes(db):
    """Logical retained bytes, not SQLite's reusable free pages or WAL copies."""
    output = db.execute("SELECT COALESCE(sum(size),0) FROM sessions").fetchone()[0]
    commands = db.execute(
        "SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) FROM commands"
    ).fetchone()[0]
    uploads = db.execute("SELECT COALESCE(sum(size),0) FROM attachments").fetchone()[0]
    control = db.execute(
        "SELECT COALESCE(sum(length(CAST(payload_json AS BLOB))),0) FROM native_control_events"
    ).fetchone()[0]
    turns = db.execute(
        "SELECT COALESCE(sum(length(CAST(COALESCE(coverage_json,'') AS BLOB))),0) FROM native_turns"
    ).fetchone()[0]
    restores = db.execute(
        "SELECT COALESCE(sum(length(CAST(plan_json AS BLOB)) + length(CAST(results_json AS BLOB))),0) FROM native_restore_journals"
    ).fetchone()[0]
    return output + commands + uploads + control + turns + restores


def public(row):
    result = {
        k: row[k]
        for k in (
            "id",
            "project_id",
            "device_id",
            "root",
            "cwd",
            "provider",
            "title",
            "status",
            "created",
            "updated",
            "size",
            "exit_code",
            "error",
        )
    }
    for key, default in (("mode", "terminal"), ("native_thread", ""), ("chat_settings", "{}")):
        result[key] = row[key] if key in row.keys() else default
    return result


def launch_argv(executable, provider, options, attachment_paths=()):
    """argv only. Preserve native options; forbid cwd overrides and implicit modes."""
    argv = [executable]
    if options.get("resume"):
        argv += ["resume"] if provider == "codex" else ["--resume"]
    for key in ("model", "provider", "effort"):
        value = options.get(key, "")
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid model/provider/effort")
        if not value:
            continue
        if key == "model":
            argv += ["--model", value]
        elif key == "provider":
            if provider == "claude":
                raise ValueError("Claude uses its native provider configuration")
            argv += (
                ["-c", "model_provider=" + json.dumps(value)]
                if provider == "codex"
                else ["--provider", value]
            )
        else:
            allowed = {"minimal", "low", "medium", "high", "xhigh"} | (
                {"off", "max"} if provider == "pi" else {"none"}
            )
            if provider == "claude":
                allowed = {"low", "medium", "high", "xhigh", "max"}
            if value not in allowed:
                raise ValueError("Unsupported thinking level; use native selector")
            argv += (
                ["-c", "model_reasoning_effort=" + json.dumps(value)]
                if provider == "codex"
                else ["--effort" if provider == "claude" else "--thinking", value]
            )
    extra = options.get("argv", [])
    if (
        not isinstance(extra, list)
        or len(extra) > 80
        or any(not isinstance(a, str) or len(a) > 4096 or "\x00" in a for a in extra)
    ):
        raise ValueError("Advanced arguments must be a JSON argv array")
    # Full flags remain available, including extension-defined flags. cwd is always mapping-derived.
    for a in extra:
        if a in {"-C", "--cd", "--cwd"} or a.startswith(("--cd=", "--cwd=", "-C")):
            raise ValueError("Working directory is controlled by project mapping")
    argv += extra
    if provider == "claude" and attachment_paths:
        raise ValueError("Use structured Claude chat to attach images or files")
    if provider == "codex":
        for path in attachment_paths:
            argv += ["--image", str(path)]
    else:
        argv += ["@" + str(path) for path in attachment_paths]
    return argv
