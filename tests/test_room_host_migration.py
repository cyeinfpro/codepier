"""Historical bindings are adopted in bounded, auditable batches."""
import json
import sqlite3
from dataclasses import replace

import pytest

from hub.collaboration import telemetry
from hub.collaboration.room_host_migration import HostBindingBackfill
from hub.collaboration.room_host_rollout import backfill
from scripts.panel_update_runtime import verify_data_contract
from shared.hub_data_contract import CONTRACT, CONTRACT_KEY, check_database, check_path
from scripts.panel_update_source import UpdateError
from shared.util import DevError
from tests.collaboration_support import collab as collab
from tests.test_room_connections import reuse, verified
from tests.test_room_members_rebuild import add, create, rooms as rooms


@pytest.mark.asyncio
async def test_adopts_own_round_trip_without_guessing_shared_gateway_identity(rooms, caplog, monkeypatch):
    s, owner, actor, _ = rooms
    metrics = telemetry.CollaborationTelemetry()
    monkeypatch.setattr(telemetry, 'collaboration_telemetry', metrics)
    source = await verified(s, actor, add(s, owner, create(s, owner)))
    target = await verified(s, actor, reuse(s, owner, create(s, owner), source))
    s.store.execute("UPDATE room_members SET status='paused' WHERE id=?", (source['member_id'],))
    route_generation = s.rooms.member(target['member_id'])['route_generation']
    backfill_service = HostBindingBackfill(s.store, clock=s.clock)

    report = backfill_service.batch()
    assert report['counts'] == {'independent_round_trip': 1, 'legacy_control_unverified': 1}
    assert not s.store.all('SELECT * FROM room_host_binding_adoptions')
    for row in report['items']:
        assert set(row) == {'member_id', 'binding_generation', 'reason', 'outcome'}

    first = backfill_service.batch(apply=True, limit=1)
    assert first['next_after'] and first['scanned_count'] == 1
    second = HostBindingBackfill(s.store, clock=s.clock).batch(apply=True, limit=1)
    assert second['next_after'] is None and second['scanned_count'] == 2
    member_source = s.rooms.member(source['member_id'])
    member_target = s.rooms.member(target['member_id'])
    assert member_source['status'] == 'paused'
    assert member_source['host_connection_id'] != member_target['host_connection_id']
    assert member_source['connection_identity_version'] == member_target['connection_identity_version'] == 2
    assert member_target['connection_source_member_id'] == source['member_id']
    assert member_target['route_generation'] == route_generation
    s.config = replace(s.config, host_connections_mode='shadow')
    with caplog.at_level('INFO', logger='codepier.collaboration.host_connections'):
        assert s.room_connections.guard_join(member_target, actor)['id'] == member_target['host_connection_id']
    assert 'room_host_shadow reason=source_paused' in caplog.text

    s.store.execute("UPDATE room_host_connections SET state='revoked',host_revocation_epoch=1 WHERE id=?",
                    (member_target['host_connection_id'],))
    with pytest.raises(DevError) as revoked:
        s.room_connections.guard_join(member_target, actor)
    assert revoked.value.code == 'ROOM_CONNECTION_REVOKED'
    assert [item for item in metrics.snapshot()['series'] if item['action'] == 'host_shadow'] == [
        {'action': 'host_shadow', 'stage': 'shadow_would_allow', 'channel': 'host',
         'reason': 'source_paused', 'count': 1},
        {'action': 'host_shadow', 'stage': 'shadow_would_block', 'channel': 'host',
         'reason': 'host_revoked', 'count': 1},
    ]
    s.store.execute("UPDATE room_members SET connection_identity_version=1,host_connection_id='' WHERE id=?",
                    (target['member_id'],))
    repeat = backfill_service.batch(apply=True, restart=True)
    assert repeat['counts'] == {'host_revoked': 1}
    assert len(s.store.all('SELECT * FROM room_host_connections')) == 2
    assert len(s.store.all('SELECT * FROM room_host_binding_adoptions')) == 2


