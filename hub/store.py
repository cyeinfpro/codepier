from __future__ import annotations
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from cryptography.fernet import Fernet

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, info TEXT NOT NULL DEFAULT '{}', last_seen REAL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, alias TEXT NOT NULL, alias_key TEXT UNIQUE NOT NULL, device_id TEXT NOT NULL REFERENCES devices(id), root TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'write', allow_tasks INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS vps_connections (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535), username TEXT NOT NULL, secret TEXT NOT NULL, host_key_policy TEXT NOT NULL DEFAULT 'strict', provider TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '', system TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1, connection_revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(host,port,username));
CREATE TABLE IF NOT EXISTS vps_projects (vps_id TEXT NOT NULL REFERENCES vps_connections(id) ON DELETE CASCADE, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, binding_id TEXT NOT NULL, PRIMARY KEY(vps_id,project_id));
CREATE INDEX IF NOT EXISTS vps_project_lookup ON vps_projects(project_id,vps_id);
CREATE TABLE IF NOT EXISTS access_profiles (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), label TEXT NOT NULL, label_key TEXT NOT NULL, scopes TEXT NOT NULL, projects TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), version INTEGER NOT NULL CHECK(version>0), created REAL NOT NULL, updated REAL NOT NULL, create_key TEXT NOT NULL, create_fingerprint TEXT NOT NULL, UNIQUE(user_id,label_key), UNIQUE(user_id,create_key));
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL, client_id TEXT, scopes TEXT NOT NULL, projects TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id), kind TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_requests (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, state TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, resource TEXT NOT NULL, grant_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, device_id TEXT, project_id TEXT, actor TEXT NOT NULL, grant_id TEXT, tool TEXT NOT NULL, args_summary TEXT NOT NULL, fingerprint TEXT NOT NULL, idem TEXT, state TEXT NOT NULL, result TEXT, error TEXT, output TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS op_idem ON operations(actor, idem) WHERE idem IS NOT NULL;
CREATE INDEX IF NOT EXISTS op_recent ON operations(created DESC);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_recent ON audit(at DESC);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, source TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, at REAL NOT NULL, elapsed_ms INTEGER, detail TEXT NOT NULL DEFAULT '{}', UNIQUE(operation_id,source,seq));
CREATE INDEX IF NOT EXISTS operation_events_lookup ON operation_events(operation_id,id);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL, grant_id TEXT, actor TEXT NOT NULL, root TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, source_operation_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS artifacts_scope ON artifacts(grant_id,project_id,created DESC,id DESC);
CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL, root TEXT NOT NULL, actor TEXT NOT NULL, grant_id TEXT, title TEXT NOT NULL, goal TEXT NOT NULL, template TEXT NOT NULL, state TEXT NOT NULL, steps TEXT NOT NULL, summary TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_recent ON workflows(created DESC,id DESC);
CREATE INDEX IF NOT EXISTS workflow_scope ON workflows(grant_id,project_id,state,created DESC,id DESC);
CREATE TABLE IF NOT EXISTS workflow_events (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL REFERENCES workflows(id), action TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL, at REAL NOT NULL, version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_event_recent ON workflow_events(workflow_id,id DESC);
CREATE TABLE IF NOT EXISTS workflow_replays (actor TEXT NOT NULL, idem TEXT NOT NULL, fingerprint TEXT NOT NULL, workflow_id TEXT NOT NULL REFERENCES workflows(id), receipt TEXT NOT NULL, PRIMARY KEY(actor,idem));
"""

class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.db = sqlite3.connect(self.directory / "hub.sqlite3", check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            # Changing a new database to WAL can return BUSY without invoking
            # SQLite's busy handler when another opener is doing the same.
            deadline = time.monotonic() + 30
            while True:
                try:
                    self.db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self.db.execute("PRAGMA foreign_keys=ON")
            with self.db:
                # Serialize the reads that decide which migrations/key creation
                # are needed with all writes made by other Store instances.
                self.db.execute("BEGIN IMMEDIATE")
                self._migrate()
                self.cipher = self._load_cipher()
            os.chmod(self.directory / "hub.sqlite3", 0o600)
        except BaseException:
            self.db.close()
            raise

    def _load_cipher(self):
        key_path = self.directory / "master.key"
        if not key_path.exists():
            for table, column in (("devices", "secret"), ("operations", "payload"), ("vps_connections", "secret")):
                columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if column in columns and self.db.execute(
                    f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1"
                ).fetchone():
                    raise RuntimeError("master.key is missing for an encrypted Hub database; restore the matching master.key from backup")
            fd, temporary = tempfile.mkstemp(prefix=".master-key-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Publish a complete, durable file without replacing an
                    # existing key, including one installed by an administrator.
                    os.link(temporary, key_path)
                except FileExistsError:
                    pass
                if os.name != "nt":
                    directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                os.unlink(temporary)
        return Fernet(key_path.read_bytes().strip())

    def _migrate(self):
        # Execute DDL within the caller's transaction; executescript would
        # implicitly commit it before applying the schema.
        for statement in SCHEMA.split(";"):
            if statement.strip() and not statement.lstrip().startswith("PRAGMA"):
                self.db.execute(statement)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', '1')")
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        if version not in {"1", "2", "3", "4", "5", "6"}:
            raise RuntimeError(f"Unsupported Hub database schema version: {version}")
        # Additive migration: v1 databases and their audit history remain readable.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(operations)")}
        additions = {
            "payload": "TEXT", "journal_id": "TEXT", "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt": "REAL NOT NULL DEFAULT 0", "deadline": "REAL",
            "accepted_at": "REAL", "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "output_seq": "INTEGER NOT NULL DEFAULT 0", "transport_error": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE operations ADD COLUMN {name} {definition}")
        grant_columns = {r[1] for r in self.db.execute("PRAGMA table_info(grants)")}
        if "resource" not in grant_columns:
            self.db.execute("ALTER TABLE grants ADD COLUMN resource TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS op_pending ON operations(state,next_attempt)")
        if "profile_id" not in grant_columns:
            self.db.execute("ALTER TABLE grants ADD COLUMN profile_id TEXT REFERENCES access_profiles(id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS grants_profile ON grants(profile_id)")
        self.db.execute("UPDATE meta SET value='6' WHERE key='schema'")

    def execute(self, sql: str, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args)

    def one(self, sql: str, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def encrypt(self, value: str):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str):
        return self.cipher.decrypt(value.encode()).decode()

    def audit(self, actor: str, action: str, target: str = "", status: str = "ok", detail=None):
        self.execute("INSERT INTO audit(at,actor,action,target,status,detail) VALUES (?,?,?,?,?,?)",
                     (time.time(), actor, action, target, status, json.dumps(detail or {}, ensure_ascii=False)))

    def close(self):
        with self.lock:
            self.db.close()
