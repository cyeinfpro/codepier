"""Evidence-derived operation summaries; never authorization from output prose.

Raw output is retained. Derived diagnostics are explicitly untrusted text and
cannot become permission, platform feedback or execution success evidence.
"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
import re

RUNTIME_FIELDS = frozenset({'schema_version', 'runtime_ref', 'revision', 'mode',
    'grants_authority', 'filesystem_scope', 'isolation', 'state', 'code', 'toolchain_state'})
DENIALS = frozenset({'READ_ONLY', 'ROOT_NOT_ALLOWED', 'TASKS_DISABLED', 'SHELL_DISABLED',
    'SHELL_NOT_ALLOWED', 'INSUFFICIENT_SCOPE', 'REMOTE_PAUSED', 'CONVERSATION_ARCHIVED',
    'RUNTIME_ISOLATION_REQUIRED', 'RUNTIME_CWD_OUTSIDE_WORKSPACE', 'RUNTIME_MOUNT_UNSAFE'})
UNAVAILABLE = frozenset({'COMMAND_MISSING', 'INVALID_CWD', 'RUNTIME_ENGINE_MISSING',
    'RUNTIME_ENGINE_UNAVAILABLE', 'RUNTIME_ENGINE_FAILED', 'RUNTIME_ENGINE_UNSUPPORTED',
    'RUNTIME_IMAGE_UNSUPPORTED', 'RUNTIME_BACKEND_UNSUPPORTED', 'RUNTIME_WORKTREE_UNSUPPORTED'})
_LINE = re.compile(r'^(?P<path>(?:[A-Za-z]:)?[^\r\n:]+):(?P<line>[1-9][0-9]{0,7})(?::(?P<column>[1-9][0-9]{0,5}))?:\s*(?P<message>.+)$')
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def failure_context(*, data=None, error=None, operation_id=None, source='agent_operation'):
    data, error = data or {}, error or {}
    cleanup = data.get('runtime_cleanup') if isinstance(data.get('runtime_cleanup'), dict) else {}
    outcome = data.get('runtime_outcome') if isinstance(data.get('runtime_outcome'), dict) else {}
    code = error.get('code')
    if not isinstance(code, str):
        code = None
    if code in DENIALS or (code and code.startswith(('ROOM_SCOPE_', 'DELEGATION_', 'LEASE_', 'FENCING_'))):
        category = 'codepier_authorization'
    elif code in UNAVAILABLE or outcome.get('state') == 'not_started':
        category = 'execution_unavailable'
    elif code in {'RUNTIME_CONTROL_UNCERTAIN', 'RUNTIME_CREATION_UNCERTAIN', 'RUNTIME_CLEANUP_PENDING', 'RUNTIME_OPERATION_REQUIRES_RECOVERY'} or cleanup.get('pending') or outcome.get('state') == 'unconfirmed':
        category = 'outcome_unconfirmed'
    elif data.get('cancelled') or code in {'CANCELLED', 'INTERRUPTED'}:
        category = 'cancelled' if code != 'INTERRUPTED' else 'outcome_unconfirmed'
    elif data.get('timed_out'):
        category = 'command_timeout'
    elif type(data.get('exit_code')) is int:
        category = 'succeeded' if data['exit_code'] == 0 else 'command_failed'
    else:
        category = 'operation_failed' if error else 'not_observed'
    return {'schema_version': 1, 'category': category, 'source': source,
        'operation_id': operation_id, 'code': code, 'caller_platform_decision': 'not_observed',
        'requires_desktop_control': False, 'grants_authority': False,
        'replay_recommended': False}


def _cwd_relative_command(command):
    """Only a supported direct producer defines relative-path semantics.

    Shells, scripts, arbitrary Python code and their children may change cwd.
    Never infer their diagnostic directory from the process launch directory.
    Ruff check reports its text locations relative to its invocation cwd.
    """
    if not isinstance(command, (list, tuple)) or not command:
        return False
    command = list(command)
    executable = PureWindowsPath(command[0]).name.lower().removesuffix('.exe')
    return ((executable == 'ruff' and command[1:2] == ['check']) or
        (re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', executable) is not None
         and command[1:4] == ['-m', 'ruff', 'check']))


def _diagnostic_directories(root, cwd, workspace_root):
    """Interpret trusted launch context without inspecting any output path."""
    host_type = PureWindowsPath if PureWindowsPath(str(root)).drive else PurePosixPath
    host_root = host_type(root)
    output_root = PurePosixPath(workspace_root) if workspace_root is not None else host_root
    if not output_root.is_absolute() or '..' in output_root.parts:
        return output_root, None
    if cwd is None:
        return output_root, None
    host_cwd = host_type(cwd)
    if not host_cwd.is_absolute() or '..' in host_cwd.parts:
        return output_root, None
    if workspace_root is not None:
        try:
            # Only the verified current driver supplies the guest mount root.
            output_cwd = output_root.joinpath(*host_cwd.relative_to(host_root).parts)
        except ValueError:
            output_cwd = None
    else:
        output_cwd = host_cwd
    return output_root, output_cwd


def _diagnostic_path(value, root, cwd):
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if not root.is_absolute():
        return None
    path_type = type(root)
    if path_type is PurePosixPath and ('\\' in value or PureWindowsPath(value).drive):
        return None
    path = path_type(value)
    # Do not collapse parent components across possibly linked directories.
    if '..' in path.parts:
        return None
    if not path.is_absolute():
        if path.anchor or cwd is None:
            return None
        path = cwd / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def diagnostics(output, root, *, cwd=None, command=None, workspace_root=None):
    """Bounded, untrusted locations derived from the original output.

    The legacy two-argument call remains valid for project-absolute locations.
    Relative locations require a supported direct producer and trusted cwd;
    a '/workspace' prefix alone is never evidence of a container mapping.
    Unknown/external locations remain available in the unchanged raw output.
    """
    if not isinstance(output, str):
        return []
    result = []
    output_root, output_cwd = _diagnostic_directories(root, cwd, workspace_root)
    if not _cwd_relative_command(command):
        output_cwd = None
    tail = output[-65536:]
    if len(output) > 65536 and output[-65537] not in '\r\n':
        # A clipped line is not a complete location, even if its suffix matches.
        tail = tail.partition('\n')[2]
    for line in tail.splitlines()[-1000:]:
        match = _LINE.match(_ANSI.sub('', line))
        if not match:
            continue
        values = match.groupdict()
        path = _diagnostic_path(values['path'], output_root, output_cwd)
        if path is None:
            continue
        row = {'path': path, 'line': int(values['line']),
            'message': values['message'][:400], 'source': 'command_output', 'untrusted': True}
        if values['column']:
            row['column'] = int(values['column'])
        result.append(row)
        if len(result) == 20:
            break
    return result


def decorate_result(data, project, operation_id, runtime, root, *,
                    cwd=None, command=None, workspace_root=None):
    result = dict(data)
    result['runtime'] = {key: value for key, value in runtime.items() if key in RUNTIME_FIELDS}
    result['failure_context'] = failure_context(data=data, operation_id=operation_id)
    result['continuation'] = {'schema_version': 1, 'project_id': project.get('id'),
        'workspace_id': project.get('_coding_workspace', project.get('workspace_id', '')),
        'operation_id': operation_id, 'target': 'agent',
        'runtime_ref': runtime['runtime_ref'], 'runtime_revision': runtime['revision'],
        'grants_authority': False, 'recovery': 'query_original_operation'}
    result['log_ref'] = {'operation_id': operation_id, 'source': 'operation_output'}
    parsed = diagnostics(data.get('output', ''), root, cwd=cwd, command=command,
                         workspace_root=workspace_root)
    if parsed:
        result['diagnostics'] = parsed
    return result


def capability_index(project=None):
    # Discovery only: use existing authorized operations, not a second dispatcher.
    return {name: {'name': 'project_query', 'arguments': {'operation': 'help', 'tool': tool, 'action': action, **({'project': project} if project else {})}}
        for name, tool, action in [('search', 'process', 'search_start'),
            ('changes', 'read', 'changes'), ('validation', 'process', 'validate'),
            ('tasks', 'workspace', 'tasks'), ('artifacts', 'read', 'artifacts')]}
