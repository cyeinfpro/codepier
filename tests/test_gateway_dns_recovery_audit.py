"""MCP-01: failed requests are not replayed; independent requests revalidate DNS."""
import json

import httpx
import pytest

from hub.gateway.remote import BackendError, RemotePool
from shared.util import DevError
from tests.test_mcp_gateway import Backend

CONFIG = {'endpoint': 'https://mcp.example/mcp', 'protocol': 'auto', 'networks': '[]', 'allow_http': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('queued', [False, True])
async def test_transport_failure_releases_stale_pin_without_replaying(queued):
    backend = Backend()
    address = ['93.184.216.34']
    lookups = []
    calls = []
    fail = [False]

    async def resolve(host, port):
        lookups.append(address[0])
        return list(address)

    async def handle(request):
        if json.loads(request.content)['method'] == 'tools/call':
            calls.append(request.url.host)
            if fail[0]:
                raise httpx.ReadTimeout('unknown result', request=request)
        return await backend.handle(request)

    pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(handle))
    key = ('owner', 'account')
    try:
        session = await pool.get(key, CONFIG, '')
        await session.call('run', {}, lambda: None)
        await pool.release(key)
        failed = await pool.get(key, CONFIG, '')
        waiting = await pool.get(key, CONFIG, '') if queued else None
        fail[0] = True
        with pytest.raises(BackendError) as error:
            await failed.call('run', {}, lambda: None)
        assert error.value.code == 'GATEWAY_BACKEND_TRANSPORT'
        assert calls == ['93.184.216.34', '93.184.216.34']
        await pool.release(key)
        if queued:
            assert pool.sessions[key] is waiting
            assert failed.client is not None  # another admitted caller still owns it
        else:
            assert key not in pool.sessions
        address[0] = '93.184.216.35'
        fail[0] = False
        recovered = waiting or await pool.get(key, CONFIG, '')
        await recovered.call('run', {}, lambda: None)
        assert lookups == ['93.184.216.34', '93.184.216.35']
        assert calls == ['93.184.216.34', '93.184.216.34', '93.184.216.35']
        await pool.release(key)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_connection_age_is_bounded_even_when_recently_used():
    backend = Backend()
    lookups = []

    async def resolve(host, port):
        lookups.append(host)
        return ['93.184.216.34']

    pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(backend.handle))
    key = ('owner', 'account')
    try:
        session = await pool.get(key, CONFIG, '')
        await session.call('run', {}, lambda: None)
        session.connected_at -= 3600
        await session.call('run', {}, lambda: None)
        assert len(lookups) == 2
        assert backend.effects == 2
        await pool.release(key)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_repin_rejects_new_private_address_without_sending_credentials():
    backend = Backend()
    address = ['93.184.216.34']

    async def resolve(host, port):
        return list(address)

    pool = RemotePool(resolver=resolve, transport=httpx.MockTransport(backend.handle))
    key = ('owner', 'account')
    try:
        session = await pool.get(key, CONFIG, 'private-backend-token')
        await session.call('run', {}, lambda: None)
        session.connected_at -= 3600
        address[0] = '127.0.0.1'
        with pytest.raises(DevError):
            await session.call('run', {}, lambda: None)
        assert backend.effects == 1
        assert all(request.url.host == '93.184.216.34' for request, _ in backend.requests)
        await pool.release(key)
    finally:
        await pool.close()
