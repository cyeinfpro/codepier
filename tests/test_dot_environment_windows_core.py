"""Cross-platform environment/index contracts selected by Windows core CI.

Mapping cases simulate Windows on other hosts; the subprocess case exercises
native environment behavior on whichever platform actually runs the suite.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import project_runtime as runtime
from agent.project_index import project_index
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


@pytest.fixture
def windows_environment(monkeypatch):
    # Replace this module's os view, never pathlib's platform or real os.environ.
    view = SimpleNamespace(**vars(os))
    view.name, view.pathsep = 'nt', ';'
    view.defpath = r'C:\Windows\System32;C:\Windows'
    view.environ = {'systemroot': r'D:\Windows', 'Dot_Choice': 'parent', 'Dot_Empty': ''}
    monkeypatch.setattr(runtime, 'os', view)
    return view.environ


@pytest.mark.parametrize('fields', [
    {'env': {'Dot_Choice': 'first', 'DOT_CHOICE': 'second'}},
    {'pass_env': ['Dot_Choice', 'DOT_CHOICE']},
])
def test_windows_profile_rejects_ambiguous_variable_names(workspace, windows_environment, fields):
    config, _, _ = workspace
    with pytest.raises(ValueError):
        configure(config, **fields)


def test_windows_operation_rejects_ambiguous_variable_names(workspace, windows_environment):
    config, project, root = workspace
    configure(config)
    with pytest.raises(DevError) as error:
        runtime.build_environment(config, project, root, {'Dot_Choice': 'first', 'DOT_CHOICE': 'second'})
    assert error.value.code == 'RUNTIME_ENV_RESERVED'


def test_windows_layers_share_one_variable_and_preserve_empty_system_values(workspace, windows_environment):
    config, project, root = workspace
    configure(config, pass_env=['dot_choice', 'dot_empty'], env={'DOT_CHOICE': 'profile'})
    environment = runtime.build_environment(config, project, root, {'dot_choice': 'operation'})
    normalized = {key.upper(): value for key, value in environment.items()}
    assert len(environment) == len(normalized)
    assert normalized['DOT_CHOICE'] == 'operation'
    assert normalized['DOT_EMPTY'] == ''
    assert normalized['SYSTEMROOT'] == r'D:\Windows'
    assert normalized['USERPROFILE'] == normalized['HOME']
    assert windows_environment['Dot_Choice'] == 'parent'


def test_windows_passthrough_revision_follows_case_insensitive_lookup(workspace, windows_environment):
    config, project, root = workspace
    configure(config, pass_env=['DOT_CHOICE'])
    first = runtime.descriptor(config, project, root)
    windows_environment['Dot_Choice'] = 'changed'
    second = runtime.descriptor(config, project, root)
    assert first['revision'] != second['revision']
    assert first['runtime_ref'] == second['runtime_ref']


def test_windows_system_tool_paths_accept_environment_spelling():
    paths = runtime.windows_system_tool_paths({
        'SYSTEMROOT': r'D:\Windows', 'PROGRAMFILES': r'E:\Apps', 'PROGRAMW6432': r'E:\Apps'})
    assert paths[0] == r'D:\Windows\System32\WindowsPowerShell\v1.0'
    assert r'E:\Apps\Git\cmd' in paths


def test_windows_host_preserves_linux_container_variable_semantics(workspace, windows_environment):
    config, project, root = workspace
    # Environment construction has no Docker side effects and needs no daemon.
    config['project_runtimes'] = {'fixture-project': {
        'schema_version': 1, 'mode': 'container_v1', 'shell': 'sh', 'path': [],
        'pass_env': [], 'env': {'Dot_Value': 'mixed', 'DOT_VALUE': 'upper'}}}
    environment = runtime.build_environment(config, project, root, {'dot_value': 'lower'})
    assert [environment[key] for key in ('Dot_Value', 'DOT_VALUE', 'dot_value')] == ['mixed', 'upper', 'lower']
    assert not any(key.upper() == 'SYSTEMROOT' for key in environment)


def test_empty_environment_is_distinct_from_absent_and_does_not_mutate_parent(workspace, monkeypatch):
    config, project, root = workspace
    monkeypatch.setenv('DOT_EMPTY', '')
    monkeypatch.delenv('DOT_ABSENT', raising=False)
    configure(config, pass_env=['DOT_EMPTY', 'DOT_ABSENT'], env={'DOT_CONFIG_EMPTY': ''})
    before = deepcopy(dict(os.environ))
    environment = runtime.build_environment(config, project, root, {'DOT_OPERATION_EMPTY': ''})
    assert all(environment[key] == '' for key in ('DOT_EMPTY', 'DOT_CONFIG_EMPTY', 'DOT_OPERATION_EMPTY'))
    assert 'DOT_ABSENT' not in environment
    parent_unchanged = dict(os.environ) == before
    assert parent_unchanged
    absent = runtime.descriptor(config, project, root)['revision']
    monkeypatch.setenv('DOT_ABSENT', '')
    assert runtime.descriptor(config, project, root)['revision'] != absent


@pytest.mark.parametrize('name', ['home', 'Path', 'bash_env', 'xDg_Cache_Home'])
@pytest.mark.parametrize('entry', ['pass_env', 'profile', 'operation'])
def test_owned_environment_variables_cannot_be_reintroduced(workspace, name, entry):
    config, project, root = workspace
    if entry == 'pass_env':
        with pytest.raises(ValueError):
            configure(config, pass_env=[name])
    elif entry == 'profile':
        with pytest.raises(ValueError):
            configure(config, env={name: 'synthetic'})
    else:
        configure(config)
        with pytest.raises(DevError) as error:
            runtime.build_environment(config, project, root, {name: 'synthetic'})
        assert error.value.code == 'RUNTIME_ENV_RESERVED'


def test_readiness_creates_no_storage_and_does_not_prove_task_dependency(workspace):
    config, project, root = workspace
    configure(config, path=runtime.default_profile()['path'])
    observed = runtime.runtime_readiness(config, project, root)
    home = Path(runtime.build_environment(config, project, root)['HOME'])
    assert observed['state'] == 'ready'
    assert observed['toolchain_state'] == 'configured_PATH'
    assert not home.exists()
    with pytest.raises(DevError) as error:
        runtime.prepare(config, project, root, ['codepier-synthetic-missing-tool-f431d6'], root, create=False)
    assert error.value.code == 'COMMAND_MISSING'
    assert not home.exists()


def test_launch_uses_private_directories_and_real_workspace(workspace, monkeypatch):
    config, project, root = workspace
    monkeypatch.setenv('DOT_PROCESS_VALUE', 'parent')
    configure(config, pass_env=['DOT_PROCESS_VALUE'],
              env={'dot_process_value': 'profile', 'DOT_CONFIG_EMPTY': ''})
    source = (
        "import os,pathlib\n"
        "assert os.environ['DOT_CONFIG_EMPTY']==''\n"
        "assert pathlib.Path(os.environ['HOME']).is_dir()\n"
        "assert pathlib.Path(os.environ['TMPDIR']).is_dir()\n"
        "names=('DOT_PROCESS_VALUE','dot_process_value','Dot_Process_Value')\n"
        "observed=[os.environ[name] for name in names]\n"
        "if os.name=='nt':\n"
        "    assert observed==['operation','operation','operation']\n"
        "    assert sum(name.upper()=='DOT_PROCESS_VALUE' for name in os.environ)==1\n"
        "else:\n"
        "    assert observed==['parent','profile','operation']\n"
        "pathlib.Path('observed.txt').write_text('same-workspace')\n")
    plan = runtime.prepare(config, project, root, [sys.executable, '-c', source], root,
                           {'Dot_Process_Value': 'operation'})
    completed = subprocess.run(plan.command, cwd=plan.cwd, env=plan.environment,
                               capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stderr
    assert (root / 'observed.txt').read_text() == 'same-workspace'
    home = Path(plan.environment['HOME'])
    assert home.is_relative_to(Path(config['state_dir']).resolve())
    if os.name == 'posix':
        assert not stat.S_IMODE(home.stat().st_mode) & 0o077


NOISE = ('.DS_Store', 'Thumbs.db', '.Spotlight-V100', '.fseventsd',
         '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache')


class IndexEngine:
    """Bounded synthetic filesystem adapter; it has no service or global state."""
    def __init__(self, root, order):
        self.directory, self.order = root, order

    def root(self, project):
        return self.directory, {}

    def path(self, root, relative, *args):
        return root / relative

    def read(self, project, args):
        value = (self.directory / args['path']).read_bytes()
        return {'path': args['path'], 'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}

    def walk(self, root, start, *, depth, deadline):
        paths = [root / name for name in self.order] if start == root else sorted(start.rglob('*'))
        for path in paths:
            if len(path.relative_to(start).parts) <= depth:
                yield path, path.relative_to(root).as_posix(), path.lstat()


@pytest.mark.parametrize('meaningful_count', [40, 41])
def test_noise_is_removed_before_root_budget_without_hiding_project_content(tmp_path, meaningful_count):
    meaningful = ['AGENTS.md', 'README.md', 'docs', 'src', 'dist', 'build', '.venv', 'macOS-notes.md']
    meaningful += [f'module-{number:02d}' for number in range(meaningful_count - len(meaningful))]
    for name in [*NOISE, *meaningful]:
        path = tmp_path / name
        if name in {'docs', 'src', 'dist', 'build', '.venv'}:
            path.mkdir()
        else:
            path.write_text('synthetic project content')
    (tmp_path / 'docs' / 'Windows-notes.md').write_text('keep platform-named documentation')
    engine = IndexEngine(tmp_path, [*NOISE, *meaningful])
    result = project_index(engine, {'alias': 'fixture'}, {'max_files': 40, 'include_skills': False})
    assert [row['path'] for row in result['root_entries']] == meaningful[:40]
    assert result['truncated'] is (meaningful_count > 40)
    assert any(row['path'] == 'AGENTS.md' and row['state'] == 'available' for row in result['instruction_index'])
    assert any(row['path'] == 'docs/Windows-notes.md' for row in result['documents'])
    assert all((tmp_path / name).read_text() == 'synthetic project content' for name in NOISE)


def test_only_noise_does_not_report_budget_exhaustion(tmp_path):
    for name in NOISE:
        (tmp_path / name).write_text('synthetic')
    result = project_index(IndexEngine(tmp_path, NOISE), {'alias': 'fixture'},
                           {'max_files': 40, 'include_skills': False})
    assert result['root_entries'] == []
    assert not result['truncated']
