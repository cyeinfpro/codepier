"""Reject invalid release ZIP metadata before any member is decompressed."""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import struct
import zipfile

import pytest

from scripts import check_release as guard


def archive_fixture(tmp_path: Path, *, source_extra: bytes = b'', manifest_extra: bytes = b''):
    source = b'# CodePier fixture\n'
    expected = {'README.md': {'sha256': hashlib.sha256(source).hexdigest(),
                              'bytes': len(source), 'mode': 0o644}}
    manifest = (expected['README.md']['sha256'] + '  README.md\n').encode()
    destination = tmp_path / 'source.zip'
    with zipfile.ZipFile(destination, 'w') as archive:
        for name, raw in [('README.md', source + source_extra),
                          ('MANIFEST.sha256', manifest + manifest_extra)]:
            entry = zipfile.ZipInfo(name, date_time=(2026, 9, 14, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    return destination, expected


def forbid_decompression(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('Invalid ZIP metadata must be rejected before decompression')
    monkeypatch.setattr(zipfile.ZipFile, 'testzip', fail)
    monkeypatch.setattr(zipfile.ZipFile, 'read', fail)


@pytest.mark.parametrize('member', ['source', 'manifest'])
def test_size_mismatch_is_rejected_before_decompression(tmp_path, monkeypatch, member):
    arguments = {'source_extra' if member == 'source' else 'manifest_extra': b'x' * 4096}
    destination, expected = archive_fixture(tmp_path, **arguments)
    forbid_decompression(monkeypatch)
    with pytest.raises(ValueError, match='(?:size|differs)'):
        guard.check_archive(destination, expected)


def test_encryption_is_rejected_before_password_or_decompression(tmp_path, monkeypatch):
    destination, expected = archive_fixture(tmp_path)
    raw = bytearray(destination.read_bytes())
    central = raw.index(b'PK\x01\x02')
    flags = struct.unpack_from('<H', raw, central + 8)[0]
    struct.pack_into('<H', raw, central + 8, flags | 1)
    destination.write_bytes(raw)
    forbid_decompression(monkeypatch)
    with pytest.raises(ValueError, match='encrypted'):
        guard.check_archive(destination, expected)


def test_valid_bounded_archive_still_verifies(tmp_path):
    destination, expected = archive_fixture(tmp_path)
    result = guard.check_archive(destination, expected)
    assert result['verified_against_current_source'] is True
    assert result['sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()


@pytest.mark.parametrize('method', [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, 99])
def test_unapproved_codec_is_rejected_before_decompression(tmp_path, monkeypatch, method):
    destination, expected = archive_fixture(tmp_path)
    raw = bytearray(destination.read_bytes())
    central = raw.index(b'PK\x01\x02')
    struct.pack_into('<H', raw, central + 10, method)
    destination.write_bytes(raw)
    forbid_decompression(monkeypatch)
    with pytest.raises(ValueError, match='compression'):
        guard.check_archive(destination, expected)


def test_member_reads_are_bounded(tmp_path, monkeypatch):
    destination, expected = archive_fixture(tmp_path)
    original = zipfile.ZipExtFile.read
    sizes = []
    def bounded(stream, size=-1):
        assert 0 < size <= 65536, 'Archive members must be read in bounded chunks'
        sizes.append(size)
        return original(stream, size)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', bounded)
    assert guard.check_archive(destination, expected)['verified_against_current_source']
    assert sizes


def test_archive_digest_does_not_load_whole_file(tmp_path, monkeypatch):
    destination, expected = archive_fixture(tmp_path)
    original = Path.read_bytes
    def bounded(path):
        assert path != destination, 'Archive SHA must be streamed, not read_bytes()'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', bounded)
    assert guard.check_archive(destination, expected)['verified_against_current_source']


def test_archive_size_is_checked_before_zip_parsing(tmp_path, monkeypatch):
    destination = tmp_path / 'oversize.zip'
    with destination.open('wb') as stream:
        stream.truncate(256 * 1024 * 1024 + 1)
    def forbid(*args, **kwargs):
        pytest.fail('Oversize ZIP must not be parsed')
    monkeypatch.setattr(zipfile, 'ZipFile', forbid)
    with pytest.raises(ValueError, match='size|MiB'):
        guard.check_archive(destination, {})


def test_corrupt_member_crc_is_still_rejected(tmp_path):
    destination, expected = archive_fixture(tmp_path)
    raw = bytearray(destination.read_bytes())
    central = raw.index(b'PK\x01\x02')
    crc = struct.unpack_from('<I', raw, central + 16)[0]
    struct.pack_into('<I', raw, central + 16, crc ^ 1)
    destination.write_bytes(raw)
    with pytest.raises((ValueError, zipfile.BadZipFile), match='CRC'):
        guard.check_archive(destination, expected)


def test_replaced_archive_is_not_certified(tmp_path, monkeypatch):
    destination, expected = archive_fixture(tmp_path)
    replacement = tmp_path / 'replacement.bin'
    replacement.write_bytes(b'not the verified archive')
    original = zipfile.ZipExtFile.read
    replaced = []
    def read_and_replace(stream, size=-1):
        data = original(stream, size)
        if not replaced:
            replacement.replace(destination)
            replaced.append(True)
        return data
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', read_and_replace)
    with pytest.raises(ValueError, match='changed|replaced'):
        guard.check_archive(destination, expected)
    assert replaced
