"""Quota recovery, immutable finalization and expiry using real private files."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import agent.coding_reviews as reviews
from agent.chat_reviews import TurnReviews
from shared.util import DevError
from tests.test_agentdock_context import workspace
from tests.test_coding_workflow import call


def bound(workspace):
    return {**workspace[1], '_coding_owner': 'grant:test', '_coding_device': 'test-device'}


def records(workspace):
    return list((workspace[0].journal.directory / 'coding-reviews').glob('*.json'))


def rewrite(path, **changes):
    document, _ = reviews.ReviewStore._read(path)
    document.update(changes)
    wrapped, _, _ = reviews.ReviewStore._pack(document)
    path.write_text(json.dumps(wrapped))


def native_reviews(workspace):
    engine, project, root = workspace
    engine.config_path.write_text(json.dumps(engine.config))
    row = {'id': 'session', 'project_id': project['id'], 'device_id': 'node', 'root': str(root)}
    return TurnReviews(engine.journal.directory/'native-cli', row, engine.config_path)


def test_new_baseline_lease_and_legacy_expiry_ignore_mtime(workspace):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    path = records(workspace)[0]
    data = json.loads(path.read_text())['payload']
    assert data['expires_at'] - data['created'] == reviews.BASELINE_TTL
    # A legacy baseline retains the original seven-day promise, even if copied
    # with an ancient mtime. Expired content is removed even with a fresh mtime.
    expires = time.time() + reviews.TTL
    rewrite(path, expires_at=expires)
    os.utime(path, (1, 1))
    store = reviews.ReviewStore(workspace[0])
    store.cleanup()
    assert store.load(baseline['baseline_ref'], bound(workspace), 'baseline')['expires_at'] == expires
    rewrite(path, expires_at=time.time()-1)
    store.cleanup()
    assert not path.exists()


def test_duplicate_review_reuses_original_interval_and_expiry(workspace, monkeypatch):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    (workspace[2]/'a.txt').write_text('first change')
    first = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'])
    monkeypatch.setattr(reviews, 'MAX_RECORDS', 2)
    second = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'])
    assert second == first
    assert len(records(workspace)) == 2
    (workspace[2]/'a.txt').write_text('second change')
    with pytest.raises(DevError, match='配额'):
        call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'])
    monkeypatch.setattr(reviews, 'MAX_RECORDS', 3)
    third = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'])
    assert third['review_ref'] != first['review_ref']


def test_finalization_fits_by_replacing_baseline_and_replays_without_recapture(workspace, monkeypatch):
    import hashlib
    (workspace[2]/'large.txt').write_text(''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(1000)))
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    original_size = sum(p.stat().st_size for p in records(workspace)[0].parent.iterdir() if p.suffix in {'.json', '.zlib'})
    monkeypatch.setattr(reviews, 'MAX_RECORDS', 1)
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', original_size)
    final = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)
    assert final['baseline_released'] and final['review_ref'] == baseline['baseline_ref']
    assert len(records(workspace)) == 1 and records(workspace)[0].stat().st_size < original_size
    stored = json.loads(records(workspace)[0].read_text())['payload']
    assert stored['expires_at'] - stored['created'] == reviews.TTL
    (workspace[2]/'large.txt').write_text('later unrelated content')
    monkeypatch.setattr(reviews, 'capture', lambda *a: pytest.fail('Finalized retries must not recapture'))
    assert call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True) == final
    assert call(workspace, 'show_changes', review_ref=final['review_ref']) == final
    with pytest.raises(DevError):
        reviews.ReviewStore(workspace[0]).release_baseline(baseline['baseline_ref'], bound(workspace))
    assert records(workspace)


def test_failed_atomic_finalization_keeps_baseline_for_retry(workspace, monkeypatch):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    path = records(workspace)[0]
    before = path.read_bytes()
    original = reviews.atomic_json
    def failure(*args):
        raise OSError('injected write failure before replacement')
    monkeypatch.setattr(reviews, 'atomic_json', failure)
    with pytest.raises(OSError):
        call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)
    assert path.read_bytes() == before
    monkeypatch.setattr(reviews, 'atomic_json', original)
    assert call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)['baseline_released']


def test_finalization_replay_after_publication_error_keeps_original_result(workspace, monkeypatch):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    original = reviews.atomic_json
    def publish_then_fail(*args):
        original(*args)
        raise OSError('injected failure after atomic replacement')
    monkeypatch.setattr(reviews, 'atomic_json', publish_then_fail)
    with pytest.raises(OSError):
        call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)
    before = records(workspace)[0].read_bytes()
    monkeypatch.setattr(reviews, 'capture', lambda *a: pytest.fail('Must recover durable result'))
    assert call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)['baseline_released']
    assert records(workspace)[0].read_bytes() == before


def test_finalization_reauthorizes_even_when_replaying(workspace):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    final = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)
    before = records(workspace)[0].read_bytes()
    for override in ({'_coding_owner': 'grant:other'}, {'_coding_device': 'other'}, {'id': 'other'}):
        with pytest.raises(DevError) as exc:
            reviews.freeze_review(workspace[0], {**bound(workspace), **override}, baseline['baseline_ref'], finalize=True)
        assert exc.value.code == 'REVIEW_NOT_FOUND'
    assert records(workspace)[0].read_bytes() == before
    assert call(workspace, 'show_changes', review_ref=final['review_ref'])['immutable']


def test_concurrent_finalization_publishes_one_result(workspace, monkeypatch):
    baseline = call(workspace, 'open_workspace', capture_baseline=True)
    (workspace[2]/'changed').write_text('result')
    original = reviews.capture
    barrier = threading.Barrier(2)
    def capture(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(reviews, 'capture', capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reviews.freeze_review, workspace[0], bound(workspace), baseline['baseline_ref'], finalize=True) for _ in range(2)]
        first, second = [future.result(timeout=10) for future in futures]
    assert first == second and len(records(workspace)) == 1


def test_native_completion_works_at_record_limit(workspace, monkeypatch):
    native = native_reviews(workspace)
    native.begin('turn')
    monkeypatch.setattr(reviews, 'MAX_RECORDS', 1)
    (workspace[2]/'changed').write_text('result')
    final = native.finish('turn')
    assert final['available'] and final['baseline_released']
    assert len(records(workspace)) == 1
    assert json.loads(records(workspace)[0].read_text())['payload']['kind'] == 'review'


def test_native_failed_settled_turn_releases_only_its_baseline(workspace, monkeypatch):
    other = call(workspace, 'open_workspace', capture_baseline=True)
    native = native_reviews(workspace)
    native.begin('turn')
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', 1)
    final = native.finish('turn')
    assert not final['available'] and final['reason'] == 'REVIEW_QUOTA'
    assert [p.stem for p in records(workspace)] == [other['baseline_ref']]


def test_new_baselines_leave_completion_headroom(workspace, monkeypatch):
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', reviews.MAX_DOCUMENT_BYTES)
    with pytest.raises(DevError) as exc:
        call(workspace, 'open_workspace', capture_baseline=True)
    assert exc.value.code == 'REVIEW_QUOTA' and not records(workspace)


def test_failed_mcp_completion_preserves_baseline_without_evicting_other_reviews(workspace, monkeypatch):
    first = call(workspace, 'open_workspace', capture_baseline=True)
    call(workspace, 'show_changes', baseline_ref=first['baseline_ref'], finalize=True)
    second = call(workspace, 'open_workspace', capture_baseline=True)
    before = {p: p.read_bytes() for p in records(workspace)}
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', 1)
    with pytest.raises(DevError) as exc:
        call(workspace, 'show_changes', baseline_ref=second['baseline_ref'], finalize=True)
    assert exc.value.code == 'REVIEW_QUOTA'
    assert {p: p.read_bytes() for p in records(workspace)} == before


def test_cleanup_metadata_cache_does_not_reparse_unchanged_source(workspace, monkeypatch):
    call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(workspace[0])
    store.cleanup()
    monkeypatch.setattr(reviews.ReviewStore, '_read', staticmethod(lambda *a: pytest.fail('Unchanged snapshots need only stat')))
    store.cleanup()


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_cleanup_preserves_linked_external_files(workspace, tmp_path, link_kind):
    call(workspace, 'open_workspace', capture_baseline=True)
    path = records(workspace)[0]
    rewrite(path, expires_at=time.time()-1)
    external = tmp_path/'external.json'
    path.rename(external)
    try:
        if link_kind == 'symlink':
            path.symlink_to(external)
        else:
            os.link(external, path)
    except OSError:
        pytest.skip('Links unavailable on this filesystem')
    before = external.read_bytes()
    with pytest.raises(DevError) as exc:
        reviews.ReviewStore(workspace[0]).cleanup()
    assert exc.value.code == 'REVIEW_STORAGE_INVALID'
    assert external.read_bytes() == before and path.exists()


def test_metadata_cache_cannot_hide_tampering_or_extend_expiry(workspace):
    call(workspace, 'open_workspace', capture_baseline=True)
    store = reviews.ReviewStore(workspace[0])
    store.cleanup()  # Warm cache.
    path = records(workspace)[0]
    raw = path.read_text()
    modified = raw.replace('"kind": "baseline"', '"kind": "tampered"')
    assert len(modified) == len(raw)
    old_stat = path.stat()
    path.write_text(modified)
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(DevError) as exc:
        store.cleanup()
    assert exc.value.code == 'REVIEW_INTEGRITY' and path.exists()


def test_cleanup_watcher_runs_without_new_requests_and_stops(workspace, monkeypatch):
    call(workspace, 'open_workspace', capture_baseline=True)
    path = records(workspace)[0]
    rewrite(path, expires_at=time.time()-1)
    monkeypatch.setattr(reviews, 'CLEANUP_INTERVAL', 0.01)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(reviews.watch_cleanup(workspace[0], stop))
        try:
            for _ in range(100):
                if not path.exists():
                    break
                await asyncio.sleep(0.01)
            assert not path.exists()
        finally:
            stop.set()
            await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_finalize_option_is_exposed_in_public_discovery_and_validated():
    from hub.core_tools import help_result, public_call
    from shared.coding_contracts import ShowChanges
    from pydantic import ValidationError
    name, args = public_call('show_changes', {'project': 'P', 'baseline_ref': 'a'*32, 'finalize': True})
    assert name == 'read' and args['options']['finalize']
    schema = help_result('read', 'changes')['inputSchema']
    assert schema['properties']['finalize']['default'] is False
    with pytest.raises(ValidationError):
        ShowChanges(project='P', review_ref='a'*32, finalize=True)


def test_finalization_requires_matching_peer_semantics():
    from shared.tool_protocol import advertisement, negotiate, require_compatible, validate_call_epoch
    current = advertisement('a'*64)
    require_compatible(negotiate(current), 'show_changes')
    validate_call_epoch('show_changes', 2)
    current['tool_contracts']['show_changes'] = 1
    for peer in (negotiate(current), negotiate({'version': '1.30.7'})):
        with pytest.raises(DevError) as exc:
            require_compatible(peer, 'show_changes')
        assert exc.value.code == 'AGENT_UPGRADE_REQUIRED'
    with pytest.raises(DevError) as exc:
        validate_call_epoch('show_changes', 1)
    assert exc.value.code == 'TOOL_PROTOCOL_MISMATCH'


def test_review_captures_root_page_before_large_artwork_directory(workspace, monkeypatch):
    engine, project, root = workspace
    (root/'artworks').mkdir()
    (root/'artworks'/'old.svg').write_text('x'*100)
    (root/'index.html').write_text('page')
    monkeypatch.setattr(reviews, 'MAX_SOURCE_BYTES', 50)
    snapshot = reviews.capture(engine, bound(workspace))
    assert snapshot['files']['index.html']['text'] == 'page'
    assert snapshot['coverage']['truncated'] and not snapshot['coverage']['complete']
    # General tree ordering remains unchanged for callers that did not opt in.
    assert next(engine.walk(root, root))[1] == 'artworks'


def test_review_excludes_internal_scratch_without_hiding_other_source(workspace):
    engine, project, root = workspace
    for name in ('.work', '.codepier-test-runs', 'work'):
        (root/name).mkdir()
        (root/name/'source.py').write_text(name)
    snapshot = reviews.capture(engine, bound(workspace))
    assert set(snapshot['files']) == {'work/source.py'}
    assert snapshot['coverage']['complete']


def test_legacy_scratch_in_baseline_is_not_reported_as_deleted(workspace, monkeypatch):
    engine, project, root = workspace
    (root/'.work').mkdir()
    (root/'.work'/'old.txt').write_text('still on disk')
    (root/'source.txt').write_text('source')
    with monkeypatch.context() as legacy:
        legacy.setattr(reviews, 'review_excluded', reviews.excluded)
        baseline = call(workspace, 'open_workspace', capture_baseline=True)
    (root/'source.txt').write_text('new source')
    final = call(workspace, 'show_changes', baseline_ref=baseline['baseline_ref'], finalize=True)
    assert [item['path'] for item in final['files']] == ['source.txt']
    assert (root/'.work'/'old.txt').read_text() == 'still on disk'


def test_expiry_during_finalization_cannot_double_subtract_baseline(workspace, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(reviews, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    (workspace[2]/'large.txt').write_text('x'*80000)
    first = call(workspace, 'open_workspace', capture_baseline=True)
    call(workspace, 'open_workspace', capture_baseline=True)
    for path in records(workspace):
        rewrite(path, expires_at=clock[0]+1)
    before = {p: p.read_bytes() for p in records(workspace)}
    monkeypatch.setattr(reviews, 'MAX_STORAGE_BYTES', len(next(iter(before.values())))-1)
    original = reviews.ReviewStore._metadata
    def metadata(*args):
        clock[0] = 1002.0
        return original(*args)
    # Simulate a scan crossing expiry after the replacement was authorized.
    monkeypatch.setattr(reviews.ReviewStore, '_metadata', staticmethod(metadata))
    with pytest.raises(DevError) as exc:
        call(workspace, 'show_changes', baseline_ref=first['baseline_ref'], finalize=True)
    assert exc.value.code == 'REVIEW_QUOTA'
    assert {p: p.read_bytes() for p in records(workspace)} == before


def test_background_cleanup_skips_store_lock_held_by_writer(workspace):
    call(workspace, 'open_workspace', capture_baseline=True)
    path = records(workspace)[0]
    rewrite(path, expires_at=time.time()-1)
    store = reviews.ReviewStore(workspace[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.lock():
            pool.submit(store.cleanup).result(timeout=2)
            assert path.exists()
    store.cleanup()
    assert not path.exists()


@pytest.mark.parametrize('cancel_count', [1, 2, 3])
def test_cleanup_cancellation_waits_for_filesystem_writer(workspace, monkeypatch, cancel_count):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow_cleanup(self):
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
    monkeypatch.setattr(reviews.ReviewStore, 'cleanup', slow_cleanup)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(reviews.watch_cleanup(workspace[0], stop))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            for _ in range(cancel_count):
                task.cancel()
                # Deliver cancellation and allow both shield/gather continuations.
                for _ in range(3):
                    await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        assert finished.is_set()
    asyncio.run(scenario())
