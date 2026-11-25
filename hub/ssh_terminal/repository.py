"""Persist terminal identity and lifecycle metadata, never terminal content.

All methods run in the existing Hub Store database worker (except initialize,
which is called during composition before the application starts).
"""

from __future__ import annotations

import json
import time

from .diagnostics import safe_metadata


SCHEMA = """
CREATE TABLE IF NOT EXISTS vps_terminal_sessions (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    vps_id TEXT NOT NULL,
    connection_revision INTEGER NOT NULL,
    binding_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    project_root TEXT NOT NULL,
    runtime_generation TEXT NOT NULL,
    requested_mode TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    reason_code TEXT,
    writer_epoch TEXT NOT NULL DEFAULT '0',
    stream_epoch TEXT NOT NULL DEFAULT '1',
    cols INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(space_id,owner_user_id,request_id)
);
CREATE INDEX IF NOT EXISTS vps_terminal_owner
    ON vps_terminal_sessions(space_id,owner_user_id,updated);
CREATE INDEX IF NOT EXISTS vps_terminal_device
    ON vps_terminal_sessions(device_id,state);
"""

TERMINAL_STATES = frozenset({"exited", "failed", "close_unknown"})
SAFE_METADATA = frozenset(
    {
        "vps_name",
        "project_alias",
        "device_name",
        "username",
        "host",
        "port",
        "bytes_in",
        "bytes_out",
        "exit_status",
        "attachment_exit_status",
        "remote_session_state",
        "output_complete",
        "recoverability",
        "tmux_ref",
        "state_revision",
        "ssh_connected",
        "direct_vps",
    }
)
UPDATE_FIELDS = frozenset(
    {
        "state",
        "reason_code",
        "mode",
        "runtime_generation",
        "stream_epoch",
        "writer_epoch",
        "cols",
        "rows",
    }
)


class TerminalRepository:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(SCHEMA)

    def get(self, sid):
        return self.store.one("SELECT * FROM vps_terminal_sessions WHERE id=?", (sid,))

    def by_request(self, space, owner, request_id):
        return self.store.one(
            "SELECT * FROM vps_terminal_sessions WHERE space_id=? AND owner_user_id=? AND request_id=?",
            (space, owner, request_id),
        )

    def insert(self, row):
        # Service admission holds one asyncio lock and this UNIQUE constraint is
        # the final guard against independent connection races.
        keys = tuple(row)
        with self.store.transaction():
            self.store.db.execute(
                f"INSERT INTO vps_terminal_sessions ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                tuple(row[key] for key in keys),
            )
        return self.get(row["id"])

    def update(self, sid, changes, *, expected_generation=None, allow_generation_change=False):
        with self.store.transaction():
            row = self.get(sid)
            if row is None:
                return None
            if expected_generation is not None and row["runtime_generation"] != expected_generation:
                return row
            generation = changes.get("runtime_generation", row["runtime_generation"])
            metadata = json.loads(row["metadata"])
            if generation != row["runtime_generation"]:
                if not allow_generation_change:
                    return row
                metadata["state_revision"] = "0"
            elif "state_revision" in changes and int(changes["state_revision"]) <= int(
                metadata.get("state_revision", "0")
            ):
                return row
            values = {key: value for key, value in changes.items() if key in UPDATE_FIELDS}
            metadata.update({key: value for key, value in changes.items() if key in SAFE_METADATA})
            diagnostic = safe_metadata(changes)
            if "stage_timings_ms" in diagnostic:
                diagnostic["stage_timings_ms"] = {
                    **safe_metadata(metadata).get("stage_timings_ms", {}),
                    **diagnostic["stage_timings_ms"],
                }
            metadata.update(diagnostic)
            values.update(metadata=json.dumps(metadata, ensure_ascii=False), updated=time.time())
            self.store.db.execute(
                "UPDATE vps_terminal_sessions SET "
                + ",".join(key + "=?" for key in values)
                + " WHERE id=?",
                (*values.values(), sid),
            )
        return self.get(sid)

    def list(self, space, owner):
        return self.store.all(
            "SELECT * FROM vps_terminal_sessions WHERE space_id=? AND owner_user_id=? ORDER BY updated DESC LIMIT 200",
            (space, owner),
        )

    def list_active(self, space, owner):
        return self.store.all(
            "SELECT * FROM vps_terminal_sessions "
            "WHERE space_id=? AND owner_user_id=? "
            "AND state NOT IN ('exited','failed','close_unknown') "
            "ORDER BY updated DESC, id DESC",
            (space, owner),
        )

    def counts(self, space, owner, device):
        rows = self.store.all(
            "SELECT space_id,owner_user_id,device_id FROM vps_terminal_sessions WHERE state NOT IN ('exited','failed','close_unknown')"
        )
        return (
            len(rows),
            sum(r["space_id"] == space and r["owner_user_id"] == owner for r in rows),
            sum(r["device_id"] == device for r in rows),
        )

    def active_device_count(self):
        return self.store.one(
            "SELECT COUNT(DISTINCT device_id) AS count FROM vps_terminal_sessions "
            "WHERE state NOT IN ('exited','failed','close_unknown')"
        )["count"]

    @staticmethod
    def public(row):
        result = {
            key: row[key]
            for key in (
                "id",
                "project_id",
                "device_id",
                "vps_id",
                "runtime_generation",
                "requested_mode",
                "mode",
                "state",
                "reason_code",
                "writer_epoch",
                "stream_epoch",
                "cols",
                "rows",
                "created",
                "updated",
            )
        }
        result.update(json.loads(row["metadata"]))
        result["session_id"] = row["id"]
        return result
