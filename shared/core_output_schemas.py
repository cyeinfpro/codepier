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
PROJECT = obj({'id': STR, 'alias': STR, 'root': STR,
               'description': STR, 'mode': STR, 'allow_tasks': BOOL, 'online': BOOL,
               'connection': obj({'state': {'enum': ['online', 'offline', 'unknown']},
                   'source': nullable(STR), 'observed_at': nullable(NUM)},
                   ('state', 'source', 'observed_at'))}, ('id', 'alias'))
FILE_DIFF = obj({'path': STR, 'sha256': STR, 'current_sha256': STR,
                 'before_sha256': STR, 'after_sha256': STR,
                 'backup_id': nullable(STR), 'state': STR, 'diff': STR,
                 'diff_truncated': BOOL, 'added_lines': INT,
                 'removed_lines': INT, 'unchanged': BOOL}, ('path',))
RESULT = obj({'ok': BOOL, 'data': OBJ, 'error': ERROR}, ('ok',))
OPERATION = obj({
    **RECEIPT, 'id': STR, 'project_id': nullable(STR),
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
    if 'error' in properties:
        properties['error'] = either(properties['error'], NULL)
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

    # Moving a scoped/anchored or recursive resource can change its meaning.
    # This catalog optimizer deliberately leaves such schemas untouched. It
    # never resolves external references or fetches another schema.
    if not isinstance(schema, dict):
        return deepcopy(schema)
    original_defs = schema.get('$defs', {})
    local_targets = {'#/$defs/' + name.replace('~', '~0').replace('/', '~1'): name
                     for name in original_defs}

    def reference_nodes(node):
        if isinstance(node, dict):
            yield node
            for child in node.values():
                yield from reference_nodes(child)
        elif isinstance(node, list):
            for child in node:
                yield from reference_nodes(child)

    nodes = list(reference_nodes(schema))

    def known_nodes(node):
        if not isinstance(node, dict):
            return
        yield node
        for field in ('properties', 'patternProperties', '$defs'):
            for child in node.get(field, {}).values():
                yield from known_nodes(child)
        for field in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(field), dict):
                yield from known_nodes(node[field])
        for field in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            for child in node.get(field, []):
                yield from known_nodes(child)

    visited_nodes = {id(node) for node in known_nodes(schema)}
    if any('$ref' in node and id(node) not in visited_nodes for node in nodes):
        return deepcopy(schema)
    if any(set(node) & {'$id', '$anchor', '$dynamicAnchor', '$dynamicRef', '$recursiveRef',
                       '$recursiveAnchor', 'if', 'then', 'else', 'dependentSchemas', 'contains',
                       'propertyNames', 'unevaluatedProperties', 'unevaluatedItems',
                       'definitions', 'dependencies', 'additionalItems'}
           or ('$ref' in node and node['$ref'] not in local_targets) for node in nodes):
        return deepcopy(schema)
    dependencies = {name: {local_targets[node['$ref']]
                          for node in reference_nodes(value) if '$ref' in node}
                    for name, value in original_defs.items()}
    visited, visiting = set(), set()

    def acyclic(name):
        if name in visiting:
            return False
        if name in visited:
            return True
        visiting.add(name)
        if not all(acyclic(other) for other in dependencies[name]):
            return False
        visiting.remove(name)
        visited.add(name)
        return True

    if not all(acyclic(name) for name in original_defs):
        return deepcopy(schema)

    def group_required(branches):
        # (C and A) or (C and B) == C and (A or B), on the same instance.
        # Only explicit required clauses in anyOf participate. References,
        # conditionals and oneOf multiplicity are never moved or regrouped.
        def size(value):
            return len(json.dumps(value, separators=(',', ':')))
        while True:
            fields = {field for branch in branches if isinstance(branch, dict)
                      and not set(branch) & {'$ref', 'if', 'then', 'else', 'anyOf', 'oneOf'}
                      for field in branch.get('required', [])}
            best = None
            for field in sorted(fields):
                selected = [index for index, branch in enumerate(branches)
                            if isinstance(branch, dict) and field in branch.get('required', [])
                            and not set(branch) & {'$ref', 'if', 'then', 'else', 'anyOf', 'oneOf'}]
                if len(selected) < 2:
                    continue
                common = set.intersection(*(set(branches[index]['required']) for index in selected))
                grouped = []
                for index in selected:
                    branch = deepcopy(branches[index])
                    branch['required'] = [name for name in branch['required'] if name not in common]
                    if not branch['required']:
                        branch.pop('required')
                    grouped.append(branch)
                combined = {'required': [name for name in branches[selected[0]]['required'] if name in common],
                            'anyOf': grouped}
                candidate = [combined if index == selected[0] else branch
                             for index, branch in enumerate(branches)
                             if index == selected[0] or index not in selected]
                saving = size(branches) - size(candidate)
                if saving > 0 and (best is None or saving > best[0]):
                    best = saving, candidate
            if best is None:
                return branches
            branches = best[1]

    def compact_types(node):
        if isinstance(node, list):
            return [compact_types(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = deepcopy(node)
        for field in ('properties', 'patternProperties', '$defs'):
            if field in node:
                result[field] = {key: compact_types(value) for key, value in node[field].items()}
        for field in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(field), dict):
                result[field] = compact_types(node[field])
        for field in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if field in node:
                result[field] = [compact_types(value) for value in node[field]]
        # Every branch is conjoined with its parent. Keep an identical
        # constraint once at the parent, including required/optional fields.
        # Conflicting or narrower branch constraints remain in that branch.
        for field in ('anyOf', 'oneOf', 'allOf'):
            for branch in result.get(field, []):
                if not isinstance(branch, dict):
                    continue
                if result.get('type') == branch.get('type'):
                    branch.pop('type', None)
                if (isinstance(result.get('properties'), dict) and isinstance(branch.get('properties'), dict)
                        and branch.get('additionalProperties', True) is True
                        and 'unevaluatedProperties' not in branch):
                    branch['properties'] = {key: value for key, value in branch['properties'].items()
                                            if json.dumps(value, sort_keys=True) !=
                                            json.dumps(result['properties'].get(key), sort_keys=True)}
                    if not branch['properties']:
                        branch.pop('properties')
        if result.get('required') == []:
            result.pop('required')
        if isinstance(result.get('anyOf'), list):
            result['anyOf'] = group_required(result['anyOf'])
        # JSON Schema defaults additionalProperties to true. Removing only
        # this explicit default is equivalent and keeps additive variants small.
        if result.get('additionalProperties') is True:
            result.pop('additionalProperties')
        choices = result.get('anyOf', [])
        if set(result) == {'anyOf'} and choices and all(
                isinstance(choice, dict) and set(choice) == {'type'}
                and isinstance(choice['type'], str) for choice in choices):
            return {'type': list(dict.fromkeys(choice['type'] for choice in choices))}
        return result

    # Keep the facade's explicit extension contract visible to simple clients.
    root_extensions = schema.get('additionalProperties') is True
    schema = compact_types(schema)
    counts = Counter()

    def key(node):
        return json.dumps(node, sort_keys=True, separators=(',', ':'))

    def children(node):
        if not isinstance(node, dict):
            return
        for name in ('properties', 'patternProperties', '$defs'):
            yield from node.get(name, {}).values()
        for name in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(name), dict):
                yield node[name]
        for name in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            yield from node.get(name, [])

    def count(node):
        fingerprint = key(node)
        if len(fingerprint) >= 30:
            counts[fingerprint] += 1
        for child in children(node):
            count(child)

    count(schema)
    definitions, names = {}, {}
    reserved = set(schema.get('$defs', {}))

    def reduce(node, *, inline=False):
        if not isinstance(node, dict):
            return deepcopy(node)
        fingerprint = key(node)
        if not inline and counts[fingerprint] > 1:
            if fingerprint not in names:
                index = len(names)
                name = chr(97 + index) if index < 26 else 'r' + str(index)
                while name in reserved:
                    index += 1
                    name = chr(97 + index) if index < 26 else 'r' + str(index)
                reserved.add(name)
                names[fingerprint] = name
                definitions[name] = reduce(node, inline=True)
            # Keep lifecycle/access annotations visible to clients that inspect
            # properties without resolving local references.
            annotations = ('deprecated', 'readOnly', 'writeOnly')
            return {'$ref': '#/$defs/' + names[fingerprint],
                    **{name: deepcopy(node[name]) for name in annotations if name in node}}
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
    if root_extensions:
        result['additionalProperties'] = True
    if definitions:
        result['$defs'] = {**result.get('$defs', {}), **definitions}

    # Factoring a parent can leave its children referenced just once. Count the
    # resulting graph, not the original tree, and inline references that cost
    # more than their definition saves. Recount after every replacement: one
    # inlined parent may expose several references to another definition.
    candidates_left = set(result.get('$defs', {}))
    while candidates_left:
        references = Counter()
        protected = set()
        local_refs = {'#/$defs/' + name.replace('~', '~0').replace('/', '~1')
                      for name in candidates_left}

        def count_references(node):
            if not isinstance(node, dict):
                return
            if node.get('$ref') in local_refs:
                references[node['$ref']] += 1
                if set(node) - {'$ref', 'title', 'description', 'deprecated', 'readOnly', 'writeOnly'}:
                    protected.add(node['$ref'])
            for child in children(node):
                count_references(child)

        count_references(result)
        candidates = []
        for name in candidates_left:
            value = result['$defs'][name]
            if not isinstance(value, dict):
                continue  # Preserve boolean definitions and their annotation siblings.
            reference = '#/$defs/' + name.replace('~', '~0').replace('/', '~1')
            if reference in protected:
                continue
            count = references[reference]
            saving = (len(key({'$ref': reference})) - len(key(value))) * count
            saving += len(key({name: value})) - 1
            if saving >= 0:
                candidates.append((saving, name))
        if not candidates:
            break
        _, name = max(candidates)
        value = result['$defs'].pop(name)
        reference = '#/$defs/' + name.replace('~', '~0').replace('/', '~1')
        candidates_left.remove(name)

        def inline(node):
            if not isinstance(node, dict):
                return deepcopy(node)
            if node.get('$ref') == reference:
                # Generated references have only identical lifecycle annotations
                # beside $ref. Preserve those annotations for simple clients.
                return {**deepcopy(value), **{k: v for k, v in node.items() if k != '$ref'}}
            node = dict(node)
            for field in ('properties', 'patternProperties', '$defs'):
                if field in node:
                    node[field] = {k: inline(v) for k, v in node[field].items()}
            for field in ('items', 'additionalProperties', 'not'):
                if isinstance(node.get(field), dict):
                    node[field] = inline(node[field])
            for field in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
                if field in node:
                    node[field] = [inline(v) for v in node[field]]
            return node

        result = inline(result)
    if not result.get('$defs'):
        result.pop('$defs', None)
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

    project_list = copy('projects_list', fields={'projects': array(PROJECT),
        'total': INT, 'offset': INT, 'next_offset': nullable(INT), 'mcp_view_version': {'const': 1}})
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
    project_created = obj({**PROJECT['properties'], 'device_id': STR, 'device_name': STR, 'created_by_role': nullable(STR),
        'role_access': array(STR)}, ('id', 'alias', 'root', 'device_id'))
    workspace = [project_list, help_index, help_detail, devices, project_created,
        *variants('projects_resolve', 'open_workspace', 'project_context',
                  'skills_list', 'skills_read', 'tasks_list', 'execution_info',
                  'readiness_get', 'fs_tree', 'fs_search', 'workspace_status', 'worktrees_create',
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
    batch_file = obj({**text_file['properties'], 'index': INT, 'ok': {'const': True},
                      'budget_limited': BOOL},
                     tuple(key for key in text_file['required'] if key != 'operation_id') + ('index', 'ok'))
    batch_error = obj({'index': INT, 'path': STR, 'ok': {'const': False}, 'error': ERROR},
                      ('index', 'path', 'ok', 'error'))
    batch_read = obj({'operation_id': STR, 'batch': {'const': True},
                      'files': array(either(batch_file, batch_error)), 'truncated': BOOL,
                      'budget_limited': BOOL, 'payload_limit_bytes': INT},
                     ('operation_id', 'batch', 'files', 'truncated'))
    # Metadata-only reads expose a hash, never invented line counts.
    # Batch text results above retain the original required line coordinates.
    line_fields = ['offset', 'end_line', 'total_lines']
    text_file['required'] = [key for key in text_file['required'] if key not in line_fields]
    text_file['anyOf'] = [
        {'required': line_fields},
        {'required': ['content_available', 'content_unavailable_reason'],
         'properties': {'content_available': {'const': False}, 'content': {'const': ''},
                        'content_unavailable_reason': {'enum': ['FILE_TOO_LARGE', 'BINARY_FILE', 'ENCODING']},
                        'truncated': {'const': True}, 'next_offset': NULL}},
    ]
    read = [text_file, image_file, batch_read, *variants('show_changes', 'artifacts_get',
            'artifacts_list', 'history_list', 'code_symbols', 'lsp_query')]
    # Import.created is a boolean; registered artifact.created is a timestamp.
    # Both must be declared before factoring shared fields across the facade.
    write = [*variants('fs_write', 'download_artifact', 'inspect_file_source'),
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
        'ssh_error': nullable(STR), 'ssh_diagnostics': OBJ, 'transport_runtime': OBJ,
        'runtime': OBJ, 'failure_context': OBJ, 'continuation': OBJ,
        'log_ref': OBJ, 'diagnostics': array(OBJ), 'build_storage_hints': array(OBJ), 'execution_isolation': nullable(OBJ),
        'runtime_cleanup': OBJ, 'runtime_outcome': OBJ}, ('operation_id', 'exit_code', 'output'))

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
        # An initial exec wait returns the full durable operation, not only
        # its execution payload or the compact pending receipt. Share the same
        # typed journal contract as task_query (including null/string errors).
        'exec': union([execution, {**OPERATION, 'required': ['id', 'tool', 'state']}]),
        'process': union(process),
        'browser': union(browser), 'computer': union(computer),
        'vps': union([obj({'vps': array(vps), 'total': INT,
                          'next_offset': nullable(INT)}, ('vps', 'total', 'next_offset'))],
                     pending=False),
    }
    if include_queries:
        result['project_query'] = union([project_list, help_index, help_detail, *variants(
            'fs_tree', 'fs_search', 'skills_list', 'skills_read',
            'open_workspace', 'tasks_list', 'execution_info', 'readiness_get',
            'workspace_status', 'workflows_list', 'workflows_get')])
        result['task_query'] = union([process[0], *variants(
            'diagnostics_get', 'activity_list')])
    return {name: factor(schema) for name, schema in result.items()}
