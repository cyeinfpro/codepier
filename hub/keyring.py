"""Offline, recoverable Hub encryption-key rotation.

A prepared keyring can read both generations before any ciphertext is rewritten.
The DB switch is atomic; retiring old keys happens only after that commit. Ordinary
startup never rotates keys, deletes backups, or guesses how to recover a bad key.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken

from shared.instance_lock import InstanceLock

RING_FILE = "master.keys.json"
JOURNAL_FILE = "rekey.json"
CIPHER_COLUMNS = (("devices", "secret"), ("operations", "payload"), ("vps_connections", "secret"))


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def private_read(path: Path, limit: int = 65536) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Encryption metadata must be an owned regular file")
    if path.stat().st_size > limit:
        raise RuntimeError("Encryption metadata exceeds its size limit")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("Encryption metadata must have mode 0600")
    return path.read_bytes()


def atomic_private(path: Path, raw: bytes) -> None:
    if path.is_symlink():
        raise RuntimeError("Refusing a symlink in encryption metadata")
    fd, temporary = tempfile.mkstemp(prefix=".rekey-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def encode_ring(active: str, keys: dict[str, bytes]) -> bytes:
    return (json.dumps({"version": 1, "active": active, "keys": {identifier: value.decode("ascii") for identifier, value in keys.items()}}, sort_keys=True) + "\n").encode()


def decode_ring(raw: bytes) -> tuple[str, dict[str, bytes]]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError()
        keys = value["keys"]
        if not isinstance(keys, dict) or not 1 <= len(keys) <= 16:
            raise ValueError()
        parsed = {}
        for identifier, key in keys.items():
            if not isinstance(key, str) or len(key) != 44:
                raise ValueError()
            raw_key = key.encode("ascii")
            Fernet(raw_key)
            if identifier != key_id(raw_key):
                raise ValueError()
            parsed[identifier] = raw_key
        active = value["active"]
        if active not in parsed:
            raise ValueError()
        return active, parsed
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RuntimeError("Invalid encryption keyring; restore its matching private backup") from None


class CipherSuite:
    def __init__(self, directory: Path, legacy_key: bytes):
        self.legacy_key = legacy_key
        self.legacy = Fernet(legacy_key)
        ring = directory / RING_FILE
        self.versioned = ring.exists() or ring.is_symlink()
        if self.versioned:
            self.active, self.keys = decode_ring(private_read(ring))
        else:
            self.active, self.keys = key_id(legacy_key), {key_id(legacy_key): legacy_key}
        self.ciphers = {identifier: Fernet(key) for identifier, key in self.keys.items()}

    def encrypt(self, plaintext: bytes) -> bytes:
        encrypted = self.ciphers[self.active].encrypt(plaintext)
        # Do not force a cipher-format migration during an ordinary upgrade.
        # The explicit offline rekey command enables versioned ciphertext.
        return b"cp1:" + self.active.encode("ascii") + b":" + encrypted if self.versioned else encrypted

    def decrypt(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"cp"):
            return self.legacy.decrypt(ciphertext)
        try:
            version, identifier, token = ciphertext.split(b":", 2)
            if version != b"cp1":
                raise ValueError()
            cipher = self.ciphers[identifier.decode("ascii")]
            return cipher.decrypt(token)
        except (ValueError, KeyError, UnicodeError):
            raise InvalidToken() from None


def _stage(directory: Path, journal: dict, stage: str) -> None:
    journal.update(stage=stage, updated=time.time())
    atomic_private(directory / JOURNAL_FILE, (json.dumps(journal, sort_keys=True) + "\n").encode())


def _backup(store, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    with store.lock:
        target = sqlite3.connect(destination / "hub.sqlite3")
        try:
            store.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Pre-rotation database backup failed integrity verification")
        finally:
            target.close()
    os.chmod(destination / "hub.sqlite3", 0o600)
    atomic_private(destination / "master.key", private_read(store.directory / "master.key"))
    ring = store.directory / RING_FILE
    if ring.exists() or ring.is_symlink():
        atomic_private(destination / RING_FILE, private_read(ring))


def rotate_key(directory: str | Path, *, resume: bool = False) -> dict:
    """Rotate an idle Hub's key, or resume exactly its recorded rotation.

    The instance lock rejects a running Hub; the command is not a panel action.
    Backups are deliberately retained and never pruned automatically.
    """
    from hub.store import Store
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Select an existing, non-symlink Hub data directory")
    directory = directory.resolve()
    lock = InstanceLock(directory / ".hub.lock")
    store = None
    key_lock = None
    try:
        key_lock = InstanceLock(directory / ".keys.lock")
        store = Store(directory)
        journal_path = directory / JOURNAL_FILE
        previous = json.loads(private_read(journal_path)) if journal_path.exists() or journal_path.is_symlink() else None
        if previous is not None and (not isinstance(previous, dict) or previous.get("version") != 1):
            raise RuntimeError("Unrecognized rotation journal; no metadata was replaced")
        if previous and previous.get("stage") != "complete" and not resume:
            raise RuntimeError("A rotation is unfinished; inspect its backup and use rekey --resume")
        if resume:
            if not isinstance(previous, dict):
                raise RuntimeError("No recorded rotation is available to resume")
            journal = previous
            if journal.get("stage") == "complete":
                return {"status": "already_completed", "key_id": journal["active"], "backup": journal["backup"]}
            identifier = journal.get("id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", identifier) or journal.get("backup") != ".rekey/" + identifier:
                raise RuntimeError("Unrecognized rotation journal; no metadata was replaced")
            destination = directory / ".rekey" / identifier
            if destination.is_symlink() or destination.parent.is_symlink():
                raise RuntimeError("Unrecognized rotation backup directory")
            staged = private_read(destination / "next-keyring.json")
            if hashlib.sha256(staged).hexdigest() != journal.get("next_sha256"):
                raise RuntimeError("Prepared keyring checksum mismatch; no metadata was replaced")
            active, keys = decode_ring(staged)
            if active != journal.get("active"):
                raise RuntimeError("Prepared keyring does not match the rotation journal")
        else:
            parent = directory / ".rekey"
            if parent.is_symlink():
                raise RuntimeError("Rotation backup directory cannot be a symlink")
            parent.mkdir(mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700)
            identifier = uuid.uuid4().hex
            destination = parent / identifier
            _backup(store, destination)
            fresh = Fernet.generate_key()
            active, keys = key_id(fresh), {**store.cipher.keys, key_id(store.cipher.legacy_key): store.cipher.legacy_key, key_id(fresh): fresh}
            if len(keys) > 16:
                raise RuntimeError("Too many retained key generations; inspect unfinished rotations")
            staged = encode_ring(active, keys)
            atomic_private(destination / "next-keyring.json", staged)
            journal = {"version": 1, "id": identifier, "active": active, "backup": ".rekey/" + identifier,
                       "next_sha256": hashlib.sha256(staged).hexdigest(), "created": time.time()}
            _stage(directory, journal, "prepared")
        # Both generations remain readable through every interruption point.
        atomic_private(directory / RING_FILE, encode_ring(active, keys))
        _stage(directory, journal, "keyring_published")
        store.cipher = CipherSuite(directory, private_read(directory / "master.key").strip())
        changed = 0
        with store.lock, store.db:
            store.db.execute("BEGIN IMMEDIATE")
            for table, column in CIPHER_COLUMNS:
                cursor = store.db.execute(f"SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL")
                while rows := cursor.fetchmany(128):
                    for row in rows:
                        plaintext = store.decrypt(row[column])
                        encrypted = store.encrypt(plaintext)
                        store.db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (encrypted, row["id"]))
                        changed += 1
            store.audit("hub:offline", "keys.rotated", active, detail={"records": changed})
        _stage(directory, journal, "database_committed")
        atomic_private(directory / "master.key", keys[active])
        _stage(directory, journal, "legacy_key_replaced")
        atomic_private(directory / RING_FILE, encode_ring(active, {active: keys[active]}))
        _stage(directory, journal, "complete")
        return {"status": "completed", "key_id": active, "records": changed, "backup": journal["backup"]}
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            try:
                if key_lock is not None:
                    key_lock.close()
            finally:
                lock.close()
