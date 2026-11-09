"""Resumable source discovery, publication, races and locale-independent reads."""
import io
import json

import pytest

from agent import project_knowledge as knowledge, workspace_probe
from agent import knowledge_refresh
from shared.util import DevError
from tests.test_project_knowledge import workspace, build, query  # noqa: F401


def test_batches_survive_restart_keep_old_map_and_publish_all(workspace):
    engine, project, root = workspace
    (root / 'old.py').write_text('pass\n')
    old = build(workspace)
    for index in range(7):
        (root / f'new{index}.py').write_text(f'def value{index}(): pass\n')
    args = {'batch_size': 2}
    seen = []
    while True:
        # Fresh Python metadata cache on each chunk models an Agent restart.
        knowledge_refresh._CACHES.clear()
        result = knowledge_refresh.build(engine, project, args)
        if result['state'] == 'ready':
            break
        assert query(workspace)['revision'] == old['revision']
        assert result['cursor'] not in seen
        seen.append(result['cursor'])
        args = result['continuation']
    assert len(seen) == 3 and result['entries_count'] == 8
    assert query(workspace)['total_matches'] == 8
    assert not engine.journal.db.execute('SELECT * FROM knowledge_build_progress').fetchall()


def test_scoped_build_prunes_generated_trees_before_metadata(workspace, monkeypatch):
    engine, project, root = workspace
    for directory in ('src', 'dist', 'src/generated', 'outside'):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / 'source.py').write_text('pass\n')
    original = engine.path
    touched = []
    def checked(root, path, *args, **kwargs):
        touched.append(path)
        assert not path.startswith(('dist', 'src/generated', 'outside'))
        return original(root, path, *args, **kwargs)
    monkeypatch.setattr(engine, 'path', checked)
    result = knowledge.build(engine, project, {'paths': ['src'], 'exclude_paths': ['src/generated']})
    assert result['state'] == 'ready' and result['entries_count'] == 1
    assert 'src/source.py' in touched
    assert [e['path'] for e in query(workspace)['entries']] == ['src/source.py']


def test_cursor_rejects_replay_changed_scope_and_concurrent_map(workspace):
    engine, project, root = workspace
    for index in range(4):
        (root / f'{index}.py').write_text('pass\n')
    first = knowledge.build(engine, project, {'batch_size': 1})
    second = knowledge.build(engine, project, first['continuation'])
    with pytest.raises(DevError, match='进度'):
        knowledge.build(engine, project, first['continuation'])
    with pytest.raises(DevError, match='范围'):
        knowledge.build(engine, project, {**second['continuation'], 'exclude_paths': ['2.py']})
    knowledge.build(engine, project, {})
    with pytest.raises(DevError):
        knowledge.build(engine, project, second['continuation'])


def test_byte_budget_returns_progress_instead_of_discarding_build(workspace, monkeypatch):
    engine, project, root = workspace
    for name in ('a', 'b'):
        (root / (name + '.py')).write_text('pass\n')
    monkeypatch.setattr(knowledge, 'SCAN_BYTES', 1)
    first = knowledge.build(engine, project, {})
    assert first['state'] == 'building' and first['entries_count'] == 1
    assert query(workspace)['state'] == 'missing'
    final = knowledge.build(engine, project, first['continuation'])
    assert final['state'] == 'ready' and final['entries_count'] == 2


def test_sensitive_annotation_is_rejected_before_any_checkpoint(workspace):
    engine, project, root = workspace
    for index in range(3):
        (root / f'{index}.py').write_text('pass\n')
    with pytest.raises(DevError, match='凭据'):
        knowledge.build(engine, project, {'batch_size': 1, 'entries': [
            {'path': '0.py', 'expected_sha256': '0' * 64, 'summary': 'password=synthetic-secret'}]})
    assert not engine.journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_build_progress'").fetchone()


def test_single_transient_read_race_is_rechecked_but_writer_is_withheld(workspace, monkeypatch):
    engine, project, root = workspace
    (root / 'source.py').write_text('def good(): pass\n')
    original = engine.read_bytes
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DevError('FILE_CHANGED', 'race', 409)
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, 'read_bytes', read)
    assert knowledge.build(engine, project, {})['entries_count'] == 1
    assert len(calls) == 2
    def changing(*args, **kwargs):
        raise DevError('FILE_CHANGED', 'continuous writer', 409)
    monkeypatch.setattr(engine, 'read_bytes', changing)
    result = knowledge.build(engine, project, {})
    assert result['entries_count'] == 0 and result['skipped_reasons'] == {'FILE_CHANGED': 1}


@pytest.mark.parametrize('entry', ['main', 'session_main'])
def test_worker_protocol_is_valid_with_windows_legacy_stdout(entry, monkeypatch):
    # Real byte encoding, including a character absent from CP936.
    request = {'tool': 'fixture', 'args': {}, 'phase': 'root', 'expected_root': '/fixture'}
    raw = json.dumps(request).encode() + b'\n'
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding='gbk', write_through=True)
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')
    monkeypatch.setattr(workspace_probe.sys, 'stdin', stdin)
    monkeypatch.setattr(workspace_probe.sys, 'stdout', stdout)
    monkeypatch.setattr(workspace_probe, 'response', lambda _: {'ok': True, 'data': {'title': '项目地图 🗺'}})
    getattr(workspace_probe, entry)()
    assert workspace_probe.decode_result(output.getvalue())['data']['title'] == '项目地图 🗺'


def test_map_over_old_entry_limit_remains_readable(workspace):
    engine, project, _ = workspace
    # Exercise the on-disk format and read-only SQL metadata without 5,001 I/Os.
    from shared.crypto import digest
    _, owner, key = knowledge.identity(engine, project)
    entries = [knowledge.structural(f'{i}.py', 'pass\n', '0' * 64) for i in range(5001)]
    payload = knowledge.encoded({'version': knowledge.VERSION, 'entries': entries, 'scan_scope': 'filesystem_walk'})
    engine.journal.db.execute('CREATE TABLE project_knowledge (map_key TEXT PRIMARY KEY, project_id TEXT, revision TEXT,payload TEXT,updated REAL)')
    engine.journal.db.execute('INSERT INTO project_knowledge VALUES (?,?,?,?,0)', (key, owner, digest(payload.encode()), payload))
    engine.journal.db.commit()
    assert len(knowledge.load(engine, key)['payload']['entries']) == 5001
    assert knowledge.index_status(engine, project)['entries_count'] == 5001
