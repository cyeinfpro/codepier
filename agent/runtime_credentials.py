"""Project-bound reuse of existing native development credentials.

Only clean execution uses this module. It never copies credential files, reads
tokens from disk, changes HOME, starts an auth process, or creates a grant.
Reuse defaults on; explicit project opt-outs take precedence. The account's
native tools still perform authentication and enforce its scope.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile

from shared.util import DevError

PROVIDERS = ('github_cli', 'git_config', 'ssh_agent')
GITHUB_TOKEN_GROUPS = (('GH_TOKEN', 'GITHUB_TOKEN'),
                       ('GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))
GITHUB_TOKENS = tuple(name for group in GITHUB_TOKEN_GROUPS for name in group)


def validate_credentials(value):
    if (not isinstance(value, dict) or set(value) - set(PROVIDERS)
            or any(type(item) is not bool for item in value.values())):
        raise ValueError('runtime.credentials must contain only github_cli, git_config and ssh_agent booleans')
    return {name: value.get(name, True) for name in PROVIDERS}


def credential_settings(profile):
    """Resolve clean defaults without rewriting an existing stored profile."""
    if profile['mode'] != 'project_clean_v1':
        return dict.fromkeys(PROVIDERS, False)
    return validate_credentials(profile.get('credentials', {}))


def _normalized(parent, windows):
    return {key.upper() if windows else key: value for key, value in parent.items()}


def _home(parent):
    return parent.get('HOME') or parent.get('USERPROFILE')


def _join(value, *parts, windows=False):
    if not value:
        return None
    path = PureWindowsPath(value) if windows else Path(value)
    # A daemon with no account HOME must not search arbitrary working dirs.
    return str(path.joinpath(*parts)) if path.is_absolute() else None


def _github_directory(parent, windows):
    if parent.get('GH_CONFIG_DIR'):
        return _native_config_path(parent['GH_CONFIG_DIR'], 'GH_CONFIG_DIR', windows)
    if parent.get('XDG_CONFIG_HOME'):
        return _join(parent['XDG_CONFIG_HOME'], 'gh', windows=windows)
    if windows and parent.get('APPDATA'):
        return _join(parent['APPDATA'], 'GitHub CLI', windows=windows)
    home = parent.get('USERPROFILE') if windows else parent.get('HOME')
    return _join(home, '.config', 'gh', windows=windows)


def _native_config_path(value, name, windows):
    if value:
        path = PureWindowsPath(value) if windows else Path(value)
        if not path.is_absolute():
            raise DevError('RUNTIME_CREDENTIAL_SOURCE_RELATIVE',
                name + ' must be absolute before reusing it across project working directories', 409)
    return value


def _git_sources(parent, windows):
    home = _home(parent)
    xdg = parent.get('XDG_CONFIG_HOME') or _join(home, '.config', windows=windows)
    paths = [_join(xdg, 'git', 'config', windows=windows),
             _join(home, '.gitconfig', windows=windows)]
    return list(dict.fromkeys(path for path in paths if path and Path(path).is_file()))


def credential_revision_inputs(profile, parent, *, windows=False):
    """Inputs for the caller's keyed digest, never a public credential report."""
    if profile['mode'] != 'project_clean_v1':
        return {}
    settings = credential_settings(profile)
    names = set()
    if settings.get('github_cli'):
        names.update((*GITHUB_TOKENS, 'GH_HOST', 'GH_CONFIG_DIR',
                      'XDG_CONFIG_HOME', 'HOME', 'USERPROFILE'))
        if windows:
            names.add('APPDATA')
    if settings.get('git_config'):
        names.update(('GIT_CONFIG_GLOBAL', 'XDG_CONFIG_HOME', 'HOME', 'USERPROFILE'))
    if settings.get('ssh_agent'):
        names.add('SSH_AUTH_SOCK')
    parent = _normalized(parent, windows)
    return {name: parent.get(name) for name in sorted(names)}


def _check_bridge_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or hasattr(os, 'getuid') and info.st_uid != os.getuid()
            or os.name == 'posix' and stat.S_IMODE(info.st_mode) & 0o077):
        raise DevError('RUNTIME_DIRECTORY_UNSAFE', 'Runtime Git configuration is not an owned private file', 409)


