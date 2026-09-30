"""Translate retained domain-level IAM scenarios to the new public MCP API.

This is a client migration helper, not server compatibility. Direct HTTP tests
separately verify that old tool names are rejected.
"""
def public_call(name, arguments):
    args = dict(arguments)
    simple = {'shell_exec': 'exec', 'fs_read': 'read', 'fs_write': 'write', 'fs_edit': 'edit'}
    if name in simple:
        return simple[name], args
    workspace = {'projects_list': 'list', 'projects_resolve': 'resolve', 'open_workspace': 'open',
                 'devices_list': 'devices', 'projects_create': 'project_create'}
    if name in workspace:
        operation = workspace[name]
        if operation in {'project_create', 'resolve'}:
            top = {k: args.pop(k) for k in ('project', 'idempotency_key') if k in args}
            return 'workspace', {'operation': operation, **top, 'options': args}
        return 'workspace', {'operation': operation, **args}
    process = {'operations_get': 'get', 'operations_wait': 'wait', 'operations_cancel': 'cancel'}
    if name in process:
        if 'operation_id' in args:
            args['operation_ids'] = [args.pop('operation_id')]
        return 'process', {'operation': process[name], **args}
    # Specialist operations retain domain schemas in the documented options bag.
    from shared.core_contracts import CORE_ACTIONS
    for public, operations in CORE_ACTIONS.items():
        for operation, internal in operations.items():
            if internal == name:
                top = {k: args.pop(k) for k in ('project', 'workspace_id', 'idempotency_key') if k in args}
                return public, {'operation': operation, **top, 'options': args}
    return name, args
