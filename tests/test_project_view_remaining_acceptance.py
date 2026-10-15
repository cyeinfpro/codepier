"""Required cases for still-unapplied projection details; failures are not waived."""
import json
from shared.mcp_presentation import present


def test_nested_connection_project_metadata_uses_same_minimal_contract():
    sentinel = 'PRIVATE_NESTED_NODE_RELATION'
    raw = {'consumer_configuration': {'scope': {'project_snapshot': {
        'project_id': 'project', 'root': '/synthetic/private/root', 'device_id': sentinel}}}}
    shown = present('collaboration_query', {'action': 'dot_connection'}, raw)
    assert sentinel not in json.dumps(shown)
    assert shown['consumer_configuration']['scope']['project_snapshot']['project_id'] == 'project'
    assert raw['consumer_configuration']['scope']['project_snapshot']['device_id'] == sentinel


def test_future_native_policy_denial_keeps_machine_readable_refusal():
    code = 'FUTURE_NATIVE_POLICY_DENIED'
    raw = {'execution_policy': {'version': 2,
        'future_check': {'state': 'denied', 'code': code, 'message': 'Actual native policy refusal'}},
        'shell': {'enabled': True}, 'tools': {}}
    shown = present('project_query', {'operation': 'status'}, raw)
    assert shown['execution_policy']['restrictions_unknown']
    assert code in json.dumps(shown['execution_policy'])


def test_connection_receipt_applies_same_projection():
    raw = {'id': 'a'*32, 'tool': 'collaboration_dot_connection', 'state': 'succeeded',
        'result': {'ok': True, 'data': {'consumer_configuration': {'scope': {
            'project_snapshot': {'project_id': 'p', 'device_id': 'PRIVATE_RECEIPT_NODE', 'root': '/private'}
        }}}}}
    shown = present('task_query', {'operation': 'get'}, {'operations': [raw]})
    assert 'PRIVATE_RECEIPT_NODE' not in json.dumps(shown)
    assert shown['operations'][0]['result']['data']['consumer_configuration']['scope']['project_snapshot']['project_id'] == 'p'
    assert raw['result']['data']['consumer_configuration']['scope']['project_snapshot']['device_id'] == 'PRIVATE_RECEIPT_NODE'


def test_policy_extension_projection_is_idempotent_and_leaves_source_opaque():
    raw = {'execution_policy': {'future': {'state': 'denied', 'code': 'ACTUAL_EXTENSION_DENIED',
            'hostname': 'PRIVATE_POLICY_HOST', 'message': 'PRIVATE_POLICY_HOST',
            'content': {'state': 'denied', 'code': 'UNTRUSTED_BODY_CODE'}}},
           'shell': {'enabled': True}, 'tools': {}}
    shown = present('project_query', {'operation': 'status'}, raw)
    assert 'ACTUAL_EXTENSION_DENIED' in json.dumps(shown)
    assert 'PRIVATE_POLICY_HOST' not in json.dumps(shown)
    assert 'UNTRUSTED_BODY_CODE' not in json.dumps(shown)
    assert shown['shell']['enabled'] is True
    assert present('project_query', {'operation': 'status'}, shown) == shown
    assert raw['execution_policy']['future']['message'] == 'PRIVATE_POLICY_HOST'


def test_nested_connection_freeform_evidence_is_not_project_metadata():
    body = {'project_snapshot': {'project_id': 'p', 'device_id': 'BODY_IS_VERBATIM'}}
    raw = {'consumer_configuration': {'scope': {'project_snapshot': {
               'project_id': 'p', 'device_id': 'PRIVATE_METADATA'}},
               'result': body, 'arguments': body, 'body_text': json.dumps(body)},
           'attempt': 2, 'fencing_token': 7, 'lease_until': 123}
    shown = present('collaboration_query', {'action': 'dot_connection'}, raw)
    assert 'PRIVATE_METADATA' not in json.dumps(shown)
    for key in ('result', 'arguments', 'body_text'):
        assert shown['consumer_configuration'][key] == raw['consumer_configuration'][key]
    for key in ('attempt', 'fencing_token', 'lease_until'):
        assert shown[key] == raw[key]
