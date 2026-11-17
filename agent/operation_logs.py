"""Bounded local spool of already-redacted process output; never raw pipe bytes."""

from __future__ import annotations

import os
from pathlib import Path

from agent.run_directories import safe_directory

MAX_LOG_BYTES = 8 * 1024 * 1024


class OperationLog:
    """Keep a prefix beyond the receipt tail, with explicit truncation/failure."""

    def __init__(self, directory: Path, limit: int = MAX_LOG_BYTES):
        self.limit = limit
        self.written = 0
        self.seen = 0
        self.error = False
        self.closed = False
        self.finished = False
        self.fd = None
        try:
            path = safe_directory(directory) / "output.redacted.log"
            self.fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except OSError:
            self.error = True

    def append(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        self.seen += len(data)
        if self.error or self.closed:
            return
        data = data[: max(0, self.limit - self.written)]
        try:
            while data:
                count = os.write(self.fd, data)
                if count <= 0:
                    raise OSError("Incomplete log write")
                self.written += count
                data = data[count:]
        except OSError:
            # No exception text: paths or command output must not enter metadata.
            self.error = True

    def finish(self) -> None:
        self.finished = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                self.error = True
            self.fd = None

    def receipt(self, operation_id: str) -> dict:
        return {
            "path": ".codepier/runs/" + operation_id + "/output.redacted.log",
            "status": "write_failed"
            if self.error
            else "truncated"
            if self.seen > self.limit
            else "complete"
            if self.finished
            else "incomplete",
            "bytes": self.written,
            "limit_bytes": self.limit,
            "redacted": True,
            "retention_days": 7,
            "access": "authorized_local_filesystem",
        }
