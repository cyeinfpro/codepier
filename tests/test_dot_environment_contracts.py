"""Real filesystem-link contracts outside the limited Windows core selection."""
from pathlib import Path
import sys

import pytest

from agent import project_runtime as runtime
from shared.util import DevError


@pytest.fixture
def workspace(tmp_path):
    root, state = tmp_path / 'project', tmp_path / 'state'
    root.mkdir()
    state.mkdir(mode=0o700)
    config = {'state_dir': str(state), 'secret': 'synthetic-test-only',
              'shell': {'command': [sys.executable], 'inherit_env': False, 'env': {}},
              'project_runtimes': {}}
    return config, {'id': 'fixture-project', 'alias': 'fixture'}, root


def configure(config, **fields):
    config['project_runtimes'] = runtime.validate_runtimes({
        'fixture-project': {'mode': 'project_clean_v1', **fields}})


def test_private_directory_link_is_rejected_without_writing_target(workspace, tmp_path):
    config, project, root = workspace
    configure(config)
    home = Path(runtime.build_environment(config, project, root)['HOME'])
    home.parent.parent.mkdir(mode=0o700)
    outside = tmp_path / 'outside-storage'
    outside.mkdir()
    home.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevError) as error:
        runtime.prepare(config, project, root, [sys.executable], root)
    assert error.value.code == 'RUNTIME_DIRECTORY_UNSAFE'
    assert list(outside.iterdir()) == []


def test_clean_cwd_rejects_external_link_while_explicit_native_stays_compatible(workspace, tmp_path):
    config, project, root = workspace
    configure(config)
    outside = tmp_path / 'outside-cwd'
    outside.mkdir()
    (root / 'linked-cwd').symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevError) as error:
        runtime.prepare(config, project, root, [sys.executable], root / 'linked-cwd')
    assert error.value.code == 'RUNTIME_CWD_OUTSIDE_WORKSPACE'
    assert not (Path(config['state_dir']) / 'project-runtimes').exists()
    configure(config, mode='native_compat')
    plan = runtime.prepare(config, project, root, [sys.executable], outside, create=False)
    assert plan.cwd == outside.resolve()
    assert plan.runtime['mode'] == 'native_compat'
