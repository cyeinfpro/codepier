"""Private native-terminal spool. No terminal content belongs in operation/audit logs."""
from __future__ import annotations
import json
import os
import re
import sqlite3
from pathlib import Path

LIVE = {'starting', 'running', 'stopping', 'orphaned'}
SESSION_QUOTA = 128 * 1024 * 1024
TOTAL_QUOTA = 1024 * 1024 * 1024
ATTACHMENT_CAP = 20 * 1024 * 1024


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Invalid native session/receipt identifier')
    return value


def database(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError('Native spool cannot be a symlink')
    os.chmod(directory, 0o700)
    path = directory / 'native.sqlite3'
    if path.is_symlink():
        raise ValueError('Native database cannot be a symlink')
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
        db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA secure_delete=ON')
    if db.execute('PRAGMA user_version').fetchone()[0] >= 3:
        os.chmod(path, 0o600)
        return db
    db.executescript('''
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL,
      root TEXT NOT NULL, cwd TEXT NOT NULL, provider TEXT NOT NULL, title TEXT NOT NULL,
      status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
      size INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, error TEXT NOT NULL DEFAULT '',
      heartbeat REAL NOT NULL DEFAULT 0, lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
      argv TEXT NOT NULL DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS output (session TEXT NOT NULL, offset INTEGER NOT NULL, data BLOB NOT NULL,
      PRIMARY KEY(session,offset));
    CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, root TEXT NOT NULL,
      name TEXT NOT NULL, size INTEGER NOT NULL, sha TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0,
      ready INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL);
    ''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(sessions)')}
    for name, declaration in {
        'worker_pid': 'INTEGER NOT NULL DEFAULT 0',
        'child_pid': 'INTEGER NOT NULL DEFAULT 0',
        'launch_signature': "TEXT NOT NULL DEFAULT ''",
        'mode': "TEXT NOT NULL DEFAULT 'terminal'",
        'native_thread': "TEXT NOT NULL DEFAULT ''",
        'chat_settings': "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in columns:
            db.execute('ALTER TABLE sessions ADD COLUMN ' + name + ' ' + declaration)
    db.execute('CREATE TABLE IF NOT EXISTS session_files (session TEXT, file TEXT, PRIMARY KEY(session,file))')
    db.execute('CREATE INDEX IF NOT EXISTS native_commands_pending ON commands(session,state)')
    db.execute('CREATE INDEX IF NOT EXISTS native_attachments_project ON attachments(project_id)')
    db.execute('PRAGMA user_version=3')
    db.commit()
    os.chmod(path, 0o600)
    return db


def process_exists(pid):
    """Conservative observation only; never signal a potentially reused PID."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) is NOT a liveness query on Windows; non-console
        # signals map to TerminateProcess there. Observe a process HANDLE.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Unknown/access denied stays busy.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WorkerLock:
    """Cross-process ownership of one session, independent of Agent transport."""
    def __init__(self, directory, sid):
        folder = Path(directory) / 'locks'
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise ValueError('Unsafe worker lock directory')
        self.fd = os.open(folder / (identifier(sid) + '.lock'),
                          os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                # A one-byte CRT lock is released when the fd/process closes.
                # Locking is nonblocking, so a stopped worker never blocks Hub IO.
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def worker_present(directory, sid):
    try:
        with WorkerLock(directory, sid):
            return False
    except (BlockingIOError, PermissionError, OSError):
        # An unreadable/contended lock is not evidence that the worker is dead.
        return True


def retained_bytes(db):
    """Logical retained bytes, not SQLite's reusable free pages or WAL copies."""
    output = db.execute('SELECT COALESCE(sum(length(data)),0) FROM output').fetchone()[0]
    commands = db.execute('SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) FROM commands').fetchone()[0]
    uploads = db.execute('SELECT COALESCE(sum(size),0) FROM attachments').fetchone()[0]
    return output + commands + uploads


def public(row):
    result = {k: row[k] for k in ('id','project_id','device_id','root','cwd','provider','title','status',
                               'created','updated','size','exit_code','error')}
    for key, default in (('mode','terminal'),('native_thread',''),('chat_settings','{}')):
        result[key] = row[key] if key in row.keys() else default
    return result


def launch_argv(executable, provider, options, attachment_paths=()):
    """argv only. Preserve native options; forbid cwd overrides and implicit modes."""
    argv = [executable]
    if options.get('resume'):
        argv += ['resume'] if provider == 'codex' else ['--resume']
    for key in ('model', 'provider', 'effort'):
        value = options.get(key, '')
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid model/provider/effort')
        if not value:
            continue
        if key == 'model':
            argv += ['--model', value]
        elif key == 'provider':
            argv += (['-c', 'model_provider=' + json.dumps(value)] if provider == 'codex' else ['--provider', value])
        else:
            allowed = {'minimal','low','medium','high','xhigh'} | ({'off','max'} if provider == 'pi' else {'none'})
            if value not in allowed:
                raise ValueError('Unsupported thinking level; use native selector')
            argv += (['-c', 'model_reasoning_effort=' + json.dumps(value)] if provider == 'codex' else ['--thinking', value])
    extra = options.get('argv', [])
    if (not isinstance(extra, list) or len(extra) > 80 or any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in extra)):
        raise ValueError('Advanced arguments must be a JSON argv array')
    # Full flags remain available, including extension-defined flags. cwd is always mapping-derived.
    for a in extra:
        if a in {'-C', '--cd', '--cwd'} or a.startswith(('--cd=', '--cwd=', '-C')):
            raise ValueError('Working directory is controlled by project mapping')
    argv += extra
    if provider == 'codex':
        for path in attachment_paths:
            argv += ['--image', str(path)]
    else:
        argv += ['@' + str(path) for path in attachment_paths]
    return argv
