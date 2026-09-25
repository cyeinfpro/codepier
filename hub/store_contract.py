"""Thread-owned SQLite access and nesting-safe transaction contexts.

Entering a context does not start a transaction: callers needing a consistent
read snapshot still issue BEGIN, and read/modify/write callers BEGIN IMMEDIATE.
Only the outermost context commits. An inner failure makes the outer transaction
rollback-only even when application code catches that failure.
"""
from __future__ import annotations

import sqlite3
import re
import threading


class OwnedRLock:
    def __init__(self):
        self._lock = threading.RLock()
        self._local = threading.local()

    @property
    def owned(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def require(self):
        if not self.owned:
            raise RuntimeError("Raw Hub SQLite access requires Store.lock in the current thread")

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._local.depth = getattr(self._local, "depth", 0) + 1
        return acquired

    def release(self):
        self.require()
        self._lock.release()
        self._local.depth -= 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class TransactionAborted(RuntimeError):
    pass


class CheckedConnection:
    def __init__(self, connection: sqlite3.Connection, lock: OwnedRLock, on_write=None):
        self._connection = connection
        self._lock = lock
        self.depth = 0
        self._rollback_only = False
        self._closed = False
        self._on_write = on_write
        self._callbacks = []

    def _require(self):
        # Preserve SQLite's normal closed-connection error for lifecycle callers.
        if not self._closed:
            self._lock.require()

    @property
    def in_transaction(self):
        self._require()
        return self._connection.in_transaction

    def execute(self, sql, parameters=()):
        self._require()
        cursor = self._connection.execute(sql, parameters)
        self._changed(sql)
        return CheckedCursor(cursor, self._lock)

    def executemany(self, sql, parameters):
        self._require()
        cursor = self._connection.executemany(sql, parameters)
        self._changed(sql)
        return CheckedCursor(cursor, self._lock)

    def _changed(self, sql):
        if self._on_write and re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I):
            self._on_write(sql)

    def after_commit(self, callback):
        self._require()
        if self.depth:
            self._callbacks.append(callback)
        else:
            callback()

    def executescript(self, script):
        self._require()
        if self.depth or self._connection.in_transaction:
            raise RuntimeError("executescript would implicitly commit the managed transaction")
        return CheckedCursor(self._connection.executescript(script), self._lock)

    def backup(self, target, **kwargs):
        self._require()
        return self._connection.backup(target, **kwargs)

    def __enter__(self):
        self._require()
        self._connection.__enter__()
        self.depth += 1
        return self

    def __exit__(self, kind, error, traceback):
        self._require()
        self.depth -= 1
        self._rollback_only = self._rollback_only or kind is not None
        if self.depth:
            return False
        rollback_only, self._rollback_only = self._rollback_only, False
        callbacks, self._callbacks = self._callbacks, []
        if rollback_only and kind is None:
            self._connection.rollback()
            raise TransactionAborted("An inner database operation failed; the whole transaction was rolled back")
        outcome = self._connection.__exit__(kind, error, traceback)
        if kind is None:
            for callback in callbacks:
                callback()
        return outcome

    def close(self):
        with self._lock:
            self._connection.close()
            self._closed = True


class CheckedCursor:
    """A cursor cannot smuggle an unlocked connection out of Store.execute."""
    def __init__(self, cursor, lock):
        self._cursor, self._lock = cursor, lock
        self.rowcount, self.lastrowid = cursor.rowcount, cursor.lastrowid
        self.description = cursor.description

    def fetchone(self):
        self._lock.require()
        return self._cursor.fetchone()

    def fetchall(self):
        self._lock.require()
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        self._lock.require()
        return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

    def close(self):
        self._lock.require()
        self._cursor.close()

    def __iter__(self):
        self._lock.require()
        return self

    def __next__(self):
        self._lock.require()
        return next(self._cursor)
