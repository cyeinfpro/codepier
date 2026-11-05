"""Per-operation output directories; never reap a directory still in use."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from shared.util import DevError

_MARKER = ".codepier-run.json"


def operation_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise DevError("INVALID_ARGUMENT", "Invalid operation ID")
    return value


def safe_directory(path: Path, *, create: bool = False) -> Path:
    """Reject symlinks in every component, including dangling links."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise DevError("UNSAFE_PATH", "Directory contains a symbolic link")
        if create:
            component.mkdir(exist_ok=True)
        if not component.is_dir():
            raise DevError("UNSAFE_PATH", "Expected a directory")
    return path


def _exclude_runs(root: Path) -> None:
    if not (root / ".git").exists():
        return
    # Git resolves the common metadata directory for registered worktrees.
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise DevError("RUN_DIRECTORY_FAILED", "Cannot locate Git exclude file")
    exclude = Path(result.stdout.strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    safe_directory(exclude.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(exclude, flags, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        text = stream.read()
        if "/.codepier/runs/" not in text.splitlines():
            stream.write(("" if not text or text.endswith("\n") else "\n") + "/.codepier/runs/\n")


def prepare_run_directory(root: Path, operation_id: str) -> Path:
    root = safe_directory(root)
    operation_name(operation_id)
    _exclude_runs(root)
    parent = safe_directory(root / ".codepier" / "runs", create=True)
    path = parent / operation_id
    # An existing directory might belong to another or recovered process.
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DevError("RUN_DIRECTORY_EXISTS", "Operation output directory already exists", 409) from exc
    (path / _MARKER).write_text(json.dumps({"created": time.time(), "finished": None}), encoding="utf-8")
    return path


def run_environment(path: Path) -> dict[str, str]:
    return {"CODEPIER_RUN_DIR": str(safe_directory(path))}


def finish_run_directory(path: Path) -> None:
    path = safe_directory(path)
    marker = path / _MARKER
    if marker.is_symlink():
        raise DevError("UNSAFE_PATH", "Invalid run directory marker")
    fd = os.open(marker, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"finished": time.time()}, stream)


def cleanup_run_directories(root: Path, ttl_seconds: float = 7 * 86400, *, now: float | None = None) -> list[str]:
    if ttl_seconds < 0:
        raise ValueError("TTL must be nonnegative")
    parent = Path(root) / ".codepier" / "runs"
    if not parent.exists():
        return []
    safe_directory(parent)
    removed = []
    cutoff = (time.time() if now is None else now) - ttl_seconds
    for path in parent.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        marker = path / _MARKER
        if marker.is_symlink():
            continue
        try:
            finished = json.loads(marker.read_text(encoding="utf-8")).get("finished")
            if not isinstance(finished, (int, float)) or finished > cutoff:
                continue
        except (OSError, ValueError, AttributeError):
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            # Another cleanup may have removed this expired entry. Only
            # suppress proven absence, not child I/O failures or replacement
            # symlinks (lstat deliberately does not follow them).
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise
        removed.append(path.name)
    return removed
