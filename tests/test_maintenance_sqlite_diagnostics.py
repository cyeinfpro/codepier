"""Read-only maintenance diagnostics against synthetic, private SQLite state."""
from contextlib import closing
import errno
import os
from pathlib import Path
import sqlite3

import pytest

from scripts import install_agent as installer


pytestmark = pytest.mark.windows_core


def make_database(base, kind='agent', statuses=()):
    path = base / ('state/agent.sqlite3' if kind == 'agent'
                   else 'state/native-cli/native.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    table = 'calls' if kind == 'agent' else 'sessions'
    with closing(sqlite3.connect(path)) as db:
        db.execute(f'CREATE TABLE {table} (status TEXT)')
        db.executemany(f'INSERT INTO {table} VALUES (?)', [(status,) for status in statuses])
        db.commit()
    return path


def sqlite_error(code, name, message):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


def test_absent_optional_state_does_not_create_files(tmp_path):
    before = list(tmp_path.rglob('*'))
    installer.require_idle(tmp_path, {})
    assert list(tmp_path.rglob('*')) == before


def test_idle_databases_remain_unchanged_and_use_configured_state(tmp_path):
    base = tmp_path / 'installation'
    state_base = tmp_path / 'configured-state'
    paths = [make_database(state_base, kind, ['completed', 'interrupted'])
             for kind in ('agent', 'native')]
    before = {path: path.read_bytes() for path in paths}
    installer.require_idle(base, {'state_dir': str(state_base / 'state')})
    assert {path: path.read_bytes() for path in paths} == before
    assert not base.exists()
    assert not list(state_base.rglob('*-wal'))
    assert not list(state_base.rglob('*-shm'))


@pytest.mark.parametrize('kind,status', [
    ('agent', 'running'), ('agent', 'accepted'),
    ('native', 'starting'), ('native', 'running'),
    ('native', 'stopping'), ('native', 'orphaned'),
])
def test_all_busy_states_still_block_maintenance(tmp_path, kind, status):
    make_database(tmp_path, kind, [status])
    expected = 'active operations' if kind == 'agent' else 'Native terminal sessions'
    with pytest.raises(ValueError, match=expected):
        installer.require_idle(tmp_path, {}, recover_stale_operations=kind == 'native')


@pytest.mark.parametrize('kind', ['agent', 'native'])
def test_committed_busy_state_in_wal_is_not_missed(tmp_path, kind):
    path = make_database(tmp_path, kind)
    table = 'calls' if kind == 'agent' else 'sessions'
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        baseline = path.read_bytes()
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute(f"INSERT INTO {table} VALUES ('running')")
        writer.commit()
        assert path.read_bytes() == baseline
        wal = Path(str(path) + '-wal')
        assert wal.stat().st_size > 0
        wal_before = wal.read_bytes()
        with pytest.raises(ValueError, match='active operations|Native terminal sessions'):
            installer.require_idle(tmp_path, {})
        assert path.read_bytes() == baseline
        assert wal.read_bytes() == wal_before


@pytest.mark.parametrize('kind', ['agent', 'native'])
def test_real_exclusive_sqlite_lock_preserves_original_failure(tmp_path, kind):
    path = make_database(tmp_path, kind)
    with closing(sqlite3.connect(path)) as owner:
        owner.execute('BEGIN EXCLUSIVE')
        try:
            with pytest.raises(ValueError) as raised:
                installer.require_idle(tmp_path, {}, recover_stale_operations=True)
            error = raised.value.__cause__
            assert isinstance(error, sqlite3.OperationalError)
            assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
            message = str(raised.value)
            assert repr(str(path)) in message
            assert str(error) in message
            assert 'sqlite_errorcode=5' in message and 'SQLITE_BUSY' in message
            assert 'database busy/locked' in message
            assert 'Maintenance remains blocked' in message
            assert 'readable' in message
        finally:
            owner.rollback()


@pytest.mark.parametrize('kind', ['agent', 'native'])
def test_real_invalid_database_is_not_misreported_as_permissions(tmp_path, kind):
    path = make_database(tmp_path, kind)
    path.write_bytes(b'not a SQLite database' * 100)
    before = path.read_bytes()
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {})
    error = raised.value.__cause__
    assert isinstance(error, sqlite3.DatabaseError)
    assert error.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    message = str(raised.value)
    assert repr(str(path)) in message and str(error) in message
    assert 'sqlite_errorcode=26' in message and 'SQLITE_NOTADB' in message
    assert 'corrupt or invalid database' in message
    assert 'permission denied' not in message
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['agent', 'native'])
def test_missing_schema_is_not_interpreted_as_idle(tmp_path, kind):
    path = make_database(tmp_path, kind)
    with closing(sqlite3.connect(path)) as db:
        db.execute('DROP TABLE ' + ('calls' if kind == 'agent' else 'sessions'))
        db.commit()
    with pytest.raises(ValueError, match='missing or incompatible database schema') as raised:
        installer.require_idle(tmp_path, {})
    assert 'no such table' in str(raised.value)
    assert 'SQLITE_ERROR' in str(raised.value)


