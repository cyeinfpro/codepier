"""Bounded tools-only MCP client. Never forwards frontend credentials or retries tool calls.

Supports modern 2026-07-28 and legacy Streamable HTTP (JSON or SSE response).
No sampling, roots, elicitation, Apps/resources, resumable SSE or Tasks capability
is advertised. Legacy sessions are isolated by credential binding AND grant.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

import httpx

from hub.gateway import network
from hub.gateway.catalog import MAX_TOOLS, review_tools
from shared.mcp_protocol import MODERN, LEGACY, PREFIX, request_headers
from shared.util import DevError, valid_json_value

MAX_RESPONSE = 2 * 1024 * 1024
MAX_SESSION_AGE = 900  # Refresh pinned DNS between independent operations, never during one.
MAX_SESSION_CONCURRENCY = 8
CLOSE_DRAIN_SECONDS = 25
CLIENT_INFO = {'name': 'codepier-mcp-gateway', 'version': '1'}


class BackendError(DevError):
    def __init__(self, code, message, status=502, *, rpc_code=None, supported=None):
        super().__init__(code, message, status if status >= 400 else 502)
        self.rpc_code = rpc_code
        self.supported = supported or []


def decode_json(raw):
    try:
        value = json.loads(raw)
        if not valid_json_value(value) or not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端返回无效 JSON-RPC') from exc


class Session:
    def __init__(self, connector, secret, *, resolver=network.resolve, transport=None,
                 concurrency=MAX_SESSION_CONCURRENCY):
        self.connector, self.secret = connector, secret
        self.resolver, self.transport = resolver, transport
        self.client = None
        if not 1 <= concurrency <= 32:
            raise ValueError('session concurrency must be between 1 and 32')
        self.concurrency = concurrency
        self.lock = asyncio.Lock()
        self.condition = asyncio.Condition(self.lock)
        self.waiters = deque()
        self.active = 0
        self.exclusive = False
        self.closing = False
        self.stateless = False
        # Aggregate timings are bounded and contain no request or identity data.
        self.stats = {'admitted': 0, 'queued_seconds': 0.0, 'active_seconds': 0.0,
                      'peak_active': 0}
        self.version = None
        self.session_id = None
        self.used = time.monotonic()
        self.connected_at = self.used
        self.invalid = False

    async def _close_transport(self):
        client, self.client = self.client, None
        self.version = self.session_id = None
        self.stateless = False
        if client is not None:
            await client.aclose()

    async def close(self, *, drain_seconds=CLOSE_DRAIN_SECONDS):
        # Stop admission immediately, even if initialization currently owns the
        # condition. A timed-out drain never closes a transport under active RPCs;
        # the last lease closes it instead. close() is safe to repeat.
        self.closing = True
        try:
            async with asyncio.timeout(drain_seconds):
                async with self.condition:
                    self.condition.notify_all()
                    await self.condition.wait_for(lambda: not self.active)
                    await self._close_transport()
        except TimeoutError:
            pass

    def _stale(self):
        return self.invalid or (self.client is not None
                                and time.monotonic() - self.connected_at >= MAX_SESSION_AGE)

    def _ready(self, ticket, exclusive):
        if self.closing:
            return True
        if self.waiters[0] is not ticket or self.exclusive:
            return False
        if self.active and (exclusive or self._stale() or self.version != MODERN or not self.stateless):
            return False
        return self.active < self.concurrency

    @asynccontextmanager
    async def operation(self, *, exclusive=False):
        ticket = object()
        started = time.monotonic()
        admitted = None
        async with self.condition:
            self.waiters.append(ticket)
            try:
                await self.condition.wait_for(lambda: self._ready(ticket, exclusive))
                if self.closing:
                    raise DevError('GATEWAY_CLOSED', 'MCP 连接正在关闭；未发送操作', 503)
                # Only an empty generation may initialize/rotate; active modern
                # leases share the same pinned transport without mutating it.
                if not self.active:
                    await self.initialize()
                if self.closing:
                    raise DevError('GATEWAY_CLOSED', 'MCP 连接正在关闭；未发送操作', 503)
                self.active += 1
                self.exclusive = exclusive
                admitted = time.monotonic()
                self.used = admitted
                self.stats['admitted'] += 1
                self.stats['queued_seconds'] += admitted - started
                self.stats['peak_active'] = max(self.stats['peak_active'], self.active)
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()
                if admitted is None and self.closing and not self.active:
                    await self._close_transport()
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.exclusive = False
                self.used = time.monotonic()
                self.stats['active_seconds'] += self.used - admitted
                self.condition.notify_all()
                if self.closing and not self.active:
                    await self._close_transport()

    async def connect(self):
        if self.client is not None:
            return
        connector = self.connector
        self.url, self.host, self.sni = await network.pin(
            connector['endpoint'], json.loads(connector['networks']), bool(connector['allow_http']), self.resolver)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency), transport=self.transport)
        self.connected_at = time.monotonic()
        self.invalid = False

    async def rpc(self, method, params=None, *, version=None, notification=False):
        await self.connect()
        version = version or self.version
        params = dict(params or {})
        headers = {'Accept': 'application/json, text/event-stream', 'Host': self.host}
        # Only this account's backend token is sent. No cookies, caller metadata,
        # incoming Authorization, proxy environment or arbitrary headers cross.
        if self.secret:
            headers['Authorization'] = 'Bearer ' + self.secret
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            body['id'] = uuid.uuid4().hex
        if version == MODERN:
            params['_meta'] = {PREFIX + 'protocolVersion': MODERN,
                               PREFIX + 'clientInfo': CLIENT_INFO,
                               PREFIX + 'clientCapabilities': {}}
            headers.update(request_headers(body))
        elif version:
            headers['MCP-Protocol-Version'] = version
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        try:
            async with self.client.stream('POST', self.url, headers=headers, json=body,
                                          extensions={'sni_hostname': self.sni}) as response:
                status = response.status_code
                if status in (401, 403):
                    raise BackendError('GATEWAY_BACKEND_AUTH_REQUIRED', '此 MCP 后端帐户需要重新连接或授权；不是 CodePier 登出', 502)
                if status == 429:
                    raise BackendError('GATEWAY_BACKEND_RATE_LIMITED', 'MCP 后端限流；未自动重发', 502)
                if status == 404 and self.session_id:
                    self.session_id = self.version = None
                    raise BackendError('GATEWAY_BACKEND_SESSION_EXPIRED', '后端会话已过期；本次操作未重发，请先核对原结果', 502)
                if 300 <= status < 400:
                    raise BackendError('GATEWAY_REDIRECT_DENIED', '不向重定向地址发送后端凭据', 502)
                if status >= 500:
                    raise BackendError('GATEWAY_BACKEND_UNAVAILABLE', 'MCP 后端服务不可用；未自动重发', 502)
                if notification:
                    if status not in (200, 202, 204):
                        raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端未接受初始化通知')
                    return {}
                mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if mime not in ('application/json', 'text/event-stream'):
                    raise BackendError('GATEWAY_BACKEND_HTTP', '后端未返回支持的 MCP 响应', status)
                value = await self.read_response(response, mime, body['id'])
                if value.get('jsonrpc') != '2.0' or value.get('id') != body['id']:
                    raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端响应编号或协议不匹配')
                if 'error' in value:
                    error = value['error']
                    code = error.get('code') if isinstance(error, dict) else None
                    data = error.get('data') if isinstance(error, dict) else None
                    supported = data.get('supported', []) if isinstance(data, dict) else []
                    raise BackendError('GATEWAY_BACKEND_RPC', '后端拒绝 MCP 请求', status,
                                       rpc_code=code, supported=supported if isinstance(supported, list) else [])
                if status >= 400 or not isinstance(value.get('result'), dict):
                    raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端 MCP 响应无效')
                if method == 'server/discover':
                    self.stateless = response.headers.get('mcp-session-id') is None
                if method == 'initialize':
                    sid = response.headers.get('mcp-session-id')
                    if sid is not None and (not sid or len(sid) > 256 or any(not 33 <= ord(c) <= 126 for c in sid)):
                        raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端会话编号无效')
                    self.session_id = sid
                return value['result']
        except asyncio.CancelledError:
            # Cancellation may interrupt a sent request. Drain sibling leases
            # before repinning; do not assume a missing response means no effect.
            self.invalid = True
            raise
        except httpx.HTTPError as exc:
            # The result may be unknown. Retire the transport, not the receipt;
            # only a later independently authorized operation may connect again.
            self.invalid = True
            raise BackendError('GATEWAY_BACKEND_TRANSPORT', '后端连接中断或超时；操作结果可能未知，未自动重发') from exc

    async def read_response(self, response, mime, request_id):
        raw = bytearray()
        event_lines, pending, count, events = [], bytearray(), 0, 0
        async for chunk in response.aiter_bytes():
            count += len(chunk)
            if count > MAX_RESPONSE:
                raise BackendError('GATEWAY_BACKEND_SIZE', '后端响应超过大小限制')
            if mime == 'application/json':
                raw.extend(chunk)
                continue
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n'); pending = bytearray(rest)
                line = line.rstrip(b'\r')
                if line.startswith(b'data:'):
                    event_lines.append(line[5:].lstrip(b' '))
                elif not line and event_lines:
                    data = b'\n'.join(event_lines); event_lines = []; events += 1
                    if events > 256:
                        raise BackendError('GATEWAY_BACKEND_SIZE', '后端 SSE 事件过多')
                    if not data:
                        continue
                    value = decode_json(data)
                    if 'method' in value:
                        if 'id' in value:
                            raise BackendError('GATEWAY_UNSUPPORTED_REQUEST', '后端要求未启用的 client capability；未自动批准')
                        continue
                    if value.get('id') != request_id:
                        raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端 SSE 响应编号不匹配')
                    return value
        if mime == 'text/event-stream':
            raise BackendError('GATEWAY_BACKEND_INCOMPLETE', '后端 SSE 未返回完整结果；未重发操作')
        return decode_json(bytes(raw))

    async def initialize(self):
        # Called under the admission condition with no active leases. DNS expiry
        # or a failed transport cannot close a sibling RPC or replay its request.
        if self._stale():
            await self._close_transport()
        if self.version:
            return
        protocol = self.connector['protocol']
        if protocol in ('auto', 'modern'):
            try:
                result = await self.rpc('server/discover', version=MODERN)
                if MODERN not in result.get('supportedVersions', []) or result.get('resultType', 'complete') != 'complete':
                    raise BackendError('GATEWAY_BACKEND_VERSION', '后端没有声明支持已实现的现代协议')
                self.version = MODERN
                return
            except BackendError as exc:
                if protocol == 'modern' or exc.rpc_code == -32022 or exc.code in (
                    'GATEWAY_BACKEND_AUTH_REQUIRED', 'GATEWAY_BACKEND_RATE_LIMITED',
                    'GATEWAY_BACKEND_TRANSPORT', 'GATEWAY_BACKEND_UNAVAILABLE', 'GATEWAY_REDIRECT_DENIED'):
                    raise
                # Probe is read-only. Do not use a failed tools/call to negotiate.
                if exc.status not in (400, 404, 405) and exc.rpc_code != -32601:
                    raise
        result = await self.rpc('initialize', {'protocolVersion': '2025-11-25',
                                                'clientInfo': CLIENT_INFO, 'capabilities': {}})
        version = result.get('protocolVersion')
        if version not in LEGACY:
            raise BackendError('GATEWAY_BACKEND_VERSION', '后端未协商到支持的协议版本')
        self.version = version
        try:
            await self.rpc('notifications/initialized', notification=True)
        except BaseException:
            self.version = self.session_id = None
            raise

    async def tools(self):
        # One lease owns the entire cursor sequence. FIFO admission prevents a
        # continuous stream of calls from starving discovery.
        async with self.operation(exclusive=True):
            tools, cursor, seen = [], None, set()
            for _ in range(16):
                result = await self.rpc('tools/list', {'cursor': cursor} if cursor else {})
                if result.get('resultType', 'complete') != 'complete' or not isinstance(result.get('tools'), list):
                    raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端没有返回完整工具目录')
                tools.extend(result['tools'])
                if len(tools) > MAX_TOOLS:
                    raise BackendError('GATEWAY_CATALOG_LIMIT', '后端工具数量超过限制')
                cursor = result.get('nextCursor')
                if not cursor:
                    return review_tools(tools)
                if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                    raise BackendError('GATEWAY_BACKEND_PROTOCOL', '后端工具目录分页循环或无效')
                seen.add(cursor)
            raise BackendError('GATEWAY_CATALOG_LIMIT', '后端工具目录分页超过限制')

    async def call(self, name, arguments, before_send):
        async with self.operation():
            # This callback executes a complete worker-owned authorization phase
            # AFTER initialization/queueing, immediately before the side effect.
            check = before_send()
            if inspect.isawaitable(check):
                await check
            return await self.rpc('tools/call', {'name': name, 'arguments': arguments})


class RemotePool:
    def __init__(self, *, resolver=network.resolve, transport=None, limit=64,
                 idle_seconds=300, concurrency=MAX_SESSION_CONCURRENCY):
        self.resolver, self.transport = resolver, transport
        self.limit, self.idle_seconds = limit, idle_seconds
        self.concurrency = concurrency
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.active = {}
        self.closed = False

    async def get(self, key, connector, secret):
        retired = []
        async with self.lock:
            if self.closed:
                raise DevError('GATEWAY_CLOSED', 'MCP 连接池已关闭；未发送操作', 503)
            now = time.monotonic()
            for old_key, session in list(self.sessions.items()):
                if old_key not in self.active and now - session.used > self.idle_seconds:
                    retired.append(self.sessions.pop(old_key))
            if key not in self.sessions:
                if len(self.sessions) >= self.limit:
                    idle = [(session.used, old_key) for old_key, session in self.sessions.items() if old_key not in self.active]
                    if not idle:
                        raise DevError('GATEWAY_BUSY', 'MCP 连接数量达到上限，请稍后重试', 429)
                    _, oldest = min(idle)
                    retired.append(self.sessions.pop(oldest))
                self.sessions[key] = Session(connector, secret, resolver=self.resolver,
                                             transport=self.transport, concurrency=self.concurrency)
            self.active[key] = self.active.get(key, 0) + 1
            session = self.sessions[key]
        # Never hold the pool-wide lock during client I/O or drain another key.
        # Retired sessions have no owners, so close only an idle transport here.
        try:
            for old in retired:
                await old.close()
        except BaseException:
            await self.release(key)
            raise
        return session

    async def release(self, key):
        retired = None
        async with self.lock:
            remaining = self.active.get(key, 0) - 1
            if remaining > 0:
                self.active[key] = remaining
            else:
                self.active.pop(key, None)
                session = self.sessions.get(key)
                if session is not None and session.invalid:
                    retired = self.sessions.pop(key)
        if retired is not None:
            await retired.close()

    async def close(self, *, drain_seconds=CLOSE_DRAIN_SECONDS):
        async with self.lock:
            self.closed = True
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.active.clear()
        await asyncio.gather(*(session.close(drain_seconds=drain_seconds) for session in sessions),
                             return_exceptions=True)
