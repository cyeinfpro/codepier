from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from shared.crypto import digest
from shared.util import DevError, fsync_directory

class Journal:
    def __init__(self, state_dir: Path):
        self.directory = state_dir.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.backups = self.directory / "backups"
        self.backups.mkdir(exist_ok=True)
        self.checkpoints = self.directory / "checkpoints"
        self.checkpoints.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / "agent.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT, acked INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS backups (id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL, existed INTEGER NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL, at REAL NOT NULL);
            ''')
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS cancellations (id TEXT PRIMARY KEY, at REAL NOT NULL)")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
            for name, definition in {"tool": "TEXT", "output": "TEXT NOT NULL DEFAULT ''", "output_seq": "INTEGER NOT NULL DEFAULT 0"}.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")
            backup_columns = {r[1] for r in self.db.execute("PRAGMA table_info(backups)")}
            if "before_mode" not in backup_columns:
                self.db.execute("ALTER TABLE backups ADD COLUMN before_mode INTEGER")
            self.db.execute("CREATE INDEX IF NOT EXISTS calls_outbox ON calls(at) WHERE status IN ('done','interrupted') AND acked=0")
            self.db.execute("CREATE INDEX IF NOT EXISTS backups_root_at ON backups(root,at DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS backups_root_path_at ON backups(root,path,at DESC)")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('journal_id',?)", (uuid.uuid4().hex,))
            # accepted means the command was durably received but never started.
            self.db.execute("UPDATE calls SET status='retryable' WHERE status='accepted'")
            from shared.contracts import TOOLS
            reads = [name for name, tool in TOOLS.items() if tool.scope == 'read']
            self.db.execute("UPDATE calls SET status='retryable' WHERE status='running' AND tool IN (%s)" % ','.join('?' for _ in reads), reads)
            self.db.execute("UPDATE calls SET status='interrupted',acked=0,result=? WHERE status='running'", (json.dumps({"ok": False, "error": {"code": "INTERRUPTED", "message": "Agent 进程在操作执行期间停止；网络重连不会重跑它。请检查文件或测试进程后再决定下一步。"}}),))
        self.journal_id = self.db.execute("SELECT value FROM meta WHERE key='journal_id'").fetchone()[0]
        os.chmod(self.directory / "agent.sqlite3", 0o600)

    def start(self, id: str, request: dict):
        fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
        with self.lock, self.db:
            old = self.db.execute("SELECT * FROM calls WHERE id=?", (id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise DevError("IDEMPOTENCY_CONFLICT", "同一个操作 ID 不能承载不同请求")
                if old["status"] in {"running", "accepted"}:
                    raise DevError("ALREADY_RUNNING", "该操作已接收或正在执行")
                if old["status"] == "retryable":
                    self.db.execute("UPDATE calls SET status='accepted',result=NULL,acked=0 WHERE id=?", (id,))
                    return None
                return json.loads(old["result"])
            tool = request.get("tool")
            self.db.execute("INSERT INTO calls(id,fingerprint,status,at,tool) VALUES (?,?,'accepted',?,?)", (id, fingerprint, time.time(), tool if isinstance(tool, str) else None))
        return None

    def mark_running(self, id):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET status='running' WHERE id=? AND status='accepted'", (id,))

    def cancel(self, id):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO cancellations VALUES (?,?)", (id, time.time()))

    def is_cancelled(self, id):
        with self.lock:
            return self.db.execute("SELECT id FROM cancellations WHERE id=?", (id,)).fetchone() is not None

    def status(self, id):
        with self.lock:
            row = self.db.execute("SELECT status,result,output,output_seq FROM calls WHERE id=?", (id,)).fetchone()
        if not row:
            return {"status": "missing"}
        return {"status": row['status'], "result": json.loads(row['result']) if row['result'] else None,
                "output": row['output'], "output_seq": row['output_seq']}

    def update_output(self, id, output, seq):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET output=?,output_seq=? WHERE id=? AND output_seq<?", (output[-131072:], seq, id, seq))

    def finish(self, id: str, result: dict):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET status='done',result=?,acked=0 WHERE id=?", (json.dumps(result, ensure_ascii=False), id))

    def ack(self, id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET acked=1 WHERE id=?", (id,))

    def outbox(self):
        # Bound reconnect memory/burst size. Reading 200 large results into RAM
        # before processing ACKs can exhaust a small home machine or slow its link.
        items, size = [], 0
        with self.lock:
            rows = self.db.execute("SELECT id,result FROM calls WHERE status IN ('done','interrupted') AND acked=0 ORDER BY at LIMIT 32")
            for row in rows:
                encoded = len(row['result'].encode('utf-8'))
                if items and size + encoded > 4 * 1024 * 1024:
                    break
                items.append({"id": row['id'], "result": json.loads(row['result'])})
                size += encoded
        return items

    def add_backup(self, id: str, root: str, path: str, before: bytes | None, after_sha: str,
                   before_mode: int | None = None):
        # Persist backup before mutating the project; a failed write may leave an unused backup.
        if before is not None:
            dest = self.backups / id
            with dest.open("xb") as f:
                f.write(before)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(dest, 0o600)
            fsync_directory(self.backups)
        with self.lock, self.db:
            self.db.execute("INSERT INTO backups(id,root,path,existed,before_sha,after_sha,at,before_mode) VALUES (?,?,?,?,?,?,?,?)", (id, root, path, int(before is not None), digest(before) if before is not None else "new", after_sha, time.time(), before_mode))

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
            row = self.db.execute("SELECT * FROM backups WHERE root=? AND id=?", (root, id)).fetchone()
        if not row:
            raise DevError("NOT_FOUND", "该项目没有此备份", 404)
        row = dict(row)
        data = (self.backups / id).read_bytes() if row["existed"] else None
        if data is not None and digest(data) != row["before_sha"]:
            raise DevError("BACKUP_CORRUPT", "备份校验失败，未改动原文件")
        return row, data
