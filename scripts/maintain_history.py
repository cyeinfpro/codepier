#!/usr/bin/env python3
"""Preview bounded history compaction; use --apply to execute the reviewed policy.

Default retention is 30 days for terminal payloads and 365 days for audit.
Unresolved/referenced operations, identity, fingerprints and replay fences stay.
No automatic startup cleanup, database vacuum, or attachment deletion occurs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.operation_history import encrypt_legacy_history, prune_history, retention_plan
from hub.state_layout import state_directory


class ReadOnlyHistory:
    """Preview the existing schema without Store startup migrations or keys."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row

    def all(self, sql, parameters=()):
        return [dict(row) for row in self.db.execute(sql, parameters)]

    def one(self, sql, parameters=()):
        row = self.db.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None

    def require_transaction(self):
        if not self.db.in_transaction:
            raise RuntimeError("History maintenance requires a transaction")

    @contextmanager
    def transaction(self, *, immediate=False):
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            self.db.rollback()

    def close(self):
        self.db.close()


def run(data_directory: Path, *, apply=False, encrypt_legacy=False, **policy):
    directory = data_directory.expanduser().resolve()
    database = state_directory(directory) / "hub.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ValueError("An existing regular Hub database is required")
    if type(apply) is not bool or type(encrypt_legacy) is not bool:
        raise ValueError("maintenance flags must be explicit booleans")
    if apply:
        from hub.store import Store

        store = Store(directory)
    else:
        store = ReadOnlyHistory(database)
    try:
        if encrypt_legacy:
            return {
                "action": "encrypt_legacy",
                "dry_run": not apply,
                "fields": encrypt_legacy_history(
                    store, batch=policy.get("batch", 100), dry_run=not apply
                ),
            }
        if not apply:
            return retention_plan(store, **policy)
        return prune_history(store, dry_run=False, **policy)
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--encrypt-legacy",
        action="store_true",
        help="Encrypt a bounded batch of legacy plaintext without pruning",
    )
    parser.add_argument("--operation-days", type=int, default=30)
    parser.add_argument("--audit-days", type=int, default=365)
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()
    report = run(
        args.data_dir,
        apply=args.apply,
        encrypt_legacy=args.encrypt_legacy,
        operation_days=args.operation_days,
        audit_days=args.audit_days,
        batch=args.batch,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