@pytest.mark.parametrize('denied_suffixes', [('',), ('-wal',), ('-shm',), ('-wal', '-shm')])
def test_access_denied_probes_identify_exact_files_without_repair(
        tmp_path, monkeypatch, denied_suffixes):
    path = make_database(tmp_path, 'native')
    Path(str(path) + '-wal').write_bytes(b'')
    Path(str(path) + '-shm').write_bytes(b'\0' * 32768)
    paths = [Path(str(path) + suffix) for suffix in ('', '-wal', '-shm')]
    before = {entry: (entry.read_bytes(), entry.stat().st_mode) for entry in paths}
    original = sqlite_error(sqlite3.SQLITE_CANTOPEN, 'SQLITE_CANTOPEN',
                            'unable to open database file')
    def fail_sqlite(*args, **kwargs):
        raise original
    monkeypatch.setattr(sqlite3, 'connect', fail_sqlite)
    real_open = os.open
    attempted = []
    def read_only_open(candidate, flags, *args, **kwargs):
        candidate = Path(candidate)
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        attempted.append(candidate)
        if candidate in [Path(str(path) + suffix) for suffix in denied_suffixes]:
            raise PermissionError(errno.EACCES, 'Access denied', str(candidate))
        return real_open(candidate, flags, *args, **kwargs)
    monkeypatch.setattr(installer.os, 'open', read_only_open)
    def native_denied(candidate):
        error = PermissionError(errno.EACCES, 'Access denied', str(candidate))
        error.winerror = 5
        raise error
    monkeypatch.setattr(installer, '_maintenance_windows_read_probe', native_denied)
    monkeypatch.setattr(installer, 'load_lifecycle_helper',
                        lambda *_: pytest.fail('diagnostic failure must not enter recovery'))
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {}, recover_stale_operations=True)
    assert raised.value.__cause__ is original
    message = str(raised.value)
    assert 'SQLITE_CANTOPEN' in message and 'sqlite_errorcode=14' in message
    assert 'permission denied' in message and 'errno=13' in message
    assert 'Access denied' in message and 'Maintenance remains blocked' in message
    for suffix in denied_suffixes:
        assert f"{str(path) + suffix!r}: permission denied" in message
    if '' not in denied_suffixes:
        assert f"{str(path)!r}: readable" in message
    assert 'size=0' in message and 'size=32768' in message
    assert attempted == paths
    assert {entry: (entry.read_bytes(), entry.stat().st_mode) for entry in paths} == before
    assert sorted(entry for entry in tmp_path.rglob('*') if entry.is_file()) == sorted(paths)


def test_access_denied_during_presence_check_is_not_absence(tmp_path, monkeypatch):
    path = make_database(tmp_path)
    original_lstat = Path.lstat
    original = PermissionError(errno.EACCES, 'state directory denied', str(path))
    original.winerror = 5
    def denied_lstat(candidate, *args, **kwargs):
        if candidate == path:
            raise original
        return original_lstat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', denied_lstat)
    with pytest.raises(ValueError, match='permission denied') as raised:
        installer.require_idle(tmp_path, {})
    assert raised.value.__cause__ is original
    assert 'state directory denied' in str(raised.value)
    assert repr(str(path)) in str(raised.value)


def test_diagnostic_failure_preserves_original_sqlite_error(tmp_path, monkeypatch):
    path = make_database(tmp_path, 'native')
    original = sqlite_error(sqlite3.SQLITE_IOERR, 'SQLITE_IOERR', 'disk I/O error')
    sqlite_attempted = False
    def fail_sqlite(*args, **kwargs):
        nonlocal sqlite_attempted
        sqlite_attempted = True
        raise original
    monkeypatch.setattr(sqlite3, 'connect', fail_sqlite)
    original_stat = Path.stat
    def denied_stat(candidate, *args, **kwargs):
        if sqlite_attempted and candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
            raise PermissionError(errno.EACCES, 'diagnostic read denied')
        return original_stat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied_stat)
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {})
    assert raised.value.__cause__ is original
    assert 'storage I/O failure' in str(raised.value)
    assert 'diagnostic read denied' in str(raised.value)
    assert 'disk I/O error' in str(raised.value)


