"""Owner configuration changes only the selected runtime with explicit SHA."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pytest
from agent.runtime_config import inspect_or_configure, main
from scripts.managed_test_run import project_result_view
from shared.util import DevError
from tests.test_shell import shell_agent  # noqa: F401


def profile(tmp_path, value=None):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(value or {'mode': 'project_clean_v1'}))
    return path


def test_doctor_preview_have_no_side_effects_and_no_secret_values(shell_agent, tmp_path, capsys):
    agent, root = shell_agent
    original = agent.config_path.read_bytes()
    chosen = profile(tmp_path)
    result = inspect_or_configure(agent.config_path, 'fixture', root, profile_path=chosen)
    assert not result['applied'] and not result['grants_changed']
    assert result['runtime']['mode'] == 'project_clean_v1'
    assert agent.config_path.read_bytes() == original
    assert not (Path(agent.config['state_dir']) / 'project-runtimes').exists()
    assert main(['doctor', '--config', str(agent.config_path), '--project', 'fixture', '--root', str(root)]) == 0
    output = capsys.readouterr().out
    assert agent.config['secret'] not in output and str(root) not in output


def test_apply_preserves_all_other_settings_and_retains_private_backup(shell_agent, tmp_path):
    agent, root = shell_agent
    original = agent.config_path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    result = inspect_or_configure(agent.config_path, 'fixture', root,
        profile_path=profile(tmp_path), apply=True, expected_sha256=expected)
    value = json.loads(agent.config_path.read_text())
    extra = value.pop('project_runtimes')
    original_value = json.loads(original)
    original_value.pop('project_runtimes', None)
    assert value == original_value
    assert extra['fixture']['mode'] == 'project_clean_v1'
    backup = agent.config_path.parent / result['backup_ref']
    assert backup.read_bytes() == original
    assert result['applied'] and not result['shell_opt_in_changed']
    if os.name == 'posix':
        assert backup.stat().st_mode & 0o077 == 0
        assert agent.config_path.stat().st_mode & 0o077 == 0
    assert not agent.config_path.with_name(agent.config_path.name + '.runtime.lock').exists()


def test_stale_sha_or_existing_lock_never_changes_configuration(shell_agent, tmp_path):
    agent, root = shell_agent
    original = agent.config_path.read_bytes()
    chosen = profile(tmp_path)
    with pytest.raises(DevError) as error:
        inspect_or_configure(agent.config_path, 'fixture', root,
            profile_path=chosen, apply=True, expected_sha256='0' * 64)
    assert error.value.code == 'SHA_CONFLICT'
    lock = agent.config_path.with_name(agent.config_path.name + '.runtime.lock')
    lock.write_text('other owner operation')
    with pytest.raises(FileExistsError):
        inspect_or_configure(agent.config_path, 'fixture', root, profile_path=chosen,
            apply=True, expected_sha256=hashlib.sha256(original).hexdigest())
    assert agent.config_path.read_bytes() == original
    assert lock.read_text() == 'other owner operation'


def test_symlinked_profile_and_unauthorized_root_are_rejected(shell_agent, tmp_path):
    agent, root = shell_agent
    chosen = profile(tmp_path)
    linked = tmp_path / 'linked.json'
    linked.symlink_to(chosen)
    with pytest.raises(ValueError):
        inspect_or_configure(agent.config_path, 'fixture', root, profile_path=linked)
    with pytest.raises(ValueError):
        inspect_or_configure(agent.config_path, 'fixture', tmp_path, profile_path=chosen)


def test_project_wrapper_paths_are_relative_without_touching_raw_evidence(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    raw = {'exit_code': 7, 'evidence': str(root / 'evidence' / 'run'),
        'manifest': str(root / 'evidence' / 'run' / 'managed-run.json')}
    before = deepcopy(raw)
    result = project_result_view(raw, root)
    assert raw == before and result['exit_code'] == 7
    assert result['evidence'] == 'evidence/run' and result['path_base'] == 'project'
    assert result['original_manifest_retained']
    outside = project_result_view({'manifest': str(tmp_path / 'elsewhere' / 'managed-run.json')}, root)
    assert 'manifest' not in outside
    assert outside['manifest_ref']['scope'] == 'outside_project'
