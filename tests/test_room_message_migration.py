"""The populated canonical message ledger survives its nullable partition upgrade."""
import sqlite3

import pytest

from hub.collaboration.room_message_migration import migrate


def populated_messages():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE collaboration_rooms(id TEXT PRIMARY KEY);
        CREATE TABLE conversation_rooms(id TEXT PRIMARY KEY);
        INSERT INTO collaboration_rooms VALUES ('partition');
        INSERT INTO conversation_rooms VALUES ('legacy-room'),('empty-room');
        CREATE TABLE collaboration_messages (
            id TEXT PRIMARY KEY,room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
            conversation_id TEXT NOT NULL DEFAULT '',thread_id TEXT NOT NULL DEFAULT '',
            thread_root_id TEXT NOT NULL DEFAULT '',reply_to_id TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL,origin TEXT NOT NULL,source_id TEXT NOT NULL,kind TEXT NOT NULL,
            body TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1,
            server_sequence INTEGER NOT NULL DEFAULT 0,conversation_sequence INTEGER NOT NULL DEFAULT 0,
            original_extension TEXT,UNIQUE(room_id,origin,author,source_id));
        CREATE UNIQUE INDEX original_conversation_order
            ON collaboration_messages(conversation_id,conversation_sequence) WHERE conversation_sequence>0;
        CREATE TABLE collaboration_room_changes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
            kind TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,created REAL NOT NULL);
        CREATE TABLE migration_receipts (
            id TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES collaboration_messages(id),receipt TEXT NOT NULL);
        CREATE TABLE migration_audit(message_id TEXT,body TEXT);
        CREATE TRIGGER original_message_audit AFTER UPDATE OF body ON collaboration_messages
            BEGIN INSERT INTO migration_audit VALUES (NEW.id,NEW.body); END;
        CREATE TRIGGER original_receipt_audit AFTER UPDATE ON migration_receipts
            BEGIN INSERT INTO migration_audit SELECT id,body FROM collaboration_messages
            WHERE id=NEW.message_id; END;
    ''')
    for index, identifier in enumerate(('old-root', 'old-reply')):
        db.execute('''INSERT INTO collaboration_messages
            VALUES (?,'partition','legacy-room','old-root','old-root',?,'owner','panel_owner',?,
                'text',?,'saved',12,4,?,?,?)''',
            (identifier, '' if not index else 'old-root', identifier,
             '{"body_text":"preserved original ' + str(index) + '"}', 17 + index, 100 + index,
             'extension-' + str(index)))
    db.execute("INSERT INTO migration_receipts VALUES ('original-operation-receipt','old-root','original stable receipt')")
    db.commit()
    return db


def snapshot(db):
    columns = [row[1] for row in db.execute('PRAGMA table_info(collaboration_messages)')]
    return columns, db.execute('SELECT * FROM collaboration_messages ORDER BY id').fetchall()


def test_populated_message_ledger_keeps_every_column_receipt_index_trigger_and_sequence():
    db = populated_messages()
    original = snapshot(db)
    old_index = db.execute("SELECT sql FROM sqlite_master WHERE name='original_conversation_order'").fetchone()[0]
    with db:
        db.execute('BEGIN')
        migrate(db)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert snapshot(db) == original
    assert db.execute("SELECT sql FROM sqlite_master WHERE name='original_conversation_order'").fetchone()[0] == old_index
    assert db.execute('SELECT * FROM migration_receipts').fetchall() == [
        ('original-operation-receipt', 'old-root', 'original stable receipt')]
    assert {row[2] for row in db.execute('PRAGMA foreign_key_list(collaboration_messages)')} == {
        'collaboration_rooms', 'conversation_rooms'}
    db.execute('PRAGMA foreign_keys=ON')
    db.execute("UPDATE migration_receipts SET receipt='checked original receipt' WHERE id='original-operation-receipt'")
    assert db.execute('SELECT * FROM migration_audit').fetchall() == [('old-root', '{"body_text":"preserved original 0"}')]
    for identifier, partition, conversation in [('new-legacy', 'partition', 'legacy-room'), ('new-native', None, 'empty-room')]:
        db.execute('''INSERT INTO collaboration_messages
            (id,room_id,conversation_id,author,origin,source_id,kind,body,state,created)
            VALUES (?,?,?,'owner','panel_owner',?,'text','{}','saved',13)''',
            (identifier, partition, conversation, identifier))
    assert db.execute("SELECT room_id,server_sequence,conversation_sequence FROM collaboration_messages WHERE id='new-legacy'").fetchone() == ('partition', 19, 102)
    assert db.execute("SELECT room_id,server_sequence,conversation_sequence FROM collaboration_messages WHERE id='new-native'").fetchone() == (None, 1, 1)
    db.execute("UPDATE collaboration_messages SET body='native update',version=2 WHERE id='new-native'")
    assert db.execute("SELECT body FROM migration_audit WHERE message_id='new-native'").fetchall() == [('native update',)]
    assert db.execute('SELECT room_id,object_id FROM collaboration_room_changes').fetchall() == [('partition', 'new-legacy')]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM collaboration_messages WHERE id='old-root'")
    db.commit()
    with db:
        db.execute('BEGIN')
        migrate(db)
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert db.execute('SELECT COUNT(*) FROM collaboration_messages').fetchone()[0] == 4
    assert db.execute("SELECT original_extension FROM collaboration_messages WHERE id='old-reply'").fetchone()[0] == 'extension-1'
    db.close()


def test_new_conversation_foreign_key_failure_rolls_back_the_original_ledger():
    db = populated_messages()
    db.execute("UPDATE collaboration_messages SET conversation_id='missing-historical-room' WHERE id='old-root'")
    db.commit()
    before = snapshot(db)
    ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='collaboration_messages'").fetchone()[0]
    with pytest.raises(RuntimeError, match='foreign keys'):
        with db:
            db.execute('BEGIN')
            migrate(db)
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Startup foreign keys failed')
    assert snapshot(db) == before
    assert db.execute("SELECT sql FROM sqlite_master WHERE name='collaboration_messages'").fetchone()[0] == ddl
    assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_room_upgrade'").fetchall()
    assert db.execute("SELECT name FROM sqlite_master WHERE name='original_receipt_audit'").fetchone()
    db.close()
