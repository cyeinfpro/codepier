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
    assert await instance.detach('dev', old) is False
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


@pytest.mark.asyncio
async def test_unregistered_connection_detach_closes_its_reply_writer(runtime):
    instance, _ = runtime
    socket = RecordingSocket()
    unregistered = Connection(socket, SecureChannel(token(), token(), 'dev', 'hub'))
    await unregistered.send({'type': 'ready'})
    writer = unregistered._writer_task
    assert writer is not None and not writer.done()
    assert not await instance.detach('dev', unregistered)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writer, 1)
    assert unregistered.unusable and not instance.connections


@pytest.mark.asyncio
async def test_reconnect_advisory_does_not_leave_queued_calls_on_wire():
    secret, challenge = token(), token()
    socket = RecordingSocket(SecureChannel(secret, challenge, 'dev', 'agent'))
    connection = Connection(socket, SecureChannel(secret, challenge, 'dev', 'hub'))
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_until_queued():
        entered.set()
        await release.wait()
        return True

    first = asyncio.create_task(connection.send({'type': 'ack'}, before_send=wait_until_queued))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert connection.send_nowait({'type': 'call', 'id': 'queued'})
        notice = asyncio.create_task(connection.send_shutdown(5))
        release.set()
        await asyncio.wait_for(asyncio.gather(first, notice), 1)
        await connection.flush_control()
        assert [packet['type'] for packet in socket.packets] == ['ack', 'going_away']
    finally:
        release.set()
        connection.close()


def test_presence_does_not_resurrect_deleted_device_or_cross_a_reconnect(runtime):
    instance, _ = runtime
    now = time.time()
    instance._upsert_device_presence('dev', now, {'load': 10})
    assert instance.device_runtime_snapshot('dev') == {'load': 10}
    connection = Connection(RecordingSocket(), SecureChannel(token(), token(), 'dev', 'hub'))
    instance._attach_connection('dev', connection)
    assert instance.device_runtime_snapshot('dev', {'scheduler': {'load': 10}}) is None
    row = instance.store.one("SELECT * FROM devices WHERE id='dev'")
    assert instance._register_agent('dev', connection, {}, row)
    assert instance.store.one("SELECT runtime_info FROM device_presence WHERE device_id='dev'")[
        'runtime_info'
    ] is None
    instance.store.execute("DELETE FROM projects WHERE device_id='dev'")
    with instance.store.transaction():
        instance.store.execute("DELETE FROM device_presence WHERE device_id='dev'")
        instance.store.execute("DELETE FROM devices WHERE id='dev'")
    assert instance._detach_fence('dev', connection)
    instance._record_disconnect('dev', connection)
    assert instance.store.one("SELECT * FROM device_presence WHERE device_id='dev'") is None


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['send', 'receive'])
async def test_sequence_counters_remain_owned_while_crypto_is_offloaded(monkeypatch, direction):
    import threading

    secret, challenge = token(), token()
    peer = SecureChannel(secret, challenge, 'dev', 'agent')
    channel = SecureChannel(secret, challenge, 'dev', 'hub')
    entering, resume = threading.Event(), threading.Event()
    body = {'text': 'synthetic'}
    incoming = peer.pack(body)
    async_call = channel.pack_async if direction == 'send' else channel.unpack_async
    sync_call = channel.pack if direction == 'send' else channel.unpack
    value = body if direction == 'send' else incoming
    name = '_pack_sequence' if direction == 'send' else '_unpack_sequence'
    process = getattr(channel, name)

    def paused(*args):
        entering.set()
        assert resume.wait(2)
        return process(*args)

    monkeypatch.setattr(channel, name, paused)
    task = asyncio.create_task(async_call(value, threshold=0))
    try:
        assert await asyncio.to_thread(entering.wait, 1)
        with pytest.raises(RuntimeError, match="sequence already in use"):
            sync_call(value)
    finally:
        resume.set()
        result = await asyncio.wait_for(task, 1)
    assert (peer.unpack(result) if direction == 'send' else result) == body
    assert (channel.sent if direction == 'send' else channel.received) == 1


def test_json_sample_skips_enumeration_for_wide_containers():
    from shared.crypto import _exceeds_json_threshold

    class WideList(list):
        def __iter__(self):
            raise AssertionError('wide list must not be enumerated')

    class WideDictionary(dict):
        def items(self):
            raise AssertionError('wide dictionary must not be enumerated')

    assert _exceeds_json_threshold({'sample': WideList(range(3000))})
    assert _exceeds_json_threshold(WideDictionary.fromkeys(range(3000)))
    assert not _exceeds_json_threshold({'sample': ['a', 'b', 'c']})


def test_device_delete_route_removes_presence_with_a_connection_open(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hub.app import create_app

    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    app = create_app(str(tmp_path / 'hub'))
    store, runtime, auth = app.state.store, app.state.runtime, app.state.auth
    seed_owner(store, 'owner', 'owner')
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)",
        (store.encrypt(token()), time.time()),
    )
    connection = Connection(RecordingSocket(), SecureChannel(token(), token(), 'dev', 'hub'))
    now = time.time()
    runtime._upsert_device_presence('dev', now, {'load': 10})
    runtime._attach_connection('dev', connection)
    assert runtime.device_runtime_snapshot('dev') is None
    with store.transaction():
        session = auth.new_session('owner')
    with TestClient(app) as client:
        client.headers.update({
            'Cookie': 'rd_session=' + session['cookie'],
            'X-RD-CSRF': session['csrf'],
            'X-CodePier-Space': 'legacy',
        })
        response = client.delete('/api/devices/dev')
        assert response.status_code == 200, response.text
        assert store.one("SELECT * FROM device_presence WHERE device_id='dev'") is None
        assert connection.unusable and 'dev' not in runtime.connections
        assert runtime._receive_agent_frame('dev', connection, {}, {'type': 'heartbeat'}) == (
            False, None,
        )
        assert store.one("SELECT * FROM device_presence WHERE device_id='dev'") is None


def test_registration_snapshot_is_never_visible_before_commit(runtime):
    instance, _ = runtime
    connection = Connection(RecordingSocket(), SecureChannel(token(), token(), 'dev', 'hub'))
    instance._attach_connection('dev', connection)
    fixture = instance.store.one("SELECT * FROM devices WHERE id='dev'")
    with pytest.raises(ValueError, match='rollback'):
        with instance.store.transaction():
            assert instance._register_agent('dev', connection, {'version': 'next'}, fixture)
            assert connection.registration is None
            raise ValueError('rollback')
    assert connection.registration is None
    assert instance._register_agent('dev', connection, {'version': 'next'}, fixture)
    assert connection.registration[1]['version'] == 'next'
