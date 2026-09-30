"""Crash-safe upload reservations; temporary SQLite/files only, no native processes."""
from contextlib import contextmanager
import base64
import hashlib
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from agent.native_cli import NativeCLI
from shared.native_cli import ATTACHMENT_CAP
from shared.util import DevError


@pytest.fixture
def upload(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    agent = SimpleNamespace(state_dir=tmp_path / 'state')
    project = {'id': 'project', 'device_id': 'device', 'root': str(root)}
    raw = b'isolated attachment fixture'
    args = {'file': uuid.uuid4().hex, 'name': 'fixture.txt', 'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}

    def restart():
        obj = NativeCLI(agent)
        obj.authorize = lambda current_project: root
        return obj

    return restart, project, args, raw


def snapshot(obj):
    with obj.connect_db() as db:
        return [dict(row) for row in db.execute('SELECT * FROM attachments')]


def complete(obj, project, args, raw):
    result = obj.action('upload_chunk', project, {'file': args['file'], 'offset': 0,
                        'data': base64.b64encode(raw).decode(), 'sha256': args['sha256']})
    assert result['ready'] and result['received'] == len(raw)
    assert Path(result['path']).read_bytes() == raw


@pytest.mark.parametrize('stage', ['insert', 'reservation_commit', 'after_reservation',
                                 'after_create', 'final_commit'])
def test_fault_windows_resume_same_id_without_orphan_or_double_quota(upload, monkeypatch, stage):
    restart, project, args, raw = upload
    obj = restart()
    original_connect = obj.connect_db
    original_materialize = obj.materialize_upload

    class FaultDB:
        def execute(self, sql, parameters=()):
            if stage == 'insert' and sql.startswith('INSERT INTO attachments'):
                raise sqlite3.OperationalError('injected insert failure')
            return self.db.execute(sql, parameters)

        def commit(self):
            if stage == 'reservation_commit':
                raise sqlite3.OperationalError('injected commit failure')
            return self.db.commit()

    @contextmanager
    def faulty_connection():
        with original_connect() as db:
            proxy = FaultDB()
            proxy.db = db
            yield proxy
            if stage == 'final_commit':
                raise sqlite3.OperationalError('injected final transaction failure')

    def faulty_materialize(row):
        if stage == 'after_reservation':
            raise sqlite3.OperationalError('injected crash before file creation')
        original_materialize(row)
        if stage == 'after_create':
            raise sqlite3.OperationalError('injected crash after file creation')

    monkeypatch.setattr(obj, 'connect_db', faulty_connection)
    monkeypatch.setattr(obj, 'materialize_upload', faulty_materialize)
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            obj.action('upload_begin', project, args)
        rows = snapshot(restart())
        assert len(rows) == (0 if stage in {'insert', 'reservation_commit'} else 1)
        assert sum(row['size'] for row in rows) == len(rows) * len(raw)
        files = list((obj.directory / 'uploads').iterdir())
        assert len(files) == (1 if stage in {'after_create', 'final_commit'} else 0)

    recovered = restart()
    assert recovered.action('upload_begin', project, args) == {
        'file': args['file'], 'received': 0, 'ready': False}
    assert recovered.action('upload_begin', project, args)['received'] == 0
    assert len(snapshot(recovered)) == 1
    complete(recovered, project, args, raw)


@pytest.mark.parametrize('kind', ['empty', 'nonempty', 'other_suffix', 'symlink', 'directory'])
def test_legacy_unregistered_path_is_preserved_with_actionable_error(upload, kind):
    restart, project, args, _ = upload
    obj = restart()
    folder = obj.directory / 'uploads'
    folder.mkdir()
    path = folder / (args['file'] + ('.png' if kind == 'other_suffix' else '.txt'))
    if kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        path.symlink_to(folder / 'missing-private-target')
    else:
        path.write_bytes(b'' if kind == 'empty' else b'preserve legacy content')
    before = path.lstat()
    for _ in range(2):
        with pytest.raises(DevError) as error:
            obj.action('upload_begin', project, args)
        assert error.value.code == 'ATTACHMENT_RECOVERY_REQUIRED'
        assert error.value.details['recovery'] == 'reselect_attachment'
        assert path.lstat().st_ino == before.st_ino
        assert not snapshot(obj)
        assert list(folder.iterdir()) == [path]
    if kind in {'nonempty', 'other_suffix'}:
        assert path.read_bytes() == b'preserve legacy content'


def test_unregistered_session_reference_is_never_reused(upload):
    restart, project, args, _ = upload
    obj = restart()
    with obj.connect_db() as db:
        db.execute('INSERT INTO session_files VALUES (?,?)', ('bound-session', args['file']))
    with pytest.raises(DevError) as error:
        obj.action('upload_begin', project, args)
    assert error.value.code == 'ATTACHMENT_RECOVERY_REQUIRED'
    assert not snapshot(obj)
    with obj.connect_db() as db:
        assert db.execute('SELECT file FROM session_files').fetchone()[0] == args['file']


def test_committed_reservation_is_visible_before_file_creation(upload, monkeypatch):
    restart, project, args, _ = upload
    obj = restart()
    original = obj.materialize_upload

    def inspect(row):
        with sqlite3.connect(obj.directory / 'native.sqlite3') as db:
            rows = db.execute('SELECT id FROM attachments').fetchall()
        assert rows == [(args['file'],)]
        assert not Path(row['path']).exists()
        original(row)

    monkeypatch.setattr(obj, 'materialize_upload', inspect)
    obj.action('upload_begin', project, args)


def test_retry_does_not_adopt_other_project_or_conflicting_metadata(upload):
    restart, project, args, raw = upload
    obj = restart()
    obj.action('upload_begin', project, args)
    with pytest.raises(DevError) as error:
        obj.action('upload_begin', {**project, 'id': 'other-project'}, args)
    assert error.value.code == 'ATTACHMENT_NOT_FOUND'
    for field, value in [('name', 'other.txt'), ('size', len(raw) + 1), ('sha256', '0' * 64)]:
        with pytest.raises(ValueError, match='Upload ID conflict'):
            obj.action('upload_begin', project, {**args, field: value})
    complete(obj, project, args, raw)
    path = Path(snapshot(obj)[0]['path'])
    inode = path.stat().st_ino
    with obj.connect_db() as db:
        db.execute('INSERT INTO session_files VALUES (?,?)', ('bound-session', args['file']))
    assert obj.action('upload_begin', project, args)['ready']
    assert path.stat().st_ino == inode and path.read_bytes() == raw


def test_missing_received_file_is_not_recreated(upload):
    restart, project, args, raw = upload
    obj = restart()
    obj.action('upload_begin', project, args)
    complete(obj, project, args, raw)
    path = Path(snapshot(obj)[0]['path'])
    path.unlink()
    with pytest.raises(DevError) as error:
        obj.action('upload_begin', project, args)
    assert error.value.code == 'ATTACHMENT_RECOVERY_REQUIRED'
    assert not path.exists()
    assert snapshot(obj)[0]['received'] == len(raw)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'oversized'])
