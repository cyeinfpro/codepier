"""Focused audit regressions using synthetic Stores and SQLite query plans."""
import sqlite3

import pytest

from hub.collaboration.projection_cache import memoized, projection
from hub.collaboration.relay_hints import migrate as migrate_hints
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


@pytest.mark.parametrize('sql,params,index', [
    ('SELECT id FROM collaboration_messages WHERE reply_to_id IN (?) ORDER BY created', ('x',), 'collaboration_messages_reply'),
    ('SELECT sequence FROM collaboration_dot_messages WHERE message_id=?', ('x',), 'dot_messages_message'),
    ('SELECT id FROM weixin_deliveries WHERE message_id=?', ('x',), 'weixin_deliveries_message'),
    ('SELECT id FROM channel_mirrors WHERE message_id=?', ('x',), 'channel_mirrors_message'),
    ('SELECT id FROM channel_mirrors WHERE binding_id=? AND created>?', ('x', 0), 'channel_mirrors_binding_created'),
    ('SELECT id FROM delegation_requests WHERE work_item_id=?', ('x',), 'delegation_requests_work'),
    ('SELECT id FROM coordination_messages WHERE goal_id=?', ('x',), 'coordination_messages_goal'),
    ('SELECT id FROM monitor_evidence WHERE expires_at<?', (0,), 'monitor_evidence_expiry'),
    ('SELECT update_id FROM telegram_inbox WHERE account_id=? AND created>?', ('x', 0), 'telegram_inbox_account_created'),
    ('SELECT id FROM collaboration_jobs WHERE assignee_agent_id IN (?)', ('x',), 'collaboration_jobs_assignee'),
    ('SELECT id FROM monitor_samples WHERE collected_at<?', (0,), 'monitor_samples_collected'),
])
def test_history_queries_use_indexes(collab, sql, params, index):
    plan = collab[0].store.all('EXPLAIN QUERY PLAN ' + sql, params)
    assert any(index in row['detail'] for row in plan), plan


def test_relay_output_and_ordinary_operations_do_not_invalidate_rooms():
    db = sqlite3.connect(':memory:')
    db.executescript("""
        CREATE TABLE operations(id TEXT PRIMARY KEY, project_id TEXT,state TEXT,output_seq INTEGER,last_operation_verified_at REAL);
        CREATE TABLE coordination_operations(operation_id TEXT PRIMARY KEY,goal_id TEXT);
        CREATE TABLE coordination_goals(id TEXT PRIMARY KEY,room_id TEXT);
        CREATE TABLE collaboration_rooms(id TEXT PRIMARY KEY,project_id TEXT);
        INSERT INTO collaboration_rooms VALUES ('room','project');
        INSERT INTO coordination_goals VALUES ('goal','room');
    """)
    migrate_hints(db)
    migrate_hints(db)  # Upgrade replaces the old trigger, never doubles hints.
    db.execute("INSERT INTO operations VALUES ('ordinary','project','running',0,0)")
    assert not db.execute('SELECT * FROM collaboration_relay_revisions').fetchall()
    db.execute("INSERT INTO operations VALUES ('managed','project','queued',0,0)")
    db.execute("INSERT INTO coordination_operations VALUES ('managed','goal')")
    db.execute("UPDATE operations SET state='running' WHERE id='managed'")
    assert db.execute('SELECT revision FROM collaboration_relay_revisions').fetchone()[0] == 1
    db.execute("UPDATE operations SET output_seq=output_seq+1,last_operation_verified_at=10 WHERE id='managed'")
    db.execute("UPDATE operations SET state=state WHERE id='managed'")
    assert db.execute('SELECT revision FROM collaboration_relay_revisions').fetchone()[0] == 1
    db.execute("UPDATE operations SET state='succeeded' WHERE id='managed'")
    assert db.execute('SELECT revision FROM collaboration_relay_revisions').fetchone()[0] == 2
    db.close()


