"""Synthetic stores and Runtime admission only; no production scopes or commands."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hub.collaboration.common import canonical
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dots import setup, send


def fixture(c):
    ctx = setup(c)
    ctx.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    return ctx


def preview(c):
    return c.s.conversations.project_scopes.preview({
        'project': 'otherproj', 'environment_id': 'production', 'conversation_id': c.room['id']}, c.owner)


def add_args(c):
    found = preview(c)
    assert found['ready'], found
    return {'project': 'otherproj', 'environment_id': 'production', 'conversation_id': c.room['id'],
            'expected_version': found['scope']['conversation_version'], 'idempotency_key': key(),
            'authorization': {'dot_id': found['scope']['dot_id'], 'scope_digest': found['scope_digest'], 'confirm_scope': True}}


def add(c):
    args = add_args(c)
    return c.s.conversations.add_project(args, c.owner), args


def goal(c, message):
    return c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (message['goal_id'],))


def create_other_work(c, message):
    return c.s.coordination.work_create({
        **c.scope, 'goal_id': message['goal_id'], 'target_project_id': 'otherproj',
        'objective': 'Inspect synthetic project', 'acceptance': 'Return synthetic evidence',
        'responsibility': 'fixture', 'assignee_grant_id': 'broad',
        'required_capabilities': ['read', 'write', 'execute'], 'idempotency_key': key()}, c.actor)['work_item']


def revoke(c, ref):
    return c.s.conversations.project_scopes.control({
        'conversation_id': c.room['id'], 'scope_id': ref['id'], 'expected_version': ref['version'],
        'idempotency_key': key()}, c.owner)


def test_preview_is_read_only_and_click_atomically_records_exact_scope(collab):
    c = fixture(collab)
    protected = {table: c.s.store.all('SELECT * FROM ' + table) for table in
                 ('grants', 'collaboration_dots', 'delegation_policies', 'mcp_event_subscriptions')}
    changes = c.s.store.one('SELECT total_changes() AS n')['n']
    found = preview(c)
    assert found['ready'] and found['changes_authority'] is False
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == changes
    scope = found['scope']
    assert scope['project_id'] == 'otherproj' and scope['dot_id'] == c.slot['id']
    assert scope['capabilities'] == ['execute', 'read', 'write']
    assert scope['execution_target'] == 'project_agent' and scope['reply_conversation_id'] == c.room['id']
    result, args = add(c)
    assert result['execution_authorized'] and result['added']
    assert result['conversation']['version'] == 2
    assert {p['project_id'] for p in result['conversation']['projects']} == {'proj', 'otherproj'}
    assert c.s.conversations.add_project(args, c.owner) == result
    assert len(c.s.store.all('SELECT * FROM conversation_project_scopes')) == 1
    for table, before in protected.items():
        assert c.s.store.all('SELECT * FROM ' + table) == before


def test_plain_membership_does_not_authorize_and_existing_membership_can_be_explicitly_approved(collab):
    c = fixture(collab)
    result = c.s.conversations.add_project({'project': 'otherproj', 'conversation_id': c.room['id'],
        'expected_version': 1, 'idempotency_key': key()}, c.owner)
    assert not result.get('execution_authorized')
    old, _ = send(c)
    assert json.loads(goal(c, old)['spec'])['project_ids'] == ['proj']
    with pytest.raises(DevError, match='不在原目标'):
        create_other_work(c, old)
    result, _ = add(c)
    assert result['execution_authorized'] and not result['added']
    assert result['conversation']['version'] == 3


def test_new_task_consumes_evidence_and_can_admit_only_its_own_project_work(collab):
    c = fixture(collab)
    old, _ = send(c)
    before = goal(c, old)
    old_snapshot = c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (old['goal_id'],))
    result, _ = add(c)
    new, _ = send(c)
    spec = json.loads(goal(c, new)['spec'])
    assert spec['project_ids'] == ['otherproj', 'proj']
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (new['goal_id'],))
    assert json.loads(link['policy_snapshot'])['project_scope_approvals'] == [result['authorization']]
    assert goal(c, old) == before
    assert c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (old['goal_id'],)) == old_snapshot
    with pytest.raises(DevError):
        create_other_work(c, old)
    item = create_other_work(c, new)
    claimed = c.s.coordination.work_claim({**c.scope, 'goal_id': new['goal_id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    admitted = c.s.coordination.admit({**c.scope, 'goal_id': new['goal_id'], 'work_item_id': item['id'],
        'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'],
        'tool': 'exec', 'arguments': {'command': 'printf synthetic-project-scope', 'target': 'agent'},
        'idempotency_key': key()}, c.actor)
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    assert operation['project_id'] == 'otherproj'
    assert operation['state'] == 'queued'  # No Agent, shell or external process runs in this fixture.
    with pytest.raises(DevError):
        c.s.coordination.admit({**c.scope, 'goal_id': new['goal_id'], 'work_item_id': item['id'],
            'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'], 'tool': 'exec',
            'arguments': {'command': 'printf wrong', 'target': 'vps:unapproved'}, 'idempotency_key': key()}, c.actor)


@pytest.mark.parametrize('change', [
    "UPDATE grants SET projects='[\"proj\"]' WHERE id='broad'",
    "UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'",
    "UPDATE projects SET root='/changed-fixture' WHERE id='otherproj'",
    "UPDATE projects SET device_id='changed-fixture' WHERE id='otherproj'",
    "UPDATE projects SET allow_tasks=0 WHERE id='otherproj'",
    "UPDATE delegation_policies SET version=version+1",
    "UPDATE collaboration_join_slots SET state='revoked' WHERE kind='dot'",
])
@pytest.mark.parametrize('stage', ['claim', 'admit'])
def test_live_changes_cannot_admit_next_step(collab, change, stage):
    c = fixture(collab)
    add(c)
    new, _ = send(c)
    item = create_other_work(c, new)
    claim_args = {**c.scope, 'goal_id': new['goal_id'], 'work_item_id': item['id'],
                  'expected_version': item['version'], 'idempotency_key': key()}
    if stage == 'admit':
        claimed = c.s.coordination.work_claim(claim_args, c.actor)['work_item']
    if "device_id='changed-fixture'" in change:
        c.s.store.execute("""INSERT INTO devices(id,name,secret,created)
            SELECT 'changed-fixture',name,secret,created FROM devices WHERE id='dev'""")
    c.s.store.execute(change)
    with pytest.raises(DevError):
        if stage == 'claim':
            c.s.coordination.work_claim(claim_args, c.actor)
        else:
            c.s.coordination.admit({**c.scope, 'goal_id': new['goal_id'], 'work_item_id': item['id'],
                'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'],
                'tool': 'exec', 'arguments': {'command': 'printf denied'}, 'idempotency_key': key()}, c.actor)
    assert not c.s.store.all('SELECT * FROM operations')


def test_revoke_blocks_new_steps_and_retry_does_not_restore_authority(collab):
    c = fixture(collab)
    old, _ = send(c)
    result, args = add(c)
    new, _ = send(c)
    create_other_work(c, new)
    revoke(c, result['authorization'])
    assert goal(c, old)['state'] == 'active'
    assert goal(c, new)['state'] == 'paused'
    with pytest.raises(DevError):
        c.s.conversations.add_project(args, c.owner)
    replacement, _ = add(c)
    assert replacement['authorization']['version'] == 2
    assert replacement['authorization']['id'] != result['authorization']['id']
    with pytest.raises(DevError):
        create_other_work(c, new)
    latest, _ = send(c)
    create_other_work(c, latest)


def test_expiry_is_not_extended_by_later_base_lifetime_change(collab):
    c = fixture(collab)
    c.s.store.execute('UPDATE delegation_policies SET expires_at=?', (c.clock[0] + 120,))
    result, args = add(c)
    task, _ = send(c)
    c.s.store.execute('UPDATE delegation_policies SET expires_at=?', (c.clock[0] + 1000,))
    c.clock[0] += 121
    with pytest.raises(DevError, match='到期'):
        create_other_work(c, task)
    with pytest.raises(DevError):
        c.s.conversations.add_project(args, c.owner)
    current, _ = send(c)
    assert json.loads(goal(c, current)['spec'])['project_ids'] == ['proj']


def test_changed_preview_rolls_back_membership_and_cannot_invent_target(collab):
    c = fixture(collab)
    args = add_args(c)
    args['authorization']['scope_digest'] = '0' * 64
    with pytest.raises(DevError, match='重新审阅'):
        c.s.conversations.add_project(args, c.owner)
    assert c.s.store.one('SELECT version FROM conversation_rooms WHERE id=?', (c.room['id'],))['version'] == 1
    assert not c.s.store.all('SELECT * FROM conversation_project_scopes')
    assert c.s.store.one('SELECT COUNT(*) AS n FROM conversation_projects WHERE conversation_id=?', (c.room['id'],))['n'] == 1
    c.s.store.execute("UPDATE grants SET projects='[\"proj\"]' WHERE id='broad'")
    assert not preview(c)['ready']
    assert not c.s.store.all('SELECT * FROM conversation_project_scopes')


def test_same_preview_concurrent_clicks_have_one_receipt_and_stale_loser(collab):
    c = fixture(collab)
    args = add_args(c)
    def attempt(value):
        try:
            return c.s.conversations.add_project(value, c.owner)
        except DevError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [args, {**args, 'idempotency_key': key()}]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'STALE_VERSION' in results
    assert len(c.s.store.all('SELECT * FROM conversation_project_scopes')) == 1


def test_duplicate_clicks_same_key_replay_one_approval_even_concurrently(collab):
    c = fixture(collab)
    args = add_args(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.s.conversations.add_project(args, c.owner), range(2)))
    assert results[0] == results[1]
    assert len(c.s.store.all('SELECT * FROM conversation_project_scopes')) == 1


def test_connector_cannot_self_approve_and_evidence_cannot_be_rewritten(collab):
    c = fixture(collab)
    args = add_args(c)
    with pytest.raises(DevError):
        c.s.conversations.add_project(args, c.actor)
    result = c.s.conversations.add_project(args, c.owner)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        c.s.store.execute("UPDATE conversation_project_scopes SET spec='{}'")
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        c.s.store.execute('UPDATE collaboration_dots SET policy_version=policy_version+1')
    assert result['execution_authorized']
