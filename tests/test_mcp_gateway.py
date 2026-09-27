"""Real Hub/IAM HTTP boundary with a deterministic external MCP transport.

The transport double replaces only network I/O, not authorization or SQL. A
separate real TCP case below verifies the pinned HTTP transport end-to-end.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hub.gateway.catalog import fingerprint, public_name, review_tools, page
from hub.gateway.remote import RemotePool, Session
from hub.gateway.network import endpoint, pin
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from shared.util import DevError
from tests.test_iam_integration import team, assign
from tests.test_roles import role, profile, credential, must
from tests.test_access_profiles import call


TOOLS = [
    {'name': 'echo', 'description': 'Return a value.', 'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False},
     'outputSchema': {'type': 'object', 'properties': {'value': {'type': 'string'}, 'account': {'type': 'string'}}, 'required': ['value', 'account']}},
    {'name': 'run', 'description': 'A side effect.', 'inputSchema': {'type': 'object', 'additionalProperties': False}},
]


class Backend:
    def __init__(self, modern=True, sse=False):
        self.modern, self.sse = modern, sse
        self.tools = copy.deepcopy(TOOLS)
        self.requests = []
        self.effects = 0
        self.initializations = 0
        self.on_initialize = None
        self.on_call = None
        self.fail_after_call = False
        self.result_override = None

    async def handle(self, request):
        body = json.loads(request.content)
        method = body['method']; self.requests.append((request, body))
        rid = body.get('id')
        headers = {}
        if method == 'server/discover':
            if not self.modern:
                return httpx.Response(400, json={'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'legacy'}}, request=request)
            result = {'resultType': 'complete', 'supportedVersions': [MODERN], 'capabilities': {'tools': {}}}
            if self.on_initialize:
                self.on_initialize()
        elif method == 'initialize':
            self.initializations += 1
            headers['Mcp-Session-Id'] = 'sid-' + str(self.initializations)
            result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'fixture', 'version': '1'}, 'capabilities': {'tools': {}}}
            if self.on_initialize:
                self.on_initialize()
        elif method == 'notifications/initialized':
            return httpx.Response(202, request=request)
        elif method == 'tools/list':
            result = {'tools': self.tools}
        elif method == 'tools/call':
            self.effects += 1
            if self.on_call:
                self.on_call()
            if self.fail_after_call:
                raise httpx.ReadTimeout('SECRET_BACKEND_DIAGNOSTIC', request=request)
            value = {'value': body['params']['arguments'].get('value', 'ran'), 'account': request.headers.get('authorization', '')}
            result = self.result_override or {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value,
                                              '_meta': {'openai/profile': True, 'mcp/www_authenticate': ['SECRET_CHALLENGE']}}
        else:
            raise AssertionError(method)
        if self.modern:
            assert body['params']['_meta'][PREFIX + 'protocolVersion'] == MODERN
            assert request.headers['Mcp-Method'] == method
            result = {**result, 'resultType': 'complete'}
        message = {'jsonrpc': '2.0', 'id': rid, 'result': result}
        if self.sse and method == 'tools/call':
            raw = ': keepalive\n\nevent: message\ndata: ' + json.dumps(message) + '\n\n'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream', **headers}, content=raw.encode(), request=request)
        return httpx.Response(200, json=message, headers=headers, request=request)


async def public_dns(host, port):
    return ['93.184.216.34']


@pytest.fixture
def gw(team):
    app, browsers = team
    gateway = app.state.gateway
    gateway.enabled = True
    backend = Backend()
    gateway.pool = RemotePool(resolver=public_dns, transport=httpx.MockTransport(backend.handle))
    return app, browsers, backend


def connector(b, label='Fixture', endpoint_url='https://mcp.example/mcp', **kwargs):
    return must(b['owner'].post('/api/mcp-gateway/connectors', json={'label': label, 'endpoint': endpoint_url, **kwargs}), 201)


def account(b, c, who='alice', sharing='private', token='UPSTREAM_TOKEN_A'):
    return must(b[who].post('/api/mcp-gateway/accounts', json={'label': who, 'connector_id': c['id'], 'sharing': sharing, 'token': token}), 201)


def publish(b, a, who='alice', alias='kiln'):
    discovered = must(b[who].post('/api/mcp-gateway/accounts/' + a['id'] + '/discover'))
    return must(b[who].post('/api/mcp-gateway/bindings', json={'account_id': a['id'], 'alias': alias, 'catalog_hash': discovered['catalog_hash'], 'tools': [tool['name'] for tool in discovered['tools']], 'confirmed': True}), 201)


def authorize(b, binding, who='alice', label='secretary', tools=None, consent=True):
    r = role(b['owner'], label=label, project_rules=[], connector_rules=[{'binding_id': binding['id'], 'tools': tools or binding['tools']}])
    assign(b['owner'], r, who)
    p = profile(b[who], r, label)
    grant = must(credential(b[who], r, p))
    if consent:
        must(b[who].post('/api/mcp-gateway/grants/' + grant['grant_id'] + '/consent', json={'confirmed': True, 'expected_role_version': r['version']}))
    return r, p, grant


def configured(gw, **kwargs):
    app, b, backend = gw
    c = connector(b)
    a = account(b, c)
    binding = publish(b, a)
    r, p, g = authorize(b, binding, **kwargs)
    return app, b, backend, c, a, binding, r, p, g


def rpc(b, token, method, params, modern=False):
    body = {'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': params}
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
    if modern:
        params = body['params'] = {**params, '_meta': {PREFIX + 'protocolVersion': MODERN, PREFIX + 'clientCapabilities': {}}}
        headers.update(request_headers(body))
    return b.post('/mcp', json=body, headers=headers)


def result(response):
    assert response.status_code == 200, response.text
    return response.json()['result']


def error_code(response):
    data = result(response)
    assert data['isError'], data
    return json.loads(data['content'][0]['text'])['error']['code']


def update_policy(b, r, rules):
    return must(b['owner'].put('/api/access-roles/' + r['id'], json={
        'label': r['label'], 'enabled': True, 'project_rules': r['project_rules'], 'device_rules': r['device_rules'],
        'connector_rules': rules, 'expected_version': r['version']}))


def test_gateway_is_native_routing_with_kill_switch_and_explicit_grant_consent(team):
    app, b = team
    assert b['owner'].get('/api/mcp-gateway').json()['enabled']
    app.state.gateway.enabled = False
    assert not b['owner'].get('/api/mcp-gateway').json()['enabled']
    assert b['owner'].post('/api/mcp-gateway/connectors', json={'label': 'no', 'endpoint': 'https://mcp.example/mcp'}).status_code == 403
    app.state.gateway.enabled = True
    backend = Backend()
    app.state.gateway.pool = RemotePool(resolver=public_dns, transport=httpx.MockTransport(backend.handle))
    c = connector(b); a = account(b, c); binding = publish(b, a)
    r, p, g = authorize(b, binding, consent=False)
    assert error_code(call(b['alice'], g['token'], 'kiln__echo', {'value': 'x'})) == 'UNKNOWN_TOOL'
    listing = result(rpc(b['alice'], g['token'], 'tools/list', {}))
    assert 'kiln__echo' not in {tool['name'] for tool in listing['tools']}
    assert backend.effects == 0
    assert b['bob'].post('/api/mcp-gateway/grants/' + g['grant_id'] + '/consent', json={'confirmed': True, 'expected_role_version': r['version']}).status_code == 404


@pytest.mark.parametrize('modern', [False, True])
def test_native_schema_namespaced_routing_and_distinct_backend_token(gw, modern):
    app, b, backend, c, a, binding, r, p, g = configured(gw)
    out = result(rpc(b['alice'], g['token'], 'tools/call', {'name': 'kiln__echo', 'arguments': {'value': 'hello'}}, modern))
    assert out['structuredContent'] == {'value': 'hello', 'account': 'Bearer UPSTREAM_TOKEN_A'}
    assert 'openai/profile' not in out['_meta'] and 'mcp/www_authenticate' not in out['_meta']
    call_id = out['_meta']['codepier/callId']
    receipt = result(call(b['alice'], g['token'], 'gateway_call_get', {'call_id': call_id}))['structuredContent']
    assert receipt['state'] == 'completed' and receipt['result']['structuredContent']['value'] == 'hello'
    stored = app.state.store.one('SELECT result FROM gateway_calls WHERE id=?', (call_id,))['result']
    assert 'hello' not in stored and 'UPSTREAM_TOKEN_A' not in stored
    _, body = next((req, msg) for req, msg in backend.requests if msg['method'] == 'tools/call')
    assert body['params']['name'] == 'echo' and 'user_id' not in body['params']
    req = backend.requests[-1][0]
    assert str(req.url.host) == '93.184.216.34' and req.headers['host'] == 'mcp.example'
    assert req.extensions['sni_hostname'] == 'mcp.example'
    assert 'cookie' not in req.headers and g['token'] not in str(req.headers)


def test_validation_happens_before_backend_dispatch(gw):
    _, b, backend, _, _, _, _, _, g = configured(gw)
    assert error_code(call(b['alice'], g['token'], 'kiln__echo', {'value': 4, 'role_id': 'owner'})) == 'GATEWAY_ARGUMENTS_INVALID'
    assert backend.effects == 0


def test_private_accounts_and_receipts_never_cross_users_or_grants(gw):
    app, b, backend, _, a, binding, r, p, g = configured(gw)
    assign(b['owner'], r, 'bob')
    bp = profile(b['bob'], r, 'Bob'); bg = must(credential(b['bob'], r, bp))
    must(b['bob'].post('/api/mcp-gateway/grants/' + bg['grant_id'] + '/consent', json={'confirmed': True, 'expected_role_version': r['version']}))
    assert error_code(call(b['bob'], bg['token'], 'kiln__echo', {'value': 'x'})) == 'UNKNOWN_TOOL'
    assert b['owner'].post('/api/mcp-gateway/accounts/' + a['id'] + '/discover').status_code == 404
    assert a['id'] not in b['bob'].get('/api/mcp-gateway').text
    first = result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'private'}))
    new = must(credential(b['alice'], r, p))
    must(b['alice'].post('/api/mcp-gateway/grants/' + new['grant_id'] + '/consent', json={'confirmed': True, 'expected_role_version': r['version']}))
    assert error_code(call(b['alice'], new['token'], 'gateway_call_get', {'call_id': first['_meta']['codepier/callId']})) == 'GATEWAY_CALL_NOT_FOUND'


def test_two_backends_same_tool_names_and_no_action_resource_product(gw):
    app, b, backend, c, _, binding, r, _, g = configured(gw, tools=['echo'])
    a2 = account(b, c, token='UPSTREAM_TOKEN_B'); binding2 = publish(b, a2, alias='research')
    r = update_policy(b, r, [{'binding_id': binding['id'], 'tools': ['echo']}, {'binding_id': binding2['id'], 'tools': ['run']}])
    assert result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'x'}))['structuredContent']['account'] == 'Bearer UPSTREAM_TOKEN_A'
    assert result(call(b['alice'], g['token'], 'research__run'))['structuredContent']['account'] == 'Bearer UPSTREAM_TOKEN_B'
    assert error_code(call(b['alice'], g['token'], 'kiln__run')) == 'UNKNOWN_TOOL'
    assert error_code(call(b['alice'], g['token'], 'research__echo', {'value': 'x'})) == 'UNKNOWN_TOOL'


def test_legacy_sessions_isolate_grants_even_for_shared_account(gw):
    app, b, backend = gw; backend.modern = False; backend.sse = True
    c = connector(b); a = account(b, c, who='owner', sharing='space'); binding = publish(b, a, who='owner')
    r, p, g = authorize(b, binding)
    assign(b['owner'], r, 'bob')
    bp = profile(b['bob'], r, 'Bob'); bg = must(credential(b['bob'], r, bp))
    must(b['bob'].post('/api/mcp-gateway/grants/' + bg['grant_id'] + '/consent', json={'confirmed': True, 'expected_role_version': r['version']}))
    result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'A'}))
    result(call(b['bob'], bg['token'], 'kiln__echo', {'value': 'B'}))
    result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'A2'}))
    sessions = [request.headers['mcp-session-id'] for request, body in backend.requests if body['method'] == 'tools/call']
    assert sessions[0] != sessions[1] and sessions[0] == sessions[2]


def test_revoke_after_initialize_prevents_dispatch(gw):
    app, b, backend, _, _, _, r, _, g = configured(gw)
    backend.on_initialize = lambda: app.state.store.execute('UPDATE access_roles SET enabled=0 WHERE id=?', (r['id'],))
    assert error_code(call(b['alice'], g['token'], 'kiln__echo', {'value': 'no'})) == 'GATEWAY_POLICY_DENIED'
    assert backend.effects == 0
    assert app.state.store.one('SELECT state FROM gateway_calls')['state'] == 'rejected'


@pytest.mark.parametrize('mutation', ['role', 'token', 'membership', 'consent', 'account'])
def test_revoke_during_backend_call_withholds_result(gw, mutation):
    app, b, backend, _, a, _, r, _, g = configured(gw)
    sql_args = {
        'role': ('UPDATE access_roles SET enabled=0 WHERE id=?', (r['id'],)),
        'token': ('UPDATE tokens SET expires=0 WHERE grant_id=?', (g['grant_id'],)),
        'membership': ("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'", ()),
        'consent': ('DELETE FROM gateway_consents WHERE grant_id=?', (g['grant_id'],)),
        'account': ('UPDATE gateway_accounts SET enabled=0 WHERE id=?', (a['id'],)),
    }
    backend.on_call = lambda: app.state.store.execute(*sql_args[mutation])
    out = result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'HIDDEN_BACKEND_RESULT'}))
    assert out['isError'] and 'HIDDEN_BACKEND_RESULT' not in json.dumps(out)
    assert backend.effects == 1
    assert app.state.store.one('SELECT result FROM gateway_calls')['result'] == ''


def test_timeout_keeps_original_receipt_and_never_retries(gw):
    app, b, backend, _, _, _, _, _, g = configured(gw)
    backend.fail_after_call = True
    params = {'name': 'kiln__run', 'arguments': {}, '_meta': {'codepier/idempotencyKey': 'intent-001'}}
    first = result(rpc(b['alice'], g['token'], 'tools/call', params))
    assert first['isError'] and 'SECRET_BACKEND_DIAGNOSTIC' not in json.dumps(first)
    assert backend.effects == 1
    assert error_code(rpc(b['alice'], g['token'], 'tools/call', params)) == 'GATEWAY_ORIGINAL_CALL'
    assert backend.effects == 1
    receipt = result(call(b['alice'], g['token'], 'gateway_call_get', {'call_id': first['_meta']['codepier/callId']}))['structuredContent']
    assert receipt['state'] == 'unknown' and receipt['replayed'] is False


def test_schema_discovery_never_silently_republishes(gw):
    app, b, backend, _, a, binding, _, _, g = configured(gw)
    backend.tools[0]['inputSchema']['required'] = []
    must(b['alice'].post('/api/mcp-gateway/accounts/' + a['id'] + '/discover'))
    assert error_code(call(b['alice'], g['token'], 'kiln__echo', {'value': 'x'})) in ('GATEWAY_SCHEMA_REVIEW_REQUIRED', 'UNKNOWN_TOOL')
    assert backend.effects == 0


def test_old_role_editor_does_not_erase_connector_rules(gw):
    _, b, _, _, _, _, r, _, g = configured(gw)
    old_shape = {key: r[key] for key in ('label', 'enabled', 'project_rules', 'device_rules')}
    updated = must(b['owner'].put('/api/access-roles/' + r['id'], json={**old_shape, 'expected_version': r['version']}))
    assert updated['connector_rules'] == r['connector_rules']
    assert not result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'still works'})).get('isError')


@pytest.mark.parametrize('path', ['/api/mcp-gateway/connectors', '/api/mcp-gateway/accounts', '/api/mcp-gateway/bindings'])
def test_csrf_required_for_gateway_mutations(gw, path):
    _, b, _ = gw
    body = {'label': 'bad', 'endpoint': 'https://mcp.example/mcp'} if path.endswith('connectors') else {'label': 'bad', 'connector_id': 'id'} if path.endswith('accounts') else {'account_id': 'a', 'alias': 'a', 'tools': ['echo'], 'catalog_hash': '0' * 64, 'confirmed': True}
    assert b['owner'].post(path, json=body, headers={'X-RD-CSRF': 'incorrect'}).status_code == 403


def test_account_api_never_returns_secret_and_rejects_cross_space(gw):
    app, b, backend = gw
    c = connector(b); a = account(b, c)
    assert 'UPSTREAM_TOKEN_A' not in json.dumps(a)
    assert 'secret' not in b['alice'].get('/api/mcp-gateway').json()['accounts'][0]
    assert b['legacy'].post('/api/mcp-gateway/accounts', json={'label': 'no', 'connector_id': c['id']}).status_code == 404
    assert b['alice'].post('/api/mcp-gateway/connectors', json={'label': 'no', 'endpoint': 'https://mcp.example/mcp'}).status_code == 403


def test_tool_metadata_and_remote_schema_references_are_not_trusted():
    tool = copy.deepcopy(TOOLS[0]); tool['_meta'] = {'openai/profile': True, 'ui': {'resourceUri': 'https://evil.example'}}
    tool['annotations'] = {'readOnlyHint': True}
    reviewed = review_tools([tool])[0]
    assert '_meta' not in reviewed and reviewed['annotations']['readOnlyHint'] is False
    tool['inputSchema']['$ref'] = 'http://169.254.169.254/'
    with pytest.raises(DevError, match='schema'):
        review_tools([tool])
    with pytest.raises(DevError):
        review_tools([TOOLS[0], TOOLS[0]])
    assert len(public_name('long_alias', 'x' * 128)) == 64
    assert public_name('long_alias', 'x' * 127 + 'a') != public_name('long_alias', 'x' * 127 + 'b')


def test_pagination_cursor_is_bound_to_catalog_and_grant():
    tools = [{'name': f'tool_{i:03}'} for i in range(150)]
    first = page(tools, None, ['space', 'grant-A'], b'key')
    assert len(first['tools']) == 100
    assert len(page(tools, first['nextCursor'], ['space', 'grant-A'], b'key')['tools']) == 50
    with pytest.raises(DevError):
        page(tools, first['nextCursor'], ['space', 'grant-B'], b'key')
    with pytest.raises(DevError):
        page(tools[1:], first['nextCursor'], ['space', 'grant-A'], b'key')


@pytest.mark.parametrize('url,networks,allow_http', [
    ('https://user:pass@mcp.example/mcp', [], False),
    ('https://mcp.example/mcp?token=secret', [], False),
    ('http://mcp.example/mcp', [], False),
    ('https://mcp.example/mcp', ['0.0.0.0/0'], False),
    ('https://mcp.example/mcp', ['169.254.0.0/16'], False),
    ('https://mcp.example/mcp\n', [], False),
])
def test_endpoint_registration_rejects_unsafe_addresses(url, networks, allow_http):
    with pytest.raises(DevError):
        endpoint(url, networks, allow_http)


@pytest.mark.asyncio
@pytest.mark.parametrize('addresses', [['93.184.216.34', '127.0.0.1'], ['169.254.169.254'], ['fec0::1'], ['ff02::1'], ['::ffff:127.0.0.1']])
async def test_dns_checks_all_answers_and_blocks_unapproved_networks(addresses):
    async def resolve(host, port):
        return addresses
    with pytest.raises(DevError):
        await pin('https://mcp.example/mcp', [], resolver=resolve)


def test_real_tcp_backend_round_trip(gw):
    app, b, _ = gw
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((body, self.headers.get('Authorization'), self.headers.get('Host')))
            if body['method'] == 'server/discover':
                value = {'resultType': 'complete', 'supportedVersions': [MODERN], 'capabilities': {'tools': {}}}
            elif body['method'] == 'tools/list':
                value = {'resultType': 'complete', 'tools': TOOLS}
            else:
                value = {'resultType': 'complete', 'content': [{'type': 'text', 'text': 'real TCP'}], 'structuredContent': {'value': 'real TCP', 'account': 'fixture'}}
            data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': value}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    app.state.gateway.pool = RemotePool()
    try:
        address = 'http://127.0.0.1:' + str(server.server_port) + '/mcp'
        c = connector(b, endpoint_url=address, networks=['127.0.0.1/32'], allow_http=True)
        a = account(b, c); binding = publish(b, a)
        _, _, g = authorize(b, binding)
        out = result(call(b['alice'], g['token'], 'kiln__echo', {'value': 'hello'}))
        assert out['structuredContent']['value'] == 'real TCP'
        assert received[-1][1] == 'Bearer UPSTREAM_TOKEN_A'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
