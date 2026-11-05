"""Bounded, streaming file operations for non-text project assets.

The same path, hardlink, local-write and SHA guards as the text engine apply.
Backups are durable before mutation; uncertain publication is never replayable.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
import uuid

from shared.util import DevError, fsync_directory

MAX_BLOB = 512 * 1024 * 1024


def snapshot(path, sink=None):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DevError('NOT_FILE', '只能读取普通文件')
        if before.st_nlink != 1:
            raise DevError('HARDLINK_BLOCKED', '不读写有多个硬链接的文件', 403)
        if before.st_size > MAX_BLOB:
            raise DevError('FILE_TOO_LARGE', '二进制文件操作上限为 512 MiB')
        sha, size = hashlib.sha256(), 0
        while True:
            block = os.read(fd, min(65536, MAX_BLOB + 1 - size))
            if not block:
                break
            size += len(block)
            if size > MAX_BLOB:
                raise DevError('FILE_TOO_LARGE', '二进制文件操作上限为 512 MiB')
            sha.update(block)
            if sink is not None:
                sink.write(block)
        after, current = os.fstat(fd), path.lstat()
        identity = (before.st_dev, before.st_ino)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or identity != (current.st_dev, current.st_ino)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                   (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise DevError('FILE_CHANGED', '文件在流式读取期间改变', 409)
        return {'sha256': sha.hexdigest(), 'bytes': size,
                'mode': stat.S_IMODE(before.st_mode), 'identity': identity}
    finally:
        os.close(fd)


def backup(engine, root, path, expected):
    identifier = uuid.uuid4().hex
    target = engine.journal.backups / identifier
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    recording = False
    try:
        with os.fdopen(fd, 'wb') as output:
            info = snapshot(path, output)
            if info['sha256'] != expected:
                raise DevError('SHA_CONFLICT', '源文件已改变，未修改', 409)
            output.flush()
            os.fsync(output.fileno())
        fsync_directory(target.parent)
        recording = True
        with engine.journal.lock, engine.journal.db:
            engine.journal.db.execute(
                'INSERT INTO backups(id,root,path,existed,before_sha,after_sha,at,before_mode) VALUES(?,?,?,?,?,?,?,?)',
                (identifier, str(root), path.relative_to(root).as_posix(), 1,
                 expected, 'new', time.time(), info['mode']))
    except BaseException:
        # A failed commit can be uncertain. Never remove bytes after attempting
        # to persist a receipt that may already refer to this backup.
        if not recording:
            target.unlink(missing_ok=True)
        raise
    return identifier, target, info


def change(engine, project, path, expected, *, destination=None, restore_source=None,
           restore_sha=None, restore_mode=None):
    root, _ = engine.root(project, True)
    source = engine.path(root, path, False)
    engine.check_local_write(source)
    try:
        initial = snapshot(source)
    except FileNotFoundError:
        initial = None
    actual = initial['sha256'] if initial else 'new'
    if actual != expected:
        raise DevError('SHA_CONFLICT', f'文件已改变：当前 SHA 为 {actual}', 409)
    if destination is not None and initial is None:
        raise DevError('NOT_FOUND', '源文件不存在', 404)
    target = engine.path(root, destination, False) if destination is not None else source
    engine.check_local_write(target)
    if not target.parent.is_dir():
        raise DevError('PARENT_MISSING', '目标父目录不存在')
    if destination is not None and target.exists():
        raise DevError('DESTINATION_EXISTS', '目标文件已存在，未覆盖', 409)
    if initial is None and restore_source is None:
        return {'path': path, 'sha256': 'new', 'unchanged': True, 'backup_id': None}
    if initial:
        backup_id, backup_path, initial = backup(engine, root, source, expected)
    else:
        backup_id, backup_path = uuid.uuid4().hex, None
        engine.journal.add_backup(backup_id, str(root), path, None, restore_sha)
    content_source = restore_source if restore_source is not None else backup_path if destination is not None else None
    after_sha = restore_sha if restore_source is not None else expected if destination is not None else 'new'
    # Restore backups describe the exact post-state, just like text writes.
    if restore_source is not None:
        with engine.journal.lock, engine.journal.db:
            engine.journal.db.execute('UPDATE backups SET after_sha=? WHERE id=?', (after_sha, backup_id))
    recovery = dict(path=path, backup_id=backup_id, expected_sha256=after_sha,
                    mutation_started=True, retryable=False)
    if destination is not None:
        recovery['destination'] = destination
    temporary, mutated, publication = None, False, None
    try:
        if content_source is not None:
            fd, temporary = tempfile.mkstemp(prefix='.rd-', dir=target.parent)
            with os.fdopen(fd, 'wb') as output:
                copied = snapshot(content_source, output)
                if copied['sha256'] != after_sha:
                    raise DevError('BACKUP_CORRUPT', '备份校验失败，未修改原文件')
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, restore_mode if restore_mode is not None else initial['mode'] if initial else 0o644)
        current_root, _ = engine.root(project, True)
        if current_root != root:
            raise DevError('ROOT_CHANGED', '项目映射在备份期间改变，未修改', 409)
        engine.path(root, path, False)
        engine.path(root, target.relative_to(root).as_posix(), False)
        engine.check_local_write(source)
        engine.check_local_write(target)
        try:
            latest = snapshot(source)
        except FileNotFoundError:
            latest = None
        if (latest['sha256'] if latest else 'new') != expected or (
                initial and latest and latest['identity'] != initial['identity']):
            raise DevError('SHA_CONFLICT', '源文件在操作期间改变，未修改', 409)
        if content_source is None:
            mutated = True
            source.unlink()
        else:
            if destination is not None or initial is None:
                publication = engine.publish_new_file(temporary, target)
            else:
                mutated = True
                os.replace(temporary, target)
            mutated = True
            # link-based no-replace publication temporarily has two links, both
            # owned here. Drop only our staging name before normal hardlink guards.
            if publication is not None and publication.source_retained:
                engine.cleanup_staging(temporary, **recovery)
                temporary = None
            engine.sync_directory(target.parent, **recovery)
            verified = snapshot(target)
            if verified['sha256'] != after_sha:
                raise DevError('FILE_CHANGED', '发布后文件校验失败', 409)
            if destination is not None:
                if not publication.atomic:
                    raise DevError('WRITE_OUTCOME_UNCONFIRMED', '兼容复制已完成；源文件保留，请核查后继续',
                                   409, source_retained=True, **recovery)
                engine.path(root, path, False)
                latest = snapshot(source)
                if latest['sha256'] != expected or latest['identity'] != initial['identity']:
                    raise DevError('FILE_CHANGED', '移动期间源文件改变，源文件保留', 409)
                source.unlink()
        engine.sync_directory(source.parent, **recovery)
        if content_source is None and source.exists():
            raise DevError('FILE_CHANGED', '删除后同名文件重新出现', 409)
    except (OSError, DevError) as exc:
        if mutated or isinstance(exc, DevError) and exc.code == 'WRITE_OUTCOME_UNCONFIRMED':
            raise DevError('WRITE_OUTCOME_UNCONFIRMED', '文件变更结果未确认；请核查文件、备份及原操作，勿直接重试',
                           500, publication='unconfirmed', **recovery) from exc
        raise
    finally:
        if temporary is not None:
            engine.cleanup_staging(temporary, **recovery)
    return {'path': path, **({'destination': destination} if destination is not None else {}),
            'sha256': after_sha, 'backup_id': backup_id, 'content_verified': True,
            'publication_method': publication.method if publication else 'replace' if content_source else 'delete',
            'atomic': publication.atomic if publication else True,
            'partial_content_visible': not publication.atomic if publication else False,
            'diff': '', 'diff_truncated': True, 'binary_or_oversize': True}


def restore(engine, project, args):
    root, _ = engine.root(project, True)
    with engine.journal.lock:
        row = engine.journal.db.execute('SELECT * FROM backups WHERE root=? AND id=?',
                                       (str(root), args['backup_id'])).fetchone()
    if row is None:
        raise DevError('NOT_FOUND', '该项目没有此备份，或备份已超过保留期', 404)
    row = dict(row)
    if not row['existed']:
        return engine.mutate(project, row['path'], args['expected_sha256'], None)
    source = engine.journal.backups / row['id']
    try:
        info = snapshot(source)
    except FileNotFoundError as exc:
        raise DevError('BACKUP_EXPIRED', '备份已过期或被移除；未改动项目文件', 410) from exc
    if info['sha256'] != row['before_sha']:
        raise DevError('BACKUP_CORRUPT', '备份校验失败，未修改原文件')
    if info['bytes'] <= 1024 * 1024:
        _, data = engine.journal.backup(str(root), row['id'])
        try:
            engine.text(data)
        except DevError as exc:
            if exc.code not in {'BINARY_FILE', 'ENCODING'}:
                raise
        else:
            return engine.mutate(project, row['path'], args['expected_sha256'], data,
                                 restore_mode=row.get('before_mode'))
    with engine.mutation_lock:
        return change(engine, project, row['path'], args['expected_sha256'],
                      restore_source=source, restore_sha=row['before_sha'],
                      restore_mode=row.get('before_mode'))
