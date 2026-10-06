"""Typed public results for the nine core facades.

Only schemas live here: no result reshaping, dispatch or authorization. Facades
can forward asynchronous Agent receipts even though their dispatcher is local.
Keep payload variants independent and permit additive diagnostic fields.
"""
from __future__ import annotations

from copy import deepcopy

STR = {'type': 'string'}
INT = {'type': 'integer'}
NUM = {'type': 'number'}
BOOL = {'type': 'boolean'}
NULL = {'type': 'null'}
OBJ = {'type': 'object', 'additionalProperties': True}


def nullable(schema):
    return {'anyOf': [schema, NULL]}


def array(schema):
    return {'type': 'array', 'items': schema}


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': True}


def either(*schemas):
    unique = []
    for schema in schemas:
        # Flatten unions before deduplication so repeated facade fields do not
        # accumulate nested copies of the same types in tools/list.
        choices = schema['anyOf'] if set(schema) == {'anyOf'} else [schema]
        for choice in choices:
            if choice not in unique:
                unique.append(deepcopy(choice))
    return unique[0] if len(unique) == 1 else {'anyOf': unique}


ERROR = obj({'code': STR, 'message': STR, 'operation_id': STR,
             'retryable': BOOL}, ('code', 'message'))
CALL = obj({'name': STR, 'tool': STR, 'arguments': OBJ}, ('arguments',))
CALL['anyOf'] = [{'required': ['name']}, {'required': ['tool']}]
RECEIPT = {
    'operation_id': STR, 'pending': BOOL, 'state': STR,
    'next': either(STR, CALL, NULL), 'next_call': nullable(CALL),
    'retry_after_seconds': nullable(INT), 'deadline': nullable(NUM),
    'elapsed_seconds': NUM,
}
IMAGE = obj({'mimeType': {'enum': ['image/png', 'image/jpeg']},
             'width': INT, 'height': INT, 'bytes': INT, 'sha256': STR},
            ('mimeType', 'width', 'height', 'bytes', 'sha256'))
MEDIA = {'text': STR, 'images': array(IMAGE), 'native_is_error': BOOL,
         'content_truncated': BOOL, 'computer_expires_at': NUM,
         'media_expired': BOOL}
PROJECT = obj({'id': STR, 'alias': STR, 'device_id': STR, 'root': STR,
               'description': STR, 'mode': STR, 'allow_tasks': BOOL,
               'device_name': STR, 'online': BOOL}, ('id', 'alias'))
FILE_DIFF = obj({'path': STR, 'sha256': STR, 'current_sha256': STR,
                 'before_sha256': STR, 'after_sha256': STR,
                 'backup_id': nullable(STR), 'state': STR, 'diff': STR,
                 'diff_truncated': BOOL, 'added_lines': INT,
                 'removed_lines': INT, 'unchanged': BOOL}, ('path',))
RESULT = obj({'ok': BOOL, 'data': OBJ, 'error': ERROR}, ('ok',))
OPERATION = obj({
    **RECEIPT, 'id': STR, 'project_id': nullable(STR), 'device_id': nullable(STR),
    'tool': STR, 'created': NUM, 'updated': NUM, 'accepted_at': nullable(NUM),
    'attempts': INT, 'cancel_requested': either(BOOL, INT),
    'result': nullable(RESULT), 'error': either(STR, ERROR, NULL),
    'output': STR, 'output_seq': INT, 'output_chars': INT,
    'output_truncated': BOOL, 'output_omitted': BOOL, 'output_unchanged': BOOL,
    'result_omitted': BOOL, 'execution': OBJ, 'current': OBJ,
    'events': array(OBJ), 'next_after_event_id': nullable(INT),
})
# List rows, full receipts, cancellation, trace and individual batch failures.
OPERATION['anyOf'] = [
    {'required': ['id', 'tool', 'state']},
    {'required': ['operation_id', 'state', 'pending']},
    {'required': ['operation_id', 'state', 'cancel_requested']},
    {'required': ['operation_id', 'current', 'events']},
    {'required': ['operation_id', 'error'], 'properties': {'error': ERROR}},
]


def union(variants, *, pending=True):
    """Factor shared properties without weakening conflicting variant types."""
    variants = deepcopy(variants)
    variants.append(obj({'error': ERROR}, ('error',)))
    if pending:
        receipt = obj({**RECEIPT, 'pending': {'const': True},
                       'state': {'enum': ['queued', 'running', 'reconnecting',
                                          'cancelling', 'unknown']}},
                      ('operation_id', 'pending', 'state'))
        variants.append(receipt)
        # A terminal journal entry can temporarily have no materialized result.
        # Runtime._pending_receipt still returns this complete receipt; it must
        # remain distinguishable from a successfully materialized payload.
        variants.append(obj({**RECEIPT, 'pending': {'const': False},
            'state': {'enum': ['succeeded', 'failed', 'cancelled',
                               'needs_review', 'interrupted']},
            'next': NULL, 'next_call': NULL},
            ('operation_id', 'pending', 'state', 'next', 'next_call', 'deadline')))
    properties = {}
    for variant in variants:
        for key, value in variant.get('properties', {}).items():
            properties[key] = either(properties[key], value) if key in properties else value
    branches = []
    for variant in variants:
        branch = {key: value for key, value in variant.items()
                  if key not in {'type', 'properties', 'additionalProperties'}}
        # The root contains union types only for fields used differently by
        # different operations. Retain each branch's narrower contract.
        differences = {key: value for key, value in variant.get('properties', {}).items()
                       if value != properties[key]}
        if differences:
            branch['properties'] = differences
        branches.append(branch)
    return {'type': 'object', 'properties': properties, 'anyOf': branches,
            'additionalProperties': True}


