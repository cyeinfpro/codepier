"""Expand the original message ledger to real project-independent conversations.

Store owns the startup transaction and the final foreign-key check. Every old
identifier, receipt reference, sequence, index and unrelated trigger is retained.
"""
from __future__ import annotations

import re


def migrate(db):
    table = 'collaboration_messages'
    columns = list(db.execute(f'PRAGMA table_info({table})'))
    partition = next(row for row in columns if row[1] == 'room_id')
    if partition[3]:
        if db.execute('PRAGMA foreign_keys').fetchone()[0]:
            raise RuntimeError('Room message migration requires the Store startup transaction')
        original = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        temporary = table + '_room_upgrade'
        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (temporary,)).fetchone():
            raise RuntimeError('Unexpected incomplete room message migration')
        replacement = re.sub(r'CREATE TABLE(?: IF NOT EXISTS)?\s+' + table + r'\s*\(',
                             'CREATE TABLE ' + temporary + ' (', original, count=1, flags=re.I)
        replacement, count = re.subn(r'room_id TEXT NOT NULL REFERENCES collaboration_rooms\(id\)',
            'room_id TEXT REFERENCES collaboration_rooms(id)', replacement, count=1)
        if count != 1:
            raise RuntimeError('Unknown canonical message schema; migration refused')
        end = replacement.rfind(')')
        replacement = replacement[:end] + (
            ", CHECK (room_id IS NOT NULL OR conversation_id!=''), "
            'FOREIGN KEY(conversation_id) REFERENCES conversation_rooms(id)'
        ) + replacement[end:]
        indexes = [row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        triggers = [(row[0], row[1]) for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")
            if re.search(r'\b' + table + r'\b', row[1])]
        names = ','.join('"' + row[1].replace('"', '""') + '"' for row in columns)
        db.execute(replacement)
        db.execute(f'INSERT INTO {temporary} ({names}) SELECT {names} FROM {table}')
        if (db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] !=
                db.execute(f'SELECT COUNT(*) FROM {temporary}').fetchone()[0] or db.execute(
                    f'SELECT 1 FROM (SELECT {names} FROM {table} EXCEPT SELECT {names} FROM {temporary}) LIMIT 1').fetchone()):
            raise RuntimeError('Message migration failed preservation verification')
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        db.execute(f'DROP TABLE {table}')
        db.execute(f'ALTER TABLE {temporary} RENAME TO {table}')
        for sql in indexes:
            db.execute(sql)
        for _, sql in triggers:
            db.execute(sql)
    # Earlier compatibility migrations reinstall their historical trigger each
    # startup; this final definition handles both scopes without a fake project.
    db.execute('DROP TRIGGER IF EXISTS collaboration_message_insert')
    db.execute('''CREATE TRIGGER collaboration_message_insert AFTER INSERT ON collaboration_messages BEGIN
        UPDATE collaboration_messages SET
            server_sequence=(SELECT COALESCE(MAX(server_sequence),0)+1 FROM collaboration_messages
                WHERE id!=NEW.id AND ((NEW.room_id IS NOT NULL AND room_id=NEW.room_id) OR
                  (NEW.room_id IS NULL AND room_id IS NULL AND conversation_id=NEW.conversation_id))),
            conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END,
            conversation_sequence=(SELECT COALESCE(MAX(conversation_sequence),0)+1 FROM collaboration_messages
                WHERE conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END AND id!=NEW.id),
            thread_root_id=CASE WHEN NEW.thread_root_id!='' THEN NEW.thread_root_id ELSE
                COALESCE((SELECT NULLIF(thread_root_id,'') FROM collaboration_messages
                    WHERE conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END
                    AND id=NEW.thread_id AND id!=NEW.id),NEW.id) END WHERE id=NEW.id;
        INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
            SELECT NEW.room_id,'message',NEW.id,NEW.version,NEW.created WHERE NEW.room_id IS NOT NULL;
        END''')
    db.execute('DROP TRIGGER IF EXISTS collaboration_message_change')
    db.execute('''CREATE TRIGGER collaboration_message_change AFTER UPDATE OF version,state,body ON collaboration_messages
        WHEN NEW.version!=OLD.version OR NEW.state!=OLD.state OR NEW.body!=OLD.body BEGIN
        INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
            SELECT NEW.room_id,'message',NEW.id,NEW.version,NEW.created WHERE NEW.room_id IS NOT NULL;
        END''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS room_message_source_identity
        ON collaboration_messages(conversation_id,origin,author,source_id) WHERE room_id IS NULL''')
    # Keep historical/native lookup scopes intact while bounding hot paths.
    # The unique positive-sequence and native source-identity indexes above
    # remain the authoritative constraints; these indexes only aid reads.
    db.execute('''CREATE INDEX IF NOT EXISTS room_message_conversation_order
        ON collaboration_messages(conversation_id,conversation_sequence,id)''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_message_owner_source_lookup
        ON collaboration_messages(conversation_id,author,source_id)
        WHERE origin='panel_owner' ''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_message_author_time
        ON collaboration_messages(conversation_id,author,created)''')
    db.execute("INSERT OR REPLACE INTO meta VALUES ('room_message_schema','1')")
