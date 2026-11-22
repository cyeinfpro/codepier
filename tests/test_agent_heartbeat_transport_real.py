"""Real encrypted WebSockets survive bounded metadata/outbox I/O faults.

Only local temporary identities, state and a loopback peer are used. The main
socket, crypto, heartbeat, reconnect loop, journal and command execution remain
real; unrelated native-terminal side channels are disabled in this fixture.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import sys
import threading
import time

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import agent.runner as runner
from shared.crypto import SecureChannel, pack_channel, token, unpack_channel
from shared.tool_protocol import advertisement
from tests.fixture_hub import bind_listener
from tests.test_audit_agent import local_agent  # noqa: F401 - shared temporary Agent fixture


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def until(predicate, seconds=3):
    # Tests watch state from real sockets, child tasks and SQLite; these
    # predicates have no single producer event. Keep one cancellable timer.
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    pending = None

    def check():
        nonlocal pending
        if ready.done():
            return
        try:
            if predicate():
                ready.set_result(None)
            else:
                pending = loop.call_later(0.01, check)
        except Exception as exc:
            ready.set_exception(exc)

    pending = loop.call_soon(check)
    try:
        await asyncio.wait_for(ready, seconds)
    finally:
        pending.cancel()


class BlockedRead:
    """One real thread waits outside journal locks; always has a fail-safe bound."""

    def __init__(self, original, *, capture_before_wait=False):
        self.original = original
        self.capture_before_wait = capture_before_wait
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.threads = set()

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())
        try:
            saved = self.original(*args, **kwargs) if self.capture_before_wait else None
            self.entered.set()
            if not self.release.wait(8):
                raise TimeoutError('bounded synthetic read stall')
            return saved if self.capture_before_wait else self.original(*args, **kwargs)
        finally:
            with self.lock:
                self.active -= 1
            self.finished.set()


@dataclass
class Peer:
    socket: object
    channel: SecureChannel
    hello: dict
    frames: list = field(default_factory=list)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, body):
        async with self.write_lock:
            await self.socket.send(await pack_channel(self.channel, body))

    def packets(self, kind, identifier=None):
        return [body for _, body in self.frames if body.get('type') == kind
                and (identifier is None or body.get('id') == identifier)]


class LoopbackHub:
    def __init__(self, agent):
        self.agent = agent
        self.peers = []
        self.errors = []
        self.agent_task = None

    async def connection(self, socket):
        peer = None
        try:
            assert socket.request.path == '/agent/ws/' + self.agent.config['device_id']
            challenge = token()
            channel = SecureChannel(self.agent.config['secret'], challenge,
                                    self.agent.config['device_id'], 'hub')
            await socket.send(json.dumps({'type': 'challenge', 'challenge': challenge}))
            hello = await unpack_channel(channel, await asyncio.wait_for(socket.recv(), 3))
            assert hello['type'] == 'hello'
            peer = Peer(socket, channel, hello)
            await peer.send({'type': 'ready', 'channel_binding': hello['channel_binding'],
                             **advertisement(self.agent.build.catalog_sha256)})
            self.peers.append(peer)
            async for raw in socket:
                peer.frames.append((time.monotonic(), await unpack_channel(channel, raw)))
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            if peer is not None:
                peer.closed.set()

    async def ready_peer(self, number):
        await until(lambda: len(self.peers) >= number or self.errors)
        assert not self.errors
        peer = self.peers[number - 1]
        await until(lambda: len(peer.packets('heartbeat')) >= 3 or self.errors)
        assert not self.errors
        assert self.agent_task is not None and not self.agent_task.done()
        return peer


@asynccontextmanager
async def running_agent(agent, monkeypatch):
    async def unused_side_channel(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, 'HEARTBEAT_INTERVAL_SECONDS', 0.05)
    monkeypatch.setattr(runner, 'OUTBOX_INTERVAL_SECONDS', 0.05)
    agent.metadata.refresh_interval = 0.05
    agent.metadata.stale_after = 0.15
    monkeypatch.setattr(agent.native, 'sync', unused_side_channel)
    monkeypatch.setattr(agent.ssh_terminal, 'run', unused_side_channel)
    # Keep the actual reconnect loop; only shorten its jitter for a bounded test.
    monkeypatch.setattr(agent, '_reconnect_delay', lambda delay, previous: (0.02, 0.02))
    hub = LoopbackHub(agent)
    listener = bind_listener()
    port = listener.getsockname()[1]
    agent.config['hub_url'] = f'http://127.0.0.1:{port}'
    async with serve(hub.connection, sock=listener, ping_interval=None,
                     close_timeout=0.3, compression=None, max_size=16 * 1024 * 1024):
        hub.agent_task = asyncio.create_task(agent.run())
        try:
            yield hub
        finally:
            # Each caller releases injected threads before entering this cleanup.
            await asyncio.wait_for(agent.stop(), 3)
            await asyncio.wait_for(hub.agent_task, 5)
            assert not hub.errors


async def reconnect(hub, old_peer):
    before = time.monotonic()
    expected_peer = len(hub.peers) + 1
    await asyncio.wait_for(old_peer.socket.close(code=1011, reason='synthetic transport loss'), 1)
    peer = await hub.ready_peer(expected_peer)
    assert time.monotonic() - before < 2, 'reconnect waited for blocked optional I/O'
    return peer


async def test_metadata_stall_keeps_real_heartbeats_and_reconnect_single_flight(local_agent, monkeypatch):
    agent, _ = local_agent
    entered, release = asyncio.Event(), asyncio.Event()
    calls = active = peak = 0
    initial = agent.metadata.snapshot()
    initial["build"].pop("observation_state", None)

    async def blocked_read():
        nonlocal calls, active, peak
        calls += 1
        active += 1
        peak = max(peak, active)
        try:
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
            return initial
        finally:
            active -= 1

    monkeypatch.setattr(agent.metadata, "_read", blocked_read)
    async with running_agent(agent, monkeypatch) as hub:
        try:
            await until(entered.is_set)
            first = await hub.ready_peer(1)
            metadata_task, outbox_task = agent._metadata_task, agent._outbox_task
            assert active == 1
            second = await reconnect(hub, first)
            third = await reconnect(hub, second)
            assert len(third.packets("heartbeat")) >= 3
            assert agent._metadata_task is metadata_task and not metadata_task.done()
            assert agent._outbox_task is outbox_task and not outbox_task.done()
            assert calls == peak == active == 1
            assert not release.is_set()
            for peer in (first, second, third):
                assert all(
                    message["build"]["observation_state"] == "pending"
                    for message in peer.packets("heartbeat")
                )
            release.set()
            await until(
                lambda: any(
                    message["build"]["observation_state"] == "fresh"
                    for message in third.packets("heartbeat")
                )
            )
            assert peak == 1
        finally:
            release.set()


async def test_outbox_stall_preserves_heartbeat_and_discards_old_socket_page(local_agent, monkeypatch):
    agent, _ = local_agent
    identifier = 'old-socket-completed-operation'
    agent.journal.start(identifier, {'tool': 'tasks_run', 'args': {'fixture': True}})
    agent.journal.finish(identifier, {'ok': True, 'data': {'marker': 'completed-before-stall'}})
    blocked = BlockedRead(agent.journal.outbox, capture_before_wait=True)
    monkeypatch.setattr(agent.journal, 'outbox', blocked)
    async with running_agent(agent, monkeypatch) as hub:
        try:
            first = await hub.ready_peer(1)
            await until(blocked.entered.is_set)
            assert not first.packets('result', identifier)
            outbox_task = agent._outbox_task
            second = await reconnect(hub, first)
            assert blocked.calls == blocked.peak == blocked.active == 1
            assert agent._outbox_task is outbox_task and not outbox_task.done()
            # A genuine authenticated control ACK makes the frozen old page stale.
            # A new query must now see no unacknowledged result for this operation.
            await second.send({'type': 'ack', 'id': identifier})

            def acknowledged():
                with agent.journal.lock:
                    return agent.journal.db.execute('SELECT acked FROM calls WHERE id=?',
                                                    (identifier,)).fetchone()[0] == 1

            await until(acknowledged)
            heartbeat_count = len(second.packets('heartbeat'))
            blocked.release.set()
            await until(lambda: blocked.calls >= 2 and len(second.packets('heartbeat')) >= heartbeat_count + 4)
            assert not second.packets('result', identifier), 'old socket page escaped into the new connection'
            assert blocked.peak == 1
            assert threading.get_ident() not in blocked.threads
        finally:
            blocked.release.set()


async def test_reconnect_replays_receipt_without_reexecuting_real_command(local_agent, monkeypatch):
    agent, root = local_agent
    marker = root / 'execution-count.txt'
    agent.config['tasks']['once'] = {
        'command': [sys.executable, '-c',
                    "from pathlib import Path; p=Path('execution-count.txt'); "
                    "f=p.open('a'); f.write('executed\\n'); f.close(); print('fixture-complete')"],
        'projects': ['fixture'], 'timeout': 3,
    }
    request = {'type': 'call', 'id': 'real-once-command', 'tool': 'tasks_run',
               'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
               'args': {'project': 'fixture', 'task': 'once', 'idempotency_key': 'real-once-command'}}
    async with running_agent(agent, monkeypatch) as hub:
        first = await hub.ready_peer(1)
        await first.send(request)
        await until(lambda: bool(first.packets('result', request['id'])))
        original = first.packets('result', request['id'])[0]['result']
        assert original['ok'], original
        assert original['data']['exit_code'] == 0
        assert marker.read_text() == 'executed\n'
        # The result deliberately remains unacknowledged across a real reconnect.
        second = await reconnect(hub, first)
        await until(lambda: bool(second.packets('result', request['id'])))
        assert second.packets('result', request['id'])[0]['result'] == original
        assert marker.read_text() == 'executed\n'
        await second.send({'type': 'ack', 'id': request['id']})
        before = len(second.packets('result', request['id']))
        await second.send(request)
        await until(lambda: len(second.packets('result', request['id'])) > before)
        assert second.packets('result', request['id'])[-1]['result'] == original
        assert marker.read_text() == 'executed\n'
        assert agent.journal.status(request['id'])['status'] == 'done'
