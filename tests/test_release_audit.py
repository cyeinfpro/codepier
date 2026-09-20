"""Regression cases for the September 2026 source and release audit.

All tests use temporary files or deterministic fakes; no native model is run.
"""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import uuid
import zipfile

import pytest

from shared import build_info
from shared.util import VERSION
from scripts import build_source_bundle as bundle


@pytest.fixture
def identity_tree(tmp_path):
    for name in ('shared', 'hub', 'web'):
        (tmp_path / name).mkdir()
    (tmp_path / 'shared/util.py').write_text(f'VERSION = "{VERSION}"\n')
    (tmp_path / 'hub/app.py').write_text('value = 1\n')
    return tmp_path


def test_identity_detects_nested_runtime_asset_changes(identity_tree):
    folder = identity_tree / 'web/mcp-apps'
    folder.mkdir()
    asset = folder / 'app.js'
    asset.write_text('const value = 1;\n')
    before = build_info.source_identity('hub', identity_tree)
    asset.write_text('const value = 2;\n')
    after = build_info.source_identity('hub', identity_tree)
    assert not before['errors'] and not after['errors']
    assert before['source_sha256'] != after['source_sha256']


def test_identity_detects_missing_runtime_directory(identity_tree):
    (identity_tree / 'hub/app.py').unlink()
    (identity_tree / 'hub').rmdir()
    assert 'hub' in build_info.source_identity('hub', identity_tree)['errors']


def test_identity_does_not_certify_unreadable_current_source(monkeypatch):
    value = {'version': VERSION, 'source_sha256': 'a' * 64, 'errors': ['web/app.js']}
    monkeypatch.setattr(build_info, 'source_identity', lambda component: dict(value))
    assert build_info.BuildIdentity('hub').describe()['restart_required']


def test_identity_ignores_installed_dependencies(identity_tree):
    before = build_info.source_identity('hub', identity_tree)
    folder = identity_tree / 'web/mcp-apps/node_modules/package'
    folder.mkdir(parents=True)
    (folder / 'index.js').write_text('not a shipped source file')
    assert build_info.source_identity('hub', identity_tree) == before


@pytest.mark.parametrize('path', [
    'agent/token-private.txt', 'hub/pairing-local.json', 'web/.env.production',
    'scripts/credentials.json', 'agent/config.json', 'web/private/secret.json',
])
def test_bundle_excludes_private_filenames(path):
    assert not bundle.include(Path(path))


def test_bundle_preserves_example_environment_file():
    assert bundle.include(Path('.env.example'))


def test_bundle_cannot_overwrite_runtime_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    path = tmp_path / 'web/browser-extension.zip'
    path.parent.mkdir()
    path.write_bytes(b'original runtime asset')
    with pytest.raises(ValueError):
        bundle.build(path)
    assert path.read_bytes() == b'original runtime asset'


def test_bundle_manifest_uses_deterministic_zip_metadata():
    destination = bundle.ROOT / 'dist' / ('audit-test-' + uuid.uuid4().hex + '.zip')
    try:
        bundle.build(destination)
        with zipfile.ZipFile(destination) as archive:
            manifest = archive.getinfo('MANIFEST.sha256')
            source = archive.getinfo('hub/app.py')
            assert manifest.date_time == source.date_time
            assert manifest.create_system == 3
            assert manifest.external_attr >> 16 & 0o777 == 0o644
    finally:
        destination.unlink(missing_ok=True)
        destination.with_suffix('.manifest.json').unlink(missing_ok=True)


def test_import_cleanup_closes_pinned_directories_even_when_unlink_fails(monkeypatch):
    from agent import incoming_artifacts as module
    closed = []
    target = module.AnchoredDestination(None, Path('/fixture'), 'file.txt')
    target.fd, target.fds = 13, [11, 12]
    monkeypatch.setattr(module.os, 'close', closed.append)
    def fail_unlink(*args, **kwargs):
        raise PermissionError('temporary cleanup denied')
    monkeypatch.setattr(module.os, 'unlink', fail_unlink)
    with pytest.raises(PermissionError):
        target.__exit__(None, None, None)
    assert closed == [13, 12, 11]
    assert target.fd is None and target.fds == []


def test_chat_admission_database_failure_is_not_silently_successful(monkeypatch):
    from agent import chat_worker
    closed = []
    def fail_query(*args):
        raise sqlite3.OperationalError('fixture database failure')
    db = SimpleNamespace(execute=fail_query, close=lambda: closed.append('database'))
    monkeypatch.setattr(chat_worker, 'database', lambda directory: db)
    monkeypatch.setattr(chat_worker, 'WorkerLock', lambda *args: SimpleNamespace(close=lambda: closed.append('ownership')))
    with pytest.raises(sqlite3.OperationalError):
        chat_worker.run('/fixture', 'a' * 32)
    assert closed == ['database', 'ownership']


def test_chat_finalization_releases_ownership_after_database_failure(monkeypatch):
    from agent import chat_worker
    closed = []
    row = {'status': 'starting', 'mode': 'invalid-mode'}
    def query(sql, *args):
        if sql.startswith('SELECT'):
            return SimpleNamespace(fetchone=lambda: row)
        raise sqlite3.OperationalError('fixture finalization failure')
    db = SimpleNamespace(execute=query, close=lambda: closed.append('database'))
    monkeypatch.setattr(chat_worker, 'database', lambda directory: db)
    monkeypatch.setattr(chat_worker, 'WorkerLock', lambda *args: SimpleNamespace(close=lambda: closed.append('ownership')))
    with pytest.raises(sqlite3.OperationalError):
        chat_worker.run('/fixture', 'b' * 32)
    assert closed == ['database', 'ownership']
