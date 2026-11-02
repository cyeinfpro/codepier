"""Expand the existing consumer journal to independent room-member identities.

The original Dot identifiers, references, tickets, receipts and fences retain
their meanings. No second queue or consumer lease engine is introduced.
"""
from __future__ import annotations

import re


def migrate(db):
    for table in ('dot_consumer_fences', 'dot_consumer_sessions', 'dot_wake_tickets', 'dot_mailbox_entries'):
        columns = list(db.execute(f'PRAGMA table_info({table})'))
        if 'member_id' in {row[1] for row in columns}:
            continue
        if not columns or db.execute('PRAGMA foreign_keys').fetchone()[0]:
            raise RuntimeError('Consumer identity migration requires the serialized Store startup transaction')
        original = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        temporary = table + '_room_upgrade'
        replacement = re.sub(r'CREATE TABLE(?: IF NOT EXISTS)?\s+' + table + r'\s*\(',
                             'CREATE TABLE ' + temporary + ' (', original, count=1, flags=re.I)
        replacement, changed = re.subn(
            r'dot_id TEXT (?:NOT NULL|PRIMARY KEY) REFERENCES collaboration_dots\(id\)',
            'dot_id TEXT ' + ('UNIQUE ' if table == 'dot_consumer_fences' else '') +
            'REFERENCES collaboration_dots(id), member_id TEXT REFERENCES room_members(id)',
            replacement, count=1)
        if changed != 1:
            raise RuntimeError('Unknown consumer journal schema; migration refused')
        end = replacement.rfind(')')
        replacement = replacement[:end] + (
            ', CHECK ((dot_id IS NOT NULL AND member_id IS NULL) OR '
            '(dot_id IS NULL AND member_id IS NOT NULL))') + replacement[end:]
        indexes = [row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        triggers = [(row[0], row[1]) for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")
            if re.search(r'\b' + table + r'\b', row[1])]
        sequence = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
        names = ','.join('"' + row[1].replace('"', '""') + '"' for row in columns)
        db.execute(replacement)
        db.execute(f'INSERT INTO {temporary} ({names}) SELECT {names} FROM {table}')
        if (db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] !=
                db.execute(f'SELECT COUNT(*) FROM {temporary}').fetchone()[0] or db.execute(
                    f'SELECT 1 FROM (SELECT {names} FROM {table} EXCEPT SELECT {names} FROM {temporary}) LIMIT 1').fetchone()):
            raise RuntimeError('Consumer migration failed journal preservation')
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        db.execute(f'DROP TABLE {table}')
        db.execute(f'ALTER TABLE {temporary} RENAME TO {table}')
        if sequence:
            db.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (sequence[0], table))
        for sql in indexes:
            db.execute(sql)
        for _, sql in triggers:
            db.execute(sql)
    for table, suffix, columns in (
        ('dot_consumer_fences', 'identity', 'member_id'),
        ('dot_consumer_sessions', 'request', 'member_id,generation,request_key'),
        ('dot_consumer_sessions', 'run', 'member_id,generation,run_id'),
        ('dot_consumer_sessions', 'fence', 'member_id,fence'),
        ('dot_wake_tickets', 'delivery', 'member_id,generation,subscription_id,delivery_key'),
        ('dot_mailbox_entries', 'object', 'member_id,generation,kind,object_id,object_version'),
    ):
        db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {table}_member_{suffix} ON {table}({columns}) WHERE member_id IS NOT NULL')
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS dot_consumer_member_single_owner
        ON dot_consumer_sessions(member_id) WHERE member_id IS NOT NULL AND state='active'""")
    db.execute('''CREATE INDEX IF NOT EXISTS dot_mailbox_member_scan
        ON dot_mailbox_entries(member_id,generation,sequence) WHERE member_id IS NOT NULL''')
    entries = {row[1] for row in db.execute('PRAGMA table_info(dot_mailbox_entries)')}
    if 'superseded_by' not in entries:
        db.execute("ALTER TABLE dot_mailbox_entries ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''")
    columns = {row[1] for row in db.execute('PRAGMA table_info(room_members)')}
    for name, declaration in (
        ('wake_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_wake_at', 'REAL NOT NULL DEFAULT 0'),
        ('wake_generation', 'INTEGER NOT NULL DEFAULT 0'),
        ('manual_wakes', 'INTEGER NOT NULL DEFAULT 0'),
        ('manual_window_at', 'REAL NOT NULL DEFAULT 0'),
        ('last_manual_wake_at', 'REAL NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE room_members ADD COLUMN {name} {declaration}')
    db.execute("INSERT OR REPLACE INTO meta VALUES ('room_consumer_schema','1')")