@pytest.mark.parametrize('fail_query', [False, True])
def test_read_only_connections_close_on_success_and_query_failure(tmp_path, monkeypatch, fail_query):
    path = make_database(tmp_path)
    if fail_query:
        with closing(sqlite3.connect(path)) as db:
            db.execute('DROP TABLE calls')
            db.commit()
    real_connect = sqlite3.connect
    connections = []
    class Connection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()
    def track_connection(database_uri, **kwargs):
        assert database_uri == path.resolve().as_uri() + '?mode=ro'
        assert kwargs == {'uri': True, 'timeout': 3}
        connection = real_connect(database_uri, factory=Connection, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', track_connection)
    if fail_query:
        with pytest.raises(ValueError):
            installer.require_idle(tmp_path, {})
    else:
        installer.require_idle(tmp_path, {})
    assert len(connections) == 1 and connections[0].closed


@pytest.mark.parametrize('code,name,category', [
    (sqlite3.SQLITE_LOCKED | (1 << 8), 'SQLITE_LOCKED_SHAREDCACHE', 'database busy/locked'),
    (sqlite3.SQLITE_CORRUPT, 'SQLITE_CORRUPT', 'corrupt or invalid database'),
    (sqlite3.SQLITE_READONLY | (1 << 8), 'SQLITE_READONLY_RECOVERY', 'SQLite read-only/recovery restriction'),
    (sqlite3.SQLITE_AUTH, 'SQLITE_AUTH', 'permission denied'),
])
def test_extended_sqlite_codes_are_classified_without_losing_detail(code, name, category):
    error = sqlite_error(code, name, 'fixture failure')
    assert installer._maintenance_error_kind(error) == category
    assert f'sqlite_errorcode={code}' in installer._maintenance_error_detail(error)
    assert name in installer._maintenance_error_detail(error)


@pytest.mark.parametrize('winerror', [32, 33])
def test_windows_sharing_violation_is_distinct_from_acl_denial(winerror):
    error = PermissionError(errno.EACCES, 'file in use')
    error.winerror = winerror
    assert installer._maintenance_error_kind(error) == 'file-sharing/lock conflict'
    assert f'winerror={winerror}' in installer._maintenance_error_detail(error)


def test_unknown_sqlite_failure_is_not_falsely_diagnosed_as_permission():
    error = sqlite3.OperationalError('an unknown failure')
    assert installer._maintenance_error_kind(error) == 'unclassified read failure'
    assert 'an unknown failure' in installer._maintenance_error_detail(error)


def test_pending_plan_still_blocks_after_successful_database_reads(tmp_path):
    make_database(tmp_path)
    make_database(tmp_path, 'native')
    folder = tmp_path / 'state/lifecycle'
    folder.mkdir()
    (folder / 'pending.json').write_text('{}')
    with pytest.raises(ValueError, match='pending'):
        installer.require_idle(tmp_path, {})


@pytest.mark.parametrize('kind', ['agent', 'native'])
def test_real_corrupt_btree_is_reported_without_modifying_database(tmp_path, kind):
    path = make_database(tmp_path, kind)
    table = 'calls' if kind == 'agent' else 'sessions'
    with closing(sqlite3.connect(path)) as db:
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        root_page = db.execute('SELECT rootpage FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
    raw = bytearray(path.read_bytes())
    raw[(root_page - 1) * page_size] = 255  # Invalid b-tree page type.
    path.write_bytes(raw)
    before = path.read_bytes()
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {})
    error = raised.value.__cause__
    assert isinstance(error, sqlite3.DatabaseError)
    assert error.sqlite_errorcode == sqlite3.SQLITE_CORRUPT
    assert 'SQLITE_CORRUPT' in str(raised.value)
    assert 'corrupt or invalid database' in str(raised.value)
    assert path.read_bytes() == before


def test_nonregular_file_probe_does_not_open_it(tmp_path, monkeypatch):
    path = tmp_path / 'native.sqlite3'
    path.mkdir()
    monkeypatch.setattr(installer.os, 'open', lambda *_a, **_k: pytest.fail('must not open directory'))
    details, kinds = installer._maintenance_file_diagnostics(path)
    assert 'not a regular file' in details[0]
    assert all('absent' in detail for detail in details[1:])
    assert not kinds


def test_failed_read_probe_closes_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'native.sqlite3'
    path.write_bytes(b'fixture')
    real_close = os.close
    closed = []
    def denied_read(fd, count):
        raise PermissionError(errno.EACCES, 'read denied')
    def native_denied(candidate):
        error = PermissionError(errno.EACCES, 'read denied')
        error.winerror = 5
        raise error
    monkeypatch.setattr(installer, '_maintenance_windows_read_probe', native_denied)
    def close(fd):
        closed.append(fd)
        real_close(fd)
    monkeypatch.setattr(installer.os, 'read', denied_read)
    monkeypatch.setattr(installer.os, 'close', close)
    details, kinds = installer._maintenance_file_diagnostics(path)
    assert len(closed) == 1
    assert 'permission denied' in kinds
    assert 'read denied' in details[0]


def windows_error(code):
    error = OSError(errno.EACCES if code in (5, 32, 33) else errno.EIO,
                    f'native fixture error {code}')
    error.winerror = code
    return error


@pytest.mark.parametrize('code,category', [
    (32, 'file-sharing/lock conflict'),
    (33, 'file-sharing/lock conflict'),
    (5, 'permission denied'),
])
def test_native_probe_preserves_windows_code_and_both_original_errors(
        tmp_path, monkeypatch, code, category):
    path = make_database(tmp_path, 'native')
    original = sqlite_error(sqlite3.SQLITE_CANTOPEN, 'SQLITE_CANTOPEN',
                            'SQLite fixture cannot open')
    def sqlite_failure(*args, **kwargs):
        raise original
    def crt_failure(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'CRT fixture cannot open')
    probed = []
    def native_failure(candidate):
        probed.append(candidate)
        raise windows_error(code)
    monkeypatch.setattr(sqlite3, 'connect', sqlite_failure)
    monkeypatch.setattr(installer.os, 'open', crt_failure)
    monkeypatch.setattr(installer, '_maintenance_windows_read_probe', native_failure)
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {}, recover_stale_operations=True)
    assert raised.value.__cause__ is original
    message = str(raised.value)
    assert repr(str(path)) in message
    assert 'SQLite fixture cannot open' in message and 'sqlite_errorcode=14' in message
    assert 'CRT fixture cannot open' in message and 'errno=13' in message
    assert f'winerror={code}' in message
    assert f'Diagnostic categories: database open failure, {category}.' in message
    assert 'Maintenance remains blocked' in message
    assert probed == [path]


