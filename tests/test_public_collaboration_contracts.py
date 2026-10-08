"""Public action contracts and compatibility invariants, without host mutations."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from shared import collaboration_contracts as c
from shared.contracts import tool_definitions as core_definitions
from shared.public_collaboration import ACTIONS, ADAPTERS, LEGACY_TOOLS, PUBLIC_TOOLS, request, resolve, tool_definitions
from shared.util import DevError

PLAN = {'intent': 'Monitor fixture', 'valid_until': '2030-01-01T00:00:00Z', 'rules': [{
    'rule_id': 'availability', 'probe_id': 'probe', 'metric': 'availability',
    'open_when': {'operator': 'lt', 'value': 0.9},
    'close_when': {'operator': 'gt', 'value': 0.99}, 'require_recovery_probe': 'probe'}]}
VALUES = {
    'project': 'project', 'id': 'item', 'goal_id': 'goal', 'policy_id': 'policy', 'policy_version': 1,
    'delegation_id': 'delegation', 'checkpoint': 'signed-checkpoint', 'conversation_id': 'room', 'room_id': 'room',
    'work_item_id': 'work', 'job_id': 'job', 'result_id': 'result', 'query': 'search text',
    'client_message_id': 'client-message', 'idempotency_key': 'stable-idempotency',
    'expected_version': 1, 'attempt': 1, 'fencing_token': 1, 'candidate': PLAN,
    'code': 'CPJ-AAAA-BBBB-CCCC-DDDD', 'body_text': 'Message text', 'request': 'Inspect project',
    'source_message_id': 'source-message', 'structured_mentions': [{'agent_id': 'agent'}],
    'objective': 'Inspect project', 'acceptance': 'Report evidence',
    'project_ids': ['project'], 'participant_grant_ids': ['grant'], 'capabilities': ['read'],
    'target_project_id': 'project', 'assignee_grant_id': 'grant', 'summary': 'Checked fixture',
    'outcome': 'succeeded', 'reason_code': 'missing_data',
    'result': {'outcome': 'blocked', 'summary': 'Missing required data'},
}


def branch(schema, action):
    value = schema['discriminator']['mapping'][action]
    if isinstance(value, str):
        return schema['$defs'][value.rsplit('/', 1)[-1]]
    return value


def example(tool, action):
    schema = next(t['inputSchema'] for t in tool_definitions() if t['name'] == tool)
    spec = branch(schema, action)
    if action == 'execute':
        return {'action': action, **{key: VALUES[key] for key in
            ('project', 'goal_id', 'work_item_id', 'attempt', 'fencing_token', 'idempotency_key')},
            'tool': 'exec', 'arguments': {'command': 'printf fixture'}}
    return {key: (action if key == 'action' else deepcopy(VALUES[key])) for key in spec['required']}


@pytest.mark.parametrize('tool,action', [(tool, action) for tool, actions in ACTIONS.items() for action in actions])
def test_every_action_accepts_exact_shape_and_rejects_missing_or_foreign_fields(tool, action):
    definition = next(t for t in tool_definitions() if t['name'] == tool)
    schema = definition['inputSchema']
    validator = Draft202012Validator(schema)
    raw = example(tool, action)
    validator.validate(raw)
    ADAPTERS[tool].validate_python(raw)
    target, adapted = resolve(tool, raw)
    c.TOOL_MODELS[target].model_validate(adapted)
    # Route adaptation must not materialize nested defaults or change any key.
    assert {k: v for k, v in raw.items() if k != 'action'} == {
        k: v for k, v in adapted.items() if k != 'kind'}
    for field in raw:
        invalid = {k: v for k, v in raw.items() if k != field}
        assert list(validator.iter_errors(invalid)), (tool, action, field)
        with pytest.raises(DevError):
            resolve(tool, invalid)
    for invalid in ({**raw, 'owner_approved': True}, {**raw, 'action': 'shell_exec'}):
        assert list(validator.iter_errors(invalid))
        with pytest.raises(DevError):
            resolve(tool, invalid)


@pytest.mark.parametrize('profile', ['core', 'full', 'coding'])
@pytest.mark.parametrize('authorization', ['fixed', 'role'])
def test_public_catalog_has_sixteen_native_tools_with_correct_hints(profile, authorization):
    base = core_definitions(profile, authorization)
    collaboration = tool_definitions(authorization)
    assert len(base) == 13 and len(base + collaboration) == 16
    names = [item['name'] for item in base + collaboration]
    assert len(names) == len(set(names))
    assert not LEGACY_TOOLS.intersection(names)
    assert PUBLIC_TOOLS <= set(names)
    for item in collaboration:
        Draft202012Validator.check_schema(item['inputSchema'])
        assert item['annotations']['readOnlyHint'] == (item['name'] == 'collaboration_query')
        assert item['annotations']['openWorldHint'] == (item['name'] == 'collaboration_work')
        for spec in item['inputSchema']['$defs'].values():
            if spec.get('type') == 'object':
                assert spec['additionalProperties'] is False
        assert 'Legacy name to action' in item['description']
        assert 'notification_only' in item['description']
    assert next(item for item in base if item['name'] == 'get_profile')['_meta']['openai/profile']
    native_file = next(item for item in base if item['name'] == 'write')['inputSchema']['properties']['file']
    assert native_file  # Native attachments stay at their original top-level host field.


@pytest.mark.parametrize('step,args', [
    ('read', {'path': 'README.md'}),
    ('write', {'path': 'new.txt', 'content': 'ok', 'expected_sha256': 'new'}),
    ('edit', {'path': 'file.txt', 'edits': [{'old_text': 'a', 'new_text': 'b'}], 'expected_sha256': 'a' * 64}),
    ('edit', {'changes': [{'path': 'file.txt', 'content': 'ok', 'expected_sha256': 'a' * 64}]}),
    ('exec', {'command': 'printf fixture', 'target': 'vps:' + 'a' * 32}),
])
def test_managed_execute_discriminates_typed_steps_and_preserves_raw_intent(step, args):
    raw = {**example('collaboration_work', 'execute'), 'tool': step, 'arguments': args}
    schema = next(item['inputSchema'] for item in tool_definitions() if item['name'] == 'collaboration_work')
    Draft202012Validator(schema).validate(raw)
    assert resolve('collaboration_work', raw) == ('collaboration_work_execute', {k: v for k, v in raw.items() if k != 'action'})
    for invalid in ({**args, 'bypass_policy': True}, {**args, 'operation': 'import'},
                    {**args, 'idempotency_key': 'replace-operation-key'}):
        with pytest.raises(ValidationError):
            ADAPTERS['collaboration_work'].validate_python({**raw, 'arguments': invalid})
    assert resolve('collaboration_work_execute', {k: v for k, v in raw.items() if k != 'action'}) == (
        'collaboration_work_execute', {k: v for k, v in raw.items() if k != 'action'})


def test_read_catalog_cannot_accept_mutating_or_executing_actions():
    for action in ACTIONS['collaboration'] | ACTIONS['collaboration_work']:
        with pytest.raises(DevError):
            resolve('collaboration_query', {'action': action, 'project': 'project'})
    for action in ('claim', 'heartbeat', 'execute', 'result'):
        with pytest.raises(DevError):
            resolve('collaboration_query', example('collaboration_work', action))


def test_canonical_requests_preserve_checkpoint_mode_and_exact_required_scope():
    args = {'project': 'project', 'environment_id': 'production', 'policy_id': 'policy',
            'policy_version': 1, 'mode': 'notification_only', 'checkpoint': 'signed-checkpoint'}
    canonical = request('collaboration_delegation_inbox', args)
    assert canonical['tool'] == 'collaboration_query'
    assert resolve(canonical['tool'], canonical['arguments']) == ('collaboration_delegation_inbox', args)
    assert canonical['arguments']['checkpoint'] == args['checkpoint']
    assert canonical['arguments']['mode'] == 'notification_only'
