"""Deterministic mock concurrency tests plus one explicitly marked loopback test."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from hub.gateway import policy
from hub.gateway.remote import BackendError, RemotePool, Session
from hub.gateway.service import Gateway
from shared.mcp_protocol import MODERN
from shared.util import DevError

CONFIG = {'endpoint': 'https://mcp.example/mcp', 'protocol': 'auto',
          'networks': '[]', 'allow_http': 0}
TOOL = {'name': 'run', 'description': 'Fixture only.', 'inputSchema': {'type': 'object'}}


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


class Backend:
    def __init__(self, *, modern=True, modern_session=False):
        self.modern = modern
        self.modern_session = modern_session
        self.calls = []
        self.methods = []
        self.hosts = []
        self.headers = []
        self.active = 0
        self.peak = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.probe_gate = asyncio.Event()
        self.probe_gate.set()
        self.call_hook = None
        self.pages = False

    async def handle(self, request):
        body = json.loads(request.content)
        method = body['method']
        self.methods.append(method)
        headers = {}
        if method == 'server/discover':
            await self.probe_gate.wait()
            if not self.modern:
                return httpx.Response(400, json={'jsonrpc': '2.0', 'id': body['id'],
                                                'error': {'code': -32601}}, request=request)
            result = {'supportedVersions': [MODERN], 'capabilities': {'tools': {}}}
            if self.modern_session:
                headers['Mcp-Session-Id'] = 'unexpected-stateful'
        elif method == 'initialize':
            headers['Mcp-Session-Id'] = 'legacy-session'
            result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}}
        elif method == 'notifications/initialized':
            return httpx.Response(202, request=request)
        elif method == 'tools/list':
            cursor = body['params'].get('cursor')
            result = {'tools': [dict(TOOL, name='next' if cursor else 'run')]}
            if self.pages and not cursor:
                result['nextCursor'] = 'page-2'
            await self.gate.wait()
        elif method == 'tools/call':
            number = body['params']['arguments'].get('number', 0)
            self.calls.append(number)
            self.hosts.append(request.url.host)
            self.headers.append(dict(request.headers))
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.call_hook:
                    response = await self.call_hook(number, request)
                    if response is not None:
                        return response
                await self.gate.wait()
                result = {'content': [{'type': 'text', 'text': str(number)}]}
            finally:
                self.active -= 1
        else:
            raise AssertionError(method)
        return httpx.Response(200, headers=headers,
                              json={'jsonrpc': '2.0', 'id': body['id'], 'result': result}, request=request)


def make_session(backend, *, concurrency=4):
    lookups = []
    address = ['93.184.216.34']

    async def resolve(host, port):
        lookups.append(address[0])
        return list(address)

    session = Session(CONFIG, 'synthetic-fixture', resolver=resolve,
                      transport=httpx.MockTransport(backend.handle), concurrency=concurrency)
    return session, lookups, address


def call(session, number, callback=lambda: None):
    return session.call('run', {'number': number}, callback)


@pytest.mark.asyncio
@pytest.mark.parametrize(('modern', 'session_header', 'expected'), [
    (True, False, 4), (False, False, 1), (True, True, 1),
])
async def test_bounded_parallelism_fifo_and_shared_transport(modern, session_header, expected):
    backend = Backend(modern=modern, modern_session=session_header)
    backend.gate.clear()
    session, lookups, _ = make_session(backend)
    tasks = [asyncio.create_task(call(session, number)) for number in range(12)]
    await until(lambda: backend.active == expected)
    original = session.client
    assert session.active == expected
    assert len(backend.calls) == expected
    backend.gate.set()
    await asyncio.gather(*tasks)
    assert backend.calls == list(range(12))
    assert backend.peak == expected
    assert session.client is original and not original.is_closed
    assert len(lookups) == 1 and backend.methods.count('server/discover') == 1
    assert session.stats['admitted'] == 12
    assert session.stats['peak_active'] == expected
    assert session.stats['queued_seconds'] >= 0 and session.stats['active_seconds'] > 0
    assert session.active == 0 and not session.waiters
    await session.close()
    assert original.is_closed


@pytest.mark.asyncio
async def test_unknown_protocol_initializes_once_before_any_send():
    backend = Backend()
    backend.probe_gate.clear()
    session, lookups, _ = make_session(backend)
    tasks = [asyncio.create_task(call(session, number)) for number in range(8)]
    await until(lambda: backend.methods == ['server/discover'])
    assert not backend.calls
    backend.probe_gate.set()
    await asyncio.gather(*tasks)
    assert len(lookups) == 1
    assert backend.methods.count('server/discover') == 1
    await session.close()


@pytest.mark.asyncio
async def test_cancelled_queue_waiter_does_not_take_a_slot_or_check_authority():
    backend = Backend()
    backend.gate.clear()
    session, _, _ = make_session(backend, concurrency=1)
    first = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    checked = []
    cancelled = asyncio.create_task(call(session, 2, lambda: checked.append(2)))
    follower = asyncio.create_task(call(session, 3))
    await until(lambda: len(session.waiters) == 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not checked and not session.invalid
    backend.gate.set()
    await asyncio.gather(first, follower)
    assert backend.calls == [1, 3]
    assert session.active == 0 and not session.waiters
    await session.close()


@pytest.mark.asyncio
async def test_revocation_after_queueing_is_checked_before_send():
    backend = Backend()
    backend.gate.clear()
    session, _, _ = make_session(backend, concurrency=1)
    first = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    revoked = False

    async def authorize():
        if revoked:
            raise DevError('REVOKED', 'revoked', 403)

    second = asyncio.create_task(call(session, 2, authorize))
    await until(lambda: len(session.waiters) == 1)
    revoked = True
    backend.gate.set()
    await first
    with pytest.raises(DevError, match='revoked'):
        await second
    original = session.client
    await call(session, 3)
    assert backend.calls == [1, 3]
    assert session.client is original and not session.invalid
    await session.close()


@pytest.mark.asyncio
async def test_failed_or_cancelled_authorization_leaves_sibling_and_client_intact():
    backend = Backend()
    backend.gate.clear()
    session, _, _ = make_session(backend, concurrency=2)
    first = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    original = session.client

    def denied():
        raise DevError('REVOKED', 'revoked', 403)

    with pytest.raises(DevError):
        await call(session, 2, denied)
    entered = asyncio.Event()

    async def pending_authority():
        entered.set()
        await asyncio.Event().wait()

    cancelled = asyncio.create_task(call(session, 3, pending_authority))
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert session.active == 1
    assert session.client is original and not original.is_closed and not session.invalid
    backend.gate.set()
    await first
    await call(session, 4)
    assert backend.calls == [1, 4]
    await session.close()


@pytest.mark.asyncio
async def test_expired_dns_waits_for_active_calls_then_rotates_once():
    backend = Backend()
    backend.gate.clear()
    session, lookups, address = make_session(backend)
    active = [asyncio.create_task(call(session, number)) for number in (1, 2)]
    await until(lambda: backend.active == 2)
    original = session.client
    session.connected_at -= 3600
    address[0] = '93.184.216.35'
    waiting = asyncio.create_task(call(session, 3))
    await until(lambda: len(session.waiters) == 1)
    assert backend.calls == [1, 2]
    assert not original.is_closed and len(lookups) == 1
    backend.gate.set()
    await asyncio.gather(*active, waiting)
    assert original.is_closed and session.client is not original
    assert lookups == ['93.184.216.34', '93.184.216.35']
    assert backend.hosts == ['93.184.216.34', '93.184.216.34', '93.184.216.35']
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_sent_failure_never_replays_or_closes_active_sibling(cancel):
    backend = Backend()
    backend.gate.clear()
    fail = asyncio.Event()

    async def hook(number, request):
        if number == 2:
            await fail.wait()
            raise httpx.ReadTimeout('synthetic unknown', request=request)

    backend.call_hook = hook
    session, lookups, _ = make_session(backend, concurrency=2)
    sibling = asyncio.create_task(call(session, 1))
    failed = asyncio.create_task(call(session, 2))
    await until(lambda: backend.active == 2)
    original = session.client
    if cancel:
        failed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await failed
    else:
        fail.set()
        with pytest.raises(BackendError):
            await failed
    waiting = asyncio.create_task(call(session, 3))
    await until(lambda: len(session.waiters) == 1)
    assert backend.calls == [1, 2] and not original.is_closed
    backend.gate.set()
    await asyncio.gather(sibling, waiting)
    assert backend.calls == [1, 2, 3] and len(lookups) == 2
    assert original.is_closed
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 500, 503, 302])
async def test_call_errors_are_not_retried_and_next_call_reuses_transport(status):
    backend = Backend()

    async def hook(number, request):
        return httpx.Response(status, request=request) if number == 1 else None

    backend.call_hook = hook
    session, lookups, _ = make_session(backend)
    with pytest.raises(BackendError):
        await call(session, 1)
    original = session.client
    await call(session, 2)
    assert backend.calls == [1, 2] and session.client is original and len(lookups) == 1
    await session.close()


@pytest.mark.asyncio
async def test_discovery_is_fifo_exclusive_across_all_catalog_pages():
    backend = Backend()
    backend.pages = True
    backend.gate.clear()
    session, _, _ = make_session(backend)
    first = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    discovery = asyncio.create_task(session.tools())
    await until(lambda: len(session.waiters) == 1)
    later = [asyncio.create_task(call(session, number)) for number in range(2, 8)]
    await until(lambda: len(session.waiters) == 7)
    assert backend.methods == ['server/discover', 'tools/call']
    backend.gate.set()
    await asyncio.gather(first, discovery, *later)
    assert backend.methods[2:4] == ['tools/list', 'tools/list']
    # review_tools canonicalizes the completed multi-page catalog by name.
    assert [tool['name'] for tool in discovery.result()] == ['next', 'run']
    assert backend.calls == list(range(1, 8))
    await session.close()


@pytest.mark.asyncio
async def test_bounded_shutdown_rejects_queue_and_defers_active_close():
    backend = Backend()
    backend.gate.clear()
    session, _, _ = make_session(backend, concurrency=1)
    first = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    original = session.client
    waiting = asyncio.create_task(call(session, 2))
    await until(lambda: len(session.waiters) == 1)
    await asyncio.wait_for(session.close(drain_seconds=0.01), 1)
    with pytest.raises(DevError) as error:
        await waiting
    assert error.value.code == 'GATEWAY_CLOSED'
    assert not original.is_closed and session.active == 1
    backend.gate.set()
    await first
    assert original.is_closed and session.client is None
    assert backend.calls == [1]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_close_during_initialization_is_bounded_and_never_sends_call(cancel):
    backend = Backend()
    backend.probe_gate.clear()
    session, _, _ = make_session(backend)
    task = asyncio.create_task(call(session, 1))
    await until(lambda: backend.methods == ['server/discover'])
    original = session.client
    await asyncio.wait_for(session.close(drain_seconds=0.01), 1)
    assert not original.is_closed
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        backend.probe_gate.set()
        with pytest.raises(DevError):
            await task
    assert not backend.calls and original.is_closed
    assert session.active == 0 and not session.waiters


@pytest.mark.asyncio
async def test_pool_shutdown_does_not_wait_under_global_lock():
    backend = Backend()
    backend.gate.clear()

    async def resolve(host, port):
        return ['93.184.216.34']

    pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(backend.handle))
    key = ('space-a', 'user-a', 'grant-a', 'binding-a', 'account-a')
    session = await pool.get(key, CONFIG, 'synthetic-a')
    task = asyncio.create_task(call(session, 1))
    await until(lambda: backend.calls == [1])
    original = session.client
    await asyncio.wait_for(pool.close(drain_seconds=0.01), 1)
    with pytest.raises(DevError) as error:
        await pool.get(key, CONFIG, 'synthetic-a')
    assert error.value.code == 'GATEWAY_CLOSED'
    await asyncio.wait_for(pool.release(key), 1)
    assert not original.is_closed
    backend.gate.set()
    await task
    assert original.is_closed and not pool.sessions and not pool.active


@pytest.mark.asyncio
async def test_pool_isolation_and_idle_eviction_do_not_evict_active_identity():
    backend = Backend()

    async def resolve(host, port):
        return ['93.184.216.34']

    pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(backend.handle), limit=2)
    first = await pool.get(('space', 'user', 'grant-a'), CONFIG, 'synthetic-a')
    second = await pool.get(('space', 'user', 'grant-b'), CONFIG, 'synthetic-b')
    assert first is not second
    await asyncio.gather(call(first, 1), call(second, 2))
    assert {header['authorization'] for header in backend.headers} == {'Bearer synthetic-a', 'Bearer synthetic-b'}
    with pytest.raises(DevError):
        await pool.get(('space', 'user', 'grant-c'), CONFIG, 'synthetic-c')
    await pool.release(('space', 'user', 'grant-b'))
    old_client = second.client
    third = await pool.get(('space', 'user', 'grant-c'), CONFIG, 'synthetic-c')
    assert third is not first and old_client.is_closed
    assert not first.client.is_closed
    await pool.release(('space', 'user', 'grant-a'))
    await pool.release(('space', 'user', 'grant-c'))
    await pool.close()


class MemoryStore:
    """Real in-memory SQL receipts with synchronous authorization, no Hub server."""
    def __init__(self):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE gateway_calls (
            id TEXT PRIMARY KEY, space_id TEXT, user_id TEXT, grant_id TEXT,
            binding_id TEXT, tool TEXT, tool_hash TEXT, state TEXT, created REAL,
            updated REAL, request_key TEXT, fingerprint TEXT, result TEXT,
            error_code TEXT)""")

    @contextmanager
    def transaction(self):
        with self.lock:
            yield
            self.db.commit()

    async def run(self, function, *args):
        await asyncio.sleep(0)
        return function(*args)

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def execute(self, sql, args=()):
        with self.transaction():
            self.db.execute(sql, args)

    def encrypt(self, value):
        return value

    def decrypt(self, value):
        return value


