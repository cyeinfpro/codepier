"""Evidence-derived operation summaries; never authorization from output prose.

Raw output is retained. Derived diagnostics are explicitly untrusted text and
cannot become permission, platform feedback or execution success evidence.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

RUNTIME_FIELDS = frozenset({'schema_version', 'runtime_ref', 'revision', 'mode',
    'grants_authority', 'filesystem_scope', 'isolation', 'state', 'code', 'toolchain_state'})
DENIALS = frozenset({'READ_ONLY', 'ROOT_NOT_ALLOWED', 'TASKS_DISABLED', 'SHELL_DISABLED',
    'SHELL_NOT_ALLOWED', 'INSUFFICIENT_SCOPE', 'REMOTE_PAUSED', 'CONVERSATION_ARCHIVED',
    'RUNTIME_ISOLATION_REQUIRED', 'RUNTIME_CWD_OUTSIDE_WORKSPACE', 'RUNTIME_MOUNT_UNSAFE'})
UNAVAILABLE = frozenset({'COMMAND_MISSING', 'INVALID_CWD', 'RUNTIME_ENGINE_MISSING',
    'RUNTIME_ENGINE_UNAVAILABLE', 'RUNTIME_ENGINE_FAILED', 'RUNTIME_ENGINE_UNSUPPORTED',
    'RUNTIME_IMAGE_UNSUPPORTED', 'RUNTIME_BACKEND_UNSUPPORTED', 'RUNTIME_WORKTREE_UNSUPPORTED'})
_LINE = re.compile(r'^(?P<path>[^\r\n:]+):(?P<line>[1-9][0-9]{0,7})(?::(?P<column>[1-9][0-9]{0,5}))?:\s*(?P<message>.+)$')


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


def diagnostics(output, root):
    result = []
    prefix = str(root).replace('\\', '/').rstrip('/') + '/'
    for line in output[-65536:].splitlines()[-1000:]:
        match = _LINE.match(line)
        if not match:
            continue
        values = match.groupdict()
        path = values['path'].replace('\\', '/')
        if path.startswith(prefix):
            path = path[len(prefix):]
        elif path.startswith('/workspace/'):
            path = path[len('/workspace/'):]
        target = PurePosixPath(path)
        if target.is_absolute() or '..' in target.parts or ':' in path or not path:
            continue
        row = {'path': target.as_posix(), 'line': int(values['line']),
            'message': values['message'][:400], 'source': 'command_output', 'untrusted': True}
        if values['column']:
            row['column'] = int(values['column'])
        result.append(row)
        if len(result) == 20:
            break
    return result


def decorate_result(data, project, operation_id, runtime, root):
    result = dict(data)
    result['runtime'] = {key: value for key, value in runtime.items() if key in RUNTIME_FIELDS}
    result['failure_context'] = failure_context(data=data, operation_id=operation_id)
    result['continuation'] = {'schema_version': 1, 'project_id': project.get('id'),
        'workspace_id': project.get('_coding_workspace', project.get('workspace_id', '')),
        'operation_id': operation_id, 'target': 'agent',
        'runtime_ref': runtime['runtime_ref'], 'runtime_revision': runtime['revision'],
        'grants_authority': False, 'recovery': 'query_original_operation'}
    result['log_ref'] = {'operation_id': operation_id, 'source': 'operation_output'}
    parsed = diagnostics(data.get('output', ''), root)
    if parsed:
        result['diagnostics'] = parsed
    return result


def capability_index(project=None):
    # Discovery only: use existing authorized operations, not a second dispatcher.
    return {name: {'name': 'project_query', 'arguments': {'operation': 'help', 'tool': tool, 'action': action, **({'project': project} if project else {})}}
        for name, tool, action in [('search', 'process', 'search_start'),
            ('changes', 'read', 'changes'), ('validation', 'process', 'validate'),
            ('tasks', 'workspace', 'tasks'), ('artifacts', 'read', 'artifacts')]}
