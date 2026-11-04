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
    'title': 'A room', 'name': 'A member', 'flow_id': 'connect-flow', 'member_id': 'room-member',
    'invite_code': 'CPR-fixture-invitation-not-a-secret-' * 2,
    'connection_proof': 'fixture-member-proof-not-a-secret-' * 2,
    'probe_nonce': 'fixture-connection-nonce-not-a-secret-' * 2,
    'work_id': 'room-work',
    'dot_id': 'task-dot', 'message_id': 'message', 'attachment_id': 'attachment', 'confirm_task': True,
    'team_id': 'team', 'binding_generation': 'binding-generation',
    'wake_ticket': 'fixture-wake-ticket-not-a-secret-' * 2,
    'consumer_run_id': 'consumer-run', 'consumer_session_id': 'consumer-session',
    'consumer_fence': 1, 'consumer_proof': 'fixture-consumer-proof-not-a-secret-' * 2,
    'expected_inbox_revision': 0, 'expected_context_version': 0, 'confirm_consumer_protocol': True,
    'entry_ids': ['mailbox-entry'], 'coordinator_epoch': 1, 'expected_intent_revision': 1,
    'recipient_dot_id': 'peer-dot', 'coordinator_dot_id': 'coordinator-dot',
    'recipient_member_id': 'peer-member', 'target_member_id': 'peer-member', 'expected_coordinator_epoch': 1,
    'handoff_id': 'handoff',
    'purpose': 'Review original constraints',
    'artifact_refs': [{'work_item_id': 'work', 'version': 1, 'digest': 'a' * 64}],
    'source_version': 1, 'source_digest': 'a' * 64,
    'project': 'project', 'id': 'item', 'goal_id': 'goal', 'policy_id': 'policy', 'policy_version': 1,
    'delegation_id': 'delegation', 'checkpoint': 'signed-checkpoint', 'conversation_id': 'room', 'room_id': 'room',
    'work_item_id': 'work', 'job_id': 'job', 'result_id': 'result', 'query': 'search text',
    'client_message_id': 'client-message', 'idempotency_key': 'stable-idempotency',
    'request_key': 'stable-original-publication-key',
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
    # Examples come from the authoritative action schema, not the broad host
    # discovery envelope. This keeps every action-specific required-field check.
    schema = ADAPTERS[tool].json_schema()
    spec = branch(schema, action)
    if action == 'dot_handle':
        return {'action': action, 'project': 'project', 'dot_id': 'dot', 'handling_schema_version': 1,
            'scope_digest': 'a' * 64, 'inputs': [{'message_id': 'source', 'source_version': 1,
                'source_digest': 'b' * 64, 'expected_revision': 0}],
            'handling_state': 'observed', 'reason_code': 'received', 'idempotency_key': 'stable-handling'}
    if action == 'execute':
        return {'action': action, **{key: VALUES[key] for key in
            ('project', 'goal_id', 'work_item_id', 'attempt', 'fencing_token', 'idempotency_key')},
            'tool': 'exec', 'arguments': {'command': 'printf fixture'}}
    result = {}
    for key in spec['required']:
        field = spec['properties'][key]
        if key == 'action':
            result[key] = action
        elif key == 'flow_action':
            result[key] = field.get('enum', [field.get('const')])[0]
        elif key == 'binding_generation' and field.get('type') == 'integer':
            result[key] = 1
        else:
            result[key] = deepcopy(VALUES[key])
    if action == 'room_context_update':
        result['reviewed_sources'] = [{'message_id': 'context-source', 'version': 1, 'digest': 'a' * 64}]
    return result


def test_room_migration_is_an_explicit_owner_mutation_not_a_query():
    assert 'room_migrate' in ACTIONS['collaboration']
    assert 'room_migrate' not in ACTIONS['collaboration_query']
    raw = {'action': 'room_migrate', 'flow_action': 'activate', 'conversation_id': 'original-room',
        'expected_version': 4, 'idempotency_key': 'original-cutover-request'}
    target, args = resolve('collaboration', raw)
    assert target == 'collaboration_room_migrate'
    assert args == {'action': 'activate', 'conversation_id': 'original-room',
        'expected_version': 4, 'idempotency_key': 'original-cutover-request'}
    for change in ({'flow_action': 'auto'}, {'expected_version': 0}, {'owner_approved': True}):
        with pytest.raises(DevError):
            resolve('collaboration', {**raw, **change})


