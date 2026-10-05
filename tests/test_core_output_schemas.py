"""Output contracts cover public data, durable receipts and native media."""
from copy import deepcopy
import json
import time

import pytest
from jsonschema import Draft202012Validator, ValidationError

from shared.contracts import OUTPUT_SCHEMAS, tool_definitions
from shared.core_contracts import CORE_TOOLS
from hub.core_tools import help_result, result as core_result
from tests.test_core_tools import agent, runtime, file_call
from tests.fake_computer_provider import png
from hub.vps import VPSInput

OP = 'a' * 32
SHA = 'b' * 64
ERROR = {'error': {'code': 'SHA_CONFLICT', 'message': 'Changed', 'retryable': False}}
PENDING = {'operation_id': OP, 'pending': True, 'state': 'queued', 'next': 'process',
           'retry_after_seconds': 2, 'deadline': None,
           'next_call': {'name': 'process', 'arguments': {'operation': 'wait', 'operation_ids': [OP]}}}
SAMPLES = {
    'workspace': {'projects': [{'id': 'project', 'alias': 'Fixture', 'online': True}]},
    'read': {'operation_id': OP, 'path': 'file.txt', 'sha256': SHA, 'bytes': 5,
             'content': 'hello', 'offset': 1, 'end_line': 1, 'total_lines': 1,
             'truncated': False, 'next_offset': None},
    'write': {'operation_id': OP, 'path': 'file.txt', 'sha256': SHA, 'backup_id': None},
    'edit': {'operation_id': OP, 'success': True, 'outcome': 'preview', 'atomic': False,
             'files': [{'path': 'file.txt', 'sha256': SHA, 'diff': '+hello'}]},
    'exec': {'operation_id': OP, 'exit_code': 0, 'output': 'done', 'command_ok': True},
    'process': {'operations': [{'operation_id': OP, 'state': 'succeeded', 'pending': False,
                'result': {'ok': True, 'data': {'exit_code': 0, 'output': 'done'}},
                'error': None, 'output_seq': 2}], 'media_truncated': False},
    'vps': {'vps': [{'id': OP, 'name': 'Test', 'host': 'example.invalid', 'port': 22,
            'username': 'root', 'target': 'vps:' + OP, 'enabled': True, 'projects': []}],
            'total': 1, 'next_offset': None},
    'browser': {'operation_id': OP, 'lease_id': OP, 'expires_at': 1.0, 'opened': True,
                'focus_changed': False, 'next': {'tool': 'browser', 'arguments': {
                    'operation': 'snapshot', 'project': 'Fixture', 'lease_id': OP}}},
    'computer': {'operation_id': OP, 'enabled': False, 'provider': {},
                 'capabilities_verified': False, 'session_active': False},
}


def validate(name, value):
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(value)


@pytest.mark.parametrize('name', sorted(CORE_TOOLS))
def test_core_results_are_typed_and_allow_diagnostic_extensions(name):
    schema = OUTPUT_SCHEMAS[name]
    Draft202012Validator.check_schema(schema)
    assert schema['type'] == 'object' and schema['properties']
    assert schema['additionalProperties'] is True
    validate(name, SAMPLES[name])
    validate(name, {**SAMPLES[name], 'future_diagnostic': {'detail': 'allowed'}})
    validate(name, ERROR)
    for invalid in ({}, {'surprise': 'not a result'}, {'error': {'code': 'MISSING_MESSAGE'}},
                    {'error': {'code': 42, 'message': 'wrong type'}}):
        with pytest.raises(ValidationError):
            validate(name, invalid)


@pytest.mark.parametrize('name', sorted(CORE_TOOLS - {'vps'}))
@pytest.mark.parametrize('state', ['queued', 'running', 'reconnecting', 'cancelling', 'unknown'])
def test_remote_and_local_facades_preserve_pending_receipts(name, state):
    validate(name, {**PENDING, 'state': state})
    with pytest.raises(ValidationError):
        validate(name, {**PENDING, 'operation_id': 123})


