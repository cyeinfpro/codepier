"""Private temporary stores exercise actual persistence, isolation and recovery."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
from types import SimpleNamespace
import zlib

import pytest

from agent import coding_reviews as reviews, review_content
from shared.util import DevError, atomic_json
from tests.test_agentdock_context import workspace
from tests.test_coding_workflow import call
from tests.test_review_store_lifecycle import bound, records, rewrite


def disk_bytes(store):
    return sum(p.stat().st_size for p in store.directory.iterdir() if p.suffix in {'.json', '.zlib'})


def test_identical_baselines_share_content_but_keep_independent_identity_and_lease(workspace):
    engine, _, root = workspace
    text = 'shared source line\n' * 10000
    (root/'source').write_text(text)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    first_bytes = disk_bytes(store)
    second = call(workspace, 'open_workspace', capture_baseline=True)
    assert first['baseline_ref'] != second['baseline_ref']
    assert len(list(store.directory.glob('*.zlib'))) == 1
    assert disk_bytes(store) < first_bytes * 2
    for result in (first, second):
        doc = store.load(result['baseline_ref'], bound(workspace), 'baseline')
        assert doc['files']['source']['text'] == text
        assert doc['expires_at'] - doc['created'] == reviews.BASELINE_TTL
    store.release_baseline(first['baseline_ref'], bound(workspace))
    assert store.load(second['baseline_ref'], bound(workspace), 'baseline')['files']['source']['text'] == text
    store.release_baseline(second['baseline_ref'], bound(workspace))
    assert not list(store.directory.glob('*.zlib'))


def test_shared_blob_survives_other_owner_cleanup(workspace):
    engine, _, root = workspace
    (root/'source').write_text('shared\n'*10000)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    other = {**bound(workspace), '_coding_owner': 'grant:other'}
    second = store.save(reviews.capture(engine, other))
    visible = store.inspect(bound(workspace))
    assert visible['records'] == 2 and visible['owned_records'] == 1
    assert [r['ref'] for r in visible['snapshots']] == [first['baseline_ref']]
    assert 'grant:other' not in json.dumps(visible)
    with pytest.raises(DevError, match='不属于'):
        store.release_baseline(second['id'], bound(workspace))
    assert store.purge_owner(bound(workspace)) == 1
    assert store.load(second['id'], other, 'baseline')['files']['source']['text'] == 'shared\n'*10000


def test_lossless_legacy_compaction_keeps_reference_contents_and_old_expiry(workspace):
    engine, _, root = workspace
    (root/'source').write_text('legacy source\n'*10000)
    store = reviews.ReviewStore(engine)
    original = store.save(reviews.capture(engine, bound(workspace)))
    original['expires_at'] = original['created'] + reviews.TTL
    path = store.directory/(original['id']+'.json')
    atomic_json(path, {'sha256': reviews.fingerprint(original), 'payload': original})
    before = path.stat().st_size
    result = store.compact(bound(workspace))
    assert result['compacted'] == 1 and result['leases_preserved']
    assert result['reclaimed_bytes'] > before * .8
    assert store.load(original['id'], bound(workspace), 'baseline') == original


def test_full_legacy_store_compacts_before_rejecting_new_baseline(workspace, monkeypatch):
    engine, _, root = workspace
    (root/'source').write_text('old duplicate source\n'*20000)
    store = reviews.ReviewStore(engine)
    original = store.save(reviews.capture(engine, bound(workspace)))
    path = store.directory/(original['id']+'.json')
    atomic_json(path, {'sha256': reviews.fingerprint(original), 'payload': original})
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', reviews.MAX_DOCUMENT_BYTES + 15000)
    second = call(workspace, 'open_workspace', capture_baseline=True)
    assert second['baseline_ref'] != original['id']
    assert store.load(original['id'], bound(workspace), 'baseline') == original
    assert disk_bytes(store) < 15000


def test_publication_failure_preserves_original_and_next_cleanup_reclaims_orphan(workspace, monkeypatch):
    engine, _, root = workspace
    (root/'source').write_text('before\n'*10000)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    original = store.load(first['baseline_ref'], bound(workspace), 'baseline')
    (root/'source').write_text('after\n'*10000)
    def fail(*args):
        raise OSError('manifest publication failed')
    monkeypatch.setattr(reviews, 'atomic_json', fail)
    with pytest.raises(OSError):
        call(workspace, 'show_changes', baseline_ref=first['baseline_ref'], finalize=True)
    assert store.load(first['baseline_ref'], bound(workspace), 'baseline') == original
    assert len(list(store.directory.glob('*.zlib'))) == 2
    store.cleanup()
    assert len(list(store.directory.glob('*.zlib'))) == 1


@pytest.mark.parametrize('attack', ['symlink', 'hardlink', 'corrupt', 'bomb'])
def test_blob_validation_rejects_unsafe_or_unbounded_content(workspace, tmp_path, attack):
    engine, _, root = workspace
    (root/'source').write_text('source\n'*10000)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    blob = next(store.directory.glob('*.zlib'))
    if attack in {'symlink', 'hardlink'}:
        external = tmp_path/'external'
        blob.rename(external)
        if attack == 'symlink':
            blob.symlink_to(external)
        else:
            os.link(external, blob)
        unchanged = external.read_bytes()
    elif attack == 'corrupt':
        blob.write_bytes(b'not-compressed')
    else:
        blob.write_bytes(zlib.compress(b'x'*(review_content.MAX_TEXT+1)))
    with pytest.raises(DevError):
        store.load(first['baseline_ref'], bound(workspace), 'baseline')
    if attack in {'symlink', 'hardlink'}:
        assert external.read_bytes() == unchanged


def test_scoped_capture_tracks_new_deleted_and_changed_files_only(workspace):
    _, _, root = workspace
    (root/'source').write_text('before')
    (root/'gone').write_text('gone')
    (root/'unrelated').write_text('x'*(reviews.MAX_SOURCE_BYTES+1))
    first = call(workspace, 'open_workspace', capture_baseline=True, baseline_paths=['source', 'new.svg', 'gone'])
    assert first['baseline_coverage']['complete']
    assert first['baseline_coverage']['scope'] == 'paths'
    (root/'source').write_text('after')
    (root/'new.svg').write_text('<svg/>')
    (root/'gone').unlink()
    (root/'unrelated').write_text('changed outside review')
    result = call(workspace, 'show_changes', baseline_ref=first['baseline_ref'], finalize=True)
    assert result['coverage']['complete']
    assert {r['path']:r['status'] for r in result['files']} == {'source':'modified', 'gone':'deleted', 'new.svg':'added'}
    assert '仅覆盖指定文件' in result['scope']


@pytest.mark.parametrize('path', ['../outside', '.env', 'docs/evidence/file', '.work/file', '.'])
def test_scoped_capture_does_not_bypass_exclusions(workspace, path):
    with pytest.raises(DevError):
        call(workspace, 'open_workspace', capture_baseline=True, baseline_paths=[path])
    assert not records(workspace)


def test_status_does_not_reclaim_expired_snapshots_or_expose_other_binding(workspace):
    first = call(workspace, 'open_workspace', capture_baseline=True)
    rewrite(records(workspace)[0], expires_at=1)
    before = records(workspace)[0].read_bytes()
    result = call(workspace, 'review_snapshots')
    assert result['snapshots'][0]['ref'] == first['baseline_ref']
    assert records(workspace)[0].read_bytes() == before


def test_release_api_refuses_completed_review_and_requires_write(workspace):
    first = call(workspace, 'open_workspace', capture_baseline=True)
    call(workspace, 'show_changes', baseline_ref=first['baseline_ref'], finalize=True)
    with pytest.raises(DevError):
        call(workspace, 'review_snapshots_manage', action='release', baseline_ref=first['baseline_ref'], idempotency_key='release-one')
    workspace[0].config['allowed_roots'][0]['writable'] = False
    with pytest.raises(DevError, match='只读'):
        call(workspace, 'review_snapshots_manage', action='compact', idempotency_key='compact-one')


def test_quota_details_survive_hub_unwrap_and_exclude_private_fields(workspace, monkeypatch):
    from shared.filesystem_errors import safe_review_error_detail
    from hub.runtime import Runtime
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', 1)
    with pytest.raises(DevError) as exc:
        call(workspace, 'open_workspace', capture_baseline=True)
    details = safe_review_error_detail(exc.value.code, {**exc.value.details, 'private_root':'secret', 'records': True})
    assert 'records' not in details and 'private_root' not in details
    with pytest.raises(DevError) as unwrapped:
        Runtime.unwrap('operation', {'ok':False, 'error':{'code':'REVIEW_QUOTA', 'message':'quota', **details}}, tool='open_workspace')
    assert unwrapped.value.details['limit_bytes'] == 1
    assert unwrapped.value.details['next_reclaim_at'] is None


def test_reclaim_time_means_enough_space_not_first_small_expiry(workspace, monkeypatch):
    engine, _, root = workspace
    store = reviews.ReviewStore(engine)
    clock = 1000.0
    monkeypatch.setattr(reviews, 'time', SimpleNamespace(time=lambda:clock, monotonic=time.monotonic))
    small = call(workspace, 'open_workspace', capture_baseline=True)
    (root/'source').write_text(''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(10000)))
    big = call(workspace, 'open_workspace', capture_baseline=True)
    rewrite(store.directory/(small['baseline_ref']+'.json'), expires_at=2000)
    rewrite(store.directory/(big['baseline_ref']+'.json'), expires_at=3000)
    (root/'source').write_text('different content\n'*10000)
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', reviews.MAX_DOCUMENT_BYTES+15000)
    with pytest.raises(DevError) as exc:
        call(workspace, 'open_workspace', capture_baseline=True)
    assert exc.value.details['next_reclaim_at'] == 3000


def test_concurrent_capture_and_release_keep_shared_blob_valid(workspace):
    engine, _, root = workspace
    (root/'source').write_text('shared source\n'*10000)
    store = reviews.ReviewStore(engine)
    def one(_):
        document = store.save(reviews.capture(engine, bound(workspace)))
        assert store.load(document['id'], bound(workspace), 'baseline')['files']['source']['text'] == 'shared source\n'*10000
        store.release_baseline(document['id'], bound(workspace))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, range(12)))
    store.cleanup()
    assert not records(workspace) and not list(store.directory.glob('*.zlib'))


def test_new_apis_discovery_routing_and_scope():
    from hub.core_tools import help_result, public_call
    from shared.contracts import TOOLS
    assert help_result('read', 'reviews')['scope'] == 'read'
    assert help_result('edit', 'reviews')['scope'] == 'write'
    assert public_call('review_snapshots', {'project':'P'}) == ('read', {'project':'P','operation':'reviews','options':{}})
    name, args = public_call('review_snapshots_manage', {'project':'P','action':'compact','idempotency_key':'compact-1'})
    TOOLS[name].model.model_validate(args)
    with pytest.raises(DevError):
        help_result('edit', 'reviews', scopes={'read'})


def test_legacy_reader_fails_closed_and_rollback_export_restores_inline(workspace, tmp_path):
    from scripts.export_review_snapshots import export
    engine, _, root = workspace
    (root/'source').write_text('源代码 source\n'*10000)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    path = records(workspace)[0]
    wrapped = json.loads(path.read_text())
    assert wrapped['sha256'] != reviews.fingerprint(wrapped['payload'])
    original = store.load(first['baseline_ref'], bound(workspace), 'baseline')
    before = {p.name:p.read_bytes() for p in store.directory.iterdir()}
    output = tmp_path/'inline-export'
    assert export(engine.journal.directory, output)['records'] == 1
    inline = json.loads((output/path.name).read_text())
    assert 'storage_version' not in inline
    assert inline['sha256'] == reviews.fingerprint(inline['payload'])
    assert inline['payload'] == original
    assert {p.name:p.read_bytes() for p in store.directory.iterdir()} == before
    with pytest.raises(FileExistsError):
        export(engine.journal.directory, output)
    assert (output/path.name).exists()


@pytest.mark.parametrize('review', [None, {'max_storage_mib':True}, {'max_storage_mib':0},
    {'max_records':-1}, {'max_records':10001}, {'unexpected':1}])
def test_invalid_review_storage_config_is_rejected(tmp_path, review):
    from agent.config import validate_config
    config = {'hub_url':'https://example.test', 'device_id':'test', 'secret':'x'*40,
              'state_dir':str(tmp_path/'state'), 'review_storage':review}
    with pytest.raises(ValueError, match='review_storage'):
        validate_config(config, tmp_path/'config.json')


def test_review_storage_config_applies_without_changing_retention(workspace):
    engine = workspace[0]
    engine.config['review_storage'] = {'max_storage_mib':1024, 'max_records':512}
    first = call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(engine)
    status = store.inspect(bound(workspace))
    assert status['limit_bytes'] == 1024**3 and status['limit_records'] == 512
    document = store.load(first['baseline_ref'], bound(workspace), 'baseline')
    assert document['expires_at'] - document['created'] == reviews.BASELINE_TTL


def test_compaction_publication_failure_is_retryable_without_changing_document(workspace, monkeypatch):
    engine, _, root = workspace
    (root/'source').write_text('legacy source\n'*10000)
    store = reviews.ReviewStore(engine)
    original = store.save(reviews.capture(engine, bound(workspace)))
    path = records(workspace)[0]
    atomic_json(path, {'sha256':reviews.fingerprint(original), 'payload':original})
    before = path.read_bytes()
    publish = reviews.atomic_json
    def fail(*args):
        raise OSError('interrupted legacy conversion')
    monkeypatch.setattr(reviews, 'atomic_json', fail)
    with pytest.raises(OSError):
        store.compact(bound(workspace))
    assert path.read_bytes() == before
    monkeypatch.setattr(reviews, 'atomic_json', publish)
    store.compact(bound(workspace))
    assert store.load(original['id'], bound(workspace), 'baseline') == original
