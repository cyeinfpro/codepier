"""Lossless, offline split-state archives; never copies attachments or deletes sources.

Run as ``python -m scripts.panel_state_archive``. Export accepts ONLY a completed,
quiesced snapshot, not a live state volume (locks, WALs and unknown files fail).
The operator must keep the source quiesced throughout export. Archives include
private keys: gzip is NOT encryption. Keep archives and scratch directories on
operator-protected storage with the same at-rest encryption/access policy as the
source; never publish or casually share them. Run as the snapshot file owner
(including for UID-mapped containers); root is not an ownership bypass.
No credentials are created here.
Hashes detect corruption, not malicious replacement by someone with write access.
Restore never overwrites data; a failed partial restore has no initialization
marker until publication. An error after publication may mean durability is
uncertain: inspect the destination, never automatically delete or retry in place.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import zlib

from hub.keyring import CIPHER_COLUMNS, CipherSuite, private_read
from hub.state_layout import STATE_LAYOUT, STATE_MARKER, STATE_SNAPSHOT_FILES, STATE_SNAPSHOT_REQUIRED

MAGIC = b"CODEPIER-STATE\x00\x01"
VERSION = 1
CHUNK = 64 * 1024
HEADER_LIMIT = 16 * 1024
DEFAULT_LIMIT = 64 * 1024**3
FILES = STATE_SNAPSHOT_FILES
REQUIRED = STATE_SNAPSHOT_REQUIRED


def _path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Symlink paths are not accepted")
    return path


def _private_directory(path):
    path = _path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("Storage must be an owned private directory (0700)")
    return path


def _open(path):
    fd = os.open(_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Only owned regular single-link files are accepted")
        if info.st_mode & 0o077:
            raise ValueError("State and archives must be private (0600)")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _names(root):
    _private_directory(root)
    names = set()
    for child in root.iterdir():
        if child.name == "native-cli":
            _private_directory(child)
            for nested in child.iterdir():
                names.add("native-cli/" + nested.name)
            if "native-cli/native.sqlite3" not in names:
                raise ValueError("Native database missing")
        else:
            names.add(child.name)
    if not REQUIRED <= names or not names <= FILES:
        raise ValueError("Incomplete or unknown state; use a completed offline snapshot")
    for name in names:
        with _open(root / name):
            pass
    return sorted(names)


def _validate(root):
    names = _names(root)
    with _open(root / STATE_MARKER) as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("State marker too large")
    marker = json.loads(raw)
    if not isinstance(marker, dict) or type(marker.get("schema_version")) is not int or marker.get("schema_version") != 1 or marker.get("layout") != STATE_LAYOUT:
        raise ValueError("Unknown state marker")
    cipher = CipherSuite(root, private_read(root / "master.key").strip())
    for name in ("hub.sqlite3", "native-cli/native.sqlite3"):
        if name not in names:
            continue
        # immutable is safe ONLY because callers require a completed offline
        # snapshot with no sidecars; it also prevents SQLite creating files.
        with closing(sqlite3.connect((root / name).as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            # Bound SQLite materialization too, not only the gzip stream.
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024 * 1024)
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Database integrity failed")
            if name == "hub.sqlite3":
                for table, column in CIPHER_COLUMNS:
                    if column not in {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}:
                        continue
                    # Constants are trusted project schema, never manifest SQL.
                    for (value,) in db.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                        if value:
                            cipher.decrypt(value.encode() if isinstance(value, str) else value)
    return names


def _digest(path):
    digest = hashlib.sha256()
    length = 0
    with _open(path) as stream:
        while chunk := stream.read(CHUNK):
            length += len(chunk)
            digest.update(chunk)
    return length, digest.hexdigest()


def _manifest(stream, max_bytes):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Invalid decompression budget")
    if stream.read(len(MAGIC)) != MAGIC:
        raise ValueError("Unknown archive format")
    length = stream.read(4)
    if len(length) != 4:
        raise ValueError("Truncated archive")
    length = struct.unpack(">I", length)[0]
    if not 0 < length <= HEADER_LIMIT:
        raise ValueError("Invalid manifest size")
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError("Truncated manifest")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or set(manifest) != {"version", "layout", "files"} or type(manifest["version"]) is not int or manifest["version"] != VERSION or manifest["layout"] != STATE_LAYOUT:
        raise ValueError("Unknown manifest version or layout")
    entries = manifest["files"]
    if not isinstance(entries, list) or not 3 <= len(entries) <= len(FILES):
        raise ValueError("Invalid file list")
    names = set()
    total = compressed = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256", "gzip_bytes", "gzip_sha256"}:
            raise ValueError("Invalid manifest entry")
        name = entry["path"]
        if not isinstance(name, str) or name not in FILES or name in names:
            raise ValueError("Unknown or duplicate file")
        names.add(name)
        for field in ("bytes", "gzip_bytes"):
            if type(entry[field]) is not int or not 0 < entry[field] <= max_bytes:
                raise ValueError("File exceeds size budget")
        for field in ("sha256", "gzip_sha256"):
            if not isinstance(entry[field], str) or not re.fullmatch("[0-9a-f]{64}", entry[field]):
                raise ValueError("Invalid digest")
        if name in {STATE_MARKER, "master.key", "master.keys.json"} and entry["bytes"] > 65536:
            raise ValueError("Metadata exceeds size budget")
        total += entry["bytes"]
        compressed += entry["gzip_bytes"]
    if not REQUIRED <= names or total > max_bytes or compressed > max_bytes:
        raise ValueError("Incomplete archive or size budget exceeded")
    return manifest


def _extract(stream, root, max_bytes):
    manifest = _manifest(stream, max_bytes)
    for entry in manifest["files"]:
        target = root / entry["path"]
        target.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        remaining = entry["gzip_bytes"]
        raw_hash, zipped_hash = hashlib.sha256(), hashlib.sha256()
        produced = 0
        decoder = zlib.decompressobj(31)
        with os.fdopen(fd, "wb") as output:
            while remaining:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("Truncated gzip stream")
                remaining -= len(chunk)
                zipped_hash.update(chunk)
                while chunk:
                    clear = decoder.decompress(chunk, min(CHUNK, entry["bytes"] - produced + 1))
                    chunk = decoder.unconsumed_tail
                    produced += len(clear)
                    if produced > entry["bytes"] or decoder.unused_data:
                        raise ValueError("Gzip size mismatch or trailing members")
                    output.write(clear)
                    raw_hash.update(clear)
            if not decoder.eof or produced != entry["bytes"] or raw_hash.hexdigest() != entry["sha256"] or zipped_hash.hexdigest() != entry["gzip_sha256"]:
                raise ValueError("Archive checksum or length mismatch")
            output.flush()
            os.fsync(output.fileno())
    if stream.read(1):
        raise ValueError("Trailing archive data")
    _validate(root)
    return manifest


def export_state(source, archive, *, confirm_offline=False, max_bytes=DEFAULT_LIMIT):
    """Export a validated, completed offline snapshot without changing any byte."""
    if confirm_offline is not True:
        raise ValueError("Explicit offline snapshot confirmation required")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Invalid size budget")
    source, archive = _path(source), _path(archive)
    _private_directory(archive.parent)
    if source == archive.parent or source in archive.parents:
        raise ValueError("Archive must be outside the source snapshot")
    if archive.exists() or archive.is_symlink():
        raise FileExistsError("Archive already exists")
    names = _validate(source)
    with tempfile.TemporaryDirectory(prefix=".state-archive-", dir=archive.parent) as scratch:
        scratch = Path(scratch)
        entries = []
        total = 0
        for index, name in enumerate(names):
            target = scratch / str(index)
            raw_hash = hashlib.sha256()
            size = 0
            compressor = zlib.compressobj(3, zlib.DEFLATED, 31)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as outgoing, _open(source / name) as incoming:
                while chunk := incoming.read(CHUNK):
                    size += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("State exceeds size budget")
                    raw_hash.update(chunk)
                    outgoing.write(compressor.compress(chunk))
                outgoing.write(compressor.flush())
            compressed_size, compressed_hash = _digest(target)
            entries.append({"path": name, "bytes": size, "sha256": raw_hash.hexdigest(), "gzip_bytes": compressed_size, "gzip_sha256": compressed_hash})
        # Detect accidental source changes before accepting the archive; this is
        # not a substitute for the operator's offline/quiescence precondition.
        if _validate(source) != names or any(_digest(source / e["path"]) != (e["bytes"], e["sha256"]) for e in entries):
            raise ValueError("Source snapshot changed during export")
        manifest = {"version": VERSION, "layout": STATE_LAYOUT, "files": entries}
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        temporary = scratch / "archive"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(MAGIC + struct.pack(">I", len(raw)) + raw)
            for index in range(len(entries)):
                with _open(scratch / str(index)) as incoming:
                    shutil.copyfileobj(incoming, output, CHUNK)
            output.flush()
            os.fsync(output.fileno())
        # Full independent decompression/DB/key verification before publication.
        verify_archive(temporary, max_bytes=max_bytes)
        os.link(temporary, archive, follow_symlinks=False)  # atomic NO-REPLACE
        temporary.unlink()
        _sync(archive.parent)
        return manifest


def verify_archive(archive, *, max_bytes=DEFAULT_LIMIT):
    archive = _path(archive)
    _private_directory(archive.parent)
    with _open(archive) as stream, tempfile.TemporaryDirectory(prefix=".state-verify-", dir=archive.parent) as scratch:
        return _extract(stream, Path(scratch), max_bytes)


def restore_state(archive, destination, *, max_bytes=DEFAULT_LIMIT):
    """Restore only into an existing empty private directory; marker is last."""
    destination = _private_directory(destination)
    if any(destination.iterdir()):
        raise ValueError("Restore destination must be empty")
    archive = _path(archive)
    _private_directory(archive.parent)
    with _open(archive) as stream, tempfile.TemporaryDirectory(prefix=".state-restore-", dir=destination) as scratch:
        scratch = Path(scratch)
        manifest = _extract(stream, scratch, max_bytes)
        if set(destination.iterdir()) != {scratch}:
            raise ValueError("Restore destination changed")
        names = [e["path"] for e in manifest["files"] if e["path"] != STATE_MARKER]
        for name in names:
            target = destination / name
            if target.parent != destination and not target.parent.exists():
                target.parent.mkdir(mode=0o700)
            _private_directory(target.parent)
            os.link(scratch / name, target, follow_symlinks=False)
            (scratch / name).unlink()
        if (destination / "native-cli").exists():
            _sync(destination / "native-cli")
        _sync(destination)
        os.link(scratch / STATE_MARKER, destination / STATE_MARKER, follow_symlinks=False)
        (scratch / STATE_MARKER).unlink()
        _sync(destination)
    _sync(destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "verify", "restore"))
    parser.add_argument("source")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--confirm-offline", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    if args.operation != "verify" and not args.destination:
        parser.error("destination is required")
    try:
        if args.operation == "export":
            result = export_state(args.source, args.destination, confirm_offline=args.confirm_offline, max_bytes=args.max_bytes)
        elif args.operation == "restore":
            result = restore_state(args.source, args.destination, max_bytes=args.max_bytes)
        else:
            result = verify_archive(args.source, max_bytes=args.max_bytes)
    except Exception:
        # No database values, key material or third-party exception payloads.
        parser.exit(1, "State archive operation failed; original state retained. Inspect private storage.\n")
    print(json.dumps({"verified": True, "files": len(result["files"]), "bytes": sum(e["bytes"] for e in result["files"]), "attachments_copied": False}))


if __name__ == "__main__":
    main()
