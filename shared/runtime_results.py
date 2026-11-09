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


_SHELL_MISSING = re.compile(r"^(?:(?:[\w./-]+/)?(?:ba|da|z|k)?sh): "
                            r"(?:(?:line )?\d+: )?(?P<command>[\w./+-]{1,200}): (?:command )?not found$")


def missing_shell_command(output):
    if isinstance(output, str):
        for line in reversed(output[-4096:].splitlines()):
            match = _SHELL_MISSING.fullmatch(_ANSI.sub('', line))
            if match:
                return PurePosixPath(match['command']).name
    return None


def failure_context(*, data=None, error=None, operation_id=None, source='agent_operation'):
    data, error = data or {}, error or {}
    cleanup = data.get('runtime_cleanup') if isinstance(data.get('runtime_cleanup'), dict) else {}
    outcome = data.get('runtime_outcome') if isinstance(data.get('runtime_outcome'), dict) else {}
    code = error.get('code')
    if not isinstance(code, str):
        code = None
    # SSH returns a completed transport receipt (ok=True), with its diagnostic
    # in data rather than error. Keep that diagnostic without deriving a caller
    # platform decision or permission to replay from the command output.
    if code is None and type(data.get('exit_code')) is int and data['exit_code'] != 0:
        ssh_code = data.get('ssh_error')
        if isinstance(ssh_code, str) and ssh_code in {'SSH_AUTH_FAILED', 'SSH_HOST_KEY_CHANGED', 'SSH_HOST_KEY_UNTRUSTED',
                        'SSH_COMMAND_OR_CONNECTION_FAILED', 'SSH_TIMEOUT', 'SSH_CANCELLED'}:
            code = ssh_code
    if code in DENIALS or (code and code.startswith(('ROOM_SCOPE_', 'DELEGATION_', 'LEASE_', 'FENCING_'))):
        category = 'codepier_authorization'
    elif code == 'COMMAND_MISSING':
        category = 'command_missing'
    elif code in UNAVAILABLE or outcome.get('state') == 'not_started':
        category = 'execution_unavailable'
    elif code in {'RUNTIME_CONTROL_UNCERTAIN', 'RUNTIME_CREATION_UNCERTAIN', 'RUNTIME_CLEANUP_PENDING', 'RUNTIME_OPERATION_REQUIRES_RECOVERY'} or cleanup.get('pending') or outcome.get('state') == 'unconfirmed':
        category = 'outcome_unconfirmed'
    elif data.get('cancelled') or code in {'CANCELLED', 'INTERRUPTED'}:
        category = 'cancelled' if code != 'INTERRUPTED' else 'outcome_unconfirmed'
    elif data.get('timed_out'):
        category = 'command_timeout'
    elif code == 'SSH_AUTH_FAILED':
        category = 'ssh_authentication_failed'
    elif code in {'SSH_HOST_KEY_CHANGED', 'SSH_HOST_KEY_UNTRUSTED'}:
        category = 'ssh_host_verification_failed'
    elif code == 'SSH_COMMAND_OR_CONNECTION_FAILED':
        category = 'ssh_command_or_connection_failed'
    elif data.get('exit_code') == 127 and missing_shell_command(data.get('output')):
        category, code = 'command_missing', 'COMMAND_MISSING'
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


def build_storage_hints(output):
    """Bounded symptom hints, never mount detection or permission to retry."""
    if not isinstance(output, str):
        return []
    tail = _ANSI.sub('', output[-65536:]).lower()
    hints = []
    def add(code, message):
        hints.append({'code': code, 'message': message, 'source': 'command_output',
                      'untrusted': True, 'cause_confirmed': False,
                      'automatic_retry': False})
    if 'was compiled with module cache path' in tail and 'but the path is currently' in tail:
        add('BUILD_CACHE_PATH_MISMATCH',
            '编译缓存记录了不同的绝对路径。为当前源码位置和工具链选择新的本机构建目录；保留原缓存供核查，不自动删除。')
    if ('unable to rename temporary' in tail or 'failed to rename' in tail) and (
            'modulecache' in tail or 'index/store' in tail or '.swiftmodule' in tail):
        add('BUILD_CACHE_RENAME_FAILED',
            '编译器报告缓存临时文件改名失败。核查错误及挂载类型；共享卷可改用本机独立构建目录，不代表源码或权限有错。')
    if 'input file' in tail and 'was modified during the build' in tail:
        add('BUILD_INPUT_CHANGED',
            '构建器检测到输入变化。核对源码摘要及并发写入；需要稳定回归时使用本机独立源码副本，不用固定等待或盲目重试掩盖问题。')
    return hints


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
    if type(data.get('exit_code')) is int and data['exit_code'] != 0:
        hints = build_storage_hints(data.get('output', ''))
        if hints:
            result['build_storage_hints'] = hints
    return result


def capability_index(project=None):
    # Discovery only: use existing authorized operations, not a second dispatcher.
    return {name: {'name': 'project_query', 'arguments': {'operation': 'help', 'tool': tool, 'action': action, **({'project': project} if project else {})}}
        for name, tool, action in [('search', 'process', 'search_start'),
            ('changes', 'read', 'changes'), ('validation', 'process', 'validate'),
            ('tasks', 'workspace', 'tasks'), ('artifacts', 'read', 'artifacts')]}
