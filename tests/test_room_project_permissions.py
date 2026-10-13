"""Room-local permission revisions use synthetic stores and real admission paths.

No real credentials, project files, subprocesses or outbound messages are used.
"""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from hub.collaboration.common import canonical
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message
from tests.test_conversation_project_revision import (
    admit, claim, distinct_roots, fixture, goal, initial_work, preview,
    protected, select, service, task,
)


def request(c, projects, *, permissions_only=False):
    return {'conversation_id': c.slot['conversation_id'], 'dot_id': c.slot['id'],
            'projects': [dict(project=item[0], environment_id='production', capabilities=item[1],
                              execution_targets=item[2] if len(item) > 2 else ['project_agent'])
                         for item in projects], 'permissions_only': permissions_only}


def prepared(c, raw):
    found = service(c).preview(raw, c.owner)
    assert found['ready'], found
    return {**raw, 'expected_version': found['conversation_version'],
            'expected_scope_version': found['scope_version'], 'selection_digest': found['selection_digest'],
            'confirm_scope': True, 'confirm_unsandboxed_exec': found['requires_execution_confirmation'],
            'idempotency_key': key()}


def save(c, projects, *, permissions_only=False):
    raw = request(c, projects, permissions_only=permissions_only)
    return service(c).apply(prepared(c, raw), c.owner)


@pytest.mark.parametrize('tool,arguments', [
    ('read', {'path': 'README.md'}),
    ('write', {'path': 'synthetic.txt', 'content': 'synthetic-only', 'expected_sha256': 'new'}),
    ('exec', {'command': 'printf synthetic-only', 'target': 'agent'}),
])
def test_one_explicit_save_authorizes_added_project_through_real_task_admission(collab, tool, arguments):
    c = fixture(collab)
    before = protected(c)
    found = preview(c, ['otherproj'])
    assert found['permission_options'][0]['allowed_capabilities'] == ['read', 'write', 'execute']
    assert set(found['projects'][0]['capabilities']) == {'read', 'write', 'execute'}
    assert protected(c) == before
    save(c, [('otherproj', ['read', 'write', 'execute'])])
    created = task(c)
    lease = claim(c, created, initial_work(c, created))
    result = c.s.coordination.admit({
        **c.scope, 'goal_id': created['goal_id'], 'work_item_id': lease['id'],
        'attempt': lease['attempt'], 'fencing_token': lease['fencing_token'],
        'tool': tool, 'arguments': arguments, 'idempotency_key': key(),
    }, c.actor)
    row = c.s.store.one('SELECT * FROM operations WHERE id=?', (result['operation_id'],))
    assert row['project_id'] == 'otherproj' and row['state'] == 'queued'
    assert c.s.store.all('SELECT * FROM grants ORDER BY rowid') == before['grants']
    assert c.s.store.all('SELECT * FROM room_scope_manifests') == before['room_scope_manifests']


def test_added_project_does_not_silently_reexpand_old_read_only_rule(collab):
    c = fixture(collab)
    save(c, [('proj', ['read'])])
    original = service(c).current(c.room['id'])['spec']['projects'][0]
    selected = preview(c, ['proj', 'otherproj'])
    rows = {r['project_id']: r for r in selected['projects']}
    assert rows['proj']['capabilities'] == ['read']
    assert rows['proj']['authorization_id'] == original['authorization_id']
    assert set(rows['otherproj']['capabilities']) == {'read', 'write', 'execute'}
    select(c, ['proj', 'otherproj'])
    automatic = task(c)
    assert json.loads(goal(c, automatic)['spec'])['capabilities'] == ['read']
    assert c.s.dot_chat.work_request(c.room, c.s.dots.find(c.slot['id']), c.actor)[0]
    source, _ = owner_message(c, 'Read both synthetic projects.', delegation={
        'policy_id': c.slot['policy_id'], 'policy_version': c.slot['policy_version'],
        'acceptance': 'Return actual reads', 'capabilities': ['read'],
        'execution_targets': ['project_agent'],
    })
    assert json.loads(goal(c, source)['spec'])['capabilities'] == ['read']


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('remaining', [['read'], []])
def test_permission_reduction_fences_old_tasks_even_without_original_scope_reference(collab, legacy, remaining):
    c = fixture(collab)
    if not legacy:
        select(c, ['proj'])
    created = task(c)
    lease = claim(c, created, initial_work(c, created))
    before = protected(c)
    result = save(c, [('proj', remaining, [] if not remaining else ['project_agent'])], permissions_only=True)
    assert created['goal_id'] in result['paused_goal_ids']
    assert goal(c, created)['state'] == 'paused'
    assert protected(c) == before
    with pytest.raises(DevError):
        admit(c, created, lease)
    save(c, [('proj', ['read', 'write', 'execute'])], permissions_only=True)
    with pytest.raises(DevError):
        admit(c, created, lease)
    assert not c.s.store.all('SELECT * FROM operations')