def make_gateway(monkeypatch, *, concurrency=1):
    backend = Backend()
    store = MemoryStore()
    principal = SimpleNamespace(space_id='space', user_id='user', grant_id='grant')
    binding = {'id': 'binding', 'version': 1}
    account = {'id': 'account', 'version': 1, 'catalog_hash': 'catalog-1', 'secret': 'synthetic-a'}
    connector = {**CONFIG, 'id': 'connector', 'version': 1}
    state = {'revoked': False}

    def require(*args):
        if state['revoked']:
            raise DevError('GATEWAY_REVOKED', 'revoked', 403)
        return principal, copy.deepcopy(binding), copy.deepcopy(account), copy.deepcopy(connector), copy.deepcopy(TOOL)

    async def resolve(host, port):
        return ['93.184.216.34']

    async def validate(*args):
        return None

    monkeypatch.setattr(policy, 'require_tool', require)
    gateway = Gateway.__new__(Gateway)
    gateway.store, gateway.enabled, gateway.inflight = store, True, 0
    gateway.secret = b'synthetic-receipt-test'
    gateway.validator = SimpleNamespace(validate=validate)
    gateway.resolve = require
    gateway.pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(backend.handle),
                              concurrency=concurrency)
    return gateway, principal, backend, state, binding, account, connector


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoke', 'binding', 'account', 'catalog', 'connector'])
async def test_gateway_rechecks_queued_identity_versions_and_stores_not_sent(monkeypatch, change):
    gateway, principal, backend, state, binding, account, connector = make_gateway(monkeypatch)
    backend.gate.clear()
    first = asyncio.create_task(gateway.call(principal, 'run', {'number': 1}, lambda: principal))
    await until(lambda: backend.calls == [1])
    session = next(iter(gateway.pool.sessions.values()))
    waiting = asyncio.create_task(gateway.call(principal, 'run', {'number': 2}, lambda: principal))
    await until(lambda: len(session.waiters) == 1)
    if change == 'revoke':
        state['revoked'] = True
    elif change == 'catalog':
        account['catalog_hash'] = 'catalog-2'
    else:
        {'binding': binding, 'account': account, 'connector': connector}[change]['version'] += 1
    backend.gate.set()
    outcomes = await asyncio.gather(first, waiting, return_exceptions=True)
    assert all(isinstance(result, DevError) for result in outcomes)
    assert backend.calls == [1]
    rows = [dict(row) for row in gateway.store.db.execute('SELECT * FROM gateway_calls ORDER BY created')]
    assert rows[0]['state'] == 'unknown'
    assert rows[1]['state'] == 'rejected'
    assert gateway.inflight == 0 and not gateway.pool.active and session.active == 0
    await gateway.close()
    gateway.store.db.close()


