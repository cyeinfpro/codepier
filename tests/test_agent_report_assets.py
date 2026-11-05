"""Synthetic S10/S11 regressions. No credentials, network or native programs."""
import json
import os
import stat
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.filesystem import FileEngine, MAX_FILE, protected, relative_path
from agent.incoming_artifacts import AnchoredDestination
from agent.integration_state import Records, binding
from agent.journal import Journal
from agent.native_cli import NativeCLI
from agent.searches import Searches
from shared.crypto import digest
from shared.util import DevError


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config = {'allowed_roots': [{'path': str(root), 'writable': True}],
              'shell': {'enabled': False}}
    journal = Journal(tmp_path / 'state')
    engine = FileEngine(config, journal, tmp_path / 'config.json')
    project = {'root': str(root), 'mode': 'write', 'id': 'project', 'device_id': 'device',
               '_integration_owner': 'owner', '_coding_owner': 'owner', '_coding_device': 'device'}
    yield engine, project, root
    journal.db.close()


@pytest.mark.parametrize('path', ['.git./config', '.ssh /config', '.env ', 'x/id_rsa.',
                                  'dir./file', 'dir /file', 'CON', 'nul.txt', 'COM1.log',
                                  'lpt9', 'CONIN$', 'file\x01', 'file\x7f'])
def test_portable_aliases_rejected(path):
    with pytest.raises(DevError) as error:
        relative_path(path)
    assert error.value.code == 'INVALID_PATH'


@pytest.mark.parametrize('path', ['id_ecdsa', 'id_dsa', '.git-credentials', '.pgpass',
                                  '.s3cfg', '.htpasswd', '.docker/config.json', '.gnupg/key',
                                  '.password-store/item', 'private.ppk', 'store.jks', 'vpn.ovpn'])
def test_credentials_protected(path):
    assert protected(path)
    with pytest.raises(DevError) as error:
        relative_path(path)
    assert error.value.code == 'PROTECTED_PATH'


@pytest.mark.parametrize('content', [b'\x00\xffasset', b'x' * (MAX_FILE + 17)], ids=['binary','oversize'])
@pytest.mark.parametrize('operation', ['delete', 'move'])
def test_asset_digest_mutation_and_durable_restore(assets, content, operation):
    engine, project, root = assets
    (root / 'asset.bin').write_bytes(content)
    (root / 'asset.bin').chmod(0o640)
    metadata = engine.read(project, {'path': 'asset.bin'})
    assert metadata['sha256'] == digest(content)
    assert metadata['content_available'] is False and metadata['truncated']
    if operation == 'delete':
        result = engine.mutate(project, 'asset.bin', metadata['sha256'], None)
    else:
        result = engine.move(project, {'path': 'asset.bin', 'destination': 'moved.bin',
                                       'expected_sha256': metadata['sha256']})
        assert (root / 'moved.bin').read_bytes() == content
    assert not (root / 'asset.bin').exists()
    row, saved = engine.journal.backup(str(root), result['backup_id'])
    assert saved == content and row['before_mode'] == 0o640
    engine.call('history_restore', project, {'backup_id': result['backup_id'], 'expected_sha256': 'new'})
    assert (root / 'asset.bin').read_bytes() == content
    assert stat.S_IMODE((root / 'asset.bin').stat().st_mode) == 0o640


def test_blob_sha_and_link_guards_preserve_data(assets):
    engine, project, root = assets
    content = b'\x00asset'
    (root / 'asset.bin').write_bytes(content)
    with pytest.raises(DevError, match='改变'):
        engine.mutate(project, 'asset.bin', '0' * 64, None)
    assert (root / 'asset.bin').read_bytes() == content
    os.link(root / 'asset.bin', root / 'alias.bin')
    with pytest.raises(DevError) as error:
        engine.mutate(project, 'asset.bin', digest(content), None)
    assert error.value.code == 'HARDLINK_BLOCKED'
    assert (root / 'asset.bin').read_bytes() == content


def test_binary_move_uncertain_publication_preserves_source(assets, monkeypatch):
    engine, project, root = assets
    content = b'\x00asset'
    (root / 'asset.bin').write_bytes(content)
    publish = engine.publish_new_file

    def copy_only(*args, **kwargs):
        result = publish(*args, **kwargs)
        return SimpleNamespace(method=result.method, atomic=False, source_retained=result.source_retained)
    monkeypatch.setattr(engine, 'publish_new_file', copy_only)
    with pytest.raises(DevError) as error:
        engine.move(project, {'path': 'asset.bin', 'destination': 'moved.bin',
                              'expected_sha256': digest(content)})
    assert error.value.code == 'WRITE_OUTCOME_UNCONFIRMED'
    assert (root / 'asset.bin').read_bytes() == content
    assert (root / 'moved.bin').read_bytes() == content
    assert engine.journal.history(str(root))