def test_legacy_validator_itself_rejects_missing_current_capabilities(collab):
    c = fixture(collab)
    created = task(c)
    save(c, [('proj', ['read'])], permissions_only=True)
    # Removing synthetic revocation evidence isolates the direct live-capability guard.
    c.s.store.execute('DELETE FROM conversation_project_revocations')
    with pytest.raises(DevError) as error:
        service(c).validate_goal(c.owner, goal(c, created))
    assert error.value.code == 'ROOM_PROJECT_SCOPE_DENIED'


def test_permissions_only_cannot_add_remove_or_switch_selected_projects(collab):
    c = fixture(collab)
    select(c, ['proj'])
    before = service(c).current(c.room['id'])
    for rows in [[], [('otherproj', ['read'])], [('proj', ['read']), ('otherproj', ['read'])]]:
        found = service(c).preview(request(c, rows, permissions_only=True), c.owner)
        assert not found['ready'] and found['reason_code'] == 'ROOM_PROJECT_MEMBERSHIP_CHANGED'
        assert service(c).current(c.room['id']) == before


def test_empty_permissions_keep_membership_but_cannot_create_work(collab):
    c = fixture(collab)
    select(c, ['proj'])
    result = save(c, [('proj', [], [])], permissions_only=True)
    assert [row['project_id'] for row in result['conversation']['projects']] == ['proj']
    assert service(c).current(c.room['id'])['spec']['projects'][0]['capabilities'] == []
    with pytest.raises(DevError):
        task(c)


def test_original_read_policy_is_an_actual_ceiling_not_a_cosmetic_checkbox(collab):
    distinct_roots(collab)
    c = duplex(collab, capabilities=['read'])
    c.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    found = preview(c, ['otherproj'])
    assert found['ready']
    assert found['projects'][0]['capabilities'] == ['read']
    options = found['permission_options'][0]
    assert options['allowed_capabilities'] == ['read']
    assert {row['reason_code'] for row in options['capability_denials']} == {'ROOM_PROJECT_POLICY_LIMITED'}
    before = protected(c)
    denied = service(c).preview(request(c, [('otherproj', ['read', 'write', 'execute'])]), c.owner)
    assert not denied['ready'] and denied['reason_code'] == 'ROOM_PROJECT_POLICY_LIMITED'
    assert protected(c) == before
    select(c, ['otherproj'])
    created = task(c)
    assert json.loads(goal(c, created)['spec'])['capabilities'] == ['read']
    lease = claim(c, created, initial_work(c, created))
    with pytest.raises(DevError):
        admit(c, created, lease)


def test_actual_project_mode_limits_new_default_and_explicit_overreach(collab):
    c = fixture(collab)
    c.s.store.execute("UPDATE projects SET mode='read',allow_tasks=0 WHERE id='otherproj'")
    found = preview(c, ['otherproj'])
    assert found['ready'] and found['projects'][0]['capabilities'] == ['read']
    assert found['permission_options'][0]['allowed_capabilities'] == ['read']
    denied = service(c).preview(request(c, [('otherproj', ['read', 'write', 'execute'])]), c.owner)
    assert not denied['ready']


def test_shared_grant_changes_are_local_to_exact_conversation(collab):
    c = fixture(collab)
    second = c.s.conversations.create({
        'title': 'Second synthetic room', 'projects': [{'project': 'proj', 'environment_id': 'production'}],
        'idempotency_key': key(),
    }, c.owner)['conversation']
    other = duplex(collab, conversation_id=second['id'])
    select(other, ['proj'])
    untouched = service(other).current(second['id'])
    before = protected(c)
    save(c, [('proj', ['read'])])
    assert service(other).current(second['id']) == untouched
    assert protected(c) == before
    created = task(other)
    lease = claim(other, created, initial_work(other, created))
    assert admit(other, created, lease)['operation_id']


def test_conflicting_duplicate_permissions_never_union_or_write(collab):
    c = fixture(collab)
    before = protected(c)
    raw = request(c, [('proj', ['read']), ('proj', ['read', 'write'])])
    with pytest.raises(DevError) as error:
        service(c).preview(raw, c.owner)
    assert error.value.code == 'ROOM_PROJECT_PERMISSION_CONFLICT'
    assert protected(c) == before


