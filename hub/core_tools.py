"""Small facades over existing authorized APIs, not a generic tool dispatcher."""
from __future__ import annotations

import asyncio
import copy
import json
from pydantic import ValidationError
from shared.contracts import TOOLS
from shared.core_contracts import CORE_TOOLS, CORE_ACTIONS
from shared.computer_media import mcp_result
from shared.util import DevError

WORKSPACE = {'list': 'projects_list', 'open': 'open_workspace', 'skills': 'skills_list',
             'skill': 'skills_read', 'tasks': 'tasks_list', 'status': 'execution_info', 'readiness': 'readiness_get', 'tree': 'fs_tree'}
COMPUTER = {'status': 'computer_status', 'apps': 'computer_apps', 'open': 'computer_session_open',
            'observe': 'computer_observe', 'action': 'computer_action', 'close': 'computer_session_close'}


def validate(name, raw):
    try:
        return TOOLS[name].model.model_validate(raw)
    except ValidationError as exc:
        issues = [{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()]
        raise DevError('INVALID_ARGUMENTS', json.dumps(issues, ensure_ascii=False)[:1500]) from exc


def help_result(tool='', action=''):
    catalogs = {**CORE_ACTIONS,
        **{name: {'file': name, **CORE_ACTIONS[name]} for name in ('read', 'write', 'edit')},
        'exec': {'run': 'exec'}, 'vps': {'list': 'vps'},
        'workspace': {**CORE_ACTIONS['workspace'], **WORKSPACE},
        'process': {**CORE_ACTIONS['process'], **{operation: 'operations_' + operation
            for operation in ('list', 'get', 'wait', 'cancel', 'trace')},
            'diagnostics': 'diagnostics_get', 'activity': 'activity_list'},
        'browser': {op: 'browser_' + op for op in ('status', 'open', 'snapshot', 'action', 'close')},
        'computer': COMPUTER}
    if not tool or not action:
        return {'tools': {name: {'operations': sorted(actions),
            'help': {'tool': 'workspace', 'arguments': {'operation': 'help', 'tool': name, 'action': '<operation>'}}}
            for name, actions in catalogs.items() if not tool or name == tool},
            'note': 'Specialized operations use options. Common operations use top-level arguments, with additional filters in options as shown by each help schema. All scopes and local opt-ins are checked on every call.'}
    target = catalogs.get(tool, {}).get(action)
    if not target:
        raise DevError('UNKNOWN_OPERATION', '未找到工具操作，请先读取 workspace.help 目录')
    from shared.contracts import _compact_input_schema
    schema = _compact_input_schema(TOOLS[target].model.model_json_schema())
    advanced = action in CORE_ACTIONS.get(tool, {})
    if advanced:
        for field in ('project', 'workspace_id', 'idempotency_key'):
            schema.get('properties', {}).pop(field, None)
            if field in schema.get('required', []): schema['required'].remove(field)
    elif tool != target:
        # Describe the actual public call, including batched operation IDs and
        # optional filters that are intentionally absent from the small catalog.
        backend = schema
        schema = _compact_input_schema(TOOLS[tool].model.model_json_schema())
        properties = schema['properties']
        exposed = (backend.get('properties', {}).keys() & properties.keys()) | {'operation'}
        extra = {k: v for k, v in backend.get('properties', {}).items() if k not in properties}
        if tool == 'process' and action in {'get', 'wait', 'cancel', 'trace'}:
            exposed |= {'operation_ids'}
            extra.pop('operation_id', None)
            if action in {'get', 'wait'}:
                exposed |= {'include_trace'}
        schema['properties'] = {k: v for k, v in properties.items() if k in exposed}
        schema['properties']['operation'] = {'type': 'string', 'const': action}
        required = set(backend.get('required', [])) & exposed
        if 'operation_id' in backend.get('required', []):
            required.add('operation_ids')
        if extra:
            required_extra = sorted(set(backend.get('required', [])) & extra.keys())
            schema['properties']['options'] = {'type': 'object', 'properties': extra, 'additionalProperties': False}
            if required_extra:
                schema['properties']['options']['required'] = required_extra
                required.add('options')
        schema['required'] = sorted(required | {'operation'})
    return {'tool': tool, 'operation': action, 'scope': TOOLS[target].scope,
            'arguments_location': 'options' if advanced else 'top-level',
            'requires_project': any('project' in TOOLS[n].model.model_fields and TOOLS[n].model.model_fields['project'].is_required() for n in (target, tool)),
            'requires_idempotency_key': 'idempotency_key' in TOOLS[target].model.model_fields and TOOLS[target].model.model_fields['idempotency_key'].is_required(),
            'inputSchema': schema, 'description': public_description(TOOLS[target].description)}


def public_call(target, arguments):
    for name, actions in CORE_ACTIONS.items():
        for operation, backend in actions.items():
            if target == backend:
                args = dict(arguments)
                outer = {key: args.pop(key) for key in ('project', 'workspace_id', 'idempotency_key') if key in args}
                return name, {**outer, 'operation': operation, 'options': args}
    for operation, backend in WORKSPACE.items():
        if target == backend:
            outer = {k: v for k, v in arguments.items() if k in TOOLS['workspace'].model.model_fields}
            options = {k: v for k, v in arguments.items() if k not in outer}
            return 'workspace', {**outer, 'operation': operation, **({'options': options} if options else {})}
    for operation, backend in COMPUTER.items():
        if target == backend:
            return 'computer', {**arguments, 'operation': operation}
    if target.startswith('browser_'):
        return 'browser', {**arguments, 'operation': target.removeprefix('browser_')}
    if target in {'operations_get', 'operations_wait', 'operations_cancel', 'operations_trace'}:
        args = dict(arguments)
        args['operation_ids'] = [args.pop('operation_id')]
        args['operation'] = target.removeprefix('operations_')
        return 'process', args
    if target in {'operations_list', 'diagnostics_get', 'activity_list'}:
        action = {'operations_list': 'list', 'diagnostics_get': 'diagnostics', 'activity_list': 'activity'}[target]
        outer = {k: v for k, v in arguments.items() if k in TOOLS['process'].model.model_fields}
        options = {k: v for k, v in arguments.items() if k not in outer}
        return 'process', {**outer, 'operation': action, **({'options': options} if options else {})}
    if target in {'fs_read', 'fs_write', 'apply_patch', 'shell_exec', 'tasks_run'}:
        name = {'fs_read': 'read', 'fs_write': 'write', 'apply_patch': 'edit', 'shell_exec': 'exec', 'tasks_run': 'exec'}[target]
        return name, {**arguments, **({'yield_seconds': 0} if name == 'exec' else {})}
    return target, arguments


def public_description(description):
    import re
    from shared.core_contracts import REPLACED_MCP_TOOLS
    return re.sub(r'\b[a-z][a-z_]+\b', lambda match: REPLACED_MCP_TOOLS.get(match[0], match[0]), description)


async def invoke(runtime, name, raw, principal):
    model = validate(name, raw)
    args = model.model_dump()
    action = args['operation']
    if name == 'workspace' and action == 'help':
        from hub.principal import refresh_principal
        user = await runtime.store.run(refresh_principal, runtime.store, principal)
        if 'read' not in user.scopes:
            raise DevError('INSUFFICIENT_SCOPE', '缺少 read 权限', 403, required_scope='read')
        return help_result(args['tool'], args['action'])
    target = CORE_ACTIONS.get(name, {}).get(action)
    if target:
        fields = TOOLS[target].model.model_fields
        outer = {'operation', 'options', 'project', 'workspace_id', 'idempotency_key'}
        if name == 'write' and action == 'import':
            outer.add('file')
        if set(model.model_fields_set) - outer:
            raise DevError('INVALID_ARGUMENTS', '专项操作参数必须放在 options 中')
        options = dict(args['options'])
        if name == 'write' and action == 'import' and args.get('file') is not None:
            if 'file' in options:
                raise DevError('INVALID_ARGUMENTS', 'file 不能在顶层和 options 中重复提供')
            options['file'] = args['file']
        if {'project', 'workspace_id', 'idempotency_key'} & options.keys():
            raise DevError('INVALID_ARGUMENTS', 'project、workspace_id、idempotency_key 必须放在顶层')
        for field in ('project', 'workspace_id', 'idempotency_key'):
            if field in fields and args.get(field):
                options[field] = args[field]
        return await runtime.invoke(target, options, principal)
    if name == 'process':
        options = dict(args['options'])
        if {'project', 'workspace_id', 'idempotency_key', 'operation_id', 'operation_ids'} & options.keys():
            raise DevError('INVALID_ARGUMENTS', '标识和授权范围参数必须放在顶层')
        if TOOLS[name].model.model_fields.keys() & options.keys():
            raise DevError('INVALID_ARGUMENTS', 'options 中不能重复顶层参数')
        if action in {'diagnostics', 'activity'}:
            target = 'diagnostics_get' if action == 'diagnostics' else 'activity_list'
            call = {k: v for k, v in model.model_dump(exclude_unset=True).items() if k in TOOLS[target].model.model_fields}
            return await runtime.invoke(target, {**call, **options}, principal)
        if action == 'list':
            return await runtime.invoke('operations_list', {k: args[k] for k in
                ('project', 'state', 'idempotency_key', 'limit', 'before_created')} | options, principal)
        # Authorization is checked by each real operation endpoint. Batch waits
        # share one wall-clock budget; one slow job never serializes the others.
        async def one(identifier):
            call = {**options, 'operation_id': identifier}
            if action not in {'cancel', 'trace'}:
                call.update({k: args[k] for k in ('include_output', 'include_result', 'output_limit', 'after_output_seq')})
            if action == 'wait':
                call['wait_seconds'] = args['wait_seconds']
            if action == 'trace' and 'limit' in model.model_fields_set:
                call['limit'] = args['limit']
            try:
                value = await runtime.invoke('operations_' + action, call, principal)
                if args['include_trace'] and action in {'get', 'wait'}:
                    value['execution'] = await runtime.invoke('operations_trace', {'operation_id': identifier}, principal)
                return value
            except DevError as exc:
                return {'operation_id': identifier, 'error': {'code': exc.code, 'message': exc.message}}
        return {'operations': await asyncio.gather(*(one(identifier) for identifier in args['operation_ids']))}
    target = (WORKSPACE[action] if name == 'workspace' else
              COMPUTER[action] if name == 'computer' else 'browser_' + action)
    fields = TOOLS[target].model.model_fields
    supplied = model.model_dump(exclude_unset=True)
    supplied.pop('operation', None)
    options = supplied.pop('options', {})
    if {'project', 'workspace_id', 'idempotency_key'} & options.keys() or supplied.keys() & options.keys():
        raise DevError('INVALID_ARGUMENTS', 'options 不能覆盖顶层参数或授权范围')
    supplied.update(options)
    # Do not silently ignore an action parameter belonging to another operation.
    unused = set(supplied) - set(fields)
    if unused:
        raise DevError('INVALID_ARGUMENTS', f'{name}.{action} 不接受参数：' + ', '.join(sorted(unused)))
    return await runtime.invoke(target, supplied, principal)


def failed(value):
    if value.get('pending'):
        return False
    nested = value.get('result') or {}
    data = nested.get('data') or value
    return bool(value.get('error') or value.get('state') in {'failed', 'cancelled', 'needs_review', 'interrupted'}
        or nested.get('ok') is False or data.get('command_ok') is False or data.get('success') is False)


def compact_receipt(value):
    for key in ('next', 'next_call'):
        call = value.get(key)
        if isinstance(call, dict) and isinstance(call.get('arguments'), dict):
            field = 'name' if 'name' in call else 'tool'
            if isinstance(call.get(field), str):
                name, args = public_call(call[field], call['arguments'])
                value[key] = {**call, field: name, 'arguments': args}
    if isinstance(value.get('next'), str) and value['next'] in {'operations_get', 'operations_wait'}:
        value['next'] = 'process'
    nested = value.get('result')
    if isinstance(nested, dict) and isinstance(nested.get('data'), dict):
        compact_receipt(nested['data'])
    return value


def result(name, arguments, value):
    if name in CORE_TOOLS:
        value = compact_receipt(copy.deepcopy(value))
    if name in {'browser', 'computer'}:
        action = arguments.get('operation', 'status')
        return mcp_result(COMPUTER[action] if name == 'computer' else 'browser_' + action, value)
    backend = CORE_ACTIONS.get(name, {}).get(arguments.get('operation'))
    if backend:
        rendered = mcp_result(backend, value)
        rendered['isError'] = rendered['isError'] or failed(value)
        return rendered
    if name == 'process' and arguments.get('operation', 'list') in {'get', 'wait', 'cancel', 'trace'}:
        rendered = [mcp_result('operations_get', compact_receipt(item)) for item in value.get('operations', [])]
        public = {'operations': [item['structuredContent'] for item in rendered]}
        # Each operation applies the existing screenshot lifetime/size checks.
        # Limit aggregate media in a batch; omitted images can be fetched singly.
        images, size = [], 0
        for item in rendered:
            for block in item['content'][1:]:
                size += len(block.get('data', ''))
                if size <= 7 * 1024 * 1024:
                    images.append(block)
                else:
                    public['media_truncated'] = True
        return {'content': [{'type': 'text', 'text': json.dumps(public, ensure_ascii=False)}, *images],
                'structuredContent': public,
                'isError': any(item['isError'] or failed(item['structuredContent']) for item in rendered)}
    rendered = mcp_result(name, value)
    if name in CORE_TOOLS:
        rendered['isError'] = rendered['isError'] or failed(value)
    return rendered
