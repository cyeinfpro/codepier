"""Forward migration and late-client permission/read-cursor races."""
import sqlite3

import pytest

from hub.collaboration.schema import SCHEMA, migrate
from shared.util import DevError
from tests.collaboration_support import collab, key


def test_speaking_permission_cas_and_revoked_grant(collab):
    s, owner, worker, _, room, _, _, scope = collab
    original = {**scope, 'room_id': room['id'], 'grant_id': worker.grant_id, 'enabled': True,
                'expected_version': 0, 'idempotency_key': key()}
    assert s.chatroom.access(original, owner)['version'] == 1
    assert s.chatroom.access({**original, 'enabled': False, 'expected_version': 1, 'idempotency_key': key()}, owner)['version'] == 2
    with pytest.raises(DevError) as exc:
        s.chatroom.access({**original, 'expected_version': 1, 'idempotency_key': key()}, owner)
    assert exc.value.code == 'STALE_VERSION'
    assert s.store.one('SELECT enabled FROM conversation_writers')['enabled'] == 0
    s.chatroom.access({**original, 'expected_version': 2, 'idempotency_key': key()}, owner)
    s.store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    with pytest.raises(DevError):
        s.chatroom.create({**scope, 'room_id': room['id'], 'body_text': 'blocked',
                           'client_message_id': key(), 'idempotency_key': key()}, worker)


def test_upgrade_unread_boundary_cannot_move_backwards(collab):
    s, owner, _, _, room, _, _, scope = collab
    for _ in range(3):
        s.chatroom.create({**scope, 'room_id': room['id'], 'body_text': 'historical',
                           'client_message_id': key(), 'idempotency_key': key()}, owner)
    s.store.execute('UPDATE collaboration_rooms SET chat_history_sequence=3 WHERE id=?', (room['id'],))
    assert s.read({**scope, 'kind': 'timeline'}, owner)['read_sequence'] == 3
    assert s.chatroom.set_read_cursor({**scope, 'room_id': room['id'], 'last_seen_sequence': 1,
                                      'idempotency_key': key()}, owner)['read_sequence'] == 3


def test_real_v1_forward_migration_threads_and_stable_order():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    db.execute("INSERT INTO meta VALUES ('schema','8')")
    db.execute("INSERT INTO meta VALUES ('collaboration_schema','1')")
    db.execute('CREATE TABLE projects (id TEXT,space_id TEXT,owner_user_id TEXT)')
    db.execute('CREATE TABLE spaces (id TEXT)')
    db.execute('CREATE TABLE users (id TEXT)')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO collaboration_rooms (id,space_id,owner_user_id,project_id,environment_id,state,created) VALUES ('room','space','owner','proj','production','paused',1)")
    db.execute("INSERT INTO collaboration_agents (id,room_id,label,kind,queue,grant_id,expires_at,created) VALUES ('agent','room','worker','work_cloud','work-analysis','grant',9999,1)")
    db.execute('''INSERT INTO collaboration_join_slots
        (id,room_id,label,kind,join_code,code_expires_at,expires_at,created,updated)
        VALUES ('slot','room','slot','work_cloud','CPJ-1111-2222-3333-4444',9999,9999,1,1)''')
    for mid, thread, kind, body in [('a','a','owner_command','{}'),('b','a','owner_command','{}'),
                                    ('c','goal','agent_result','{"job_id":"job"}')]:
        db.execute('''INSERT INTO collaboration_messages (id,room_id,thread_id,author,origin,source_id,kind,body,state,created)
            VALUES (?,'room',?,'owner','panel',?,?,?,'saved',1)''', (mid,thread,mid,kind,body))
    db.execute('''INSERT INTO collaboration_goals (id,room_id,source_message_id,request,acceptance,created,updated)
        VALUES ('goal','room','a','fixture','proof',1,1)''')
    db.execute('''INSERT INTO collaboration_jobs
        (id,room_id,goal_id,kind,assignee_agent_id,target_grant_id,queue,business_key,deadline_at,context,created,updated)
        VALUES ('job','room','goal','analyze_incident','agent','grant','work-analysis','legacy',9999,'{"origin_message_id":"a"}',1,1)''')
    jobs = db.execute('SELECT * FROM collaboration_jobs').fetchall()
    joins = db.execute('SELECT * FROM collaboration_join_slots').fetchall()
    migrate(db)
    migrate(db)
    assert db.execute('SELECT * FROM collaboration_jobs').fetchall() == jobs
    assert db.execute('SELECT * FROM collaboration_join_slots').fetchall() == joins
    assert db.execute('SELECT id,server_sequence,thread_root_id FROM collaboration_messages ORDER BY server_sequence').fetchall() == [('a',1,'a'),('b',2,'a'),('c',3,'a')]
    assert db.execute('SELECT state,chat_history_sequence FROM collaboration_rooms').fetchone() == ('paused',3)
    assert db.execute("SELECT value FROM meta WHERE key='collaboration_schema'").fetchone()[0] == '3'
    db.close()


def test_real_v2_sequence_and_read_position_survive_clock_ties():
    from hub.collaboration.schema import migrate_chatroom
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
    db.execute("INSERT INTO meta VALUES ('schema','8')")
    db.execute("INSERT INTO meta VALUES ('collaboration_schema','2')")
    db.execute('CREATE TABLE projects (id TEXT,space_id TEXT,owner_user_id TEXT)')
    db.execute('CREATE TABLE spaces (id TEXT)')
    db.execute('CREATE TABLE users (id TEXT)')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO collaboration_rooms (id,space_id,owner_user_id,project_id,environment_id,created) VALUES ('room','space','owner','proj','production',1)")
    migrate_chatroom(db)
    for identifier in ('z_first', 'a_second'):
        db.execute('''INSERT INTO collaboration_messages (id,room_id,thread_id,author,origin,source_id,kind,body,state,created)
            VALUES (?,'room',?,'owner','panel',?,'text','{}','saved',1)''', (identifier,identifier,identifier))
    db.execute("INSERT INTO collaboration_read_cursors VALUES ('room','owner',1,1)")
    migrate(db)
    assert db.execute('SELECT id,server_sequence,conversation_sequence FROM collaboration_messages ORDER BY conversation_sequence').fetchall() == [('z_first',1,1),('a_second',2,2)]
    assert db.execute("SELECT sequence FROM conversation_read_cursors WHERE conversation_id='room'").fetchone()[0] == 1
    db.close()
