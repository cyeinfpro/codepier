"""Metadata fast path, revocation and source races in synthetic projects."""
import os

import pytest

from agent.filesystem import FileEngine
from agent.journal import Journal
from agent import knowledge_refresh as refresh
from agent import project_knowledge as knowledge
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    journal = Journal(tmp_path / 'state')
    config = {'allowed_roots': [{'path': str(root), 'writable': True}]}
    engine = FileEngine(config, journal, tmp_path / 'private' / 'config.json')
    project = {'id': 'fixture', 'root': str(root), 'mode': 'write'}
    yield engine, project, root
    journal.db.close()
    refresh._CACHES.clear()


def build(workspace, **args):
    engine, project, _ = workspace
    return refresh.build(engine, project, args)


def test_incremental_reads_only_changed_files_and_detects_external_rename_delete(workspace, monkeypatch):
    engine, project, root = workspace
    for name in ('a', 'b'):
        (root / (name + '.py')).write_text('def ' + name + '(): pass\n')
    build(workspace)
    reads = []
    original = engine.read_bytes
    def read(path, **kwargs):
        reads.append(path.name)
        return original(path, **kwargs)
    monkeypatch.setattr(engine, 'read_bytes', read)
    assert build(workspace, incremental=True)['refresh']['metadata_reused'] == 2
    assert reads == []
    (root / 'a.py').write_text('def changed(): pass\n')
    assert build(workspace, incremental=True)['refresh']['metadata_reused'] == 1
    assert reads == ['a.py']
    (root / 'a.py').rename(root / 'renamed.py')
    (root / 'b.py').unlink()
    build(workspace, incremental=True)
    result = knowledge.query(engine, project, {})
    assert [entry['path'] for entry in result['entries']] == ['renamed.py']
    assert result['entries'][0]['symbols'] == ['changed']


def test_periodic_full_and_restart_force_sha_reads(workspace, monkeypatch):
    engine, _, root = workspace
    (root / 'a.py').write_text('pass\n')
    build(workspace)
    assert build(workspace, incremental=True)['refresh']['mode'] == 'incremental'
    monkeypatch.setattr(refresh, 'FULL_INTERVAL', 0)
    assert build(workspace, incremental=True)['refresh']['mode'] == 'full'
    refresh._CACHES.clear()
    assert build(workspace, incremental=True)['refresh']['mode'] == 'full'


def test_mtime_restored_change_detected_and_query_withholds_stale(workspace):
    engine, project, root = workspace
    path = root / 'a.py'
    path.write_text('def old(): pass\n')
    build(workspace)
    before = path.stat()
    path.write_text('def new(): pass\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    stale = knowledge.query(engine, project, {})['entries'][0]
    assert stale['state'] == 'stale' and 'summary' not in stale
    build(workspace, incremental=True)
    assert knowledge.query(engine, project, {})['entries'][0]['symbols'] == ['new']


def test_cache_cannot_bypass_root_path_or_hardlink_policy(workspace, monkeypatch):
    engine, project, root = workspace
    path = root / 'a.py'
    path.write_text('pass\n')
    build(workspace)
    engine.config['allowed_roots'][0]['writable'] = False
    with pytest.raises(DevError, match='只读'):
        build(workspace, incremental=True)
    engine.config['allowed_roots'][0]['writable'] = True
    os.link(path, root / 'b.py')
    assert build(workspace, incremental=True)['entries_count'] == 0
    (root / 'b.py').unlink()
    build(workspace)
    original = engine.path
    def denied(root, value, *args):
        if value == 'a.py':
            raise DevError('PROTECTED_PATH', 'new policy', 403)
        return original(root, value, *args)
    monkeypatch.setattr(engine, 'path', denied)
    assert build(workspace, incremental=True)['entries_count'] == 0


def test_read_queries_never_seed_or_update_cache(workspace):
    engine, project, root = workspace
    (root / 'a.py').write_text('pass\n')
    knowledge.query(engine, project, {})
    assert not refresh._CACHES
    build(workspace)
    snapshot = repr(refresh._CACHES)
    changes = engine.journal.db.total_changes
    knowledge.query(engine, project, {})
    knowledge.auto_context(engine, project)
    assert repr(refresh._CACHES) == snapshot
    assert changes == engine.journal.db.total_changes


def test_failed_build_keeps_cache_and_map(workspace, monkeypatch):
    engine, _, root = workspace
    (root / 'a.py').write_text('pass\n')
    build(workspace)
    snapshot = repr(refresh._CACHES)
    monkeypatch.setattr(knowledge, 'MAP_BYTES', 1)
    with pytest.raises(DevError):
        build(workspace, incremental=True)
    assert repr(refresh._CACHES) == snapshot


def test_structure_version_invalidates_metadata_cache(workspace, monkeypatch):
    _, _, root = workspace
    (root / 'a.py').write_text('pass\n')
    build(workspace)
    monkeypatch.setattr(knowledge, 'STRUCTURE_VERSION', 99, raising=False)
    assert build(workspace, incremental=True)['refresh']['metadata_reused'] == 0


def test_race_after_read_never_seeds_new_metadata_for_old_sha(workspace, monkeypatch):
    engine, _, root = workspace
    path = root / 'a.py'
    path.write_text('def old(): pass\n')
    original = engine.read_bytes
    def read(path, **kwargs):
        data = original(path, **kwargs)
        path.write_text('def new(): pass\n')
        return data
    monkeypatch.setattr(engine, 'read_bytes', read)
    build(workspace)
    assert all('a.py' not in cache['files'] for cache in refresh._CACHES.values())
    monkeypatch.setattr(engine, 'read_bytes', original)
    assert build(workspace, incremental=True)['refresh']['metadata_reused'] == 0


def test_incremental_cannot_initialize_replaced_or_missing_map(workspace):
    _, project, root = workspace
    (root / 'a.py').write_text('pass\n')
    with pytest.raises(DevError, match='显式生成'):
        build(workspace, incremental=True)
    build(workspace)
    replacement = root.parent / 'replacement'
    replacement.mkdir()
    project['root'] = str(replacement)
    workspace[0].config['allowed_roots'].append({'path': str(replacement), 'writable': True})
    with pytest.raises(DevError, match='显式生成'):
        build(workspace, incremental=True)


def test_metadata_reuse_obeys_total_covered_byte_budget(workspace, monkeypatch):
    _, _, root = workspace
    (root / 'a.py').write_text('pass\n')
    build(workspace)
    monkeypatch.setattr(knowledge, 'SCAN_BYTES', 1)
    with pytest.raises(DevError) as error:
        build(workspace, incremental=True)
    assert error.value.code == 'KNOWLEDGE_SCAN_LIMIT'
