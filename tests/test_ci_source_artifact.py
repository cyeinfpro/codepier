"""CI graph and source-build provenance use real scripts with synthetic inputs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts.ci_source_artifact import source_build_record

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']


def test_source_build_is_shared_and_static_checks_still_cover_both_platforms():
    jobs = workflow()
    assert jobs['verify']['strategy']['matrix']['shard'] == list(range(6))
    assert jobs['verify']['needs'] == ['promotion', 'source']
    source_steps = jobs['source']['steps']
    verify_steps = jobs['verify']['steps']
    source = '\n'.join(step.get('run', '') for step in source_steps)
    assert 'sha256sum --check' in source
    assert 'scripts/ci_source_artifact.py' in source
    assert '-m ruff check' in source and '-m mypy' in source
    download = next(step for step in verify_steps if step.get('name') == 'Download the single verified public source build')
    assert download['with'] == {'name': 'public-source', 'path': 'dist'}
    archive = next(step for step in verify_steps if step.get('id') == 'archive')
    assert 'build_source_bundle.py --public' not in archive['run']
    assert 'release_acceptance.py prepare' in archive['run']
    for name in ('Verify reproducible browser build and release inputs', 'Static and type checks',
                 'Dependency advisories (one copy per OS)'):
        assert next(s for s in verify_steps if s.get('name') == name)['if'] == "runner.os == 'macOS' && matrix.shard == 0"
    regression = next(s for s in verify_steps if s.get('name') == 'Exhaustive deterministic shard from the verified public archive')
    assert regression['env']['CODEPIER_LONG_BUDGET_TEST'] == '1'
    assert '--shard-count 6' in regression['run']
    assert '--coverage' in regression['run'] and '--timeout 2400' in regression['run']


@pytest.mark.parametrize('reused', ['true', 'false'])
@pytest.mark.parametrize('failed', [None, 'PROMOTION_RESULT', 'SOURCE_RESULT', 'WINDOWS_RESULT',
                                    'VERIFY_RESULT', 'COMPLETE_RESULT', 'OIDC_RESULT'])
def test_release_gate_requires_the_selected_complete_path(reused, failed):
    run = workflow()['release-gate']['steps'][0]['run']
    env = dict(os.environ, REUSED=reused)
    keys = ['PROMOTION_RESULT', 'SOURCE_RESULT', 'WINDOWS_RESULT', 'VERIFY_RESULT', 'COMPLETE_RESULT', 'OIDC_RESULT']
    env.update({key: 'skipped' if reused == 'true' and key != 'PROMOTION_RESULT' else 'success' for key in keys})
    if failed:
        env[failed] = 'failure'
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', run], env=env, capture_output=True, timeout=5)
    accepted = failed is None
    assert (result.returncode == 0) == accepted


@pytest.fixture
def record_inputs(tmp_path, monkeypatch):
    root, directory = tmp_path / 'checkout', tmp_path / 'dist'
    (root / '.github/workflows').mkdir(parents=True)
    (root / '.github/workflows/ci.yml').write_text('name: Synthetic CI\n')
    directory.mkdir()
    for suffix in ['-full.zip', '-full.manifest.json', '.zip', '.manifest.json']:
        (directory / ('codepier-1.2.3-source' + suffix)).write_bytes(b'synthetic build asset')
    check = {'passed': True, 'version': '1.2.3', 'inventory_sha256': 'b' * 64,
             'verified_against_current_source': True}
    for name in ['public-check.json', 'panel-update-check.json']:
        (directory / name).write_text(json.dumps(check))
    monkeypatch.setattr(subprocess, 'check_output', lambda args, **kwargs: 'c' * 40 if args[-1] == 'HEAD^{tree}' else 'a' * 40)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: None)
    from shared.runtime_qualification import CHECKS
    qualification = {'schema_version': 1, 'backend': 'docker_restricted_v1', 'verified': True,
        'commit': 'a'*40, 'archive_sha256': hashlib.sha256(b'synthetic build asset').hexdigest(),
        'source_inventory_sha256': 'b'*64, 'source_files_unchanged': True,
        'cleanup_pending': False, 'checks': {name: True for name in CHECKS}}
    (directory / 'runtime-acceptance.json').write_text(json.dumps(qualification))
    context = {'GITHUB_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_RUN_ID': '12',
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
    return root, directory, context


def test_build_record_binds_workflow_tree_run_and_exact_assets(record_inputs):
    root, directory, context = record_inputs
    record = source_build_record(root, directory, context)
    assert record['commit'] == 'a' * 40 and record['tree'] == 'c' * 40
    assert record['run_id'] == 12 and record['run_attempt'] == 1
    assert record['workflow_sha256'] == hashlib.sha256((root / '.github/workflows/ci.yml').read_bytes()).hexdigest()
    assert len(record['assets']) == 4
    assert all(asset['sha256'] == hashlib.sha256(b'synthetic build asset').hexdigest() for asset in record['assets'].values())


@pytest.mark.parametrize('change', ['wrong_sha', 'failed_check', 'missing_asset', 'extra_asset', 'symlink', 'mixed_version'])
def test_build_record_rejects_unbound_inputs(record_inputs, change):
    root, directory, context = record_inputs
    if change == 'wrong_sha':
        context['GITHUB_SHA'] = 'f' * 40
    elif change == 'failed_check':
        (directory / 'public-check.json').write_text('{"passed": false}')
    elif change == 'missing_asset':
        (directory / 'codepier-1.2.3-source-full.zip').unlink()
    elif change == 'extra_asset':
        (directory / 'codepier-1.2.3-source-other.zip').write_text('extra')
    elif change == 'mixed_version':
        path = directory / 'codepier-1.2.3-source-full.zip'
        path.rename(directory / 'codepier-4.5.6-source-full.zip')
    else:
        path = directory / 'codepier-1.2.3-source-full.zip'
        path.unlink()
        path.symlink_to(directory / 'codepier-1.2.3-source.zip')
    with pytest.raises(ValueError):
        source_build_record(root, directory, context)



def test_source_job_cannot_skip_or_ignore_real_runtime_qualification():
    steps = workflow()['source']['steps']
    gate = next(step for step in steps if step.get('name') == 'Mandatory real project-container acceptance from public archive')
    assert 'if' not in gate and 'continue-on-error' not in gate
    assert 'release_acceptance.py prepare' in gate['run'] and 'release_acceptance.py finish' in gate['run']
    assert 'scripts/verify_project_runtime.py' in gate['run'] and '--commit "$GITHUB_SHA"' in gate['run']
    assert gate['run'].index('scripts/verify_project_runtime.py') < gate['run'].index('scripts/ci_source_artifact.py')
    artifact = next(step for step in steps if step.get('name') == 'Retain immutable source inputs for every platform')
    assert 'dist/runtime-acceptance.json' in artifact['with']['path']


@pytest.mark.parametrize('field,value', [('verified', False), ('checks', {}), ('commit', 'c'*40), ('cleanup_pending', True)])
def test_source_record_rejects_missing_or_foreign_runtime_acceptance(record_inputs, field, value):
    root, directory, context = record_inputs
    path = directory / 'runtime-acceptance.json'
    proof = json.loads(path.read_text()); proof[field] = value; path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match='Real container acceptance'):
        source_build_record(root, directory, context)


@pytest.mark.parametrize('prefix', ['', '# syntax=docker/dockerfile:1\n\n# pinned image\n', '\n\n'])
def test_container_qualification_reads_pinned_base_after_comments(prefix):
    from shared.runtime_qualification import pinned_base_image
    image = 'python:3.13-slim@sha256:' + 'a'*64
    assert pinned_base_image(prefix+'FROM '+image+' AS base\nRUN true') == image
    assert pinned_base_image(prefix+'from --platform=$BUILDPLATFORM '+image+' AS base') == image
    assert pinned_base_image((ROOT/'Dockerfile').read_text()).startswith('python:')


@pytest.mark.parametrize('source', ['# FROM ignored\n', 'FROM python:latest', 'FROM scratch\nFROM python@sha256:'+'a'*64])
def test_container_qualification_rejects_mutable_or_missing_first_base(source):
    from shared.runtime_qualification import pinned_base_image
    with pytest.raises(ValueError):
        pinned_base_image(source)
