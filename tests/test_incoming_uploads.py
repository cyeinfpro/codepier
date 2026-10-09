"""Offline resumable-ingress contracts; all files and identities are synthetic."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import incoming_uploads as incoming
from agent.filesystem import FileEngine
from agent.journal import Journal
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config_path = tmp_path / 'config.json'
    config_path.write_text('{}')
    journal = Journal(tmp_path / 'state')
    config = {'allowed_roots': [{'path': str(root), 'writable': True}], 'tasks': {}}
    engine = FileEngine(config, journal, config_path)
    project = {'id': 'project-a', 'root': str(root), 'mode': 'write',
               '_coding_owner': 'owner-a', '_coding_device': 'node-a',
               '_coding_scopes': ['write'], '_workspace_id': ''}
    yield incoming.IncomingUploads(engine), engine, project, root
    engine.journal.db.close()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def begin_args(data=b'abcdef', path='binary.dat'):
    return {'path': path, 'size': len(data), 'sha256': sha(data)}


def chunk_args(identifier, data=b'abcdef', offset=0):
    return {'upload_id': identifier, 'offset': offset, 'data': data, 'chunk_sha256': sha(data)}


def start(workspace, data=b'abcdef', path='binary.dat', identifier=None):
    uploads, _, project, _ = workspace
    identifier = identifier or uuid.uuid4().hex
    uploads.begin(identifier, project, begin_args(data, path))
    return identifier


def filled(workspace, data=b'abcdef', path='binary.dat'):
    uploads, _, project, _ = workspace
    identifier = start(workspace, data, path)
    for offset in range(0, len(data), incoming.CHUNK_BYTES):
        uploads.chunk(project, chunk_args(identifier, data[offset:offset + incoming.CHUNK_BYTES], offset))
    return identifier


def spool_path(engine, identifier):
    return engine.journal.directory / 'incoming-upload' / (identifier + '.part')


def row_for(engine, identifier):
    return engine.journal.db.execute('SELECT * FROM incoming_uploads WHERE id=?', (identifier,)).fetchone()


def expect(code, call):
    with pytest.raises(DevError) as error:
        call()
    assert error.value.code == code
    return error.value


@pytest.mark.parametrize('data,path', [
    (b'', 'empty.bin'),
    (b'\x00\xff\x80PK\x03\x04not-a-real-archive', '资料/附件 🧪.unknown-extension'),
    (b'#!/bin/sh\nexit 99\n', 'payload.exe'),
    (bytes(range(256)) * 2049, 'multi-chunk.bin'),
], ids=['empty', 'unicode-binary', 'executable-extension', 'multi-chunk'])
def test_binary_roundtrip_is_private_non_executable_and_exact(workspace, data, path):
    uploads, engine, project, root = workspace
    identifier = filled(workspace, data, path)
    pending = uploads.status(project, {'upload_id': identifier})
    assert pending['received'] == len(data)
    assert pending['ready'] is False and pending['created'] is False
    before = spool_path(engine, identifier).stat()
    result = uploads.finish(project, {'upload_id': identifier})
    assert result == {
        'upload_id': identifier, 'path': path, 'bytes': len(data), 'received': len(data),
        'sha256': sha(data), 'ready': True, 'created': True,
        'state': 'complete', 'expires': result['expires'],
    }
    target = root / path
    assert target.read_bytes() == data
    assert (target.stat().st_dev, target.stat().st_ino) != (before.st_dev, before.st_ino)
    assert target.stat().st_nlink == 1
    if os.name != 'nt':
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not spool_path(engine, identifier).exists()
    assert not list(root.rglob('.rd-import-*'))
    assert uploads.finish(project, {'upload_id': identifier}) == result
    assert uploads.begin(identifier, project, begin_args(data, path)) == result
    assert uploads.status(project, {'upload_id': identifier}) == result
    serialized = json.dumps(result)
    assert str(engine.journal.directory) not in serialized
    assert 'spool' not in serialized and 'staging' not in serialized


def test_metadata_and_owner_overrides_are_not_trusted(workspace):
    uploads, engine, project, _ = workspace
    identifier = uuid.uuid4().hex
    args = {**begin_args(), 'mime_type': 'application/x-executable', 'owner': 'another-owner',
            'download_url': 'https://unused.invalid/PRIVATE_SIGNATURE', 'data': b'secret'}
    uploads.begin(identifier, project, args)
    record = row_for(engine, identifier)
    assert record['owner'] == project['_coding_owner']
    saved = json.dumps(dict(record))
    assert 'PRIVATE_SIGNATURE' not in saved
    assert 'secret' not in saved
    assert 'another-owner' not in saved


def test_resume_after_journal_restart_and_duplicate_chunks(workspace):
    uploads, engine, project, root = workspace
    identifier = start(workspace)
    uploads.chunk(project, chunk_args(identifier, b'abc'))
    first = uploads.chunk(project, chunk_args(identifier, b'abc'))
    assert first['received'] == 3
    directory = engine.journal.directory
    engine.journal.db.close()
    engine.journal = Journal(directory)
    resumed = incoming.IncomingUploads(engine)
    assert resumed.begin(identifier, project, begin_args())['received'] == 3
    resumed.chunk(project, chunk_args(identifier, b'def', 3))
    result = resumed.finish(project, {'upload_id': identifier})
    assert result['created'] and (root / 'binary.dat').read_bytes() == b'abcdef'


@pytest.mark.parametrize('change', [
    {'path': 'another.bin'}, {'size': 5}, {'sha256': '0' * 64},
])
def test_begin_conflicts_do_not_change_existing_reservation(workspace, change):
    uploads, engine, project, _ = workspace
    identifier = start(workspace)
    before = dict(row_for(engine, identifier))
    expect('UPLOAD_CONFLICT', lambda: uploads.begin(identifier, project, {**begin_args(), **change}))
    assert dict(row_for(engine, identifier)) == before


@pytest.mark.parametrize('offset,data,code', [
    (1, b'b', 'UPLOAD_OFFSET'),
    (5, b'xx', 'UPLOAD_OFFSET'),
    (-1, b'a', 'INVALID_UPLOAD_CHUNK'),
    (True, b'a', 'INVALID_UPLOAD_CHUNK'),
    (0, b'', 'INVALID_UPLOAD_CHUNK'),
    (0, b'x' * (incoming.CHUNK_BYTES + 1), 'INVALID_UPLOAD_CHUNK'),
], ids=['gap', 'overflow', 'negative', 'boolean', 'empty', 'oversized'])
def test_bad_chunks_leave_offset_and_bytes_unchanged(workspace, offset, data, code):
    uploads, engine, project, _ = workspace
    identifier = start(workspace)
    expect(code, lambda: uploads.chunk(project, chunk_args(identifier, data, offset)))
    assert row_for(engine, identifier)['received'] == 0
    assert spool_path(engine, identifier).read_bytes() == b''


def test_retry_does_not_extend_partial_overlap_and_requires_exact_bytes(workspace):
    uploads, _, project, _ = workspace
    identifier = start(workspace)
    uploads.chunk(project, chunk_args(identifier, b'abc'))
    expect('UPLOAD_CONFLICT', lambda: uploads.chunk(project, chunk_args(identifier, b'abx')))
    expect('UPLOAD_OFFSET', lambda: uploads.chunk(project, chunk_args(identifier, b'bcde', 1)))
    assert uploads.chunk(project, chunk_args(identifier, b'bc', 1))['received'] == 3
    args = {**chunk_args(identifier, b'def', 3), 'chunk_sha256': '0' * 64}
    expect('UPLOAD_CHUNK_INTEGRITY', lambda: uploads.chunk(project, args))


def test_incomplete_and_full_checksum_mismatch_never_publish(workspace):
    uploads, engine, project, root = workspace
    identifier = start(workspace)
    expect('UPLOAD_INCOMPLETE', lambda: uploads.finish(project, {'upload_id': identifier}))
    uploads.chunk(project, chunk_args(identifier, b'abcdeg'))
    expect('UPLOAD_INTEGRITY', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert row_for(engine, identifier)['state'] == 'failed'
    assert not (root / 'binary.dat').exists()
    expect('UPLOAD_INTEGRITY', lambda: uploads.status(project, {'upload_id': identifier}))


def test_fsync_before_database_ack_reconciles_tail_on_restart(workspace):
    uploads, engine, project, root = workspace
    identifier = start(workspace)
    uploads.chunk(project, chunk_args(identifier, b'abc'))
    db = engine.journal.db
    with db:
        db.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF received ON incoming_uploads
                      BEGIN SELECT RAISE(ABORT, 'synthetic acknowledgement failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        uploads.chunk(project, chunk_args(identifier, b'def', 3))
    assert row_for(engine, identifier)['received'] == 3
    assert spool_path(engine, identifier).read_bytes() == b'abcdef'
    with db:
        db.execute('DROP TRIGGER fail_ack')
    resumed = incoming.IncomingUploads(engine)
    assert resumed.status(project, {'upload_id': identifier})['received'] == 3
    assert spool_path(engine, identifier).read_bytes() == b'abc'
    resumed.chunk(project, chunk_args(identifier, b'def', 3))
    resumed.finish(project, {'upload_id': identifier})
    assert (root / 'binary.dat').read_bytes() == b'abcdef'


def test_reservation_is_durable_before_file_creation(workspace, monkeypatch):
    uploads, engine, project, _ = workspace
    identifier = uuid.uuid4().hex
    original = incoming._Spool.open
    def interrupted(self, row, *, create=False):
        assert row_for(engine, identifier) is not None
        if create:
            raise OSError('synthetic create interruption')
        return original(self, row, create=create)
    monkeypatch.setattr(incoming._Spool, 'open', interrupted)
    expect('UPLOAD_STORAGE', lambda: uploads.begin(identifier, project, begin_args()))
    assert row_for(engine, identifier)['received'] == 0
    assert not spool_path(engine, identifier).exists()
    monkeypatch.setattr(incoming._Spool, 'open', original)
    assert uploads.begin(identifier, project, begin_args())['received'] == 0


def test_failed_reservation_never_creates_spool(workspace):
    uploads, engine, project, _ = workspace
    with engine.journal.db:
        engine.journal.db.execute("""CREATE TRIGGER fail_reserve BEFORE INSERT ON incoming_uploads
                                    BEGIN SELECT RAISE(ABORT, 'synthetic reservation failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        uploads.begin(uuid.uuid4().hex, project, begin_args())
    assert not (engine.journal.directory / 'incoming-upload').exists()


@pytest.mark.parametrize('field,value', [
    ('_coding_owner', 'owner-b'), ('_integration_owner', 'owner-b'), ('id', 'project-b'),
    ('_workspace_id', 'different-workspace'), ('_coding_device', 'node-b'),
    ('_original_root', '/changed-original-root'),
])
@pytest.mark.parametrize('method', ['begin', 'status', 'chunk', 'finish'])
def test_exact_binding_at_every_step_even_for_admin(workspace, field, value, method):
    uploads, _, project, _ = workspace
    identifier = start(workspace)
    other = {**project, field: value, '_integration_admin': True}
    if method == 'begin':
        call = lambda: uploads.begin(identifier, other, begin_args())
    elif method == 'chunk':
        call = lambda: uploads.chunk(other, chunk_args(identifier))
    else:
        call = lambda: getattr(uploads, method)(other, {'upload_id': identifier})
    expect('UPLOAD_NOT_FOUND', call)


@pytest.mark.parametrize('scopes', [None, [], ['read'], ['execute'], 'write', ['write', 1]])
@pytest.mark.parametrize('method', ['begin', 'status', 'chunk', 'finish'])
def test_write_scope_cannot_be_missing_revoked_or_replaced_with_execute(workspace, scopes, method):
    uploads, _, project, _ = workspace
    identifier = start(workspace)
    other = {**project, '_coding_scopes': scopes, '_integration_admin': True}
    if method == 'begin':
        call = lambda: uploads.begin(identifier, other, begin_args())
    elif method == 'chunk':
        call = lambda: uploads.chunk(other, chunk_args(identifier))
    else:
        call = lambda: getattr(uploads, method)(other, {'upload_id': identifier})
    expect('UPLOAD_FORBIDDEN', call)


def test_owner_is_required_despite_caller_override(workspace):
    uploads, _, project, _ = workspace
    other = {k: v for k, v in project.items() if k != '_coding_owner'}
    expect('INTEGRATION_OWNER_REQUIRED', lambda: uploads.begin(
        uuid.uuid4().hex, other, {**begin_args(), 'owner': 'owner-a'}))


def test_root_recreation_and_workspace_retarget_are_not_same_binding(workspace):
    uploads, engine, project, root = workspace
    identifier = start(workspace)
    moved = root.with_name('old-project')
    root.rename(moved)
    root.mkdir()
    expect('UPLOAD_NOT_FOUND', lambda: uploads.status(project, {'upload_id': identifier}))
    project = {**project, '_original_root': project['root'], 'root': str(moved)}
    engine.config['allowed_roots'].append({'path': str(moved), 'writable': True})
    expect('UPLOAD_NOT_FOUND', lambda: uploads.finish(project, {'upload_id': identifier}))


@pytest.mark.parametrize('revoke', ['root', 'nested', 'mode'])
def test_local_write_policy_rechecked_on_every_step(workspace, revoke):
    uploads, engine, project, root = workspace
    identifier = start(workspace, path='nested/binary.dat')
    if revoke == 'root':
        engine.config['allowed_roots'][0]['writable'] = False
    elif revoke == 'nested':
        (root / 'nested').mkdir()
        engine.config['allowed_roots'].append({'path': str(root / 'nested'), 'writable': False})
    else:
        project['mode'] = 'read'
    for call in [
        lambda: uploads.begin(identifier, project, begin_args(path='nested/binary.dat')),
        lambda: uploads.status(project, {'upload_id': identifier}),
        lambda: uploads.chunk(project, chunk_args(identifier)),
        lambda: uploads.finish(project, {'upload_id': identifier}),
    ]:
        expect('READ_ONLY', call)


def test_write_revocation_during_copy_is_checked_before_publication(workspace, monkeypatch):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    original = incoming.AnchoredDestination.write
    def revoke(self, data):
        original(self, data)
        project['_coding_scopes'] = ['read']
    monkeypatch.setattr(incoming.AnchoredDestination, 'write', revoke)
    expect('UPLOAD_FORBIDDEN', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert not (root / 'binary.dat').exists()
    assert not list(root.glob('.rd-import-*'))
    assert row_for(engine, identifier)['state'] == 'staging'


@pytest.mark.parametrize('path', [
    '../escape', '/absolute', 'a\\b', 'a:stream', '.git/config',
    '.env', 'private.pem', '.codex/sessions/file', '.rd-private',
    'CON', 'NUL.bin', 'lpt1.txt', 'file.', 'file ', 'nested/bad\nname',
])
def test_paths_reject_traversal_protected_and_nonportable_names(workspace, path):
    uploads, _, project, root = workspace
    with pytest.raises(DevError):
        uploads.begin(uuid.uuid4().hex, project, begin_args(path=path))
    assert list(root.iterdir()) == []


def test_same_path_and_same_content_never_overwrite_or_adopt(workspace):
    uploads, _, project, root = workspace
    first = filled(workspace)
    second = filled(workspace)
    uploads.finish(project, {'upload_id': first})
    expect('ARTIFACT_DESTINATION_EXISTS', lambda: uploads.finish(project, {'upload_id': second}))
    expect('ARTIFACT_DESTINATION_EXISTS', lambda: start(workspace))
    assert (root / 'binary.dat').read_bytes() == b'abcdef'


def test_concurrent_duplicate_steps_across_instances_are_serialized(workspace):
    uploads, engine, project, root = workspace
    other = incoming.IncomingUploads(engine)
    identifier = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=8) as pool:
        begun = list(pool.map(lambda i: (uploads if i % 2 else other).begin(identifier, project, begin_args()), range(16)))
        chunks = list(pool.map(lambda i: (uploads if i % 2 else other).chunk(project, chunk_args(identifier)), range(16)))
        results = list(pool.map(lambda i: (uploads if i % 2 else other).finish(project, {'upload_id': identifier}), range(16)))
    assert len({r['received'] for r in begun}) == 1
    assert all(r['received'] == 6 for r in chunks)
    assert all(r == results[0] for r in results)
    assert (root / 'binary.dat').read_bytes() == b'abcdef'
    assert engine.journal.db.execute('SELECT count(*) FROM incoming_uploads').fetchone()[0] == 1


def test_size_validation_and_double_copy_reservation_limit(workspace):
    uploads, engine, project, _ = workspace
    for size in [-1, True, 1.0, incoming.DEFAULT_MAX_IMPORT_BYTES + 1]:
        expect('UPLOAD_TOO_LARGE', lambda: uploads.begin(
            uuid.uuid4().hex, project, {**begin_args(), 'size': size}))
    engine.config['integrations'] = {'max_import_bytes': incoming.MAX_IMPORT_BYTES + 1}
    expect('UPLOAD_CONFIGURATION', lambda: start(workspace))
    engine.config['integrations']['max_import_bytes'] = incoming.MAX_IMPORT_BYTES
    for i in range(2):
        uploads.begin(uuid.uuid4().hex, project, {
            **begin_args(path=f'large-{i}'), 'size': incoming.MAX_IMPORT_BYTES})
    expect('UPLOAD_QUOTA', lambda: uploads.begin(uuid.uuid4().hex, project, {
        **begin_args(path='over-quota'), 'size': 1}))
    assert incoming.RESERVED_BYTES == 2 * 1024 ** 3


def test_owner_and_node_count_quotas(workspace, monkeypatch):
    uploads, _, project, _ = workspace
    assert incoming.OWNER_UPLOADS == 32 and incoming.NODE_UPLOADS == 256
    monkeypatch.setattr(incoming, 'OWNER_UPLOADS', 2)
    monkeypatch.setattr(incoming, 'NODE_UPLOADS', 3)
    start(workspace, b'', 'one')
    start(workspace, b'', 'two')
    expect('UPLOAD_QUOTA', lambda: start(workspace, b'', 'three'))
    other = {**project, '_coding_owner': 'owner-b'}
    uploads.begin(uuid.uuid4().hex, other, begin_args(b'', 'three'))
    expect('UPLOAD_QUOTA', lambda: uploads.begin(uuid.uuid4().hex, {
        **project, '_coding_owner': 'owner-c'}, begin_args(b'', 'four')))


def test_ttl_cleanup_only_owned_expired_spool_and_preserves_receipt(workspace, monkeypatch):
    uploads, engine, project, root = workspace
    clock = [1000.0]
    monkeypatch.setattr(incoming.time, 'time', lambda: clock[0])
    success = filled(workspace, path='success')
    uploads.finish(project, {'upload_id': success})
    pending = start(workspace, path='pending')
    unrelated = engine.journal.directory / 'incoming-upload' / 'unregistered.part'
    unrelated.write_bytes(b'unowned')
    original_expiry = uploads.status(project, {'upload_id': pending})['expires']
    clock[0] += 100
    uploads.chunk(project, chunk_args(pending, b'abc'))
    assert uploads.status(project, {'upload_id': pending})['expires'] == original_expiry
    assert uploads.cleanup()['expired_uploads'] == 0
    clock[0] = original_expiry + 1
    expect('UPLOAD_EXPIRED', lambda: uploads.status(project, {'upload_id': pending}))
    assert uploads.cleanup()['expired_uploads'] == 1
    assert not spool_path(engine, pending).exists()
    assert row_for(engine, pending) is None
    assert uploads.status(project, {'upload_id': success})['created']
    assert unrelated.read_bytes() == b'unowned'
    assert (root / 'success').read_bytes() == b'abcdef'
    clock[0] = 1000 + incoming.RECEIPT_SECONDS + 1
    assert uploads.cleanup()['expired_receipts'] == 1
    assert (root / 'success').read_bytes() == b'abcdef'


@pytest.mark.parametrize('attack', ['symlink', 'hardlink', 'replace', 'missing', 'truncate', 'grow'])
def test_spool_identity_and_shape_fail_closed(workspace, attack):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    path = spool_path(engine, identifier)
    other = root / 'untouched'
    other.write_bytes(b'abcdef')
    if attack == 'symlink':
        path.unlink()
        path.symlink_to(other)
    elif attack == 'hardlink':
        path.unlink()
        os.link(other, path)
    elif attack == 'replace':
        path.rename(path.with_suffix('.old'))
        path.write_bytes(b'abcdef')
        path.chmod(0o600)
    elif attack == 'missing':
        path.unlink()
    elif attack == 'truncate':
        path.write_bytes(b'a')
    else:
        with path.open('ab') as stream:
            stream.write(b'x')
    expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert other.read_bytes() == b'abcdef'
    assert not (root / 'binary.dat').exists()


def test_spool_directory_link_and_orphan_are_never_adopted(workspace):
    uploads, engine, project, root = workspace
    directory = engine.journal.directory / 'incoming-upload'
    directory.symlink_to(root, target_is_directory=True)
    with pytest.raises((OSError, DevError)):
        start(workspace)
    assert list(root.iterdir()) == []
    directory.unlink()
    directory.mkdir(mode=0o700)
    identifier = uuid.uuid4().hex
    orphan = directory / (identifier + '.part')
    orphan.write_bytes(b'abcdef')
    orphan.chmod(0o600)
    expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.begin(identifier, project, begin_args()))
    assert orphan.read_bytes() == b'abcdef'


