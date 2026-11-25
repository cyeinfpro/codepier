"""Offline-verifiable Hub database backups with a separately held key archive."""

from __future__ import annotations

from contextlib import closing
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import zipfile

from hub.collaboration.delivery_compatibility import assert_compatible
from hub.keyring import CipherSuite, RING_FILE, atomic_private, private_read
from shared.instance_lock import InstanceLock
from shared.util import VERSION, fsync_directory

DATABASE = "hub.sqlite3"
MANIFEST = "manifest.json"
KEY = "master.key"
COUNTS = (
    "devices",
    "grants",
    "oauth_clients",
    "tokens",
    "collaboration_rooms",
    "channel_attachments",
)
SMOKE_FIELDS = (
    ("devices", "secret"),
    ("oauth_lifetime_credentials", "secret"),
    ("collaboration_secrets", "secret"),
    ("channel_attachments", "content"),
)


def database_summary(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Hub backup failed integrity_check")
        assert_compatible(db)
        schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if not schema:
            raise RuntimeError("Hub backup has no schema version")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {
            table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in COUNTS
            if table in tables
        }
    with path.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    return {"schema": schema[0], "rows": counts, "database_sha256": digest}


def key_fingerprints(keys):
    return {name: sha256(raw).hexdigest() for name, raw in keys.items()}


def _backup_database(source, target):
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Hub database is not a regular file")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(target)) as snapshot:
            live.backup(snapshot, pages=256)
            snapshot.execute("PRAGMA journal_mode=DELETE")


def _archive(path, content, database=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            if database is not None:
                archive.write(database, DATABASE)
            for name, raw in content.items():
                archive.writestr(name, raw)
        stream.flush()
        os.fsync(stream.fileno())


def write_backup(state, output, keys_output):
    state = Path(state).absolute()
    output, keys_output = Path(output).absolute(), Path(keys_output).absolute()
    if output.parent.resolve() == keys_output.parent.resolve():
        raise ValueError("Use separate directories for the backup and the key archive")
    for path in (output, keys_output):
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    with InstanceLock(state / ".keys.lock"):
        keys = {KEY: private_read(state / KEY)}
        ring = state / RING_FILE
        if ring.exists() or ring.is_symlink():
            keys[RING_FILE] = private_read(ring)
        with TemporaryDirectory(prefix=".codepier-backup-", dir=output.parent) as data_temp:
            with TemporaryDirectory(prefix=".codepier-keys-", dir=keys_output.parent) as key_temp:
                data_stage = Path(data_temp)
                database = data_stage / DATABASE
                _backup_database(state / DATABASE, database)
                manifest = {
                    "format": 1,
                    "hub_version": VERSION,
                    **database_summary(database),
                    "key_sha256": key_fingerprints(keys),
                }
                data_zip, keys_zip = data_stage / "backup.zip", Path(key_temp) / "keys.zip"
                _archive(
                    data_zip, {MANIFEST: json.dumps(manifest, sort_keys=True).encode()}, database
                )
                _archive(keys_zip, keys)
                os.link(keys_zip, keys_output)
                fsync_directory(keys_output.parent)
                try:
                    os.link(data_zip, output)
                except OSError:
                    if keys_output.exists() and os.path.samestat(
                        keys_zip.stat(), keys_output.stat()
                    ):
                        keys_output.unlink()
                        fsync_directory(keys_output.parent)
                    raise
                fsync_directory(output.parent)
    return manifest


def _load_keys(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(archive.namelist())
        if names not in ([KEY], sorted([KEY, RING_FILE])):
            raise RuntimeError("Unexpected files in the Hub key archive")
        if any(archive.getinfo(name).file_size > 65536 for name in names):
            raise RuntimeError("Hub key archive entry exceeds its size limit")
        return {name: archive.read(name) for name in names}


def _checked_scratch_dir(scratch_dir):
    if scratch_dir is not None:
        scratch_dir = Path(scratch_dir).expanduser().resolve(strict=True)
        info = scratch_dir.stat()
        if not scratch_dir.is_dir() or (
            hasattr(os, "geteuid") and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
        ):
            raise RuntimeError("Hub restore scratch directory must be private and owned")
    return scratch_dir


def _check_restore_capacity(archive, stage):
    database_bytes = archive.getinfo(DATABASE).file_size
    if database_bytes > shutil.disk_usage(stage).free:
        raise RuntimeError(
            "Hub restore scratch space is too small; choose a private disk-backed --scratch-dir"
        )


def restore_backup(backup_path, keys_path, *, destination=None, scratch_dir=None):
    scratch_dir = _checked_scratch_dir(scratch_dir)
    with TemporaryDirectory(prefix=".codepier-restore-", dir=scratch_dir) as temp:
        stage = Path(temp)
        database = stage / DATABASE
        with zipfile.ZipFile(backup_path) as archive:
            if sorted(archive.namelist()) != sorted([DATABASE, MANIFEST]):
                raise RuntimeError("Expected a split Hub backup archive")
            if archive.getinfo(MANIFEST).file_size > 65536:
                raise RuntimeError("Hub backup manifest exceeds its size limit")
            manifest = json.loads(archive.read(MANIFEST))
            if not isinstance(manifest, dict) or manifest.get("format") != 1:
                raise RuntimeError("Unknown Hub backup manifest")
            _check_restore_capacity(archive, stage)
            with archive.open(DATABASE) as source, database.open("xb") as stream:
                shutil.copyfileobj(source, stream)
        keys = _load_keys(keys_path)
        if key_fingerprints(keys) != manifest.get("key_sha256"):
            raise RuntimeError("Key archive does not match the Hub backup")
        if any(manifest.get(name) != value for name, value in database_summary(database).items()):
            raise RuntimeError("SQLite contents do not match the Hub backup manifest")
        for name, raw in keys.items():
            atomic_private(stage / name, raw)
        cipher = CipherSuite(stage, keys[KEY].strip())
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            for table, column in SMOKE_FIELDS:
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if column in columns:
                    sample = db.execute(
                        f"SELECT {column} FROM {table} WHERE length({column}) > 0 LIMIT 1"
                    ).fetchone()
                    if sample:
                        cipher.decrypt(sample[0].encode())
        if destination is not None:
            _publish_restore(Path(destination).absolute(), database, keys)
    return manifest


def _publish_restore(destination, database, keys):
    # Only create a fresh directory. Existing live state and sidecars are never opened.
    destination.mkdir(mode=0o700)
    try:
        fd = os.open(destination / DATABASE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream, database.open("rb") as source:
            shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        for name, raw in keys.items():
            atomic_private(destination / name, raw)
        fsync_directory(destination)
        fsync_directory(destination.parent)
    except BaseException:
        for name in (DATABASE, *keys):
            (destination / name).unlink(missing_ok=True)
        destination.rmdir()
        raise
