"""Read-only result presentation must accept edit file-count summaries."""

from copy import deepcopy

import pytest

from shared.mcp_presentation import token_view


@pytest.mark.parametrize('name', ['write', 'edit'])
@pytest.mark.parametrize('files', [0, 3, None, 'unknown', {'count': 3}])
def test_file_count_is_not_treated_as_a_batch(name, files):
    original = {'files': files, 'operation_id': 'synthetic-operation'}
    before = deepcopy(original)
    assert token_view(name, {}, original) == original
    assert original == before


@pytest.mark.parametrize('polling', ['task_query', 'process', 'exec'])
def test_file_count_in_stored_edit_receipt_remains_readable(polling):
    original = {
        'id': 'synthetic-operation', 'tool': 'edit', 'state': 'succeeded',
        'args_summary': {'operation': 'checkpoint'},
        'result': {'ok': True, 'data': {'files': 3, 'bytes': 42}},
    }
    payload = original if polling == 'exec' else {'operations': [original]}
    before = deepcopy(payload)
    assert token_view(polling, {}, payload) == payload
    assert payload == before


def test_batch_diff_still_truncates_without_mutating_stored_result():
    original = {'files': [{'path': 'example.txt', 'diff': 'line\n' * 1000}, None]}
    before = deepcopy(original)
    rendered = token_view('edit', {}, original)
    assert rendered['files'][0]['diff_truncated'] is True
    assert len(rendered['files'][0]['diff']) <= 4000
    assert rendered['files'][1] is None
    assert original == before


@pytest.mark.parametrize('options', [{'view': 1}, {'verbosity': 'full'}])
def test_full_and_legacy_views_keep_complete_batch_result(options):
    original = {'files': [{'path': 'example.txt', 'diff': 'line\n' * 1000}]}
    before = deepcopy(original)
    assert token_view('edit', {}, original, **options) == before
    assert original == before