@pytest.mark.asyncio
async def test_gateway_duplicate_key_returns_receipt_without_sending_again(monkeypatch):
    gateway, principal, backend, *_ = make_gateway(monkeypatch)
    first = await gateway.call(principal, 'run', {'number': 1}, lambda: principal, request_key='request-0001')
    second = await gateway.call(principal, 'run', {'number': 1}, lambda: principal, request_key='request-0001')
    assert first == second and backend.calls == [1]
    assert first['_meta']['codepier/callId'].startswith('gwc_')
    with pytest.raises(DevError):
        await gateway.call(principal, 'run', {'number': 2}, lambda: principal, request_key='request-0001')
    assert backend.calls == [1]
    await gateway.close()
    gateway.store.db.close()


@pytest.mark.asyncio
async def test_gateway_unknown_receipt_is_never_replayed(monkeypatch):
    gateway, principal, backend, *_ = make_gateway(monkeypatch)

    async def fail(number, request):
        raise httpx.ReadTimeout('unknown', request=request)

    backend.call_hook = fail
    with pytest.raises(DevError):
        await gateway.call(principal, 'run', {'number': 1}, lambda: principal, request_key='request-unknown')
    with pytest.raises(DevError) as error:
        await gateway.call(principal, 'run', {'number': 1}, lambda: principal, request_key='request-unknown')
    assert error.value.code == 'GATEWAY_ORIGINAL_CALL'
    assert backend.calls == [1]
    assert gateway.store.one('SELECT state FROM gateway_calls')['state'] == 'unknown'
    await gateway.close()
    gateway.store.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['binding', 'account', 'catalog', 'connector'])
