"""Project-relative recovery evidence survives without leaking arbitrary data."""
import pytest

from shared.filesystem_errors import safe_filesystem_error_detail


def test_known_file_error_keeps_only_typed_recovery_evidence():
    detail = {'retryable': False, 'publication': 'unconfirmed', 'filesystem_errno': 5,
              'durability': 'unconfirmed', 'staging_cleanup': 'unconfirmed',
              'path': 'src/new.py', 'destination': 'renamed.py', 'backup_id': 'a' * 32,
              'expected_sha256': 'b' * 64, 'mutation_started': True,
              'destination_overwritten': False}
    unsafe = {**detail, 'secret': 'do-not-disclose', 'token': 'do-not-disclose',
              'environment': {'KEY': 'do-not-disclose'}, 'config_path': '/private/agent/config'}
    assert safe_filesystem_error_detail('write', 'WRITE_OUTCOME_UNCONFIRMED', unsafe) == detail


@pytest.mark.parametrize('tool,code', [('exec', 'WRITE_OUTCOME_UNCONFIRMED'),
    ('agent_roots_set', 'DURABILITY_UNCONFIRMED'), ('write', 'UNRECOGNIZED_ERROR'),
    ('write', None), (None, 'WRITE_OUTCOME_UNCONFIRMED'),
    ([], 'WRITE_OUTCOME_UNCONFIRMED'), ('write', {})])
def test_unrelated_tools_and_errors_never_receive_file_details(tool, code):
    assert safe_filesystem_error_detail(tool, code, {'path': 'a', 'retryable': False}) == {}


@pytest.mark.parametrize('value', ['/private/config', 'C:\\secret', '\\\\server\\secret',
    '../outside', 'a/../../outside', 'a\x00b', 'a\nb', 'a' * 4097])
def test_absolute_traversal_and_unbounded_paths_are_not_disclosed(value):
    assert safe_filesystem_error_detail('write', 'WRITE_OUTCOME_UNCONFIRMED', {'path': value}) == {}


def test_wrong_types_and_arbitrary_strings_are_not_disclosed():
    detail = {'retryable': 'false', 'filesystem_errno': True, 'path': {'secret': 'value'},
              'backup_id': '/private/backup', 'expected_sha256': 'new',
              'publication': 'do-not-disclose', 'durability': 'credentials',
              'staging_cleanup': 1, 'destination_overwritten': 0}
    assert safe_filesystem_error_detail('write', 'WRITE_OUTCOME_UNCONFIRMED', detail) == {}
