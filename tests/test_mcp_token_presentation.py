"""Host-facing token savings preserve receipts, source bodies and diagnostics."""
from copy import deepcopy
import json
import pytest
from jsonschema import Draft202012Validator
from hub.core_tools import result
from shared.contracts import tool_definitions

OP = 'a' * 32


def checked(name, args, value, **options):
    before = deepcopy(value)
    rendered = result(name, args, value, **options)
    assert before == value
    schema = next(tool['outputSchema'] for tool in tool_definitions() if tool['name'] == name)
    Draft202012Validator(schema).validate(rendered['structuredContent'])
    return rendered['structuredContent']


@pytest.mark.parametrize('exit_code', [0, 1])
@pytest.mark.parametrize('view', [1, 2])
def test_terminal_exec_null_error_conforms(exit_code, view):
    value = {'operation_id': OP, 'error': None, 'exit_code': exit_code,
             'output': 'ok\n', 'command_ok': exit_code == 0,
             'state': 'succeeded' if exit_code == 0 else 'failed', 'pending': False}
    shown = checked('exec', {}, value, view=view)
    assert shown['exit_code'] == exit_code
    assert ('error' in shown) == (view == 1)


def test_typed_output_is_preserved_and_only_compact_text_deduplicates():
    from shared.mcp_presentation import compact_output_text
    raw = {'id': OP, 'tool': 'shell_exec', 'state': 'succeeded', 'output': 'same',
           'result': {'ok': True, 'data': {'operation_id': OP, 'exit_code': 0, 'output': 'same'}}}
    shown = checked('task_query', {'operation': 'get'}, {'operations': [raw]})['operations'][0]
    assert shown['result']['data']['output'] == 'same'
    compact = compact_output_text({'operations': [shown]})['operations'][0]
    assert compact['result']['data']['output_ref'] == 'output'
    assert 'output' not in compact['result']['data']
    assert shown['result']['data']['output'] == 'same'
    raw['output'] = 'tail page'
    assert compact_output_text(raw)['result']['data']['output'] == 'same'
    shown = checked('task_query', {'operation': 'get'}, {'operations': [raw]})['operations'][0]
    assert shown['result']['data']['output'] == 'same'
    shown = checked('task_query', {'operation': 'get'}, {'operations': [raw]}, verbosity='full')['operations'][0]
    assert shown['result']['data']['output'] == 'same'


def test_success_savings_preserve_isolation_evidence_and_failure_details():
    value = {'operation_id': OP, 'exit_code': 0, 'output': '', 'command_ok': True,
        'runtime': {'mode': 'container_v1', 'isolation': 'verified'},
        'execution_isolation': {'state': 'verified', 'driver': 'docker'},
        'runtime_cleanup': {'state': 'failed', 'code': 'CLEANUP_FAILED'},
        'continuation': {'operation_id': OP}, 'log_ref': {'operation_id': OP},
        'failure_context': {'category': 'succeeded'}}
    shown = checked('exec', {}, value)
    assert shown['runtime']['mode'] == 'container_v1'
    assert shown['runtime_cleanup']['state'] == 'failed'
    assert shown['execution_isolation']['state'] == 'verified'
    assert 'log_ref' not in shown
    value.update(exit_code=1, command_ok=False)
    value['failure_context']['category'] = 'command_failed'
    shown = checked('exec', {}, value)
    assert shown['failure_context']['category'] == 'command_failed'
    assert shown['log_ref']['operation_id'] == OP


def test_new_write_does_not_repeat_submitted_source():
    value = {'operation_id': OP, 'path': 'new.py', 'sha256': 'b' * 64,
             'diff': '+synthetic\n' * 1000, 'added_lines': 1000}
    shown = checked('write', {'expected_sha256': 'new'}, value)
    assert 'diff' not in shown and shown['diff_omitted'] == 'created'
    assert len(json.dumps(shown)) < 1000
    assert checked('write', {'expected_sha256': 'new'}, value, view=1)['diff'] == value['diff']


def test_write_existing_diff_bounded_with_full_escape_hatch():
    value = {'operation_id': OP, 'path': 'old.py', 'sha256': 'b' * 64,
             'diff': '-before\n+after\n' * 1000}
    shown = checked('write', {}, value)
    assert len(shown['diff']) <= 4000 and shown['diff_truncated']
    assert checked('write', {}, value, verbosity='full')['diff'] == value['diff']


def test_compact_text_keeps_complete_metadata_and_body():
    value = {'operation_id': OP, 'path': 'a.py', 'sha256': 'b' * 64, 'bytes': 2,
        'content': 'x\n', 'offset': 1, 'end_line': 1, 'total_lines': 1,
        'truncated': False, 'next_offset': None}
    rendered = result('read', {}, value, text_mode='compact')
    header, body = rendered['content'][0]['text'].split('\n', 1)
    assert json.loads(header)['sha256'] == value['sha256']
    assert body == value['content']
    assert rendered['structuredContent'] == value


def test_single_read_budget_is_utf8_safe_and_resumable():
    from shared.core_contracts import READ_BATCH_PAYLOAD_BYTES
    value = {'operation_id': OP, 'path': 'large.txt', 'sha256': 'b' * 64,
        'bytes': 180000, 'content': ('😀\\\\"quoted\n' * 10000),
        'offset': 10, 'end_line': 10009, 'total_lines': 20000,
        'truncated': True, 'next_offset': 10010}
    rendered = result('read', {}, value)
    shown = rendered['structuredContent']
    assert len(json.dumps({'jsonrpc': '2.0', 'id': None, 'result': rendered},
                         ensure_ascii=False, separators=(',', ':')).encode()) <= READ_BATCH_PAYLOAD_BYTES
    assert shown['budget_limited'] and shown['truncated']
    assert shown['next_offset'] == shown['end_line'] + 1
    assert shown['content'] == ''.join(value['content'].splitlines(keepends=True)[:shown['next_offset'] - 10])


