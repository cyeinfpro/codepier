"""Automatic host connections across fresh, compatible and historical state."""
import json
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hub.__main__ import main
from hub.app import create_app
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.room_host_migration import HostBindingBackfill
from hub.collaboration.room_host_rollout import resolve_startup_mode
from hub.store import Store
from shared.hub_data_contract import CONTRACT, CONTRACT_KEY, check_path
from tests.collaboration_support import collab as collab
from tests.test_room_members_rebuild import add, create, rooms as rooms


@pytest.fixture(autouse=True)
def startup_env(monkeypatch):
    monkeypatch.delenv('HUB_STATE_DIR', raising=False)
    monkeypatch.delenv('CODEPIER_ROOM_HOST_CONNECTIONS_MODE', raising=False)
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')


def test_fresh_app_uses_independent_mode_by_default_and_guards_rollback(tmp_path):
    state = tmp_path / 'fresh'
    with TestClient(create_app(state)) as client:
        collaboration = client.app.state.runtime.collaboration
        assert collaboration.rooms.host_connections_enabled()
        assert collaboration.store.one('SELECT completed FROM room_host_backfill_progress')['completed'] == 1

    with pytest.raises(RuntimeError, match='min_reader_version'):
        check_path(state, '1.33.8')


def test_init_records_compatibility_before_collaboration_is_enabled(tmp_path, monkeypatch):
    state = tmp_path / 'init'
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'false')
    monkeypatch.setenv('HUB_DATA_DIR', str(state))
    monkeypatch.setenv('CODEPIER_ADMIN_PASSWORD', 'fixture-admin-password')
    monkeypatch.setattr(sys, 'argv', ['hub', '--data-dir', str(state), 'init'])
    main()
    assert check_path(state, '1.33.9', required=True)

    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    with TestClient(create_app(state)) as client:
        assert client.app.state.runtime.collaboration.rooms.host_connections_enabled()


def test_existing_unmarked_database_starts_with_legacy_mode(tmp_path):
    state = tmp_path / 'legacy'
    Store(state).close()
    with TestClient(create_app(state)) as client:
        collaboration = client.app.state.runtime.collaboration
        assert collaboration.config.host_connections_mode == 'off'
        assert not collaboration.store.one('SELECT 1 FROM meta WHERE key=?', (CONTRACT_KEY,))


@pytest.mark.parametrize('mode', ['off', 'shadow'])
def test_explicit_override_keeps_fresh_state_unmarked(tmp_path, monkeypatch, mode):
    state = tmp_path / mode
    monkeypatch.setenv('CODEPIER_ROOM_HOST_CONNECTIONS_MODE', mode)
    with TestClient(create_app(state)) as client:
        assert client.app.state.runtime.collaboration.config.host_connections_mode == mode
    assert not check_path(state, '1.33.9')


def test_explicit_on_requires_an_activated_existing_database(tmp_path, monkeypatch):
    state = tmp_path / 'explicit'
    Store(state).close()
    monkeypatch.setenv('CODEPIER_ROOM_HOST_CONNECTIONS_MODE', 'on')
    with pytest.raises(RuntimeError, match='activation receipt'):
        create_app(state)


def test_auto_mode_resumes_backfill_and_enables_after_legacy_members_are_scanned(rooms, monkeypatch):
    service, owner, _, _ = rooms
    room = create(service, owner)
    add(service, owner, room)
    add(service, owner, room)
    service.store.execute('INSERT INTO meta VALUES (?,?)', (CONTRACT_KEY, json.dumps(CONTRACT)))

    original = HostBindingBackfill.batch

    def one_per_batch(self, **kwargs):
        return original(self, limit=1, **kwargs)

    monkeypatch.setattr(HostBindingBackfill, 'batch', one_per_batch)
    requested = replace(service.config, host_connections_mode='auto')
    initial, pending = resolve_startup_mode(service.store, requested)
    assert initial.host_connections_mode == 'shadow' and pending
    assert not service.store.one('SELECT 1 FROM room_host_backfill_progress')

    service.config = initial
    from hub.collaboration.lifecycle import CollaborationLoops
    loops = CollaborationLoops(service)
    loops.host_backfill_pending = pending
    loops.resume_host_backfill()
    assert service.store.one('SELECT scanned_count FROM room_host_backfill_progress')['scanned_count'] == 1

    service.config, pending = resolve_startup_mode(service.store, requested)
    assert service.config.host_connections_mode == 'shadow' and pending
    restarted = CollaborationLoops(service)
    restarted.host_backfill_pending = pending
    restarted.resume_host_backfill()
    assert service.rooms.host_connections_enabled() and not restarted.host_backfill_pending
    assert service.store.one('SELECT scanned_count FROM room_host_backfill_progress')['scanned_count'] == 2
    assert len(service.store.all('SELECT * FROM room_host_backfill_results WHERE outcome=\'pending\'')) == 2


def test_auto_is_the_configuration_default():
    assert CollaborationConfig.from_env().host_connections_mode == 'auto'
