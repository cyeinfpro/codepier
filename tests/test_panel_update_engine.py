import fcntl
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from scripts import panel_update_engine as engine


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path.resolve()
    home = root / 'home'; home.mkdir(mode=0o700)
    (home / 'jobs').mkdir()
    (home / 'config.json').write_text(json.dumps({'repository': 'cyeinfpro/codepier'}))
    source = root / 'release' / 'source'; source.mkdir(parents=True)
    (source / 'scripts').mkdir(); (source / 'shared').mkdir()
    metadata = {'name': 'CodePier', 'version': '1.30.6',
                'host_updater': {'protocol': 1, 'min_launcher': 1}}
    (source / 'RELEASE.json').write_text(json.dumps(metadata))
    (source / 'shared/util.py').write_text("VERSION = '1.30.6'\n")
    for name in engine.ENGINE_FILES:
        (source / 'scripts' / name).write_text('# synthetic ' + name + '\n')
    manifest(source)
    archive = source.parent / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.write(source / 'MANIFEST.sha256', 'MANIFEST.sha256')
    release = {'repository': 'cyeinfpro/codepier', 'version': '1.30.6', 'release_id': 10,
               'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'bytes': archive.stat().st_size}
    manifest(source)
    return home, source, release


def manifest(source):
    paths = [p for p in source.rglob('*') if p.is_file() and p.name != 'MANIFEST.sha256']
    (source / 'MANIFEST.sha256').write_text(''.join(
        engine.digest_file(p) + '  ' + str(p.relative_to(source)) + '\n' for p in paths))


def stage(fixture):
    home, source, release = fixture
    return engine.stage_engine(home, source, release, expected_uid=os.getuid())


def test_stage_verify_and_reuse(fixture):
    identity = stage(fixture); home, _, _ = fixture
    assert engine.verify_engine(home, identity, expected_uid=os.getuid()).name == identity['id']
    assert stage(fixture) == identity
    assert len(list((home / 'engines').iterdir())) == 1


@pytest.mark.parametrize('change', ['archive', 'source', 'version', 'protocol', 'repo', 'missing'])
def test_stage_rejects_unverified_or_unsupported(fixture, change):
    home, source, release = fixture
    if change == 'archive':
        (source.parent / 'source.zip').write_bytes(b'changed')
    elif change == 'source':
        (source / 'scripts/panel_updater.py').write_text('# changed')
    elif change == 'version':
        release['version'] = '1.30.7'
    elif change == 'protocol':
        data = json.loads((source / 'RELEASE.json').read_text()); data['host_updater']['protocol'] = 2
        (source / 'RELEASE.json').write_text(json.dumps(data)); manifest(source)
    elif change == 'repo':
        release['repository'] = 'other/repo'
    else:
        (source / 'scripts/panel_updater.py').unlink()
    with pytest.raises((engine.EngineError, FileNotFoundError)):
        stage(fixture)


def test_existing_engine_tamper_is_not_overwritten(fixture):
    identity = stage(fixture); home, _, _ = fixture
    file = home / 'engines' / identity['id'] / 'panel_updater.py'
    file.write_text('changed')
    with pytest.raises(engine.EngineError):
        stage(fixture)
    assert file.read_text() == 'changed'


def test_symlink_rejected(fixture):
    home, source, _ = fixture
    file = source / 'scripts/panel_updater.py'; file.unlink()
    file.symlink_to(source / 'scripts/panel_update_runtime.py'); manifest(source)
    with pytest.raises(engine.EngineError):
        stage(fixture)


def test_unknown_file_rejected(fixture):
    identity = stage(fixture); home, _, _ = fixture
    (home / 'engines' / identity['id'] / 'extra.py').write_text('bad')
    with pytest.raises(engine.EngineError):
        engine.verify_engine(home, identity, expected_uid=os.getuid())


def job(home, identity, state='running', identifier='a' * 32):
    path = home / 'jobs' / identifier; path.mkdir(exist_ok=True)
    data = {'id': identifier, 'engine': identity, 'state': state, 'commit_decided': True,
            'target_version': identity['version']}
    (path / 'status.json').write_text(json.dumps(data))
    return path


def test_pending_and_recovery_pin_original_engine(fixture):
    identity = stage(fixture); home, _, _ = fixture
    (home / 'engine-current.json').write_text('{}')
    for state in ('running', 'recovery_required', 'queued'):
        job(home, identity, state)
        assert engine.select_engine(home, expected_uid=os.getuid()) == identity


def test_unknown_or_conflicting_owner_fails_closed(fixture):
    identity = stage(fixture); home, _, _ = fixture
    job(home, identity)
    second = job(home, identity, identifier='b' * 32)
    data = json.loads((second / 'status.json').read_text()); data['engine'] = None
    (second / 'status.json').write_text(json.dumps(data))
    with pytest.raises(engine.EngineError):
        engine.select_engine(home, expected_uid=os.getuid())


def test_promote_requires_success_and_exact_release(fixture):
    identity = stage(fixture); home, _, release = fixture
    engine.atomic_json(home / 'engine-current.json', identity)
    path = job(home, identity, 'succeeded')
    engine.atomic_json(path / 'release.json', release)
    assert engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid()) == identity
    release['release_id'] += 1; engine.atomic_json(path / 'release.json', release)
    with pytest.raises(engine.EngineError):
        engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid())


