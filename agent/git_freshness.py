"""Bounded local Git metadata only: no fetch, hooks, project code or writes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time


_REF_TIMEOUT = 3
_OPEN_STATUS_TIMEOUT = 3
_CHANGES_TIMEOUT = 10


def _read(root, args, *, trim=True, timeout=_REF_TIMEOUT):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.untrackedCache=false",
                "--no-optional-locks",
                *args,
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.upper().startswith("GIT_")
                },
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if result.returncode:
            raise ValueError("git metadata unavailable")
        output.seek(0)
        data = output.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("git metadata exceeds budget")
        decoded = data.decode("utf-8", "replace")
        return decoded.strip() if trim else decoded


def _unavailable(reason):
    return {"available": False, "reason": reason, "network_checked": False}


def _root_error(root):
    marker = root / ".git"
    if not marker.exists() or marker.is_symlink():
        return "not_a_git_root"
    try:
        # Unlike status, this does not walk the index or working tree.
        if _read(root, ["rev-parse", "--is-inside-work-tree", "--show-prefix"]) != "true":
            return "not_a_git_root"
    except subprocess.TimeoutExpired:
        return "git_metadata_timeout"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "git_metadata_unavailable"
    return None


def _try_ref(root, args):
    try:
        return _read(root, args) or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def working_changes(root, *, offset, limit):
    root = Path(root)
    if reason := _root_error(root):
        return _unavailable(reason)
    try:
        status = _read(
            root, ["status", "--porcelain=v1", "--untracked-files=normal"],
            trim=False, timeout=_CHANGES_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _unavailable("git_status_timeout")
    except (OSError, subprocess.SubprocessError, ValueError):
        return _unavailable("git_status_unavailable")
    entries = status.splitlines()
    end = offset + limit
    return {
        "available": True,
        "scope": "working_tree",
        "network_checked": False,
        "dirty": len(entries),
        "entries": entries[offset:end],
        "offset": offset,
        "next_offset": end if end < len(entries) else None,
    }


def _partial_freshness(root, result, reason, warning):
    result.update(
        dirty=None,
        upstream=None,
        ahead=None,
        behind=None,
        branch=_try_ref(root, ["symbolic-ref", "--quiet", "--short", "HEAD"]),
        head=_try_ref(root, ["rev-parse", "--verify", "HEAD"]),
        status_reason=reason,
        warning=warning,
    )
    return result


def freshness(root):
    root = Path(root)
    if reason := _root_error(root):
        return _unavailable(reason)
    result = {
        "available": True,
        "branch": None,
        "upstream": None,
        "head": None,
        "ahead": None,
        "behind": None,
        "dirty": None,
        "network_checked": False,
        "last_fetch_age_seconds": None,
    }
    try:
        status = _read(
            root, ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            timeout=_OPEN_STATUS_TIMEOUT,
        )
        dirty = 0
        for line in status.splitlines():
            if line.startswith("# branch.head "):
                result["branch"] = line[len("# branch.head ") :]
            elif line.startswith("# branch.oid "):
                result["head"] = line[len("# branch.oid ") :]
            elif line.startswith("# branch.upstream "):
                result["upstream"] = line[len("# branch.upstream ") :]
            elif line.startswith("# branch.ab "):
                ahead, behind = line.split()[2:4]
                result["ahead"], result["behind"] = int(ahead), -int(behind)
            elif line and not line.startswith("#"):
                dirty += 1
        result["dirty"] = dirty
    except subprocess.TimeoutExpired:
        return _partial_freshness(
            root, result, "git_status_timeout",
            "Git repository detected; reading working-tree changes timed out. Changes are unknown.",
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _partial_freshness(
            root, result, "git_status_unavailable",
            "Git repository detected; working-tree changes could not be read.",
        )

    # Fetch age is optional metadata; it must not invalidate observed status.
    fetch_path = _try_ref(root, ["rev-parse", "--git-path", "FETCH_HEAD"])
    if fetch_path:
        try:
            mtime = (root / fetch_path).stat().st_mtime
            result["last_fetch_age_seconds"] = max(0, int(time.time() - mtime))
        except OSError:
            pass
    if result["behind"]:
        result["warning"] = (
            "Local checkout is behind its cached upstream; prefer a fresh automatic-base worktree. Dirty files were not changed."
        )
    return result
