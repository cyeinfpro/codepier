"""Bounded discovery and connection fencing from the 2026-10-20 report."""
import asyncio
import json
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from hub.project_views import list_projects
from hub.runtime import Connection, Principal, Runtime, alias_key
from hub.store import Store
from shared.crypto import SecureChannel, token
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store, 'owner', 'owner')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)",
                  (store.encrypt(token()), time.time()))
    instance = Runtime(store)
    instance.wait_seconds = 0
    add_project(instance, 'project', 'Project')
    principal = Principal('panel:owner', 'owner', {'read', 'write', 'execute'}, ['*'], admin=True)
    yield instance, principal
    store.close()


def add_project(runtime, identifier, alias, description=''):
    runtime.store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) '
        'VALUES (?,?,?,?,?,?,?,?,?)', (identifier, alias, alias_key(alias), 'dev',
        '/tmp/fixture-' + identifier, description, 'write', 1, time.time()))


def grant(runtime, identifier, projects):
    runtime.store.execute('INSERT INTO grants(id,user_id,label,scopes,projects,created) VALUES (?,?,?,?,?,?)',
        (identifier, 'owner', identifier, json.dumps(['read']), json.dumps(projects), time.time()))
    return runtime.grant_principal(runtime.store.one('SELECT * FROM grants WHERE id=?', (identifier,)))


def test_project_discovery_reads_bounded_candidates_and_observes_only_returned_page(runtime, monkeypatch):
    instance, principal = runtime
    for number in range(265):
        add_project(instance, f'p{number:03}', f'Flow {number:03}')
    add_project(instance, 'unicode', 'Ｆｌｏｗ Extra')
    denied = {'p001', 'p099', 'p111', 'p264'}
    authorize = instance.authorize

    def checked_authorize(who, action, **kwargs):
        if kwargs.get('project_id') in denied:
            raise DevError('ROLE_POLICY_DENIED', 'fixture denial', 403)
        return authorize(who, action, **kwargs)

    monkeypatch.setattr(instance, 'authorize', checked_authorize)
    observed = []
    monkeypatch.setattr(instance, 'online', lambda identifier: observed.append(identifier) or True)
    read_all, candidates = instance.store.all, []

    def bounded_read(sql, parameters=()):
        rows = read_all(sql, parameters)
        if sql.startswith('SELECT p.*,d.name AS device_name FROM projects p'):
            candidates.append(len(rows))
            assert len(rows) <= 100, 'Discovery must not materialize every project before slicing'
        return rows

    monkeypatch.setattr(instance.store, 'all', bounded_read)
    first = list_projects(instance, {'query': 'ＦＬＯＷ', 'offset': 100, 'limit': 7}, principal)
    expected = [f'p{number:03}' for number in range(265) if f'p{number:03}' not in denied] + ['unicode']
    assert [row['id'] for row in first['projects']] == expected[100:107]
    assert first['total'] == len(expected) and first['next_offset'] == 107
    assert first['query'] == 'ＦＬＯＷ' and first['view_version'] == 1
    assert all('alias_key' not in row and 'owner_user_id' not in row for row in first['projects'])
    assert observed == ['dev']
    assert candidates == [100, 100, 67]

    observed.clear()
    last = list_projects(instance, {'query': 'flow', 'offset': len(expected) - 1, 'limit': 7}, principal)
    assert [row['id'] for row in last['projects']] == ['unicode']
    assert last['next_offset'] is None and observed == ['dev']
    observed.clear()
    empty = list_projects(instance, {'query': 'flow', 'offset': 500, 'limit': 7}, principal)
    assert empty['projects'] == [] and empty['total'] == len(expected) and empty['next_offset'] is None
    assert not observed


def test_project_discovery_preserves_unicode_description_and_exact_id_search(runtime):
    instance, principal = runtime
    add_project(instance, 'empty-description', 'Straße')
    add_project(instance, 'description-match', 'Unrelated', 'ＭＣＰ Straße')
    add_project(instance, 'id-target', 'Another', '')
    assert [row['id'] for row in list_projects(instance, {'query': 'STRASSE'}, principal)['projects']] == ['empty-description', 'description-match']
    assert [row['id'] for row in list_projects(instance, {'query': 'mcp strasse'}, principal)['projects']] == ['description-match']
    assert [row['id'] for row in list_projects(instance, {'query': 'ID-TARGET'}, principal)['projects']] == ['id-target']
    assert list_projects(instance, {'query': 'id-tar'}, principal)['projects'] == []


