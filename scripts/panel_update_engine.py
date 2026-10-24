"""Fixed host-engine staging under the configured publisher's root-code trust.

Hashes authenticate bytes to the verified release; they do not sandbox Python.
The installed helper/launcher are not automatically replaced by release code.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import zipfile

ENGINE_PROTOCOL = 1
LAUNCHER_VERSION = 1
RESTART_EXIT = 75
ENGINE_FILES = ('panel_updater.py', 'panel_update_runtime.py', 'panel_update_source.py')
TERMINAL = {'succeeded', 'failed', 'rolled_back'}
MAX_FILE = 8 * 1024 * 1024


class EngineError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message, self.status = code, message, 409


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.engine-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_path(path, *, expected_uid=0, directory=False):
    path = Path(path)
    for item in (path, *path.parents):
        info = item.lstat()
        test_ancestor = expected_uid != 0 and item != path and info.st_uid == 0
        sticky_test_parent = test_ancestor and stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)
        if (stat.S_ISLNK(info.st_mode) or (info.st_uid != expected_uid and not test_ancestor) or
                (info.st_mode & 0o022 and not sticky_test_parent)):
            raise EngineError('UNSAFE_ENGINE_PATH', 'Host engine path is not privately owned')
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise EngineError('UNSAFE_ENGINE_PATH', 'Expected engine directory')
    return path


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise EngineError('ENGINE_VERSION', 'Invalid engine version')
    return tuple(map(int, value.split('.')))


def canonical_id(identity):
    return hashlib.sha256(json.dumps({k: v for k, v in identity.items() if k != 'id'},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_identity(identity):
    fields = {'id', 'version', 'protocol', 'min_launcher', 'repository', 'release_id', 'sha256', 'files', 'bootstrap'}
    if not isinstance(identity, dict) or set(identity) != fields or identity.get('id') != canonical_id(identity):
        raise EngineError('ENGINE_IDENTITY', 'Engine identity is invalid')
    version(identity['version'])
    if (type(identity['protocol']) is not int or identity['protocol'] != ENGINE_PROTOCOL or
            type(identity['min_launcher']) is not int or not 1 <= identity['min_launcher'] <= LAUNCHER_VERSION):
        raise EngineError('ENGINE_PROTOCOL', 'Host launcher upgrade required')
    if set(identity['files']) != set(ENGINE_FILES) or any(
            not re.fullmatch(r'[a-f0-9]{64}', value) for value in identity['files'].values()):
        raise EngineError('ENGINE_IDENTITY', 'Engine file identity is invalid')
    if (type(identity['bootstrap']) is not bool or type(identity['release_id']) is not int or
            not re.fullmatch(r'[a-f0-9]{64}', identity['sha256']) or
            not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', identity['repository'])):
        raise EngineError('ENGINE_IDENTITY', 'Engine publisher identity is invalid')
    if identity['release_id'] < (0 if identity['bootstrap'] else 1):
        raise EngineError('ENGINE_IDENTITY', 'Engine release is invalid')


def verify_engine(home, identity, *, expected_uid=0):
    validate_identity(identity)
    home = secure_path(home, expected_uid=expected_uid, directory=True)
    directory = secure_path(home / 'engines' / identity['id'], expected_uid=expected_uid, directory=True)
    if set(p.name for p in directory.iterdir()) != set(ENGINE_FILES) | {'identity.json'}:
        raise EngineError('ENGINE_CONTENT', 'Unexpected engine file')
    if read_json(directory / 'identity.json') != identity:
        raise EngineError('ENGINE_IDENTITY', 'Engine identity changed')
    for name, expected in identity['files'].items():
        path = secure_path(directory / name, expected_uid=expected_uid)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or digest_file(path) != expected:
            raise EngineError('ENGINE_CONTENT', 'Engine source changed')
    return directory


def _stage(home, source, release, bootstrap, expected_uid):
    home = secure_path(home, expected_uid=expected_uid, directory=True)
    source = secure_path(source, expected_uid=expected_uid, directory=True)
    metadata = read_json(source / 'RELEASE.json')
    capability = metadata.get('host_updater', {})
    if (metadata.get('name') != 'CodePier' or capability !=
            {'protocol': ENGINE_PROTOCOL, 'min_launcher': LAUNCHER_VERSION}):
        raise EngineError('ENGINE_PROTOCOL', 'Release requires a compatible host launcher')
    source_version = re.search(r'^VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', (source / 'shared/util.py').read_text(), re.M)
    if not source_version or source_version.group(1) != metadata['version']:
        raise EngineError('ENGINE_VERSION', 'Release source version differs')
    repository = read_json(home / 'config.json')['repository']
    if not bootstrap:
        if release.get('repository') != repository or release.get('version') != metadata['version']:
            raise EngineError('ENGINE_RELEASE', 'Engine release differs from confirmed publisher/version')
        archive = secure_path(source.parent / 'source.zip', expected_uid=expected_uid)
        if archive.stat().st_size != release['bytes'] or digest_file(archive) != release['sha256']:
            raise EngineError('ENGINE_RELEASE', 'Confirmed source archive changed')
        manifest = {}
        with zipfile.ZipFile(archive) as bundle:
            member = bundle.getinfo('MANIFEST.sha256')
            if member.file_size > 4 * 1024 * 1024:
                raise EngineError('ENGINE_RELEASE', 'Engine manifest exceeds limit')
            manifest_text = bundle.read(member).decode('utf-8')
        for line in manifest_text.splitlines():
            sha, name = line.split('  ', 1)
            if name in manifest:
                raise EngineError('ENGINE_RELEASE', 'Duplicate manifest entry')
            manifest[name] = sha
        for name in ['RELEASE.json', 'shared/util.py'] + ['scripts/' + n for n in ENGINE_FILES]:
            if manifest.get(name) != digest_file(source / name):
                raise EngineError('ENGINE_RELEASE', 'Engine source differs from release manifest')
    files = {}
    for name in ENGINE_FILES:
        path = secure_path(source / 'scripts' / name, expected_uid=expected_uid)
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_FILE:
            raise EngineError('ENGINE_CONTENT', 'Invalid engine source file')
        files[name] = digest_file(path)
    identity = {'version': metadata['version'], 'protocol': capability['protocol'],
                'min_launcher': capability['min_launcher'], 'repository': repository,
                'release_id': 0 if bootstrap else release['release_id'],
                'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() if bootstrap else release['sha256'],
                'files': files, 'bootstrap': bootstrap}
    identity['id'] = canonical_id(identity)
    validate_identity(identity)
    engines = home / 'engines'
    engines.mkdir(mode=0o700, exist_ok=True)
    secure_path(engines, expected_uid=expected_uid, directory=True)
    destination = engines / identity['id']
    if destination.exists() or destination.is_symlink():
        verify_engine(home, identity, expected_uid=expected_uid)
        return identity
    temporary = Path(tempfile.mkdtemp(prefix='.stage-', dir=engines))
    try:
        for name in ENGINE_FILES:
            data = (source / 'scripts' / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != files[name]:
                raise EngineError('ENGINE_CONTENT', 'Engine changed while staging')
            with (temporary / name).open('xb') as stream:
                os.chmod(temporary / name, 0o600)
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        atomic_json(temporary / 'identity.json', identity)
        sync_dir(temporary)
        os.rename(temporary, destination)
        sync_dir(engines)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    verify_engine(home, identity, expected_uid=expected_uid)
    return identity


def stage_engine(home, source, release, *, expected_uid=0):
    return _stage(Path(home), Path(source), release, False, expected_uid)


def stage_bootstrap(home, source, *, expected_uid=0):
    """Explicit administrator installation only; does not promote a selector."""
    return _stage(Path(home), Path(source), None, True, expected_uid)


def pending_jobs(home):
    result = []
    for path in sorted((Path(home) / 'jobs').glob('*/status.json')):
        job = read_json(path)
        if job.get('state') not in TERMINAL:
            result.append(job)
    return result


def select_engine(home, *, expected_uid=0):
    home = Path(home)
    pending = pending_jobs(home)
    if pending:
        identities = [job.get('engine') for job in pending]
        if any(not identity for identity in identities) or any(i != identities[0] for i in identities):
            raise EngineError('ENGINE_RECOVERY_REQUIRED', 'Pending operations have unknown or conflicting engine owners')
        identity = identities[0]
    else:
        identity = read_json(home / 'engine-current.json')
    verify_engine(home, identity, expected_uid=expected_uid)
    return identity


def promote_engine(home, identity, job_id, *, expected_uid=0):
    """Caller MUST hold admission fence and prevent subsequent submissions."""
    home = Path(home)
    verify_engine(home, identity, expected_uid=expected_uid)
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise EngineError('ENGINE_JOB', 'Invalid engine promotion job')
    job = read_json(home / 'jobs' / job_id / 'status.json')
    release = read_json(home / 'jobs' / job_id / 'release.json')
    if (job.get('state') != 'succeeded' or not job.get('commit_decided') or pending_jobs(home) or
            job.get('target_version') != identity['version'] or
            any(release.get(k) != identity[k] for k in ('repository', 'release_id', 'sha256', 'version'))):
        raise EngineError('ENGINE_PROMOTION', 'Engine promotion is not bound to a successful idle release')
    current = read_json(home / 'engine-current.json')
    if version(identity['version']) < version(current['version']):
        raise EngineError('ENGINE_DOWNGRADE', 'Engine downgrade refused')
    same_bootstrap_bytes = (current.get('bootstrap') is True and identity.get('bootstrap') is False and
                            all(current.get(k) == identity.get(k) for k in
                                ('files', 'repository', 'protocol', 'min_launcher')))
    if (version(identity['version']) == version(current['version']) and identity['id'] != current['id']
            and not same_bootstrap_bytes):
        raise EngineError('ENGINE_DOWNGRADE', 'Same-version engine replacement refused')
    atomic_json(home / 'engine-current.json', identity)
    return identity


def mark_ready(home, identity, *, expected_uid=0):
    verify_engine(home, identity, expected_uid=expected_uid)
    atomic_json(Path(home) / 'engine-ready.json', {'id': identity['id'], 'pid': os.getpid(), 'at': time.time()})


def validate_supervised_fd(home, descriptor, *, expected_uid=0):
    if type(descriptor) is not int or descriptor < 0:
        raise EngineError('ENGINE_LOCK', 'Invalid supervised lock')
    info = os.fstat(descriptor)
    path = Path(home) / 'service.lock'
    expected = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(expected.st_mode) or
            (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino) or info.st_uid != expected_uid or info.st_mode & 0o077):
        raise EngineError('ENGINE_LOCK', 'Supervised lock does not match installed service')
    # A matching open file is not sufficient: a separately opened description
    # must observe an existing exclusive flock rather than acquire it itself.
    probe = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise EngineError('ENGINE_LOCK', 'Passed descriptor does not own the service lock') from exc
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise EngineError('ENGINE_LOCK', 'Supervised descriptor has no active service lock')
    finally:
        os.close(probe)
    return descriptor