def test_search_reports_skips_and_continues(assets):
    engine, project, root = assets
    (root / 'large.txt').write_bytes(b'x' * (MAX_FILE + 1))
    (root / 'small.txt').write_text('needle')
    args = {'path': '.', 'query': 'needle', 'case_sensitive': False, 'file_glob': '*',
            'offset': 0, 'limit': 100}
    result = engine.search(project, args)
    assert result['matches'][0]['path'] == 'small.txt'
    assert result['truncated'] and result['skipped_reasons'] == {'FILE_TOO_LARGE': 1}
    manager = Searches(engine)
    sid = uuid.uuid4().hex
    manager.start(sid, project, {**args, 'mode': 'text', 'timeout_seconds': 10, 'max_results': 100, 'max_files': 100})
    result = manager.get(project, {'search_id': sid, 'cursor': 0, 'limit': 100})
    assert result['truncated'] and result['skipped_reasons'] == {'FILE_TOO_LARGE': 1}
    restarted = Searches(engine).get(project, {'search_id': sid, 'cursor': 0, 'limit': 100})
    assert restarted['skipped_reasons'] == result['skipped_reasons']


def test_record_quota_isolates_active_kind_and_binding_without_erasing_receipts(assets):
    engine, project, _ = assets
    records = Records(engine.journal)
    bound = json.dumps(binding(project), sort_keys=True)
    closed = [( 'browser', f'{i:032x}', bound, '{"state":"closed"}', 1) for i in range(2000)]
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.executemany('INSERT INTO integration_records VALUES(?,?,?,?,?)', closed)
    records.save('browser', 'f' * 32, project, {'state': 'opening'})
    records.save('validation', 'e' * 32, project, {'state': 'executing'})
    assert records.load('browser', '0' * 32, project)['state'] == 'closed'
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.execute("""UPDATE integration_records SET body='{"state":"opening"}' WHERE kind='browser'""")
    with pytest.raises(DevError) as error:
        records.save('browser', 'd' * 32, project, {'state': 'opening'})
    assert error.value.code == 'RECORD_QUOTA'
    records.save('browser', 'd' * 32, {**project, '_integration_owner': 'other'}, {'state': 'opening'})
    with pytest.raises(DevError):
        records.load('browser', '0' * 32, {**project, '_integration_owner': 'other'})


def test_import_staging_and_published_attachment_stay_private(assets):
    engine, _, root = assets
    with AnchoredDestination(engine, root, 'assets/file.bin') as target:
        target.write(b'\x00fixture')
        assert stat.S_IMODE(os.fstat(target.fd).st_mode) == 0o600
        target.publish()
    assert stat.S_IMODE((root / 'assets/file.bin').stat().st_mode) == 0o600
    assert not (root / 'assets').stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


@pytest.mark.parametrize('action', ['clear', 'delete'])
def test_stopped_replay_cleanup_works_without_shell_or_project_write(assets, action):
    engine, project, root = assets
    agent = SimpleNamespace(engine=engine, config=engine.config, state_dir=engine.journal.directory)
    native = NativeCLI(agent)
    sid = uuid.uuid4().hex
    # Authenticated local recovery owner, not an arbitrary client-selected owner.
    project = {**project, '_native_owner': 'user:owner', '_native_space': 'space'}
    with native.connect_db() as db:
        native.security(db, 'session', sid, project, create=True)
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (sid, project['id'], project['device_id'], str(root), str(root), 'codex', 'fixture', 'exited', time.time(), time.time()))
    project['mode'] = 'read'
    engine.config['allowed_roots'][0]['writable'] = False
    result = native.action(action, project, {'id': sid, 'confirm': sid})
    assert result['cleared'] is True
    assert result['native_history'] == 'untouched'


@pytest.mark.parametrize('action', ['delete', 'move'])
def test_modern_read_and_single_asset_edit(assets, action):
    from agent.core_files import CoreFiles
    engine, project, root = assets
    content = b'\x00fixture'
    (root / 'asset.bin').write_bytes(content)
    core = CoreFiles(engine)
    meta = core.call('read', project, {'path': 'asset.bin', 'expected_sha256': '', 'offset': 1, 'limit': 20})
    assert not meta['content_available'] and meta['sha256'] == digest(content)
    from jsonschema import Draft202012Validator
    from shared.contracts import OUTPUT_SCHEMAS
    Draft202012Validator(OUTPUT_SCHEMAS['read']).validate({'operation_id': 'a' * 32, **meta})
    args = {'changes': [{'action': action, 'path': 'asset.bin', 'expected_sha256': meta['sha256'],
                          'destination': 'moved.bin'}], 'dry_run': True}
    preview = core.call('edit', project, args)
    assert preview['outcome'] == 'preview' and (root / 'asset.bin').exists()
    assert not engine.journal.history(str(root))
    result = core.call('edit', project, {**args, 'dry_run': False})
    assert result['success'] and result['files'][0]['backup_id']
    assert not (root / 'asset.bin').exists()
    if action == 'move':
        assert (root / 'moved.bin').read_bytes() == content


