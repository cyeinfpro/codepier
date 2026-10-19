"""Request-scoped discovery over registered contracts, never an authority grant.

Project opt-ins and operation ownership remain execution checks. This module has
no connection-global cache or "last opened project" state; all callers supply the
current effective scopes. It projects copies, never cached schemas or receipts.
"""
from __future__ import annotations

import copy

from shared.contracts import TOOLS, required_scope
from shared.core_contracts import CORE_ACTIONS, CORE_TOOLS
from shared.query_contracts import QUERY_TOOLS

WORKSPACE = {'list': 'projects_list', 'open': 'open_workspace', 'skills': 'skills_list',
             'skill': 'skills_read', 'tasks': 'tasks_list', 'status': 'execution_info',
             'readiness': 'readiness_get', 'tree': 'fs_tree'}
COMPUTER = {'status': 'computer_status', 'apps': 'computer_apps', 'open': 'computer_session_open',
            'observe': 'computer_observe', 'action': 'computer_action', 'close': 'computer_session_close'}


def catalogs(scopes=None):
    values = {**CORE_ACTIONS,
        **{name: {'file': name, **CORE_ACTIONS[name]} for name in ('read', 'write', 'edit')},
        'exec': {'run': 'exec'}, 'vps': {'list': 'vps'},
        'workspace': {**CORE_ACTIONS['workspace'], **WORKSPACE},
        'process': {**CORE_ACTIONS['process'], **{operation: 'operations_' + operation
            for operation in ('list', 'get', 'wait', 'cancel', 'trace')},
            'diagnostics': 'diagnostics_get', 'activity': 'activity_list'},
        'browser': {op: 'browser_' + op for op in ('status', 'open', 'snapshot', 'action', 'close')},
        'computer': COMPUTER}
    values['read']['batch'] = 'read'
    return {name: visible for name, actions in values.items()
            if (visible := {op: target for op, target in actions.items()
                            if scope_allows(target, {}, scopes)})}


def scope_allows(target, arguments, scopes):
    if scopes is None:
        return True
    if target not in TOOLS:
        return None  # Unknown hints retain their original recovery information.
    if 'read' not in scopes or required_scope(target, arguments) not in scopes:
        return False
    if target == 'open_workspace' and arguments.get('capture_baseline') and 'write' not in scopes:
        return False
    return True


def call_visible(name, arguments, scopes):
    """Known discovery hints only; malformed/unknown hints are not dispatchable."""
    if scopes is None:
        return True
    if not isinstance(arguments, dict):
        return None
    original = name
    name = {'project_query': 'workspace', 'task_query': 'process'}.get(name, name)
    if name == 'workspace' and arguments.get('operation') == 'help':
        if 'read' not in scopes:
            return False
        tool, action = arguments.get('tool'), arguments.get('action')
        if tool and action and action != '<operation>':
            target = catalogs().get(tool, {}).get(action)
            return scope_allows(target, {}, scopes) if target else None
        return not tool or tool in catalogs(scopes)
    if name in CORE_TOOLS:
        default = {'workspace': 'list', 'process': 'list', 'browser': 'status',
                   'computer': 'status', 'exec': 'run', 'vps': 'list'}.get(name, 'file')
        action = arguments.get('operation', default)
        if original in QUERY_TOOLS:
            allowed = TOOLS[original].model.model_fields['operation'].annotation.__args__
            if action not in allowed:
                return False
        target = catalogs().get(name, {}).get(action)
        if not target:
            return None
        # Mirror the facade's forwarding shape before argument-sensitive checks.
        supplied = arguments
        if action in CORE_ACTIONS.get(name, {}):
            supplied = arguments.get('options', {})
        elif name == 'workspace':
            options = arguments.get('options', {})
            if not isinstance(options, dict):
                return False
            if ({'project', 'workspace_id', 'idempotency_key'} & options.keys()
                    or (arguments.keys() - {'operation', 'options'}) & options.keys()):
                return False
            if original == 'project_query' and ('capture_baseline' in options
                    or arguments.get('capture_baseline', False) is not False):
                return False
            supplied = {**arguments, **options}
        return scope_allows(target, supplied if isinstance(supplied, dict) else {}, scopes)
    return scope_allows(name, arguments, scopes)


def _prune_definitions(schema):
    definitions = schema.get('$defs', {})
    reached = set()
    def visit(value):
        if isinstance(value, dict):
            reference = value.get('$ref', '')
            if isinstance(reference, str) and reference.startswith('#/$defs/'):
                name = reference[len('#/$defs/'):].split('/')[0]
                if name in definitions and name not in reached:
                    reached.add(name)
                    visit(definitions[name])
            for key, child in value.items():
                if key != '$defs':
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    if reached:
        schema['$defs'] = {key: value for key, value in definitions.items() if key in reached}
    else:
        schema.pop('$defs', None)
    return schema


