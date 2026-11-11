"""Synthetic migration history: steady-state reads, pending work and rollback."""
import sqlite3

import pytest

from hub.collaboration.conversations import migrate as migrate_conversations
from hub.collaboration.schema import SCHEMA, migrate


@pytest.fixture
def migration_db():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO meta VALUES ('schema','8')")
        db.execute('CREATE TABLE projects (id TEXT,space_id TEXT,owner_user_id TEXT)')
        db.execute('CREATE TABLE spaces (id TEXT)')
        db.execute('CREATE TABLE users (id TEXT)')
        db.executescript(SCHEMA)
        for index in range(64):
            room = f'partition-{index}'
            db.execute("""INSERT INTO collaboration_rooms
                (id,space_id,owner_user_id,project_id,environment_id,created)
                VALUES (?,'space','owner',?,'production',1)""", (room, room))
            for number in range(3):
                identifier = f'{room}-{number}'
                db.execute("""INSERT INTO collaboration_messages
                    (id,room_id,thread_id,author,origin,source_id,kind,body,state,created)
                    VALUES (?,?,?,'owner','panel',?,'text','{}','saved',1)""",
                    (identifier, room, f'{room}-0', identifier))
        migrate(db)
        db.execute("""INSERT INTO conversation_rooms
            (id,space_id,owner_user_id,title,created) VALUES ('combined','space','owner','Shared room',1)""")
        db.execute("UPDATE collaboration_messages SET conversation_id='combined' WHERE room_id='partition-63'")
        db.execute('UPDATE conversation_rooms SET history_sequence=2')
        db.execute("INSERT INTO conversation_read_cursors VALUES ('combined','owner',2,1)")
        db.commit()
        yield db
    finally:
        db.close()


def snapshot(db):
    return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
            for table in ('collaboration_messages', 'conversation_rooms', 'conversation_projects',
                          'conversation_partition_migrations', 'conversation_read_cursors')}


@pytest.mark.parametrize('pending', [False, True])
def test_conversation_migration_only_inspects_pending_history(migration_db, pending):
    db = migration_db
    before = snapshot(db)
    if pending:
        db.execute("UPDATE collaboration_messages SET conversation_sequence=0 WHERE id='partition-63-2'")
    statements = []
    db.set_trace_callback(statements.append)
    try:
        migrate_conversations(db)
    finally:
        db.set_trace_callback(None)
    sequence_reads = [sql for sql in statements
                      if sql.startswith('SELECT COALESCE(MAX(conversation_sequence),0)')]
    assert len(sequence_reads) == int(pending)
    if pending:
        assert "conversation_id='combined'" in sequence_reads[0]
    assert not [sql for sql in statements if sql.startswith(
        'SELECT 1 FROM conversation_partition_migrations WHERE room_id=')]
    assert snapshot(db) == before
    plan = db.execute("""EXPLAIN QUERY PLAN SELECT DISTINCT m.conversation_id
        FROM collaboration_messages m JOIN conversation_rooms c ON c.id=m.conversation_id
        WHERE m.conversation_sequence=0""").fetchall()
    assert any('conversation_message_backfill_pending' in row[3] for row in plan)


def test_conversation_migration_rollback_keeps_pending_work_discoverable(migration_db):
    db = migration_db
    expected = snapshot(db)
    db.execute("UPDATE collaboration_messages SET conversation_sequence=0 WHERE id='partition-63-2'")
    db.commit()

    def deny_write(action, table, column, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == 'collaboration_messages' else sqlite3.SQLITE_OK

    try:
        with pytest.raises(sqlite3.DatabaseError):
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.set_authorizer(deny_write)
                migrate_conversations(db)
    finally:
        db.set_authorizer(None)
    assert db.execute("SELECT conversation_sequence FROM collaboration_messages WHERE id='partition-63-2'").fetchone() == (0,)
    with db:
        migrate_conversations(db)
    assert snapshot(db) == expected


def test_unknown_conversation_is_not_implicitly_created_by_backfill(migration_db):
    db = migration_db
    db.execute("""UPDATE collaboration_messages SET conversation_id='missing-room',conversation_sequence=0
        WHERE id='partition-63-2'""")
    before = snapshot(db)
    migrate_conversations(db)
    assert snapshot(db) == before
    assert not db.execute("SELECT 1 FROM conversation_rooms WHERE id='missing-room'").fetchone()