def test_expired_link_attack_is_retained_for_recovery(workspace):
    uploads, engine, _, root = workspace
    identifier = start(workspace)
    original = root / 'preserve'
    original.write_bytes(b'valuable')
    spool = spool_path(engine, identifier)
    spool.unlink()
    os.link(original, spool)
    with engine.journal.db:
        engine.journal.db.execute('UPDATE incoming_uploads SET expires=0,spool_expires=0 WHERE id=?', (identifier,))
    result = uploads.cleanup()
    assert result['recovery_required'] == 1
    assert row_for(engine, identifier) is not None
    assert original.read_bytes() == b'valuable'
    assert spool.exists()


def test_destination_parent_symlink_is_rechecked(workspace):
    uploads, _, project, root = workspace
    identifier = filled(workspace, path='nested/file')
    outside = root.parent / 'outside'
    outside.mkdir()
    (root / 'nested').symlink_to(outside, target_is_directory=True)
    expect('SYMLINK_BLOCKED', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('after_publish', [False, True])
def test_interrupted_publication_keeps_intent_and_never_invents_receipt(workspace, monkeypatch, after_publish):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    original = incoming.AnchoredDestination.publish
    def interrupt(self):
        record = row_for(engine, identifier)
        assert record['state'] == 'publishing'
        intent = json.loads(record['staging'])
        info = os.fstat(self.fd)
        assert (intent['dev'], intent['ino']) == (info.st_dev, info.st_ino)
        assert intent['sha256'] == sha(b'abcdef') and intent['bytes'] == 6
        assert not engine.journal.db.in_transaction or engine.journal.db.execute(
            'SELECT state FROM incoming_uploads WHERE id=?', (identifier,)).fetchone()[0] == 'publishing'
        if after_publish:
            original(self)
        raise OSError('synthetic interrupted publication')
    monkeypatch.setattr(incoming.AnchoredDestination, 'publish', interrupt)
    expect('UPLOAD_STORAGE', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert (root / 'binary.dat').exists() is after_publish
    monkeypatch.setattr(incoming.AnchoredDestination, 'publish', original)
    resumed = incoming.IncomingUploads(engine)
    for action in [
        lambda: resumed.begin(identifier, project, begin_args()),
        lambda: resumed.status(project, {'upload_id': identifier}),
        lambda: resumed.finish(project, {'upload_id': identifier}),
    ]:
        expect('UPLOAD_RECOVERY_REQUIRED', action)
    assert row_for(engine, identifier)['state'] == 'publishing'
    if not after_publish:
        (root / 'binary.dat').write_bytes(b'abcdef')
        expect('UPLOAD_RECOVERY_REQUIRED', lambda: resumed.finish(project, {'upload_id': identifier}))
    assert (root / 'binary.dat').read_bytes() == b'abcdef'


def test_historical_receipt_does_not_republish_after_target_changes(workspace):
    uploads, _, project, root = workspace
    identifier = filled(workspace)
    receipt = uploads.finish(project, {'upload_id': identifier})
    (root / 'binary.dat').write_bytes(b'later-authorized-edit')
    assert uploads.finish(project, {'upload_id': identifier}) == receipt
    assert (root / 'binary.dat').read_bytes() == b'later-authorized-edit'
    expect('UPLOAD_COMPLETE', lambda: uploads.chunk(project, chunk_args(identifier)))


def test_real_process_exit_during_copy_retains_one_staging_identity_and_quota(workspace):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    script = """
import json, os, sys
from pathlib import Path
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent import incoming_uploads as module
config, project = json.loads(sys.argv[1]), json.loads(sys.argv[2])
journal = Journal(Path(sys.argv[3]))
engine = FileEngine(config, journal, Path(sys.argv[4]))
uploads = module.IncomingUploads(engine)
original = module.AnchoredDestination.write
def crash(self, data):
    original(self, data)
    os.fsync(self.fd)
    os._exit(73)
module.AnchoredDestination.write = crash
uploads.finish(project, {'upload_id': sys.argv[5]})
"""
    process = subprocess.run([
        sys.executable, '-c', script, json.dumps(engine.config), json.dumps(project),
        str(engine.journal.directory), str(engine.config_path), identifier,
    ], capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, process.stderr
    record = row_for(engine, identifier)
    assert record['state'] == 'staging'
    intent = json.loads(record['staging'])
    stage = root / intent['name']
    assert stage.read_bytes() == b'abcdef'
    assert (stage.stat().st_dev, stage.stat().st_ino) == (intent['dev'], intent['ino'])
    assert not (root / 'binary.dat').exists()
    for _ in range(3):
        expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert list(root.glob('.rd-import-*')) == [stage]
    with engine.journal.db:
        engine.journal.db.execute('UPDATE incoming_uploads SET expires=0,spool_expires=0 WHERE id=?', (identifier,))
    uploads.cleanup()
    assert not spool_path(engine, identifier).exists()
    assert row_for(engine, identifier)['state'] == 'staging'
    assert stage.read_bytes() == b'abcdef'
    assert engine.journal.db.execute(
        "SELECT sum(2*size) FROM incoming_uploads WHERE spool_name IS NOT NULL OR state IN ('staging','publishing')"
    ).fetchone()[0] == 12
    expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.status(project, {'upload_id': identifier}))


def test_unicode_normalization_collision_is_never_overwritten(workspace):
    uploads, _, project, root = workspace
    first = filled(workspace, path='é.bin')
    uploads.finish(project, {'upload_id': first})
    alternate = 'e\u0301.bin'
    if (root / alternate).exists():
        expect('ARTIFACT_DESTINATION_EXISTS', lambda: filled(workspace, path=alternate))
    else:
        identifier = filled(workspace, b'other', alternate)
        uploads.finish(project, {'upload_id': identifier})
        assert (root / alternate).read_bytes() == b'other'
    assert (root / 'é.bin').read_bytes() == b'abcdef'


def test_root_identity_changed_during_copy_is_rejected_before_publish(workspace, monkeypatch):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    alternate = root.parent / 'alternate'
    alternate.mkdir()
    engine.config['allowed_roots'].append({'path': str(alternate), 'writable': True})
    original_root = project['root']
    original = incoming.AnchoredDestination.write
    def retarget(self, data):
        original(self, data)
        project['_original_root'] = original_root
        project['root'] = str(alternate)
    monkeypatch.setattr(incoming.AnchoredDestination, 'write', retarget)
    expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert not (root / 'binary.dat').exists()
    assert not (alternate / 'binary.dat').exists()


def test_source_change_between_hash_and_copy_does_not_publish(workspace, monkeypatch):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    original = incoming.AnchoredDestination.__enter__
    def corrupt(self):
        result = original(self)
        spool_path(engine, identifier).write_bytes(b'abcdeg')
        return result
    monkeypatch.setattr(incoming.AnchoredDestination, '__enter__', corrupt)
    expect('UPLOAD_INTEGRITY', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert not (root / 'binary.dat').exists()


def test_hardlinked_destination_is_not_replaced(workspace):
    uploads, _, project, root = workspace
    identifier = filled(workspace)
    source = root / 'existing'
    source.write_bytes(b'original')
    os.link(source, root / 'binary.dat')
    expect('ARTIFACT_DESTINATION_EXISTS', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert source.read_bytes() == b'original'
    assert (root / 'binary.dat').read_bytes() == b'original'


def test_spool_creation_without_identity_ack_is_not_adopted(workspace, monkeypatch):
    uploads, engine, project, _ = workspace
    identifier = uuid.uuid4().hex
    original = incoming._Spool.sync
    def crash(self):
        original(self)
        raise OSError('synthetic crash before inode acknowledgement')
    monkeypatch.setattr(incoming._Spool, 'sync', crash)
    expect('UPLOAD_STORAGE', lambda: uploads.begin(identifier, project, begin_args()))
    assert spool_path(engine, identifier).exists()
    assert row_for(engine, identifier)['spool_dev'] is None
    monkeypatch.setattr(incoming._Spool, 'sync', original)
    expect('UPLOAD_RECOVERY_REQUIRED', lambda: uploads.begin(identifier, project, begin_args()))


def test_permission_revoked_during_staging_fsync_is_rechecked_after_checkpoint(workspace, monkeypatch):
    uploads, engine, project, root = workspace
    identifier = filled(workspace)
    original = incoming.os.fsync
    def revoke(fd):
        original(fd)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_size == 6:
            project['_coding_scopes'] = []
    monkeypatch.setattr(incoming.os, 'fsync', revoke)
    expect('UPLOAD_FORBIDDEN', lambda: uploads.finish(project, {'upload_id': identifier}))
    assert not (root / 'binary.dat').exists()
    assert row_for(engine, identifier)['state'] == 'publishing'


def test_storage_errors_do_not_disclose_private_spool_paths(workspace, monkeypatch):
    uploads, engine, project, _ = workspace
    identifier = uuid.uuid4().hex
    private_path = str(spool_path(engine, identifier))
    def failure(self, row, *, create=False):
        raise OSError(28, 'No space left on device', private_path)
    monkeypatch.setattr(incoming._Spool, 'open', failure)
    error = expect('UPLOAD_STORAGE', lambda: uploads.begin(identifier, project, begin_args()))
    assert private_path not in str(error)
    assert str(engine.journal.directory) not in json.dumps(error.details)
    assert error.details['recovery'] == 'read_upload_status'
