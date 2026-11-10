"""Local offline rollout commands for the bridge database."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from hub.collaboration.delivery_compatibility import raise_floor
from hub.collaboration.room_host_migration import HostBindingBackfill
from hub.state_layout import state_directory
from hub.store import Store
from shared.hub_data_contract import CONTRACT, CONTRACT_KEY, check_database, check_path, verify_compatibility
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


def activate(data_directory, *, confirmed):
    if not confirmed:
        raise RuntimeError('Verify supported launchers and stop all existing Hub writers before confirming activation')
    verify_compatibility(CONTRACT, VERSION)
    state = state_directory(data_directory)
    database = state / 'hub.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise RuntimeError('Bridge state database is missing')
    with InstanceLock(state / '.hub.lock'):
        store = Store(data_directory)
        try:
            with store.transaction():
                raise_floor(store)
                if not check_database(store.db, VERSION):
                    store.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                                  (CONTRACT_KEY, json.dumps(CONTRACT, sort_keys=True)))
        finally:
            store.close()
    return {'activated': True, **CONTRACT}


def backfill(data_directory, *, apply, after, limit, restart):
    if (apply and after) or (restart and not apply):
        raise ValueError('Only dry-run accepts --after, and only --apply accepts --restart-scan')
    state = state_directory(data_directory)
    database = state / 'hub.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise RuntimeError('Bridge state database is missing')
    if apply:
        with InstanceLock(state / '.hub.lock'):
            check_path(state, VERSION, required=True)
            store = Store(data_directory)
            try:
                return HostBindingBackfill(store).batch(apply=True, limit=limit, restart=restart)
            finally:
                store.close()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        check_database(connection, VERSION)
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(room_members)')}
        if 'connection_identity_version' not in columns:
            raise RuntimeError('Initialize the additive bridge schema before requesting a backfill report')
        return HostBindingBackfill(ReadOnlyStore(connection)).batch(after=after, limit=limit)
