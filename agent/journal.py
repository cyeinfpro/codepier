from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
import uuid
from pathlib import Path
from shared.crypto import digest
from shared.util import DevError, fsync_directory

MAX_INTERRUPTED_READS = 3
TERMINAL_CACHE_LIMIT = 2048


class Journal:
    def __init__(self, state_dir: Path):
        self.directory = state_dir.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.backups = self.directory / "backups"
        self.backups.mkdir(exist_ok=True)
        self.checkpoints = self.directory / "checkpoints"
        self.checkpoints.mkdir(exist_ok=True)
        self.db = sqlite3.connect(
            self.directory / "agent.sqlite3", timeout=30.0, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        with self.db:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=30000;
            PRAGMA temp_store=MEMORY;
            CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT, acked INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL, existed INTEGER NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL, at REAL NOT NULL);
            """)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS cancellations (id TEXT PRIMARY KEY, at REAL NOT NULL)"
            )
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
            for name, definition in {
                "tool": "TEXT",
                "output": "TEXT NOT NULL DEFAULT ''",
                "output_seq": "INTEGER NOT NULL DEFAULT 0",
                "recovery_attempts": "INTEGER NOT NULL DEFAULT 0",
                "result_expired": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")
            backup_columns = {r[1] for r in self.db.execute("PRAGMA table_info(backups)")}
            if "before_mode" not in backup_columns:
                self.db.execute("ALTER TABLE backups ADD COLUMN before_mode INTEGER")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS calls_retention ON calls(result_expired,acked,status,at DESC)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS calls_outbox ON calls(at) WHERE status IN ('done','interrupted') AND acked=0"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS backups_root_at ON backups(root,at DESC)")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS backups_root_path_at ON backups(root,path,at DESC)"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('journal_id',?)", (uuid.uuid4().hex,)
            )
            # accepted means the command was durably received but never started.
            self.db.execute("UPDATE calls SET status='retryable' WHERE status='accepted'")
            from shared.contracts import TOOLS
            from shared.integration_contracts import INCOMING_UPLOAD_TOOLS

            # These byte-ingress mutations have their own durable offset/identity
            # state machine. Replay only this explicit set, never general writes.
            reads = [
                name
                for name, tool in TOOLS.items()
                if tool.scope == "read" or name in INCOMING_UPLOAD_TOOLS
            ]
            # Only count reads that actually began. A poison request must not
            # kill every replacement Agent indefinitely; preserve its receipt.
            self.db.execute(
                "UPDATE calls SET recovery_attempts=recovery_attempts+1 WHERE status='running' AND tool IN (%s)"
                % ",".join("?" for _ in reads),
                reads,
            )
            self.db.execute(
                "UPDATE calls SET status='interrupted',acked=0,result=? WHERE status='running' AND tool IN (%s) AND recovery_attempts>=?"
                % ",".join("?" for _ in reads),
                [
                    json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "code": "AGENT_RESTART_LIMIT",
                                "message": f"此只读请求已连续 {MAX_INTERRUPTED_READS} 次在 Agent 退出时未完成；已停止自动重试，请检查文件与日志后再发起新请求",
                            },
                        }
                    ),
                    *reads,
                    MAX_INTERRUPTED_READS,
                ],
            )
            self.db.execute(
                "UPDATE calls SET status='retryable' WHERE status='running' AND tool IN (%s)"
                % ",".join("?" for _ in reads),
                reads,
            )
            self.db.execute(
                "UPDATE calls SET status='interrupted',acked=0,result=? WHERE status='running'",
                (
                    json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "code": "INTERRUPTED",
                                "message": "Agent 进程在操作执行期间停止；网络重连不会重跑它。请检查文件或测试进程后再决定下一步。",
                            },
                        }
                    ),
                ),
            )
        self.journal_id = self.db.execute(
            "SELECT value FROM meta WHERE key='journal_id'"
        ).fetchone()[0]
        os.chmod(self.directory / "agent.sqlite3", 0o600)
        self.active_states: dict[str, dict] = {}
        self.terminal_statuses: OrderedDict[str, str] = OrderedDict()
        self.cancelled_ids: set[str] = set()
        self._cancelled_at: dict[str, float] = {}
        for row in self.db.execute(
            "SELECT id,status,result,output,output_seq FROM calls "
            "WHERE status IN ('accepted','running','retryable')"
        ):
            self.active_states[row["id"]] = {
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "output": row["output"] or "",
                "output_seq": row["output_seq"] or 0,
            }
        for row in self.db.execute(
            "SELECT id,status FROM calls WHERE status IN ('done','interrupted') "
            "AND result_expired=0 ORDER BY at DESC LIMIT ?",
            (TERMINAL_CACHE_LIMIT,),
        ).fetchall()[::-1]:
            self._remember_terminal(row["id"], row["status"])
        for row in self.db.execute("SELECT id,at FROM cancellations"):
            self.cancelled_ids.add(row["id"])
            self._cancelled_at[row["id"]] = row["at"]

    def _remember_terminal(self, identifier, status):
        # Caller holds _state_lock (or is initializing the private journal).
        self.terminal_statuses[identifier] = status
        self.terminal_statuses.move_to_end(identifier)
        if len(self.terminal_statuses) > TERMINAL_CACHE_LIMIT:
            self.terminal_statuses.popitem(last=False)

    def forget_retired_terminals(self, identifiers):
        with self._state_lock:
            for identifier in identifiers:
                self.terminal_statuses.pop(identifier, None)

    def prune(self, **settings):
        from agent.journal_retention import prune

        result = prune(self, **settings)
        if result.get("cancellations"):
            with self.lock:
                current = {r[0] for r in self.db.execute("SELECT id FROM cancellations")}
            now = time.time()
            with self._state_lock:
                for identifier in list(self.cancelled_ids - current):
                    if (
                        identifier not in self.active_states
                        and now - self._cancelled_at.get(identifier, 0) > 60
                    ):
                        self.cancelled_ids.discard(identifier)
                        self._cancelled_at.pop(identifier, None)
        return result

    def meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def start(self, id: str, request: dict):
        fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
        new_active = None
        done_status = None
        done_result = None
        with self.lock, self.db:
            old = self.db.execute("SELECT * FROM calls WHERE id=?", (id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise DevError("IDEMPOTENCY_CONFLICT", "同一个操作 ID 不能承载不同请求")
                if old["status"] in {"running", "accepted"}:
                    raise DevError("ALREADY_RUNNING", "该操作已接收或正在执行")
                if old["status"] == "retryable":
                    self.db.execute(
                        "UPDATE calls SET status='accepted',result=NULL,acked=0 WHERE id=?", (id,)
                    )
                    new_active = {
                        "status": "accepted",
                        "result": None,
                        "output": old["output"] or "",
                        "output_seq": old["output_seq"] or 0,
                    }
                else:
                    done_status = old["status"]
                    done_result = json.loads(old["result"])
            else:
                tool = request.get("tool")
                self.db.execute(
                    "INSERT INTO calls(id,fingerprint,status,at,tool) VALUES (?,?,'accepted',?,?)",
                    (id, fingerprint, time.time(), tool if isinstance(tool, str) else None),
                )
                new_active = {
                    "status": "accepted",
                    "result": None,
                    "output": "",
                    "output_seq": 0,
                }
        with self._state_lock:
            if new_active is not None:
                self.active_states[id] = new_active
                self.terminal_statuses.pop(id, None)
            elif done_status is not None:
                self.active_states.pop(id, None)
        return done_result

    def completed_run(self, identifier):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM calls WHERE id=? AND tool='exec' AND status='done'",
                    (identifier,),
                ).fetchone()
                is not None
            )

    def mark_running_memory(self, id):
        with self._state_lock:
            entry = self.active_states.get(id)
            if entry is not None and entry["status"] == "accepted":
                entry["status"] = "running"
                return True
        return False

    def mark_running_db(self, id):
        with self.lock, self.db:
            return (
                self.db.execute(
                    "UPDATE calls SET status='running' WHERE id=? AND status='accepted'", (id,)
                ).rowcount
                == 1
            )

    def mark_running(self, id):
        self.mark_running_memory(id)
        self.mark_running_db(id)

    def cancel_memory(self, id):
        now = time.time()
        with self._state_lock:
            self.cancelled_ids.add(id)
            self._cancelled_at[id] = now
        return now

    def cancel_db(self, id, at=None):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO cancellations VALUES (?,?)",
                (id, time.time() if at is None else at),
            )

    def cancel(self, id):
        at = self.cancel_memory(id)
        self.cancel_db(id, at)

    def is_cancelled(self, id):
        with self._state_lock:
            return id in self.cancelled_ids

    def active_status(self, id):
        """Return in-memory state if the call is active, else None (zero SQLite I/O)."""
        with self._state_lock:
            entry = self.active_states.get(id)
            return dict(entry) if entry is not None else None

    def fast_status(self, id):
        """Check active/recent status without DB I/O; cold terminals use status()."""
        with self._state_lock:
            entry = self.active_states.get(id)
            if entry is not None:
                return dict(entry)
            terminal = self.terminal_statuses.get(id)
            if terminal is not None:
                return {"status": terminal}
        return {"status": "missing"}

    def status(self, id):
        active = self.active_status(id)
        if active is not None:
            return active
        with self.lock:
            row = self.db.execute(
                "SELECT status,result,output,output_seq FROM calls WHERE id=?", (id,)
            ).fetchone()
        if not row:
            return {"status": "missing"}
        return {
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "output": row["output"],
            "output_seq": row["output_seq"],
        }

    def durable_status(self, id):
        """Look up cold status without loading a retained operation result."""
        with self.lock:
            row = self.db.execute("SELECT status FROM calls WHERE id=?", (id,)).fetchone()
        return row["status"] if row else "missing"

    def update_output(self, id, output, seq):
        trimmed = output[-131072:]
        with self.lock, self.db:
            self.db.execute(
                "UPDATE calls SET output=?,output_seq=? WHERE id=? AND output_seq<?",
                (trimmed, seq, id, seq),
            )
        with self._state_lock:
            entry = self.active_states.get(id)
            if entry is not None and entry["output_seq"] < seq:
                entry["output"] = trimmed
                entry["output_seq"] = seq

    def finish(self, id: str, result: dict):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE calls SET status='done',result=?,acked=0 WHERE id=?",
                (json.dumps(result, ensure_ascii=False), id),
            )
        with self._state_lock:
            self.active_states.pop(id, None)
            self._remember_terminal(id, "done")

    def ack(self, id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET acked=1 WHERE id=?", (id,))

    def acks_batch(self, ids: list[str] | tuple[str, ...]):
        unique = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
        if not unique:
            return
        with self.lock, self.db:
            for offset in range(0, len(unique), 500):
                chunk = unique[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                self.db.execute(f"UPDATE calls SET acked=1 WHERE id IN ({placeholders})", chunk)

    def outbox(self):
        # Bound reconnect memory/burst size. Reading 200 large results into RAM
        # before processing ACKs can exhaust a small home machine or slow its link.
        items, size = [], 0
        with self.lock:
            rows = self.db.execute(
                "SELECT id,result FROM calls WHERE status IN ('done','interrupted') AND acked=0 ORDER BY at LIMIT 32"
            )
            for row in rows:
                encoded = len(row["result"].encode("utf-8"))
                if items and size + encoded > 4 * 1024 * 1024:
                    break
                items.append({"id": row["id"], "result": json.loads(row["result"])})
                size += encoded
        return items

    def add_backup(
        self,
        id: str,
        root: str,
        path: str,
        before: bytes | None,
        after_sha: str,
        before_mode: int | None = None,
    ):
        self.add_backups_batch([(id, root, path, before, after_sha, before_mode)])

    def add_backups_batch(self, items):
        """Make every original durable before publishing any catalog rows."""
        rows = []
        wrote_files = False
        for identifier, root, path, before, after_sha, before_mode in items:
            if before is not None:
                dest = self.backups / identifier
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(before)
                    stream.flush()
                    os.fsync(stream.fileno())
                wrote_files = True
            rows.append(
                (
                    identifier,
                    root,
                    path,
                    int(before is not None),
                    digest(before) if before is not None else "new",
                    after_sha,
                    time.time(),
                    before_mode,
                )
            )
        if wrote_files:
            fsync_directory(self.backups)
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO backups(id,root,path,existed,before_sha,after_sha,at,before_mode) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )

    def history(self, root: str, path: str = "", limit: int = 30):
        sql, args = "SELECT * FROM backups WHERE root=?", [root]
        if path:
            sql += " AND path=?"
            args.append(path)
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.db.execute(sql + " ORDER BY at DESC LIMIT ?", args)]

    def backup(self, root: str, id: str):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM backups WHERE root=? AND id=?", (root, id)
            ).fetchone()
        if not row:
            raise DevError("NOT_FOUND", "该项目没有此备份，或备份已超过保留期", 404)
        row = dict(row)
        try:
            data = (self.backups / id).read_bytes() if row["existed"] else None
        except FileNotFoundError as exc:
            raise DevError("BACKUP_EXPIRED", "备份已过期或被移除；未改动项目文件", 410) from exc
        if data is not None and digest(data) != row["before_sha"]:
            raise DevError("BACKUP_CORRUPT", "备份校验失败，未改动原文件")
        return row, data
