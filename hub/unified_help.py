"""Model-facing help for one visible unified route."""
from __future__ import annotations

from copy import deepcopy

from hub.tool_discovery import catalogs, guarded_schema
from shared import unified_contracts as u
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, _compact_input_schema
from shared.core_contracts import Process, QueueOptions, ReadBatchOptions
from shared.schema_cache import schema_for

_OP_ID = 'a' * 32
_SHA = 'b' * 64
_KEY = 'example-new-operation-key'
_PROJECT = 'ExampleProject'

# Examples use syntactically valid, illustrative values. They never resolve a
# resource, invent an approval, or become an automatic follow-up request.
_EXAMPLES = {
    ('query', 'project', 'open'): {'project': _PROJECT},
    ('query', 'project', 'list'): {'limit': 20},
    ('query', 'file', 'file'): {'project': _PROJECT, 'path': 'README.md', 'limit': 80},
    ('query', 'file', 'batch'): {'project': _PROJECT, 'args': {'items': [{'path': 'README.md'}, {'path': 'pyproject.toml'}]}},
    ('query', 'tree', 'tree'): {'project': _PROJECT, 'path': '.', 'args': {'format': 'compact'}},
    ('query', 'search', 'sync'): {'project': _PROJECT, 'path': 'src', 'limit': 10,
                                 'args': {'query': 'load_config', 'file_glob': 'src/*.py'}},
    ('query', 'search', 'start'): {'project': _PROJECT, 'path': 'src', 'args': {
        'query': 'load_config', 'mode': 'symbols', 'file_glob': 'src/*.py', 'max_results': 100}},
    ('query', 'search', 'get'): {'project': _PROJECT, 'args': {'search_id': _OP_ID, 'cursor': 0}},
    ('query', 'search', 'cancel'): {'project': _PROJECT, 'args': {'search_id': _OP_ID}},
    ('query', 'job', 'get'): {'ids': [_OP_ID], 'args': {'after_output_seq': 1}},
    ('query', 'job', 'wait'): {'ids': [_OP_ID], 'args': {'wait_seconds': 5, 'after_output_seq': 1}},
    ('query', 'job', 'list'): {'project': _PROJECT, 'limit': 10},
    ('query', 'job', 'trace'): {'ids': [_OP_ID]},
    ('query', 'job', 'diagnostics'): {'project': _PROJECT},
    ('query', 'knowledge', 'query'): {'project': _PROJECT, 'args': {'query': 'load_config'}},
    ('act', 'knowledge', 'build'): {'project': _PROJECT, 'idempotency_key': _KEY, 'args': {'auto_refresh': False}},
    ('query', 'changes', 'changes'): {'project': _PROJECT, 'args': {'baseline_ref': _OP_ID}},
    ('query', 'changes', 'reviews'): {'project': _PROJECT},
    ('query', 'skills', 'list'): {'project': _PROJECT},
    ('query', 'tasks', 'list'): {'project': _PROJECT},
    ('query', 'artifacts', 'list'): {'project': _PROJECT},
    ('act', 'baseline', 'open'): {'project': _PROJECT, 'idempotency_key': _KEY,
                                   'args': {'baseline_paths': ['README.md'], 'context_id': _SHA}},
    ('act', 'exec', 'run'): {'project': _PROJECT, 'command': 'git status --short', 'cwd': '.',
                              'idempotency_key': _KEY, 'wait': 15},
    ('act', 'edit', 'edit'): {'project': _PROJECT, 'path': 'README.md', 'idempotency_key': _KEY,
                               'expected_sha256': _SHA, 'edits': [{'old_text': 'Old heading', 'new_text': 'New heading'}]},
    ('act', 'edit', 'write'): {'project': _PROJECT, 'path': 'notes.txt', 'idempotency_key': _KEY,
                                'expected_sha256': 'new', 'content': 'Notes\n'},
}

_GLOBS = ('file_glob matches the whole project-relative path, including for a single file. '
          'path restricts the scan root; a basename pattern like config.py only matches at the project root. '
          'Use *config.py for nested paths or src/*.py for a path prefix.')
