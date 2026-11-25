"""Bounded metadata-assisted refresh. This module never schedules work.

Only explicit write calls reach it. Metadata is an optimization, not freshness
proof: query still hashes selected sources and full builds run every 15 minutes.
Caches are process-local and bounded; restart deliberately forces a full read.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections import OrderedDict

from agent import project_knowledge as knowledge
from shared.util import DevError

FULL_INTERVAL = 900
MAX_CACHES = 32
MAX_FILE_SIGNATURES = 16384
_LOCK = threading.RLock()
_CACHES = OrderedDict()
_BUILDS = OrderedDict()


def signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
    )


def checked_signature(engine, root, path):
    actual = engine.path(root, path, False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(actual, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DevError("KNOWLEDGE_UNSAFE_SOURCE", "缓存源不是单链接普通文件", 403)
        if info.st_size > knowledge.FILE_BYTES:
            raise DevError("FILE_TOO_LARGE", "缓存源超过文件预算", 409)
        # Rewalk every component after opening; cached descriptors confer no access.
        engine.path(root, path, False)
        current = actual.lstat()

        # Windows stat/lstat can expose creation time as ctime while fstat
        # exposes change time. Compare shared object attributes across APIs,
        # then compare the complete descriptor signature to detect real edits.
        def object_signature(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_mode,
                value.st_nlink,
            )

        latest = os.fstat(fd)
        if object_signature(current) != object_signature(info) or signature(latest) != signature(
            info
        ):
            raise DevError("FILE_CHANGED", "缓存源在核验期间变化", 409)
        return signature(info)
    finally:
        os.close(fd)


class Reuse:
    def __init__(self, previous=None):
        self.previous = previous or {}
        self.current = {}
        self.hits = 0

    def before(self, engine, root, path, prior):
        info = checked_signature(engine, root, path)
        old = self.previous.get(path)
        version = getattr(knowledge, "STRUCTURE_VERSION", 1)
        if (
            prior
            and old
            and old["signature"] == info
            and old["sha256"] == prior["sha256"]
            and old["structure_version"] == version
        ):
            self.current[path] = old
            self.hits += 1
            return info, dict(prior)
        return info, None

    def after(self, engine, root, path, before, sha):
        if checked_signature(engine, root, path) == before:
            self.current[path] = {
                "signature": before,
                "sha256": sha,
                "structure_version": getattr(knowledge, "STRUCTURE_VERSION", 1),
            }


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > MAX_CACHES:
        cache.popitem(last=False)


def _published_generation(engine, key, result):
    # Validate a ready result against its actual publication, including an
    # identical-revision replacement by a different writer.
    row = engine.journal.db.execute(
        "SELECT revision,updated FROM project_knowledge WHERE map_key=?", (key,)
    ).fetchone()
    return row is not None and (row[0], row[1]) == (result["revision"], result["generated_at"])


def _building_generation(engine, key, cursor):
    row = engine.journal.db.execute(
        "SELECT cursor FROM knowledge_build_progress WHERE map_key=?", (key,)
    ).fetchone()
    return row is not None and row[0] == cursor


def build(engine, project, args):
    _, _, key = knowledge.identity(engine, project, True)
    cache_key = (str(engine.journal.directory), key)
    cursor = args.get("cursor", "")
    now = time.monotonic()
    saved = knowledge.load(engine, key, repair=True)
    base = saved["revision"] if saved else "new"
    if args.get("incremental") and saved is None and not cursor:
        raise DevError("KNOWLEDGE_MISSING", "自动刷新需要原目录中显式生成的地图；请重新生成", 409)
    with _LOCK:
        cached = _CACHES.get(cache_key)
        pending = _BUILDS.get(cache_key) if cursor else None
        if pending is not None and (pending["cursor"], pending["base"]) != (cursor, base):
            pending = None
    if pending is not None:
        full = pending["full"]
        full_at = pending["full_at"]
        previous = pending["previous"]
        accumulated = pending["files"]
        hits = pending["hits"]
        complete = pending["complete"]
    else:
        full = bool(
            cursor
            or not args.get("incremental")
            or not cached
            or cached["revision"] != base
            or now - cached["full_at"] >= FULL_INTERVAL
        )
        full_at = now if full else cached["full_at"]
        previous = None if full else cached["files"]
        accumulated = {}
        hits = 0
        # A restarted/evicted process can still use the durable progress, but
        # missing historic signatures never turn into a partial published cache.
        complete = not cursor

    reuse = Reuse(previous)
    result = knowledge.build(engine, project, args, reuse=reuse)
    # Do not hold either lock during the scan. Only the writer that still owns
    # the durable cursor/publication can install or retire in-memory state.
    next_files = {**accumulated, **reuse.current} if complete else {}
    complete = complete and len(next_files) <= MAX_FILE_SIGNATURES
    if not complete:
        next_files = {}
    hits += reuse.hits
    with engine.journal.lock, _LOCK:
        if result["state"] == "building":
            if _building_generation(engine, key, result["cursor"]):
                _remember(
                    _BUILDS,
                    cache_key,
                    {
                        "cursor": result["cursor"],
                        "base": result["revision"],
                        "full": full,
                        "full_at": full_at,
                        "previous": previous,
                        "files": next_files,
                        "hits": hits,
                        "complete": complete,
                    },
                )
            return result
        if _published_generation(engine, key, result):
            # A newer first batch may now own the current progress, even if
            # the completed map's revision was unchanged.
            state = _BUILDS.get(cache_key)
            if state is not None and state["cursor"] == cursor:
                _BUILDS.pop(cache_key, None)
            if complete:
                _remember(
                    _CACHES,
                    cache_key,
                    {"files": next_files, "revision": result["revision"], "full_at": full_at},
                )
            else:
                _CACHES.pop(cache_key, None)
    result["refresh"] = {
        "mode": "full" if full else "incremental",
        "metadata_reused": hits,
        "full_interval_seconds": FULL_INTERVAL,
        "freshness": "Metadata reuse is not source-SHA verification.",
    }
    return result
