"""Public checkout and fail-closed archive contracts; no external services."""
from pathlib import Path
import hashlib
import json
import stat
import zipfile

import pytest

from scripts import build_source_bundle as bundle
from scripts import check_release as guard


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    for name in bundle.REQUIRED_FILES - {'LOCAL_RELEASE.json'}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture source\n', encoding='utf-8')
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    return tmp_path


def test_public_checkout_can_build_without_private_release_record(source_tree):
    result = bundle.build(source_tree / 'dist/source.zip')
    assert result['verified'] and result['skipped_selected_files'] == []


def test_selected_symlink_directory_is_not_silently_omitted(source_tree):
    outside = source_tree / 'outside'
    outside.mkdir()
    (outside / 'module.py').write_text('value = 1')
    (source_tree / 'agent/linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symlink'):
        bundle.build(source_tree / 'dist/source.zip', public=True)
    assert not (source_tree / 'dist/source.zip').exists()


def test_selected_oversize_file_is_not_certified(source_tree):
    with (source_tree / 'agent/oversize.py').open('wb') as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match='8 MiB'):
        bundle.build(source_tree / 'dist/source.zip', public=True)


def test_selected_symlink_file_is_not_certified(source_tree):
    (source_tree / 'agent/linked.py').symlink_to(source_tree / 'hub/app.py')
    with pytest.raises(RuntimeError, match='regular'):
        bundle.build(source_tree / 'dist/source.zip', public=True)


def test_public_profile_ignores_local_evidence_symlinks(source_tree):
    evidence = source_tree / 'docs/evidence'
    evidence.mkdir()
    (evidence / 'local-cache').symlink_to(source_tree / 'hub', target_is_directory=True)
    files = guard.public_files(source_tree)
    result = bundle.build(source_tree / 'dist/source.zip', public=True)
    assert result['verified']
    assert all(not name.startswith('docs/evidence/') for name in files)


def test_build_is_reproducible_and_matches_current_source(source_tree):
    first = bundle.build(source_tree / 'dist/one.zip', public=True)
    second = bundle.build(source_tree / 'dist/two.zip', public=True)
    assert first['sha256'] == second['sha256']
    assert guard.check_archive(source_tree / 'dist/one.zip', guard.public_files(source_tree))['verified_against_current_source']


def test_archive_guard_rejects_extra_entry(source_tree):
    destination = source_tree / 'dist/source.zip'
    bundle.build(destination, public=True)
    with zipfile.ZipFile(destination, 'a') as archive:
        archive.writestr('extra-private.txt', 'not an approved source file')
    with pytest.raises(ValueError, match='unexpected'):
        guard.check_archive(destination, guard.public_files(source_tree))


def test_archive_guard_rejects_stale_source(source_tree):
    destination = source_tree / 'dist/source.zip'
    bundle.build(destination, public=True)
    (source_tree / 'hub/app.py').write_text('changed source')
    with pytest.raises(ValueError, match='differs'):
        guard.check_archive(destination, guard.public_files(source_tree))


def test_private_inputs_never_enter_public_profile():
    for name in ['LOCAL_RELEASE.json', 'docs/evidence/native-cli-20260914/browser-final.xml',
                 '.env.local', 'scripts/Token-Private.txt', 'agent/PAIRING-TICKET.JSON',
                 'agent/private/state.json', '../README.md']:
        assert not bundle.include(Path(name), public=True)


def test_credentials_are_detected_without_printing_values():
    fake = 'ghp_' + 'a' * 36
    assert guard.CREDENTIAL_PATTERNS['github-token'].search(fake)
    assert not guard.CREDENTIAL_PATTERNS['github-token'].search('ghp_example')


