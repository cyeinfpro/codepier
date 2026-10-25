"""Execute the disposable resource-job layout without changing OS services."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def steps():
    return yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']['verify']['steps']


def named(name):
    return next(step for step in steps() if step.get('name') == name)


def test_resource_layout_moves_generated_files_before_dependency_install(tmp_path):
    setup = named('Isolate macOS resource workspace from Spotlight indexing')
    assert setup['if'] == "runner.os == 'macOS' && matrix.shard == 4"
    workspace = tmp_path / 'checkout with spaces'
    temporary = tmp_path / 'runner temp'
    workspace.mkdir()
    temporary.mkdir()
    env_file = tmp_path / 'github-env'
    env_file.touch()
    env = {**os.environ, 'RUNNER_TEMP': str(temporary), 'GITHUB_WORKSPACE': str(workspace),
           'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_ENV': str(env_file)}
    subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', setup['run']], env=env, check=True)
    values = dict(line.split('=', 1) for line in env_file.read_text().splitlines())
    scratch = Path(values['CODEPIER_RESOURCE_SCRATCH'])
    assert scratch.name == 'codepier-resource-123-2.noindex'
    assert (workspace / 'dist').is_symlink()
    assert (workspace / 'dist').resolve() == scratch / 'dist'
    assert Path(values['TMPDIR']).is_dir() and Path(values['PIP_CACHE_DIR']).is_dir()
    (workspace / 'dist' / 'receipt.json').write_text('{}')
    assert (scratch / 'dist' / 'receipt.json').read_text() == '{}'
    before = env_file.read_bytes()
    # Never adopt or overwrite another attempt's generated directories.
    repeated = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', setup['run']],
                              env=env, capture_output=True)
    assert repeated.returncode != 0 and env_file.read_bytes() == before
    install = named('Fresh environments; cached downloads, never shared virtualenvs')
    assert steps().index(setup) < steps().index(install)
    for value in (setup['run'], install['run']):
        assert not any(command in value for command in ('mdutil', 'launchctl', 'sudo', 'killall'))


@pytest.mark.parametrize('isolated', [False, True])
def test_dependency_links_and_acceptance_parent_use_the_same_layout(tmp_path, isolated):
    install = named('Fresh environments; cached downloads, never shared virtualenvs')['run']
    # Execute only the environment construction; no network dependency installation.
    construction = install.split('.venv/bin/python -m pip install', 1)[0]
    binary = tmp_path / 'bin'
    binary.mkdir()
    python = binary / 'python'
    python.write_text('#!/bin/sh\ntest "$1" = -m && test "$2" = venv || exit 9\nmkdir "$3"\n')
    python.chmod(0o700)
    scratch = tmp_path / 'resource.noindex'
    scratch.mkdir()
    env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.defpath,
           'CODEPIER_RESOURCE_SCRATCH': str(scratch) if isolated else ''}
    subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', construction],
                   cwd=tmp_path, env=env, check=True)
    for name in ('.venv', '.venv-compat'):
        assert (tmp_path / name).is_dir()
        assert (tmp_path / name).is_symlink() is isolated
        if isolated:
            assert (tmp_path / name).resolve().parent == scratch
    bind = named('Bind the verified public source archive to this commit')['run']
    assert '${CODEPIER_RESOURCE_SCRATCH:-$RUNNER_TEMP}/codepier-public-' in bind
    assert 'release_acceptance.py prepare' in bind and '--commit "$GITHUB_SHA"' in bind
    readiness = named('Wait for genuine resource-runner readiness')['run']
    assert '--timeout 300 --stable-seconds 15' in readiness