@pytest.mark.parametrize('state', ['queued', 'running', 'failed', 'rolled_back', 'recovery_required'])
def test_non_success_never_promotes(fixture, state):
    identity = stage(fixture); home, _, release = fixture
    engine.atomic_json(home / 'engine-current.json', identity)
    path = job(home, identity, state); engine.atomic_json(path / 'release.json', release)
    with pytest.raises(engine.EngineError):
        engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid())


def test_ready_is_separate_from_selector(fixture):
    identity = stage(fixture); home, _, _ = fixture
    engine.atomic_json(home / 'engine-current.json', identity)
    assert not (home / 'engine-ready.json').exists()
    engine.mark_ready(home, identity, expected_uid=os.getuid())
    assert engine.read_json(home / 'engine-ready.json')['id'] == identity['id']


def test_bootstrap_explicit_and_not_promoted(fixture):
    home, source, _ = fixture
    (source / 'MANIFEST.sha256').unlink(); (source.parent / 'source.zip').unlink()
    identity = engine.stage_bootstrap(home, source, expected_uid=os.getuid())
    assert identity['bootstrap'] and identity['release_id'] == 0
    assert not (home / 'engine-current.json').exists()


def test_supervised_fd_matches_inode_and_private_mode(tmp_path):
    home = tmp_path; fd = os.open(home / 'service.lock', os.O_CREAT | os.O_RDWR, 0o600)
    other = os.open(home / 'other', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(engine.EngineError):
            engine.validate_supervised_fd(home, fd, expected_uid=os.getuid())
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert engine.validate_supervised_fd(home, fd, expected_uid=os.getuid()) == fd
        with pytest.raises(engine.EngineError):
            engine.validate_supervised_fd(home, other, expected_uid=os.getuid())
        os.chmod(home / 'service.lock', 0o644)
        with pytest.raises(engine.EngineError):
            engine.validate_supervised_fd(home, fd, expected_uid=os.getuid())
    finally:
        os.close(fd); os.close(other)

def test_rewritten_source_manifest_cannot_override_archive(fixture):
    home, source, release = fixture
    (source / 'scripts/panel_updater.py').write_text('new code')
    manifest(source)
    with pytest.raises(engine.EngineError):
        stage(fixture)


@pytest.mark.parametrize('mutation', ['downgrade', 'same_version_different_identity', 'other_pending'])
def test_promotion_is_monotonic_and_idle(fixture, mutation):
    identity = stage(fixture); home, _, release = fixture
    path = job(home, identity, 'succeeded'); engine.atomic_json(path / 'release.json', release)
    previous = dict(identity)
    if mutation == 'downgrade':
        previous['version'] = '1.30.7'
    elif mutation == 'same_version_different_identity':
        previous['sha256'] = 'b' * 64
    else:
        job(home, identity, 'recovery_required', identifier='b' * 32)
    previous['id'] = engine.canonical_id(previous)
    engine.atomic_json(home / 'engine-current.json', previous)
    with pytest.raises(engine.EngineError):
        engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid())


def test_source_mode_rejected(fixture):
    _, source, _ = fixture
    (source / 'scripts/panel_updater.py').chmod(0o666)
    with pytest.raises(engine.EngineError):
        stage(fixture)


def test_unsupported_min_launcher(fixture):
    _, source, _ = fixture
    metadata = engine.read_json(source / 'RELEASE.json')
    metadata['host_updater']['min_launcher'] = 2
    engine.atomic_json(source / 'RELEASE.json', metadata)
    with pytest.raises(engine.EngineError):
        stage(fixture)


def test_bootstrap_same_version_can_gain_verified_release_identity(fixture):
    home, source, release = fixture
    bootstrap = engine.stage_bootstrap(home, source, expected_uid=os.getuid())
    identity = stage(fixture)
    assert bootstrap['id'] != identity['id'] and bootstrap['files'] == identity['files']
    engine.atomic_json(home / 'engine-current.json', bootstrap)
    path = job(home, bootstrap, 'succeeded')
    engine.atomic_json(path / 'release.json', release)
    assert engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid()) == identity
    assert engine.select_engine(home, expected_uid=os.getuid()) == identity


def test_bootstrap_same_version_different_code_stays_forbidden(fixture):
    home, source, release = fixture
    identity = stage(fixture)
    bootstrap = engine.stage_bootstrap(home, source, expected_uid=os.getuid())
    bootstrap['files'] = dict(bootstrap['files'])
    bootstrap['files']['panel_updater.py'] = 'f' * 64
    bootstrap['id'] = engine.canonical_id(bootstrap)
    engine.atomic_json(home / 'engine-current.json', bootstrap)
    path = job(home, identity, 'succeeded'); engine.atomic_json(path / 'release.json', release)
    with pytest.raises(engine.EngineError):
        engine.promote_engine(home, identity, 'a' * 32, expected_uid=os.getuid())


def test_supervised_fd_cannot_borrow_other_description_lock(tmp_path):
    path = tmp_path / 'service.lock'
    owner = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    unrelated = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(engine.EngineError):
            engine.validate_supervised_fd(tmp_path, unrelated, expected_uid=os.getuid())
        assert engine.validate_supervised_fd(tmp_path, owner, expected_uid=os.getuid()) == owner
    finally:
        os.close(unrelated); os.close(owner)
