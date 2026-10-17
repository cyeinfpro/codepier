#!/usr/bin/env python3
"""Owned, bounded test scratch with durable evidence and conservative recovery.

Only scratch created by this helper is removed. Evidence and pre-existing files
are never removed. POSIX fd-relative/no-follow operations are required: unsupported
platforms fail closed. Resource budgets are sampled admission/stop guards, not a
filesystem quota. Commands must keep descendants in their inherited process group;
escaping the group (setsid/daemonization) is outside this runner's contract.
Recovery never signals a saved PID, and refuses live/unknown owner or child PIDs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.owned_process_group import child_exited, live_group_members, stop_owned_group

SCHEMA = 1
MANIFEST = 'managed-run.json'
LOCK = '.managed-run.lock'
MARKER = '.managed-owner.json'
MIN_FREE = 256 * 1024**2
MAX_SCRATCH = 2 * 1024**3
MAX_LOG = 8 * 1024**2


class SafetyError(RuntimeError):
    """The required ownership, identity or process proof is unavailable."""


class AdmissionError(SafetyError):
    """Insufficient resources; no command was started."""


def _supported():
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW')
            or not hasattr(os, 'waitid') or not hasattr(os, 'WNOWAIT')
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or not shutil.rmtree.avoids_symlink_attacks):
        raise SafetyError('fd-safe POSIX cleanup and unreaped-child observation required')


def _identity(value):
    return {'dev': value.st_dev, 'ino': value.st_ino, 'uid': value.st_uid}


def _matches(value, expected):
    return _identity(value) == expected


def _directory(path):
    """Open a canonical absolute path without following any current symlink."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise SafetyError('Expected a canonical absolute path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            newer = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = newer
        return fd
    except BaseException:
        os.close(fd)
        raise


def _json_read(fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 65536:
            raise SafetyError('Invalid ownership or size of metadata')
        chunks = os.read(handle, 65537)
        if len(chunks) > 65536:
            raise SafetyError('Metadata exceeds limit')
        return json.loads(chunks)
    finally:
        os.close(handle)


def _json_write(fd, name, value, *, initial=False):
    encoded = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    temporary = name if initial else '.manifest-' + uuid.uuid4().hex
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        if not initial:
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        # Do not hide a failed durable update by claiming cleanup succeeded.
        raise


def _dead(pid):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)  # observation only, never a stop signal
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _scan(fd, *, device, limit=None, count=None, mutable=False, depth=0):
    """Snapshot identity; symlink leaves are recorded but never traversed."""
    if depth > 128:
        raise SafetyError('Scratch depth exceeds safe bound')
    if count is None:
        count = [0]
    entries, total = {}, 0
    for name in os.listdir(fd):
        count[0] += 1
        if count[0] > 100000:
            raise SafetyError('Scratch inventory exceeds safe entry bound')
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if mutable:
                continue
            raise
        if info.st_dev != device or info.st_uid != os.getuid():
            raise SafetyError('Scratch ownership or filesystem changed')
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                if mutable:
                    continue
                raise
            try:
                if not _matches(os.fstat(child), _identity(info)):
                    if mutable:
                        continue
                    raise SafetyError('Scratch directory identity changed')
                children, size = _scan(child, device=device, limit=limit, count=count, mutable=mutable, depth=depth + 1)
            finally:
                os.close(child)
            entries[name] = (_identity(info), children)
            total += size
        elif stat.S_ISREG(info.st_mode):
            entries[name] = (_identity(info), None)
            total += info.st_size
        elif stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode):
            entries[name] = (_identity(info), stat.S_IFMT(info.st_mode))
            total += info.st_size
        else:
            raise SafetyError('Scratch contains a device or unsupported file')
        if limit is not None and total > limit:
            raise AdmissionError('Scratch byte budget exceeded')
    return entries, total