def test_projection_cache_is_request_local_copy_safe_and_write_invalidated(collab):
    class Reader:
        def __init__(self, store):
            self.store, self.calls = store, 0

        @memoized
        def value(self, identifier):
            self.calls += 1
            return {'id': identifier, 'values': []}

        @projection
        def page(self, *, write=False):
            self.value('one')['values'].append('must-not-leak')
            assert self.value('one')['values'] == []
            if write:
                self.store.execute("INSERT OR REPLACE INTO meta VALUES ('audit_projection','test')")
                self.value('one')

    reader = Reader(collab[0].store)
    reader.page()
    assert reader.calls == 1
    reader.page(write=True)
    assert reader.calls == 3
    reader.value('one')
    assert reader.calls == 4


@pytest.mark.parametrize('query,found', [('mentions', False), ('"v2"', True), ('line one\nline two', True), ('100%_', True)])
def test_message_search_reads_text_not_json_metadata(collab, query, found):
    service, owner, _, _, room, _, _, scope = collab
    saved = service.chatroom.create({**scope, 'room_id': room['id'],
        'body_text': 'release "v2": line one\nline two 100%_', 'client_message_id': key(),
        'idempotency_key': key()}, owner)['message']
    result = service.read({**scope, 'conversation_id': room['id'], 'kind': 'search', 'query': query}, owner)
    assert (saved['id'] in {row['id'] for row in result['items']}) == found


def test_room_patch_archive_and_canonical_restore_share_snapshot(collab):
    service, owner, _, _, room, *_ = collab
    current = service.store.one('SELECT * FROM conversation_rooms WHERE id=?', (room['id'],))
    archived = service.rooms.update({'conversation_id': room['id'], 'state': 'archived',
        'expected_version': current['version'], 'idempotency_key': key()}, owner)
    assert archived['room']['state'] == 'archived'
    current = service.store.one('SELECT * FROM conversation_rooms WHERE id=?', (room['id'],))
    assert service.store.one('SELECT 1 FROM conversation_archives WHERE conversation_id=?', (room['id'],))
    restored = service.conversations.archive.restore({'conversation_id': room['id'],
        'expected_version': current['version'], 'confirm_restore': True, 'idempotency_key': key()}, owner)
    assert restored['requires_reauthorization']
    assert service.store.one('SELECT state FROM conversation_rooms WHERE id=?', (room['id'],))['state'] == 'active'


def test_legacy_archive_missing_snapshot_returns_business_error(collab):
    service, owner, _, _, room, *_ = collab
    service.store.execute("UPDATE conversation_rooms SET state='archived',archived_at=1 WHERE id=?", (room['id'],))
    current = service.store.one('SELECT * FROM conversation_rooms WHERE id=?', (room['id'],))
    with pytest.raises(DevError) as error:
        service.conversations.archive.restore({'conversation_id': room['id'],
            'expected_version': current['version'], 'confirm_restore': True, 'idempotency_key': key()}, owner)
    assert error.value.code == 'ARCHIVE_SNAPSHOT_MISSING'


def test_expired_goal_and_work_are_settled_once(collab):
    service, owner, _, _, room, _, clock, scope = collab
    q = service.coordination
    goal = q.create({**scope, 'conversation_id': room['id'], 'objective': 'Synthetic audit check',
        'acceptance': 'Report results', 'project_ids': ['proj'], 'participant_grant_ids': ['worker'],
        'capabilities': ['read'], 'idempotency_key': key()}, owner)['goal']
    goal = q.approve({**scope, 'goal_id': goal['id'], 'expected_version': goal['version'],
        'digest': goal['digest'], 'idempotency_key': key()}, owner)['goal']
    clock[0] = goal['expires_at'] + 1
    q.reconcile()
    expired = service.store.one('SELECT * FROM coordination_goals WHERE id=?', (goal['id'],))
    assert expired['state'] == 'expired'
    assert not service.store.one("SELECT 1 FROM coordination_work WHERE goal_id=? AND state IN ('queued','leased','running')", (goal['id'],))
    q.reconcile()
    assert service.store.one('SELECT version FROM coordination_goals WHERE id=?', (goal['id'],))['version'] == expired['version']


