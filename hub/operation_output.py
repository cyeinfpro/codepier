"""Durable bounded live tails, independent of the wide operation journal.

Agent v2 sends a complete tail snapshot and a monotonic sequence. Agent v1
sends append fragments. Neither is an unbounded stdout archive. A terminal
commit materializes its final tail in the historical operation row and removes
the live row in the same transaction as the completion outbox. Existing
databases therefore need no history rewrite and old receipts stay readable.
"""

from __future__ import annotations

import time
from hub.operation_history import seal, unseal

OUTPUT_LIMIT = 131072
OUTPUT_STATES = frozenset(
    {"queued", "running", "reconnecting", "cancelling", "unknown", "needs_review"}
)


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS operation_output_tails (
        operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
        output_seq INTEGER NOT NULL CHECK(output_seq >= 0),
        output TEXT NOT NULL CHECK(length(output) <= 131072))""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(operation_output_tails)")}
    if "sealed_output" not in columns:
        db.execute("ALTER TABLE operation_output_tails ADD COLUMN sealed_output TEXT")
        db.execute("ALTER TABLE operation_output_tails ADD COLUMN output_chars INTEGER")
    # These predicates are used by authorized project history reads and active
    # admission checks; no owner/grant/privacy predicate is removed or cached.
    db.execute("""CREATE INDEX IF NOT EXISTS operations_project_recent
        ON operations(space_id,project_id,created DESC,state,owner_user_id,visibility,grant_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS operations_project_active
        ON operations(space_id,project_id,state,owner_user_id,visibility,grant_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS operations_device_active
        ON operations(device_id,state)""")


def output_window(text, limit, offset=None):
    """Exact explicit ranges; default tails prefer complete lines when available."""
    if not limit:
        return ""
    if offset is not None:
        return text[offset : offset + limit]
    start = max(0, len(text) - limit)
    if start and text[start - 1] != "\n":
        boundary = text.find("\n", start) + 1
        if start < boundary < len(text):
            start = boundary
    return text[start:]


class OperationOutput:
    def __init__(self, store):
        self.store = store

    def read(self, row, *, limit=OUTPUT_LIMIT, include_body=True, offset=None):
        """Read only after the caller authorized this operation.

        The parent sequence is authoritative. A legacy writer which changes the
        operation row cannot accidentally expose a stale side-table snapshot.
        Compact status reads select only a character count from the live row.
        """

        def window(text):
            if not include_body:
                return None
            if not limit:
                return ""
            return output_window(text, limit, offset)

        columns = "COALESCE(output_chars,length(output)) AS chars"
        args = [row["id"], row["output_seq"]]
        if include_body and limit:
            columns += ",output,sealed_output"
        live = self.store.one(
            "SELECT "
            + columns
            + " FROM operation_output_tails WHERE operation_id=? AND output_seq=?",
            args,
        )
        if live is not None:
            text = (
                unseal(self.store, live.get("sealed_output") or live.get("output") or "")
                if include_body and limit
                else ""
            )
            return window(text), live["chars"]
        from hub.operation_archive import POINTER

        if not include_body and str(row.get("output") or "").startswith(POINTER):
            archived = self.store.one(
                "SELECT output_chars FROM operation_payload_archives WHERE operation_id=?",
                (row["id"],),
            )
            if archived is None:
                raise RuntimeError("Archived output is missing")
            return None, archived["output_chars"]
        legacy = unseal(self.store, row.get("output") or "")
        return window(legacy), len(legacy)

    def receive(self, identifier, data):
        """Commit each accepted output before publishing its availability.

        No volatile coalescing buffer can lose an acknowledged snapshot on a
        Hub crash. The state and sequence checks share the tail transaction.
        """
        with self.store.transaction():
            row = self.store.one(
                "SELECT id,state,output_seq FROM operations WHERE id=?", (identifier,)
            )
            if row is None or row["state"] not in OUTPUT_STATES:
                return False
            if data.get("snapshot"):
                sequence = data.get("seq")
                if type(sequence) is not int or sequence <= row["output_seq"]:
                    return False
                output = str(data.get("text", ""))[-OUTPUT_LIMIT:]
            else:
                sequence = row["output_seq"] + 1
                # Only legacy append messages need to read the previous body.
                legacy = self.store.one("SELECT output FROM operations WHERE id=?", (identifier,))
                previous, _ = self.read({**row, **legacy})
                output = (previous + str(data.get("text", ""))[:8192])[-OUTPUT_LIMIT:]
            now = time.time()
            self.store.execute(
                """INSERT INTO operation_output_tails(operation_id,output_seq,output,sealed_output,output_chars)
                VALUES(?,?,'',?,?) ON CONFLICT(operation_id) DO UPDATE
                SET output_seq=excluded.output_seq,output='',sealed_output=excluded.sealed_output,output_chars=excluded.output_chars""",
                (identifier, sequence, seal(self.store, output), len(output)),
            )
            # Keep the hot parent row small. A legacy active tail is moved only
            # when that operation receives output, never by a bulk migration.
            self.store.execute(
                "UPDATE operations SET output='',output_seq=?,updated=?,last_output_at=? WHERE id=?",
                (sequence, now, now, identifier),
            )
        return True

    def discard(self, identifier):
        self.store.require_transaction()
        self.store.execute("DELETE FROM operation_output_tails WHERE operation_id=?", (identifier,))
