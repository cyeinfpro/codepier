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


def _execution(value):
    result = {key: deepcopy(item) for key, item in value.items() if key in _EXECUTION}
    result['project_root'] = '.'
    shell = value.get('shell', {})
    result['shell'] = {key: deepcopy(item) for key, item in shell.items() if key in _SHELL}
    command = shell.get('command')
    if isinstance(command, list) and command and isinstance(command[0], str):
        result['shell']['executable'] = re.split(r'[\\/]', command[0])[-1]
    result['shell']['filesystem_scope'] = 'Execution account permissions; not restricted to the project directory.'
    if isinstance(value.get('tools'), dict):
        result['tools'] = {name: bool(path) for name, path in value['tools'].items()}
    result['note'] = ('Commands run on the workspace execution node with its account permissions. '
        'This is not an OS sandbox. File tools enforce their own project path rules. '
        'No automatic command backup, rollback or interactive input.')
    return result


def _skill_summary(value):
    # skill_id + resource_path are the real read API; an absolute installation
    # path is not a project file path and must not be suggested to read/edit.
    result = {key: deepcopy(item) for key, item in value.items()
              if key not in {'path', 'skill_dir', 'source_root', 'local_path'}}
    result['path'] = 'SKILL.md'
    result['resource_path'] = 'SKILL.md'
    return result


def _view(value, *, explicit_skill=False):
    if isinstance(value, list):
        return [_view(item, explicit_skill=explicit_skill) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get('shell'), dict) and ('platform' in value or 'execution_policy' in value):
        value = _execution(value)
    if 'skill_id' in value and 'source' in value and not explicit_skill:
        value = _skill_summary(value)
    if 'project_root' in value and not explicit_skill:
        value['project_root'] = '.'
    if 'root' in value and ('alias' in value or 'project' in value or 'project_id' in value):
        if value['root'] is not None:
            value['root'] = '.'
    if 'device_name' in value:
        value['device_name'] = node_label(value['device_id']) if value.get('device_id') else 'execution-node'
    if 'workspace_id' in value and 'base_commit' in value:
        if 'path' in value:
            value['path'] = '.'
        value.pop('identity', None)
    if 'exit_code' in value and 'output' in value:
        # These repeat the runner's host paths/launcher argv, not its outcome.
        for key in ('cwd', 'shell', 'command'):
            value.pop(key, None)
    if 'runtime' in value and 'source_verified' in value and isinstance(value['runtime'], dict):
        value['runtime'].pop('pid', None)
    if 'id' in value and 'tool' in value and 'state' in value:
        # Command/resource summaries are redundant host metadata. Original
        # arguments remain in the panel audit. Keep selection bindings used by
        # the MCP App to reject receipts from another project/worktree.
        summary = value.get('args_summary')
        if isinstance(summary, dict):
            value['args_summary'] = {key: item for key, item in summary.items()
                if key in {'project', 'workspace_id', 'task', 'target', 'timeout_seconds'}}
    if value.get('tool') == 'skills_read':
        explicit_skill = True
    devices = value.get('devices')
    if isinstance(devices, list):
        names = {}
        for device in devices:
            if not isinstance(device, dict) or 'id' not in device:
                continue
            label = node_label(device['id'])
            if isinstance(device.get('name'), str) and device['name']:
                names[device['name']] = label
            device['name'] = label
            for key in ('platform', 'hostname', 'python', 'roots'):
                device.pop(key, None)
        if names and isinstance(value.get('warnings'), list):
            pattern = re.compile('|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
            value['warnings'] = [pattern.sub(lambda match: names[match[0]], item)
                                 if isinstance(item, str) else item for item in value['warnings']]
    if isinstance(value.get('sources'), list) and 'skills' in value and not explicit_skill:
        value['sources'] = [{key: item for key, item in source.items() if key != 'path'}
                            if isinstance(source, dict) else source for source in value['sources']]
    if 'tasks' in value and isinstance(value['tasks'], list):
        for task in value['tasks']:
            if isinstance(task, dict) and 'name' in task:
                for key in ('command', 'environment_keys'):
                    task.pop(key, None)
    result = {}
    for key, item in value.items():
        if key in _OPAQUE:
            result[key] = item
        elif key in {'error', 'errors', 'warnings', 'denial', 'transport_error', 'scan_error'}:
            result[key] = error_view(item)
        else:
            result[key] = _view(item, explicit_skill=explicit_skill)
    return result


def present(name, arguments, value):
    # Account identity is intentionally returned by the profile tool. VPS
    # connection identities likewise remain usable; only nested projects change.
    if name in {'get_profile', 'get_access_context'}:
        return deepcopy(value)
    explicit_skill = name == 'skills_read' or name in {'workspace', 'project_query'} and arguments.get('operation') == 'skill'
    return _view(deepcopy(value), explicit_skill=explicit_skill)


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