def test_worker_database_initialization_failure_releases_ownership(monkeypatch):
    from agent import chat_worker
    from types import SimpleNamespace
    closed = []
    monkeypatch.setattr(chat_worker, 'WorkerLock', lambda *args: SimpleNamespace(close=lambda: closed.append('lock')))
    def fail(*args):
        raise OSError('database cannot be opened')
    monkeypatch.setattr(chat_worker, 'database', fail)
    with pytest.raises(OSError):
        chat_worker.run('/fixture', 'c' * 32)
    assert closed == ['lock']


def test_import_cleanup_does_not_replace_primary_exception(monkeypatch):
    from agent import incoming_artifacts as module
    destination = module.AnchoredDestination(None, Path('/fixture'), 'file.txt')
    destination.fd, destination.fds = 20, [21]
    closed = []
    monkeypatch.setattr(module.os, 'close', closed.append)
    def fail(*args, **kwargs):
        raise PermissionError('cleanup denied')
    monkeypatch.setattr(module.os, 'unlink', fail)
    primary = ValueError('download checksum mismatch')
    destination.__exit__(type(primary), primary, None)
    assert closed == [20, 21] and primary.__notes__


def test_regression_snapshot_covers_root_build_inputs(tmp_path, monkeypatch):
    from scripts import check_full_regression as runner
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    path = tmp_path / 'requirements.txt'
    path.write_text('dependency==1\n')
    before = runner.snapshot()
    path.write_text('dependency==2\n')
    assert before['requirements.txt'] != runner.snapshot()['requirements.txt']


def test_panel_profile_requires_public_source(source_tree):
    with pytest.raises(ValueError, match='public profile'):
        bundle.build(source_tree / 'dist/panel.zip', panel_update=True)


def test_real_release_upgrades_with_unmodified_1_13_updater(tmp_path):
    """Synthetic minimal archives missed the 1.14.0 extra-root regression."""
    import runpy
    import uuid
    from scripts import panel_update_source
    paths = [bundle.ROOT / 'dist' / (uuid.uuid4().hex + suffix) for suffix in ['-full.zip', '-panel.zip']]
    try:
        full = bundle.build(paths[0], public=True)
        panel = bundle.build(paths[1], public=True, panel_update=True)
        expected = guard.public_files(bundle.ROOT)
        assert guard.check_panel_update(paths[1], expected)['panel_updaters_verified'] == ['1.13.0', 'current']
        with zipfile.ZipFile(paths[0]) as complete, zipfile.ZipFile(paths[1]) as compatible:
            complete_names = set(complete.namelist()) - {'MANIFEST.sha256'}
            compatible_names = set(compatible.namelist()) - {'MANIFEST.sha256'}
            assert complete_names - compatible_names == bundle.PANEL_UPDATE_EXCLUDES
            assert compatible_names < complete_names
            for name in compatible_names:
                assert complete.read(name) == compatible.read(name), name
                assert complete.getinfo(name).external_attr == compatible.getinfo(name).external_attr, name
        legacy = runpy.run_path(str(bundle.ROOT / 'tests/fixtures/panel_update_source_v1_13_0.py'))
        release = {'version': bundle.source_version(), 'bytes': full['bytes'], 'sha256': full['sha256']}
        with pytest.raises(legacy['UpdateError'], match='未允许的顶层文件'):
            legacy['unpack_bundle'](paths[0], tmp_path / 'rejected', release)
        assert not (tmp_path / 'rejected').exists()
        # Unknown future root inputs must still fail instead of being silently omitted.
        with zipfile.ZipFile(paths[1], 'a') as archive:
            archive.writestr('future-root.toml', 'unknown = true\n')
        release.update(bytes=paths[1].stat().st_size, sha256=hashlib.sha256(paths[1].read_bytes()).hexdigest())
        with pytest.raises(panel_update_source.UpdateError, match='未允许的顶层文件'):
            panel_update_source.unpack_bundle(paths[1], tmp_path / 'unknown', release)
        assert panel['panel_update_profile'] and not full['panel_update_profile']
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            path.with_suffix('.manifest.json').unlink(missing_ok=True)
