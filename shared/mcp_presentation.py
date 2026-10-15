"""Minimal host metadata for native MCP results, never an execution sandbox.

Apply on copies at the MCP boundary only. Source, diffs, command output, native
media, caller arguments and upstream MCP results are not rewritten. The panel,
Agent protocol and durable receipts keep their original data. Explicit skill
reads retain executable resource locations; summaries use skill IDs instead.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re


_HOME = re.compile(r'(?<![\w])(?:/(?:Users|home)/[^/\r\n\'"<>:]+|[A-Za-z]:[\\/]Users[\\/][^\\/\r\n\'"<>:]+)(?=[\\/\r\n\'"<>:]|$)')
_OPAQUE = frozenset({'content', 'preview', 'text', 'output', 'diff', 'old_text',
    'new_text', 'arguments', 'args', 'options', 'inputSchema', 'outputSchema',
    '_computer_content', 'elements', 'resources'})
_IDENTITY = frozenset({'platform', 'user', 'uid', 'credential_environment_present', 'tool_lookup'})
_SHELL = frozenset({'enabled', 'configured_enabled', 'denial', 'executable_available',
    'max_timeout_seconds', 'interactive', 'caller_permitted', 'effective_ready', 'next', 'executable'})
_EXECUTION = frozenset({'operation_id', 'pending', 'state', 'next', 'next_call',
    'retry_after_seconds', 'deadline', 'elapsed_seconds', 'execution_policy',
    'shell', 'tools', 'ssh', 'authorization'})


def node_label(identifier):
    return 'node-' + hashlib.sha256(str(identifier).encode()).hexdigest()[:12]


def error_view(value):
    """Remove account home prefixes from error metadata, retaining codes/details.

    This is deliberately not a generic path scrubber or a source/log filter.
    Arbitrary command output and explicit source reads can disclose host data.
    """
    if isinstance(value, str):
        return _HOME.sub('[account-home]', value)
    if isinstance(value, list):
        return [error_view(item) for item in value]
    if isinstance(value, dict):
        return {key: deepcopy(item) if key in _OPAQUE else error_view(item)
                for key, item in value.items()}
    return value


VIEW_VERSION = 1
_RECEIPT = frozenset({'operation_id', 'pending', 'state', 'next', 'next_call',
    'retry_after_seconds', 'deadline', 'elapsed_seconds', 'recovery', 'error',
    'request_id', 'code', 'message', 'retryable', 'note'})
_PROJECT = frozenset({'id', 'alias', 'project', 'project_id', 'workspace_id',
    'root', 'description', 'mode', 'allow_tasks', 'online', 'connection',
    'granted_scopes', 'capabilities', 'execution_targets', 'project_label'})
_OPERATION = _RECEIPT | frozenset({'id', 'project_id', 'tool', 'created', 'updated',
    'accepted_at', 'attempts', 'next_attempt', 'cancel_requested', 'result',
    'output', 'output_seq', 'output_chars', 'output_truncated', 'output_omitted',
    'output_unchanged', 'result_omitted', 'execution', 'current', 'events',
    'next_after_event_id', 'args_summary', 'transport_error', 'last_output_at',
    'last_operation_verified_at', 'diagnostic_write_errors', 'clock_note',
    'history_limit', 'history_may_be_pruned', 'validation', 'review', 'artifact',
    'exit_code', 'timed_out', 'cancelled', 'command_ok', 'work_item_id', 'attempt',
    'fencing_token', 'goal_id', 'operation_role', 'supersedes_operation_id'})
_CONTEXT = frozenset({'project', 'project_root', 'documents', 'instruction_index',
    'skills', 'codex_skills', 'skills_catalog', 'root_entries', 'truncated',
    'remaining_documents', 'warnings', 'budget', 'scope', 'trust', 'next'})
_FILE_INDEX = frozenset({'path', 'sha256', 'bytes', 'state', 'code', 'next',
    'must_read_before_edit'})
_DOCUMENT = _FILE_INDEX | frozenset({'content', 'total_lines', 'start_line',
    'end_line', 'offset', 'truncated', 'next_offset', 'next_start_line'})
_OPEN = _RECEIPT | frozenset({'context_id', 'context_unchanged', 'workspace',
    'context', 'context_detail', 'view_version', 'observed_at', 'truncated',
    'baseline_ref', 'baseline_coverage', 'baseline_expires_at', 'context_scope',
    'context_refresh_required', 'compatibility'})
_EXEC_RESULT = _RECEIPT | frozenset({'exit_code', 'output', 'output_truncated',
    'output_omitted', 'timed_out', 'cancelled', 'duration_ms', 'command_ok',
    'target', 'resource_coordination', 'host', 'port', 'username', 'ssh_error'})
_STATUS_POLICY = frozenset({'version', 'local_block_codex', 'effective_block_codex',
    'local_block_native_computer', 'effective_block_native_computer',
    'split_capabilities', 'enforcement', 'os_sandbox', 'state', 'denial', 'code',
    'message', 'restrictions_unknown', 'unrecognized_denials'})
_RULES = ('AGENTS.md', 'MEMORY.md', 'CLAUDE.md')


def _pick(value, fields):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {key: deepcopy(item) for key, item in value.items() if key in fields}
    for key in ('error', 'errors', 'warnings', 'denial', 'transport_error', 'scan_error'):
        if key in result:
            result[key] = error_view(result[key])
    return result


def _project(value, *, management=False):
    if not isinstance(value, dict):
        return deepcopy(value)
    fields = _PROJECT | ({'device_id', 'device_name', 'created_by_role', 'role_access'} if management else set())
    result = _pick(value, fields)
    if result.get('root') is not None:
        result['root'] = '.'
    if isinstance(result.get('connection'), dict):
        result['connection'] = _pick(result['connection'], {'state', 'source', 'observed_at'})
    # Preserve the stable, authorized management identifier, never in ordinary lists.
    if management and 'device_name' in result:
        result['device_name'] = node_label(result['device_id']) if result.get('device_id') else 'execution-node'
    return result


def _policy_denials(value):
    """Keep refusal evidence from native policy extensions, not arbitrary bodies.

    Only the native execution-policy caller uses this visitor. Unknown paths,
    prose and identity metadata never become public. No decision is upgraded to
    an authorization grant and no shell/action configuration is changed.
    """
    found = []
    def visit(item, refusal=False):
        if isinstance(item, list):
            for child in item:
                visit(child, refusal)
        elif isinstance(item, dict):
            denied = refusal or item.get('state') == 'denied' or item.get('allowed') is False
            if denied:
                row = {'state': 'denied', 'message': 'A native policy check reported a refusal.'}
                code = item.get('code')
                if isinstance(code, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}', code):
                    row['code'] = code
                if row not in found:
                    found.append(row)
            for key, child in item.items():
                if key not in _OPAQUE:
                    visit(child, key in {'denial', 'error'} and isinstance(child, dict))
    visit(value)
    return found


def _execution(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = _pick(value, _EXECUTION | _RECEIPT)
    result['project_root'] = '.'
    shell = value.get('shell', {})
    result['shell'] = _pick(shell, _SHELL)
    command = shell.get('command')
    if isinstance(command, list) and command and isinstance(command[0], str):
        result['shell']['executable'] = re.split(r'[\\/]', command[0])[-1]
    result['shell']['filesystem_scope'] = 'Execution account permissions; not restricted to the project directory.'
    policy = value.get('execution_policy')
    if isinstance(policy, dict):
        result['execution_policy'] = _pick(policy, _STATUS_POLICY)
        extra = {key: item for key, item in policy.items() if key not in _STATUS_POLICY}
        if extra:
            result['execution_policy']['restrictions_unknown'] = True
        denials = _policy_denials([extra, policy.get('unrecognized_denials', [])])
        if denials:
            result['execution_policy']['unrecognized_denials'] = denials
    if isinstance(value.get('tools'), dict):
        result['tools'] = {name: bool(path) for name, path in value['tools'].items()
            if name in {'git', 'ssh', 'sshpass', 'gh', 'docker', 'python3', 'uv', 'node', 'npm', 'pnpm'}}
    if isinstance(value.get('ssh'), dict):
        result['ssh'] = _pick(value['ssh'], {'tool', 'password_auth_available', 'host_key_policy', 'local_script_password'})
    if isinstance(value.get('authorization'), dict):
        result['authorization'] = _pick(value['authorization'], {'known', 'granted_scopes', 'panel_owner'})
    result['note'] = ('Commands run on the workspace execution node with its account permissions. '
        'This is not an OS sandbox. File tools enforce their own project path rules. '
        'No automatic command backup, rollback or interactive input.')
    return result


def _skill_summary(value):
    fields = {'skill_id', 'name', 'description', 'source', 'sha256', 'bytes',
        'allow_implicit_invocation', 'enabled', 'path', 'next', 'resource_path'}
    result = _pick(value, fields)
    if isinstance(result, dict) and 'skill_id' in result:
        result.update(path='SKILL.md', resource_path='SKILL.md')
    return result


def _context(value, detail):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = _pick(value, _CONTEXT)
    result['project_root'] = '.'
    result['documents'] = [_pick(item, _DOCUMENT if detail == 'documents' else _FILE_INDEX)
                           for item in value.get('documents', [])]
    result['skills'] = [_skill_summary(item) for item in value.get('skills', [])]
    result['codex_skills'] = [_skill_summary(item) for item in value.get('codex_skills', [])]
    if isinstance(result.get('skills_catalog'), dict):
        result['skills_catalog'] = _pick(result['skills_catalog'], {'next', 'loaded',
            'codex_enabled_for_project', 'catalog_sha256', 'truncated', 'has_more', 'warnings'})
    if 'instruction_index' in value:
        result['instruction_index'] = [_pick(item, _FILE_INDEX) for item in value['instruction_index']]
    else:
        # Legacy Agents did not index every rule: omission is NOT evidence of absence.
        indexed = {item['path']: item for item in result['documents'] if isinstance(item, dict) and 'path' in item}
        result['instruction_index'] = [
            {**_pick(indexed[name], _FILE_INDEX), 'state': 'available', 'must_read_before_edit': True}
            if name in indexed else {'path': name, 'state': 'not_indexed', 'next': 'read'}
            for name in _RULES]
    entries = value.get('root_entries', [])
    result['root_entries'] = [_pick(item, {'path', 'type'}) for item in entries[:40]]
    if len(entries) > 40:
        result['truncated'] = True
    if isinstance(result.get('budget'), dict):
        result['budget'] = _pick(result['budget'], {'preview_chars', 'max_chars', 'max_files'})
        if detail != 'documents':
            result['budget']['preview_chars'] = 0
    return result


def _open(value, args):
    result = _pick(value, _OPEN)
    # Use the stored backend detail when rendering a receipt, not a polling caller's options.
    detail = args.get('context_detail', value.get('context_detail', 'minimal'))
    result['context_detail'] = detail
    result['mcp_view_version'] = VIEW_VERSION
    if isinstance(value.get('workspace'), dict):
        result['workspace'] = _project(value['workspace'])
    if 'context' in value:
        result['context'] = _context(value['context'], detail)
    if value.get('context_unchanged') and value.get('view_version') != VIEW_VERSION:
        # Old receipt contains no body from which to construct the new index.
        # Never pretend this was a fresh minimal-context observation.
        result['context_refresh_required'] = True
        result['compatibility'] = 'LEGACY_CONTEXT_REFRESH_REQUIRED'
    return result


def _build(value):
    result = deepcopy(value)
    if isinstance(result, dict) and isinstance(result.get('runtime'), dict):
        result['runtime'].pop('pid', None)
    return result


def _devices(value):
    result = deepcopy(value)
    names = {}
    for device in result.get('devices', []):
        if not isinstance(device, dict) or 'id' not in device:
            continue
        original = device.get('name')
        device['name'] = node_label(device['id'])
        if isinstance(original, str) and original:
            names[original] = device['name']
        for key in ('platform', 'hostname', 'python', 'roots'):
            device.pop(key, None)
        if 'build' in device:
            device['build'] = _build(device['build'])
    if names and isinstance(result.get('warnings'), list):
        pattern = re.compile('|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
        result['warnings'] = [pattern.sub(lambda match: names[match[0]], item)
                             if isinstance(item, str) else item for item in result['warnings']]
    if 'hub' in result:
        result['hub'] = _build(result['hub'])
    return result


def _operation(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = _pick(value, _OPERATION)
    args = value.get('args_summary') or {}
    if isinstance(args, dict) and 'args_summary' in result:
        result['args_summary'] = _pick(args, {'project', 'workspace_id', 'task', 'target',
            'timeout_seconds', 'operation_kind', 'context_detail'})
    if isinstance(value.get('result'), dict):
        saved = value['result']
        result['result'] = _pick(saved, {'ok', 'error', 'data'})
        if 'data' in saved:
            result['result']['data'] = _native(value.get('tool', ''), args, saved['data'])
    if isinstance(value.get('execution'), dict):
        result['execution'] = _trace(value['execution'])
    if isinstance(value.get('current'), dict):
        result['current'] = _pick(value['current'], {'state', 'reason', 'online',
            'last_agent_stage', 'stage_observed_at', 'transport_error', 'attempts', 'elapsed_ms'})
    if isinstance(value.get('events'), list):
        result['events'] = [_pick(item, {'id', 'operation_id', 'source', 'seq', 'stage',
            'at', 'elapsed_ms', 'blocked_by', 'label', 'detail'}) for item in value['events']]
    return result


def _trace(value):
    return _operation(value)


def _readiness(value):
    if value.get('operation_kind'):
        from shared.project_view_contracts import ACTION_READINESS, platform_unknown
        result = _pick(value, set(ACTION_READINESS['properties']) | _RECEIPT)
        for key in ('codepier_authorization', 'execution_readiness'):
            if isinstance(result.get(key), dict):
                result[key] = _pick(result[key], {'state', 'source', 'observed_at', 'scope', 'code', 'freshness'})
        # There is deliberately no platform-feedback parser in this contract.
        result['caller_platform_decision'] = platform_unknown()
        result['operation_evidence'] = None
    else:
        result = _pick(value, {'build', 'execution', 'admission', 'language_servers',
            'browser', 'capabilities', 'checks', 'local_control', 'file_import', 'note'} | _RECEIPT)
        if 'execution' in result:
            result['execution'] = _execution(result['execution'])
        if 'build' in result:
            result['build'] = _build(result['build'])
    result['mcp_view_version'] = VIEW_VERSION
    return result


# Only traverse declared native record containers. Free-form messages, source,
# evidence documents, command output, candidate plans and user result bodies are
# never parsed or traversed. A user dictionary shaped like execution_info is data.
_COLLAB_CONTAINERS = frozenset({'spec', 'scope', 'scope_diagnostics', 'setup',
    'goal', 'goals', 'policy', 'policies', 'delegation', 'delegations',
    'work_item', 'work_items', 'task_scope', 'working_scope', 'project_scope',
    'project_scopes', 'project_rules', 'target_snapshot', 'execution_targets',
    'connection', 'manifest', 'room_scope', 'working_project_scope',
    'consumer_configuration', 'slot', 'slots', 'dot', 'dots', 'room', 'rooms'})


def _collaboration(value):
    if isinstance(value, list):
        return [_collaboration(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    result.pop('_meta', None)
    for key, item in value.items():
        if key in {'project_snapshot', 'project'} and isinstance(item, dict):
            result[key] = _project(item)
        elif key == 'projects' and isinstance(item, list):
            result[key] = [_project(project) for project in item]
        elif key in _COLLAB_CONTAINERS:
            result[key] = _collaboration(item)
        elif key in {'operation', 'receipt'} and isinstance(item, dict):
            result[key] = _operation(item)
        elif key in {'operations', 'operation_summaries'} and isinstance(item, list):
            result[key] = [_operation(row) for row in item]
    return result


def _native(name, args, value):
    if not isinstance(value, dict):
        return deepcopy(value)
    from shared.public_collaboration import PUBLIC_TOOLS, LEGACY_TOOLS
    if name in PUBLIC_TOOLS | LEGACY_TOOLS:
        return _collaboration(value)
    if value.get('pending') or 'context_id' not in value and set(value) <= _RECEIPT:
        return _pick(value, _RECEIPT)
    if name == 'projects_list':
        result = _pick(value, {'projects', 'total', 'offset', 'next_offset', 'query', 'view_version'} | _RECEIPT)
        result['projects'] = [_project(row) for row in value.get('projects', [])]
        result['mcp_view_version'] = VIEW_VERSION
        return result
    if name in {'projects_resolve', 'projects_create'}:
        return {**_project(value, management=name == 'projects_create'), 'mcp_view_version': VIEW_VERSION}
    if name == 'open_workspace':
        return _open(value, args)
    if name == 'project_context':
        # Explicit legacy context remains a bounded document request, not an environment request.
        return {**_pick(value, _RECEIPT), **_context(value, 'documents'), 'mcp_view_version': VIEW_VERSION}
    if name == 'execution_info':
        return _execution(value)
    if name == 'readiness_get':
        return _readiness(value)
    if name in {'exec', 'shell_exec', 'tasks_run', 'ssh_exec', 'vps_exec'}:
        return _pick(value, _EXEC_RESULT)
    if name in {'operations_get', 'operations_wait', 'operations_cancel', 'operations_trace'}:
        return _operation(value)
    if name == 'operations_list':
        return {**_pick(value, {'next_before_created'}),
                'operations': [_operation(row) for row in value.get('operations', [])]}
    if name in {'devices_list', 'diagnostics_get'}:
        return _devices(value)
    if name == 'skills_read':
        return deepcopy(value)  # Explicit resource content and executable locations are real.
    if name == 'skills_list':
        result = deepcopy(value)
        result['skills'] = [_skill_summary(row) for row in value.get('skills', [])]
        if isinstance(value.get('sources'), list):
            result['sources'] = [_pick(row, {'source', 'scope', 'enabled', 'reason'}) for row in value['sources']]
        result['project_root'] = '.'
        return result
    if name == 'tasks_list':
        result = deepcopy(value)
        result['tasks'] = [_pick(row, {'name', 'description', 'cwd', 'timeout_seconds',
            'executable_available', 'available', 'enabled', 'denial'}) for row in value.get('tasks', [])]
        return result
    if name in {'worktrees_create', 'worktrees_list', 'worktrees_remove'}:
        result = deepcopy(value)
        rows = result.get('workspaces', []) if name == 'worktrees_list' else [result]
        for row in rows:
            if isinstance(row, dict):
                if 'path' in row:
                    row['path'] = '.'
                row.pop('identity', None)
                row.pop('device_id', None)
        return result
    if name == 'workspace_status':
        result = deepcopy(value)
        if result.get('root') is not None:
            result['root'] = '.'
        for key in ('recent_operations', 'evidence'):
            if isinstance(result.get(key), list):
                result[key] = [_operation(row) for row in result[key]]
        return result
    if name == 'vps':
        result = deepcopy(value)
        for row in result.get('vps', []):
            row['projects'] = [_project(project) for project in row.get('projects', [])]
        return result
    if name in {'read', 'write', 'edit', 'fs_read', 'fs_read_many', 'fs_write', 'fs_edit', 'apply_patch', 'show_changes'}:
        from shared.contracts import OUTPUT_SCHEMAS
        fields = set(OUTPUT_SCHEMAS[name].get('properties', {})) | _RECEIPT | {'_computer_content'}
        result = _pick(value, fields)
        if value.get('batch') is True and isinstance(result.get('files'), list):
            result['files'] = [_pick(item, _DOCUMENT | {'index', 'ok', 'error', 'budget_limited'}) for item in result['files']]
        return result
    # Other native file/patch/GUI/validation payloads already have explicit data
    # contracts. Only protocol-added metadata is excluded; contents are untouched.
    result = deepcopy(value)
    result.pop('_meta', None)
    for key in ('error', 'errors', 'warnings', 'denial', 'transport_error', 'scan_error'):
        if key in result:
            result[key] = error_view(result[key])
    return result


def present(name, arguments, value):
    if name in {'get_profile', 'get_access_context'}:
        return deepcopy(value)  # v2 connection diagnostics keep their required contract.
    from shared.public_collaboration import PUBLIC_TOOLS, LEGACY_TOOLS
    if name in PUBLIC_TOOLS | LEGACY_TOOLS:
        return _collaboration(value)
    if isinstance(value, dict) and {'id', 'tool', 'state'} <= value.keys():
        return _operation(value)
    if name in {'workspace', 'project_query'}:
        from shared.core_contracts import CORE_ACTIONS
        actions = {'list': 'projects_list', 'open': 'open_workspace', 'skills': 'skills_list',
            'skill': 'skills_read', 'tasks': 'tasks_list', 'status': 'execution_info',
            'readiness': 'readiness_get', 'tree': 'fs_tree', **CORE_ACTIONS['workspace']}
        name = actions.get(arguments.get('operation', 'list'), name)
        arguments = {**arguments, **arguments.get('options', {})}
    elif name in {'process', 'task_query'}:
        if arguments.get('operation', 'list') in {'list', 'get', 'wait', 'cancel', 'trace'}:
            if isinstance(value, dict) and 'operations' in value:
                return {**_pick(value, {'next_before_created', 'media_truncated'}),
                        'operations': [_operation(row) for row in value['operations']]}
        from shared.core_contracts import CORE_ACTIONS
        name = {**CORE_ACTIONS['process'], 'diagnostics': 'diagnostics_get',
            'activity': 'activity_list'}.get(arguments.get('operation'), name)
    return _native(name, arguments, value)


def output_schema(name, schema):
    """Reflect the MCP projection without changing the internal Agent contract."""
    if name not in {'workspace', 'project_query'}:
        return deepcopy(schema)

    def visit(node):
        if isinstance(node, list):
            return [visit(child) for child in node]
        if not isinstance(node, dict):
            return node
        result = {key: visit(child) for key, child in node.items()}
        properties = result.get('properties')
        if isinstance(properties, dict):
            for key in _IDENTITY:
                properties.pop(key, None)
            def availability(tools):
                item = tools.get('additionalProperties', {})
                if isinstance(item, dict) and any(choice.get('type') == 'string' for choice in item.get('anyOf', [])):
                    tools['additionalProperties'] = {'type': 'boolean'}
                for choice in tools.get('anyOf', []):
                    availability(choice)
            availability(properties.get('tools', {}))
        if isinstance(result.get('required'), list):
            result['required'] = [key for key in result['required'] if key not in _IDENTITY]
        return result

    return visit(schema)
