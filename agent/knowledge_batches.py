"""Durable build progress, separate from the last published project map."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time

from shared.crypto import digest
from shared.util import DevError


def options(args):
    return {name: args.get(name, default) for name, default in (
        ('paths', []), ('exclude_paths', []), ('max_files', 0), ('entries', []))}


def excluded(path, excludes):
    from agent.filesystem import protected, relative_path, scan_ignored
    from agent.project_knowledge import NOISE
    try:
        relative_path(path, False)
    except DevError:
        return True
    if len(path) > 1024:
        return True
    parts = path.split('/')
    return (protected(path) or scan_ignored(path)
            or any(part.casefold() in NOISE for part in parts)
            or any(path == value or path.startswith(value + '/') for value in excludes))


def start(engine, root, args, base):
    """Get Git names before any source stat; generated trees never consume I/O."""
    paths = args.get('paths') or ['.']
    paths = [p for p in paths if not any(p != other and p.startswith(other + '/') for other in paths)]
    for path in paths:
        engine.path(root, path, path == '.')
    pending, scope = list(reversed(paths)), 'filesystem_walk'
    if (root / '.git').exists():
        environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        environment['GIT_TERMINAL_PROMPT'] = '0'
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(['git', '--no-optional-locks', '--literal-pathspecs',
                    '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + str(engine.journal.directory / 'no-hooks'),
                    'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', *paths],
                    cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.DEVNULL, timeout=10, check=False)
                output.seek(0)
                raw = output.read(32 * 1024 * 1024 + 1)
            if result.returncode == 0 and len(raw) <= 32 * 1024 * 1024:
                from agent.project_knowledge import eligible
                pending = sorted({name.decode('utf-8') for name in raw.split(b'\0') if name}, reverse=True)
                pending = [p for p in pending if eligible(p) and not excluded(p, args.get('exclude_paths', []))]
                scope = 'git_index'
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    return {'base': base, 'options': options(args), 'pending': pending, 'entries': {},
            'skipped': {}, 'changed': 0, 'reused': 0, 'scan_scope': scope}


def load(engine, key, cursor, args, base):
    with engine.journal.lock:
        db = engine.journal.db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_build_progress'").fetchone():
            row = None
        else:
            row = db.execute('SELECT cursor,payload FROM knowledge_build_progress WHERE map_key=?', (key,)).fetchone()
    if not row or row[0] != cursor:
        raise DevError('KNOWLEDGE_CURSOR_CONFLICT', '构建进度已变化或不存在；请使用最近一批的续建参数', 409)
    try:
        state = json.loads(row[1])
        if (not isinstance(state, dict) or not isinstance(state.get('pending'), list)
                or not isinstance(state.get('entries'), dict) or not isinstance(state.get('skipped'), dict)
                or not isinstance(state.get('options'), dict) or not isinstance(state.get('base'), str)):
            raise ValueError('invalid progress')
    except (ValueError, TypeError) as error:
        raise DevError('KNOWLEDGE_PROGRESS_CORRUPT', '构建进度无法读取；请重新开始构建，原地图仍保留', 409) from error
    if state['base'] != base or state['options'] != options(args):
        raise DevError('KNOWLEDGE_CONFLICT', '原地图或扫描范围已变化；未覆盖地图，请重新开始构建', 409)
    return state


def save(engine, key, state, expected_cursor):
    from agent.project_knowledge import MAP_BYTES, encoded
    raw = encoded(state)
    if len(raw.encode()) > MAP_BYTES * 4:
        raise DevError('KNOWLEDGE_STORAGE_LIMIT', '构建进度超过存储预算；已发布地图未改变', 409)
    cursor = digest(raw.encode())
    with engine.journal.lock, engine.journal.db:
        db = engine.journal.db
        db.execute('''CREATE TABLE IF NOT EXISTS knowledge_build_progress (
            map_key TEXT PRIMARY KEY,cursor TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL)''')
        if expected_cursor:
            updated = db.execute('UPDATE knowledge_build_progress SET cursor=?,payload=?,updated=? WHERE map_key=? AND cursor=?',
                                 (cursor, raw, time.time(), key, expected_cursor))
            if updated.rowcount != 1:
                raise DevError('KNOWLEDGE_CURSOR_CONFLICT', '另一批构建已更新进度', 409)
        else:
            db.execute('INSERT OR REPLACE INTO knowledge_build_progress VALUES (?,?,?,?)',
                       (key, cursor, raw, time.time()))
    return cursor


def inventory(engine, root, state):
    """Advance a serializable traversal; every file is reauthorized when visited."""
    from agent.project_knowledge import eligible
    pending = state['pending']
    while pending:
        path = pending.pop()
        if path != '.' and excluded(path, state['options']['exclude_paths']):
            continue
        try:
            actual = engine.path(root, path, path == '.')
            info = actual.lstat()
            if stat.S_ISDIR(info.st_mode):
                # Names only: stat, link checks and reads happen in later batches.
                with os.scandir(actual) as iterator:
                    names = [entry.name for entry in iterator]
                children = [(name if path == '.' else path + '/' + name) for name in sorted(names, reverse=True)]
                pending.extend(p for p in children if not excluded(p, state['options']['exclude_paths']))
            elif stat.S_ISREG(info.st_mode) and eligible(path):
                yield actual, path, info
        except (DevError, OSError) as error:
            code = getattr(error, 'code', 'FILE_READ_FAILED')
            state['skipped'][code] = state['skipped'].get(code, 0) + 1
        # Let the caller checkpoint even a tree of directories or irrelevant files.
        yield None