_GUIDES = {
    ('project', 'open'): 'Read project rules and status. Omit context_id on first open; reuse the returned ID only when you have its body.',
    ('project', 'list'): 'Discover authorized aliases and connection state. Use next_offset for another page.',
    ('file', 'file'): 'Read one file. offset is one-based; resume at next_offset. Keep the SHA for edits.',
    ('file', 'batch'): 'Read up to 8 items from one project. Follow each file’s next_offset if its content was limited.',
    ('tree', 'tree'): 'Compact entries are the default. Set args.format=structured for objects; retain next_offset for pagination.',
    ('search', 'sync'): 'Bounded literal search in one call. Use limit and offset for the returned page. ' + _GLOBS,
    ('search', 'start'): 'Create a bounded search session for paginated text, symbols or references. Use max_results and max_files for the session. Advance with search view=get and its search_id/cursor. ' + _GLOBS,
    ('search', 'get'): 'Read or advance the existing search session. Pass its cursor, never the operation output sequence.',
    ('search', 'cancel'): 'Stop advancing the named search session.',
    ('job', 'get'): 'Read the original operations. Supply their IDs and the output sequence already seen. For multiple IDs, request them separately when their cursors differ.',
    ('job', 'wait'): 'Wait on the original operations. wait_seconds bounds this call; after_output_seq suppresses already-read output.',
    ('job', 'trace'): 'Inspect the original operation timeline. Use the same IDs.',
    ('gui', 'app_status'): 'Read app status. Active probing requires computer scope.',
    ('changes', 'changes'): 'Supply exactly one of baseline_ref or review_ref. Freeze by baseline_ref, then page using review_ref.',
    ('baseline', 'open'): 'Capture source before edits. Use exact baseline_paths and verify coverage per path. Reuse context_id if you have the prior context.',
    ('exec', 'run'): 'Supply exactly one of command or args.task. wait controls the unified response (default 30s); timeout_seconds limits command execution. Inspect pending or uncertain work by original operation ID.',
    ('edit', 'edit'): 'Supply path, expected_sha256 and unique replacements, or args.changes for a SHA-checked batch.',
    ('edit', 'write'): 'Use the current SHA or new to create a file. content is the complete file body.',
    ('knowledge', 'query'): 'Read the stored map, with query or paths as optional filters. Selected files are SHA-checked when returned.',
    ('knowledge', 'build'): 'Build the map without executing project code. auto_refresh defaults to true and requires current authorization.',
}


def _operation_schema(legacy, operation):
    """Reuse the backend machine contract and adapt only facade parameters."""
    if legacy in u.COLLAB_TOOLS:
        from shared.public_collaboration import action_help
        return action_help(legacy, operation)['inputSchema'], None
    if (legacy, operation) == ('read', 'batch'):
        schema = schema_for(ReadBatchOptions)
        fields = schema['properties']
        for key in ('project', 'workspace_id', 'idempotency_key'):
            fields[key] = schema_for(TOOLS['read'].model)['properties'][key]
        schema['required'].append('project')
        return schema, OUTPUT_SCHEMAS['read']
    target = catalogs().get(legacy, {}).get(operation, legacy)
    schema = schema_for(TOOLS[target].model)
    if legacy == 'process' and operation in {'get', 'wait', 'cancel', 'trace'}:
        properties = schema['properties']
        properties.pop('operation_id', None)
        properties['ids'] = {**u._PROPERTIES['ids'], 'minItems': 1, 'uniqueItems': True}
        schema['required'] = ['ids', *[k for k in schema.get('required', []) if k != 'operation_id']]
        process = schema_for(Process)['properties']
        if operation == 'trace':
            properties['limit'] = process['limit']
        if operation in {'get', 'wait'}:
            for key in ('include_trace', 'output_limit', 'include_output', 'include_result', 'after_output_seq'):
                properties[key] = process[key]
        if operation == 'wait':
            properties['wait_seconds'] = process['wait_seconds']
    output = 'process' if legacy == 'process' and operation in {'get', 'wait', 'list', 'cancel', 'trace'} else target
    return schema, OUTPUT_SCHEMAS.get(output)