def _erase(fd, snapshot):
    """Delete only snapshotted entries, always relative to held directory fds."""
    if set(os.listdir(fd)) != set(snapshot):
        raise SafetyError('Scratch entries changed before cleanup')
    for name in sorted(snapshot, key=lambda item: item == MARKER):
        expected, children = snapshot[name]
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not _matches(info, expected):
            raise SafetyError('Scratch identity changed before cleanup')
        if isinstance(children, int):
            if stat.S_IFMT(info.st_mode) != children:
                raise SafetyError('Scratch leaf type changed')
            os.unlink(name, dir_fd=fd)
        elif children is None:
            if not stat.S_ISREG(info.st_mode):
                raise SafetyError('Scratch file type changed')
            os.unlink(name, dir_fd=fd)
        else:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if not _matches(os.fstat(child), expected):
                    raise SafetyError('Scratch child changed before cleanup')
                _erase(child, children)
                if not _matches(os.stat(name, dir_fd=fd, follow_symlinks=False), expected):
                    raise SafetyError('Scratch child moved before cleanup')
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)


def _scratch_name(fd, data):
    choices = []
    for name in (data['scratch_name'], data['cleanup_name']):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or not _matches(info, data['scratch_identity']):
            raise SafetyError('Scratch path is a symlink or has changed identity')
        choices.append(name)
    if len(choices) > 1:
        raise SafetyError('Multiple scratch identities require manual inspection')
    return choices[0] if choices else None


def _short_tmp(data, *, remove=False, limit=None):
    """Verify and optionally reclaim this run's real, short temporary root."""
    info = data.get('short_tmp')
    if info is None:
        if data.get('short_tmp_setup') == 'pending':
            raise SafetyError('Short temporary creation identity is incomplete')
        return 0  # Older receipts predate short-path support.
    if not isinstance(info, dict):
        raise SafetyError('Invalid short temporary metadata')
    root = Path('/tmp').resolve(strict=True)
    if info.get('parent') != str(root):
        raise SafetyError('Short temporary parent changed')
    parent = _directory(root)
    child = None
    try:
        if not _matches(os.fstat(parent), info['parent_identity']):
            raise SafetyError('Short temporary parent identity changed')
        base = 'cpt-' + data['run_id']
        names = []
        for name in (base, base + '-cleanup'):
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(st.st_mode) or not _matches(st, info['identity']) or stat.S_IMODE(st.st_mode) != 0o700:
                raise SafetyError('Short temporary directory identity changed')
            names.append(name)
        pending = data.get('cleanup_state') in {'cleanup_pending', 'cleaned'}
        if not names and pending:
            return 0
        if len(names) != 1:
            raise SafetyError('Short temporary directory missing or ambiguous')
        name = names[0]
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        if not _matches(os.fstat(child), info['identity']):
            raise SafetyError('Short temporary directory changed while opening')
        try:
            marker = _json_read(child, MARKER)
        except FileNotFoundError:
            if not pending or os.listdir(child):
                raise
        else:
            if marker != {'run_id': data['run_id'], 'uid': data['uid'], 'evidence': data['evidence']}:
                raise SafetyError('Short temporary ownership marker changed')
        if not remove:
            if limit is None:
                return 0
            return _scan(child, device=info['identity']['dev'], limit=limit, mutable=True)[1]
        inventory, total = _scan(child, device=info['identity']['dev'])
        if name == base:
            os.rename(base, base + '-cleanup', src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            name = base + '-cleanup'
        if not _matches(os.stat(name, dir_fd=parent, follow_symlinks=False), info['identity']):
            raise SafetyError('Short temporary directory changed during quarantine')
        _erase(child, inventory)
        if not _matches(os.stat(name, dir_fd=parent, follow_symlinks=False), info['identity']):
            raise SafetyError('Short temporary directory moved during cleanup')
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
        return total
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)


def _create_short_tmp(data):
    # Canonical real directory stays short even for applications using realpath.
    root = Path('/tmp').resolve(strict=True)
    parent = _directory(root)
    child = None
    try:
        name = 'cpt-' + data['run_id']
        os.mkdir(name, mode=0o700, dir_fd=parent)
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        st = os.fstat(child)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise SafetyError('Short temporary directory ownership differs')
        info = {'parent': str(root), 'parent_identity': _identity(os.fstat(parent)),
                'identity': _identity(st)}
        data['short_tmp'] = info  # Available to failure handling before more I/O.
        _json_write(child, MARKER, {'run_id': data['run_id'], 'uid': data['uid'],
                                   'evidence': data['evidence']}, initial=True)
        os.fsync(parent)
        return info
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)


