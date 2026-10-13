"""Independent adversarial coverage for versioned room working projects.

Every database, credential, provider and operation here is synthetic. No Agent,
real subscription endpoint, shell process, production data or external send runs.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.events import EventService
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, inbox, owner_message, send_dot
from tests.test_collaboration_dots import call, send
from tests.test_collaboration_events import Receiver
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet


def distinct_roots(store_fixture):
    store_fixture[0].store.execute("UPDATE projects SET root='/tmp/revision-fixture/' || id")


def fixture(store_fixture):
    distinct_roots(store_fixture)
    c = duplex(store_fixture)
    c.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'",
                      (canonical(['proj', 'otherproj']),))
    return c


def service(c):
    return c.s.conversations.working_scopes


def preview(c, projects, **extra):
    return service(c).preview({
        'conversation_id': c.slot['conversation_id'], 'dot_id': c.slot['id'],
        'projects': [{'project': project, 'environment_id': 'production'} for project in projects],
        **extra}, c.owner)


def apply_args(c, projects):
    found = preview(c, projects)
    assert found['ready'], found
    return {
        'conversation_id': c.slot['conversation_id'], 'dot_id': c.slot['id'],
        'projects': [{'project': project, 'environment_id': 'production'} for project in projects],
        'expected_version': found['conversation_version'],
        'expected_scope_version': found['scope_version'],
        'selection_digest': found['selection_digest'], 'confirm_scope': True,
        'idempotency_key': key(),
    }


def select(c, projects):
    args = apply_args(c, projects)
    return service(c).apply(args, c.owner), args


def goal(c, task):
    return c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (task['goal_id'],))


def task(c):
    created, _ = send(c)
    return created


def initial_work(c, task):
    return c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=? ORDER BY created,id',
                         (task['goal_id'],))


def work(c, task, project):
    return c.s.coordination.work_create({
        **c.scope, 'goal_id': task['goal_id'], 'target_project_id': project,
        'objective': 'Inspect synthetic fixture', 'acceptance': 'Return synthetic evidence',
        'responsibility': 'regression', 'assignee_grant_id': 'broad',
        'required_capabilities': ['read', 'write', 'execute'], 'idempotency_key': key(),
    }, c.actor)['work_item']


def claim(c, task, item):
    return c.s.coordination.work_claim({
        **c.scope, 'goal_id': task['goal_id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key(),
    }, c.actor)['work_item']


def admit(c, task, claimed):
    return c.s.coordination.admit({
        **c.scope, 'goal_id': task['goal_id'], 'work_item_id': claimed['id'],
        'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'],
        'tool': 'exec', 'arguments': {'command': 'printf synthetic-only', 'target': 'agent'},
        'idempotency_key': key(),
    }, c.actor)


def protected(c):
    return {name: c.s.store.all('SELECT * FROM ' + name + ' ORDER BY rowid') for name in (
        'grants', 'collaboration_dots', 'collaboration_join_slots', 'delegation_policies',
        'collaboration_join_routes', 'mcp_event_subscriptions',
        'room_scope_manifests', 'weixin_accounts', 'weixin_bindings',
    )}


def revision_count(c):
    return c.s.store.one('SELECT COUNT(*) AS n FROM conversation_working_scopes')['n']


def test_preview_is_read_only_and_does_not_grant_or_join_projects(collab):
    c = fixture(collab)
    before = protected(c)
    changes = c.s.store.one('SELECT total_changes() AS n')['n']
    found = preview(c, ['otherproj'])
    assert found['ready']
    assert found['scope_version'] == 0
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == changes
    assert protected(c) == before
    assert revision_count(c) == 0
    assert c.s.store.one('SELECT COUNT(*) AS n FROM conversation_projects WHERE conversation_id=?',
                         (c.slot['conversation_id'],))['n'] == 1


def test_remove_anchor_preserves_original_dot_connection_thread_and_grant(collab):
    c = fixture(collab)
    source, _ = owner_message(c, 'Keep this conversation and its replies.')
    baseline = protected(c)
    old_connection = c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)
    result, _ = select(c, ['otherproj'])
    assert result['applied']
    assert {p['project_id'] for p in result['conversation']['projects']} == {'otherproj'}
    assert protected(c) == baseline
    assert c.s.store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                         (c.slot['conversation_id'], c.room['id']))
    connection = c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)
    assert connection['dot_id'] == old_connection['dot_id']
    assert connection['inbox_request'] == old_connection['inbox_request']
    assert connection['subscription_requests'] == old_connection['subscription_requests']
    assert connection['subscription_request']['arguments']['project_id'] == 'proj'
    assert inbox(c)['items'][0]['message_id'] == source['message']['id']
    reply = send_dot(c, 'Original thread is still available.', source['message']['id'])
    assert reply['message']['reply_to_id'] == source['message']['id']
    followup, _ = owner_message(c, 'Continue.', mentions=[], reply_to_id=reply['message']['id'])
    assert inbox(c)['items'][0]['message_id'] == followup['message']['id']


def test_new_goal_and_kickoff_exclude_removed_anchor(collab):
    c = fixture(collab)
    select(c, ['otherproj'])
    new = task(c)
    spec = json.loads(goal(c, new)['spec'])
    assert spec['project_ids'] == ['otherproj']
    assert spec['room_project_scope']
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (new['goal_id'],))
    assert json.loads(link['policy_snapshot'])['request_scope']['room_project_scope'] == spec['room_project_scope']
    kickoff = initial_work(c, new)
    assert kickoff['project_id'] == 'otherproj'
    with pytest.raises(DevError):
        work(c, new, 'proj')
    lease = claim(c, new, kickoff)
    result = admit(c, new, lease)
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (result['operation_id'],))
    assert operation['project_id'] == 'otherproj'
    assert operation['state'] == 'queued'


@pytest.mark.parametrize('legacy', [True, False])
def test_addition_never_expands_or_fences_old_task(collab, legacy):
    c = fixture(collab)
    if not legacy:
        select(c, ['proj'])
    old = task(c)
    old_goal = goal(c, old)
    old_link = c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (old['goal_id'],))
    old_work = initial_work(c, old)
    select(c, ['proj', 'otherproj'])
    assert goal(c, old) == old_goal
    assert c.s.store.one('SELECT * FROM delegation_requests WHERE goal_id=?', (old['goal_id'],)) == old_link
    with pytest.raises(DevError):
        work(c, old, 'otherproj')
    lease = claim(c, old, old_work)
    operation = admit(c, old, lease)
    assert c.s.store.one('SELECT project_id FROM operations WHERE id=?',
                         (operation['operation_id'],))['project_id'] == 'proj'
    new = task(c)
    assert set(json.loads(goal(c, new)['spec'])['project_ids']) == {'proj', 'otherproj'}


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('stage', ['claim', 'admit'])
def test_removal_fences_original_steps_and_readdition_cannot_restore_them(collab, legacy, stage):
    c = fixture(collab)
    if not legacy:
        select(c, ['proj'])
    old = task(c)
    item = initial_work(c, old)
    lease = claim(c, old, item) if stage == 'admit' else None
    select(c, ['otherproj'])
    assert goal(c, old)['state'] != 'active'
    select(c, ['proj', 'otherproj'])
    with pytest.raises(DevError):
        if stage == 'claim':
            claim(c, old, item)
        else:
            admit(c, old, lease)
    with pytest.raises(DevError):
        work(c, old, 'otherproj')
    assert not c.s.store.all('SELECT * FROM operations')
    new = task(c)
    assert set(json.loads(goal(c, new)['spec'])['project_ids']) == {'proj', 'otherproj'}


def test_removal_cancels_queued_not_delivered_operation_without_touching_route(collab):
    c = fixture(collab)
    select(c, ['proj'])
    old = task(c)
    lease = claim(c, old, initial_work(c, old))
    admitted = admit(c, old, lease)
    assert c.s.store.one('SELECT attempts FROM operations WHERE id=?',
                         (admitted['operation_id'],))['attempts'] == 0
    select(c, [])
    op = c.s.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    assert op['cancel_requested']
    assert op['state'] not in {'queued', 'running'}
    assert c.s.dots.connection({**c.scope, 'dot_id': c.slot['id']}, c.actor)['dot_id'] == c.slot['id']


def test_last_project_removed_is_visible_chat_only_and_cached_task_request_denied(collab):
    c = fixture(collab)
    source, _ = owner_message(c, 'Please inspect a project when allowed.')
    cached_request = inbox(c)['items'][0]['task_request']
    assert cached_request is not None
    result, _ = select(c, [])
    assert result['conversation']['projects'] == []
    listed = c.s.conversations.listing(c.owner)
    assert any(row['id'] == c.slot['conversation_id'] for row in listed['items'])
    page = inbox(c)
    assert page['items'][0]['message_id'] == source['message']['id']
    assert page['items'][0]['task_request'] is None
    with pytest.raises(DevError):
        call(c, cached_request)
    reply = send_dot(c, 'We can continue discussing here.', source['message']['id'])
    assert reply['message']['reply_to_id'] == source['message']['id']
    assert not c.s.store.all('SELECT * FROM delegation_requests')
    assert not c.s.store.all('SELECT * FROM operations')


def test_preview_digest_version_and_membership_are_atomic(collab):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    before = protected(c)
    for changes in (
        {'selection_digest': '0' * 64},
        {'expected_version': args['expected_version'] + 1},
        {'expected_scope_version': args['expected_scope_version'] + 1},
        {'confirm_scope': False},
    ):
        with pytest.raises(DevError):
            service(c).apply({**args, **changes, 'idempotency_key': key()}, c.owner)
        assert revision_count(c) == 0
        assert protected(c) == before


def test_project_change_after_preview_rolls_back_revision(collab):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    c.s.store.execute("UPDATE projects SET root='/tmp/moved-synthetic' WHERE id='otherproj'")
    with pytest.raises(DevError):
        service(c).apply(args, c.owner)
    assert revision_count(c) == 0
    assert c.s.conversations.view(c.owner, c.s.conversations.object(c.owner, c.room['id']))['projects'][0]['project_id'] == 'proj'


@pytest.mark.parametrize('change', [
    "UPDATE grants SET projects='[\"proj\"]' WHERE id='broad'",
    "UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'",
    "UPDATE grants SET revoked=1 WHERE id='broad'",
    "UPDATE projects SET allow_tasks=0 WHERE id='otherproj'",
])
def test_current_grant_and_project_denials_cannot_be_overridden_by_preview(collab, change):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    c.s.store.execute(change)
    before = c.s.store.all('SELECT * FROM grants ORDER BY id')
    with pytest.raises(DevError):
        service(c).apply(args, c.owner)
    assert revision_count(c) == 0
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == before


def test_new_working_scope_cannot_expand_connection_wide_room_ceiling(collab):
    c = fixture(collab)
    c.s.room_scope.set({
        **c.scope, 'conversation_id': c.slot['conversation_id'], 'grant_id': 'broad',
        'expected_version': 0, 'project_rules': [
            {'project_id': 'proj', 'capabilities': ['read', 'write', 'execute']}],
        'execution_mode': 'account_permissions_acknowledged',
        'acknowledge_unsandboxed_exec': True, 'confirm_connection_wide_ceiling': True,
        'idempotency_key': key(),
    }, c.owner)
    before = protected(c)
    try:
        found = preview(c, ['proj', 'otherproj'])
    except DevError:
        found = {'ready': False}
    assert not found['ready']
    assert protected(c) == before
    assert revision_count(c) == 0
    with pytest.raises(DevError):
        c.s.runtime.project('otherproj', c.actor)


def test_connector_cannot_author_its_own_room_project_revision(collab):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    with pytest.raises(DevError):
        service(c).apply(args, c.actor)
    assert revision_count(c) == 0


def test_concurrent_distinct_requests_use_compare_and_swap(collab):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    def attempt(raw):
        try:
            return service(c).apply(raw, c.owner)
        except DevError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [args, {**args, 'idempotency_key': key()}]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'STALE_VERSION' in results
    assert revision_count(c) == 1


def test_concurrent_same_key_has_one_revision_and_replay_cannot_undo_newer_choice(collab):
    c = fixture(collab)
    args = apply_args(c, ['otherproj'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service(c).apply(args, c.owner), range(2)))
    assert results[0] == results[1]
    assert revision_count(c) == 1
    select(c, [])
    count = revision_count(c)
    service(c).apply(args, c.owner)
    assert revision_count(c) == count
    assert c.s.conversations.view(c.owner, c.s.conversations.object(c.owner, c.room['id']))['projects'] == []


def test_same_key_different_selection_is_a_conflict(collab):
    c = fixture(collab)
    _, args = select(c, ['otherproj'])
    with pytest.raises(DevError) as error:
        service(c).apply({**args, 'projects': []}, c.owner)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    assert revision_count(c) == 1


def test_revision_evidence_cannot_be_rewritten(collab):
    c = fixture(collab)
    select(c, ['otherproj'])
    with pytest.raises(sqlite3.IntegrityError):
        c.s.store.execute("UPDATE conversation_working_scopes SET spec='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        c.s.store.execute("UPDATE conversation_working_scopes SET digest=?", ('0' * 64,))


@pytest.mark.asyncio
async def test_original_message_subscription_survives_anchor_and_last_project_removal(collab):
    c = fixture(collab)
    c.s.config = replace(c.s.config, events_enabled=True)
    receiver = Receiver()
    c.s.events = events = EventService(c.s, receiver)
    await events.subscribe({
        **c.connected['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://revision.example.invalid/events',
                     'secret': 'whsec_' + base64.b64encode(b'synthetic-only-test-secret-32byte!').decode()},
    }, c.actor)
    for selected in (['otherproj'], []):
        before = protected(c)
        select(c, selected)
        assert protected(c) == before
        source, _ = owner_message(c, 'Synthetic message after scope revision')
        await events.tick()
        delivered = json.loads(receiver.requests[-1][1])
        assert delivered['data']['message_id'] == source['message']['id']
        send_dot(c, 'Same dot, same message route.', source['message']['id'])


def test_weixin_binding_and_pending_reply_survive_chat_only_transition(collab):
    distinct_roots(collab)
    c, w, account, binding, scope = fixture_channel(collab)
    c.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'",
                      (canonical(['proj', 'otherproj']),))
    source = ingest(c, w, account, packet(2, 'Synthetic chat only'))['message_ids'][0]
    posted = c.s.invoke('collaboration', {
        'action': 'dot_message_v2', **c.scope, 'dot_id': c.slot['id'],
        'body_text': 'Synthetic reply queued, never sent.', 'reply_to_id': source,
        'idempotency_key': key(),
    }, c.actor)
    delivery = c.s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',
                             (posted['message']['id'],))
    assert delivery and delivery['state'] == 'queued'
    before = protected(c)
    parts = c.s.store.all('SELECT * FROM weixin_parts ORDER BY ordinal')
    select(c, [])
    assert protected(c) == before
    assert c.s.store.all('SELECT * FROM weixin_parts ORDER BY ordinal') == parts
    assert w.delivery.validate(delivery)[0]['id'] == binding['binding_id']
    next_source = ingest(c, w, account, packet(3, 'Still connected'))['message_ids'][0]
    c.s.invoke('collaboration', {
        'action': 'dot_message_v2', **c.scope, 'dot_id': c.slot['id'],
        'body_text': 'Second synthetic reply.', 'reply_to_id': next_source,
        'idempotency_key': key(),
    }, c.actor)
    assert len(c.s.store.all('SELECT * FROM weixin_deliveries')) == 2
    assert {r['state'] for r in c.s.store.all('SELECT * FROM weixin_parts')} == {'queued'}


@pytest.mark.parametrize('change', [
    "UPDATE grants SET projects='[\"proj\"]' WHERE id='broad'",
    "UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'",
    "UPDATE projects SET root='/tmp/changed-live-mapping' WHERE id='otherproj'",
    "UPDATE projects SET mode='read' WHERE id='otherproj'",
    "UPDATE projects SET allow_tasks=0 WHERE id='otherproj'",
    "UPDATE delegation_policies SET version=version+1",
    "UPDATE collaboration_join_slots SET state='revoked' WHERE kind='dot'",
])
def test_each_new_step_revalidates_live_grant_mapping_policy_and_dot(collab, change):
    c = fixture(collab)
    select(c, ['otherproj'])
    new = task(c)
    lease = claim(c, new, initial_work(c, new))
    c.s.store.execute(change)
    with pytest.raises(DevError):
        admit(c, new, lease)
    assert not c.s.store.all('SELECT * FROM operations')


def test_overlapping_parent_root_cannot_hide_a_removed_project(collab):
    c = fixture(collab)
    c.s.store.execute("UPDATE projects SET root='/tmp/revision-fixture' WHERE id='otherproj'")
    found = preview(c, ['otherproj'])
    assert not found['ready']
    assert found['reason_code'] == 'ROOM_PROJECT_ROOT_OVERLAP'
    assert revision_count(c) == 0


def test_working_revision_cannot_move_connection_to_other_dot(collab):
    c = fixture(collab)
    select(c, ['proj'])
    other = duplex(collab)
    found = preview(c, ['proj'], dot_id=other.slot['id'])
    assert not found['ready']
    assert found['reason_code'] == 'ROOM_PROJECT_CONNECTION_CHANGED'
    assert revision_count(c) == 1


@pytest.mark.parametrize('inactive', ['expired', 'paused'])
@pytest.mark.parametrize('remaining', [[], ['otherproj']])
def test_owner_can_reduce_existing_scope_when_task_policy_is_inactive(collab, inactive, remaining):
    c = fixture(collab)
    select(c, ['proj', 'otherproj'])
    if inactive == 'expired':
        c.s.store.execute('UPDATE delegation_policies SET expires_at=?', (c.clock[0] - 1,))
    else:
        c.s.delegation.control({
            **c.scope, 'policy_id': c.slot['policy_id'], 'expected_version': 1,
            'action': 'pause', 'idempotency_key': key(),
        }, c.owner)
    before = protected(c)
    result, _ = select(c, remaining)
    assert {p['project_id'] for p in result['conversation']['projects']} == set(remaining)
    assert protected(c) == before
    source, _ = owner_message(c, 'Only discuss; execution is inactive.')
    page = inbox(c)
    assert page['items'][0]['task_request'] is None
    reply = send_dot(c, 'We can still discuss.', source['message']['id'])
    assert reply['message']['reply_to_id'] == source['message']['id']
    assert not c.s.store.all('SELECT * FROM operations')


@pytest.mark.parametrize('extra', [
    {'room_project_scope': {'version': 999, 'digest': '0' * 64}},
    {'grant_id': 'different-connection'},
    {'capabilities': ['read', 'write', 'execute', 'computer']},
    {'spec': {'projects': ['otherproj']}},
])
def test_selection_cannot_inject_unknown_authority_fields(collab, extra):
    c = fixture(collab)
    raw = {'conversation_id': c.slot['conversation_id'], 'dot_id': c.slot['id'],
           'projects': [{'project': 'proj', 'environment_id': 'production'}]}
    with pytest.raises(DevError):
        service(c).preview({**raw, **extra}, c.owner)
    args = apply_args(c, ['proj'])
    with pytest.raises(DevError):
        service(c).apply({**args, **extra}, c.owner)
    assert revision_count(c) == 0


def test_legacy_add_and_authorize_api_cannot_expand_versioned_active_scope(collab):
    from tests.test_conversation_project_scopes import add as legacy_add
    c = fixture(collab)
    select(c, ['proj'])
    before = service(c).current(c.slot['conversation_id'])
    added, _ = legacy_add(c)
    assert added['execution_authorized']
    assert service(c).current(c.slot['conversation_id']) == before
    assert [p['project_id'] for p in added['conversation']['projects']] == ['proj']
    created = task(c)
    assert json.loads(goal(c, created)['spec'])['project_ids'] == ['proj']
    with pytest.raises(DevError):
        work(c, created, 'otherproj')


def test_repeated_migration_keeps_chat_only_revision_and_historical_partition(collab):
    from hub.collaboration.schema import migrate
    c = fixture(collab)
    select(c, [])
    before = service(c).current(c.slot['conversation_id'])
    with c.s.store.transaction():
        migrate(c.s.store.db)
    assert service(c).current(c.slot['conversation_id']) == before
    assert c.s.conversations.view(c.owner, c.s.conversations.object(c.owner, c.room['id']))['projects'] == []
    assert c.s.store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                         (c.slot['conversation_id'], c.room['id']))
    source, _ = owner_message(c, 'Conversation after repeated migration.')
    assert inbox(c)['items'][0]['task_request'] is None
    send_dot(c, 'The same dot can still reply.', source['message']['id'])


@pytest.mark.parametrize('existing_default', [False, True])
def test_selected_partition_does_not_create_or_merge_independent_conversations(collab, existing_default):
    from hub.collaboration.schema import migrate
    c = fixture(collab)
    if existing_default:
        c.s.room_create({'project': 'otherproj', 'environment_id': 'production',
                         'idempotency_key': key()}, c.owner)
    else:
        assert not c.s.store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproj'")
    original_ids = {row['id'] for row in c.s.store.all('SELECT id FROM conversation_rooms')}
    select(c, ['otherproj'])
    assert {row['id'] for row in c.s.store.all('SELECT id FROM conversation_rooms')} == original_ids
    assert c.s.store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproj'")
    with c.s.store.transaction():
        migrate(c.s.store.db)
    assert {row['id'] for row in c.s.store.all('SELECT id FROM conversation_rooms')} == original_ids
    assert {row['id'] for row in c.s.conversations.listing(c.owner)['items']} == original_ids
