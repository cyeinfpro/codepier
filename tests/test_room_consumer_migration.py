"""Populated legacy consumer journals survive the room identity expansion."""
import sqlite3

import pytest

from hub.collaboration.dot_consumers import migrate as legacy_consumers
from hub.collaboration.dot_mailbox import migrate as legacy_mailbox
from hub.collaboration.room_consumer_migration import migrate


def populated_journal():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE collaboration_dots(id TEXT PRIMARY KEY);
        CREATE TABLE room_members(id TEXT PRIMARY KEY);
        INSERT INTO collaboration_dots VALUES ('legacy-dot');
        INSERT INTO room_members VALUES ('native-member');
    ''')
    legacy_consumers(db)
    legacy_mailbox(db)
    db.execute("INSERT INTO dot_consumer_fences VALUES ('legacy-dot',17)")
    db.execute('''INSERT INTO dot_consumer_sessions VALUES
        ('session','legacy-dot','generation','grant','subscription','run','request',
         'request-digest','proof-hash','encrypted-proof','scope-digest',17,'active',900,10,11)''')
    db.execute('''INSERT INTO dot_consumer_mutations VALUES
        ('session','heartbeat','mutation-key','mutation-digest','{"original":true}',12)''')
    db.execute('''INSERT INTO dot_wake_tickets VALUES
        ('ticket-hash','legacy-dot','generation','grant','subscription','delivery',
         'encrypted-ticket',900,'session',9)''')
    db.execute('''INSERT INTO dot_mailbox_entries
        (sequence,id,dot_id,generation,kind,object_id,object_version,reference,revision,acknowledged_at,created) VALUES
        (23,'entry','legacy-dot','generation','message','message',2,'{"message_id":"message"}',8,NULL,10)''')
    db.execute('''INSERT INTO dot_mailbox_entries
        (sequence,id,dot_id,generation,kind,object_id,object_version,reference,revision,acknowledged_at,created) VALUES
        (101,'deleted','legacy-dot','generation','message','deleted',1,'{}',9,12,11)''')
    db.execute("DELETE FROM dot_mailbox_entries WHERE id='deleted'")
    db.execute("UPDATE dot_mailbox_entries SET acknowledged_scope_digest='preserved-scope' WHERE id='entry'")
    db.execute('ALTER TABLE dot_consumer_sessions ADD COLUMN preserved_extension TEXT')
    db.execute("UPDATE dot_consumer_sessions SET preserved_extension='original-extension'")
    db.execute('CREATE TABLE migration_fixture(source_id TEXT)')
    db.execute('''CREATE TRIGGER original_mutation_receipt AFTER UPDATE ON dot_consumer_mutations
        BEGIN INSERT INTO migration_fixture SELECT id FROM dot_consumer_sessions
        WHERE id=NEW.session_id; END''')
    db.commit()
    return db


def snapshot(db, table):
    columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})')]
    return columns, db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall()


def test_populated_journal_retains_ciphertext_fences_receipts_foreign_keys_and_high_water():
    db = populated_journal()
    tables = ('dot_consumer_fences', 'dot_consumer_sessions', 'dot_consumer_mutations',
              'dot_wake_tickets', 'dot_mailbox_entries')
    original = {table: snapshot(db, table) for table in tables}
    with db:
        db.execute('BEGIN')
        migrate(db)
    for table, (columns, rows) in original.items():
        assert db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall() == rows
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('PRAGMA foreign_key_list(dot_consumer_mutations)').fetchone()[2] == 'dot_consumer_sessions'
    db.execute('''INSERT INTO dot_mailbox_entries
        (id,member_id,generation,kind,object_id,object_version,reference,revision,created)
        VALUES ('native-entry','native-member','native-generation','message','source',1,'{}',1,13)''')
    assert db.execute("SELECT sequence,dot_id FROM dot_mailbox_entries WHERE id='native-entry'").fetchone() == (102, None)
    db.execute("UPDATE dot_consumer_mutations SET created=13 WHERE session_id='session'")
    assert db.execute('SELECT * FROM migration_fixture').fetchall() == [('session',)]
    db.commit()
    with db:
        db.execute('BEGIN')
        migrate(db)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT COUNT(*) FROM dot_mailbox_entries').fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize('dot,member', [(None, None), ('legacy-dot', 'native-member')])
def test_consumer_identity_is_exclusive_and_never_an_implicit_fallback(dot, member):
    db = populated_journal()
    with db:
        db.execute('BEGIN')
        migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO dot_consumer_fences(dot_id,member_id,fence) VALUES (?,?,0)', (dot, member))
    db.close()


def test_failure_in_a_later_journal_rolls_back_the_whole_startup_migration():
    db = populated_journal()
    # A schema from an unknown future version is not guessed or partially upgraded.
    db.execute('ALTER TABLE dot_wake_tickets RENAME COLUMN dot_id TO unknown_identity')
    db.commit()
    before = snapshot(db, 'dot_consumer_sessions')
    with pytest.raises(RuntimeError, match='Unknown consumer journal schema'):
        with db:
            db.execute('BEGIN')
            migrate(db)
    assert snapshot(db, 'dot_consumer_sessions') == before
    assert 'member_id' not in {row[1] for row in db.execute('PRAGMA table_info(dot_consumer_fences)')}
    assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_room_upgrade'").fetchall() == []
    db.close()
