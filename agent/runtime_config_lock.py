"""Cooperating runtime-config writers with conservative crash recovery.

A stable OS-locked guard serializes new writers. The legacy exclusive marker is
also held, so older writers still see busy. Only a private, versioned marker for
a definitely exited process may be reclaimed; an unknown/legacy marker stays
blocked. No timeout, guessed PID termination, or forced lock removal is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import uuid


def process_exists(pid):
    """True/False/None (unknown). Never send Windows TerminateProcess(0)."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def _identity(info):
    return info.st_dev, info.st_ino


def _private_regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        and (not hasattr(os, 'getuid') or info.st_uid == os.getuid())
        and (os.name != 'posix' or not stat.S_IMODE(info.st_mode) & 0o077))


def _same(path, info):
    try:
        current = path.lstat()
        return _identity(current) == _identity(info) and _private_regular(current)
    except OSError:
        return False


def _recover_marker(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not _private_regular(info) or not 1 <= info.st_size <= 1024:
            return False
        raw = os.read(descriptor, 1025)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return False
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'pid', 'nonce'}
                or type(value.get('schema_version')) is not int or value['schema_version'] != 2
                or type(value.get('pid')) is not int or not 1 <= value['pid'] <= 2147483647
                or not isinstance(value.get('nonce'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', value['nonce'])
                or process_exists(value['pid']) is not False):
            return False
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 1025) != raw or not _same(path, info):
            return False
        # Windows cannot remove a marker while this read handle is open.
        # Keep the stable guard held, close, then recheck the exact inode.
    finally:
        os.close(descriptor)
    if not _same(path, info):
        return False
    path.unlink()
    return True


def _lock(descriptor):
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise FileExistsError('Another runtime configuration writer owns the guard') from exc


def _unlock(descriptor):
    if os.name == 'nt':
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def config_lock(config_path):
    path = Path(config_path)
    marker = path.with_name(path.name + '.runtime.lock')
    guard = path.with_name(path.name + '.runtime.guard')
    if guard.is_symlink():
        raise ValueError('Runtime configuration guard cannot be a symbolic link')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(guard, flags, 0o600)
    locked = False
    marker_fd = None
    marker_info = None
    try:
        info = os.fstat(descriptor)
        if not _private_regular(info) or not _same(guard, info):
            raise ValueError('Runtime configuration guard must be an owned private regular file')
        _lock(descriptor)
        locked = True
        if not _same(guard, info):
            raise FileExistsError('Runtime configuration guard changed')
        marker_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        try:
            marker_fd = os.open(marker, marker_flags, 0o600)
        except FileExistsError:
            if not _recover_marker(marker):
                raise
            marker_fd = os.open(marker, marker_flags, 0o600)
        marker_info = os.fstat(marker_fd)
        with os.fdopen(os.dup(marker_fd), 'wb') as stream:
            stream.write((json.dumps({'schema_version': 2, 'pid': os.getpid(), 'nonce': uuid.uuid4().hex}) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        if marker_info is not None and _same(marker, marker_info):
            try:
                marker.unlink()
            except OSError:
                pass  # Preserved marker is recoverable only with process-exit evidence.
        if locked:
            try:
                _unlock(descriptor)
            except OSError:
                pass  # close also releases the OS lock; never mask a committed write.
        os.close(descriptor)
        # Never unlink the stable guard: racing openers must lock the same inode.