def _git_bridge(home, sources, *, create):
    # The bridge global file is never ~/.gitconfig: turning reuse off must
    # stop referencing host configuration on the next process launch.
    encoded = json.dumps(sources, ensure_ascii=False, separators=(',', ':'))
    suffix = hashlib.sha256(encoded.encode()).hexdigest()[:24]
    path = Path(home) / ('git-credentials-' + suffix + '.config')
    if not create:
        return str(path)
    if path.exists() or path.is_symlink():
        _check_bridge_file(path)
        return str(path)
    # Git's normal XDG-before-HOME order is retained at GLOBAL scope. Local
    # repository/worktree settings and explicit -c options still win.
    content = '# CodePier: native Git configuration references while reuse is enabled.\n'
    for source in sources:
        if any(ord(char) < 32 for char in source):
            raise DevError('RUNTIME_CREDENTIAL_CONFIG_INVALID', 'Git configuration source has unsupported control characters', 409)
        content += '[include]\n\tpath = ' + json.dumps(source, ensure_ascii=False) + '\n'
    descriptor, name = tempfile.mkstemp(prefix='.git-credentials-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publish without replacing an existing file or racing a user's
        # git config --global update. Concurrent launches see complete bytes.
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        _check_bridge_file(path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


def apply_credentials(profile, parent, environment, *, windows=False, create=False):
    """Fill absent provider values after the explicit environment layers.

    A project token or config selection takes precedence across gh's alternate
    token names, so a higher-priority account token cannot shadow that choice.
    Explicit empty values remain explicit. No process environment is mutated.
    """
    if profile['mode'] != 'project_clean_v1':
        return environment
    settings = credential_settings(profile)
    parent = _normalized(parent, windows)
    if settings.get('github_cli'):
        explicit_config = 'GH_CONFIG_DIR' in environment
        explicit_token = any(name in environment for name in GITHUB_TOKENS)
        if not explicit_config:
            for group in GITHUB_TOKEN_GROUPS:
                if not any(name in environment for name in group):
                    for name in group:
                        if name in parent:
                            environment[name] = parent[name]
            if not explicit_token and 'GH_HOST' in parent:
                environment.setdefault('GH_HOST', parent['GH_HOST'])
            directory = _github_directory(parent, windows)
            if directory is not None and (parent.get('GH_CONFIG_DIR') or Path(directory).is_dir()):
                environment['GH_CONFIG_DIR'] = directory
        environment.setdefault('GH_PROMPT_DISABLED', '1')
    if settings.get('git_config') and 'GIT_CONFIG_GLOBAL' not in environment:
        if 'GIT_CONFIG_GLOBAL' in parent:
            environment['GIT_CONFIG_GLOBAL'] = _native_config_path(
                parent['GIT_CONFIG_GLOBAL'], 'GIT_CONFIG_GLOBAL', windows)
        else:
            sources = _git_sources(parent, windows)
            if sources:
                environment['GIT_CONFIG_GLOBAL'] = _git_bridge(environment['HOME'], sources, create=create)
    if settings.get('ssh_agent') and 'SSH_AUTH_SOCK' in parent:
        environment.setdefault('SSH_AUTH_SOCK', parent['SSH_AUTH_SOCK'])
    return environment


def credential_readiness(profile, parent, environment, *, windows=False):
    """Pure source observation; configured does not mean remotely authorized."""
    parent = _normalized(parent, windows)
    settings = credential_settings(profile)
    result = {}
    for name in PROVIDERS:
        enabled = bool(settings.get(name))
        state, source = ('missing' if enabled else 'disabled'), 'none'
        if enabled and name == 'github_cli':
            if any(environment.get(key) for key in GITHUB_TOKENS):
                state, source = 'configured', 'environment'
            elif environment.get('GH_CONFIG_DIR') and Path(environment['GH_CONFIG_DIR']).is_dir():
                state, source = 'configured', 'configuration'
        elif enabled and name == 'git_config':
            location = environment.get('GIT_CONFIG_GLOBAL')
            if location:
                sources = _git_sources(parent, windows) if 'GIT_CONFIG_GLOBAL' not in parent else []
                planned = _git_bridge(environment['HOME'], sources, create=False) if sources else None
                if Path(location).is_file() or location == planned:
                    state, source = 'configured', 'configuration'
        elif enabled and name == 'ssh_agent' and environment.get('SSH_AUTH_SOCK'):
            state, source = 'configured', 'agent_socket'
        result[name] = {'enabled': enabled, 'state': state, 'source': source}
    return result