@pytest.mark.asyncio
async def test_cycle_pending_conflict_isolated_and_resumes_after_repair(rooms):
    s, owner, actor, _ = rooms
    first = await verified(s, actor, add(s, owner, create(s, owner)), 'first')
    second = await verified(s, actor, add(s, owner, create(s, owner)), 'second')
    third = await verified(s, actor, add(s, owner, create(s, owner)), 'third')
    s.store.execute("""UPDATE room_members SET connection_source_member_id=id,connection_source_generation=binding_generation
        WHERE id=?""", (first['member_id'],))
    s.store.execute("""CREATE TABLE host_backfill_conflict_fixture (member_id TEXT NOT NULL)""")
    s.store.execute("""INSERT INTO host_backfill_conflict_fixture VALUES (?)""", (second['member_id'],))
    s.store.execute("""CREATE TRIGGER host_backfill_conflict BEFORE INSERT ON room_host_connections
        WHEN EXISTS(SELECT 1 FROM host_backfill_conflict_fixture WHERE member_id=NEW.established_by_member_id)
        BEGIN SELECT RAISE(ABORT,'fixture conflict'); END""")
    migrator = HostBindingBackfill(s.store, clock=s.clock)
    report = migrator.batch(apply=True)
    assert report['counts'] == {'source_cycle_or_depth': 1, 'adoption_conflict': 1, 'independent_round_trip': 1}
    assert report['next_after'] is None
    assert s.store.one('SELECT * FROM room_host_backfill_results WHERE member_id=?',
                       (second['member_id'],))['outcome'] == 'error'
    assert s.rooms.member(third['member_id'])['connection_identity_version'] == 2

    s.store.execute('DELETE FROM host_backfill_conflict_fixture')
    report = migrator.batch(apply=True, restart=True)
    assert report['counts'] == {'source_cycle_or_depth': 1, 'independent_round_trip': 1}
    assert s.rooms.member(second['member_id'])['connection_identity_version'] == 2
    assert s.rooms.member(first['member_id'])['connection_identity_version'] == 1


def test_dry_run_cli_can_report_without_mutating_database(rooms):
    s, owner, _, _ = rooms
    member = add(s, owner, create(s, owner))['member']
    report = backfill(s.store.directory, apply=False, after='', limit=1, restart=False)
    assert report['items'] == [{'member_id': member['id'], 'binding_generation': 1,
                               'outcome': 'pending', 'reason': 'binding_not_verified'}]
    assert not s.store.all('SELECT * FROM room_host_backfill_results')
    assert not s.store.all('SELECT * FROM room_host_backfill_progress')
    with pytest.raises(RuntimeError, match='activation receipt'):
        backfill(s.store.directory, apply=True, after='', limit=1, restart=False)


def test_compatibility_refuses_unknown_old_and_incompatible_future_contracts(tmp_path):
    database = tmp_path / 'hub.sqlite3'
    db = sqlite3.connect(database)
    try:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        assert not check_database(db, '1.33.8')
        db.execute('INSERT INTO meta VALUES (?,?)', (CONTRACT_KEY, json.dumps(CONTRACT)))
        db.commit()
        with pytest.raises(RuntimeError, match='min_reader_version'):
            check_path(tmp_path, '1.33.8')
        assert check_path(tmp_path, '1.33.9', required=True)
        with pytest.raises(RuntimeError, match='Unknown Hub release'):
            check_path(tmp_path, 'unreleased')
        db.execute('UPDATE meta SET value=?', (json.dumps({**CONTRACT, 'version': 2}),))
        db.commit()
        with pytest.raises(RuntimeError, match='Unknown Hub data contract'):
            check_path(tmp_path, '1.34.0')
    finally:
        db.close()

    with pytest.raises(UpdateError) as rejected:
        verify_data_contract(CONTRACT, '1.33.8')
    assert rejected.value.code == 'DATA_CONTRACT_INCOMPATIBLE'
    verify_data_contract(CONTRACT, '1.33.9')
