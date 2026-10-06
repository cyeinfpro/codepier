"""Public archive acceptance: no browsers, real temporary Git, exact proof binding."""
import copy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

from scripts import release_acceptance as acceptance
from scripts.regression_plan import fingerprint


def inventory(root):
    files = {}
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file() or '.git' in path.parts or path.name == 'MANIFEST.sha256':
            continue
        raw = path.read_bytes()
        files[path.relative_to(root).as_posix()] = {
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'mode': stat.S_IMODE(path.stat().st_mode)}
    return {'inventory_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}, files


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    (root / 'module.py').write_text('value = 1\n')
    (root / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
    (root / 'run.sh').chmod(0o755)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '--', 'module.py', 'run.sh'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    _, files = inventory(root)
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        items = [(name, (root / name).read_bytes(), item['mode']) for name, item in files.items()]
        manifest = ''.join(item['sha256'] + '  ' + name + '\n' for name, item in sorted(files.items())).encode()
        items.append(('MANIFEST.sha256', manifest, 0o644))
        for name, raw, mode in items:
            entry = zipfile.ZipInfo(name, (2026, 9, 14, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            bundle.writestr(entry, raw)
    monkeypatch.setattr(acceptance, 'check_source', inventory)
    return root, archive, tmp_path / 'extracted', tmp_path / 'proof.json', commit


def test_prepare_and_finish_keep_source_and_executable_modes(source):
    root, archive, destination, proof, commit = source
    record = acceptance.prepare(root, archive, destination, proof, commit)
    assert record['commit_binding_verified'] and not record['post_regression_source_verified']
    assert (destination / 'module.py').read_bytes() == (root / 'module.py').read_bytes()
    assert stat.S_IMODE((destination / 'run.sh').stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / 'MANIFEST.sha256').stat().st_mode) == 0o644
    assert acceptance.finish(destination, archive, proof)['post_regression_source_verified']


@pytest.mark.parametrize('change', ['wrong_commit', 'dirty', 'untracked', 'inside_checkout', 'existing_destination'])
def test_prepare_rejects_unbound_or_unsafe_inputs(source, change):
    root, archive, destination, proof, commit = source
    if change == 'wrong_commit':
        commit = '0' * 40
    elif change == 'dirty':
        (root / 'module.py').write_text('value = 2\n')
    elif change == 'untracked':
        (root / 'unexpected.py').write_text('value = 3\n')
    elif change == 'inside_checkout':
        destination = root / 'nested'
    else:
        destination.mkdir()
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        acceptance.prepare(root, archive, destination, proof, commit)
    assert not proof.exists()


@pytest.mark.parametrize('change', ['source', 'mode', 'manifest', 'manifest_mode', 'archive'])
def test_finish_rejects_changes_and_invalidates_old_success(source, change):
    root, archive, destination, proof, commit = source
    acceptance.prepare(root, archive, destination, proof, commit)
    acceptance.finish(destination, archive, proof)
    if change == 'source':
        (destination / 'module.py').write_text('value = 2\n')
    elif change == 'mode':
        (destination / 'run.sh').chmod(0o644)
    elif change == 'manifest':
        (destination / 'MANIFEST.sha256').write_text('stale\n')
    elif change == 'manifest_mode':
        (destination / 'MANIFEST.sha256').chmod(0o600)
    else:
        archive.write_bytes(b'not a zip')
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        acceptance.finish(destination, archive, proof)
    assert not json.loads(proof.read_text())['post_regression_source_verified']


def reports(tmp_path):
    commit = 'a' * 40
    files = {'module.py': {'sha256': 'b' * 64, 'bytes': 4, 'mode': 0o644}}
    source = {'module.py': 'b' * 64}
    nodes = ['tests/test_example.py::test_' + str(i) for i in range(4)]
    proof = {'schema': 1, 'commit': commit, 'files': files, 'archive_sha256': 'c' * 64,
             'source_inventory_sha256': 'd' * 64, 'commit_binding_verified': True,
             'post_regression_source_verified': True}
    paths = []
    for i in range(4):
        directory = tmp_path / str(i)
        directory.mkdir()
        summary = {'verified': True, 'shard': {'index': i, 'count': 4}, 'full_collection': nodes,
                   'collection_sha256': fingerprint(nodes), 'source_inventory_sha256': fingerprint(source),
                   'outcomes': {nodes[i]: 'passed'}}
        path = directory / 'summary.json'
        for name, value in [('summary.json', summary), ('archive-proof.json', proof),
                            ('source-before.json', source), ('source-after.json', source)]:
            acceptance.write_json(directory / name, value)
        paths.append(path)
    return paths, commit


def test_four_complete_archive_shards_bind_one_commit(tmp_path):
    paths, commit = reports(tmp_path)
    assert acceptance.verify_reports(paths, commit)['verified']


@pytest.mark.parametrize('change', ['missing', 'wrong_commit', 'unfinished', 'other_archive',
                                    'foreign_source', 'changed_source', 'failed_shard', 'duplicate_shard'])
def test_aggregate_rejects_missing_stale_or_mixed_evidence(tmp_path, change):
    paths, commit = reports(tmp_path)
    if change == 'missing':
        paths.pop()
    elif change == 'duplicate_shard':
        paths[3] = paths[0]
    elif change == 'wrong_commit':
        commit = 'f' * 40
    else:
        name = ('summary.json' if change == 'failed_shard' else
                'source-after.json' if change == 'changed_source' else 'archive-proof.json')
        path = paths[0].parent / name
        value = json.loads(path.read_text())
        if change == 'unfinished':
            value['post_regression_source_verified'] = False
        elif change == 'other_archive':
            value['archive_sha256'] = 'e' * 64
        elif change == 'foreign_source':
            value['files']['module.py']['sha256'] = 'e' * 64
        elif change == 'changed_source':
            value['module.py'] = 'e' * 64
        else:
            value['verified'] = False
        acceptance.write_json(path, value)
    with pytest.raises(ValueError):
        acceptance.verify_reports(paths, commit)


def test_workflow_preserves_matrix_and_runs_extracted_source():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
    for fragment in ['os: [ubuntu-24.04, macos-15]', 'shard: [0, 1, 2, 3]',
                     'windows-core:', 'oidc-authentik:', 'release_acceptance.py prepare',
                     'release_acceptance.py finish', 'release_acceptance.py verify',
                     'cd "$ACCEPTANCE"', 'MCP_COMPAT_PYTHON:', '--coverage',
                     'dist/ci-results/archive-proof.json']:
        assert fragment in workflow
