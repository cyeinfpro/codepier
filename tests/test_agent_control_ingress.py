"""Authenticated frames/final send fences share the fair DB control lane."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from hub.auth import Auth
from hub.principal import Principal
from hub.operation_history import PREFIX, unseal
from hub.runtime import Connection, Runtime
from hub.store import Store
from shared.contracts import TOOLS
from shared.crypto import SecureChannel
from shared.tool_protocol import advertisement, catalog_digest
from tests.legacy_iam_fixture import seed_owner


@pytest.fixture
def ingress(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store)
    secret = 'synthetic-control-device-secret-for-fixtures-only'
    store.execute("""INSERT INTO devices(id,name,secret,created,space_id,owner_user_id)
        VALUES ('dev','Fixture',?,?,'legacy','owner')""",
                  (store.encrypt(secret), time.time()))
    store.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id)
        VALUES ('proj','Fixture','fixture','dev',?,'write',1,?,'legacy','owner')''', (str(tmp_path / 'project'), time.time()))
    runtime = Runtime(store)
    owner = Principal('panel:owner', 'owner', {'read', 'write', 'execute'}, ['*'], admin=True)
    grant = Auth(store).issue_grant(owner, 'fixture-control', ['read', 'execute'], ['proj'], 30)
    principal = runtime.grant_principal(store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],)))
    yield SimpleNamespace(store=store, runtime=runtime, secret=secret, principal=principal, grant=grant)
    store.close()


def admit(fixture, key='fixture-control'):
    args = TOOLS['shell_exec'].model.model_validate({
        'project': 'Fixture', 'command': 'synthetic command', 'idempotency_key': key}).model_dump()
    project = fixture.runtime.project('Fixture', fixture.principal)
    return fixture.runtime._admit_operation('shell_exec', args, project, fixture.principal)[0]


def result_frame(identifier):
    return {'type': 'result', 'id': identifier,
            'result': {'ok': True, 'data': {'exit_code': 0, 'output': 'final fixture tail'}}}


class PlainSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, value):
        self.sent.append(json.loads(value))

    async def close(self, **_):
        pass


def connection(fixture):
    peer = Connection(PlainSocket(), SimpleNamespace(pack=json.dumps))
    peer.device_secret = fixture.store.one("SELECT secret FROM devices WHERE id='dev'")['secret']
    peer.journal_id = 'fixture-control-journal'
    fixture.runtime.connections['dev'] = peer
    return peer


async def queued(store, count):
    async with asyncio.timeout(5):
        while store.stats_snapshot()['worker']['queued'] < count:
            await asyncio.sleep(0)


async def hold_worker(store):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10)

    task = asyncio.create_task(store.run(hold))
    assert await asyncio.to_thread(entered.wait, 5)
    return task, release


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['replacement', 'secret', 'device', 'owner'])
async def test_queued_frame_rechecks_current_connection_and_identity(ingress, change):
    e = ingress
    identifier = admit(e)
    peer = connection(e)
    first, release = await hold_worker(e.store)
    frame = asyncio.create_task(e.store.run_control(
        e.runtime._receive_agent_frame, 'dev', peer, {}, result_frame(identifier)))
    try:
        await queued(e.store, 1)
        before = e.store.one("SELECT last_seen FROM devices WHERE id='dev'")
        if change == 'replacement':
            connection(e)
        elif change == 'secret':
            e.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (e.store.encrypt('rotated fixture'),))
        elif change == 'device':
            e.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
        else:
            e.store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    finally:
        release.set()
        await first
    assert await frame == (False, None)
    assert e.store.one("SELECT last_seen FROM devices WHERE id='dev'") == before
    assert e.store.one('SELECT result FROM operations WHERE id=?', (identifier,))['result'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['replacement', 'secret', 'device', 'grant', 'mapping', 'cancel'])
async def test_final_control_guard_rechecks_after_database_queue_wait(ingress, change):
    e = ingress
    peer = connection(e)
    identifier = admit(e)
    prepared = e.runtime._prepare_delivery(identifier)
    first, release = await hold_worker(e.store)
    sending = asyncio.create_task(e.runtime._send_prepared(identifier, prepared))
    try:
        await queued(e.store, 1)
        assert peer.lock.locked()
        if change == 'replacement':
            connection(e)
        elif change == 'secret':
            e.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (e.store.encrypt('rotated fixture'),))
        elif change == 'device':
            e.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
        elif change == 'grant':
            e.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (e.grant['grant_id'],))
        elif change == 'mapping':
            e.store.execute("UPDATE projects SET root='/different' WHERE id='proj'")
        else:
            e.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?', (identifier,))
    finally:
        release.set()
        await asyncio.gather(first, sending)
    assert peer.socket.sent == []


@pytest.mark.asyncio
async def test_socket_ack_is_not_stranded_behind_normal_queued_send_guard(ingress):
    e = ingress
    peer = connection(e)
    sending_id, finished_id = admit(e, 'control-sending'), admit(e, 'control-finishing')
    prepared = e.runtime._prepare_delivery(sending_id)
    first, release_first = await hold_worker(e.store)
    normal_entered, release_normal = threading.Event(), threading.Event()

    def normal():
        normal_entered.set()
        assert release_normal.wait(10)

    ordinary = asyncio.create_task(e.store.run(normal))
    tasks = [first, ordinary]
    try:
        await queued(e.store, 1)
        sending = asyncio.create_task(e.runtime._send_prepared(sending_id, prepared))
        tasks.append(sending)
        await queued(e.store, 2)
        assert peer.lock.locked()

        async def finish():
            current, reply = await e.store.run_control(
                e.runtime._receive_agent_frame, 'dev', peer, {}, result_frame(finished_id))
            assert current and reply == {'type': 'ack', 'id': finished_id}
            await peer.send(reply)

        finishing = asyncio.create_task(finish())
        tasks.append(finishing)
        await queued(e.store, 3)
        release_first.set()
        # The ordinary phase is now blocked, but both the final send fence and
        # durable completion have run, so the receipt ACK can use the socket.
        await asyncio.wait_for(asyncio.shield(finishing), 5)
        assert await asyncio.to_thread(normal_entered.wait, 5)
        assert not release_normal.is_set()
        assert peer.socket.sent[-1] == {'type': 'ack', 'id': finished_id}
        with sqlite3.connect(e.store.state_directory / 'hub.sqlite3') as reader:
            assert reader.execute('SELECT state FROM operations WHERE id=?', (finished_id,)).fetchone()[0] == 'succeeded'
    finally:
        release_first.set()
        release_normal.set()
        await asyncio.gather(*tasks)


class AuthenticatedSocket:
    """Real channel/handshake and Runtime receive loop; no network listener."""
    def __init__(self, fixture, frames):
        self.fixture, self.frames = fixture, list(frames)
        self.channel = None
        self.hello = True
        self.sent = []
        self.ack_state = []

    async def accept(self):
        pass

    async def send_json(self, value):
        self.channel = SecureChannel(self.fixture.secret, value['challenge'], 'dev', 'agent')

    async def receive_text(self):
        if self.hello:
            self.hello = False
            return self.channel.pack({'type': 'hello', 'journal_id': 'fixture-control-journal',
                'delivery_protocol': 2, **advertisement(catalog_digest())})
        if not self.frames:
            raise WebSocketDisconnect(code=1000)
        return self.channel.pack(self.frames.pop(0))

    async def send_text(self, value):
        frame = self.channel.unpack(value)
        self.sent.append(frame)
        if frame['type'] == 'ack':
            with sqlite3.connect(self.fixture.store.state_directory / 'hub.sqlite3') as reader:
                persisted = reader.execute(
                    'SELECT state,output,result FROM operations WHERE id=?', (frame['id'],)).fetchone()
                # Read through a separate connection at the exact ACK boundary:
                # decrypt these committed bytes, never an in-memory Runtime view.
                assert persisted[1].startswith(PREFIX) and 'final fixture tail' not in persisted[1]
                assert persisted[2].startswith(PREFIX)
                assert json.loads(unseal(self.fixture.store, persisted[2]))['ok'] is True
                self.ack_state.append((
                    (persisted[0], unseal(self.fixture.store, persisted[1])),
                    reader.execute("SELECT value FROM meta WHERE key='control-outbox'").fetchone()))

    async def close(self, **_):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_outbox', [False, True])
async def test_real_receive_loop_queues_once_and_ack_follows_durable_outbox(ingress, monkeypatch, fail_outbox):
    e = ingress
    identifier = admit(e)
    frames = [
        {'type': 'accepted', 'id': identifier},
        {'type': 'started', 'id': identifier},
        {'type': 'output', 'id': identifier, 'snapshot': True, 'seq': 3, 'text': 'live fixture tail'},
        result_frame(identifier),
    ]
    socket = AuthenticatedSocket(e, frames)
    submitted = []
    original = e.store.run_control

    async def observe(function, /, *args, **kwargs):
        submitted.append(function.__name__)
        return await original(function, *args, **kwargs)

    def outbox(completed):
        assert completed == identifier
        e.store.execute("INSERT INTO meta VALUES ('control-outbox','committed')")
        if fail_outbox:
            raise sqlite3.OperationalError('synthetic outbox failure')

    monkeypatch.setattr(e.store, 'run_control', observe)
    monkeypatch.setattr(e.runtime.collaboration, 'events', SimpleNamespace(operation_completed=outbox))
    await e.runtime.agent_socket(socket, 'dev')
    assert submitted == ['_receive_agent_frame'] * len(frames)
    if fail_outbox:
        assert socket.ack_state == []
        row = e.store.one('SELECT result,payload FROM operations WHERE id=?', (identifier,))
        assert row['result'] is None and row['payload'] is not None
        assert e.store.one("SELECT value FROM meta WHERE key='control-outbox'") is None
        with sqlite3.connect(e.store.state_directory / 'hub.sqlite3') as reader:
            output, sealed, chars = reader.execute(
                'SELECT output,sealed_output,output_chars FROM operation_output_tails WHERE operation_id=?',
                (identifier,)).fetchone()
        assert output == '' and sealed.startswith(PREFIX) and 'live fixture tail' not in sealed
        assert unseal(e.store, sealed) == 'live fixture tail'
        assert chars == len('live fixture tail')
    else:
        assert socket.ack_state == [(('succeeded', 'final fixture tail'), ('committed',))]
        assert not e.store.all('SELECT * FROM operation_output_tails')


@pytest.mark.asyncio
async def test_audit_failure_after_commit_replays_ack_without_repeating_completion(ingress, monkeypatch):
    e = ingress
    identifier = admit(e)
    completed = []
    audit = e.store.audit

    def outbox(value):
        completed.append(value)
        e.store.execute("INSERT INTO meta VALUES ('control-outbox','committed')")

    def fail_auxiliary_audit(actor, action, *args, **kwargs):
        if action == 'shell_exec':
            raise OSError('synthetic auxiliary audit failure')
        return audit(actor, action, *args, **kwargs)

    monkeypatch.setattr(e.runtime.collaboration, 'events', SimpleNamespace(operation_completed=outbox))
    monkeypatch.setattr(e.store, 'audit', fail_auxiliary_audit)
    first = AuthenticatedSocket(e, [result_frame(identifier)])
    await e.runtime.agent_socket(first, 'dev')
    assert first.ack_state == []
    persisted = e.store.one('SELECT state,result,output,output_seq FROM operations WHERE id=?', (identifier,))
    assert persisted['state'] == 'succeeded'
    assert completed == [identifier]
    # The Agent keeps its result until ACK. A new authenticated connection can
    # replay that receipt without reverting or repeating a committed result.
    second = AuthenticatedSocket(e, [result_frame(identifier)])
    await e.runtime.agent_socket(second, 'dev')
    assert second.ack_state == [(('succeeded', 'final fixture tail'), ('committed',))]
    assert e.store.one('SELECT state,result,output,output_seq FROM operations WHERE id=?', (identifier,)) == persisted
    assert completed == [identifier]