def test_candidate_queries_filter_credential_ids_before_loading_rows(runtime, monkeypatch):
    instance, _ = runtime
    for number in range(210):
        add_project(instance, f'p{number:03}', f'Project {number:03}')
    principal = grant(instance, 'restricted', ['p001', 'p120', "p001') OR 1=1 --"])
    read_all, candidates = instance.store.all, []

    def observe(sql, parameters=()):
        rows = read_all(sql, parameters)
        if sql.startswith('SELECT p.*,d.name AS device_name FROM projects p'):
            candidates.extend(row['id'] for row in rows)
        return rows

    monkeypatch.setattr(instance.store, 'all', observe)
    assert [row['id'] for row in instance.list_projects(principal)] == ['p001', 'p120']
    assert candidates == ['p001', 'p120']
    empty = grant(instance, 'empty', [])
    assert instance.list_projects(empty) == []


def test_legacy_project_list_keeps_all_rows_with_one_connection_observation_per_device(runtime, monkeypatch):
    instance, principal = runtime
    for number in range(205):
        add_project(instance, f'p{number:03}', f'Project {number:03}')
    observations = Counter()

    def online(identifier):
        observations[identifier] += 1
        return False

    monkeypatch.setattr(instance, 'online', online)
    rows = instance.list_projects(principal)
    assert len(rows) == 206 and len({row['id'] for row in rows}) == 206
    assert all(row['online'] is False for row in rows)
    assert observations == {'dev': 1}


@pytest.mark.asyncio
async def test_operation_projections_keep_bound_identifiers_and_live_record_authorization(runtime):
    instance, principal = runtime
    receipt = await instance.invoke('fs_read', {'project': 'Project', 'path': 'README.md'}, principal)
    identifier = receipt['operation_id']
    restricted = grant(instance, 'other-credential', ['project'])
    for status_only in (False, True):
        with pytest.raises(DevError) as invalid:
            instance.operation_row("missing' OR 1=1 --", principal, status_only=status_only)
        assert invalid.value.status in {403, 404}
        with pytest.raises(DevError) as hidden:
            instance.operation_row(identifier, restricted, status_only=status_only)
        assert hidden.value.status in {403, 404}
    status = instance.operation_row(identifier, principal, status_only=True)
    assert status['id'] == identifier
    assert not {'payload', 'result', 'output', 'fingerprint', 'idem'} & status.keys()
    assert instance.operation_row(identifier, principal)['id'] == identifier


class RecordingSocket:
    def __init__(self, peer=None):
        self.packets = []
        self.peer = peer

    async def send_text(self, packet):
        self.packets.append(self.peer.unpack(packet) if self.peer else packet)

    async def close(self, **kwargs):
        pass


@pytest.mark.asyncio
async def test_replaced_connection_is_rechecked_after_awaited_send_guard():
    entered, release = asyncio.Event(), asyncio.Event()
    socket = RecordingSocket()
    channel = SecureChannel(token(), token(), 'dev', 'hub')
    connection = Connection(socket, channel)

    async def guard():
        entered.set()
        await release.wait()
        return True

    sender = asyncio.create_task(connection.send({'type': 'call'}, before_send=guard))
    await asyncio.wait_for(entered.wait(), 1)
    connection.unusable = True
    release.set()
    with pytest.raises(ConnectionError):
        await sender
    assert socket.packets == [] and channel.sent == 0


@pytest.mark.asyncio
async def test_old_connection_cleanup_cannot_detach_new_authenticated_connection(runtime):
    instance, _ = runtime
    old, current = SimpleNamespace(unusable=True), SimpleNamespace(unusable=False)
    instance.connections['dev'] = current
    assert await instance.detach('dev', old) is False
    assert instance.detach_connection('dev', old) is False
    assert instance.connections['dev'] is current and not current.unusable


@pytest.mark.asyncio
async def test_legitimate_send_burst_retains_strict_sequence_without_qps_drops():
    secret, challenge = token(), token()
    peer = SecureChannel(secret, challenge, 'dev', 'agent')
    socket = RecordingSocket(peer)
    channel = SecureChannel(secret, challenge, 'dev', 'hub')
    connection = Connection(socket, channel)
    results = await asyncio.gather(*(connection.send({'type': 'ack', 'id': str(number)}) for number in range(128)))
    assert all(results) and channel.sent == peer.received == 128
    assert [packet['id'] for packet in socket.packets] == [str(number) for number in range(128)]
    assert not connection.unusable and not connection.lock.locked()


@pytest.mark.asyncio
async def test_cancelled_waiting_sender_does_not_invalidate_active_ciphertext():
    entered, release = asyncio.Event(), asyncio.Event()
    socket = RecordingSocket()
    channel = SecureChannel(token(), token(), 'dev', 'hub')
    connection = Connection(socket, channel)

    async def guard():
        entered.set()
        await release.wait()
        return True

    first = asyncio.create_task(connection.send({'type': 'first'}, before_send=guard))
    await asyncio.wait_for(entered.wait(), 1)
    waiting = asyncio.create_task(connection.send({'type': 'cancelled'}))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    assert await first
    assert await connection.send({'type': 'last'})
    assert channel.sent == 2 and len(socket.packets) == 2 and not connection.unusable