def factor(schema):
    """Reuse repeated schema nodes with local references, never loosen types."""
    import json
    from collections import Counter

    counts = Counter()

    def key(node):
        return json.dumps(node, sort_keys=True, separators=(',', ':'))

    def children(node):
        for name in ('properties', 'patternProperties', '$defs'):
            yield from node.get(name, {}).values()
        for name in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(name), dict):
                yield node[name]
        for name in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            yield from node.get(name, [])

    def count(node):
        fingerprint = key(node)
        if len(fingerprint) >= 180:
            counts[fingerprint] += 1
        for child in children(node):
            count(child)

    count(schema)
    definitions, names = {}, {}

    def reduce(node, *, inline=False):
        fingerprint = key(node)
        if not inline and counts[fingerprint] > 1:
            if fingerprint not in names:
                name = 'r' + str(len(names))
                names[fingerprint] = name
                definitions[name] = reduce(node, inline=True)
            return {'$ref': '#/$defs/' + names[fingerprint]}
        result = deepcopy(node)
        for name in ('properties', 'patternProperties', '$defs'):
            if name in node:
                result[name] = {field: reduce(child) for field, child in node[name].items()}
        for name in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(name), dict):
                result[name] = reduce(node[name])
        for name in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if name in node:
                result[name] = [reduce(child) for child in node[name]]
        return result

    result = reduce(schema, inline=True)
    if definitions:
        result['$defs'] = definitions
    return result


