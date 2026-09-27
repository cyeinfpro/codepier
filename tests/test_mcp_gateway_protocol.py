"""Transport safety, bounded schema validation and response envelope regression."""
import asyncio
import json
import pytest
import httpx

from hub.gateway.remote import Session, BackendError, RemotePool
from hub.gateway.service import result_value
from hub.gateway.validation import Validator
from shared.util import DevError
from tests.test_mcp_gateway import Backend, public_dns, TOOLS

CONFIG = {'endpoint': 'https://mcp.example/mcp', 'protocol': 'auto', 'networks': '[]', 'allow_http': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 500, 503, 302])
async def test_failed_probe_does_not_retry_with_legacy_or_leak_challenges(status):
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://unapproved.example', 'WWW-Authenticate': 'SECRET'}, request=request)
    session = Session(CONFIG, 'backend-only', resolver=public_dns, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(DevError) as error:
            await session.tools()
        assert 'SECRET' not in str(error.value)
        assert len(requests) == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_expired_legacy_session_is_not_replayed():
    backend = Backend(modern=False)
    calls = []
    async def handle(request):
        body = json.loads(request.content)
        if body['method'] == 'tools/call':
            calls.append(body)
            return httpx.Response(404, request=request)
        return await backend.handle(request)
    session = Session(CONFIG, '', resolver=public_dns, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(BackendError, match='过期'):
            await session.call('run', {}, lambda: None)
        assert len(calls) == 1 and session.session_id is None and session.version is None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_pool_does_not_share_active_credentials_or_evict_active_client():
    pool = RemotePool(limit=1)
    session = await pool.get(('grant-A',), CONFIG, 'a')
    assert await pool.get(('grant-A',), CONFIG, 'a') is session
    await pool.release(('grant-A',))
    with pytest.raises(DevError, match='上限'):
        await pool.get(('grant-B',), CONFIG, 'b')
    await pool.release(('grant-A',))
    assert await pool.get(('grant-B',), CONFIG, 'b') is not session
    await pool.release(('grant-B',))
    await pool.close()


@pytest.mark.parametrize('raw', [
    {'resultType': 'input_required', 'content': []},
    {'content': [{'type': 'resource_link', 'uri': 'https://private.example'}]},
    {'content': [{'type': 'image', 'data': 'not base64', 'mimeType': 'image/png'}]},
    {'content': [], 'structuredContent': 'bad'},
])
def test_unsupported_results_fail_closed_without_fake_conversion(raw):
    with pytest.raises(DevError):
        result_value(raw, TOOLS[0])


@pytest.mark.asyncio
async def test_pathological_schema_worker_is_bounded_and_capacity_recovers():
    validator = Validator(timeout=2)
    schema = {'type': 'object', 'properties': {'text': {'type': 'string', 'pattern': '^(a+)+$'}}}
    with pytest.raises(DevError):
        await validator.validate(schema, {'text': 'a' * 1000 + '!'})
    assert validator.active == 0
    # Independent normal validation must not inherit the exhausted worker.
    validator.timeout = 8
    await validator.validate({'type': 'object'}, {})
    assert validator.active == 0


@pytest.mark.asyncio
async def test_cancelled_schema_worker_is_reaped_and_capacity_recovers(monkeypatch):
    # Wait for actual IPC admission, not a machine-speed-dependent sleep.
    validator = Validator(timeout=8)
    entered = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec
    workers = []
    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        workers.append(process)
        communicate = process.communicate
        async def observed(data):
            entered.set()
            return await communicate(data)
        process.communicate = observed
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    schema = {'type': 'object', 'properties': {'text': {'type': 'string', 'pattern': '^(a+)+$'}}}
    task = asyncio.create_task(validator.validate(schema, {'text': 'a' * 1000 + '!'}))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert validator.active == 0
    assert len(workers) == 1 and workers[0].returncode is not None