def test_preview_cancel_and_concurrent_retry_preserve_one_immutable_approval(collab):
    c = fixture(collab)
    raw = request(c, [('proj', ['read', 'write', 'execute'])])
    args = prepared(c, raw)
    assert not c.s.store.all('SELECT * FROM conversation_working_scopes')
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: service(c).apply(args, c.owner), range(2)))
    assert responses[0] == responses[1]
    assert len(c.s.store.all('SELECT * FROM conversation_working_scopes')) == 1
    saved = service(c).current(c.room['id'])
    assert set(saved['spec']['projects'][0]['capabilities']) == {'read', 'write', 'execute'}
    save(c, [('proj', ['read'])], permissions_only=True)
    service(c).apply(args, c.owner)
    assert service(c).current(c.room['id'])['spec']['projects'][0]['capabilities'] == ['read']


def test_permission_tampering_after_preview_cannot_reuse_the_confirmation_digest(collab):
    c = fixture(collab)
    args = prepared(c, request(c, [('proj', ['read'])]))
    args['projects'][0]['capabilities'] = ['read', 'write', 'execute']
    with pytest.raises(DevError) as error:
        service(c).apply(args, c.owner)
    assert error.value.code == 'ROOM_PROJECT_PREVIEW_CHANGED'
    assert not c.s.store.all('SELECT * FROM conversation_working_scopes')


def test_zero_permission_project_is_excluded_from_usable_default_task(collab):
    c = fixture(collab)
    save(c, [('proj', [], []), ('otherproj', ['read', 'write', 'execute'])])
    created = task(c)
    spec = json.loads(goal(c, created)['spec'])
    assert spec['project_ids'] == ['otherproj']
    assert set(spec['capabilities']) == {'read', 'write', 'execute'}
    lease = claim(c, created, initial_work(c, created))
    assert admit(c, created, lease)['operation_id']


