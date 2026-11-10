from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hub.collaboration.delivery_compatibility import assert_compatible, check_database, raise_floor
from hub.collaboration.telemetry import Action, Channel, CollaborationTelemetry, Stage
from hub.runtime import Runtime
from hub.store import Store
from scripts.migrate_hub_data import probe_admin
from scripts.panel_update_runtime import DATABASE_FLOOR_PROBE


def test_floor_activates_with_protected_transaction_and_survives_reopen(tmp_path):
    store = Store(tmp_path)
    try:
        assert store.one("SELECT value FROM meta WHERE key='schema'") == {'value': '10'}
        with pytest.raises(RuntimeError):
            raise_floor(store)
        with pytest.raises(ValueError), store.transaction():
            raise_floor(store)
            raise ValueError('business effect rolled back')
        assert store.one("SELECT value FROM meta WHERE key='schema'") == {'value': '10'}
        with store.transaction():
            raise_floor(store)
            raise_floor(store)
            store.execute("INSERT INTO meta(key,value) VALUES ('retained_receipt','unknown')")
        check_database(tmp_path)
    finally:
        store.close()
    with closing(sqlite3.connect(tmp_path / 'hub.sqlite3')) as db:
        with pytest.raises(RuntimeError, match='reader/writer floor'):
            assert_compatible(db, reader=0, writer=1)
        with pytest.raises(RuntimeError, match='reader/writer floor'):
            assert_compatible(db, reader=1, writer=0)
    reopened = Store(tmp_path)
    try:
        assert reopened.one("SELECT value FROM meta WHERE key='schema'") == {'value': '11'}
        assert reopened.one("SELECT value FROM meta WHERE key='retained_receipt'") == {'value': 'unknown'}
    finally:
        reopened.close()


def test_malformed_floor_fails_before_writer_and_install_probe(tmp_path):
    store = Store(tmp_path)
    with store.transaction():
        raise_floor(store)
        store.execute("DELETE FROM meta WHERE key='collaboration_min_writer'")
    store.close()
    database = tmp_path / 'hub.sqlite3'
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match='compatibility declaration'):
        Store(tmp_path)
    assert probe_admin(tmp_path) == 4
    assert database.read_bytes() == before


def test_readonly_controller_probe_reads_exact_floor(tmp_path):
    store = Store(tmp_path)
    with store.transaction():
        raise_floor(store)
    store.close()
    db_path = tmp_path / 'hub.sqlite3'
    before = db_path.read_bytes()
    probe = DATABASE_FLOOR_PROBE.replace("'/state/hub.sqlite3'", repr(str(db_path)))
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'schema': '11', 'collaboration_min_reader': '1',
                                        'collaboration_min_writer': '1'}
    assert db_path.read_bytes() == before


def test_telemetry_is_typed_and_counts_invalid_labels_and_failed_sinks():
    def broken_sink(event):
        assert set(event) == {'action', 'stage', 'channel', 'reason'}
        raise OSError('sink unavailable')

    telemetry = CollaborationTelemetry(broken_sink)
    telemetry.record(Action.WAKE, Stage.ACCEPTED, channel=Channel.HOST)
    telemetry.record('private-room-id', Stage.ACCEPTED, channel=Channel.HOST)
    snapshot = telemetry.snapshot()
    assert snapshot['series'] == [{'action': 'wake', 'stage': 'accepted', 'channel': 'host',
                                   'reason': 'none', 'count': 1}]
    assert snapshot['omitted_events'] == 2
    assert snapshot['sink_errors'] == snapshot['invalid_events'] == 1
    assert 'private-room-id' not in json.dumps(snapshot)


def test_runtime_counts_all_attempts_and_only_proven_commits(monkeypatch):
    telemetry = CollaborationTelemetry()
    monkeypatch.setattr('hub.collaboration.telemetry.collaboration_telemetry', telemetry)

    async def run(invoke, name, raw, principal):
        return raw['result']

    runtime = SimpleNamespace(_loop=None, store=SimpleNamespace(run=run),
                              collaboration=SimpleNamespace(invoke=object()))

    async def fixture():
        for result in ({'replayed': False, 'effects_created': True},
                       {'replayed': True, 'effects_created': False},
                       {'replayed': False, 'effects_created': False}):
            assert await Runtime._invoke_async(runtime, 'collaboration_room_reply', {'result': result}, None) == result
        await Runtime._invoke_async(runtime, 'collaboration_room_inbox', {'result': {}}, None)

    asyncio.run(fixture())
    summary = {(item['action'], item['stage']): item['count'] for item in telemetry.snapshot()['series']}
    assert summary == {('reply', 'attempted'): 3, ('reply', 'committed'): 1,
                       ('reply', 'replayed'): 1, ('reply', 'succeeded'): 1,
                       ('read', 'attempted'): 1, ('read', 'succeeded'): 1}
