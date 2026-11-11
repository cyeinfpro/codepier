"""Startup-only expansion of the existing event journal to user-room scope.

Legacy partition IDs keep their meaning. Room events use a separate nullable
foreign key, and the existing delivery journal, leases and replay cursors remain
unchanged. Store runs this migration in its serialized startup transaction with
foreign keys disabled, then checks every foreign key before committing.
"""

from __future__ import annotations

import re


def migrate(db):
    for table in ("mcp_event_subscriptions", "mcp_event_outbox"):
        columns = list(db.execute(f"PRAGMA table_info({table})"))
        if not columns or "conversation_id" in {row[1] for row in columns}:
            continue
        if db.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError("Room event migration requires the Store startup transaction")
        original = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        temporary = table + "_room_upgrade"
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (temporary,)).fetchone():
            raise RuntimeError("Unexpected incomplete room event migration")
        replacement = re.sub(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+" + table + r"\s*\(",
            "CREATE TABLE " + temporary + " (",
            original,
            count=1,
            flags=re.I,
        )
        replacement, changed = re.subn(
            r"room_id TEXT NOT NULL REFERENCES collaboration_rooms\(id\)",
            "room_id TEXT REFERENCES collaboration_rooms(id), "
            "conversation_id TEXT REFERENCES conversation_rooms(id)",
            replacement,
            count=1,
        )
        if changed != 1:
            raise RuntimeError("Unknown event journal schema; migration refused")
        end = replacement.rfind(")")
        replacement = (
            replacement[:end]
            + (
                ", CHECK ((room_id IS NOT NULL AND conversation_id IS NULL) OR "
                "(room_id IS NULL AND conversation_id IS NOT NULL))"
            )
            + replacement[end:]
        )
        indexes = [
            row[0]
            for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (table,),
            )
        ]
        triggers = [
            (row[0], row[1])
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
            )
            if re.search(r"\b" + table + r"\b", row[1])
        ]
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        names = ",".join('"' + row[1].replace('"', '""') + '"' for row in columns)
        db.execute(replacement)
        db.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM {table}")
        before = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        after = db.execute(f"SELECT COUNT(*) FROM {temporary}").fetchone()[0]
        different = db.execute(
            f"SELECT 1 FROM (SELECT {names} FROM {table} EXCEPT SELECT {names} FROM {temporary}) LIMIT 1"
        ).fetchone()
        if before != after or different:
            raise RuntimeError("Room event migration failed journal preservation")
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
        if sequence:
            db.execute(
                "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (sequence[0], table)
            )
        for statement in indexes:
            db.execute(statement)
        for _, statement in triggers:
            db.execute(statement)
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_subscriptions_conversation
        ON mcp_event_subscriptions(conversation_id,grant_id,state) WHERE conversation_id IS NOT NULL""")
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_outbox_conversation
        ON mcp_event_outbox(conversation_id,seq) WHERE conversation_id IS NOT NULL""")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('room_event_schema','1')")
