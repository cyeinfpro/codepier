"""Minimal host metadata for native MCP results, never an execution sandbox.

Apply on copies at the MCP boundary only. Source, diffs, command output, native
media, caller arguments and upstream MCP results are not rewritten. The panel,
Agent protocol and durable receipts keep their original data. Explicit skill
reads retain executable resource locations; summaries use skill IDs instead.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import posixpath
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
    'shell', 'tools', 'ssh', 'authorization', 'runtime'})


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
    'must_read_before_edit', 'read_hint'})
_DOCUMENT = _FILE_INDEX | frozenset({'content', 'total_lines', 'start_line',
    'end_line', 'offset', 'truncated', 'next_offset', 'next_start_line'})
_OPEN = _RECEIPT | frozenset({'context_id', 'context_unchanged', 'workspace',
    'context', 'context_detail', 'view_version', 'observed_at', 'truncated',
    'baseline_ref', 'baseline_coverage', 'baseline_expires_at', 'context_scope',
    'context_refresh_required', 'compatibility', 'runtime', 'capability_index', 'knowledge'})
_EXEC_RESULT = _RECEIPT | frozenset({'exit_code', 'output', 'output_truncated',
    'output_omitted', 'timed_out', 'cancelled', 'duration_ms', 'command_ok',
    'target', 'resource_coordination', 'host', 'port', 'username', 'ssh_error', 'ssh_diagnostics', 'transport_runtime',
    'runtime', 'failure_context', 'continuation', 'log_ref', 'diagnostics', 'build_storage_hints', 'execution_isolation', 'runtime_cleanup', 'runtime_outcome'})
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


def _runtime(value):
    from shared.runtime_results import RUNTIME_FIELDS
    return _pick(value, RUNTIME_FIELDS)


def _exec_result(value):
    result = _pick(value, _EXEC_RESULT)
    fields = {
        'runtime': None,
        'transport_runtime': {'mode', 'target', 'isolation', 'grants_authority'},
        'ssh_diagnostics': {'schema_version', 'source', 'log_available', 'log_truncated', 'log_cleanup',
            'authentication', 'failure_stage', 'credential_source', 'password_prompt_limit',
            'connection_attempt_limit', 'automatic_replay', 'retryable', 'attempt_count',
            'attempt_limit', 'attempts', 'retry_policy', 'retry_stop_reason', 'retry_exhausted'},
        'failure_context': {'schema_version', 'category', 'source', 'operation_id', 'code',
            'caller_platform_decision', 'requires_desktop_control', 'grants_authority', 'replay_recommended'},
        'continuation': {'schema_version', 'project_id', 'workspace_id', 'operation_id', 'target',
            'runtime_ref', 'runtime_revision', 'grants_authority', 'recovery'},
        'log_ref': {'operation_id', 'source'},
        'execution_isolation': {'state', 'driver', 'evidence_sha256', 'filesystem_scope', 'network', 'host_credentials_inherited'},
        'runtime_cleanup': {'state', 'pending', 'code'},
        'runtime_outcome': {'state', 'source', 'workload_exit_code', 'transport_exit_code', 'oom_killed'},
    }
    for key, allowed in fields.items():
        if key in result:
            result[key] = _runtime(result[key]) if key == 'runtime' else _pick(result[key], allowed)
    if isinstance(result.get('ssh_diagnostics'), dict) and isinstance(result['ssh_diagnostics'].get('attempts'), list):
        from shared.ssh_diagnostics import SSH_ATTEMPT_LIMIT, safe_ssh_detail
        result['ssh_diagnostics']['attempts'] = [safe_ssh_detail(row) for row in
            result['ssh_diagnostics']['attempts'][:SSH_ATTEMPT_LIMIT] if isinstance(row, dict)]
    if isinstance(result.get('diagnostics'), list):
        result['diagnostics'] = [_pick(row, {'path', 'line', 'column', 'message', 'source', 'untrusted'})
                                  for row in result['diagnostics'][:20]]
    if 'build_storage_hints' in result:
        rows = result['build_storage_hints']
        allowed = {'BUILD_CACHE_PATH_MISMATCH', 'BUILD_CACHE_RENAME_FAILED', 'BUILD_INPUT_CHANGED'}
        result['build_storage_hints'] = [
            {'code': row['code'], 'message': row['message'][:400],
             'source': 'command_output', 'untrusted': True,
             'cause_confirmed': False, 'automatic_retry': False}
            for row in rows[:3] if isinstance(row, dict)
            and isinstance(row.get('code'), str) and row['code'] in allowed
            and isinstance(row.get('message'), str)] if isinstance(rows, list) else []
    return result


def _execution(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = _pick(value, _EXECUTION | _RECEIPT)
    result['project_root'] = '.'
    if 'runtime' in result:
        result['runtime'] = _runtime(result['runtime'])
    shell = value.get('shell', {})
    result['shell'] = _pick(shell, _SHELL)
    command = shell.get('command')
    if isinstance(command, list) and command and isinstance(command[0], str):
        result['shell']['executable'] = re.split(r'[\\/]', command[0])[-1]
    container = isinstance(result.get('runtime'), dict) and result['runtime'].get('mode') == 'container_v1'
    result['shell']['filesystem_scope'] = ('Approved workspace bind; isolation must be verified before each start.' if container else
        'Execution account permissions; not restricted to the project directory.')
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
    if container:
        result['note'] = 'Configured isolated runtime; this query is not proof of execution or isolation. The driver must verify each created container before starting project code. No native fallback.'
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
    if 'runtime' in result:
        result['runtime'] = _runtime(result['runtime'])
    if 'capability_index' in result:
        from shared.runtime_results import capability_index
        result['capability_index'] = capability_index(args.get('project'))
    result['mcp_view_version'] = VIEW_VERSION
    if isinstance(value.get('workspace'), dict):
        result['workspace'] = _project(value['workspace'])
    if isinstance(value.get('git'), dict):
        result['git'] = _pick(value['git'], {'available', 'reason', 'branch', 'upstream',
            'head', 'ahead', 'behind', 'dirty', 'network_checked', 'last_fetch_age_seconds', 'warning'})
    if 'context' in value:
        result['context'] = _context(value['context'], detail)
    if isinstance(value.get('knowledge'), dict):
        result['knowledge'] = _pick(value['knowledge'], {'state', 'revision', 'generated_at', 'entries_count',
                                                        'scan_scope', 'skipped_files', 'maintenance', 'maintenance_request', 'next'})
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
        return _exec_result(value)
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
        if 'runtime' in result:
            result['runtime'] = _runtime(result['runtime'])
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
    if name in {'read', 'write', 'edit', 'fs_read', 'fs_read_many', 'fs_write', 'fs_edit', 'apply_patch', 'show_changes', 'artifacts_register'}:
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
            'readiness': 'readiness_get', 'tree': 'fs_tree', 'search': 'fs_search', **CORE_ACTIONS['workspace']}
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
            if properties.get('mcp_view_version') == {'const': 1}:
                properties['mcp_view_version'] = {'enum': [1, 2]}
            if 'entries' in properties:
                properties['entries'] = {'anyOf': [properties['entries'], {'type': 'string'}]}
            for key in _IDENTITY:
                properties.pop(key, None)
            def resolved(value, seen=frozenset()):
                reference = value.get('$ref') if isinstance(value, dict) else None
                if (set(value) == {'$ref'} if isinstance(value, dict) else False) and isinstance(reference, str) and reference.startswith('#/$defs/') and reference not in seen:
                    target = schema.get('$defs', {}).get(reference[len('#/$defs/'):])
                    if isinstance(target, dict):
                        return resolved(target, seen | {reference})
                return value
            def availability(tools, seen=frozenset()):
                # Factoring can put the map or its value union behind a $ref.
                # Clone only this tools occurrence: the same definition may be
                # used by unrelated path/string maps whose types must not change.
                if not isinstance(tools, dict):
                    return tools
                reference = tools.get('$ref')
                if reference in seen:
                    return tools
                candidate = deepcopy(resolved(tools))
                item = resolved(candidate.get('additionalProperties', {}))
                if isinstance(item, dict) and (isinstance(item.get('type'), list) and 'string' in item['type'] or any(isinstance(resolved(choice), dict) and resolved(choice).get('type') == 'string' for choice in item.get('anyOf', []))):
                    candidate['additionalProperties'] = {'type': 'boolean'}
                for union in ('anyOf', 'oneOf', 'allOf'):
                    if union in candidate:
                        candidate[union] = [availability(choice, seen | ({reference} if reference else set())) for choice in candidate[union]]
                return candidate if candidate != resolved(tools) else tools
            if 'tools' in properties:
                properties['tools'] = availability(properties['tools'])
        if isinstance(result.get('required'), list):
            result['required'] = [key for key in result['required'] if key not in _IDENTITY]
        return result

    return visit(schema)


def path_label(path):
    if path.startswith(('"', ' ')) or any(ord(char) < 32 or char in '\x7f\u0085\u2028\u2029' for char in path):
        return json.dumps(path, ensure_ascii=True)
    return path


def _tree_lines(entries, prefix, *, indent):
    rows, parents = [], []
    for entry in entries:
        path = entry.get('path', '')
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        parent, _, leaf = path.rpartition('/')
        while parents and parents[-1] != parent:
            parents.pop()
        label = ' ' * len(parents) + path_label(leaf) if indent and parents else path_label(path)
        directory = entry.get('type') == 'directory'
        rows.append(label + ('/' if directory else '\t' + str(entry.get('size', ''))))
        if directory and indent:
            parents.append(path)
    return '\n'.join(rows)


def _compact_search(result):
    groups = {}
    for match in result['matches']:
        line = f"{match.get('line')}:{match.get('text', '').strip()}"
        groups.setdefault(path_label(match.get('path', '')), []).append(
            line + (' …' if match.get('line_truncated') else ''))
    result['matches'] = '\n'.join(path + '\n' + '\n'.join(rows) for path, rows in groups.items())
    result['matches_format'] = 'path_line_then_line_colon_text'
    status = result.get('scan_status')
    if status in {'matches', 'page_limited', 'no_matches', 'end_of_results', 'no_files'} and not result.get('scan_error'):
        result.pop('limits', None)
    if status in {'matches', 'page_limited', 'no_matches', 'end_of_results'} and not result.get('skipped_reasons'):
        result.pop('hint', None)
    for key in ('hint', 'scan_error', 'skipped_reasons', 'skipped_files'):
        if not result.get(key):
            result.pop(key, None)


def token_view(name, arguments, value, *, view=2, verbosity='lean', output_mode='raw'):
    """Versioned presentation-only savings; never visit arbitrary user bodies."""
    result = deepcopy(value)
    if view == 1 or verbosity == 'full' or not isinstance(result, dict):
        return result

    def receipt(row):
        if not isinstance(row, dict):
            return
        # Null error means no error. Keep required null continuation fields and
        # all substantive denial/failure evidence, including unknown extensions.
        if row.get('error', False) is None:
            row.pop('error')
        nested = row.get('result')
        data = nested.get('data') if isinstance(nested, dict) else None
        if isinstance(nested, dict) and nested.get('error', False) is None:
            nested.pop('error')
        if isinstance(data, dict) and row.get('tool') in {'write', 'edit', 'fs_write', 'fs_edit', 'apply_patch'}:
            tool = 'write' if row['tool'] in {'write', 'fs_write'} else 'edit'
            nested['data'] = token_view(tool, row.get('args_summary') or {}, data, view=view, verbosity=verbosity)

    if {'id', 'tool', 'state'} <= result.keys():
        receipt(result)
    elif name in {'process', 'task_query'} and isinstance(result.get('operations'), list):
        for row in result['operations']:
            receipt(row)
    elif name == 'exec':
        receipt(result)

    if name in {'write', 'edit'} and not result.get('pending') and not result.get('error'):
        def diff(row, created=False):
            if not isinstance(row, dict) or not isinstance(row.get('diff'), str):
                return
            if created:
                row.pop('diff')
                row['diff_omitted'] = 'created'
            elif len(row['diff']) > 4000:
                end = row['diff'].rfind('\n', 0, 4000)
                row['diff'] = row['diff'][:end if end > 0 else 4000]
                row['diff_truncated'] = True
        diff(result, arguments.get('expected_sha256') == 'new')
        # Checkpoint/restore results may report a file count, not batch rows.
        # Keep that receipt intact instead of failing after the mutation finished.
        if isinstance(result.get('files'), list):
            for row in result['files']:
                diff(row)
    if name in {'workspace', 'project_query'} and arguments.get('operation') == 'tree':
        if arguments.get('options', {}).get('format') == 'compact' and isinstance(result.get('entries'), list):
            base = arguments.get('path', arguments.get('options', {}).get('path', '.')) or '.'
            base = posixpath.normpath(base)
            prefix = '' if base == '.' else base.rstrip('/') + '/'
            result['base'] = base
            result['entries'] = _tree_lines(result['entries'], prefix, indent=view >= 3)
            result['entries_format'] = ('indent_name_slash_or_tab_size' if view >= 3
                                        else 'json_path_slash_or_tab_size')
        if (arguments.get('offset', arguments.get('options', {}).get('offset', 0))
                and (view < 3 or not result.get('scan_error'))):
            result.pop('exclusions', None)
    if (view >= 3 and name in {'workspace', 'project_query'} and arguments.get('operation') == 'search'
            and arguments.get('options', {}).get('format') != 'structured'
            and isinstance(result.get('matches'), list) and not result.get('error')):
        _compact_search(result)
    if output_mode == 'normalized':
        for row in output_records(result):
            original = row['output']
            plain = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\\\))', '', original)
            plain = '\n'.join(part.split('\r')[-1] for part in plain.replace('\r\n', '\n').split('\n'))
            lines = plain.splitlines(keepends=True)
            collapsed, count, previous = [], 0, None
            for line in lines + [None]:
                if line == previous and line is not None:
                    count += 1
                    continue
                if previous is not None:
                    collapsed.append(previous)
                    if count > 1:
                        collapsed.append(f'[… repeated {count} times]\n')
                previous, count = line, 1
            row['output'] = ''.join(collapsed)
            if row['output'] != original:
                row['output_normalized'] = True
                row['output_original_chars'] = len(original)
                row['output_recovery'] = 'Query the same operation_id using connection URL ?output=raw&verbosity=full; do not rerun the command.'
    if name in {'workspace', 'project_query'} and arguments.get('operation') == 'open':
        if result.get('context_detail', 'minimal') == 'minimal':
            result['help'] = 'project_query(help, tool, action)'
            result.pop('context_scope', None)
            context = result.get('context')
            if isinstance(context, dict):
                remaining = context.pop('remaining_documents', None)
                if isinstance(remaining, list):
                    context['remaining_documents_count'] = len(remaining)
                    context['remaining_documents_truncated'] = len(remaining) >= 64
                    context['documents_next'] = 'project_query(tree, path=docs)'
                for key in ('scope', 'trust'):
                    context.pop(key, None)
        result['mcp_view_version'] = 2
    return result


def output_records(value):
    """Known receipt output slots only; never traverse caller documents."""
    if not isinstance(value, dict):
        return []
    rows = [value] if isinstance(value.get('output'), str) and ('operation_id' in value or {'id', 'tool', 'state'} <= value.keys()) else []
    nested = value.get('result')
    if isinstance(nested, dict) and isinstance(nested.get('data'), dict):
        data = nested['data']
        if value.get('tool') in {'exec', 'shell_exec', 'tasks_run', 'ssh_exec', 'vps_exec'} and isinstance(data.get('output'), str):
            rows.append(data)
    for operation in value.get('operations', []):
        rows.extend(output_records(operation))
    return rows


def compact_output_text(value):
    """De-duplicate only explicitly requested human text, never typed results."""
    result = deepcopy(value)
    rows = [result, *result.get('operations', [])] if isinstance(result, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get('result')
        data = nested.get('data') if isinstance(nested, dict) else None
        execution = data if isinstance(data, dict) else row
        if (execution.get('command_ok') is True and execution.get('exit_code') == 0
                and not execution.get('timed_out') and not execution.get('cancelled')
                and not row.get('pending') and row.get('state', 'succeeded') == 'succeeded'
                and (execution.get('failure_context') or {}).get('category') in (None, 'succeeded')):
            for key in ('continuation', 'failure_context', 'log_ref'):
                execution.pop(key, None)
        if isinstance(data, dict) and isinstance(row.get('output'), str) and data.get('output') == row['output']:
            data.pop('output')
            data['output_ref'] = 'output'
    return result
