"""Owner-bound, resumable binary ingress, independent of CLI execution rights.

Transport callers supply authenticated project context, never an owner override.
A publishing intent is committed before atomic no-overwrite publication. An
interrupted intent is deliberately not adopted, even if a target has the same
hash: an operator must resolve that ambiguous publication on the execution node.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import threading
import time

from pathlib import Path

from agent.filesystem import relative_path
from agent.incoming_artifacts import AnchoredDestination
from agent.integration_state import binding

from shared.file_sources import DEFAULT_MAX_IMPORT_BYTES, MAX_IMPORT_BYTES
from shared.util import DevError

CHUNK_BYTES = 256 * 1024
RESERVED_BYTES = 2 * 1024 * 1024 * 1024
OWNER_UPLOADS = 32
NODE_UPLOADS = 256
ACTIVE_SECONDS = 24 * 60 * 60
RECEIPT_SECONDS = 7 * 24 * 60 * 60


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise DevError('INVALID_UPLOAD', '无效上传编号')
    return value


def _relative(value):
    path = relative_path(value, False)
    for part in path.split('/'):
        stem = part.split('.', 1)[0].upper()
        if (part.endswith((' ', '.')) or any(ord(c) < 32 for c in part)
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', stem)):
            raise DevError('INVALID_PATH', '文件路径包含不可移植的保留名称或末尾字符')
    return path


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise DevError('INVALID_UPLOAD', 'SHA-256 必须是 64 位小写十六进制')
    return value


def _recovery():
    return DevError('UPLOAD_RECOVERY_REQUIRED',
                    '上传存储或发布状态无法安全确认；请在原执行节点核查，勿覆盖或重新发布', 409,
                    recovery='inspect_local_upload', state='recovery_required')


def _identity(info):
    return info.st_dev, info.st_ino


class _Spool:
    """Pin the private spool directory and refuse links or unowned file names."""

    def __init__(self, journal):
        self.base = journal.directory
        self.path = self.base / 'incoming-upload'
        self.fds = []
        self.handles = []

    def __enter__(self):
        try:
            if os.name == 'nt':
                current = Path(self.base.anchor)
                self._windows_pin(current)
                for part in self.base.parts[1:]:
                    current /= part
                    self._windows_pin(current)
                self.path.mkdir(mode=0o700, exist_ok=True)
                self._windows_pin(self.path)
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                base_fd = os.open(self.base.anchor, flags)
                self.fds.append(base_fd)
                for part in self.base.parts[1:]:
                    base_fd = os.open(part, flags, dir_fd=base_fd)
                    self.fds.append(base_fd)
                try:
                    os.mkdir('incoming-upload', mode=0o700, dir_fd=base_fd)
                except FileExistsError:
                    pass
                self.fds.append(os.open('incoming-upload', flags, dir_fd=base_fd))
            info = self.path.lstat()
            if not stat.S_ISDIR(info.st_mode) or self.path.is_symlink():
                raise _recovery()
            if os.name != 'nt':
                if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                    raise _recovery()
                if _identity(info) != _identity(os.fstat(self.fds[-1])):
                    raise _recovery()
            self.info = info
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _windows_pin(self, path):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        # No FILE_SHARE_DELETE: directory replacement is blocked until close.
        handle = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02000000 | 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise _recovery()
        self.handles.append(handle)
        if path.is_symlink() or path.is_junction():
            raise _recovery()

    def _check_directory(self):
        if _identity(self.path.lstat()) != _identity(self.info) or self.path.is_symlink():
            raise _recovery()

    def _stat(self, name):
        self._check_directory()
        if self.fds:
            return os.stat(name, dir_fd=self.fds[-1], follow_symlinks=False)
        return (self.path / name).lstat()

    @staticmethod
    def _check_file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _recovery()
        if os.name != 'nt' and (info.st_mode & 0o177 or info.st_uid != os.geteuid()):
            raise _recovery()

    def open(self, row, *, create=False):
        name = _identifier(row['id']) + '.part'
        if row['spool_name'] != name:
            raise _recovery()
        try:
            before = self._stat(name)
        except FileNotFoundError:
            before = None
        if create:
            if before is not None or row['spool_dev'] is not None:
                raise _recovery()
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        else:
            if before is None or row['spool_dev'] is None:
                raise _recovery()
            self._check_file(before)
            if _identity(before) != (row['spool_dev'], row['spool_ino']):
                raise _recovery()
            flags = os.O_RDWR
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        kwargs = {'dir_fd': self.fds[-1]} if self.fds else {}
        path = name if self.fds else self.path / name
        fd = os.open(path, flags, 0o600, **kwargs)
        try:
            info = os.fstat(fd)
            self._check_file(info)
            current = self._stat(name)
            if _identity(current) != _identity(info):
                raise _recovery()
            if not create and _identity(info) != (row['spool_dev'], row['spool_ino']):
                raise _recovery()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def unlink(self, row):
        """Only a recorded regular file with its original identity can be removed."""
        if row['spool_name'] != _identifier(row['id']) + '.part':
            raise _recovery()
        try:
            info = self._stat(row['spool_name'])
        except FileNotFoundError:
            return
        self._check_file(info)
        if row['spool_dev'] is None or _identity(info) != (row['spool_dev'], row['spool_ino']):
            raise _recovery()
        if self.fds:
            os.unlink(row['spool_name'], dir_fd=self.fds[-1])
        else:
            (self.path / row['spool_name']).unlink()

    def sync(self):
        if self.fds:
            os.fsync(self.fds[-1])
            os.fsync(self.fds[-2])

    def __exit__(self, exc_type, exc_value, traceback):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.handles.clear()


class IncomingUploads:
    """All public steps require current write scope and the exact saved binding."""

    _lock = threading.RLock()

    def __init__(self, engine):
        self.engine = engine
        self.journal = engine.journal
        with self._lock, self.journal.lock, self.journal.db:
            self.journal.db.execute("""
                CREATE TABLE IF NOT EXISTS incoming_uploads (
                    id TEXT PRIMARY KEY, binding TEXT NOT NULL, owner TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    received INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, spool_expires REAL NOT NULL,
                    spool_name TEXT, spool_dev INTEGER, spool_ino INTEGER,
                    staging TEXT
                )
            """)
            self.journal.db.execute(
                'CREATE INDEX IF NOT EXISTS incoming_upload_expiry ON incoming_uploads(expires)')

    @contextlib.contextmanager
    def _locked(self):
        # The mutation lock always precedes the journal lock, matching FileEngine.
        with self.engine.mutation_lock, self._lock, self.journal.lock:
            db = self.journal.db
            if db.in_transaction:
                raise DevError('UPLOAD_JOURNAL_BUSY', '上传需独立持久事务，请稍后重试', 409)
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except OSError:
                db.rollback()
                # Agent runner may expose exception text. Never return private
                # spool paths, device paths or platform syscall diagnostics.
                raise DevError('UPLOAD_STORAGE', '上传存储操作未完成；请查询原上传状态后再处理', 500,
                               recovery='read_upload_status') from None
            except BaseException:
                db.rollback()
                raise

    def _authorize(self, project, path=None):
        scopes = project.get('_coding_scopes')
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes) or 'write' not in scopes:
            raise DevError('UPLOAD_FORBIDDEN', '上传需要当前连接的 write 权限', 403)
        bound = binding(project)
        root, _ = self.engine.root(project, True)
        info = root.stat()
        bound = {**bound, 'resolved_root': str(root),
                 'root_dev': info.st_dev, 'root_ino': info.st_ino}
        if path is not None:
            destination = self.engine.path(root, path, False)
            self.engine.check_local_write(destination)
        return root, json.dumps(bound, sort_keys=True), bound['owner']

    def _row(self, db, identifier, project):
        # Validate current authorization before revealing whether an ID exists.
        _, bound, _ = self._authorize(project)
        row = db.execute('SELECT * FROM incoming_uploads WHERE id=?', (_identifier(identifier),)).fetchone()
        if row is None or row['binding'] != bound:
            raise DevError('UPLOAD_NOT_FOUND', '上传不属于当前调用方、项目映射或工作区', 404)
        self._authorize(project, row['path'])
        if row['state'] in {'staging', 'publishing'}:
            raise _recovery()
        if row['expires'] <= time.time():
            raise DevError('UPLOAD_EXPIRED', '上传或回执已过期，请重新开始', 410)
        if row['state'] == 'failed':
            raise DevError('UPLOAD_INTEGRITY', '完整文件校验失败；请使用新上传编号重新开始', 409)
        return row

    @staticmethod
    def _result(row):
        complete = row['state'] == 'complete'
        return {'upload_id': row['id'], 'path': row['path'], 'bytes': row['size'],
                'received': row['received'], 'sha256': row['sha256'],
                'ready': complete, 'created': complete, 'state': row['state'],
                'expires': row['expires']}

    def _materialize(self, db, spool, row):
        if row['spool_dev'] is None:
            if row['received']:
                raise _recovery()
            fd = spool.open(row, create=True)
            try:
                os.fsync(fd)
                spool.sync()
                info = os.fstat(fd)
                db.execute('UPDATE incoming_uploads SET spool_dev=?,spool_ino=? WHERE id=?',
                           (*_identity(info), row['id']))
            finally:
                os.close(fd)
            row = db.execute('SELECT * FROM incoming_uploads WHERE id=?', (row['id'],)).fetchone()
        return row

    @staticmethod
    def _reconcile(fd, row):
        actual = os.fstat(fd).st_size
        if actual < row['received'] or actual > row['size']:
            raise _recovery()
        if actual > row['received']:
            # fsync may precede the durable acknowledgement. Discard only the
            # unacknowledged tail of this identity-checked, registered spool.
            os.ftruncate(fd, row['received'])
            os.fsync(fd)

    def begin(self, identifier, project, args):
        identifier = _identifier(identifier)
        path = _relative(args.get('path'))
        size, sha = args.get('size'), _sha(args.get('sha256'))
        limit = self.engine.config.get('integrations', {}).get('max_import_bytes', DEFAULT_MAX_IMPORT_BYTES)
        if type(limit) is not int or not 0 <= limit <= MAX_IMPORT_BYTES:
            raise DevError('UPLOAD_CONFIGURATION', '本机文件导入大小上限无效')
        if type(size) is not int or not 0 <= size <= limit:
            raise DevError('UPLOAD_TOO_LARGE', '文件大小超过本机导入上限或无效')
        with self._locked() as db:
            root, bound, owner = self._authorize(project, path)
            old = db.execute('SELECT * FROM incoming_uploads WHERE id=?', (identifier,)).fetchone()
            if old is not None:
                row = self._row(db, identifier, project)
                if (row['path'], row['size'], row['sha256']) != (path, size, sha):
                    raise DevError('UPLOAD_CONFLICT', '同一上传编号不能改变路径、大小或 SHA-256', 409)
                if row['state'] == 'complete':
                    return self._result(row)
            else:
                if self.engine.path(root, path, False).exists():
                    raise DevError('ARTIFACT_DESTINATION_EXISTS', '目标文件已存在，未覆盖', 409)
                reserved = db.execute(
                    "SELECT COALESCE(sum(2*size),0) FROM incoming_uploads WHERE spool_name IS NOT NULL OR state IN ('staging','publishing')").fetchone()[0]
                active = db.execute("SELECT count(*) FROM incoming_uploads WHERE state!='complete'").fetchone()[0]
                owned = db.execute(
                    "SELECT count(*) FROM incoming_uploads WHERE state!='complete' AND owner=?", (owner,)).fetchone()[0]
                if reserved + 2 * size > RESERVED_BYTES or active >= NODE_UPLOADS or owned >= OWNER_UPLOADS:
                    raise DevError('UPLOAD_QUOTA', '上传预留空间或活跃上传数量已达上限', 409)
                now = time.time()
                db.execute("""
                    INSERT INTO incoming_uploads
                    (id,binding,owner,path,size,sha256,state,created,expires,spool_expires,spool_name)
                    VALUES (?,?,?,?,?,?,'receiving',?,?,?,?)
                """, (identifier, bound, owner, path, size, sha, now,
                      now + ACTIVE_SECONDS, now + ACTIVE_SECONDS, identifier + '.part'))
                # Reserve identity and capacity durably before touching the spool.
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                row = self._row(db, identifier, project)
            if row['state'] != 'complete':
                with _Spool(self.journal) as spool:
                    row = self._materialize(db, spool, row)
                    fd = spool.open(row)
                    try:
                        self._reconcile(fd, row)
                    finally:
                        os.close(fd)
            return self._result(row)

    def status(self, project, args):
        with self._locked() as db:
            row = self._row(db, args.get('upload_id'), project)
            if row['state'] != 'complete':
                with _Spool(self.journal) as spool:
                    row = self._materialize(db, spool, row)
                    fd = spool.open(row)
                    try:
                        self._reconcile(fd, row)
                    finally:
                        os.close(fd)
            return self._result(row)

    def chunk(self, project, args):
        data, offset = args.get('data'), args.get('offset')
        sha = _sha(args.get('chunk_sha256'))
        if not isinstance(data, bytes) or not 0 < len(data) <= CHUNK_BYTES or type(offset) is not int or offset < 0:
            raise DevError('INVALID_UPLOAD_CHUNK', '文件块必须是至多 256 KiB 的二进制及有效偏移')
        if hashlib.sha256(data).hexdigest() != sha:
            raise DevError('UPLOAD_CHUNK_INTEGRITY', '文件块 SHA-256 不匹配，未接收')
        with self._locked() as db:
            row = self._row(db, args.get('upload_id'), project)
            if row['state'] == 'complete':
                raise DevError('UPLOAD_COMPLETE', '上传已发布，请读取原回执', 409)
            end = offset + len(data)
            if end > row['size'] or offset > row['received'] or offset < row['received'] < end:
                raise DevError('UPLOAD_OFFSET', '偏移存在间隙、部分重叠或超出文件大小；从已确认偏移重试', 409)
            with _Spool(self.journal) as spool:
                row = self._materialize(db, spool, row)
                fd = spool.open(row)
                try:
                    self._reconcile(fd, row)
                    os.lseek(fd, offset, os.SEEK_SET)
                    if offset < row['received']:
                        if os.read(fd, len(data)) != data:
                            raise DevError('UPLOAD_CONFLICT', '重试文件块与已确认内容不一致', 409)
                    else:
                        view = memoryview(data)
                        while view:
                            written = os.write(fd, view)
                            if written <= 0:
                                raise OSError('Short spool write')
                            view = view[written:]
                        os.fsync(fd)
                        db.execute('UPDATE incoming_uploads SET received=? WHERE id=?', (end, row['id']))
                finally:
                    os.close(fd)
            return self._result(db.execute('SELECT * FROM incoming_uploads WHERE id=?', (row['id'],)).fetchone())

    def finish(self, project, args):
        with self._locked() as db:
            row = self._row(db, args.get('upload_id'), project)
            if row['state'] == 'complete':
                self._discard_spool(db, row)
                return self._result(row)
            if row['received'] != row['size']:
                raise DevError('UPLOAD_INCOMPLETE', '文件未完整接收，不能发布', 409)
            root, _, _ = self._authorize(project, row['path'])
            if self.engine.path(root, row['path'], False).exists():
                raise DevError('ARTIFACT_DESTINATION_EXISTS', '目标文件已存在，未覆盖', 409)
            with _Spool(self.journal) as spool:
                row = self._materialize(db, spool, row)
                fd = spool.open(row)
                try:
                    self._reconcile(fd, row)
                    with os.fdopen(fd, 'rb') as source:
                        fd = None
                        actual = hashlib.file_digest(source, 'sha256').hexdigest()
                        if actual != row['sha256']:
                            db.execute("UPDATE incoming_uploads SET state='failed' WHERE id=?", (row['id'],))
                            db.commit()
                            raise DevError('UPLOAD_INTEGRITY', '完整文件 SHA-256 不匹配，未发布', 409)
                        source.seek(0)
                        with AnchoredDestination(self.engine, root, row['path']) as target:
                            # Record the staging inode before copying any large
                            # payload. Restart must not allocate another full
                            # staging copy after an interrupted copy.
                            os.fsync(target.fd)
                            identity = os.fstat(target.fd)
                            staging = json.dumps({'name': target.temporary,
                                                  'dev': identity.st_dev, 'ino': identity.st_ino,
                                                  'bytes': row['size'], 'sha256': row['sha256'],
                                                  'phase': 'copying'}, sort_keys=True)
                            db.execute("UPDATE incoming_uploads SET state='staging',staging=?,expires=? WHERE id=?",
                                       (staging, time.time() + RECEIPT_SECONDS, row['id']))
                            db.commit()
                            db.execute('BEGIN IMMEDIATE')
                            copied, digest = 0, hashlib.sha256()
                            while block := source.read(CHUNK_BYTES):
                                copied += len(block)
                                if copied > row['size']:
                                    raise _recovery()
                                digest.update(block)
                                target.write(block)
                            if copied != row['size'] or digest.hexdigest() != row['sha256']:
                                raise DevError('UPLOAD_INTEGRITY', '复制期间文件内容改变，未发布', 409)
                            if self._authorize(project, row['path'])[1] != row['binding']:
                                raise _recovery()
                            os.fsync(target.fd)
                            identity = os.fstat(target.fd)
                            staging = json.dumps({'name': target.temporary,
                                                  'dev': identity.st_dev, 'ino': identity.st_ino,
                                                  'bytes': copied, 'sha256': row['sha256'],
                                                  'phase': 'publishing'}, sort_keys=True)
                            db.execute("UPDATE incoming_uploads SET state='publishing',staging=?,expires=? WHERE id=?",
                                       (staging, time.time() + RECEIPT_SECONDS, row['id']))
                            db.commit()
                            db.execute('BEGIN IMMEDIATE')
                            # Revalidate again after the durable checkpoint:
                            # fsync/commit can wait while local policy changes.
                            if self._authorize(project, row['path'])[1] != row['binding']:
                                raise _recovery()
                            # Never infer a receipt from a pre-existing destination.
                            target.publish()
                            db.execute("UPDATE incoming_uploads SET state='complete',expires=? WHERE id=?",
                                       (time.time() + RECEIPT_SECONDS, row['id']))
                            db.commit()
                            db.execute('BEGIN IMMEDIATE')
                finally:
                    if fd is not None:
                        os.close(fd)
            row = db.execute('SELECT * FROM incoming_uploads WHERE id=?', (row['id'],)).fetchone()
            self._discard_spool(db, row)
            return self._result(row)

    def _discard_spool(self, db, row):
        if row['spool_name'] is None:
            return
        # Publication already has a durable receipt. A cleanup failure preserves
        # its reserved capacity and evidence; it never changes that receipt.
        try:
            with _Spool(self.journal) as spool:
                spool.unlink(row)
                spool.sync()
        except (OSError, DevError):
            return
        db.execute('UPDATE incoming_uploads SET spool_name=NULL WHERE id=?', (row['id'],))

    def cleanup(self):
        """Internal maintenance: expire only owned spool identities and receipts.

        No project path is opened, removed or changed here. Unknown/orphaned files
        and link attacks are retained for local recovery, never glob-deleted.
        """
        removed = receipts = recovery = 0
        with self._locked() as db:
            now = time.time()
            rows = db.execute(
                'SELECT * FROM incoming_uploads WHERE spool_expires<=? OR expires<=?', (now, now)).fetchall()
            for row in rows:
                if row['spool_name'] is not None and row['spool_expires'] <= now:
                    try:
                        with _Spool(self.journal) as spool:
                            spool.unlink(row)
                            spool.sync()
                    except (OSError, DevError):
                        recovery += 1
                        continue
                    db.execute('UPDATE incoming_uploads SET spool_name=NULL WHERE id=?', (row['id'],))
                    removed += 1
                # Unresolved project staging/publication is never erased or
                # released from quota by a timer. Only local recovery can
                # reconcile it; cleanup never touches project files.
                if row['expires'] <= now and row['state'] not in {'staging', 'publishing'}:
                    db.execute('DELETE FROM incoming_uploads WHERE id=?', (row['id'],))
                    receipts += row['state'] == 'complete'
            return {'expired_uploads': removed, 'expired_receipts': receipts,
                    'recovery_required': recovery}