def guarded_schema(tool, schema, scopes=None):
    schema = copy.deepcopy(schema)
    properties = schema.get('properties', {})
    if tool == 'computer' and 'probe' in properties:
        if scopes is not None and 'computer' not in scopes:
            properties['probe'] = {'type': 'boolean', 'const': False, 'default': False,
                'description': 'Static status only. Active probing additionally requires computer scope.'}
        else:
            properties['probe']['description'] = 'false uses read scope; true additionally requires computer scope. Neither performs a desktop action.'
    if tool in {'workspace', 'project_query'} and 'capture_baseline' in properties and scopes is not None and 'write' not in scopes:
        properties['capture_baseline'] = {'type': 'boolean', 'const': False, 'default': False}
    return _prune_definitions(schema)


def _facade_schema(name, schema, scopes, visible):
    tool = {'project_query': 'workspace', 'task_query': 'process'}.get(name, name)
    schema = copy.deepcopy(schema)
    props = schema.get('properties', {})
    if 'operation' in props:
        allowed = set(visible.get(tool, {}))
        if tool == 'workspace' and 'read' in scopes:
            allowed.add('help')
        old = props['operation'].get('enum', [])
        operations = [op for op in old if op in allowed]
        props['operation']['enum'] = operations
        if props['operation'].get('default') not in operations and operations:
            props['operation']['default'] = operations[0]
        # Only prune fields when authorization actually removes operations.
        if operations != old:
            fields = {'operation'}
            for op in operations:
                if op == 'help':
                    fields |= {'tool', 'action'}
                    continue
                target = visible[tool][op]
                if op in CORE_ACTIONS.get(tool, {}) or (tool == 'read' and op == 'batch'):
                    fields |= {'project', 'workspace_id', 'idempotency_key', 'options'}
                    if tool == 'write' and op in {'import', 'source_check'}:
                        fields.add('file')
                elif tool == target:
                    fields |= props.keys()
                else:
                    fields |= TOOLS[target].model.model_fields.keys()
                    if tool in {'workspace', 'process'}:
                        fields.add('options')
                    if tool == 'process' and op in {'get', 'wait', 'cancel', 'trace'}:
                        fields.add('operation_ids')
                        if op in {'get', 'wait'}:
                            fields.add('include_trace')
            schema['properties'] = props = {k: v for k, v in props.items() if k in fields}
            if 'required' in schema:
                schema['required'] = [k for k in schema['required'] if k in fields]
    if tool == 'workspace' and 'tool' in props:
        props['tool']['enum'] = [value for value in props['tool'].get('enum', [])
                                  if not value or value in visible]
    return guarded_schema(tool, schema, scopes)


def visible_definitions(definitions, scopes):
    """Copy only native descriptors; gateway/collaboration retain their policy."""
    visible = catalogs(scopes)
    result = []
    for original in definitions:
        name = original['name']
        tool = {'project_query': 'workspace', 'task_query': 'process'}.get(name, name)
        if name not in CORE_TOOLS | QUERY_TOOLS | {'get_profile', 'get_access_context'}:
            result.append(original)
            continue
        if tool in CORE_TOOLS:
            if tool not in visible:
                continue
        elif scope_allows(name, {}, scopes) is False:
            continue
        item = copy.deepcopy(original)
        if tool in CORE_TOOLS:
            item['inputSchema'] = _facade_schema(name, item['inputSchema'], scopes, visible)
            # The read-only source_check entry shares the write facade. Publish
            # its actual required scope without changing mutation annotations.
            if name == 'write' and 'write' not in scopes:
                for schemes in (item.get('securitySchemes', []),
                                item.get('_meta', {}).get('securitySchemes', [])):
                    for scheme in schemes:
                        if 'write' in scheme.get('scopes', []):
                            scheme['scopes'] = ['read']
                item['description'] = 'Inspect native file-source metadata with source_check (read scope). Discover exact options with project_query.help; this operation does not download or save a file.'
            if tool in {'computer', 'browser'} and 'computer' not in scopes:
                item['description'] = ('Read static project capability status; no app, screen or input action. '
                    'Node readiness and permission are independent; every invocation rechecks current authorization.')
        result.append(item)
    return result


def project_hints(value, scopes):
    """Mutate a presentation copy at known receipt paths, never raw file data."""
    if scopes is None or not isinstance(value, dict):
        return value
    def visible_hint(hint):
        if isinstance(hint, str):
            return call_visible(hint, {}, scopes) is not False
        if not isinstance(hint, dict):
            return True
        name = hint.get('name', hint.get('tool'))
        if not isinstance(name, str):
            return True
        return call_visible(name, hint.get('arguments', {}), scopes) is not False
    for key in ('next', 'next_call'):
        if key in value and not visible_hint(value[key]):
            value[key] = None
    index = value.get('capability_index')
    if isinstance(index, dict):
        value['capability_index'] = {key: hint for key, hint in index.items() if visible_hint(hint)}
    nested = value.get('result')
    if isinstance(nested, dict) and isinstance(nested.get('data'), dict):
        project_hints(nested['data'], scopes)
    for operation in value.get('operations', []) if isinstance(value.get('operations'), list) else []:
        if isinstance(operation, dict):
            project_hints(operation, scopes)
    return value