def _input_schema(tool, action, view, legacy, schema):
    fields = schema.get('properties', {})
    required = set(schema.get('required', []))
    if legacy in u.COLLAB_TOOLS:
        fields.pop('action', None)
        required.discard('action')
        fields['handle'] = {'type': 'string', 'description': 'Optional handle for supported credential fields.'}
    fields.pop('operation', None)
    required.discard('operation')
    if legacy in {'exec', 'read', 'write', 'edit'} and 'options' in fields:
        fields.pop('options')
        fields.update(schema_for(QueueOptions)['properties'])
    if action == 'project':
        for key in ('capture_baseline', 'baseline_paths'):
            fields.pop(key, None)
    if (action, view) == ('baseline', 'open'):
        fields.pop('capture_baseline')
        fields['idempotency_key'] = u._PROPERTIES['idempotency_key']
        required.add('idempotency_key')
    if action == 'tree':
        fields['format']['default'] = 'compact'
    if (action, view) in {('search', 'sync'), ('project', 'list')}:
        fields['format'] = {'enum': ['compact', 'structured'],
                            'description': 'Unified view=3 presentation. Use structured for object rows.'}
    if action == 'help':
        fields = {key: fields[key] for key in ('project', 'workspace_id')}
        fields.update({key: {'type': 'string'} for key in ('action', 'tool', 'view', 'event')})
        fields['detail'] = {'enum': ['brief', 'schema'], 'default': 'brief'}
        # 'action' and 'view' inside args select the target, not this envelope.
    if action == 'file' and view == 'file':
        required.add('path')
    if (action, view) == ('edit', 'write'):
        fields.pop('file', None)
        required.update(('path', 'expected_sha256', 'content'))
    properties = {'action': {'const': action}, 'view': {'const': view}}
    top = set(u.TOP_LEVEL[tool])
    nested = {key: spec for key, spec in fields.items() if key not in top}
    properties.update({key: spec for key, spec in fields.items() if key in top})
    if action not in u.NO_WAIT_ACTIONS and not action.startswith('collab.'):
        properties['wait'] = {**u._PROPERTIES['wait'], 'default': u.DEFAULT_WAIT_SECONDS}
    outer_required = ['action', *sorted(required & top)]
    if nested:
        properties['args'] = {'type': 'object', 'additionalProperties': False, 'properties': nested}
        if required & nested.keys():
            properties['args']['required'] = sorted(required & nested.keys())
            outer_required.append('args')
    result = {'type': 'object', 'additionalProperties': False, 'properties': properties,
              'required': outer_required}
    if schema.get('$defs'):
        result['$defs'] = schema['$defs']
    if action == 'exec':
        result['oneOf'] = [{'required': ['command']},
                           {'required': ['args'], 'properties': {'args': {'required': ['task']}}}]
    if (action, view) == ('edit', 'edit'):
        result['oneOf'] = [{'required': ['path', 'edits', 'expected_sha256']},
                           {'required': ['args'], 'properties': {'args': {'required': ['changes']}}}]
    return _compact_input_schema(result)


def action_help(address, views, *, project='', workspace_id='', detail='brief', scopes=None):
    tool, action, view, legacy, operation = address
    raw, output = _operation_schema(legacy, operation)
    schema = _input_schema(tool, action, view, legacy, guarded_schema(legacy, raw, scopes))
    props = schema['properties']
    args = props.get('args', {})
    required = [key for key in schema['required'] if key not in {'action', 'args'}]
    required.extend('args.' + key for key in args.get('required', []))
    info = {'tool': tool, 'action': action, 'view': view, 'default_view': next(iter(views)), 'views': list(views),
            'required': required, 'parameters': [k for k in props if k not in {'action', 'view', 'args'}],
            'args': list(args.get('properties', {}))}
    if action in {'search', 'tree', 'job', 'exec'}:
        defaults = {key: spec['default'] for key, spec in (props | args.get('properties', {})).items()
                    if 'default' in spec and spec['default'] not in (None, '', [], {})}
        if defaults:
            info['defaults'] = defaults
    auto_view = {'project': 'With project: open; otherwise list.',
                 'file': 'With args.items: batch; otherwise file.',
                 'job': 'With ids: get; otherwise list.',
                 'edit': 'With content only: write; otherwise edit.'}.get(action)
    if auto_view:
        info['implicit_view'] = auto_view
    if guide := _GUIDES.get((action, view)):
        info['description'] = guide
    if sample := _EXAMPLES.get((tool, action, view)):
        example = {'action': action, 'view': view, **deepcopy(sample)}
        if 'project' in sample and project:
            example['project'] = project
        if 'project' in example and workspace_id and 'workspace_id' in props:
            example['workspace_id'] = workspace_id
        info['example'] = example
    if detail == 'schema':
        info['inputSchema'] = schema
        if output is not None:
            info['outputSchema'] = deepcopy(output)
    return info
