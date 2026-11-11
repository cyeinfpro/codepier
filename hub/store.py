from __future__ import annotations
import json
from contextlib import contextmanager, nullcontext
import os
import sqlite3
import tempfile
import asyncio
from anyio import CancelScope
from contextvars import copy_context
from functools import partial
from hub.store_contract import CheckedConnection, OwnedRLock
from hub.worker_metrics import WorkerMetrics
from hub.store_metrics import LockMetrics, TransactionMetrics
from hub.store_worker import StoreWorker
import time
import threading
from concurrent.futures import ThreadPoolExecutor
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
CREATE TABLE IF NOT EXISTS access_roles (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), label TEXT NOT NULL, label_key TEXT NOT NULL, policy TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), version INTEGER NOT NULL CHECK(version>0), created REAL NOT NULL, updated REAL NOT NULL, create_key TEXT NOT NULL, create_fingerprint TEXT NOT NULL, UNIQUE(user_id,label_key), UNIQUE(user_id,create_key));
CREATE TABLE IF NOT EXISTS role_created_projects (role_id TEXT NOT NULL REFERENCES access_roles(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, created REAL NOT NULL, PRIMARY KEY(role_id,project_id));
CREATE TABLE IF NOT EXISTS access_profiles (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), label TEXT NOT NULL, label_key TEXT NOT NULL, scopes TEXT NOT NULL, projects TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), version INTEGER NOT NULL CHECK(version>0), created REAL NOT NULL, updated REAL NOT NULL, create_key TEXT NOT NULL, create_fingerprint TEXT NOT NULL, UNIQUE(user_id,label_key), UNIQUE(user_id,create_key));
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL, client_id TEXT, scopes TEXT NOT NULL, projects TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id), kind TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_requests (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, state TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, resource TEXT NOT NULL, grant_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, device_id TEXT, project_id TEXT, actor TEXT NOT NULL, grant_id TEXT, tool TEXT NOT NULL, args_summary TEXT NOT NULL, fingerprint TEXT NOT NULL, idem TEXT, state TEXT NOT NULL, result TEXT, error TEXT, output TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);

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
    def __init__(self, directory: str | Path, *, read_workers=3, initialize_host_contract=False):
        if type(read_workers) is not int or not 2 <= read_workers <= 4:
            raise ValueError("read_workers must be between 2 and 4")
        self._reader_state = threading.local()
        self._read_connections = []
        self._read_connections_lock = threading.Lock()
        self.directory = Path(directory).resolve()
        from hub.state_layout import state_directory

        self.state_directory = state_directory(self.directory)
        from hub.collaboration.delivery_compatibility import check_database as check_delivery_path
        from hub.collaboration.delivery_compatibility import raise_floor_in_transaction

        check_delivery_path(self.state_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        from shared.hub_data_contract import check_database, check_path, initialize_fresh_contract
        from shared.util import VERSION

        check_path(self.state_directory, VERSION)
        connection = sqlite3.connect(
            self.state_directory / "hub.sqlite3", check_same_thread=False, timeout=30
        )
        connection.row_factory = sqlite3.Row
        self.lock_metrics = LockMetrics()
        self._writer_lock = OwnedRLock(on_release=self.lock_metrics.observe)
        self.iam_enabled = True
        self.session_revision = 0
        self._event_loop = None
        self.transaction_metrics = TransactionMetrics()
        self._writer_db = CheckedConnection(
            connection,
            self.lock,
            self._security_write,
            on_transaction=self.transaction_metrics.observe,
        )
        self._executor = StoreWorker()
        self.worker_metrics = WorkerMetrics()
        with self.lock:
            try:
                # Changing a new database to WAL can return BUSY without invoking
                # SQLite's busy handler when another opener is doing the same.
                deadline = time.monotonic() + 30
                while True:
                    try:
                        self.db.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if (
                            getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY
                            or time.monotonic() >= deadline
                        ):
                            raise
                        time.sleep(0.05)
                self.db.execute("PRAGMA foreign_keys=OFF")
                with self.db:
                    # Serialize the reads that decide which migrations/key creation
                    # are needed with all writes made by other Store instances.
                    self.db.execute("BEGIN IMMEDIATE")
                    check_database(self.db, VERSION)
                    fresh = (
                        initialize_host_contract
                        and not self.db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                        ).fetchone()
                    )
                    self._migrate()
                    if fresh:
                        initialize_fresh_contract(self.db, VERSION)
                    if check_database(self.db, VERSION):
                        raise_floor_in_transaction(self.db)
                    self.cipher = self._load_cipher()
                    if self.db.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Hub migration has invalid foreign-key relationships")
                self.db.execute("PRAGMA foreign_keys=ON")
                os.chmod(self.state_directory / "hub.sqlite3", 0o600)
            except BaseException:
                self.db.close()
                raise

        self._read_executor = ThreadPoolExecutor(
            max_workers=read_workers, thread_name_prefix="hub-sqlite-read"
        )
        self.read_worker_metrics = WorkerMetrics()

    @property
    def lock(self):
        binding = getattr(self._reader_state, "binding", None)
        return binding[0] if binding else self._writer_lock

    @property
    def db(self):
        binding = getattr(self._reader_state, "binding", None)
        return binding[1] if binding else self._writer_db

    def _read_phase(self, call):
        # One connection belongs to each worker. No transaction, authorization
        # memo or connection binding survives this synchronous phase.
        binding = getattr(self._reader_state, "connection", None)
        if binding is None:
            connection = sqlite3.connect(
                (self.state_directory / "hub.sqlite3").as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=30,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA cache_size=-2048")
            lock = OwnedRLock()
            binding = (lock, CheckedConnection(connection, lock))
            self._reader_state.connection = binding
            with self._read_connections_lock:
                self._read_connections.append(binding)
        self._reader_state.binding = binding
        try:
            for attempt in range(4):
                # Discard audits from invalidated snapshots. Only the final
                # accepted/failed phase produces externally visible records.
                self._reader_state.denial_audits = []
                # A security write during the snapshot invalidates the entire
                # decision. Under sustained churn make the last pass while
                # briefly fencing the writer, rather than return stale authority.
                fence = self._writer_lock if attempt == 3 else nullcontext()
                with fence:
                    revision = self.session_revision
                    with self.lock:
                        version = self.db.execute("PRAGMA data_version").fetchone()[0]
                    with self.lock, self.db:
                        self.db.execute("BEGIN")
                        value = call()
                    with self.lock:
                        current_version = self.db.execute("PRAGMA data_version").fetchone()[0]
                    if revision == self.session_revision and version == current_version:
                        return value
            from shared.util import DevError

            raise DevError(
                "AUTHORIZATION_CHANGED",
                "Authorization changed during read; retry the read request",
                503,
                retry_after=1,
            )
        finally:
            audits = getattr(self._reader_state, "denial_audits", [])
            del self._reader_state.binding
            del self._reader_state.denial_audits
            # Both the reader transaction and final writer fence are released.
            # Keep this admitted worker alive until its audit write settles, so
            # cancellation and close drain the read and necessary audit together.
            if audits:

                def flush():
                    for context, call in audits:
                        context.run(call)

                self._executor.submit(flush).result()

    def defer_read_denial(self, actor, target, detail):
        """Collect role denials only; never allow arbitrary writes in readers."""
        if getattr(self._reader_state, "binding", None) is None:
            return False
        self._reader_state.denial_audits.append(
            (
                copy_context(),
                partial(self.audit, actor, "role.denied", target, "denied", dict(detail)),
            )
        )
        return True

    def _load_cipher(self):
        key_path = self.state_directory / "master.key"
        if not key_path.exists():
            from hub.keyring import CIPHER_COLUMNS, PREFIXED_CIPHER_COLUMNS

            for table, column in CIPHER_COLUMNS:
                columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if (
                    column in columns
                    and self.db.execute(
                        f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND {column}!='' LIMIT 1"
                    ).fetchone()
                ):
                    raise RuntimeError(
                        "master.key is missing for an encrypted Hub database; restore the matching master.key from backup"
                    )
            for table, column, prefix in PREFIXED_CIPHER_COLUMNS:
                columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if (
                    column in columns
                    and self.db.execute(
                        f"SELECT 1 FROM {table} WHERE {column} LIKE ? LIMIT 1", (prefix + "%",)
                    ).fetchone()
                ):
                    raise RuntimeError(
                        "master.key is missing for an encrypted Hub database; restore the matching master.key from backup"
                    )
            fd, temporary = tempfile.mkstemp(prefix=".master-key-", dir=self.state_directory)
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
                    directory_fd = os.open(
                        self.state_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                os.unlink(temporary)
        from hub.keyring import CipherSuite, private_read

        return CipherSuite(self.state_directory, private_read(key_path).strip())

    def _migrate(self):
        # Execute DDL within the caller's transaction; executescript would
        # implicitly commit it before applying the schema.
        for statement in SCHEMA.split(";"):
            if statement.strip() and not statement.lstrip().startswith("PRAGMA"):
                self.db.execute(statement)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', '1')")
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        from hub.collaboration.delivery_compatibility import (
            PROTECTED_SCHEMA_VERSION,
            assert_compatible,
        )

        assert_compatible(self.db)
        if version not in {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "10",
            PROTECTED_SCHEMA_VERSION,
        }:
            raise RuntimeError(f"Unsupported Hub database schema version: {version}")
        # Additive migration: v1 databases and their audit history remain readable.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(operations)")}
        additions = {
            "payload": "TEXT",
            "journal_id": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt": "REAL NOT NULL DEFAULT 0",
            "deadline": "REAL",
            "accepted_at": "REAL",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "output_seq": "INTEGER NOT NULL DEFAULT 0",
            "transport_error": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE operations ADD COLUMN {name} {definition}")
        grant_columns = {r[1] for r in self.db.execute("PRAGMA table_info(grants)")}
        if "resource" not in grant_columns:
            self.db.execute("ALTER TABLE grants ADD COLUMN resource TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS op_pending ON operations(state,next_attempt)")
        if "profile_id" not in grant_columns:
            self.db.execute(
                "ALTER TABLE grants ADD COLUMN profile_id TEXT REFERENCES access_profiles(id)"
            )
        self.db.execute("CREATE INDEX IF NOT EXISTS grants_profile ON grants(profile_id)")
        if "authorization_mode" not in grant_columns:
            self.db.execute(
                "ALTER TABLE grants ADD COLUMN authorization_mode TEXT NOT NULL DEFAULT 'fixed' CHECK(authorization_mode IN ('fixed','role'))"
            )
        if "role_id" not in grant_columns:
            self.db.execute(
                "ALTER TABLE grants ADD COLUMN role_id TEXT REFERENCES access_roles(id)"
            )
        profile_columns = {r[1] for r in self.db.execute("PRAGMA table_info(access_profiles)")}
        if "role_id" not in profile_columns:
            self.db.execute(
                "ALTER TABLE access_profiles ADD COLUMN role_id TEXT REFERENCES access_roles(id)"
            )
        self.db.execute("CREATE INDEX IF NOT EXISTS profiles_role ON access_profiles(role_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS grants_role ON grants(role_id)")
        from hub.iam_schema import migrate

        migrate(self.db)
        from hub.gateway.schema import migrate as migrate_gateway

        migrate_gateway(self.db)
        from hub.mcp_tasks_schema import migrate as migrate_tasks

        migrate_tasks(self.db)
        from hub.collaboration.schema import migrate as migrate_collaboration

        migrate_collaboration(self.db)
        from hub.auth_schema import migrate as migrate_auth

        migrate_auth(self.db)
        from hub.operation_output import migrate as migrate_output

        migrate_output(self.db)
        from hub.operation_history import migrate as migrate_history

        migrate_history(self.db)
        from hub.grant_expiry import migrate as migrate_grant_expiry

        migrate_grant_expiry(self.db)

    @contextmanager
    def transaction(self, *, immediate=True):
        """Join the checked outer context; any nested failure is rollback-only."""
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield self.db

    @contextmanager
    def savepoint(self):
        """Opt-in local rollback; requires an enclosing active transaction."""
        with self.lock:
            with self.db.savepoint() as db:
                yield db

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

    def audit(
        self,
        actor: str,
        action: str,
        target: str = "",
        status: str = "ok",
        detail=None,
        *,
        commit=True,
        target_kind=None,
    ):
        from hub.iam import audit_context

        context = audit_context.get()
        # Context is trusted only for its originating Store AND actor. A task
        # can outlive the HTTP request, and direct callers may use another Store.
        scoped = bool(context and context[0] is self and context[1] == actor)
        sid, uid = context[2:] if scoped else ("legacy", None)
        if not scoped and actor.startswith("mcp:"):
            grant = self.one(
                "SELECT space_id,user_id FROM grants WHERE id=?", (actor.split(":", 2)[1],)
            )
            if grant:
                sid, uid = grant["space_id"], grant["user_id"]
        elif actor.startswith("panel:") and uid is None:
            user = self.one("SELECT id FROM users WHERE username=?", (actor[6:],))
            if user:
                uid = user["id"]
        # Do not retarget a scoped caller's audit entry using an untrusted ID
        # from a denied cross-Space request. Also avoid loading operation bodies
        # just to audit a thin status read.
        tables = {
            "operation": "operations",
            "project": "projects",
            "device": "devices",
            "profile": "access_profiles",
            "role": "access_roles",
            "workflow": "workflows",
            "artifact": "artifacts",
            "grant": "grants",
            "vps": "vps_connections",
        }
        if target_kind is not None and target_kind not in tables:
            raise ValueError("Unknown audit target kind")
        # A verified caller scope (including MCP) takes precedence even when the
        # target is an ID in another Space. Free-form names/URLs never cause lookup.
        if target_kind and target and not scoped and not actor.startswith("mcp:"):
            row = self.one(
                f"SELECT space_id,owner_user_id FROM {tables[target_kind]} WHERE id=?", (target,)
            )
            if row:
                sid = row["space_id"]
        sql = "INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?,?)"
        args = (
            time.time(),
            actor,
            action,
            target,
            status,
            json.dumps(detail or {}, ensure_ascii=False),
            sid,
            uid,
        )
        if commit:
            self.execute(sql, args)
        else:
            self.db.execute(sql, args)

    def _security_write(self, sql):
        # Conservative invalidation is safe even if the transaction rolls back.
        # No SQL text or bound value is retained in this process-local revision.
        import re

        if re.search(
            r"\b(?:sessions|session_security|users|iam_users|spaces|memberships|membership_blocks|external_identities|oidc_providers|access_roles|access_profiles|role_assignments|role_created_projects|grants|tokens|projects|devices|gateway_[a-z_]+)\b",
            sql,
            re.I,
        ):
            self.session_revision += 1

    def after_commit(self, callback):
        if self.lock.owned:
            self.db.after_commit(callback)
        else:
            callback()

    def require_transaction(self):
        self.lock.require()
        if not self.db.depth:
            raise RuntimeError("This helper requires an enclosing Store database context")

    async def run(self, function, /, *args, **kwargs):
        """Run a whole synchronous DB phase on this Store's single worker.

        Cancellation waits for the admitted phase to settle before releasing the
        caller's async admission lock. No transaction or thread lock crosses an
        await; cancellation is not a claim that a durable write did not happen.
        """
        return await self._run(function, args, kwargs, control=False)

    async def run_read(self, function, /, *args, **kwargs):
        """Explicit pure read phase on a fresh WAL snapshot; SQL writes fail closed.

        Rechecked from scratch after each await. Callers must not perform
        network I/O or non-database side effects inside this retryable phase.
        """
        return await self._run(function, args, kwargs, control=False, read_only=True)

    async def run_control(self, function, /, *args, **kwargs):
        """Internal authenticated Agent receipt/final-send-fence path only.

        No request parameter selects this lane. Normal phases make progress
        after a bounded control burst; running work is never interrupted.
        """
        return await self._run(function, args, kwargs, control=True)

    async def _run(self, function, args, kwargs, *, control, read_only=False):
        if self.lock.owned:
            raise RuntimeError("Do not await Store.run while holding Store.lock")
        loop = asyncio.get_running_loop()
        self._event_loop = loop
        context = copy_context()
        call = partial(function, *args, **kwargs)
        metrics = self.read_worker_metrics if read_only else self.worker_metrics
        submitted = metrics.submit()

        def measured():
            started = metrics.start(submitted)
            failed = False
            try:
                return context.run(self._read_phase, call) if read_only else context.run(call)
            except BaseException:
                failed = True
                raise
            finally:
                metrics.finish(started, failed=failed)

        try:
            submit = (
                self._read_executor.submit
                if read_only
                else self._executor.submit_control
                if control
                else self._executor.submit
            )
            future = asyncio.wrap_future(submit(measured), loop=loop)
        except BaseException:
            metrics.reject()
            raise
        return await self._settle(future)

    @staticmethod
    async def _settle(future):
        """Cancellation cannot detach an admitted phase or database close."""
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # ASGI task groups use level cancellation: a cancelled parent scope
            # cancels every await, including asyncio.shield(). Shield that scope
            # while draining the admitted job, otherwise disconnects busy-loop.
            # Direct Task.cancel() still reaches us and is drained by the loop.
            # Never abandon the worker or release admission before it settles.
            with CancelScope(shield=True):
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            # Retrieve a failing job's exception without masking cancellation.
            if not future.cancelled():
                future.exception()
            raise

    def stats_snapshot(self):
        """Internal bounded process metrics; never queries or mutates SQLite.

        Each group is consistent under its own lock. This aggregate is not a
        tenant endpoint and must not be exposed as a user's room statistics.
        """
        return {
            "schema_version": 1,
            "scope": "hub_process",
            "read_only": True,
            "worker": self.worker_metrics.snapshot(),
            "read_worker": self.read_worker_metrics.snapshot(),
            "lock": self.lock_metrics.snapshot(),
            "transactions": self.transaction_metrics.snapshot(),
        }

    async def aclose(self):
        await self._settle(asyncio.create_task(asyncio.to_thread(self.close)))

    def close(self):
        if self.lock.owned:
            raise RuntimeError("Release Store.lock before closing the database worker")
        self._read_executor.shutdown(wait=True, cancel_futures=False)
        self._executor.shutdown(wait=True, cancel_futures=False)
        for lock, db in self._read_connections:
            with lock:
                db.close()
        with self.lock:
            self.db.close()
