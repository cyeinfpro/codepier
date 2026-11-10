"""Monotonic collaboration data floor, independent of admission switches."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

CONTRACT_VERSION = 1
LEGACY_SCHEMA_VERSION = '10'
PROTECTED_SCHEMA_VERSION = '11'
MIN_READER_KEY = 'collaboration_min_reader'
MIN_WRITER_KEY = 'collaboration_min_writer'


def assert_compatible(db, *, reader=CONTRACT_VERSION, writer=CONTRACT_VERSION):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return
    values = dict(db.execute(
        "SELECT key,value FROM meta WHERE key IN ('schema', ?, ?)",
        (MIN_READER_KEY, MIN_WRITER_KEY),
    ).fetchall())
    if values.get('schema') != PROTECTED_SCHEMA_VERSION and not ({MIN_READER_KEY, MIN_WRITER_KEY} & values.keys()):
        return
    limits = (values.get(MIN_READER_KEY), values.get(MIN_WRITER_KEY))
    if (values.get('schema') != PROTECTED_SCHEMA_VERSION
            or any(not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                   or not 1 <= len(value) <= 6 or value.startswith('0') for value in limits)):
        raise RuntimeError('Invalid collaboration compatibility declaration')
    if int(limits[0]) > reader or int(limits[1]) > writer:
        raise RuntimeError('Hub binary does not meet the collaboration database reader/writer floor')


def check_database(state: str | Path):
    """Inspect current state before starting the service or opening a writer."""
    path = Path(state) / 'hub.sqlite3'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Hub database is not a regular file')
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            assert_compatible(db)


def raise_floor(store):
    """Activate the floor in the same transaction as the first protected effect."""
    store.require_transaction()
    raise_floor_in_transaction(store.db)


def raise_floor_in_transaction(db):
    if not db.in_transaction:
        raise RuntimeError('A transaction is required to raise the collaboration floor')
    assert_compatible(db)
    schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
    if schema == PROTECTED_SCHEMA_VERSION:
        return
    if schema != LEGACY_SCHEMA_VERSION:
        raise RuntimeError('Hub core schema must finish migration before raising the collaboration floor')
    db.executemany("INSERT INTO meta(key,value) VALUES (?,?)",
                         ((MIN_READER_KEY, str(CONTRACT_VERSION)),
                          (MIN_WRITER_KEY, str(CONTRACT_VERSION))))
    # Existing Hub binaries reject schema 11 even if they ignore the new keys.
    db.execute("UPDATE meta SET value=? WHERE key='schema'", (PROTECTED_SCHEMA_VERSION,))
