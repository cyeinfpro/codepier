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
_LOCK = threading.RLock()
_CACHES = OrderedDict()


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def checked_signature(engine, root, path):
    actual = engine.path(root, path, False)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(actual, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DevError('KNOWLEDGE_UNSAFE_SOURCE', '缓存源不是单链接普通文件', 403)
        if info.st_size > knowledge.FILE_BYTES:
            raise DevError('FILE_TOO_LARGE', '缓存源超过文件预算', 409)
        # Rewalk every component after opening; cached descriptors confer no access.
        engine.path(root, path, False)
        current = actual.lstat()
        if signature(current) != signature(info):
            raise DevError('FILE_CHANGED', '缓存源在核验期间变化', 409)
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
        version = getattr(knowledge, 'STRUCTURE_VERSION', 1)
        if (prior and old and old['signature'] == info and old['sha256'] == prior['sha256']
                and old['structure_version'] == version):
            self.current[path] = old
            self.hits += 1
            return info, dict(prior)
        return info, None

    def after(self, engine, root, path, before, sha):
        if checked_signature(engine, root, path) == before:
            self.current[path] = {'signature': before, 'sha256': sha,
                                  'structure_version': getattr(knowledge, 'STRUCTURE_VERSION', 1)}


def build(engine, project, args):
    _, _, key = knowledge.identity(engine, project, True)
    cache_key = (str(engine.journal.directory), key)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHES.get(cache_key)
    saved = knowledge.load(engine, key, repair=True)
    if args.get('incremental') and saved is None:
        raise DevError('KNOWLEDGE_MISSING', '自动刷新需要原目录中显式生成的地图；请重新生成', 409)
    full = (not args.get('incremental') or not cached or not saved
            or cached['revision'] != saved['revision']
            or now - cached['full_at'] >= FULL_INTERVAL)
    reuse = Reuse(None if full else cached['files'])
    result = knowledge.build(engine, project, args, reuse=reuse)
    # Cache is only published following successful map CAS. Errors keep old state.
    with _LOCK:
        _CACHES[cache_key] = {'files': reuse.current, 'revision': result['revision'],
                            'full_at': now if full else cached['full_at']}
        _CACHES.move_to_end(cache_key)
        while len(_CACHES) > MAX_CACHES:
            _CACHES.popitem(last=False)
    result['refresh'] = {'mode': 'full' if full else 'incremental',
                         'metadata_reused': reuse.hits, 'full_interval_seconds': FULL_INTERVAL,
                         'freshness': 'Metadata reuse is not source-SHA verification.'}
    return result
