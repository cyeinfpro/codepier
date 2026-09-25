"""Startup, interruption and concurrency regressions for Hub persistence."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import sqlite3
import threading

import pytest
from cryptography.fernet import Fernet

from hub.store import SCHEMA, Store


def legacy_database(directory):
    directory.mkdir()
    with sqlite3.connect(directory / "hub.sqlite3") as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES ('schema', '1')")
        db.execute("INSERT INTO audit(at,actor,action,status,detail) VALUES (1,'owner','before-upgrade','ok','{}')")


def test_concurrent_openers_never_observe_a_partial_master_key(tmp_path, monkeypatch):
    directory = tmp_path / "hub"
    generating = threading.Event()
    release = threading.Event()
    original_generate = Fernet.generate_key

    def delayed_key():
        generating.set()
        assert release.wait(10)
        return original_generate()

    monkeypatch.setattr(Fernet, "generate_key", delayed_key)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(Store, directory)
        try:
            assert generating.wait(10)
            # A crash while producing the key must leave no visible empty key.
            assert not (directory / "master.key").exists()
            second = workers.submit(Store, directory)
        finally:
            release.set()
        stores = [first.result(timeout=10), second.result(timeout=10)]
    try:
        assert stores[1].decrypt(stores[0].encrypt("survives-concurrent-startup")) == "survives-concurrent-startup"
        assert len((directory / "master.key").read_bytes()) == 44
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_many_concurrent_openers_complete_atomic_migration(tmp_path, legacy):
    directory = tmp_path / "hub"
    if legacy:
        legacy_database(directory)
    start = threading.Barrier(8)

    def open_store():
        start.wait(timeout=10)
        store = Store(directory)
        try:
            store.audit("owner", "concurrent-open")
            return store.encrypt("same-key")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as workers:
        encrypted = list(workers.map(lambda _: open_store(), range(8)))
    store = Store(directory)
    try:
        assert all(store.decrypt(value) == "same-key" for value in encrypted)
        assert store.one("SELECT count(*) AS n FROM audit WHERE action='concurrent-open'")["n"] == 8
        assert store.one("SELECT value FROM meta WHERE key='schema'")["value"] == "9"
        assert "resource" in {row["name"] for row in store.all("PRAGMA table_info(grants)")}
        if legacy:
            assert store.one("SELECT action FROM audit WHERE action='before-upgrade'")
    finally:
        store.close()


@pytest.mark.parametrize("encrypted_record", ["device", "operation"])
def test_missing_master_key_cannot_silently_orphan_encrypted_records(tmp_path, encrypted_record):
    directory = tmp_path / "hub"
    store = Store(directory)
    key = (directory / "master.key").read_bytes()
    encrypted = store.encrypt("must-remain-recoverable")
    if encrypted_record == "device":
        store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,1)", (encrypted,))
    else:
        store.execute("""INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,created,updated,payload)
                         VALUES ('op','owner','fs_read','{}','fingerprint','queued',1,1,?)""", (encrypted,))
    store.close()
    (directory / "master.key").unlink()

    with pytest.raises(RuntimeError, match="master.key is missing"):
        Store(directory)
    assert not (directory / "master.key").exists()
    # Restoring the real key must recover the same ciphertext and records.
    (directory / "master.key").write_bytes(key)
    recovered = Store(directory)
    try:
        table, column = ("devices", "secret") if encrypted_record == "device" else ("operations", "payload")
        assert recovered.decrypt(recovered.one(f"SELECT {column} FROM {table}")[column]) == "must-remain-recoverable"
    finally:
        recovered.close()


def test_key_flush_failure_does_not_publish_key_and_closes_connection(tmp_path, monkeypatch):
    directory = tmp_path / "hub"
    original_connect = sqlite3.connect
    connections = []

    def capture_connection(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    def fail_fsync(fd):
        raise OSError("injected key flush failure")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", capture_connection)
        patch.setattr(os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="injected key flush"):
            Store(directory)
    assert not (directory / "master.key").exists()
    assert not list(directory.glob(".master-key-*"))
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    recovered = Store(directory)
    recovered.close()


def test_interrupted_migration_rolls_back_and_can_retry(tmp_path, monkeypatch):
    directory = tmp_path / "hub"
    legacy_database(directory)
    original_connect = sqlite3.connect
    connections = []

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE operations ADD COLUMN journal_id"):
                raise sqlite3.OperationalError("injected migration interruption")
            return super().execute(sql, *args, **kwargs)

    def interrupted_connect(*args, **kwargs):
        connection = original_connect(*args, factory=InterruptedConnection, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", interrupted_connect)
        with pytest.raises(sqlite3.OperationalError, match="injected migration interruption"):
            Store(directory)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with sqlite3.connect(directory / "hub.sqlite3") as db:
        assert "payload" not in {row[1] for row in db.execute("PRAGMA table_info(operations)")}
        assert db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "1"
    store = Store(directory)
    try:
        assert store.one("SELECT action FROM audit")["action"] == "before-upgrade"
        assert store.one("SELECT value FROM meta WHERE key='schema'")["value"] == "9"
    finally:
        store.close()


def test_future_schema_is_rejected_without_downgrading_metadata(tmp_path):
    directory = tmp_path / "hub"
    store = Store(directory)
    store.execute("UPDATE meta SET value='999' WHERE key='schema'")
    store.close()
    with pytest.raises(RuntimeError, match="Unsupported Hub database schema version: 999"):
        Store(directory)
    with sqlite3.connect(directory / "hub.sqlite3") as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "999"


def test_v2_grant_migration_preserves_existing_authorizations(tmp_path):
    directory = tmp_path / "hub"
    store = Store(directory)
    store.execute("ALTER TABLE grants DROP COLUMN resource")
    store.execute("UPDATE meta SET value='2' WHERE key='schema'")
    store.execute("""INSERT INTO grants(id,user_id,label,client_id,scopes,projects,created)
                     VALUES ('grant','owner','before-upgrade','client','["read"]','["project"]',1)""")
    key = (directory / "master.key").read_bytes()
    store.close()
    upgraded = Store(directory)
    try:
        grant = upgraded.one("SELECT * FROM grants WHERE id='grant'")
        assert grant["resource"] is None
        assert grant["scopes"] == '["read"]'
        assert grant["projects"] == '["project"]'
        assert upgraded.one("SELECT value FROM meta WHERE key='schema'")["value"] == "9"
        assert (directory / "master.key").read_bytes() == key
    finally:
        upgraded.close()


def test_close_waits_for_an_active_database_user(tmp_path):
    store = Store(tmp_path / "hub")
    close_started = threading.Event()

    def close_store():
        close_started.set()
        store.close()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.lock:
            closed = workers.submit(close_store)
            assert close_started.wait(10)
            with pytest.raises(FutureTimeoutError):
                closed.result(timeout=0.1)
            assert store.db.execute("SELECT 1").fetchone()[0] == 1
            assert not closed.done()
        closed.result(timeout=10)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.db.execute("SELECT 1")
