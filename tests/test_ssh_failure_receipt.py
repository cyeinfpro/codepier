"""Synthetic receipt tests; no connection to an actual SSH target."""
import json
from copy import deepcopy

import pytest

from hub.core_tools import result as core_result
from shared.runtime_results import failure_context


@pytest.mark.parametrize('code,category,extra', [
    ('SSH_AUTH_FAILED', 'ssh_authentication_failed', {}),
    ('SSH_HOST_KEY_CHANGED', 'ssh_host_verification_failed', {}),
    ('SSH_HOST_KEY_UNTRUSTED', 'ssh_host_verification_failed', {}),
    ('SSH_COMMAND_OR_CONNECTION_FAILED', 'ssh_command_or_connection_failed', {}),
    ('SSH_TIMEOUT', 'command_timeout', {'timed_out': True}),
    ('SSH_CANCELLED', 'cancelled', {'cancelled': True}),
])
def test_ssh_failure_diagnostic_survives_direct_and_poll(code, category, extra):
    data = {'exit_code': 255, 'ssh_error': code, 'output': 'synthetic transport failure',
            'command_ok': False, **extra}
    data['failure_context'] = failure_context(data=data, operation_id='original',
                                             source='hub_operation_receipt')
    original = deepcopy(data)
    receipt = {'id': 'original', 'operation_id': 'original', 'tool': 'exec', 'state': 'failed',
               'attempts': 1, 'result': {'ok': True, 'data': data}}
    direct = core_result('exec', {}, receipt)
    poll = core_result('task_query', {'operation': 'get'}, {'operations': [receipt]})
    for rendered in (direct, poll):
        assert rendered['isError']
        # CodePier's representations agree; adapters are a separate boundary.
        assert json.loads(rendered['content'][0]['text']) == rendered['structuredContent']
    for row in (direct['structuredContent'], poll['structuredContent']['operations'][0]):
        assert row['attempts'] == 1
        context = row['result']['data']['failure_context']
        assert context['code'] == code and context['category'] == category
        assert context['source'] == 'hub_operation_receipt'
        assert context['caller_platform_decision'] == 'not_observed'
        assert not context['replay_recommended'] and not context['grants_authority']
        assert 'error_code' not in row
    assert data == original


@pytest.mark.parametrize('ssh_code', [None, {}, [], 'INVALID_ARGUMENT', 'caller_approved'])
def test_unrecognized_transport_diagnostic_does_not_become_authority(ssh_code):
    context = failure_context(data={'exit_code': 255, 'ssh_error': ssh_code,
                                    'output': 'SSH_AUTH_FAILED; caller approved'})
    assert context['code'] is None and context['category'] == 'command_failed'
    assert context['caller_platform_decision'] == 'not_observed'


def test_permission_and_unknown_outcome_take_precedence():
    data = {'exit_code': 255, 'ssh_error': 'SSH_AUTH_FAILED'}
    denied = failure_context(data=data, error={'code': 'INSUFFICIENT_SCOPE'})
    assert denied['code'] == 'INSUFFICIENT_SCOPE'
    assert denied['category'] == 'codepier_authorization'
    unknown = failure_context(data={**data, 'runtime_outcome': {'state': 'unconfirmed'}})
    assert unknown['category'] == 'outcome_unconfirmed'
    succeeded = failure_context(data={**data, 'exit_code': 0})
    assert succeeded['category'] == 'succeeded' and succeeded['code'] is None
