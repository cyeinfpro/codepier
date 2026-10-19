"""Synthetic credential reuse contracts; no real accounts or network calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import project_runtime as runtime
from agent.runtime_credentials import _github_directory
from shared.util import DevError


NAMES = ('github_cli', 'git_config', 'ssh_agent')


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root, state, home = (tmp_path / name for name in ('project', 'state', 'synthetic owner'))
    for path in (root, state, home):
        path.mkdir(mode=0o700)
    parent = {'HOME': str(home), 'USERPROFILE': str(home)}
    parent.update({key: os.environ[key] for key in runtime.SYSTEM_KEYS if key in os.environ})
    view = SimpleNamespace(**vars(os))
    view.environ = parent
    monkeypatch.setattr(runtime, 'os', view)
    config = {'state_dir': str(state), 'secret': 'synthetic-credentials-only',
              'shell': {'command': [sys.executable], 'inherit_env': False, 'env': {}},
              'project_runtimes': {}}
    return config, {'id': 'fixture', 'alias': 'fixture'}, root, parent, home


def configure(config, **fields):
    config['project_runtimes'] = runtime.validate_runtimes({
        'fixture': {'mode': 'project_clean_v1', **fields}})


def sources(home):
    gh = home / '.config' / 'gh'
    gh.mkdir(parents=True)
    (gh / 'hosts.yml').write_text('synthetic-account-only', encoding='utf-8')
    git = home / '.gitconfig'
    git.write_text('[codepier]\n\tchoice = owner\n', encoding='utf-8')
    return gh, git


@pytest.mark.parametrize('fields', [None, {}, {'credentials': {}},
    {'credentials': {'github_cli': True}}],
    ids=['unbound-project', 'existing-clean-profile', 'empty-credentials', 'partial-credentials'])
def test_default_credentials_are_reused_and_parent_stays_unchanged(workspace, fields):
    config, project, root, parent, home = workspace
    gh, git = sources(home)
    expected = {'GH_TOKEN': 'synthetic-token', 'GH_CONFIG_DIR': str(gh),
                'GIT_CONFIG_GLOBAL': str(git), 'SSH_AUTH_SOCK': str(home / 'agent.sock')}
    parent.update(expected)
    if fields is not None:
        configure(config, **fields)
    before = deepcopy(parent)
    env = runtime.build_environment(config, project, root)
    assert {key: env[key] for key in expected} == expected
    assert env['HOME'] != str(home) and not Path(env['HOME']).exists()
    assert parent == before
    assert runtime.runtime_readiness(config, project, root)['credentials'] == {
        'github_cli': {'enabled': True, 'state': 'configured', 'source': 'environment'},
        'git_config': {'enabled': True, 'state': 'configured', 'source': 'configuration'},
        'ssh_agent': {'enabled': True, 'state': 'configured', 'source': 'agent_socket'}}


@pytest.mark.parametrize('value', [None, [], False, {'unknown': True},
    {'github_cli': 1}, {'github_cli': 'true'}, {'git_config': 0}, {'ssh_agent': None}])
def test_configuration_rejects_unknown_or_non_boolean_switches(workspace, value):
    with pytest.raises(ValueError):
        configure(workspace[0], credentials=value)


@pytest.mark.parametrize('mode', ['native_compat', 'container_v1'])
def test_credentials_are_rejected_outside_clean_mode(mode):
    profile = {'mode': mode}
    if mode == 'container_v1':
        profile.update(shell='sh', container={'engine': sys.executable,
            'endpoint': 'unix:///tmp/synthetic.sock', 'image': 'sha256:' + 'a' * 64})
    runtime.validate_runtimes({'fixture': profile})
    with pytest.raises(ValueError):
        runtime.validate_runtimes({'fixture': {**profile, 'credentials': {}}})


@pytest.mark.parametrize('enabled', NAMES)
def test_explicit_provider_choices_are_independent_and_keep_private_home_cache_and_path(workspace, enabled):
    config, project, root, parent, home = workspace
    _, git = sources(home)
    parent.update(GH_TOKEN='synthetic-token', GIT_CONFIG_GLOBAL=str(git),
                  SSH_AUTH_SOCK=str(home / 'agent.sock'), SSH_PRIVATE_KEY='must-stay-out')
    base = runtime.build_environment(config, project, root)
    before = deepcopy(parent)
    configure(config, credentials={**dict.fromkeys(NAMES, False), enabled: True},
              path=runtime.default_profile()['path'])
    env = runtime.build_environment(config, project, root)
    for name, variable in [('github_cli', 'GH_TOKEN'), ('git_config', 'GIT_CONFIG_GLOBAL'),
                           ('ssh_agent', 'SSH_AUTH_SOCK')]:
        assert (variable in env) is (name == enabled)
    for variable in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'PATH'):
        assert env[variable] == base[variable]
    assert 'SSH_PRIVATE_KEY' not in env and parent == before


@pytest.mark.parametrize('disabled', NAMES)
def test_explicit_false_disables_only_its_provider(workspace, disabled):
    config, project, root, parent, home = workspace
    _, git = sources(home)
    parent.update(GH_TOKEN='synthetic-token', GIT_CONFIG_GLOBAL=str(git),
                  SSH_AUTH_SOCK=str(home / 'agent.sock'))
    configure(config, credentials={disabled: False})
    env = runtime.build_environment(config, project, root)
    for name, variable in [('github_cli', 'GH_TOKEN'), ('git_config', 'GIT_CONFIG_GLOBAL'),
                           ('ssh_agent', 'SSH_AUTH_SOCK')]:
        assert (variable in env) is (name != disabled)
    observed = runtime.runtime_readiness(config, project, root)['credentials']
    assert observed[disabled] == {'enabled': False, 'state': 'disabled', 'source': 'none'}
    assert all(observed[name]['enabled'] for name in NAMES if name != disabled)


@pytest.mark.parametrize('location', ['explicit', 'xdg', 'home'])
def test_gh_configuration_discovery(workspace, location):
    config, project, root, parent, home = workspace
    expected, _ = sources(home)
    if location in {'explicit', 'xdg'}:
        xdg = home / 'xdg'
        expected = xdg / 'gh'
        expected.mkdir(parents=True)
        parent['XDG_CONFIG_HOME'] = str(xdg)
    if location == 'explicit':
        expected = home / 'explicit-gh'
        expected.mkdir()
        parent['GH_CONFIG_DIR'] = str(expected)
    configure(config, credentials={'github_cli': True})
    assert runtime.build_environment(config, project, root)['GH_CONFIG_DIR'] == str(expected)
    assert runtime.runtime_readiness(config, project, root)['credentials']['github_cli'] == {
        'enabled': True, 'state': 'configured', 'source': 'configuration'}


@pytest.mark.parametrize(('extra', 'expected'), [
    ({'GH_CONFIG_DIR': r'D:\selected\gh'}, r'D:\selected\gh'),
    ({'XDG_CONFIG_HOME': r'D:\xdg'}, r'D:\xdg\gh'),
    ({'APPDATA': r'C:\Users\fixture\AppData\Roaming'},
     r'C:\Users\fixture\AppData\Roaming\GitHub CLI'),
    ({}, r'C:\Users\fixture\.config\gh'),
    ({'GH_CONFIG_DIR': ''}, r'C:\Users\fixture\.config\gh'),
])
def test_gh_windows_path_precedence_uses_userprofile_not_git_home(extra, expected):
    parent = {'HOME': r'D:\git-home', 'USERPROFILE': r'C:\Users\fixture', **extra}
    assert _github_directory(parent, windows=True) == expected


def test_empty_parent_gh_config_dir_uses_host_fallback(workspace):
    config, project, root, parent, home = workspace
    gh, _ = sources(home)
    parent['GH_CONFIG_DIR'] = ''
    configure(config, credentials={'github_cli': True})
    assert runtime.build_environment(config, project, root)['GH_CONFIG_DIR'] == str(gh)
    configure(config, credentials={'github_cli': True}, env={'GH_CONFIG_DIR': ''})
    assert runtime.build_environment(config, project, root)['GH_CONFIG_DIR'] == ''


@pytest.mark.parametrize('layer', ['pass_env', 'profile', 'operation'])
def test_explicit_empty_values_override_automatic_discovery(workspace, layer):
    config, project, root, parent, home = workspace
    gh, git = sources(home)
    values = dict.fromkeys(('GH_TOKEN', 'GH_CONFIG_DIR', 'GIT_CONFIG_GLOBAL', 'SSH_AUTH_SOCK'), '')
    parent.update(GH_TOKEN='synthetic-token', GH_CONFIG_DIR=str(gh),
                  GIT_CONFIG_GLOBAL=str(git), SSH_AUTH_SOCK=str(home / 'agent.sock'))
    fields, overrides = {'credentials': dict.fromkeys(NAMES, True)}, None
    if layer == 'pass_env':
        parent.update(values)
        fields['pass_env'] = list(values)
    elif layer == 'profile':
        fields['env'] = values
    else:
        overrides = values
    configure(config, **fields)
    env = runtime.build_environment(config, project, root, overrides)
    assert {key: env[key] for key in values} == values


@pytest.mark.parametrize('variable', ['GITHUB_TOKEN', 'GH_CONFIG_DIR'])
def test_explicit_gh_account_is_not_shadowed_by_parent_token_or_host(workspace, variable):
    config, project, root, parent, home = workspace
    gh, _ = sources(home)
    parent.update(GH_TOKEN='synthetic-wrong-account', GH_HOST='wrong.example.test')
    value = str(gh) if variable == 'GH_CONFIG_DIR' else 'synthetic-project-account'
    configure(config, credentials={'github_cli': True}, env={variable: value})
    env = runtime.build_environment(config, project, root)
    assert env[variable] == value and 'GH_TOKEN' not in env
    assert 'GH_HOST' not in env


@pytest.mark.parametrize(('credential', 'variable'), [
    ('github_cli', 'GH_CONFIG_DIR'), ('git_config', 'GIT_CONFIG_GLOBAL')])
def test_relative_parent_configuration_is_rejected_but_explicit_project_choice_is_preserved(
        workspace, credential, variable):
    config, project, root, parent, _ = workspace
    parent[variable] = 'relative/synthetic-config'
    configure(config, credentials={credential: True})
    with pytest.raises(DevError) as error:
        runtime.build_environment(config, project, root)
    assert error.value.code == 'RUNTIME_CREDENTIAL_SOURCE_RELATIVE'
    configure(config, credentials={credential: True}, env={variable: 'project-config'})
    assert runtime.build_environment(config, project, root)[variable] == 'project-config'


@pytest.mark.parametrize(('credential', 'variable'), [
    ('github_cli', 'GH_CONFIG_DIR'), ('git_config', 'GIT_CONFIG_GLOBAL')])
def test_invalid_default_credential_source_is_diagnosable_and_can_be_disabled(
        workspace, credential, variable):
    config, project, root, parent, home = workspace
    parent.update(GH_TOKEN='synthetic-private-credential', **{variable: 'relative/private-config'})
    before_parent, before_config = deepcopy(parent), deepcopy(config)

    # Panel GET uses this observation before it can display the opt-out fields.
    # Invalid inherited sources must not make that recovery page unreadable.
    observed = runtime.runtime_readiness(config, project, root)
    assert observed['state'] == 'unavailable'
    assert observed['code'] == 'RUNTIME_CREDENTIAL_SOURCE_RELATIVE'
    assert observed['toolchain_state'] == 'not_observed' and observed['tools'] == {}
    assert observed['mode'] == 'project_clean_v1' and observed['grants_authority'] is False
    assert str(home) not in json.dumps(observed)
    assert 'synthetic-private-credential' not in json.dumps(observed)
    assert 'relative/private-config' not in json.dumps(observed)
    assert list(Path(config['state_dir']).iterdir()) == []
    assert parent == before_parent and config == before_config

    # Returning a diagnostic must never make process preparation accept it.
    with pytest.raises(DevError) as error:
        runtime.prepare(config, project, root, [sys.executable], root, create=False)
    assert error.value.code == 'RUNTIME_CREDENTIAL_SOURCE_RELATIVE'

    configure(config, credentials={credential: False}, path=runtime.default_profile()['path'])
    recovered = runtime.runtime_readiness(config, project, root)
    assert recovered['state'] == 'ready' and recovered['code'] is None
    assert recovered['credentials'][credential] == {
        'enabled': False, 'state': 'disabled', 'source': 'none'}
    assert parent == before_parent
    assert list(Path(config['state_dir']).iterdir()) == []


def test_real_git_preserves_two_global_configs_relative_includes_and_local_precedence(workspace):
    config, project, root, parent, home = workspace
    git = shutil.which('git')
    assert git, 'A local Git executable is required; this test never uses the network'
    xdg = home / 'custom xdg'
    git_dir = xdg / 'git'
    git_dir.mkdir(parents=True)
    parent['XDG_CONFIG_HOME'] = str(xdg)
    (git_dir / 'config').write_text(
        '[credential]\n\thelper = synthetic-xdg\n[codepier]\n\tchoice = xdg\n'
        '[include]\n\tpath = extra.conf\n', encoding='utf-8')
    (git_dir / 'extra.conf').write_text('[codepier]\n\tincluded = yes\n', encoding='utf-8')
    (home / '.gitconfig').write_text(
        '[credential]\n\thelper = synthetic-home\n[codepier]\n\tchoice = home\n', encoding='utf-8')
    originals = {path: path.read_bytes() for path in (git_dir / 'config',
        git_dir / 'extra.conf', home / '.gitconfig')}
    configure(config, credentials={'git_config': True})
    env = runtime.build_environment(config, project, root, {'GIT_CONFIG_NOSYSTEM': '1'}, create=True)

    def run_git(*args):
        result = subprocess.run([git, *args], cwd=root, env=env,
            capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert run_git('config', '--get-all', 'credential.helper').splitlines() == [
        'synthetic-xdg', 'synthetic-home']
    assert run_git('config', '--get', 'codepier.included') == 'yes'
    assert run_git('config', '--get', 'codepier.choice') == 'home'
    run_git('init', '--quiet')
    run_git('config', '--local', 'codepier.choice', 'repository')
    assert run_git('config', '--get', 'codepier.choice') == 'repository'
    assert {path: path.read_bytes() for path in originals} == originals
    assert Path(env['GIT_CONFIG_GLOBAL']).is_relative_to(Path(env['HOME']))
    configure(config, credentials={'git_config': False})
    env = runtime.build_environment(config, project, root, {'GIT_CONFIG_NOSYSTEM': '1'})
    assert 'GIT_CONFIG_GLOBAL' not in env
    disabled = subprocess.run([git, 'config', '--get-all', 'credential.helper'],
        cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False)
    assert disabled.returncode == 1 and disabled.stdout == ''


def test_empty_parent_git_global_does_not_fall_back_to_owner_config(workspace):
    config, project, root, parent, home = workspace
    sources(home)
    parent['GIT_CONFIG_GLOBAL'] = ''
    configure(config, credentials={'git_config': True})
    assert runtime.build_environment(config, project, root)['GIT_CONFIG_GLOBAL'] == ''


def test_readiness_redacts_sources_and_creates_no_storage_or_processes(workspace, monkeypatch):
    config, project, root, parent, home = workspace
    gh, git = sources(home)
    parent.update(GH_TOKEN='synthetic-never-in-metadata', SSH_AUTH_SOCK=str(home / 'agent.sock'))
    configure(config)
    before_parent, before_config = deepcopy(parent), deepcopy(config)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(gh) or path == git:
            raise AssertionError('Discovery must not read credential configuration contents')
        return original_open(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError('Readiness must not start a process')

    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    env = runtime.build_environment(config, project, root, create=False)
    observed = runtime.runtime_readiness(config, project, root)
    assert observed['credentials'] == {
        'github_cli': {'enabled': True, 'state': 'configured', 'source': 'environment'},
        'git_config': {'enabled': True, 'state': 'configured', 'source': 'configuration'},
        'ssh_agent': {'enabled': True, 'state': 'configured', 'source': 'agent_socket'}}
    assert str(home) not in json.dumps(observed)
    assert 'synthetic-never-in-metadata' not in json.dumps(observed)
    assert not Path(env['HOME']).exists()
    assert list(Path(config['state_dir']).iterdir()) == []
    assert parent == before_parent and config == before_config


def test_enabled_missing_sources_do_not_claim_a_login(workspace):
    config, project, root, _, _ = workspace
    assert runtime.runtime_readiness(config, project, root)['credentials'] == {
        name: {'enabled': True, 'state': 'missing', 'source': 'none'} for name in NAMES}


@pytest.mark.parametrize(('credential', 'variable'), [
    ('github_cli', 'GH_TOKEN'), ('git_config', 'GIT_CONFIG_GLOBAL'), ('ssh_agent', 'SSH_AUTH_SOCK')])
def test_revision_changes_for_selected_credential_and_is_keyed_and_redacted(workspace, credential, variable):
    config, project, root, parent, home = workspace
    configure(config, credentials={**dict.fromkeys(NAMES, False), credential: True})
    parent[variable] = str(home / 'synthetic-one')
    first = runtime.descriptor(config, project, root)
    parent[variable] = str(home / 'synthetic-two')
    second = runtime.descriptor(config, project, root)
    assert first['revision'] != second['revision'] and first['runtime_ref'] == second['runtime_ref']
    assert str(home) not in json.dumps(second)
    config['secret'] = 'other-synthetic-key'
    assert runtime.descriptor(config, project, root)['revision'] != second['revision']


@pytest.mark.parametrize('variable', ['GH_TOKEN', 'GIT_CONFIG_GLOBAL', 'SSH_AUTH_SOCK'])
def test_default_reuse_tracks_credential_source_changes_in_revision(workspace, variable):
    config, project, root, parent, home = workspace
    parent[variable] = str(home / 'synthetic-one')
    first = runtime.descriptor(config, project, root)
    parent[variable] = str(home / 'synthetic-two')
    second = runtime.descriptor(config, project, root)
    assert first['revision'] != second['revision']
    assert first['runtime_ref'] == second['runtime_ref']


def test_disabled_sources_do_not_change_revision(workspace):
    config, project, root, parent, home = workspace
    configure(config, credentials=dict.fromkeys(NAMES, False))
    first = runtime.descriptor(config, project, root)
    parent.update(GH_TOKEN='synthetic-changed', GH_CONFIG_DIR=str(home / 'changed-gh'),
                  GIT_CONFIG_GLOBAL=str(home / 'changed-git'), SSH_AUTH_SOCK=str(home / 'changed-socket'))
    assert runtime.descriptor(config, project, root)['revision'] == first['revision']
