"""Temporary stores only: recoverable removal, fencing, isolation and owner/CSRF."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from hub.collaboration.common import MESSAGE_EVENT, canonical
from hub.collaboration.schema import migrate
from hub.principal import Principal
from shared.util import DevError
from tests.collaboration_support import collab as collab, collaboration_stack as collaboration_stack, key
from tests.test_collaboration_chatroom_projects import create, send, permit
from tests.test_collaboration_dots import setup, send as dot_send


def preview(s, owner, cid):
    return s.conversations.archive.preview({'conversation_id': cid}, owner)


def request(s, owner, cid):
    found = preview(s, owner, cid)
    return {'conversation_id': cid, 'expected_version': found['conversation']['version'],
            'impact_digest': found['impact_digest'], 'confirm_archive': True, 'idempotency_key': key()}


def archive(s, owner, cid):
    return s.conversations.archive.archive(request(s, owner, cid), owner)


def restore(s, owner, cid):
    room = s.conversations.object(owner, cid, include_archived=True)
    return s.conversations.archive.restore({'conversation_id': cid, 'expected_version': room['version'],
                                           'confirm_restore': True, 'idempotency_key': key()}, owner)


def seed_subscription(s, room, cid, identifier):
    s.store.execute("""INSERT INTO mcp_event_subscriptions
        (id,room_id,principal,grant_id,name,arguments,secret,expires_at,verified_until,key_digest,created,updated)
        VALUES (?,?,?,'worker','fixture',?,'synthetic-encrypted-placeholder',9999999999,9999999999,'fixture',1,1)""",
        (identifier, room['id'], '{}', canonical({'conversation_id': cid})))
    s.store.execute("""INSERT INTO mcp_event_outbox(id,room_id,name,object_id,object_version,data,created)
        VALUES (?,?,'fixture',?,1,'{}',1)""", ('event-' + identifier, room['id'], identifier))
    seq = s.store.one('SELECT seq FROM mcp_event_outbox WHERE id=?', ('event-' + identifier,))['seq']
    s.store.execute("""INSERT INTO mcp_event_deliveries(id,subscription_id,event_id,event_seq,state,lease_until,created)
        VALUES (?,?,?,?,'leased',9999999999,1)""", ('delivery-' + identifier, identifier, 'event-' + identifier, seq))


def test_preview_cancel_owner_and_digest_are_read_only(collab):
    s, owner, worker, _, room, *_ = collab
    before = s.store.one('SELECT total_changes() AS n')['n']
    found = preview(s, owner, room['id'])
    assert found['reversible'] and found['warnings'] and found['retained']
    assert found['impact']['exclusive_projects'] == 1
    assert s.store.one('SELECT total_changes() AS n')['n'] == before  # Dismiss/Cancel sends no mutation.
    args = request(s, owner, room['id'])
    with pytest.raises(DevError):
        s.conversations.archive.archive({**args, 'confirm_archive': False}, owner)
    with pytest.raises(DevError, match='管理员'):
        preview(s, worker, room['id'])
    other = Principal('panel:other', 'other', {'read', 'write'}, ['*'], admin=True)
    with pytest.raises(DevError):
        preview(s, other, room['id'])
    assert s.store.one('SELECT total_changes() AS n')['n'] == before
    send(collab, room, 'arrived after preview')
    with pytest.raises(DevError) as error:
        s.conversations.archive.archive(args, owner)
    assert error.value.code == 'CONVERSATION_ARCHIVE_PREVIEW_CHANGED'
    assert s.conversations.object(owner, room['id'])['archived_at'] is None


def test_default_archive_no_restart_resurrection_or_implicit_authority_restore(collab):
    s, owner, worker, _, room, agents, _, scope = collab
    message = send(collab, room, 'retained history')
    permit(collab, room['id'], room)
    seed_subscription(s, room, room['id'], 'default-sub')
    before_grants = s.store.all('SELECT * FROM grants ORDER BY id')
    args = request(s, owner, room['id'])
    result = s.conversations.archive.archive(args, owner)
    assert result['archived'] and result['can_restore']
    assert s.conversations.archive.archive(args, owner)['replayed']
    assert not s.conversations.listing(owner)['items']
    assert s.conversations.listing(owner, include_archived=True)['items'][0]['archived_at'] is not None
    assert not s.store.all('SELECT * FROM conversation_projects')
    assert s.store.one('SELECT state FROM collaboration_rooms WHERE id=?', (room['id'],))['state'] == 'paused'
    assert all(not a['enabled'] for a in s.store.all('SELECT * FROM collaboration_agents'))
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='default-sub'")['state'] == 'revoked'
    delivery = s.store.one("SELECT * FROM mcp_event_deliveries WHERE id='delivery-default-sub'")
    assert delivery['state'] == 'abandoned' and delivery['fence'] == 1 and delivery['lease_until'] is None
    with pytest.raises(DevError) as error:
        s.read({**scope, 'kind': 'timeline'}, owner)
    assert error.value.code == 'CONVERSATION_ARCHIVED'
    s.conversations.ensure_default(room)
    with s.store.transaction():
        migrate(s.store.db)
    assert not s.store.all('SELECT * FROM conversation_projects')
    restored = restore(s, owner, room['id'])
    assert restored['restored'] and restored['requires_reauthorization']
    assert restored['conversation']['restored_from_archive']
    assert [m['id'] for m in s.read({**scope, 'kind': 'timeline'}, owner)['items']] == [message['id']]
    assert not s.store.one('SELECT enabled FROM conversation_writers WHERE conversation_id=?', (room['id'],))['enabled']
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='default-sub'")['state'] == 'revoked'
    assert s.store.all('SELECT * FROM grants ORDER BY id') == before_grants
    assert s.conversations.archive.archive(args, owner)['conversation']['archived_at'] is None
    assert len(s.store.all('SELECT * FROM conversation_archives')) == 1
    with s.store.transaction():
        migrate(s.store.db)
    assert not s.store.one('SELECT enabled FROM conversation_writers WHERE conversation_id=?', (room['id'],))['enabled']


def test_shared_project_keeps_other_room_subscriptions_writers_and_credentials(collab):
    s, owner, worker, _, room, *_ = collab
    group = create(collab, 'proj')
    send(collab, room, 'private group', group['id'])
    permit(collab, group['id'], room)
    permit(collab, room['id'], room)
    seed_subscription(s, room, group['id'], 'group-sub')
    seed_subscription(s, room, room['id'], 'other-sub')
    unaffected = {table: s.store.all('SELECT * FROM ' + table + ' ORDER BY id') for table in ('grants', 'projects', 'collaboration_rooms', 'collaboration_agents')}
    found = preview(s, owner, group['id'])
    assert found['impact']['shared_projects'] == 1 and found['impact']['exclusive_projects'] == 0
    archive(s, owner, group['id'])
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='group-sub'")['state'] == 'revoked'
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='other-sub'")['state'] == 'active'
    assert s.store.one('SELECT enabled FROM conversation_writers WHERE conversation_id=?', (room['id'],))['enabled'] == 1
    for table, original in unaffected.items():
        assert s.store.all('SELECT * FROM ' + table + ' ORDER BY id') == original
    send(collab, room, 'other room still works')
    with pytest.raises(DevError):
        send(collab, room, 'stale room fails', group['id'])
    restore(s, owner, group['id'])
    with pytest.raises(DevError):
        send(collab, room, 'old writer cannot resume', group['id'], principal=worker)


def test_concurrent_archive_one_snapshot_and_receipt_per_request(collab):
    s, owner, _, _, room, *_ = collab
    args = request(s, owner, room['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: s.conversations.archive.archive(args, owner), range(2)))
    assert all(r['archived'] for r in results)
    assert len(s.store.all('SELECT * FROM conversation_archives')) == 1
    with pytest.raises(DevError) as error:
        s.conversations.archive.archive({**args, 'idempotency_key': key()}, owner)
    assert error.value.code == 'STALE_VERSION'
    with pytest.raises(DevError) as error:
        s.conversations.archive.archive({**args, 'expected_version': 9}, owner)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


def test_tasks_lease_policy_and_pending_operation_stay_revoked_after_restore(collab):
    c = setup(collab)
    sent, _ = dot_send(c)
    item = c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (sent['goal_id'],))
    item = c.s.coordination.work_claim({**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    lease = {**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
             'attempt': item['attempt'], 'fencing_token': item['fencing_token']}
    admitted = c.s.coordination.admit({**lease, 'tool': 'exec',
        'arguments': {'command': 'printf synthetic-only', 'target': 'agent'}, 'idempotency_key': key()}, c.actor)
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    assert operation['attempts'] == 0  # No real command runs.
    result = archive(c.s, c.owner, c.room['id'])
    assert result['operations'][0]['cancel_note'] == 'cancelled_before_delivery'
    work = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (item['id'],))
    assert work['state'] == 'cancelled' and work['lease_until'] is None and work['fencing_token'] > item['fencing_token']
    assert c.s.store.one('SELECT state FROM delegation_policies')['state'] == 'revoked'
    assert c.s.store.one('SELECT state FROM collaboration_join_slots WHERE id=?', (c.slot['id'],))['state'] == 'revoked'
    restore(c.s, c.owner, c.room['id'])
    with pytest.raises(DevError):
        c.s.coordination.admit({**lease, 'tool': 'exec', 'arguments': {'command': 'printf must-not-run'},
                               'idempotency_key': key()}, c.actor)
    with pytest.raises(DevError):
        c.s.joining.join({**c.join_raw, 'idempotency_key': key()}, c.actor)


def test_dependent_global_ceiling_blocks_archive_without_changing_global_authority(collab):
    from tests.test_dot_room_scope import setup as scope_setup
    s, owner, actor, room, args = scope_setup(collab)
    s.room_scope.set(args, owner)
    before = {table: s.store.all('SELECT * FROM ' + table) for table in
              ('room_scope_manifests', 'grants', 'conversation_rooms', 'conversation_projects')}
    found = preview(s, owner, room['id'])
    assert not found['ready'] and found['reason_code'] == 'CONVERSATION_ARCHIVE_CEILING_DEPENDENCY'
    assert found['dependent_connections'][0]['grant_id'] == 'broad'
    with pytest.raises(DevError) as error:
        archive(s, owner, room['id'])
    assert error.value.code == 'CONVERSATION_ARCHIVE_CEILING_DEPENDENCY'
    for table, rows in before.items():
        assert s.store.all('SELECT * FROM ' + table) == rows
    assert s.runtime._invoke('read', {'project': 'proj', 'path': 'README.md'}, actor).project['id'] == 'proj'


def test_http_archive_restore_csrf_and_explicit_confirmation(collaboration_stack):
    s = collaboration_stack
    room = s.must(s.client.post('/api/collaboration/room',
        json={'project': 'ProjectAlpha', 'idempotency_key': key()}))['room']
    cid = room['id']
    found = s.must(s.client.post('/api/collaboration/conversation-archive-preview', json={'conversation_id': cid}))
    args = {'conversation_id': cid, 'expected_version': found['conversation']['version'],
            'impact_digest': found['impact_digest'], 'confirm_archive': True, 'idempotency_key': key()}
    assert s.client.post('/api/collaboration/conversation-archive', json=args,
                         headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    assert s.client.post('/api/collaboration/conversation-archive', json={**args, 'confirm_archive': False}).status_code == 422
    result = s.must(s.client.post('/api/collaboration/conversation-archive', json=args))
    assert result['archived']
    assert s.must(s.client.get('/api/collaboration/conversations'))['items'] == []
    archived = s.must(s.client.get('/api/collaboration/conversations', params={'include_archived': 'true'}))['items'][0]
    assert archived['id'] == cid and archived['archived_at'] is not None
    restored = s.must(s.client.post('/api/collaboration/conversation-restore',
        json={'conversation_id': cid, 'expected_version': archived['version'], 'confirm_restore': True, 'idempotency_key': key()}))
    assert restored['restored'] and restored['requires_reauthorization']


def test_archived_default_proposals_cannot_schedule_on_a_shared_partition(collab):
    from tests.test_collaboration_service import command
    s, owner, worker, _, room, agents, _, scope = collab
    other = create(collab, 'proj')
    raw = {**scope, 'room_id': room['id'], 'request': 'Synthetic legacy proposal',
           'structured_mentions': [{'agent_id': agents[0]['id']}],
           'source_message_id': key(), 'idempotency_key': key()}
    proposal = s.command(raw, worker)
    _, running = command(collab)
    archive(s, owner, room['id'])
    assert s.store.one('SELECT state FROM collaboration_rooms WHERE id=?', (room['id'],))['state'] == 'active'
    assert s.store.one('SELECT state FROM collaboration_messages WHERE id=?', (proposal['id'],))['state'] == 'rejected'
    assert s.store.one('SELECT state FROM collaboration_goals WHERE id=?', (running['goal_id'],))['state'] == 'cancelled'
    with pytest.raises(DevError):
        s.command({**raw, 'source_message_id': key(), 'idempotency_key': key()}, worker)
    with pytest.raises(DevError):
        s.control({**scope, 'target_id': proposal['id'], 'action': 'accept_proposal',
                   'expected_version': 2, 'idempotency_key': key()}, owner)
    message = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (proposal['id'],))
    with pytest.raises(DevError) as error:
        s.schedule_message(room, message, owner)
    assert error.value.code == 'CONVERSATION_ARCHIVED'
    with pytest.raises(DevError):
        s.make_job(room, agent_id=agents[0]['id'], kind='propose_monitor_plan',
                   business_key='cannot-escape-archive', context={'origin_message_id': proposal['id']})
    assert not s.store.one("SELECT 1 FROM collaboration_jobs WHERE business_key='cannot-escape-archive'")
    send(collab, room, 'unaffected shared conversation', other['id'])
    restore(s, owner, room['id'])
    with pytest.raises(DevError):
        s.control({**scope, 'target_id': proposal['id'], 'action': 'accept_proposal',
                   'expected_version': 2, 'idempotency_key': key()}, owner)


def test_in_flight_operations_report_uncertainty_and_do_not_replay(collab):
    c = setup(collab)
    sent, _ = dot_send(c)
    item = c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (sent['goal_id'],))
    item = c.s.coordination.work_claim({**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()}, c.actor)['work_item']
    admitted = c.s.coordination.admit({**c.scope, 'goal_id': sent['goal_id'], 'work_item_id': item['id'],
        'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'tool': 'exec',
        'arguments': {'command': 'printf synthetic-only', 'target': 'agent'}, 'idempotency_key': key()}, c.actor)
    # Synthetic receipt only, no delivery or real command.
    c.s.store.execute("UPDATE operations SET state='running',attempts=1 WHERE id=?", (admitted['operation_id'],))
    result = archive(c.s, c.owner, c.room['id'])
    operation = next(o for o in result['operations'] if o['id'] == admitted['operation_id'])
    assert operation['cancel_requested'] == 1 and operation['state'] == 'cancelling'
    assert operation['cancel_note'] == 'cancel_requested_in_flight_not_proven_stopped'
    assert any('不能保证' in warning for warning in result['warnings'])


def test_receipt_replays_report_current_state_without_repeating_old_actions(collab):
    s, owner, _, _, room, *_ = collab
    delete_args = request(s, owner, room['id'])
    deleted = s.conversations.archive.archive(delete_args, owner)
    restore_args = {'conversation_id': room['id'], 'expected_version': deleted['conversation']['version'],
                    'confirm_restore': True, 'idempotency_key': key()}
    recovered = s.conversations.archive.restore(restore_args, owner)
    old_delete = s.conversations.archive.archive(delete_args, owner)
    assert old_delete['replayed'] and not old_delete['archived']
    assert old_delete['conversation']['version'] == recovered['conversation']['version']
    deleted_again = archive(s, owner, room['id'])
    old_restore = s.conversations.archive.restore(restore_args, owner)
    assert old_restore['replayed'] and not old_restore['restored']
    assert old_restore['conversation']['archived_at'] is not None
    assert old_restore['conversation']['version'] == deleted_again['conversation']['version']
    assert len(s.store.all('SELECT * FROM conversation_archives')) == 2


def insert_row(s, table, values):
    # Synthetic, isolated ledgers: omitted required metadata gets inert values.
    for column in s.store.all('PRAGMA table_info(' + table + ')'):
        if column['name'] not in values and column['notnull'] and column['dflt_value'] is None:
            values[column['name']] = '' if column['type'] == 'TEXT' else 0
    s.store.execute('INSERT INTO ' + table + '(' + ','.join(values) + ') VALUES (' +
                    ','.join('?' for _ in values) + ')', tuple(values.values()))


def test_shared_channel_account_keeps_other_binding_and_fences_uncertain_sends(collab):
    s, owner, _, _, room, *_ = collab
    group = create(collab, 'proj')
    insert_row(s, 'telegram_accounts', {'id': 'account', 'bot_id': 'fixture-bot', 'space_id': owner.space_id,
        'owner_user_id': owner.user_id, 'secret': s.store.encrypt('synthetic-only'), 'state': 'active'})
    for bid, cid in [('owned', group['id']), ('other', room['id'])]:
        insert_row(s, 'telegram_bindings', {'id': bid, 'account_id': 'account', 'space_id': owner.space_id,
            'owner_user_id': owner.user_id, 'room_id': room['id'], 'conversation_id': cid, 'project_id': 'proj',
            'state': 'active', 'user_id': bid, 'chat_id': bid})
        insert_row(s, 'channel_mirrors', {'id': bid, 'channel': 'telegram', 'binding_id': bid,
            'room_id': room['id'], 'conversation_id': cid, 'message_id': bid, 'state': 'sending'})
        insert_row(s, 'channel_mirror_parts', {'id': bid, 'delivery_id': bid, 'state': 'sending', 'lease_fence': 3})
    account = s.store.one("SELECT * FROM telegram_accounts WHERE id='account'")
    other = s.store.one("SELECT * FROM telegram_bindings WHERE id='other'")
    archive(s, owner, group['id'])
    assert s.store.one("SELECT * FROM telegram_accounts WHERE id='account'") == account
    assert s.store.one("SELECT * FROM telegram_bindings WHERE id='other'") == other
    assert s.store.one("SELECT state FROM telegram_bindings WHERE id='owned'")['state'] == 'revoked'
    part = s.store.one("SELECT * FROM channel_mirror_parts WHERE id='owned'")
    assert part['state'] == 'unknown' and part['lease_fence'] == 4 and part['lease_until'] == 0
    assert s.store.one("SELECT state FROM channel_mirror_parts WHERE id='other'")['state'] == 'sending'
    restore(s, owner, group['id'])
    assert s.store.one("SELECT state FROM telegram_bindings WHERE id='owned'")['state'] == 'revoked'


def test_implicit_default_message_subscription_does_not_reactivate_on_restore(collab):
    s, owner, _, _, room, *_ = collab
    create(collab, 'proj')
    seed_subscription(s, room, room['id'], 'implicit-sub')
    s.store.execute("UPDATE mcp_event_subscriptions SET name=?,arguments='{}' WHERE id='implicit-sub'", (MESSAGE_EVENT,))
    archive(s, owner, room['id'])
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='implicit-sub'")['state'] == 'revoked'
    restore(s, owner, room['id'])
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='implicit-sub'")['state'] == 'revoked'


def test_shared_legacy_slot_only_loses_current_policy_and_subscription(collab):
    s, owner, _, _, room, *_ = collab
    group = create(collab, 'proj')
    insert_row(s, 'collaboration_join_slots', {'id': 'shared-slot', 'room_id': room['id'], 'label': 'shared',
        'kind': 'dot', 'state': 'registered', 'join_code': 'synthetic-shared-code', 'grant_id': 'dot'})
    for pid, cid in [('group-policy', group['id']), ('other-policy', room['id'])]:
        insert_row(s, 'delegation_policies', {'id': pid, 'room_id': room['id'], 'conversation_id': cid,
            'slot_id': 'shared-slot', 'grant_id': 'dot', 'state': 'active', 'spec': '{}', 'principal': '{}',
            'grant_snapshot': '{}', 'digest': 'synthetic-digest'})
        seed_subscription(s, room, cid, pid)
        s.store.execute('UPDATE mcp_event_subscriptions SET arguments=? WHERE id=?',
                        (canonical({'policy_id': pid}), pid))
        s.store.execute('INSERT INTO collaboration_join_routes(subscription_id,slot_id,endpoint_digest,credential_digest,created) VALUES (?,?,?,?,?)',
                        (pid, 'shared-slot', 'fixture', 'fixture', 1))
    original = s.store.one("SELECT * FROM collaboration_join_slots WHERE id='shared-slot'")
    archive(s, owner, group['id'])
    assert s.store.one("SELECT * FROM collaboration_join_slots WHERE id='shared-slot'") == original
    assert s.store.one("SELECT state FROM delegation_policies WHERE id='group-policy'")['state'] == 'revoked'
    assert s.store.one("SELECT state FROM delegation_policies WHERE id='other-policy'")['state'] == 'active'
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='group-policy'")['state'] == 'revoked'
    assert s.store.one("SELECT state FROM mcp_event_subscriptions WHERE id='other-policy'")['state'] == 'active'


def test_archive_failure_rolls_back_snapshot_links_and_authority(collab, monkeypatch):
    s, owner, _, _, room, *_ = collab
    group = create(collab, 'proj')
    args = request(s, owner, group['id'])
    before = {table: s.store.all('SELECT * FROM ' + table) for table in
              ('conversation_rooms', 'conversation_projects', 'conversation_archives', 'conversation_writers', 'grants')}
    original = s.conversations.archive.stop
    def fail(*arguments):
        original(*arguments)
        raise RuntimeError('synthetic failure before commit')
    monkeypatch.setattr(s.conversations.archive, 'stop', fail)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        s.conversations.archive.archive(args, owner)
    for table, rows in before.items():
        assert s.store.all('SELECT * FROM ' + table) == rows
    assert not s.store.all("SELECT * FROM conversation_archives")


def test_restore_exposes_visible_history_projects_without_restoring_work_scope(collab):
    from tests.test_conversation_project_revision import fixture
    from tests.test_room_project_permissions import save
    c = fixture(collab)
    save(c, [('proj', ['read']), ('otherproj', ['read'])])
    old = c.s.conversations.view(c.owner, c.s.conversations.object(c.owner, c.room['id']))
    assert {p['project_id'] for p in old['projects']} == {'proj', 'otherproj'}
    assert 'history_projects' not in old
    archive(c.s, c.owner, c.room['id'])
    restored = restore(c.s, c.owner, c.room['id'])['conversation']
    assert restored['restored_from_archive']
    assert restored['projects'] == []
    assert restored['working_scope']['project_ids'] == []
    assert restored['project_authorizations'] == []
    assert {p['project_id'] for p in restored['history_projects']} == {'proj', 'otherproj'}
    assert all(p['execution_authorized'] is False and p['capabilities'] == [] and
               p['execution_targets'] == [] for p in restored['history_projects'])
    current = c.s.conversations.working_scopes.current(c.room['id'])
    assert current['spec']['projects'] == []
    listed = next(item for item in c.s.conversations.listing(c.owner)['items'] if item['id'] == c.room['id'])
    assert listed['history_projects'] == restored['history_projects']
    # A narrower current reader sees only the historical membership it can read.
    limited = c.s.conversations.view(collab[2], c.s.conversations.object(collab[2], c.room['id']))
    assert [p['project_id'] for p in limited['history_projects']] == ['proj']
    assert limited['projects'] == [] and limited['working_scope']['project_ids'] == []
