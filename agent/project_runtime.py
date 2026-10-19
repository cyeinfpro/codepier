"""Explicit project execution profiles. Environment hygiene is not OS isolation.

Configuration belongs to the Agent owner, never to project files or MCP args.
The same pure builder is used for discovery and process launch; only launch may
create private HOME/cache directories. Explicit compatibility profiles retain their semantics; otherwise clean is the default.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat

from shared.util import DevError

PROTOCOL = 1
MODES = {'native_compat', 'project_clean_v1', 'container_v1'}
RESERVED = frozenset({'HOME', 'USER', 'LOGNAME', 'USERNAME', 'USERPROFILE', 'PATH',
    'SHELL', 'ENV', 'BASH_ENV', 'ZDOTDIR', 'TMPDIR', 'TMP', 'TEMP',
    'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'APPDATA', 'LOCALAPPDATA',
    'CODEPIER_RUNTIME_REF', 'CODEPIER_RUNTIME_REVISION'})
SYSTEM_KEYS = ('SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT')
TOOLS = ('git', 'ssh', 'sshpass', 'gh', 'docker', 'python3', 'uv', 'node', 'npm', 'pnpm')


def _environment_keys(value, windows):
    """Use one spelling per Windows variable without changing Linux guests."""
    return {key.upper() if windows else key: item for key, item in value.items()}


def _environment(value, label, *, windows=False):
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError(f'{label} must be a bounded environment object')
    names = set()
    for key, item in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                or not isinstance(item, str) or '\x00' in item or len(item) > 65536):
            raise ValueError(f'{label} contains an invalid variable')
        if key.upper() in RESERVED:
            raise ValueError(f'{label}.{key} is owned by the runtime profile')
        name = key.upper() if windows else key
        if name in names:
            raise ValueError(f'{label} contains ambiguous variable names')
        names.add(name)
    return dict(value)


def validate_runtimes(value):
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError('project_runtimes must map exact project IDs or aliases to profiles')
    result = {}
    for selector, raw in value.items():
        if (not isinstance(selector, str) or not 1 <= len(selector) <= 100
                or selector != selector.strip() or '*' in selector or '\x00' in selector):
            raise ValueError('Runtime bindings require an exact project ID or alias; no wildcard')
        if not isinstance(raw, dict) or set(raw) - {'schema_version', 'mode', 'path', 'env', 'pass_env', 'shell', 'container'}:
            raise ValueError('Unknown project runtime configuration field')
        if type(raw.get('schema_version', 1)) is not int or raw.get('schema_version', 1) != 1:
            raise ValueError('Unsupported project runtime schema_version')
        mode = raw.get('mode')
        if mode not in MODES:
            raise ValueError('Unknown project runtime mode')
        if mode == 'native_compat':
            if set(raw) - {'schema_version', 'mode'}:
                raise ValueError('native_compat uses the existing shell configuration')
            result[selector] = {'schema_version': 1, 'mode': mode}
            continue
        windows = os.name == 'nt' and mode == 'project_clean_v1'
        paths = raw.get('path', [])
        if not isinstance(paths, list) or len(paths) > 32 or any(
                not isinstance(p, str) or not p or '\x00' in p or os.pathsep in p for p in paths):
            raise ValueError('runtime.path must contain explicit PATH directories')
        passed = raw.get('pass_env', [])
        if (not isinstance(passed, list) or len(passed) > 64
                or any(not isinstance(name, str) for name in passed)
                or len({name.upper() if windows else name for name in passed}) != len(passed)):
            raise ValueError('runtime.pass_env must contain distinct variable names')
        for name in passed:
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or name.upper() in RESERVED:
                raise ValueError('runtime.pass_env contains a reserved or invalid variable')
        shell = raw.get('shell', 'powershell' if os.name == 'nt' else 'sh')
        if shell not in {'sh', 'bash', 'powershell'}:
            raise ValueError('runtime.shell must be sh, bash or powershell; login shells are not supported')
        profile = {'schema_version': 1, 'mode': mode, 'path': list(paths),
                   'env': _environment(raw.get('env', {}), 'runtime.env', windows=windows),
                   'pass_env': list(passed), 'shell': shell}
        if mode == 'container_v1':
            from agent.container_runtime import validate_container
            if passed:
                raise ValueError('Container profiles do not inherit Agent environment variables')
            if shell == 'powershell':
                raise ValueError('container_v1 requires a Linux sh/bash toolchain')
            profile['container'] = validate_container(raw.get('container'))
            if any(not p.startswith('/') for p in paths):
                raise ValueError('Container PATH directories must be guest-absolute paths')
        elif 'container' in raw:
            raise ValueError('container settings require container_v1')
        result[selector] = profile
    return result


def windows_system_tool_paths(parent):
    """Deterministic system tools, not arbitrary personal PATH inheritance.

    Git for Windows publishes its command shim under the machine Program Files
    directory. Keep both native/32-bit installation roots where applicable;
    project-specific tools still precede them and explicit profiles stay exact.
    """
    parent = _environment_keys(parent, True)
    system = PureWindowsPath(parent.get('SYSTEMROOT') or r'C:\Windows')
    if not system.is_absolute():
        system = PureWindowsPath(r'C:\Windows')
    result = [str(system / 'System32' / 'WindowsPowerShell' / 'v1.0'), str(system / 'System32')]
    drive = PureWindowsPath(system.anchor)
    for name, default in [('ProgramW6432', drive / 'Program Files'),
                          ('ProgramFiles', drive / 'Program Files'),
                          ('ProgramFiles(x86)', drive / 'Program Files (x86)')]:
        location = PureWindowsPath(parent.get(name.upper()) or default)
        if location.is_absolute():
            result.append(str(location / 'Git' / 'cmd'))
    return list(dict.fromkeys(result))


def default_profile():
    """A deterministic project-first tool path, never the personal login PATH."""
    paths = ['.venv/Scripts' if os.name == 'nt' else '.venv/bin', 'node_modules/.bin']
    if os.name == 'nt':
        paths += windows_system_tool_paths(os.environ)
    else:
        if sys.platform == 'darwin':
            paths.append('/opt/homebrew/bin')
        paths.append('/usr/local/bin')
    return {'schema_version': 1, 'mode': 'project_clean_v1', 'shell': 'powershell' if os.name == 'nt' else 'sh',
            'path': paths, 'pass_env': [], 'env': {}}


def selected(config, project):
    profiles = config.get('project_runtimes', {})
    matches = [profiles[k] for k in dict.fromkeys((project.get('id'), project.get('alias'))) if k in profiles]
    if len(matches) > 1 and matches[0] != matches[1]:
        raise DevError('RUNTIME_BINDING_AMBIGUOUS', 'Project ID and alias select different runtime profiles', 409)
    return deepcopy(matches[0]) if matches else default_profile()


def _digest(config, value):
    # Keyed revisions do not expose an offline hash oracle for bound credentials.
    key = str(config.get('secret', 'synthetic-config')).encode()
    return hmac.new(key, json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode(), hashlib.sha256).hexdigest()


def profile_revision(config, profile):
    windows = os.name == 'nt' and profile['mode'] == 'project_clean_v1'
    parent = _environment_keys(os.environ, windows)
    return _digest(config, ['project-runtime-config-v1', profile,
        {key: parent.get(key.upper() if windows else key) for key in profile.get('pass_env', [])}])


def descriptor(config, project, root):
    profile = selected(config, project)
    binding = [project.get('id', project.get('alias')), str(Path(root).resolve()),
               project.get('_coding_workspace', project.get('workspace_id', ''))]
    revision = profile_revision(config, profile)
    if profile['mode'] == 'native_compat':
        revision = _digest(config, [revision, config.get('shell', {})])
    revision = _digest(config, ['project-runtime-revision-v1', binding, revision])
    return {'schema_version': PROTOCOL, 'runtime_ref': _digest(config, ['project-runtime-v1', binding]),
        'revision': revision, 'mode': profile['mode'], 'grants_authority': False,
        'filesystem_scope': 'workspace_bind' if profile['mode'] == 'container_v1' else 'execution_account',
        'isolation': 'required_at_start' if profile['mode'] == 'container_v1' else 'none'}


def advertised_profiles(config):
    """Configuration capability only, authenticated by the existing Agent channel.

    No claim that a daemon, image or a particular invocation is ready/isolated.
    """
    return [{'project': key, 'mode': profile['mode'], 'revision': profile_revision(config, profile),
             'driver': 'docker_restricted_v1'} for key, profile in config.get('project_runtimes', {}).items()
            if profile['mode'] == 'container_v1']


def shell_command(config, project):
    profile = selected(config, project)
    if profile['mode'] == 'native_compat':
        return list(config['shell']['command'])
    kind = profile['shell']
    if kind == 'powershell':
        return ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command']
    if kind == 'bash':
        return ['/bin/bash', '--noprofile', '--norc', '-c']
    return ['/bin/sh', '-c']


def _private_directories(config, reference, create):
    base = Path(config['state_dir']).resolve() / 'project-runtimes' / reference
    paths = {key: base / name for key, name in [('HOME', 'home'), ('TMPDIR', 'tmp'),
        ('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data')]}
    if create:
        # Never follow a pre-existing link in the owner-controlled runtime tree.
        for path in [base.parent, base, *paths.values()]:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid())
                    or os.name == 'posix' and stat.S_IMODE(info.st_mode) & 0o077):
                raise DevError('RUNTIME_DIRECTORY_UNSAFE', 'Runtime storage is not an owned directory', 409)
            if path.resolve() != path:
                raise DevError('RUNTIME_DIRECTORY_UNSAFE', 'Runtime storage cannot traverse links', 409)
    return {key: str(path) for key, path in paths.items()}


def legacy_environment(inherit_env, task_env=None, *, full_access=False, parent=None):
    parent = os.environ if parent is None else parent
    environment = {k: v for k, v in parent.items() if inherit_env or k in {
        'PATH', 'HOME', 'USER', 'USERNAME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
        'SYSTEMROOT', 'SystemRoot', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'SHELL', 'NVM_DIR'}}
    environment.setdefault('GIT_TERMINAL_PROMPT', '0')
    if not full_access:
        environment.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', NO_COLOR='1', CI='1')
    environment.update(task_env or {})
    return environment


def build_environment(config, project, root, overrides=None, *, create=False):
    profile = selected(config, project)
    if profile['mode'] == 'native_compat':
        shell = config['shell']
        return legacy_environment(shell['inherit_env'], {**shell['env'], **(overrides or {})}, full_access=True)
    windows = os.name == 'nt' and profile['mode'] == 'project_clean_v1'
    try:
        extra = _environment(overrides or {}, 'operation.env', windows=windows)
    except ValueError as exc:
        raise DevError('RUNTIME_ENV_RESERVED', str(exc), 400) from exc
    root = Path(root).resolve()
    meta = descriptor(config, project, root)
    parent = _environment_keys(os.environ, windows)
    inherited = [key.upper() if windows else key for key in (*SYSTEM_KEYS, *profile['pass_env'])]
    environment = {key: parent[key] for key in inherited if key in parent}
    if profile['mode'] == 'container_v1':
        environment = {'HOME': '/home/project', 'TMPDIR': '/tmp', 'XDG_CACHE_HOME': '/home/project/.cache',
                       'XDG_CONFIG_HOME': '/home/project/.config', 'XDG_DATA_HOME': '/home/project/.local/share'}
        paths = profile['path'] or ['/usr/local/bin', '/usr/bin', '/bin']
        separator = ':'
    else:
        environment.update(_private_directories(config, meta['runtime_ref'], create))
        paths = [str((root / p).resolve()) if not Path(p).is_absolute() else p for p in profile['path']]
        paths += os.defpath.split(os.pathsep)
        separator = os.pathsep
        if os.name == 'nt':
            environment.update(USERPROFILE=environment['HOME'], APPDATA=environment['XDG_CONFIG_HOME'],
                               LOCALAPPDATA=environment['XDG_DATA_HOME'])
    environment.update(PATH=separator.join(dict.fromkeys(paths)), TEMP=environment['TMPDIR'], TMP=environment['TMPDIR'],
        LANG='C.UTF-8', GIT_TERMINAL_PROMPT='0', CI='1', NO_COLOR='1',
        CODEPIER_RUNTIME_REF=meta['runtime_ref'], CODEPIER_RUNTIME_REVISION=meta['revision'])
    environment.update(_environment_keys(profile['env'], windows))
    environment.update(_environment_keys(extra, windows))
    return environment


def resolve_executable(command, environment, cwd):
    executable = command[0]
    if '/' in executable or '\\' in executable:
        path = Path(executable)
        path = path if path.is_absolute() else Path(cwd) / path
        found = str(path.absolute()) if path.is_file() and os.access(path, os.X_OK) else None
    else:
        found = shutil.which(executable, path=environment.get('PATH', os.defpath))
    if not found:
        raise DevError('COMMAND_MISSING', 'Command is unavailable in the configured project runtime: ' + Path(executable).name)
    return [found, *command[1:]]


def runtime_readiness(config, project, root):
    """Observe the shell and configured PATH without storage or process effects.

    Ready is not proof that a named task, its imports or network dependencies
    can run. prepare resolves the actual task executable before launch.
    """
    profile = selected(config, project)
    meta = descriptor(config, project, root)
    environment = build_environment(config, project, root)
    if profile['mode'] == 'container_v1':
        from agent.container_runtime import readiness
        return {**meta, **readiness(profile['container']), 'tools': {}, 'toolchain_state': 'not_observed'}
    command = shell_command(config, project)
    try:
        resolve_executable(command, environment, root)
        state, code = 'ready', None
    except DevError as exc:
        state, code = 'unavailable', exc.code
    return {**meta, 'state': state, 'code': code, 'toolchain_state': 'configured_PATH',
            'tools': {name: bool(shutil.which(name, path=environment.get('PATH'))) for name in TOOLS}}


@dataclass(frozen=True)
class RuntimePlan:
    command: list[str]
    cwd: Path
    environment: dict[str, str]
    runtime: dict
    profile: dict


def prepare(config, project, root, command, cwd, overrides=None, *, create=True):
    profile = selected(config, project)
    root, cwd = Path(root).resolve(), Path(cwd).resolve(strict=True)
    if profile['mode'] != 'native_compat' and not cwd.is_relative_to(root):
        raise DevError('RUNTIME_CWD_OUTSIDE_WORKSPACE', 'This runtime requires a workspace-relative working directory', 403)
    requirement = project.get('_runtime_requirement')
    if requirement is not None:
        from shared.runtime_requirements import valid_requirement
        if (not valid_requirement(requirement, project.get('id'))
                or profile['mode'] != 'container_v1'
                or requirement.get('revision') != profile_revision(config, profile)):
            raise DevError('RUNTIME_ISOLATION_REQUIRED', 'The original task requires its approved isolated runtime revision', 403)
    environment = build_environment(config, project, root, overrides, create=create)
    resolved = list(command) if profile['mode'] == 'container_v1' else resolve_executable(command, environment, cwd)
    return RuntimePlan(resolved, cwd, environment, descriptor(config, project, root), profile)
