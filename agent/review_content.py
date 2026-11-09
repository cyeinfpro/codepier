"""Private content-addressed review text. Callers hold the review-store lock.

Manifests keep independent bindings and leases. Blobs grant no read authority.
Publication is blobs -> manifest -> garbage collection; a crash can leave an
unreferenced blob, never a published manifest referencing an unwritten blob.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import zlib

from shared.util import DevError, fsync_directory

THRESHOLD = 4096
MAX_TEXT = 32 * 1024 * 1024
MARKER = '_review_text_blob'


def private_bytes(path, limit):
    if path.is_symlink():
        raise DevError('REVIEW_STORAGE_INVALID', '审阅内容存储存在符号链接', 403)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise DevError('REVIEW_STORAGE_INVALID', '审阅内容存储无效', 409)
        return source.read(limit + 1)


def blob_path(directory, digest):
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise DevError('REVIEW_INTEGRITY', '审阅内容引用无效', 409)
    # Flat private directory: no additional parent component can be a symlink.
    return directory / (digest + '.zlib')


def encode(document):
    blobs = {}

    def visit(value, key=None):
        if key == 'text' and isinstance(value, str) and len(value) >= THRESHOLD:
            raw = value.encode('utf-8')
            digest = hashlib.sha256(raw).hexdigest()
            compressed = zlib.compress(raw, 6)
            if len(compressed) + 128 < len(raw):
                blobs[digest] = compressed
                return {MARKER: digest}
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value

    return visit(document), blobs


def references(value):
    if isinstance(value, dict):
        if set(value) == {MARKER}:
            digest = value[MARKER]
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                raise DevError('REVIEW_INTEGRITY', '审阅内容引用无效', 409)
            return {digest}
        return set().union(*(references(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(references(v) for v in value)) if value else set()
    return set()


def decode(document, directory):
    remaining = MAX_TEXT

    def visit(value):
        nonlocal remaining
        if isinstance(value, dict):
            if set(value) == {MARKER}:
                digest = value[MARKER]
                try:
                    packed = private_bytes(blob_path(directory, digest), MAX_TEXT)
                    stream = zlib.decompressobj()
                    raw = stream.decompress(packed, remaining + 1)
                    if len(raw) > remaining or not stream.eof or stream.unused_data or stream.unconsumed_tail:
                        raise ValueError('Invalid compressed content')
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError('Content digest mismatch')
                    remaining -= len(raw)
                    return raw.decode('utf-8')
                except (OSError, ValueError, zlib.error) as exc:
                    raise DevError('REVIEW_INTEGRITY', '审阅内容缺失或校验失败', 409) from exc
            return {k: visit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value

    return visit(document)


def publish(directory, blobs):
    for digest, content in blobs.items():
        path = blob_path(directory, digest)
        if path.exists() or path.is_symlink():
            # Never silently trust or overwrite damaged/linked existing content.
            decode({MARKER: digest}, directory)
            continue
        fd, temporary = tempfile.mkstemp(prefix='.review-blob-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            fsync_directory(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def sizes(directory, refs):
    result = {}
    for digest in refs:
        path = blob_path(directory, digest)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_TEXT:
            raise DevError('REVIEW_STORAGE_INVALID', '审阅内容存储无效', 409)
        result[digest] = info.st_size
    return result


def collect(directory, live):
    removed = False
    for path in list(directory.glob('*.zlib')) + list(directory.glob('.review-blob-*')):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DevError('REVIEW_STORAGE_INVALID', '审阅内容存储无效', 409)
        if path.name.startswith('.review-blob-') or re.fullmatch('[a-f0-9]{64}', path.stem) and path.stem not in live:
            path.unlink()
            removed = True
    if removed:
        fsync_directory(directory)