def _validate(fd, data):
    if not isinstance(data, dict):
        raise SafetyError('Manifest must be an object')
    if data.get('schema') != SCHEMA or data.get('uid') != os.getuid():
        raise SafetyError('Unsupported or foreign manifest')
    token = data.get('run_id')
    if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise SafetyError('Invalid run identity')
    if data.get('scratch_name') != '.scratch-' + token or data.get('cleanup_name') != '.cleanup-' + token:
        raise SafetyError('Invalid owned scratch name')
    if not _matches(os.fstat(fd), data.get('evidence_identity')):
        raise SafetyError('Evidence directory identity changed')
    current = _directory(data['evidence'])
    try:
        if not _matches(os.fstat(current), data['evidence_identity']):
            raise SafetyError('Evidence path identity changed')
    finally:
        os.close(current)
    lock_info = os.stat(LOCK, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(lock_info.st_mode) or not _matches(lock_info, data.get('lock_identity')):
        raise SafetyError('Ownership lock changed')
    _short_tmp(data)
    name = _scratch_name(fd, data)
    if name is not None:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if not _matches(os.fstat(child), data['scratch_identity']):
                raise SafetyError('Scratch changed while opening')
            try:
                marker = _json_read(child, MARKER)
            except FileNotFoundError:
                # Last-marker deletion may have preceded a crash before rmdir.
                if name == data['cleanup_name'] and data.get('cleanup_state') == 'cleanup_pending' and not os.listdir(child):
                    return name
                raise
            if marker != {'run_id': token, 'uid': data['uid'], 'evidence': data['evidence']}:
                raise SafetyError('Ownership marker changed')
        finally:
            os.close(child)
    return name


def _cleanup(fd, data):
    """Persist cleanup_pending before touching owned scratch, retain evidence."""
    name = _validate(fd, data)
    data['cleanup_pending'] = True
    data['cleanup_state'] = 'cleanup_pending'
    _json_write(fd, MANIFEST, data)
    _short_tmp(data, remove=True)
    if name is None:
        data['cleanup_pending'] = False
        data['cleanup_state'] = 'cleaned'
        _json_write(fd, MANIFEST, data)
        return
    data['cleanup_pending'] = True
    data['cleanup_state'] = 'cleanup_pending'
    _json_write(fd, MANIFEST, data)
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if not _matches(os.fstat(child), data['scratch_identity']):
            raise SafetyError('Scratch identity changed')
        inventory, _ = _scan(child, device=data['scratch_identity']['dev'])
    finally:
        os.close(child)
    # Quarantine first, then verify again. A replacement raced into this name
    # can be moved but never erased, because its inode will fail the check.
    if name != data['cleanup_name']:
        os.rename(name, data['cleanup_name'], src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    name = data['cleanup_name']
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if not _matches(os.fstat(child), data['scratch_identity']):
            raise SafetyError('Scratch changed during quarantine')
        _erase(child, inventory)
        if not _matches(os.stat(name, dir_fd=fd, follow_symlinks=False), data['scratch_identity']):
            raise SafetyError('Scratch moved during cleanup')
        os.rmdir(name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(child)
    data['cleanup_pending'] = False
    data['cleanup_state'] = 'cleaned'
    data.pop('cleanup_error', None)
    _json_write(fd, MANIFEST, data)


class ManagedTestRun:
    """One command per instance; retain reports under output and own only scratch."""

    def __init__(self, output, *, min_free_bytes=MIN_FREE,
                 max_scratch_bytes=MAX_SCRATCH, max_log_bytes=MAX_LOG, metadata=None):
        _supported()
        for value in (min_free_bytes, max_scratch_bytes, max_log_bytes):
            if type(value) is not int or value < 0:
                raise ValueError('Resource limits must be nonnegative integers')
        if max_scratch_bytes == 0:
            raise ValueError('Scratch budget must be positive')
        if metadata is not None and (not isinstance(metadata, dict) or
                any(key not in {'project', 'workspace', 'task'} or not isinstance(value, str)
                    or len(value) > 200 for key, value in metadata.items())):
            raise ValueError('Metadata accepts project/workspace/task strings up to 200 characters')
        requested = Path(output).absolute()
        if requested.is_symlink():
            raise SafetyError('Evidence directory must not be a symlink')
        # Resolve existing system ancestors once (notably macOS /var and /tmp).
        requested.mkdir(parents=True, exist_ok=True)
        self.evidence = requested.resolve(strict=True)
        self._fd = _directory(self.evidence)
        self._lock_fd = None
        self._closed = False
        self._started = False
        self._stopped = True
        self._cancel = threading.Event()
        self.min_free_bytes = min_free_bytes
        self.max_scratch_bytes = max_scratch_bytes
        self.max_log_bytes = max_log_bytes
        self.manifest_path = self.evidence / MANIFEST
        try:
            self._admit()
            if os.fstat(self._fd).st_uid != os.getuid():
                raise SafetyError('Evidence directory belongs to another user')
            # A prior lock/manifest is retained evidence, never adopted/overwritten.
            if os.listdir(self._fd):
                raise SafetyError('Use a fresh output directory; existing run evidence retained')
            import fcntl
            self._lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            token = uuid.uuid4().hex
            name = '.scratch-' + token
            os.mkdir(name, mode=0o700, dir_fd=self._fd)
            self.scratch = self.evidence / name
            scratch_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)
            try:
                _json_write(scratch_fd, MARKER, {'run_id': token, 'uid': os.getuid(), 'evidence': str(self.evidence)}, initial=True)
                for folder in ('tmp', 'cache', 'pycache'):
                    os.mkdir(folder, mode=0o700, dir_fd=scratch_fd)
                scratch_identity = _identity(os.fstat(scratch_fd))
            finally:
                os.close(scratch_fd)
            self.data = {
                'schema': SCHEMA, 'run_id': token, 'uid': os.getuid(),
                'evidence': str(self.evidence), 'evidence_identity': _identity(os.fstat(self._fd)),
                'scratch_name': name, 'cleanup_name': '.cleanup-' + token,
                'scratch_identity': scratch_identity, 'lock_identity': _identity(os.fstat(self._lock_fd)),
                'owner_pid': os.getpid(), 'child_pid': None, 'child_state': 'not_started',
                'status': 'created', 'cleanup_pending': True, 'cleanup_state': 'active',
                'created_at': time.time(), 'metadata': metadata or {},
                'limits': {'min_free_bytes': min_free_bytes, 'max_scratch_bytes': max_scratch_bytes, 'max_log_bytes': max_log_bytes},
            }
            self.data['short_tmp_setup'] = 'pending'
            _json_write(self._fd, MANIFEST, self.data, initial=True)
            self.data['short_tmp'] = _create_short_tmp(self.data)
            self.data.pop('short_tmp_setup')
            _json_write(self._fd, MANIFEST, self.data)
        except BaseException:
            if getattr(self, 'data', {}).get('short_tmp') is not None:
                try:
                    _short_tmp({**self.data, 'cleanup_state': 'cleanup_pending'}, remove=True)
                except (OSError, KeyError, TypeError, SafetyError):
                    pass  # Retain unverifiable identity rather than erase it.
            if getattr(self, 'data', None) is not None:
                try:
                    self.data['cleanup_state'] = 'cleanup_pending'
                    self.data['status'] = 'initialization_failed'
                    _json_write(self._fd, MANIFEST, self.data)
                except OSError:
                    pass  # The first receipt still identifies the run/root name.
            self._close()
            raise

    def _admit(self):
        info = os.fstatvfs(self._fd)
        if info.f_bavail * info.f_frsize < self.min_free_bytes:
            raise AdmissionError('Minimum free disk headroom is unavailable')

    def environment(self):
        temporary = str(Path(self.data['short_tmp']['parent']) / ('cpt-' + self.data['run_id']))
        return {**os.environ, 'TMPDIR': temporary,
                'TMP': temporary, 'TEMP': temporary,
                'XDG_CACHE_HOME': str(self.scratch / 'cache'),
                'PYTHONPYCACHEPREFIX': str(self.scratch / 'pycache'),
                'PYTHONDONTWRITEBYTECODE': '1'}

    def cancel(self):
        """Thread-safe request; only the live runner may signal its owned child."""
        self._cancel.set()

    def _budget(self):
        self._admit()
        name = _validate(self._fd, self.data)
        temporary_bytes = _short_tmp(self.data, limit=self.max_scratch_bytes)
        temporary_fd = _directory(Path(self.data['short_tmp']['parent']))
        try:
            info = os.fstatvfs(temporary_fd)
            if info.f_bavail * info.f_frsize < self.min_free_bytes:
                raise AdmissionError('Minimum temporary disk headroom is unavailable')
        finally:
            os.close(temporary_fd)
        if name != self.data['scratch_name']:
            raise SafetyError('Active scratch moved or disappeared')
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)
        try:
            _scan(fd, device=self.data['scratch_identity']['dev'], limit=self.max_scratch_bytes - temporary_bytes, mutable=True)
        finally:
            os.close(fd)

    def run(self, command, *, timeout, cwd=None, env=None, log_name='output.log'):
        if self._started or self._closed:
            raise SafetyError('A managed run is single-use')
        if timeout <= 0 or not command:
            raise ValueError('A command and positive timeout are required')
        if Path(log_name).name != log_name or log_name in {LOCK, MANIFEST, MARKER}:
            raise ValueError('Log must be a new evidence basename')
        self._started = True
        begin = time.monotonic()
        process = None
        selector = selectors.DefaultSelector()
        log = None
        record = {'exit_code': None, 'timed_out': False, 'forced_termination': False,
                  'cancelled': False, 'seconds': 0, 'status': 'failed',
                  'log_truncated': False, 'cleanup_pending': True,
                  'evidence': str(self.evidence), 'manifest': str(self.manifest_path)}
        written = 0
        try:
            self._budget()
            if self._cancel.is_set():
                record['cancelled'] = True
                record['status'] = 'cancelled'
                return record
            logfd = os.open(log_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            log = os.fdopen(logfd, 'wb')
            self.data['child_state'] = 'launching'
            self.data['status'] = 'running'
            _json_write(self._fd, MANIFEST, self.data)
            self._stopped = False
            # A crash in this launch window is deliberately unrecoverable without
            # inspection: there may be a child whose PID was never committed.
            process = subprocess.Popen(command, cwd=cwd, env={**self.environment(), **(env or {}),
                                                **{key: value for key, value in self.environment().items()
                                                   if key in {'TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME',
                                                              'PYTHONPYCACHEPREFIX', 'PYTHONDONTWRITEBYTECODE'}}},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            self.data['child_pid'] = process.pid
            self.data['child_state'] = 'running'
            _json_write(self._fd, MANIFEST, self.data)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            next_budget = begin
            while True:
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        remaining = max(0, self.max_log_bytes - written)
                        log.write(chunk[:remaining])
                        written += min(remaining, len(chunk))
                        record['log_truncated'] |= len(chunk) > remaining
                now = time.monotonic()
                if self._cancel.is_set():
                    record['cancelled'] = True
                    record['status'] = 'cancelled'
                    break
                if now - begin >= timeout:
                    record['timed_out'] = True
                    record['status'] = 'timeout'
                    break
                if now >= next_budget:
                    self._budget()
                    next_budget = now + .25
                if child_exited(process.pid):
                    record['status'] = 'finished'
                    break
        except BaseException as error:
            record['status'] = 'cancelled' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed'
            record['cancelled'] = isinstance(error, (KeyboardInterrupt, SystemExit))
            record['error'] = type(error).__name__ + ': ' + str(error)
            if (process is None and self.data.get('child_state') == 'launching'
                    and isinstance(error, (FileNotFoundError, PermissionError))):
                # Popen reaps its failed exec child for these ordinary errors.
                # An asynchronous interruption in the launch window stays unknown.
                self._stopped = True
                self.data['child_state'] = 'not_started'
            raise
        finally:
            try:
                if process is not None:
                    # Never poll()/wait() first: the unreaped child pins the PGID.
                    natural = child_exited(process.pid)
                    descendants = bool(live_group_members(process.pid)) if natural else True
                    self._stopped, code = stop_owned_group(process.pid, grace=.5, kill_timeout=3, grouped=True)
                    process.returncode = code
                    record['exit_code'] = code
                    record['forced_termination'] = not natural or descendants or not self._stopped
                    self.data['child_state'] = 'stopped' if self._stopped else 'unknown'
                    if self._stopped:
                        # Drain only data already buffered; keep storage bounded.
                        while process.stdout is not None:
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            remaining = max(0, self.max_log_bytes - written)
                            if log is not None:
                                log.write(chunk[:remaining])
                            written += min(remaining, len(chunk))
                            record['log_truncated'] |= len(chunk) > remaining
                    if process.stdout is not None:
                        process.stdout.close()
            except BaseException as error:
                self._stopped = False
                self.data['child_state'] = 'unknown'
                record['stop_error'] = type(error).__name__ + ': ' + str(error)
            finally:
                selector.close()
                if log is not None:
                    try:
                        log.flush()
                        os.fsync(log.fileno())
                    except OSError as error:
                        record['evidence_error'] = type(error).__name__ + ': ' + str(error)
                        record['status'] = 'failed'
                    finally:
                        try:
                            log.close()
                        except OSError as error:
                            record['evidence_error'] = type(error).__name__ + ': ' + str(error)
                            record['status'] = 'failed'
                if record['status'] == 'finished':
                    record['status'] = ('success' if record['exit_code'] == 0 and self._stopped
                                        and not record['forced_termination'] else 'failed')
                record['seconds'] = round(time.monotonic() - begin, 3)
                self.data['result'] = dict(record)
                self.finish(record['status'])
                record['cleanup_pending'] = self.data['cleanup_pending']
                self.data['result'] = dict(record)
                # finish retains the fd until context exit, so persist final receipt.
                try:
                    _json_write(self._fd, MANIFEST, self.data)
                except OSError as error:
                    record['cleanup_pending'] = True
                    record['evidence_error'] = type(error).__name__ + ': ' + str(error)
                    self.data['cleanup_pending'] = True
        return record

    def finish(self, status):
        if self._closed:
            return self.data
        self.data['status'] = status
        self.data['finished_at'] = time.time()
        self.data['cleanup_pending'] = True
        try:
            _json_write(self._fd, MANIFEST, self.data)
            if not self._stopped or self.data['child_state'] not in {'not_started', 'stopped'}:
                raise SafetyError('Process stop is unverified; scratch retained')
            _cleanup(self._fd, self.data)
        except (OSError, ValueError, KeyError, SafetyError) as error:
            self.data['cleanup_pending'] = True
            self.data['cleanup_state'] = 'cleanup_pending'
            self.data['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            try:
                _json_write(self._fd, MANIFEST, self.data)
            except OSError:
                pass  # Earlier durable state remains pending; never claim success.
        return self.data

    def _close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if not self._closed:
            os.close(self._fd)
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if not self._closed and self.data.get('cleanup_pending'):
                self.finish('cancelled' if kind and issubclass(kind, (KeyboardInterrupt, SystemExit)) else
                            'failed' if kind else self.data['status'])
        finally:
            self._close()
        return False


def recover(manifest_path, *, dry_run=True):
    """Inspect by default. Explicit apply removes only unchanged stopped scratch."""
    result = {'status': 'blocked', 'cleanup_pending': True, 'dry_run': dry_run, 'reasons': []}
    fd = lockfd = None
    try:
        _supported()
        path = Path(manifest_path).absolute()
        if path.name != MANIFEST or path.is_symlink():
            raise SafetyError('Expected a non-symlink managed-run.json')
        path = path.parent.resolve(strict=True) / MANIFEST
        fd = _directory(path.parent)
        data = _json_read(fd, MANIFEST)
        if not isinstance(data, dict) or data.get('evidence') != str(path.parent):
            raise SafetyError('Evidence path differs from manifest')
        _validate(fd, data)
        import fcntl
        lockfd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
        if not _matches(os.fstat(lockfd), data['lock_identity']):
            raise SafetyError('Ownership lock changed while opening')
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError('Run ownership lock is active') from None
        if not _dead(data.get('owner_pid')):
            raise SafetyError('Owner PID is alive or unknown')
        state = data.get('child_state')
        if state not in {'not_started', 'running', 'stopped'}:
            raise SafetyError('Child launch or stop state is unknown')
        pid = data.get('child_pid')
        if state != 'not_started':
            if not _dead(pid):
                raise SafetyError('Child PID is alive or unknown')
            if live_group_members(pid):
                raise SafetyError('Child process group still has live members')
        name = _validate(fd, data)
        if name:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _scan(child, device=data['scratch_identity']['dev'])
            finally:
                os.close(child)
        if not dry_run:
            data['recovered_at'] = time.time()
            _cleanup(fd, data)
        result.update(status='ready' if dry_run and name else 'cleaned',
                      cleanup_pending=bool(name) if dry_run else False)
    except (OSError, ValueError, KeyError, TypeError, SafetyError, subprocess.SubprocessError) as error:
        result['reasons'].append(type(error).__name__ + ': ' + str(error))
    finally:
        if lockfd is not None:
            os.close(lockfd)
        if fd is not None:
            os.close(fd)
    return result


def project_result_view(result, root):
    """Project-relative presentation of wrapper metadata, not log rewriting.

    The full durable manifest remains unchanged for owner-side recovery. An
    external result is explicitly not a project file; it never gets a fake path.
    """
    value = dict(result)
    root = Path(root).resolve()
    for field in ('evidence', 'manifest'):
        if not isinstance(value.get(field), str):
            continue
        path = Path(value[field]).resolve()
        if path.is_relative_to(root):
            value[field] = path.relative_to(root).as_posix()
        else:
            value.pop(field)
            value[field + '_ref'] = {'scope': 'outside_project', 'name': path.name}
    value.update(path_base='project', original_manifest_retained=True)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument('--output', type=Path)
    destination.add_argument('--output-root', type=Path, help='Create a unique retained run under this root')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--result-view', choices=('absolute', 'project'), default='absolute',
                        help='Present wrapper paths relative to cwd; raw logs and recovery manifests are unchanged')
    for label in ('project', 'workspace', 'task'):
        parser.add_argument('--' + label, default='')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--min-free-bytes', type=int, default=MIN_FREE)
    parser.add_argument('--max-scratch-bytes', type=int, default=MAX_SCRATCH)
    parser.add_argument('--max-log-bytes', type=int, default=MAX_LOG)
    parser.add_argument('--recover', type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply safe recovery; inspection is the default')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.recover:
        result = recover(args.recover, dry_run=not args.apply or args.dry_run)
        print(json.dumps(result))
        return 0 if result['status'] in {'ready', 'cleaned'} else 2
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if (args.output is None and args.output_root is None) or not command:
        parser.error('--output/--output-root and a command after -- are required')
    output = args.output or args.output_root / ('run-' + uuid.uuid4().hex)
    metadata = {key: getattr(args, key) for key in ('project', 'workspace', 'task') if getattr(args, key)}
    import signal
    try:
        with ManagedTestRun(output, min_free_bytes=args.min_free_bytes,
                            max_scratch_bytes=args.max_scratch_bytes,
                            max_log_bytes=args.max_log_bytes, metadata=metadata) as run:
            old = {number: signal.signal(number, lambda *_: run.cancel())
                   for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                result = run.run(command, timeout=args.timeout, cwd=args.cwd)
            finally:
                for number, handler in old.items():
                    signal.signal(number, handler)
        presented = project_result_view(result, args.cwd or Path.cwd()) if args.result_view == 'project' else result
        print(json.dumps(presented))
        return 0 if result['status'] == 'success' and not result['cleanup_pending'] else 1
    except (OSError, ValueError, SafetyError) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