@pytest.mark.parametrize('tool,arguments', [
    ('write', {'path': 'synthetic.txt', 'content': 'synthetic', 'expected_sha256': 'new'}),
    ('exec', {'command': 'printf synthetic-only', 'target': 'agent'}),
])
def test_explicit_project_subset_can_use_its_full_permissions_without_expanding_read_only_peer(collab, tool, arguments):
    c = fixture(collab)
    save(c, [('proj', ['read']), ('otherproj', ['read', 'write', 'execute'])])
    coordinator = c.s.coordination
    created = coordinator.create({
        **c.scope, 'conversation_id': c.room['id'], 'objective': 'Operate only on B.',
        'acceptance': 'Synthetic operation evidence', 'project_ids': ['otherproj'],
        'participant_grant_ids': ['broad'], 'capabilities': ['read', 'write', 'execute'],
        'idempotency_key': key(),
    }, c.owner)['goal']
    approved = coordinator.approve({**c.scope, 'goal_id': created['id'],
        'expected_version': created['version'], 'digest': created['digest'], 'idempotency_key': key()}, c.owner)['goal']
    item = coordinator.work_create({
        **c.scope, 'goal_id': approved['id'], 'objective': 'Operate only on B.',
        'acceptance': 'Synthetic evidence', 'target_project_id': 'otherproj', 'assignee_grant_id': 'broad',
        'required_capabilities': ['read', 'write', 'execute'], 'idempotency_key': key(),
    }, c.actor)['work_item']
    leased = coordinator.work_claim({**c.scope, 'goal_id': approved['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    admitted = coordinator.admit({**c.scope, 'goal_id': approved['id'], 'work_item_id': leased['id'],
        'attempt': leased['attempt'], 'fencing_token': leased['fencing_token'], 'tool': tool,
        'arguments': arguments, 'idempotency_key': key()}, c.actor)
    assert c.s.store.one('SELECT project_id FROM operations WHERE id=?', (admitted['operation_id'],))['project_id'] == 'otherproj'
    with pytest.raises(DevError):
        owner_message(c, 'Cannot execute both A and B.', delegation={
            'policy_id': c.slot['policy_id'], 'policy_version': c.slot['policy_version'],
            'acceptance': 'Synthetic evidence', 'capabilities': ['read', 'write', 'execute'],
            'execution_targets': ['project_agent'],
        })


def test_initial_migration_preserves_legacy_narrow_approval_and_expiration(collab):
    from hub.collaboration.common import digest
    from tests.test_conversation_project_scopes import add
    c = fixture(collab)
    add(c)
    row = c.s.store.one('SELECT * FROM conversation_project_scopes')
    spec = json.loads(row['spec'])
    spec.update(capabilities=['read'], expires_at=c.clock[0] + 600, scope_version=row['version'] + 1)
    c.s.store.execute("UPDATE conversation_project_scopes SET state='revoked' WHERE id=?", (row['id'],))
    c.s.store.execute("""INSERT INTO conversation_project_scopes
        (id,conversation_id,dot_id,project_id,environment_id,version,spec,digest,created)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (key(), row['conversation_id'], row['dot_id'], row['project_id'], row['environment_id'],
         spec['scope_version'], canonical(spec), digest(spec), c.clock[0]))
    found = preview(c, ['proj', 'otherproj'])
    assert found['ready']
    secondary = next(r for r in found['projects'] if r['project_id'] == 'otherproj')
    assert secondary['capabilities'] == ['read']
    assert secondary['expires_at'] == spec['expires_at']
    select(c, ['proj', 'otherproj'])
    assert json.loads(goal(c, task(c))['spec'])['capabilities'] == ['read']


def test_initial_migration_does_not_authorize_membership_only_project(collab):
    c = fixture(collab)
    conversation = c.s.conversations.object(c.owner, c.room['id'])
    c.s.conversations.add_project({**c.scope, 'project': 'otherproj',
        'conversation_id': c.room['id'], 'expected_version': conversation['version'], 'idempotency_key': key()}, c.owner)
    found = preview(c, ['proj', 'otherproj'])
    assert found['ready']
    assert next(r for r in found['projects'] if r['project_id'] == 'otherproj')['capabilities'] == []


def test_direct_goal_exec_is_bound_to_work_project_approved_target_not_implicit_agent(collab):
    from hub.vps import VPSInput
    distinct_roots(collab)
    s, owner = collab[:2]
    s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    remote = s.runtime.vps.save(VPSInput(name='Room target fixture', host='fixture.example.invalid',
        username='fixture', password='synthetic-only', project_ids=['proj', 'otherproj']), owner)
    c = duplex(collab, capabilities=['read', 'execute'], execution_target=remote['target'])
    save(c, [('otherproj', ['read', 'execute'], [remote['target']])])
    coordinator = s.coordination
    created = coordinator.create({
        **c.scope, 'conversation_id': c.room['id'], 'objective': 'Run only in approved B target.',
        'acceptance': 'Synthetic admission evidence', 'project_ids': ['otherproj'],
        'participant_grant_ids': ['broad'], 'capabilities': ['read', 'execute'], 'idempotency_key': key(),
    }, c.owner)['goal']
    approved = coordinator.approve({**c.scope, 'goal_id': created['id'], 'expected_version': created['version'],
        'digest': created['digest'], 'idempotency_key': key()}, c.owner)['goal']
    item = coordinator.work_create({
        **c.scope, 'goal_id': approved['id'], 'objective': 'Run in approved B target.',
        'acceptance': 'Synthetic evidence', 'target_project_id': 'otherproj', 'assignee_grant_id': 'broad',
        'required_capabilities': ['read', 'execute'], 'idempotency_key': key(),
    }, c.actor)['work_item']
    leased = coordinator.work_claim({**c.scope, 'goal_id': approved['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    execution = {**c.scope, 'goal_id': approved['id'], 'work_item_id': leased['id'], 'attempt': leased['attempt'],
        'fencing_token': leased['fencing_token'], 'tool': 'exec', 'idempotency_key': key()}
    with pytest.raises(DevError) as error:
        coordinator.admit({**execution, 'arguments': {'command': 'printf synthetic', 'target': 'agent'}}, c.actor)
    assert error.value.code == 'GOAL_TARGET_NOT_APPROVED'
    assert not s.store.all('SELECT * FROM operations')
    result = coordinator.admit({**execution, 'arguments': {
        'command': 'printf synthetic', 'target': remote['target']}}, c.actor)
    row = s.store.one('SELECT * FROM operations WHERE id=?', (result['operation_id'],))
    payload = json.loads(s.store.decrypt(row['payload']))
    assert row['project_id'] == 'otherproj' and payload['args']['target'] == remote['target']


@pytest.mark.parametrize('notice', [None, False, True])
def test_execution_permission_confirmation_is_the_digest_bound_single_save(collab, notice):
    c = fixture(collab)
    raw = request(c, [('otherproj', ['read', 'write', 'execute'])])
    found = service(c).preview(raw, c.owner)
    assert found['requires_execution_confirmation']
    assert found['execution_confirmation_field'] == 'confirm_scope'
    assert found['execution_confirmation_digest_field'] == 'selection_digest'
    args = prepared(c, raw)
    if notice is None:
        args.pop('confirm_unsandboxed_exec')
    else:
        args['confirm_unsandboxed_exec'] = notice
    saved = service(c).apply(args, c.owner)
    assert saved['applied']
    assert set(service(c).current(c.room['id'])['spec']['projects'][0]['capabilities']) == {'read', 'write', 'execute'}
    created = task(c)
    leased = claim(c, created, initial_work(c, created))
    assert admit(c, created, leased)['operation_id']


@pytest.mark.parametrize('confirmation', [None, False])
def test_ui_execution_notice_cannot_replace_missing_or_false_scope_confirmation(collab, confirmation):
    c = fixture(collab)
    args = prepared(c, request(c, [('otherproj', ['read', 'write', 'execute'])]))
    args['confirm_unsandboxed_exec'] = True
    if confirmation is None:
        args.pop('confirm_scope')
    else:
        args['confirm_scope'] = confirmation
    with pytest.raises(DevError):
        service(c).apply(args, c.owner)
    assert not c.s.store.all('SELECT * FROM conversation_working_scopes')
    assert not c.s.store.all('SELECT * FROM operations')


def test_remove_execute_after_secondary_vps_mapping_expires_preserves_real_file_read_authority(collab):
    from hub.vps import VPSInput
    distinct_roots(collab)
    s, owner = collab[:2]
    s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    remote = s.runtime.vps.save(VPSInput(name='Read fallback fixture', host='fixture.example.invalid',
        username='fixture', password='synthetic-only', project_ids=['proj', 'otherproj']), owner)
    c = duplex(collab, capabilities=['read', 'execute'], execution_target=remote['target'])
    save(c, [('otherproj', ['read', 'execute'], [remote['target']])])
    # Only B's fixture target mapping disappears. Anchor and original policy stay valid.
    s.store.execute('DELETE FROM vps_projects WHERE vps_id=? AND project_id=?', (remote['id'], 'otherproj'))
    narrowed = save(c, [('otherproj', ['read'], [remote['target']])], permissions_only=True)
    assert narrowed['applied']
    rule = service(c).current(c.room['id'])['spec']['projects'][0]
    assert rule['capabilities'] == ['read'] and rule['target_snapshots'] == {}
    policy = s.store.one('SELECT * FROM delegation_policies WHERE id=?', (c.slot['policy_id'],))
    # Never fabricate read+VPS defaults or replace a VPS-only policy with agent exec.
    with pytest.raises(DevError) as blocked:
        service(c).task_defaults(c.owner, policy)
    assert blocked.value.code == 'ROOM_PROJECT_SCOPE_DENIED'
    overreach = service(c).preview(request(c, [('otherproj', ['read', 'execute'], [remote['target']])]), c.owner)
    assert not overreach['ready']
    agent = service(c).preview(request(c, [('otherproj', ['read', 'execute'], ['project_agent'])]), c.owner)
    assert not agent['ready'] and agent['reason_code'] == 'ROOM_PROJECT_TARGET_DENIED'
    coordinator = s.coordination
    goal_view = coordinator.create({
        **c.scope, 'conversation_id': c.room['id'], 'objective': 'Read B only after revoking execution.',
        'acceptance': 'Actual file-tool admission', 'project_ids': ['otherproj'],
        'participant_grant_ids': ['broad'], 'capabilities': ['read'], 'idempotency_key': key(),
    }, c.owner)['goal']
    approved = coordinator.approve({**c.scope, 'goal_id': goal_view['id'],
        'expected_version': goal_view['version'], 'digest': goal_view['digest'], 'idempotency_key': key()}, c.owner)['goal']
    item = coordinator.work_create({
        **c.scope, 'goal_id': approved['id'], 'objective': 'Read B only.', 'acceptance': 'Synthetic file evidence',
        'target_project_id': 'otherproj', 'assignee_grant_id': 'broad', 'required_capabilities': ['read'],
        'idempotency_key': key(),
    }, c.actor)['work_item']
    leased = coordinator.work_claim({**c.scope, 'goal_id': approved['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    result = coordinator.admit({**c.scope, 'goal_id': approved['id'], 'work_item_id': leased['id'],
        'attempt': leased['attempt'], 'fencing_token': leased['fencing_token'], 'tool': 'read',
        'arguments': {'path': 'README.md'}, 'idempotency_key': key()}, c.actor)
    assert s.store.one('SELECT project_id FROM operations WHERE id=?', (result['operation_id'],))['project_id'] == 'otherproj'