async def test_gateway_versions_create_distinct_transport_identity(monkeypatch, change):
    gateway, principal, backend, _, binding, account, connector = make_gateway(monkeypatch)
    await gateway.call(principal, 'run', {'number': 1}, lambda: principal)
    first_key, first = next(iter(gateway.pool.sessions.items()))
    if change == 'catalog':
        account['catalog_hash'] = 'catalog-2'
    else:
        {'binding': binding, 'account': account, 'connector': connector}[change]['version'] += 1
    await gateway.call(principal, 'run', {'number': 2}, lambda: principal)
    assert len(gateway.pool.sessions) == 2
    assert any(key != first_key and session is not first for key, session in gateway.pool.sessions.items())
    assert backend.calls == [1, 2]
    await gateway.close()
    gateway.store.db.close()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_loopback_http11_connections_are_bounded_and_reused():
    """Explicit loopback-only transport proof; excluded from the pure-mock run."""
    gate = asyncio.Event()
    gate.set()
    connections = []
    handlers = set()
    received = []
    tasks = []

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connections.append(writer)
        try:
            while await reader.readline():
                headers = {}
                while (line := await reader.readline()) not in (b'\r\n', b''):
                    key, value = line.decode().split(':', 1)
                    headers[key.lower()] = value.strip()
                body = json.loads(await reader.readexactly(int(headers.get('content-length', 0))))
                if body['method'] == 'server/discover':
                    result = {'supportedVersions': [MODERN], 'capabilities': {'tools': {}}}
                else:
                    assert body['method'] == 'tools/call'
                    received.append(body['params']['arguments']['number'])
                    await gate.wait()
                    result = {'content': []}
                payload = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                             b'Connection: keep-alive\r\nContent-Length: '
                             + str(len(payload)).encode() + b'\r\n\r\n' + payload)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(host, port):
        return ['127.0.0.1']

    config = {**CONFIG, 'endpoint': f'http://localhost:{port}/mcp',
              'allow_http': 1, 'networks': '["127.0.0.1/32"]'}
    session = Session(config, '', resolver=resolve, concurrency=4)
    try:
        await call(session, 0)
        original = session.client
        gate.clear()
        tasks = [asyncio.create_task(call(session, number)) for number in range(1, 9)]
        await until(lambda: len(received) == 5)
        assert len(connections) == 4
        gate.set()
        await asyncio.gather(*tasks)
        warmed_connections = len(connections)
        tasks = [asyncio.create_task(call(session, number)) for number in range(9, 25)]
        await asyncio.gather(*tasks)
        assert session.client is original
        assert len(connections) == warmed_connections == 4
        assert sorted(received) == list(range(25))
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.close()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 3)
