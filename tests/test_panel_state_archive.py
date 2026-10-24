"""Synthetic-only lossless archive, corruption and fail-closed safety tests."""
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from cryptography.fernet import Fernet

from scripts import panel_state_archive as archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir(mode=0o700)
        self.destination = self.root / 'restored'
        self.destination.mkdir(mode=0o700)
        self.archive = self.root / 'backup.cps'
        self.key = Fernet.generate_key()
        self.write('master.key', self.key)
        self.write(archive.STATE_MARKER, json.dumps({'schema_version': 1, 'layout': archive.STATE_LAYOUT}).encode())
        with sqlite3.connect(self.source / 'hub.sqlite3') as db:
            db.execute('CREATE TABLE devices(id TEXT PRIMARY KEY,secret TEXT)')
            db.execute('INSERT INTO devices(rowid,id,secret) VALUES(19,?,?)', ('fixture', Fernet(self.key).encrypt(b'synthetic-only').decode()))
            db.execute('CREATE TABLE holes(value TEXT)')
            db.executemany('INSERT INTO holes(rowid,value) VALUES(?,?)', [(7, 'a'), (9999, 'z')])
        os.chmod(self.source / 'hub.sqlite3', 0o600)

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o600)

    def export(self):
        return archive.export_state(self.source, self.archive, confirm_offline=True)

    def rewrite_manifest(self, change):
        data = self.archive.read_bytes()
        offset = len(archive.MAGIC)
        size = struct.unpack('>I', data[offset:offset + 4])[0]
        manifest = json.loads(data[offset + 4:offset + 4 + size])
        payload = data[offset + 4 + size:]
        change(manifest)
        raw = json.dumps(manifest).encode()
        self.archive.write_bytes(archive.MAGIC + struct.pack('>I', len(raw)) + raw + payload)

    def test_exact_bytes_rows_and_private_permissions(self):
        native = self.source / 'native-cli'
        native.mkdir(mode=0o700)
        with sqlite3.connect(native / 'native.sqlite3') as db:
            db.execute('CREATE TABLE sessions(value TEXT)')
            db.execute("INSERT INTO sessions VALUES('synthetic')")
        (native / 'native.sqlite3').chmod(0o600)
        before = {name: (self.source / name).read_bytes() for name in archive._names(self.source)}
        self.export()
        archive.verify_archive(self.archive)
        archive.restore_state(self.archive, self.destination)
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        for name, content in before.items():
            self.assertEqual(content, (self.source / name).read_bytes())
            self.assertEqual(content, (self.destination / name).read_bytes())
            self.assertEqual((self.destination / name).stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.destination / 'hub.sqlite3') as db:
            self.assertEqual(db.execute('SELECT rowid,value FROM holes').fetchall(), [(7, 'a'), (9999, 'z')])

    def test_requires_offline_confirmation(self):
        with self.assertRaises(ValueError):
            archive.export_state(self.source, self.archive)
        self.assertFalse(self.archive.exists())

    def test_unknown_sidecars_and_rekey_fail_closed(self):
        for name in ('attachments', 'hub.sqlite3-wal', '.hub.lock', 'rekey.json', '.rekey', 'native-cli/locks'):
            with self.subTest(name=name):
                self.write(name, b'not archived')
                with self.assertRaises(ValueError):
                    self.export()
                (self.source / name).unlink()
                native = self.source / 'native-cli'
                if native.exists():
                    native.rmdir()
        self.assertFalse(self.archive.exists())

    def test_wrong_keys_and_invalid_sqlite(self):
        self.write('master.key', Fernet.generate_key())
        with self.assertRaises(Exception):
            self.export()
        self.write('master.key', self.key)
        self.write('hub.sqlite3', b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            self.export()

    def test_no_overwrite_and_nonempty_restore(self):
        self.export()
        before = self.archive.read_bytes()
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.archive.read_bytes(), before)
        (self.destination / 'keep').write_text('keep')
        with self.assertRaises(ValueError):
            archive.restore_state(self.archive, self.destination)
        self.assertEqual((self.destination / 'keep').read_text(), 'keep')

    def test_truncation_corruption_and_trailing_bytes(self):
        self.export()
        good = self.archive.read_bytes()
        for bad in (good[:-1], good[:5], good + b'extra', good[:-8] + bytes([good[-8] ^ 0xff]) + good[-7:]):
            with self.subTest(length=len(bad)):
                self.archive.write_bytes(bad)
                with self.assertRaises((ValueError, zlib.error)):
                    archive.restore_state(self.archive, self.destination)
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_manifest_rejects_version_paths_duplicates_hash_and_limits(self):
        self.export()
        good = self.archive.read_bytes()
        mutations = [lambda m: m.update(version=2), lambda m: m.update(version=True),
                     lambda m: m['files'][0].update(path='../outside'),
                     lambda m: m['files'][0].update(path='/tmp/absolute'),
                     lambda m: m['files'].append(m['files'][0]),
                     lambda m: m['files'][0].update(sha256='0' * 64),
                     lambda m: m['files'][0].update(gzip_sha256='0' * 64),
                     lambda m: m['files'][0].update(bytes=archive.DEFAULT_LIMIT + 1)]
        for mutate in mutations:
            self.archive.write_bytes(good)
            self.rewrite_manifest(mutate)
            with self.assertRaises(ValueError):
                archive.verify_archive(self.archive)
        self.archive.write_bytes(good)
        with self.assertRaises(ValueError):
            archive.verify_archive(self.archive, max_bytes=1)

    def test_bomb_is_bounded_by_declared_raw_size(self):
        self.export()
        self.rewrite_manifest(lambda m: m['files'][1].update(bytes=1))
        with self.assertRaises(ValueError):
            archive.verify_archive(self.archive)

    def test_symlink_hardlink_and_public_permissions_rejected(self):
        key = self.source / 'master.key'
        other = self.root / 'other'
        key.rename(other)
        key.symlink_to(other)
        with self.assertRaises(ValueError):
            self.export()
        key.unlink()
        os.link(other, key)
        with self.assertRaises(ValueError):
            self.export()
        other.unlink()
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            self.export()
        key.chmod(0o600)
        self.export()
        self.archive.chmod(0o644)
        with self.assertRaises(ValueError):
            archive.verify_archive(self.archive)

    def test_export_publish_failure_cleans_only_own_temps(self):
        sentinel = self.root / 'unrelated'
        sentinel.write_text('keep')
        with patch.object(archive.os, 'link', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.export()
        self.assertFalse(self.archive.exists())
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse(list(self.root.glob('.state-*')))
        self.assertTrue((self.source / archive.STATE_MARKER).exists())

    def test_restore_partial_failure_never_publishes_marker(self):
        self.export()
        real_link = os.link
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic partial restore')
            return real_link(*args, **kwargs)
        with patch.object(archive.os, 'link', side_effect=fail_second):
            with self.assertRaises(OSError):
                archive.restore_state(self.archive, self.destination)
        self.assertFalse((self.destination / archive.STATE_MARKER).exists())
        self.assertTrue(list(self.destination.iterdir()))
        self.assertFalse(list(self.destination.glob('.state-*')))
        self.assertTrue(self.archive.exists())

    def test_restore_revalidates_keys_not_only_hashes(self):
        self.export()
        # Checksums are not authentication. A self-consistent archive with a
        # mismatched key must still fail semantic verification.
        with patch.object(archive, '_validate', return_value=sorted(archive.REQUIRED)):
            self.write('master.key', Fernet.generate_key())
            self.archive.unlink()
            self.export()
        with self.assertRaises(Exception):
            archive.restore_state(self.archive, self.destination)
        self.assertFalse((self.destination / archive.STATE_MARKER).exists())

    def test_large_multichunk_roundtrip_and_keyring(self):
        from hub.keyring import encode_ring, key_id
        self.write('master.keys.json', encode_ring(key_id(self.key), {key_id(self.key): self.key}))
        with sqlite3.connect(self.source / 'hub.sqlite3') as db:
            db.execute('CREATE TABLE bulk(value BLOB)')
            db.executemany('INSERT INTO bulk VALUES(?)', [(b'x' * 200000,), (os.urandom(200000),)])
        before = (self.source / 'hub.sqlite3').read_bytes()
        self.export()
        archive.restore_state(self.archive, self.destination)
        self.assertEqual((self.destination / 'hub.sqlite3').read_bytes(), before)
        self.assertEqual((self.destination / 'master.keys.json').read_bytes(), (self.source / 'master.keys.json').read_bytes())

    def test_concurrent_archive_destination_never_overwritten(self):
        real_link = os.link
        def race(source, destination, **kwargs):
            Path(destination).write_bytes(b'other operator archive')
            return real_link(source, destination, **kwargs)
        with patch.object(archive.os, 'link', side_effect=race):
            with self.assertRaises(FileExistsError):
                self.export()
        self.assertEqual(self.archive.read_bytes(), b'other operator archive')
        self.assertFalse(list(self.root.glob('.state-*')))

    def test_fsync_failure_before_publish_retains_source(self):
        before = (self.source / 'hub.sqlite3').read_bytes()
        with patch.object(archive.os, 'fsync', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError):
                self.export()
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.source / 'hub.sqlite3').read_bytes(), before)
        self.assertFalse(list(self.root.glob('.state-*')))

    def test_symlink_restore_root_and_archive_rejected(self):
        self.export()
        linked = self.root / 'linked'
        linked.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            archive.restore_state(self.archive, linked)
        linked.unlink()
        linked.symlink_to(self.archive)
        with self.assertRaises(ValueError):
            archive.verify_archive(linked)


if __name__ == '__main__':
    unittest.main()
