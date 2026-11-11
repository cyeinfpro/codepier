"""Publish a complete staged file without replacing an existing destination.

Some SMB servers reject hard links while supporting exclusive rename.  The
fallback must be a native *no-replace* operation, never a check followed by
``os.replace`` for strict atomic publication. Filesystem capabilities
are tested by the actual operation, rather than cached by path/mount name.
Explicit compatibility callers can opt into non-atomic exclusive creation.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass


_LINK_UNSUPPORTED = frozenset(
    value
    for value in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if value is not None
)
_RENAME_UNSUPPORTED = _LINK_UNSUPPORTED | {errno.EINVAL}


class AtomicCreateUnsupported(OSError):
    """Neither hard links nor native exclusive rename are supported."""


@dataclass(frozen=True)
class FilePublication:
    method: str
    source_retained: bool
    atomic: bool = True
    identity: tuple[int, int] | None = None


class ExclusiveCreateUnconfirmed(OSError):
    """The exclusive target may contain partial data; never replay or unlink."""


def _pin_parent(stack, path, directory_fd):
    """Walk without symlinks, then address the leaf relative to a pinned parent."""
    value = os.fspath(path)
    if directory_fd is not None:
        if not isinstance(value, str) or value in ("", ".", "..") or "/" in value:
            raise ValueError("Directory-relative publication requires a single filename")
        return directory_fd, value
    parent, leaf = os.path.split(os.path.abspath(value))
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, fd)
    for component in parent.split(os.path.sep):
        if not component:
            continue
        fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        stack.callback(os.close, fd)
    return fd, leaf


def _exclusive_create_copy(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
    """No-clobber compatibility, NOT atomic content publication.

    A crash can leave a partial destination. Never unlink it on failure: a
    check-then-unlink could remove a concurrent replacement. The caller records
    an unconfirmed receipt and retains recovery evidence instead of replaying.
    """
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise AtomicCreateUnsupported(errno.ENOTSUP, "Safe exclusive creation is unavailable")
    with ExitStack() as stack:
        source_parent, source_name = _pin_parent(stack, source, src_dir_fd)
        dest_parent, dest_name = _pin_parent(stack, destination, dst_dir_fd)
        source_fd = os.open(
            source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent
        )
        stack.callback(os.close, source_fd)
        original = os.fstat(source_fd)
        if not stat.S_ISREG(original.st_mode):
            raise OSError(errno.EINVAL, "Staging file must be regular")
        try:
            dest_fd = os.open(
                dest_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                stat.S_IMODE(original.st_mode),
                dir_fd=dest_parent,
            )
        except OSError as exc:
            if exc.errno in {
                errno.EIO,
                errno.ETIMEDOUT,
                errno.EINTR,
                getattr(errno, "ENOTCONN", -1),
                getattr(errno, "ESTALE", -1),
            }:
                raise ExclusiveCreateUnconfirmed(
                    exc.errno, "Exclusive creation outcome is unknown"
                ) from exc
            raise
        try:
            with os.fdopen(dest_fd, "w+b", buffering=0) as output:
                os.fchmod(output.fileno(), stat.S_IMODE(original.st_mode))
                created = os.fstat(output.fileno())
                expected = hashlib.sha256()
                remaining_bytes = original.st_size
                while remaining_bytes:
                    chunk = os.read(source_fd, min(1024 * 1024, remaining_bytes))
                    if not chunk:
                        raise OSError(errno.EIO, "Staging file was truncated")
                    remaining_bytes -= len(chunk)
                    expected.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        written = output.write(remaining)
                        if not written:
                            raise OSError(errno.EIO, "Short write")
                        remaining = remaining[written:]
                os.fsync(output.fileno())
                latest = os.fstat(source_fd)
                if (original.st_size, original.st_mtime_ns, original.st_ctime_ns) != (
                    latest.st_size,
                    latest.st_mtime_ns,
                    latest.st_ctime_ns,
                ):
                    raise OSError(errno.EIO, "Staging content changed")
                before_verify = os.fstat(output.fileno())
                output.seek(0)
                actual = hashlib.sha256()
                verify_remaining = original.st_size + 1
                while verify_remaining and (
                    chunk := output.read(min(1024 * 1024, verify_remaining))
                ):
                    verify_remaining -= len(chunk)
                    actual.update(chunk)
                verified = os.fstat(output.fileno())
                current = os.stat(dest_name, dir_fd=dest_parent, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino)
                    or actual.digest() != expected.digest()
                    or verified.st_size != original.st_size
                    or (before_verify.st_size, before_verify.st_mtime_ns, before_verify.st_ctime_ns)
                    != (verified.st_size, verified.st_mtime_ns, verified.st_ctime_ns)
                    or (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                    != (verified.st_size, verified.st_mtime_ns, verified.st_ctime_ns)
                ):
                    raise OSError(errno.EIO, "Exclusive destination changed")
                if dst_dir_fd is None:
                    repinned, _ = _pin_parent(stack, destination, None)
                    before_parent, after_parent = os.fstat(dest_parent), os.fstat(repinned)
                    if (before_parent.st_dev, before_parent.st_ino) != (
                        after_parent.st_dev,
                        after_parent.st_ino,
                    ):
                        raise OSError(errno.EIO, "Destination parent changed")
        except OSError as exc:
            raise ExclusiveCreateUnconfirmed(
                exc.errno or errno.EIO,
                "Exclusive destination may contain partial data; inspect before retrying",
            ) from exc
        return FilePublication("exclusive_create", True, False, (created.st_dev, created.st_ino))


def _native_rename_no_replace(
    source,
    destination,
    *,
    src_dir_fd: int | None = None,
    dst_dir_fd: int | None = None,
) -> str:
    """Use the OS no-clobber primitive, including pinned-directory support."""
    if sys.platform == "win32":
        # Windows os.rename refuses any existing destination, unlike POSIX.
        if src_dir_fd is not None or dst_dir_fd is not None:
            raise OSError(errno.ENOSYS, "Directory-relative rename is unavailable")
        os.rename(source, destination)
        return "rename_no_replace"

    if sys.platform == "darwin":
        symbol, at_fdcwd, flags = "renameatx_np", -2, 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        symbol, at_fdcwd, flags = "renameat2", -100, 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOSYS, "Native exclusive rename is unavailable")

    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    if b"\0" in source_bytes or b"\0" in destination_bytes:
        raise ValueError("embedded null byte")
    try:
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, symbol)
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "Native exclusive rename is unavailable") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(
        at_fdcwd if src_dir_fd is None else src_dir_fd,
        source_bytes,
        at_fdcwd if dst_dir_fd is None else dst_dir_fd,
        destination_bytes,
        flags,
    )
    if result:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), os.fspath(source), None, os.fspath(destination))
    return "rename_exclusive"


def publish_new_file(
    source,
    destination,
    *,
    src_dir_fd: int | None = None,
    dst_dir_fd: int | None = None,
    follow_symlinks: bool = True,
    allow_exclusive_copy: bool = False,
) -> FilePublication:
    """Publish an already flushed staging file, failing if the target exists.

    The caller owns permissions, staged-content validation, final directory
    sync, and cleanup.  A successful exclusive rename consumes the source name;
    callers must record any staging identity needed for rollback beforehand.
    Explicit allow_exclusive_copy callers accept partial-content visibility and
    must expose the returned atomic flag; the default retains strict semantics.
    Permission, network and I/O errors are never retried with another primitive:
    the original result may be uncertain, so replay could corrupt concurrent
    changes.  No process-local lock is presented as cross-process protection.
    """
    if os.fspath(source) == os.fspath(destination) and src_dir_fd == dst_dir_fd:
        raise ValueError("Staging and destination paths must differ")
    link_kwargs = {}
    if src_dir_fd is not None:
        link_kwargs["src_dir_fd"] = src_dir_fd
    if dst_dir_fd is not None:
        link_kwargs["dst_dir_fd"] = dst_dir_fd
    if not follow_symlinks:
        link_kwargs["follow_symlinks"] = False
    try:
        os.link(source, destination, **link_kwargs)
        return FilePublication("hard_link", True)
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise

    try:
        method = _native_rename_no_replace(
            source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
        )
    except OSError as exc:
        if exc.errno not in _RENAME_UNSUPPORTED:
            raise
        if allow_exclusive_copy:
            return _exclusive_create_copy(
                source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
            )
        raise AtomicCreateUnsupported(
            errno.ENOTSUP,
            "This filesystem supports neither hard links nor exclusive rename; "
            "the destination was not replaced",
            os.fspath(destination),
        ) from exc
    return FilePublication(method, False)