def test_active_record_visible_behind_historical_receipts(assets):
    engine, project, _ = assets
    records = Records(engine.journal)
    bound = json.dumps(binding(project), sort_keys=True)
    rows = [('browser', f'{i:032x}', bound, '{"state":"closed"}', i + 10) for i in range(2001)]
    rows.append(('browser', 'f' * 32, bound, '{"state":"ready"}', 1))
    with engine.journal.lock, engine.journal.db:
        engine.journal.db.executemany('INSERT INTO integration_records VALUES(?,?,?,?,?)', rows)
    assert records.list('browser', project, 1) == [{'state': 'ready'}]


def test_blob_read_budget_is_enforced_before_mutation(assets, monkeypatch):
    import agent.file_blobs as blobs
    engine, project, root = assets
    content = b'\x00' * 65
    (root / 'asset.bin').write_bytes(content)
    monkeypatch.setattr(blobs, 'MAX_BLOB', 64)
    with pytest.raises(DevError) as error:
        engine.mutate(project, 'asset.bin', digest(content), None)
    assert error.value.code == 'FILE_TOO_LARGE'
    assert (root / 'asset.bin').read_bytes() == content
    assert not engine.journal.history(str(root))


def test_cleanup_retains_owner_mapping_and_confirmation_guards(assets):
    engine, project, root = assets
    native = NativeCLI(SimpleNamespace(engine=engine, config=engine.config, state_dir=engine.journal.directory))
    project = {**project, '_native_owner': 'user:owner', '_native_space': 'space'}
    sid = uuid.uuid4().hex
    with native.connect_db() as db:
        native.security(db, 'session', sid, project, create=True)
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (sid, project['id'], project['device_id'], str(root), str(root), 'codex', 'fixture', 'exited', 1, 1))
    with pytest.raises(DevError) as error:
        native.action('delete', {**project, '_native_owner': 'user:other'}, {'id': sid, 'confirm': sid})
    assert error.value.code == 'CLI_OWNER_DENIED'
    with pytest.raises(DevError) as error:
        native.action('delete', {**project, 'device_id': 'other'}, {'id': sid, 'confirm': sid})
    assert error.value.code == 'CLI_MAPPING_CHANGED'
    with pytest.raises(ValueError, match='confirmation'):
        native.action('delete', project, {'id': sid, 'confirm': 'bad'})


def test_blob_unlink_uncertainty_preserves_backup_and_no_retry_advice(assets, monkeypatch):
    import errno
    from pathlib import Path
    engine, project, root = assets
    content = b'\x00fixture'
    source = root / 'asset.bin'
    source.write_bytes(content)
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == source:
            unlink(path, *args, **kwargs)
            raise OSError(errno.EINTR, 'synthetic interruption after unlink')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', interrupted)
    with pytest.raises(DevError) as error:
        engine.mutate(project, 'asset.bin', digest(content), None)
    assert error.value.code == 'WRITE_OUTCOME_UNCONFIRMED'
    assert error.value.details['retryable'] is False
    _, saved = engine.journal.backup(str(root), error.value.details['backup_id'])
    assert saved == content


def test_corrupt_binary_backup_never_restored(assets):
    engine, project, root = assets
    content = b'\x00fixture'
    (root / 'asset.bin').write_bytes(content)
    result = engine.mutate(project, 'asset.bin', digest(content), None)
    (engine.journal.backups / result['backup_id']).write_bytes(b'changed')
    with pytest.raises(DevError) as error:
        engine.call('history_restore', project, {'backup_id': result['backup_id'], 'expected_sha256': 'new'})
    assert error.value.code == 'BACKUP_CORRUPT'
    assert not (root / 'asset.bin').exists()


def test_destination_changed_after_publish_preserves_binary_source(assets, monkeypatch):
    engine, project, root = assets
    content = b'\x00fixture'
    (root / 'asset.bin').write_bytes(content)
    sync = engine.sync_directory

    def changed(*args, **kwargs):
        if (root / 'moved.bin').exists():
            (root / 'moved.bin').write_bytes(b'local edit')
        return sync(*args, **kwargs)
    monkeypatch.setattr(engine, 'sync_directory', changed)
    with pytest.raises(DevError) as error:
        engine.move(project, {'path': 'asset.bin', 'destination': 'moved.bin', 'expected_sha256': digest(content)})
    assert error.value.code == 'WRITE_OUTCOME_UNCONFIRMED'
    assert (root / 'asset.bin').read_bytes() == content
    assert (root / 'moved.bin').read_bytes() == b'local edit'