def test_reserved_path_replacement_is_not_modified(upload, kind):
    restart, project, args, _ = upload
    obj = restart()
    obj.action('upload_begin', project, args)
    path = Path(snapshot(obj)[0]['path'])
    path.unlink()
    target = obj.directory / 'unrelated'
    target.write_bytes(b'preserve')
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'x' * (args['size'] + 1))
    before = path.lstat()
    with pytest.raises((ValueError, DevError)):
        obj.action('upload_begin', project, args)
    assert path.lstat().st_ino == before.st_ino
    assert target.read_bytes() == b'preserve'


def test_empty_reservation_can_be_deleted_and_quota_is_reserved_once(upload):
    restart, project, args, _ = upload
    obj = restart()
    reservations = []
    for _ in range(10):
        reservation = {**args, 'file': uuid.uuid4().hex, 'size': ATTACHMENT_CAP}
        obj.action('upload_begin', project, reservation)
        reservations.append(reservation)
    for _ in range(3):
        obj.action('upload_begin', project, reservations[-1])
    assert sum(row['size'] for row in snapshot(obj)) == 200 * 1024 * 1024
    with pytest.raises(DevError) as error:
        obj.action('upload_begin', project, args)
    assert error.value.code == 'ATTACHMENT_QUOTA'
    released = reservations[-1]['file']
    path = Path(next(row['path'] for row in snapshot(obj) if row['id'] == released))
    path.unlink()
    assert obj.action('upload_delete', project, {'file': released, 'confirm': released})['deleted']
    obj.action('upload_begin', project, args)
    assert len(snapshot(obj)) == 10


def test_retry_after_unacknowledged_chunk_preserves_bytes(upload):
    restart, project, args, raw = upload
    obj = restart()
    obj.action('upload_begin', project, args)
    path = Path(snapshot(obj)[0]['path'])
    path.write_bytes(raw)  # Simulate fsynced chunk whose DB transaction rolled back.
    inode = path.stat().st_ino
    assert obj.action('upload_begin', project, args)['received'] == 0
    assert path.stat().st_ino == inode and path.read_bytes() == raw
    complete(obj, project, args, raw)


def test_another_instance_can_resume_during_reservation_commit_gap(upload, monkeypatch):
    restart, project, args, raw = upload
    obj, other = restart(), restart()
    original = obj.connect_db
    replies = []

    class InterleaveDB:
        def execute(self, *params):
            return self.db.execute(*params)

        def commit(self):
            self.db.commit()
            replies.append(other.action('upload_begin', project, args))

    @contextmanager
    def interleave():
        with original() as db:
            proxy = InterleaveDB()
            proxy.db = db
            yield proxy

    monkeypatch.setattr(obj, 'connect_db', interleave)
    result = obj.action('upload_begin', project, args)
    assert replies == [result]
    assert len(snapshot(other)) == 1
    assert len(list((obj.directory / 'uploads').iterdir())) == 1
    complete(other, project, args, raw)


def test_missing_empty_upload_directory_recovers(upload):
    restart, project, args, raw = upload
    obj = restart()
    obj.action('upload_begin', project, args)
    path = Path(snapshot(obj)[0]['path'])
    path.unlink()
    path.parent.rmdir()
    recovered = restart()
    assert recovered.action('upload_begin', project, args)['received'] == 0
    complete(recovered, project, args, raw)


def test_delete_in_reservation_commit_gap_never_leaves_a_file(upload, monkeypatch):
    restart, project, args, _ = upload
    obj, other = restart(), restart()
    original = obj.connect_db

    class DeleteDB:
        def execute(self, *params):
            return self.db.execute(*params)

        def commit(self):
            self.db.commit()
            other.action('upload_delete', project, {'file': args['file'], 'confirm': args['file']})

    @contextmanager
    def interleave():
        with original() as db:
            proxy = DeleteDB()
            proxy.db = db
            yield proxy

    monkeypatch.setattr(obj, 'connect_db', interleave)
    with pytest.raises(DevError) as error:
        obj.action('upload_begin', project, args)
    assert error.value.code == 'ATTACHMENT_NOT_FOUND'
    assert not snapshot(other)
    assert not list((obj.directory / 'uploads').iterdir())
