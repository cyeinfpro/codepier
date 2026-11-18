"""Short-lived, principal-bound immutable text pages for explicit pinned reads."""

from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import stat
import struct
import time
import uuid

from shared.crypto import digest
from shared.util import DevError
from agent.core_files import OUTPUT_BYTES

MAX_DATA = 8 * 1024 * 1024
MAX_INDEX = 4 * 1024 * 1024
MAX_STORED = 64 * 1024 * 1024
TTL = 600


def _scope(project, root, path):
    owner = project.get("_coding_owner")
    if not owner:
        raise DevError("READ_SNAPSHOT_UNAVAILABLE", "固定版本读取需要当前项目和身份绑定", 409)
    return digest(
        json.dumps([owner, project.get("_coding_device"), project.get("id"), str(root), str(path)])
    )


def _accessible(path):
    # Check the current path and OS read permission without re-reading the live version.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info, named = os.fstat(fd), path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not os.path.samestat(info, named)
        ):
            raise DevError("READ_SNAPSHOT_UNAVAILABLE", "固定版本对应路径已失效", 409)
    finally:
        os.close(fd)


def prune_expired(directory):
    path = directory / "read-pages.sqlite3"
    if not path.is_file() or path.is_symlink():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'").fetchone():
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM pages WHERE expires<=?", (time.time(),))


def read_snapshot(engine, root, project, args, token):
    path = engine.path(root, args["path"], False)
    scope = _scope(project, root, path)
    try:
        _accessible(path)
    except OSError as exc:
        raise DevError("READ_SNAPSHOT_UNAVAILABLE", "固定版本对应文件已不可读", 409) from exc
    data = None
    if token == "create":
        data = engine.read_bytes(path, max_bytes=MAX_DATA, verify_path=True)
        sha = digest(data)
        if args.get("expected_sha256") and args["expected_sha256"] != sha:
            raise DevError("SHA_CONFLICT", "文件版本已改变", 409)
        lines = engine.text(data).splitlines(keepends=True)
        if 4 * (len(lines) + 1) > MAX_INDEX:
            raise DevError("READ_SNAPSHOT_LIMIT", "固定版本行数超过索引上限", 413)
        index = bytearray(struct.pack("<I", 0))
        pos = 0
        for line in lines:
            pos += len(line.encode("utf-8"))
            index.extend(struct.pack("<I", pos))
        count = len(lines)
        del lines
        token = uuid.uuid4().hex
    dbpath = engine.journal.directory / "read-pages.sqlite3"
    with closing(sqlite3.connect(dbpath, timeout=5)) as db:
        os.chmod(dbpath, 0o600)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS pages (
            token TEXT NOT NULL UNIQUE,scope TEXT NOT NULL,sha TEXT NOT NULL,expires REAL NOT NULL,
            n INTEGER NOT NULL,bytes INTEGER NOT NULL,cost INTEGER NOT NULL,data BLOB NOT NULL,idx BLOB NOT NULL)""")
        now = time.time()
        with db:
            db.execute("BEGIN IMMEDIATE" if data is not None else "BEGIN")
            if data is not None:
                db.execute("DELETE FROM pages WHERE expires<=?", (now,))
                cost = len(data) + len(index)
                # One bounded store shared across processes, with atomic admission and eviction.
                for ident, retained, rows in db.execute(
                    "SELECT rowid,SUM(cost) OVER (ORDER BY rowid DESC),ROW_NUMBER() OVER (ORDER BY rowid DESC) "
                    "FROM pages ORDER BY rowid DESC"
                ).fetchall():
                    if retained + cost > MAX_STORED or rows >= 64:
                        db.execute("DELETE FROM pages WHERE rowid=?", (ident,))
                db.execute(
                    "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                    (token, scope, sha, now + TTL, count, len(data), cost, data, index),
                )
            row = db.execute(
                "SELECT rowid,sha,n,bytes,expires FROM pages WHERE token=? AND scope=? AND expires>?",
                (token, scope, now),
            ).fetchone()
            if not row:
                raise DevError("READ_SNAPSHOT_UNAVAILABLE", "固定版本已过期或不属于当前文件", 409)
            ident, sha, count, size, expiry = row
            if args.get("expected_sha256") and args["expected_sha256"] != sha:
                raise DevError("SHA_CONFLICT", "请求摘要与固定版本不符", 409)
            start = args["offset"] - 1
            if start < 0 or start > 0 and start >= count:
                raise DevError("INVALID_OFFSET", "offset 超出文件行数")
            n = min(args["limit"], count - start)
            with db.blobopen("pages", "idx", ident, readonly=True) as offsets:
                offsets.seek(start * 4)
                positions = [v for (v,) in struct.iter_unpack("<I", offsets.read((n + 1) * 4))]
            stop = 0
            for i, offset in enumerate(positions):
                if offset - positions[0] > OUTPUT_BYTES:
                    break
                stop = i
            if n and not stop:
                raise DevError("LINE_TOO_LARGE", "此行超过固定版本分页预算")
            with db.blobopen("pages", "data", ident, readonly=True) as blob:
                blob.seek(positions[0])
                body = blob.read(positions[stop] - positions[0]).decode("utf-8")
    end = start + stop
    return {
        "path": args["path"],
        "bytes": size,
        "sha256": sha,
        "read_snapshot": token,
        "snapshot_expires_at": expiry,
        "content": body,
        "offset": start + 1,
        "end_line": end,
        "total_lines": count,
        "truncated": end < count,
        "next_offset": end + 1 if end < count else None,
    }