@pytest.mark.parametrize('tool,action', [(tool, action) for tool, actions in ACTIONS.items() for action in actions])
def test_every_action_accepts_exact_shape_and_rejects_missing_or_foreign_fields(tool, action):
    definition = next(t for t in tool_definitions() if t['name'] == tool)
    schema = definition['inputSchema']
    validator = Draft202012Validator(ADAPTERS[tool].json_schema())
    raw = example(tool, action)
    Draft202012Validator(schema).validate(raw)
    validator.validate(raw)
    ADAPTERS[tool].validate_python(raw)
    target, adapted = resolve(tool, raw)
    c.TOOL_MODELS[target].model_validate(adapted)
    # Route adaptation must not materialize nested defaults or change any key.
    expected = {k: v for k, v in raw.items() if k != 'action'}
    if 'flow_action' in expected:
        expected['action'] = expected.pop('flow_action')
    assert expected == {k: v for k, v in adapted.items() if k != 'kind'}
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
    assert len(base) == (9 if profile == 'coding' else 13)
    assert len(base + collaboration) == (12 if profile == 'coding' else 16)
    names = [item['name'] for item in base + collaboration]
    assert len(names) == len(set(names))
    assert not LEGACY_TOOLS.intersection(names)
    assert PUBLIC_TOOLS <= set(names)
    for item in collaboration:
        Draft202012Validator.check_schema(item['inputSchema'])
        assert item['annotations']['readOnlyHint'] == (item['name'] == 'collaboration_query')
        assert item['annotations']['openWorldHint'] == (item['name'] == 'collaboration_work')
        assert item['inputSchema']['additionalProperties'] is False
        assert set(item['inputSchema']['properties']['action']['enum']) == set(ACTIONS[item['name']])
        for spec in ADAPTERS[item['name']].json_schema()['$defs'].values():
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


@pytest.mark.parametrize('action', ['takeover', 'restore_binding'])
def test_room_recovery_requires_explicit_member_and_current_role_and_intent(action):
    raw = {**example('collaboration', 'room_work_control'), 'flow_action': action}
    with pytest.raises(DevError):
        resolve('collaboration', raw)
    approved = {**raw, 'target_member_id': 'original-member',
        'expected_coordinator_epoch': 2, 'expected_intent_revision': 3}
    target, arguments = resolve('collaboration', approved)
    assert target == 'collaboration_room_work_control'
    assert arguments['target_member_id'] == 'original-member'
    assert arguments['expected_coordinator_epoch'] == 2
    assert arguments['expected_intent_revision'] == 3
    for field in ('target_member_id', 'expected_coordinator_epoch', 'expected_intent_revision'):
        with pytest.raises(DevError):
            resolve('collaboration', {key: value for key, value in approved.items() if key != field})
    with pytest.raises(DevError):
        resolve('collaboration', {**approved, 'expected_coordinator_epoch': True})


def test_room_handoff_preserves_native_identity_and_rejects_blank_purpose():
    raw = example('collaboration', 'room_work_handoff')
    target, arguments = resolve('collaboration', raw)
    assert target == 'collaboration_room_work_handoff'
    assert arguments['member_id'] == VALUES['member_id']
    assert arguments['recipient_member_id'] == VALUES['recipient_member_id']
    assert 'project' not in arguments and 'dot_id' not in arguments
    with pytest.raises(DevError):
        resolve('collaboration', {**raw, 'purpose': '  '})


@pytest.mark.parametrize('empty', [{}, {'facts': []}, {'reviewed_sources': []},
    {'facts': [], 'reviewed_sources': []}, {'supersedes': ['old-fact']}])
def test_room_context_schema_and_runtime_reject_updates_without_source_or_fact(empty):
    raw = {k: v for k, v in example('collaboration', 'room_context_update').items() if k != 'reviewed_sources'}
    raw.update(empty)
    validator = Draft202012Validator(ADAPTERS['collaboration'].json_schema())
    assert list(validator.iter_errors(raw))
    with pytest.raises(DevError):
        resolve('collaboration', raw)


@pytest.mark.parametrize('mode', ['facts', 'reviewed_sources', 'both'])
def test_room_context_schema_and_runtime_accept_each_nonempty_source_alternative(mode):
    raw = example('collaboration', 'room_context_update')
    sources = deepcopy(raw['reviewed_sources'])
    if mode != 'reviewed_sources':
        raw['facts'] = [{'kind': 'question', 'key': 'pending', 'text': 'Which constraints remain open?', 'sources': sources}]
    if mode == 'facts':
        raw.pop('reviewed_sources')
    validator = Draft202012Validator(ADAPTERS['collaboration'].json_schema())
    validator.validate(raw)
    target, arguments = resolve('collaboration', raw)
    assert target == 'collaboration_room_context_update'
    assert arguments['expected_context_version'] == 0