@pytest.mark.parametrize('outcome', ['success', 'missing-ctypes', 'missing-api'])
def test_native_recheck_success_or_unavailability_never_proves_idle_or_acl_failure(
        tmp_path, monkeypatch, outcome):
    path = make_database(tmp_path)
    original = sqlite_error(sqlite3.SQLITE_CANTOPEN, 'SQLITE_CANTOPEN', 'SQLite open failed')
    def sqlite_failure(*args, **kwargs):
        raise original
    def crt_failure(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'CRT read-open failed')
    def native_probe(candidate):
        assert candidate == path
        if outcome == 'missing-ctypes':
            raise ImportError('native probe dependency unavailable')
        if outcome == 'missing-api':
            raise AttributeError('native API unavailable')
    monkeypatch.setattr(sqlite3, 'connect', sqlite_failure)
    monkeypatch.setattr(installer.os, 'open', crt_failure)
    monkeypatch.setattr(installer, '_maintenance_windows_read_probe', native_probe)
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    with pytest.raises(ValueError) as raised:
        installer.require_idle(tmp_path, {})
    assert raised.value.__cause__ is original
    message = str(raised.value)
    assert 'access denied or file-sharing conflict (native detail unavailable)' in message
    assert 'CRT read-open failed' in message
    assert 'SQLite open failed' in message
    assert 'permission denied' not in message
    assert 'Maintenance remains blocked' in message
    if outcome == 'success':
        assert 'Native read-open probe succeeded; original failure remains unresolved' in message
    else:
        assert 'Native read-open probe unavailable' in message


@pytest.mark.parametrize('open_error,close_ok', [
    (None, True), (32, True), (33, True), (5, True), (None, False),
])
def test_win32_probe_is_read_only_and_closes_every_opened_handle(
        tmp_path, monkeypatch, open_error, close_ok):
    import ctypes
    from types import SimpleNamespace
    calls = []
    handle = 123
    class NativeCall:
        def __init__(self, name, result):
            self.name, self.result = name, result
        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result
    kernel = SimpleNamespace(
        CreateFileW=NativeCall('open', ctypes.c_void_p(-1).value if open_error else handle),
        CloseHandle=NativeCall('close', int(close_ok)),
    )
    def load_library(name, **kwargs):
        assert name == 'kernel32' and kwargs == {'use_last_error': True}
        return kernel
    monkeypatch.setattr(ctypes, 'WinDLL', load_library, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: open_error or 6, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', windows_error, raising=False)
    path = tmp_path / 'native.sqlite3'
    if open_error or not close_ok:
        with pytest.raises(OSError) as raised:
            installer._maintenance_windows_read_probe(path)
        assert raised.value.winerror == (open_error or 6)
    else:
        assert installer._maintenance_windows_read_probe(path) is None
    assert calls[0] == ('open', (str(path), 0x80000000, 7, None, 3, 0x80, None))
    assert calls[1:] == ([] if open_error else [('close', (handle,))])
    assert not path.exists()


def test_windows_crt_eacces_alone_is_ambiguous(monkeypatch):
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    error = PermissionError(errno.EACCES, 'Permission denied')
    assert installer._maintenance_error_kind(error) == (
        'access denied or file-sharing conflict (native detail unavailable)')