def test_minimal_open_is_versioned_and_source_not_pruned():
    raw = {'operation_id': OP, 'context_id': 'b' * 64, 'context_unchanged': False,
        'workspace': {'project': 'Example', 'project_id': 'project', 'root': '.'},
        'capability_index': {'large': 'constant'}, 'context_detail': 'minimal',
        'context': {'project_root': '.', 'documents': [], 'remaining_documents': ['docs/old.md'],
                    'scope': 'boilerplate', 'trust': 'boilerplate'}, 'truncated': False}
    shown = checked('project_query', {'operation': 'open'}, raw)
    assert shown['mcp_view_version'] == 2
    assert 'capability_index' not in shown and 'help' in shown
    assert shown['context']['remaining_documents_count'] == 1
    legacy = checked('project_query', {'operation': 'open'}, raw, view=1)
    assert legacy['mcp_view_version'] == 1 and 'capability_index' in legacy


def test_large_exec_budget_keeps_head_tail_and_recovery_without_false_cursor():
    from shared.core_contracts import READ_BATCH_PAYLOAD_BYTES
    output = 'HEAD\n' + 'synthetic filler\n' * 20000 + 'TAIL\n'
    raw = {'operation_id': OP, 'exit_code': 0, 'output': output, 'command_ok': True}
    shown = result('exec', {}, raw)
    assert len(json.dumps({'jsonrpc': '2.0', 'id': None, 'result': shown},
                         ensure_ascii=False, separators=(',', ':')).encode()) <= READ_BATCH_PAYLOAD_BYTES
    public = shown['structuredContent']
    assert public['output'].startswith('HEAD\n') and public['output'].endswith('TAIL\n')
    assert public['output_budget_limited'] and public['output_truncated']
    assert public['output_original_chars'] == len(output)
    assert 'after_output_seq' not in public and 'do not rerun' in public['output_recovery']
    assert result('exec', {}, raw, verbosity='full')['structuredContent']['output'] == output
    assert raw['output'] == output


def test_normalized_output_is_opt_in_and_original_recoverable():
    output = '\x1b[31mred\x1b[0m\n0%\r50%\r100%\nrepeat\nrepeat\nrepeat\n'
    raw = {'operation_id': OP, 'exit_code': 0, 'output': output}
    assert result('exec', {}, raw)['structuredContent']['output'] == output
    shown = result('exec', {}, raw, output_mode='normalized')['structuredContent']
    assert shown['output'] == 'red\n100%\nrepeat\n[… repeated 3 times]\n'
    assert shown['output_normalized'] and shown['output_original_chars'] == len(output)
    assert '?output=raw&verbosity=full' in shown['output_recovery']
    assert result('exec', {}, raw, output_mode='normalized', verbosity='full')['structuredContent']['output'] == output


def test_budget_never_changes_failure_diagnostics_or_arbitrary_bodies():
    from shared.mcp_presentation import output_records
    assert output_records({'output': 'untrusted body'}) == []
    raw = {'operation_id': OP, 'exit_code': 7, 'output': 'x\n' * 100000,
           'command_ok': False, 'failure_context': {'category': 'command_failed', 'code': 'REAL_FAILURE'}}
    shown = result('exec', {}, raw)['structuredContent']
    assert shown['exit_code'] == 7 and shown['failure_context'] == raw['failure_context']
    assert shown['output_truncated']


def test_polled_write_diff_is_bounded_without_mutating_saved_result():
    raw = {'id': OP, 'tool': 'fs_write', 'state': 'succeeded',
           'result': {'ok': True, 'data': {'operation_id': OP, 'path': 'x', 'sha256': 'b'*64,
                                        'diff': '+synthetic\n' * 1000}}}
    shown = checked('task_query', {'operation': 'get'}, {'operations': [raw]})['operations'][0]
    assert len(shown['result']['data']['diff']) <= 4000
    assert shown['result']['data']['diff_truncated']


def test_oversized_metadata_keeps_receipt_and_never_claims_mutation_failed():
    from hub.core_tools import bound_batch_response
    original = {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
        'operation_id': OP, 'state': 'succeeded', 'details': 'metadata' * 20000},
        'content': [{'type': 'text', 'text': 'metadata' * 20000}], 'isError': False}}
    saved = deepcopy(original)
    shown = bound_batch_response(original, output_budget=True)
    assert original == saved
    assert shown == saved
    # A successful authorization/control response cannot become a fake error.
    assert shown['result']['isError'] is False


@pytest.mark.parametrize('envelope', [
    {'result': {'task': {'taskId': OP, 'status': 'working'}}},
    {'error': {'code': -32602, 'message': 'Invalid params'}},
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': 'bad args'}]}},
])
def test_content_budget_preserves_non_content_protocol_envelopes(envelope):
    from hub.core_tools import bound_batch_response
    saved = deepcopy(envelope)
    assert bound_batch_response(envelope, output_budget=True) == saved
    assert envelope == saved


def test_text_budget_does_not_erase_native_screenshots():
    from hub.core_tools import bound_batch_response
    payload = {'result': {'structuredContent': {'operation_id': OP},
        'content': [{'type': 'text', 'text': '{}'}, {'type': 'image', 'data': 'x' * 100000, 'mimeType': 'image/png'}]}}
    assert bound_batch_response(payload, output_budget=True) == payload
