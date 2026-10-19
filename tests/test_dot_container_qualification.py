"""Portable qualification-contract tests; these do not claim real Docker proof."""
from copy import deepcopy
import hashlib
import json
import os
import stat
import zipfile

import pytest

from scripts import verify_project_runtime as qualification
from shared.runtime_qualification import CHECKS, verified_receipt
from shared.util import DevError


def archive_fixture(path, files):
    contents = {name: data for name, data in files.items()}
    contents['MANIFEST.sha256'] = ''.join(
        hashlib.sha256(files[name]).hexdigest() + '  ' + name + '\n' for name in sorted(files)).encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in contents.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, content)


@pytest.fixture
def qualification_inputs(tmp_path, monkeypatch):
    source = tmp_path / 'source.py'
    source.write_bytes(b'print("synthetic source")\n')
    source.chmod(0o644)
    archive = tmp_path / 'source.zip'
    archive_fixture(archive, {'source.py': source.read_bytes()})
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'mode': 'container_v1'}))
    output = tmp_path / 'receipt.json'
    scratch = tmp_path / 'scratch'
    scratch.mkdir()

    def inventory(_):
        raw = source.read_bytes()
        return {'source.py': {'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'mode': stat.S_IMODE(source.stat().st_mode)}}

    monkeypatch.setattr(qualification, 'public_files', inventory)
    arguments = ['--profile-json', str(profile), '--output', str(output), '--archive', str(archive),
        '--commit', 'a' * 40, '--scratch-root', str(scratch)]
    return source, archive, output, scratch, arguments


def runtime_checks():
    return {name: True for name in CHECKS if name != 'archive_matches_executed_source'}


@pytest.mark.parametrize('change', ['content', 'replaced', 'mode', 'symlink'])
def test_canary_integrity_catches_write_replacement_and_link_changes(tmp_path, change):
    root = tmp_path / 'project'
    root.mkdir()
    targets, originals = qualification.prepare_canaries(tmp_path, root)
    assert len(targets) == 6 and len(originals) == 2
    assert all(not path.is_relative_to(root) for path in originals)
    assert len(list(root.iterdir())) == 4
    qualification.assert_canaries_unchanged(originals)
    path = next(iter(originals))
    if change == 'content':
        path.write_text('modified')
    elif change == 'replaced':
        copy = path.with_name('replacement')
        copy.write_bytes(path.read_bytes())
        copy.replace(path)
    elif change == 'mode':
        path.chmod(0o444)
    else:
        other = path.with_name('link-target')
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    with pytest.raises(AssertionError):
        qualification.assert_canaries_unchanged(originals)


def test_boundary_probe_fails_when_a_synthetic_external_file_is_accessible(tmp_path):
    outside = tmp_path / 'canary.txt'
    outside.write_text('harmless fixture')
    source = qualification.boundary_source([str(outside)], '/synthetic/no-docker.sock')
    with pytest.raises(AssertionError, match='outside fixture became accessible'):
        exec(compile(source, '<boundary-fixture>', 'exec'), {})
    assert outside.read_text() == 'harmless fixture'


def test_canary_parents_remain_traversable_under_restrictive_umask(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    previous = os.umask(0o077)
    try:
        _, originals = qualification.prepare_canaries(tmp_path, root)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    for canary in originals:
        assert stat.S_IMODE(canary.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(canary.stat().st_mode) == 0o666


@pytest.mark.asyncio
async def test_original_id_survives_first_real_agent_runtime_failure(tmp_path, monkeypatch):
    from agent.journal import Journal
    profile = {'mode': 'container_v1', 'shell': 'sh', 'container': {
        'engine': '/synthetic/docker', 'endpoint': 'unix:///synthetic/docker.sock',
        'image': 'sha256:' + 'a' * 64}}
    monkeypatch.setattr('agent.container_runtime.readiness', lambda _: {
        'state': 'unavailable', 'code': 'RUNTIME_ENGINE_UNAVAILABLE'})
    observations = {}
    with pytest.raises(AssertionError):
        await qualification.verify_container(profile, tmp_path, observations=observations)
    assert observations['operations'] == [], 'no container was verified'
    original_ids = observations['original_operation_ids']
    assert len(original_ids) == len(set(original_ids)) == 2
    recovered = Journal(tmp_path / 'state')
    try:
        assert recovered.status(original_ids[0])['result']['ok'] is True
        failed = recovered.status(original_ids[1])
        assert failed['result']['error']['code'] == 'RUNTIME_ENGINE_UNAVAILABLE'
        assert recovered.status(original_ids[1]) == failed
    finally:
        recovered.db.close()


def test_archive_must_match_running_source_before_any_container_call(qualification_inputs, monkeypatch):
    source, archive, output, scratch, arguments = qualification_inputs
    archive_fixture(archive, {'source.py': b'print("different source")\n'})

    async def forbidden(*args, **kwargs):
        pytest.fail('Mismatched source must fail before starting any runtime')

    monkeypatch.setattr(qualification, 'verify_container', forbidden)
    with pytest.raises(ValueError, match='differs from current source'):
        qualification.main(arguments)
    receipt = json.loads(output.read_text())
    assert receipt['verified'] is False
    assert receipt['checks'] == {} and receipt['archive_sha256'] is None
    assert list(scratch.iterdir()) == []
    assert source.read_bytes() == b'print("synthetic source")\n'


def test_backend_failure_preserves_original_control_receipts_without_retry(qualification_inputs, monkeypatch):
    _, _, output, scratch, arguments = qualification_inputs
    calls = []

    async def uncertain(profile, directory, *, observations):
        calls.append(directory)
        (directory / 'original-operation.json').write_text('{"state":"creation_unconfirmed"}')
        observations['operations'] = [{'operation_id': 'original', 'state': 'creation_unconfirmed'}]
        observations['original_operation_ids'] = ['original']
        raise DevError('RUNTIME_CREATION_UNCERTAIN', 'synthetic lost create response', 409)

    monkeypatch.setattr(qualification, 'verify_container', uncertain)
    with pytest.raises(DevError):
        qualification.main(arguments)
    receipt = json.loads(output.read_text())
    assert len(calls) == 1 and list(scratch.iterdir()) == calls
    assert (calls[0] / 'original-operation.json').is_file()
    assert receipt['verified'] is False and receipt['cleanup_pending'] is True
    assert receipt['fixture_retained'] is True
    assert receipt['runtime_observations']['operations'][0]['operation_id'] == 'original'
    assert receipt['runtime_observations']['original_operation_ids'] == ['original']
    assert receipt['runtime_observations']['fixture_directory'] == str(calls[0])
    with pytest.raises(SystemExit):
        qualification.main(arguments)
    assert len(calls) == 1, 'an existing proof path cannot restart the original execution'


def test_success_requires_all_checks_and_matching_archive_then_removes_fixture(qualification_inputs, monkeypatch):
    _, archive, output, scratch, arguments = qualification_inputs

    async def completed(profile, directory, *, observations):
        assert directory.parent == scratch and directory.is_dir()
        observations['operations'] = [{'operation_id': 'synthetic-confirmed'}]
        return runtime_checks()

    monkeypatch.setattr(qualification, 'verify_container', completed)
    assert qualification.main(arguments) == 0
    receipt = json.loads(output.read_text())
    assert set(receipt['checks']) == CHECKS and list(scratch.iterdir()) == []
    assert verified_receipt(receipt, commit='a' * 40,
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        source_inventory_sha256=receipt['source_inventory_sha256'])


@pytest.mark.parametrize('change', ['source', 'archive', 'missing-check'])
def test_mixed_source_or_incomplete_result_never_becomes_qualified(qualification_inputs, monkeypatch, change):
    source, archive, output, scratch, arguments = qualification_inputs

    async def changed(profile, directory, *, observations):
        checks = runtime_checks()
        if change == 'source':
            source.write_bytes(b'changed while qualification ran')
        elif change == 'archive':
            archive_fixture(archive, {'source.py': b'other archive'})
        else:
            checks.pop('cancellation_enforced')
        return checks

    monkeypatch.setattr(qualification, 'verify_container', changed)
    if change == 'archive':
        with pytest.raises(ValueError):
            qualification.main(arguments)
    else:
        assert qualification.main(arguments) == 1
    receipt = json.loads(output.read_text())
    assert receipt['verified'] is False and receipt['fixture_retained'] is True
    assert len(list(scratch.iterdir())) == 1


@pytest.mark.parametrize('missing', ['host_absolute_path_denied', 'sibling_project_denied',
    'cross_boundary_link_denied', 'management_socket_denied', 'cancellation_enforced',
    'original_operation_query_safe', 'archive_matches_executed_source'])
def test_shared_receipt_consumers_reject_each_missing_new_check(missing):
    receipt = {'schema_version': 1, 'backend': 'docker_restricted_v1', 'verified': True,
        'commit': 'a' * 40, 'archive_sha256': 'b' * 64, 'source_inventory_sha256': 'c' * 64,
        'cleanup_pending': False, 'source_files_unchanged': True,
        'checks': {name: True for name in CHECKS}}
    expected = {'commit': 'a' * 40, 'archive_sha256': 'b' * 64, 'source_inventory_sha256': 'c' * 64}
    assert verified_receipt(receipt, **expected)
    incomplete = deepcopy(receipt)
    del incomplete['checks'][missing]
    assert not verified_receipt(incomplete, **expected)
    incomplete['checks'][missing] = 1
    assert not verified_receipt(incomplete, **expected), 'truthy numbers are not observed passing checks'


def test_interrupted_mutation_recovers_original_journal_result_without_replay(tmp_path):
    from agent.journal import Journal
    state = tmp_path / 'journal'
    request = {'tool': 'exec', 'project': {'root': str(tmp_path / 'project')},
        'args': {'command': 'synthetic-side-effect', 'idempotency_key': 'original-key'}}
    journal = Journal(state)
    assert journal.start('original', request) is None
    journal.mark_running('original')
    journal.update_output('original', 'work started', 1)
    journal.db.close()
    recovered = Journal(state)
    try:
        before = recovered.status('original')
        assert before['status'] == 'interrupted'
        assert before['result']['error']['code'] == 'INTERRUPTED'
        for _ in range(3):
            assert recovered.status('original') == before
            assert recovered.start('original', request) == before['result']
        assert recovered.status('original') == before
        assert before['output'] == 'work started' and before['output_seq'] == 1
    finally:
        recovered.db.close()
