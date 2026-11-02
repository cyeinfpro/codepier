"""Publish a complete staged file without replacing an existing destination.

Some SMB servers reject hard links while supporting exclusive rename.  The
fallback must be a native *no-replace* operation, never a check followed by
``os.replace`` or a streaming copy into the final name.  Filesystem capabilities
are tested by the actual operation, rather than cached by path/mount name.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
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
) -> FilePublication:
    """Publish an already flushed staging file, failing if the target exists.

    The caller owns permissions, staged-content validation, final directory
    sync, and cleanup.  A successful exclusive rename consumes the source name;
    callers must record any staging identity needed for rollback beforehand.
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
        raise AtomicCreateUnsupported(
            errno.ENOTSUP,
            "This filesystem supports neither hard links nor exclusive rename; "
            "the destination was not replaced",
            os.fspath(destination),
        ) from exc
    return FilePublication(method, False)