@pytest.mark.parametrize('name', sorted(CORE_TOOLS - {'vps'}))
def test_terminal_receipts_without_materialized_result_are_preserved(name):
    validate(name, {**PENDING, 'pending': False, 'state': 'interrupted',
                    'next': None, 'next_call': None, 'retry_after_seconds': None})
    with pytest.raises(ValidationError):
        validate(name, {'operation_id': OP, 'pending': False, 'state': 'succeeded'})


@pytest.mark.parametrize('name,field,value', [
    ('workspace', 'projects', 'not a list'),
    ('read', 'content', 5), ('read', 'bytes', '5'),
    ('write', 'sha256', 5), ('edit', 'success', 'true'),
    ('exec', 'exit_code', '0'), ('process', 'operations', {}),
    ('vps', 'total', '1'), ('browser', 'lease_id', 5),
    ('computer', 'enabled', 'false'),
])
def test_wrong_primary_field_types_are_rejected(name, field, value):
    with pytest.raises(ValidationError):
        validate(name, {**SAMPLES[name], field: value})


@pytest.mark.parametrize('name,mutate', [
    ('process', lambda value: value['operations'][0].update(output_seq='two')),
    ('process', lambda value: value['operations'][0]['result'].update(ok='true')),
    ('vps', lambda value: value['vps'][0].update(port='22')),
    ('edit', lambda value: value['files'][0].update(path=42)),
    ('workspace', lambda value: value['projects'][0].update(online='yes')),
])
def test_nested_public_records_are_typed(name, mutate):
    value = deepcopy(SAMPLES[name])
    mutate(value)
    with pytest.raises(ValidationError):
        validate(name, value)


def test_help_index_and_detail_match_actual_facade():
    validate('workspace', help_result())
    validate('workspace', help_result('read', 'lsp'))
    validate('workspace', help_result('process', 'wait'))


def test_actual_core_file_results_and_expired_images(agent):
    result = file_call(agent, 'write', path='file.txt', content='hello',
        expected_sha256='new', idempotency_key='typed-output-write')
    validate('write', {'operation_id': OP, **result})
    read = file_call(agent, 'read', path='file.txt')
    validate('read', {'operation_id': OP, **read})
    edit = file_call(agent, 'edit', path='file.txt', expected_sha256=read['sha256'],
        edits=[{'old_text': 'hello', 'new_text': 'goodbye'}],
        idempotency_key='typed-output-edit')
    validate('edit', {'operation_id': OP, **edit})
    instance, project, root = agent
    (root / 'image.png').write_bytes(png())
    image = {'operation_id': OP, **file_call(agent, 'read', path='image.png')}
    rendered = core_result('read', {}, image)
    validate('read', rendered['structuredContent'])
    assert rendered['content'][1]['type'] == 'image'
    expired = core_result('read', {}, {**image, 'computer_expires_at': time.time() - 1})
    validate('read', expired['structuredContent'])
    assert expired['structuredContent']['media_expired'] is True


def test_process_batch_keeps_individual_errors_list_rows_and_continuations():
    validate('process', {'operations': [
        {'id': OP, 'tool': 'exec', 'state': 'running', 'cancel_requested': 0, 'error': None},
        PENDING,
        {'operation_id': 'missing', **ERROR},
        {'operation_id': OP, 'state': 'cancelling', 'cancel_requested': True, 'next': 'process'},
        {'operation_id': OP, 'current': {}, 'events': []},
    ]})


def test_artifact_timestamp_does_not_inherit_file_import_boolean_type():
    registered = {'operation_id': OP, 'artifact_id': OP, 'sha256': SHA,
                  'bytes': 4, 'created': 1791205037.5, 'expires': 1791208637.5}
    validate('write', registered)
    imported = {'operation_id': OP, 'path': 'file.txt', 'bytes': 4, 'sha256': SHA,
                'created': True, 'overwritten': False, 'extracted': False, 'executed': False}
    validate('write', imported)
    with pytest.raises(ValidationError):
        validate('write', {**registered, 'created': True})
    with pytest.raises(ValidationError):
        validate('write', {**registered, 'created': 'yesterday'})


