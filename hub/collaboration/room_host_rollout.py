"""Local offline rollout commands for the bridge database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

from hub.collaboration.delivery_compatibility import raise_floor
from hub.collaboration.room_host_migration import HostBindingBackfill
from hub.state_layout import state_directory
from hub.store import Store
from shared.hub_data_contract import (
    CONTRACT,
    CONTRACT_KEY,
    check_database,
    check_path,
    verify_compatibility,
)
from shared.instance_lock import InstanceLock
from shared.util import VERSION


class ReadOnlyStore:
    def __init__(self, connection):
        self.connection = connection

    def one(self, sql, args=()):
        row = self.connection.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]


def has_unscanned_legacy_members(store):
    return bool(
        store.one("""SELECT 1 FROM room_members m WHERE m.connection_identity_version=1
        AND NOT EXISTS (SELECT 1 FROM room_host_backfill_results r
            WHERE r.member_id=m.id AND r.binding_generation=m.binding_generation) LIMIT 1""")
    )


def advance_auto_backfill(store):
    with store.transaction():
        report = HostBindingBackfill(store).batch(apply=True)
        if report["next_after"] is not None:
            return False
        # A legacy member may have appeared before the saved cursor while the
        # Hub was serving requests in shadow mode. Give it a separate pass.
        if has_unscanned_legacy_members(store):
            store.execute("""UPDATE room_host_backfill_progress SET after_member_id='',scanned_count=0,
                completed=0 WHERE id=1""")
            return False
        return True


def resolve_startup_mode(store, config):
    if config.host_connections_mode == "on":
        if not store.one("SELECT id FROM room_host_backfill_progress WHERE id=1 AND completed=1"):
            raise RuntimeError(
                "Finish room-host-backfill --apply before enabling independent host connections"
            )
        return config, False
    if config.host_connections_mode != "auto":
        return config, False
    if not config.enabled or not store.one("SELECT 1 FROM meta WHERE key=?", (CONTRACT_KEY,)):
        return replace(config, host_connections_mode="off"), False
    if store.one("SELECT 1 FROM room_members WHERE connection_identity_version=1 LIMIT 1"):
        completed = bool(
            store.one("SELECT 1 FROM room_host_backfill_progress WHERE id=1 AND completed=1")
        ) and not has_unscanned_legacy_members(store)
    else:
        # No legacy rows need migration. Initialize the empty checkpoint before
        # the server accepts its first independent binding.
        completed = advance_auto_backfill(store)
    return replace(config, host_connections_mode="on" if completed else "shadow"), not completed


def activate(data_directory, *, confirmed):
    if not confirmed:
        raise RuntimeError(
            "Verify supported launchers and stop all existing Hub writers before confirming activation"
        )
    verify_compatibility(CONTRACT, VERSION)
    state = state_directory(data_directory)
    database = state / "hub.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise RuntimeError("Bridge state database is missing")
    with InstanceLock(state / ".hub.lock"):
        store = Store(data_directory)
        try:
            with store.transaction():
                raise_floor(store)
                if not check_database(store.db, VERSION):
                    store.execute(
                        "INSERT INTO meta(key,value) VALUES (?,?)",
                        (CONTRACT_KEY, json.dumps(CONTRACT, sort_keys=True)),
                    )
        finally:
            store.close()
    return {"activated": True, **CONTRACT}


def backfill(data_directory, *, apply, after, limit, restart):
    if (apply and after) or (restart and not apply):
        raise ValueError("Only dry-run accepts --after, and only --apply accepts --restart-scan")
    state = state_directory(data_directory)
    database = state / "hub.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise RuntimeError("Bridge state database is missing")
    if apply:
        with InstanceLock(state / ".hub.lock"):
            check_path(state, VERSION, required=True)
            store = Store(data_directory)
            try:
                return HostBindingBackfill(store).batch(apply=True, limit=limit, restart=restart)
            finally:
                store.close()
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        check_database(connection, VERSION)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(room_members)")}
        if "connection_identity_version" not in columns:
            raise RuntimeError(
                "Initialize the additive bridge schema before requesting a backfill report"
            )
        return HostBindingBackfill(ReadOnlyStore(connection)).batch(after=after, limit=limit)