def build_core_output_schemas(legacy, *, include_queries=False):
    """Called before legacy schemas receive their generic transport wrapper."""
    def copy(name, *, fields=None, required=None):
        schema = deepcopy(legacy[name])
        schema['properties'].update(fields or {})
        if required is not None:
            schema['required'] = list(required)
        return schema

    def variants(*names):
        return [copy(name) for name in names]

    project_list = copy('projects_list', fields={'projects': array(PROJECT)})
    help_index = obj({'tools': {'type': 'object', 'additionalProperties': obj({
        'operations': array(STR), 'help': CALL}, ('operations', 'help'))},
        'note': STR}, ('tools', 'note'))
    help_detail = obj({'tool': STR, 'operation': STR, 'scope': STR,
        'arguments_location': {'enum': ['options', 'top-level']},
        'requires_project': BOOL, 'requires_idempotency_key': BOOL,
        'inputSchema': OBJ, 'description': STR},
        ('tool', 'operation', 'scope', 'arguments_location', 'inputSchema'))
    devices = obj({'devices': array(obj({'id': STR, 'name': STR,
        'online': BOOL, 'platform': STR}, ('id', 'name')))}, ('devices',))
    project_created = obj({**PROJECT['properties'], 'created_by_role': nullable(STR),
        'role_access': array(STR)}, ('id', 'alias', 'root', 'device_id'))
    workspace = [project_list, help_index, help_detail, devices, project_created,
        *variants('projects_resolve', 'open_workspace', 'project_context',
                  'skills_list', 'skills_read', 'tasks_list', 'execution_info',
                  'readiness_get', 'fs_tree', 'workspace_status', 'worktrees_create',
                  'worktrees_list', 'worktrees_remove', 'workflows_create',
                  'workflows_list', 'workflows_get', 'workflows_update',
                  'workflows_handoff', 'lsp_status')]

    # Core text reads use offset/next_offset, unlike the private fs_read tool.
    file_meta = {'operation_id': STR, 'path': STR, 'sha256': STR, 'bytes': INT,
                 'truncated': BOOL, 'next_offset': nullable(INT)}
    text_file = obj({**file_meta, 'content': STR, 'offset': INT,
                     'end_line': INT, 'total_lines': INT},
                    ('operation_id', 'path', 'sha256', 'bytes', 'content',
                     'offset', 'end_line', 'total_lines', 'truncated', 'next_offset'))
    image_file = obj({**file_meta, **MEDIA},
                     ('operation_id', 'path', 'sha256', 'bytes', 'images',
                      'native_is_error', 'truncated', 'next_offset'))
    read = [text_file, image_file, *variants('show_changes', 'artifacts_get',
            'artifacts_list', 'history_list', 'code_symbols', 'lsp_query')]
    # Import.created is a boolean; registered artifact.created is a timestamp.
    # Both must be declared before factoring shared fields across the facade.
    write = [*variants('fs_write', 'download_artifact'),
             copy('artifacts_register', fields={'created': NUM, 'expires': NUM})]
    patch = copy('apply_patch', fields={
        'files': array(FILE_DIFF), 'patch_id': STR, 'added_lines': INT,
        'removed_lines': INT, 'attempted': array(STR), 'restored': array(STR),
        'rollback_errors': array(obj({'path': STR, 'code': STR, 'message': STR,
                                     'backup_id': STR}, ('path', 'code', 'message'))),
        'error': ERROR, 'next': STR,
    })
    edit = [copy('fs_edit'), patch, *variants('history_restore', 'project_checkpoint')]

    # A saved task, local shell and saved VPS command all expose real exit/output
    # fields. cwd/host/command are transport-specific optional metadata.
    execution = obj({**RECEIPT, 'exit_code': INT, 'output': STR,
        'output_truncated': BOOL, 'timed_out': BOOL, 'cancelled': BOOL,
        'duration_ms': INT, 'command_ok': BOOL, 'cwd': STR, 'shell': array(STR),
        'command': array(STR), 'host': STR, 'port': INT, 'username': STR,
        'ssh_error': nullable(STR)}, ('operation_id', 'exit_code', 'output'))

    process = [obj({'operations': array(OPERATION),
        'next_before_created': nullable(NUM), 'media_truncated': BOOL}, ('operations',)),
        *variants('diagnostics_get', 'agent_diagnostics', 'activity_list',
                  'searches_start', 'searches_cancel',
                  'validation_run', 'validations_get', 'validations_list'),
        copy('searches_get', fields={'error': nullable(STR)})]

    option = obj({'label': STR, 'value': STR, 'disabled': BOOL, 'selected': BOOL},
                 ('label', 'value', 'disabled', 'selected'))
    element = obj({'id': STR, 'tag': STR, 'role': STR, 'label': STR, 'type': STR,
        'value': STR, 'disabled': BOOL, 'options': array(option),
        'options_truncated': BOOL}, ('id',))
    browser_status = copy('browser_status', fields={
        'pool': obj({'pool_size': nullable(INT), 'available': nullable(INT),
                     'leased': nullable(INT)}),
        'active_leases': array(obj({'lease_id': STR, 'state': STR,
            'expires_at': NUM}, ('lease_id', 'state', 'expires_at')))})
    browser_snapshot = copy('browser_snapshot', fields={
        'elements': array(element), 'media_expired': BOOL})
    expired_snapshot = copy('browser_snapshot', fields={'media_expired': {'const': True}},
        required=('lease_id', 'observation_id', 'expires_at', 'document_id', 'media_expired'))
    browser = [browser_status, copy('browser_open', fields={'next': CALL}),
        browser_snapshot, expired_snapshot,
        copy('browser_action', fields={'next': CALL, 'input_dispatched': BOOL,
             'trusted_os_input': BOOL, 'business_outcome_verified': BOOL,
             'expires_at': NUM}),
        copy('browser_close')]

    computer = []
    for name in ('computer_status', 'computer_apps', 'computer_session_open',
                 'computer_observe', 'computer_action', 'computer_session_close'):
        fields = {**MEDIA, 'session_expires_at': NUM, 'observation_error': STR,
            'session_closed': BOOL, 'next': STR, 'native_tools': array(STR),
            'missing_actions': array(STR), 'action_type': STR,
            'active_session': nullable(obj({'session_id': STR, 'app': STR,
                'expires_at': NUM, 'remaining_seconds': INT},
                ('session_id', 'app', 'expires_at', 'remaining_seconds')))}
        computer.append(copy(name, fields=fields))

    vps = obj({
        **{key: STR for key in ('id', 'name', 'host', 'username', 'host_key_policy',
                               'provider', 'region', 'system', 'notes', 'target')},
        'port': INT, 'enabled': BOOL, 'version': INT, 'connection_revision': INT,
        'created': NUM, 'updated': NUM, 'has_password': BOOL,
        'projects': array(PROJECT), 'project_ids': array(STR),
    }, ('id', 'name', 'host', 'port', 'username', 'target', 'enabled', 'projects'))
    result = {
        'workspace': union(workspace),
        'read': union(read), 'write': union(write), 'edit': union(edit),
        'exec': union([execution]), 'process': union(process),
        'browser': union(browser), 'computer': union(computer),
        'vps': union([obj({'vps': array(vps), 'total': INT,
                          'next_offset': nullable(INT)}, ('vps', 'total', 'next_offset'))],
                     pending=False),
    }
    if include_queries:
        result['project_query'] = union([project_list, help_index, help_detail, *variants(
            'fs_tree', 'skills_list', 'skills_read',
            'open_workspace', 'tasks_list', 'execution_info', 'readiness_get',
            'workspace_status', 'workflows_list', 'workflows_get')])
        result['task_query'] = union([process[0], *variants(
            'diagnostics_get', 'activity_list')])
    return {name: factor(schema) for name, schema in result.items()}