@pytest.mark.parametrize('error', [None, 'SEARCH_BUDGET', 'PARSER_TIMEOUT'])
def test_search_status_keeps_nullable_code_separate_from_tool_error(error):
    progress = {'operation_id': OP, 'search_id': OP, 'state': 'completed',
                'results': [], 'cursor': 0, 'error': error}
    validate('process', progress)
    validate('process', ERROR)
    with pytest.raises(ValidationError):
        validate('process', {**progress, 'error': False})


def test_browser_snapshot_fresh_and_expired_contracts():
    value = {'operation_id': OP, 'lease_id': OP, 'observation_id': OP, 'expires_at': 2.0,
        'document_id': 'document', 'url': 'https://example.invalid', 'title': 'Example',
        'text': 'hello', 'elements': [{'id': 'select', 'tag': 'select', 'options': [
            {'value': 'one', 'label': 'One', 'selected': True, 'disabled': False}]}]}
    validate('browser', value)
    validate('browser', {'operation_id': OP, 'lease_id': OP, 'observation_id': OP,
        'expires_at': 2.0, 'document_id': 'document', 'media_expired': True})
    value['elements'][0]['id'] = 1
    with pytest.raises(ValidationError):
        validate('browser', value)


def test_advanced_variants_keep_next_state_and_error_shapes():
    validate('workspace', {'workspace_id': OP, 'path': '/fixture', 'base_commit': 'abc',
        'state': 'ready', 'source_dirty': False, 'next': {'tool': 'workspace', 'arguments': {}}})
    validate('process', {'validation_id': OP, 'state': 'failed', 'before': {}, 'after': {},
        'exit_code': None, 'execution_operation_id': OP})
    validate('edit', {'operation_id': OP, 'success': False, 'outcome': 'rolled_back',
        'atomic': False, 'files': [{'path': 'file.txt', 'state': 'original_restored'}],
        **ERROR, 'rollback_errors': [], 'next': 'Inspect files and backups.'})
    with pytest.raises(ValidationError):
        validate('workspace', {'workspace_id': OP, 'path': '/fixture', 'base_commit': 'abc',
            'state': 'queued', 'source_dirty': False, 'next': {}})


@pytest.mark.asyncio
async def test_runtime_local_lists_match_advertised_output(runtime):
    instance, principal = runtime
    validate('workspace', await instance.invoke('workspace', {}, principal))
    validate('process', await instance.invoke('process', {}, principal))
    instance.vps.save(VPSInput(name='Typed', host='example.invalid',
        password='fixture-password', project_ids=['proj']), principal)
    validate('vps', await instance.invoke('vps', {}, principal))


def test_catalog_advertises_identical_schemas_without_legacy_tool_names():
    definitions = {item['name']: item for item in tool_definitions()}
    for name in CORE_TOOLS:
        assert definitions[name]['outputSchema']['properties']
        Draft202012Validator(definitions[name]['outputSchema']).validate(SAMPLES[name])
    assert not {'fs_read', 'browser_open', 'operations_get'} & definitions.keys()


def test_repeated_types_use_local_definitions_without_losing_constraints():
    # A byte budget must never be met by turning typed objects into catch-alls.
    assert any('$defs' in OUTPUT_SCHEMAS[name] for name in CORE_TOOLS)
    def references(value):
        if isinstance(value, dict):
            if '$ref' in value:
                yield value['$ref']
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
    for name in CORE_TOOLS:
        assert all(ref.startswith('#/$defs/') for ref in references(OUTPUT_SCHEMAS[name]))
    invalid = deepcopy(SAMPLES['process'])
    invalid['operations'][0]['result']['ok'] = 'yes'
    with pytest.raises(ValidationError):
        validate('process', invalid)


def test_read_only_queries_advertise_only_their_output_variants():
    validate('project_query', SAMPLES['workspace'])
    validate('task_query', SAMPLES['process'])
    validate('project_query', PENDING)
    validate('task_query', ERROR)
    with pytest.raises(ValidationError):
        validate('project_query', {'workspace_id': OP, 'path': '/fixture', 'base_commit': 'abc',
            'state': 'ready', 'source_dirty': False, 'next': {'tool': 'workspace', 'arguments': {}}})
    with pytest.raises(ValidationError):
        validate('task_query', {'validation_id': OP, 'state': 'failed', 'before': {}, 'after': {},
            'exit_code': None, 'execution_operation_id': OP})