def test_legacy_archive_reconciliation_preserves_snapshot_and_authority_boundary(collab):
    service, owner, _, _, room, *_ = collab
    service.store.execute("UPDATE conversation_rooms SET state='archived',archived_at=1 WHERE id=?", (room['id'],))
    with service.store.transaction():
        service.conversations.archive.reconcile_legacy()
    current = service.store.one('SELECT * FROM conversation_rooms WHERE id=?', (room['id'],))
    assert service.store.one('SELECT 1 FROM conversation_archives WHERE conversation_id=?', (room['id'],))
    assert not service.store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=?', (room['id'],))
    with service.store.transaction():
        service.conversations.archive.reconcile_legacy()
    assert service.store.one('SELECT version FROM conversation_rooms WHERE id=?', (room['id'],))['version'] == current['version']
    result = service.conversations.archive.restore({'conversation_id': room['id'],
        'expected_version': current['version'], 'confirm_restore': True, 'idempotency_key': key()}, owner)
    assert result['requires_reauthorization']


def test_empty_native_room_archive_restore_never_invents_projects(collab):
    service, owner, *_ = collab
    room = service.rooms.create({'title': 'Empty synthetic room', 'projects': [], 'idempotency_key': key()}, owner)['room']
    archived = service.rooms.update({'conversation_id': room['id'], 'state': 'archived',
        'expected_version': room['version'], 'idempotency_key': key()}, owner)['room']
    restored = service.rooms.update({'conversation_id': room['id'], 'state': 'active',
        'expected_version': archived['version'], 'idempotency_key': key()}, owner)['room']
    assert restored['state'] == 'active' and restored['projects'] == []


def test_work_page_bounds_invisible_candidates_and_keeps_cursor(collab, monkeypatch):
    service, owner, _, _, room, *_ = collab
    original = service.store.all
    scanned = []

    def rows(sql, args=()):
        if sql.startswith('SELECT * FROM coordination_goals WHERE conversation_id='):
            assert args[-1] <= 201
            return [{'id': str(index), 'created': 10000 - index} for index in range(args[-1])]
        return original(sql, args)

    def invisible(goal, principal, **kwargs):
        assert kwargs['include_recovery'] is False
        scanned.append(goal['id'])
        raise DevError('HIDDEN', 'synthetic hidden candidate', 403)

    monkeypatch.setattr(service.store, 'all', rows)
    monkeypatch.setattr(service.room_runtime, 'work_view', invisible)
    page = service.room_runtime.work_page(room['id'], owner, limit=100)
    assert len(scanned) == 200 and page['next_cursor'] and not page['pagination_complete']


def test_idle_channel_backoff_only_when_no_enabled_account(collab, monkeypatch):
    from hub.collaboration.lifecycle import CollaborationLoops
    service = collab[0]
    loops = CollaborationLoops(service)
    assert loops.channel_delay('telegram_poll', 1) == 30
    assert loops.channel_delay('channel_mirrors', 1) == 30
    monkeypatch.setattr(service.store, 'one', lambda *args: {'active': 1})
    assert loops.channel_delay('telegram_poll', 1) == 1


def test_monitor_cleanup_bounds_tombstones_and_keeps_referenced_evidence(collab):
    from hub.collaboration.monitor import MonitorService
    service, _, _, _, room, _, clock, _ = collab
    old = clock[0] - 40 * 86400
    with service.store.transaction():
        for index in range(502):
            service.store.execute("""INSERT INTO monitor_evidence(id,room_id,body,digest,created,expires_at)
                VALUES (?,?,'null','synthetic',?,?)""", ('old-' + str(index), room['id'], old - 1, old))
        service.store.execute("""INSERT INTO monitor_evidence(id,room_id,body,digest,created,expires_at)
            VALUES ('pinned',?,'null','synthetic',?,?)""", (room['id'], old - 1, old))
        service.store.execute("""INSERT INTO monitor_incidents
            (id,room_id,fingerprint,rule_id,probe_id,plan_id,plan_version,state,severity,evidence_id,opened_at,updated)
            VALUES ('incident',?,'synthetic','rule','probe','plan',1,'resolved','low','pinned',?,?)""",
            (room['id'], old - 1, old))
    monitor = MonitorService(service)
    monitor.cleanup()
    assert service.store.one("SELECT COUNT(*) AS n FROM monitor_evidence WHERE id LIKE 'old-%'")['n'] == 2
    assert service.store.one("SELECT id FROM monitor_evidence WHERE id='pinned'")
    monitor.cleanup()
    assert service.store.one('SELECT COUNT(*) AS n FROM monitor_evidence')['n'] == 1
