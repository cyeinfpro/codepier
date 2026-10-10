"""Independent HTTP wire contracts; all credentials/callbacks below are synthetic."""
import base64
import hashlib
import hmac
import json
import ssl
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub import mcp_request_audit
from hub.collaboration.event_errors import CallbackEndpointError, callback_reason
from hub.collaboration.network import Reply
from hub.runtime import Principal
from shared.crypto import digest, password_hash
from shared.util import DevError
from tests.legacy_iam_fixture import attach_session_security

EVENT_NAMES = {
    'codepier.monitor.status_changed.v1', 'codepier.monitor.result_ready.v1',
    'codepier.monitor.incident_changed.v1', 'codepier.collaboration.task_available.v1',
    'codepier.collaboration.message_mentioned.v1', 'codepier.collaboration.work_available.v1',
    'codepier.collaboration.delegation_available.v1', 'codepier.operation.completed.v1',
}
EVENT = 'codepier.monitor.status_changed.v1'
SECRET = 'whsec_' + base64.b64encode(b'fixture-only-signing-material-32b!').decode()
PRIVATE = 'callback-private-marker'


@pytest.fixture
def wire(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    app = create_app(str(tmp_path / 'hub'))
    store = app.state.store
    store.execute('INSERT INTO users VALUES (?,?,?,?)', ('owner', 'admin', password_hash('fixture-password'), time.time()))
    store.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest('session'), 'owner', 'csrf', time.time() + 3600))
    attach_session_security(store)
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',
                  ('device', 'fixture', store.encrypt('x' * 43), time.time()))
    store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES (?,?,?,?,?,?,?,?,?)',
                  ('project', 'fixture', 'fixture', 'device', '/tmp/fixture', '', 'write', 0, time.time()))
    owner = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    token = app.state.auth.issue_grant(owner, 'fixture', ['read'], ['project'])['token']
    service = app.state.runtime.collaboration
    service.config = replace(service.config, enabled=True, events_enabled=True)
    captured = []

    async def receiver(url, body, headers):
        # Deliberately do not use the production signing/header helpers.
        captured.append((url, body, headers))
        assert headers['webhook-id'].startswith('msg_verification_')
        assert len(headers['webhook-id']) == len('msg_verification_') + 32
        assert abs(time.time() - int(headers['webhook-timestamp'])) < 300
        signed = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + body
        expected = base64.b64encode(hmac.new(base64.b64decode(SECRET[6:]), signed, hashlib.sha256).digest()).decode()
        assert hmac.compare_digest(headers['webhook-signature'], 'v1,' + expected)
        assert headers['X-MCP-Subscription-Id']
        payload = json.loads(body)
        assert set(payload) == {'type', 'challenge'} and payload['type'] == 'verification'
        return Reply(200, json.dumps({'challenge': payload['challenge']}).encode())

    service.events.sender = receiver
    args = {'name': EVENT, 'arguments': {'project_id': 'project', 'environment_id': 'production'},
            'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/' + PRIVATE, 'secret': SECRET}}
    with TestClient(app) as client:
        yield app, client, token, args, captured


def rpc(wire, method, params, headers=None):
    _, client, token, _, _ = wire
    # Literal standard headers and metadata, independent of request_headers().
    request_headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': method}
    request_headers.update(headers or {})
    metadata = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                'io.modelcontextprotocol/clientCapabilities': {}}
    return client.post('/mcp', headers=request_headers,
                       json={'jsonrpc': '2.0', 'id': 7, 'method': method,
                             'params': {**params, '_meta': metadata}})


@pytest.mark.parametrize('mirror', [False, True])
def test_http_subscribe_and_unsubscribe_optional_name_header(wire, mirror):
    app, _, _, args, captured = wire
    headers = {'Mcp-Name': EVENT} if mirror else {}
    discovered = rpc(wire, 'server/discover', {}).json()['result']
    assert discovered['resultType'] == 'complete' and discovered['capabilities']['events'] == {}
    assert '2026-07-28' in discovered['supportedVersions']
    listed = rpc(wire, 'events/list', {}).json()['result']
    assert {event['name'] for event in listed['events']} == EVENT_NAMES
    response = rpc(wire, 'events/subscribe', args, headers)
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result['resultType'] == 'complete'
    assert captured[0][2]['X-MCP-Subscription-Id'] == result['id']
    assert len(captured) == 1
    stop = {**args, 'delivery': {key: value for key, value in args['delivery'].items() if key != 'secret'}}
    response = rpc(wire, 'events/unsubscribe', stop, headers)
    assert response.status_code == 200, response.text
    assert app.state.store.one('SELECT state FROM mcp_event_subscriptions')['state'] == 'unsubscribed'


@pytest.mark.parametrize('project_visible', [True, False])
def test_catalog_audit_distinguishes_empty_success_from_discovery(wire, monkeypatch, project_visible):
    events = []
    monkeypatch.setattr(mcp_request_audit, 'write_event', events.append)
    if not project_visible:
        wire[0].state.store.execute("UPDATE grants SET projects='[]'")
    response = rpc(wire, 'events/list', {})
    assert response.status_code == 200
    expected = len(EVENT_NAMES) if project_visible else 0
    assert {event['name'] for event in response.json()['result']['events']} == (EVENT_NAMES if project_visible else set())
    assert len(response.json()['result']['events']) == expected
    listed = [e for e in events if e['stage'] == 'event_catalog_returned']
    assert len(listed) == 1 and listed[0]['event_count'] == expected
    assert SECRET not in json.dumps(events) and PRIVATE not in json.dumps(events)


@pytest.mark.parametrize('method', ['events/subscribe', 'events/unsubscribe'])
@pytest.mark.parametrize('name', ['wrong', '=?base64?%%%?='])
def test_supplied_event_name_header_must_match(wire, method, name):
    response = rpc(wire, method, wire[3], {'Mcp-Name': name})
    assert response.status_code == 400 and response.json()['error']['code'] == -32020
    assert not wire[4]


@pytest.mark.parametrize('method,params', [
    ('tools/call', {'name': 'workspace', 'arguments': {}}),
    ('prompts/get', {'name': 'review_project'}),
    ('resources/read', {'uri': 'rd://projects'}),
])
def test_core_name_header_is_still_required(wire, method, params):
    response = rpc(wire, method, params)
    assert response.status_code == 400 and response.json()['error']['code'] == -32020


@pytest.mark.parametrize('headers', [
    {'Mcp-Method': 'events/list'}, {'MCP-Protocol-Version': '2025-11-25'},
    {'Accept': 'application/json'},
])
def test_event_transport_checks_still_reject(wire, headers):
    response = rpc(wire, 'events/subscribe', wire[3], headers)
    assert response.status_code in {400, 406}
    assert not wire[4]


@pytest.mark.parametrize('change,status,code', [
    ({'arguments': {}}, 422, -32602),
    ({'name': 'unknown.event'}, 404, -32011),
    ({'delivery': {'mode': 'webhook', 'url': 'http://example.invalid/', 'secret': SECRET}}, 400, -32602),
    ({'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/', 'secret': 'bad'}}, 400, -32602),
    ({'arguments': {'project_id': 'unapproved', 'environment_id': 'production'}}, 404, -32011),
])
def test_event_http_errors_use_specific_codes_without_challenge(wire, change, status, code):
    response = rpc(wire, 'events/subscribe', {**wire[3], **change})
    assert response.status_code == status, response.text
    assert response.json()['error']['code'] == code
    assert not wire[4]
    assert not wire[0].state.store.all('SELECT * FROM mcp_event_subscriptions')


@pytest.mark.parametrize('name,status,code,extra', [
    ('INSUFFICIENT_SCOPE', 403, -32012, {}),
    ('INVALID_TOKEN', 401, -32012, {}),
    ('SUBSCRIPTION_LIMIT', 409, -32013, {'limit': 'subscriptions', 'max': 32}),
    ('CHALLENGE_RATE_LIMIT', 429, -32013, {'limit': 'callback_challenges_per_minute', 'max': 6}),
    ('STALE_VERSION', 409, -32000, {}),
])
def test_event_mapping_preserves_http_status(wire, monkeypatch, name, status, code, extra):
    async def denied(*_):
        raise DevError(name, 'fixture denial', status)
    monkeypatch.setattr(wire[0].state.runtime.collaboration.events, 'call', denied)
    response = rpc(wire, 'events/subscribe', wire[3])
    assert response.status_code == status
    assert response.json()['error']['code'] == code
    assert response.json()['error']['data'] == {'code': name, **extra}


def test_revoked_credential_still_rejected_before_callback(wire):
    wire[0].state.store.execute('UPDATE grants SET revoked=1')
    response = rpc(wire, 'events/subscribe', wire[3])
    assert response.status_code == 401
    assert response.headers['WWW-Authenticate'].startswith('Bearer ')
    assert not wire[4]


def test_tool_business_error_semantics_unchanged(wire, monkeypatch):
    async def denied(*_):
        raise DevError('INVALID_ARGUMENTS', 'fixture denial', 422)
    monkeypatch.setattr(wire[0].state.runtime, 'invoke', denied)
    response = rpc(wire, 'tools/call', {'name': 'workspace', 'arguments': {}}, {'Mcp-Name': 'workspace'})
    assert response.status_code == 200
    assert response.json()['result']['isError'] is True
    assert response.json()['result']['structuredContent']['error']['code'] == 'INVALID_ARGUMENTS'


@pytest.mark.parametrize('kind,reason,http_status', [
    ('http4', 'http_4xx', 403), ('http5', 'http_5xx', 503),
    ('echo', 'challenge_failed', 200), ('json', 'challenge_failed', 200),
    ('timeout', 'timeout', None), ('connect', 'connection_refused', None),
    ('tls', 'tls_error', None),
])
def test_callback_errors_are_categorized_and_logs_are_redacted(wire, monkeypatch, kind, reason, http_status):
    from hub import mcp_request_audit
    observed = []
    monkeypatch.setattr(mcp_request_audit, 'write_event', observed.append)
    async def rejected(url, body, headers):
        if kind == 'http4': return Reply(403, PRIVATE.encode())
        if kind == 'http5': return Reply(503, PRIVATE.encode())
        if kind == 'echo': return Reply(200, json.dumps({'challenge': PRIVATE}).encode())
        if kind == 'json': return Reply(200, PRIVATE.encode())
        if kind == 'timeout': raise httpx.ReadTimeout(PRIVATE + SECRET)
        if kind == 'connect': raise httpx.ConnectError(PRIVATE + SECRET)
        if kind == 'tls':
            try: raise ssl.SSLError(PRIVATE + SECRET)
            except ssl.SSLError as cause: raise httpx.ConnectError(PRIVATE) from cause
    wire[0].state.runtime.collaboration.events.sender = rejected
    response = rpc(wire, 'events/subscribe', wire[3])
    assert response.status_code == 400, response.text
    assert response.json()['error'] == {'code': -32015, 'message': 'Callback verification failed', 'data': {'reason': reason}}
    events = [item for item in observed if item['stage'] == 'event_callback_failed']
    assert len(events) == 1 and events[0]['callback_reason'] == reason
    assert events[0].get('callback_http_status') == http_status
    assert events[0]['request_id'] == response.headers['X-CodePier-Request-ID']
    serialized = json.dumps(observed) + response.text
    assert PRIVATE not in serialized and SECRET not in serialized
    assert wire[3]['delivery']['url'] not in serialized
    assert not wire[0].state.store.all('SELECT * FROM mcp_event_subscriptions')


def test_callback_diagnostic_whitelist_rejects_untrusted_values(monkeypatch):
    from hub import mcp_request_audit
    observed = []
    monkeypatch.setattr(mcp_request_audit, 'write_event', observed.append)
    trace = mcp_request_audit.Trace(True, 'POST')
    trace.record('event_callback_failed', callback_reason=PRIVATE, callback_http_status=PRIVATE)
    trace.record('parsed', callback_reason='tls_error', callback_http_status=403)
    for item in observed:
        assert 'callback_reason' not in item and 'callback_http_status' not in item
    error = CallbackEndpointError(PRIVATE, True)
    assert error.reason == 'challenge_failed' and error.http_status is None
    assert PRIVATE not in json.dumps(observed)
    assert callback_reason(OSError(PRIVATE)) == 'challenge_failed'


@pytest.mark.parametrize('method', ['events/subscribe', 'events/unsubscribe'])
def test_event_name_still_required_in_body(wire, method):
    response = rpc(wire, method, {key: value for key, value in wire[3].items() if key != 'name'})
    assert response.status_code == 400 and response.json()['error']['code'] == -32602
    assert not wire[4]


def test_duplicate_event_name_headers_rejected(wire):
    headers = [('Authorization', 'Bearer ' + wire[2]), ('Content-Type', 'application/json'),
               ('Accept', 'application/json, text/event-stream'), ('MCP-Protocol-Version', '2026-07-28'),
               ('Mcp-Method', 'events/subscribe'), ('Mcp-Name', EVENT), ('Mcp-Name', EVENT)]
    body = {'jsonrpc': '2.0', 'id': 8, 'method': 'events/subscribe', 'params': {
        **wire[3], '_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                           'io.modelcontextprotocol/clientCapabilities': {}}}}
    response = wire[1].post('/mcp', headers=headers, json=body)
    assert response.status_code == 400 and response.json()['error']['code'] == -32020
    assert not wire[4]


def emit_status(wire, label, *, age=0):
    service = wire[0].state.runtime.collaboration
    room = service.store.one('SELECT * FROM collaboration_rooms')
    service.emit(room, EVENT, label, 1, {'component': 'fixture', 'status': 'degraded',
                 'reason_code': label, 'observed_at': '2026-10-07T00:00:00Z',
                 'recovery_state': 'unknown'})
    service.store.execute('UPDATE mcp_event_outbox SET created=? WHERE object_id=?',
                          (service.clock() - age, label))
    return service.store.one('SELECT * FROM mcp_event_outbox WHERE object_id=?', (label,))


@pytest.mark.parametrize('ttl,seconds', [(-1, 60), (0, 60), (1, 60), (999, 60),
                                       (61000, 61), (10**100, 86400), (None, 86400)])
def test_ttl_suggestion_is_clamped_and_grant_is_authoritative(wire, ttl, seconds):
    service = wire[0].state.runtime.collaboration
    now = time.time()
    service.clock = lambda: now
    response = rpc(wire, 'events/subscribe', {**wire[3], 'ttlMs': ttl})
    assert response.status_code == 200, response.text
    row = service.store.one('SELECT * FROM mcp_event_subscriptions')
    assert row['expires_at'] == now + seconds
    assert response.json()['result']['refreshBefore'] is not None


@pytest.mark.parametrize('ttl', [True, '60000', 0.5])
def test_ttl_remains_a_strict_nullable_integer(wire, ttl):
    response = rpc(wire, 'events/subscribe', {**wire[3], 'ttlMs': ttl})
    assert response.status_code == 422 and response.json()['error']['code'] == -32602
    assert not wire[4]


@pytest.mark.parametrize('mode', ['poll', 'push'])
@pytest.mark.parametrize('method', ['events/subscribe', 'events/unsubscribe'])
def test_known_but_unavailable_delivery_mode_is_unsupported(wire, mode, method):
    delivery = {**wire[3]['delivery'], 'mode': mode}
    if method == 'events/unsubscribe':
        delivery.pop('secret')
    response = rpc(wire, method, {**wire[3], 'delivery': delivery})
    assert response.status_code == 400
    assert response.json()['error']['code'] == -32014
    assert response.json()['error']['data']['feature'] == 'deliveryMode'
    assert response.json()['error']['data']['value'] == mode
    assert not wire[4]


def test_unsubscribe_url_only_and_unknown_identity(wire):
    assert rpc(wire, 'events/subscribe', wire[3]).status_code == 200
    stop = {**wire[3], 'delivery': {'url': wire[3]['delivery']['url']}}
    assert rpc(wire, 'events/unsubscribe', stop).status_code == 200
    assert rpc(wire, 'events/unsubscribe', stop).status_code == 200  # Known tombstone is idempotent.
    unknown = {**stop, 'delivery': {'url': stop['delivery']['url'] + '/unknown'}}
    response = rpc(wire, 'events/unsubscribe', unknown)
    assert response.status_code == 404
    assert response.json()['error']['code'] == -32011
    assert response.json()['error']['data']['kind'] == 'subscription'


@pytest.mark.parametrize('restart', ['new_identity', 'expired'])
@pytest.mark.parametrize('explicit_null', [False, True])
def test_omitted_or_null_cursor_starts_now_without_history(wire, restart, explicit_null):
    service = wire[0].state.runtime.collaboration
    assert rpc(wire, 'events/subscribe', wire[3]).status_code == 200
    old = emit_status(wire, 'older')
    args = dict(wire[3])
    if restart == 'new_identity':
        args['delivery'] = {**args['delivery'], 'url': args['delivery']['url'] + '/new'}
    else:
        service.events.reserve()  # Fence a pre-expiry in-flight delivery as well.
        service.store.execute('UPDATE mcp_event_subscriptions SET expires_at=?', (service.clock() - 1,))
    if explicit_null:
        args['cursor'] = None
    args['maxAgeMs'] = 1  # Ignored for null/omitted cursor, never a request for history.
    response = rpc(wire, 'events/subscribe', args)
    assert response.status_code == 200, response.text
    sub = service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (response.json()['result']['id'],))
    assert sub['ack_seq'] == sub['scan_seq'] == old['seq']
    assert response.json()['result']['truncated'] is False
    assert not service.store.all("SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (sub['id'],))
    fresh = emit_status(wire, 'fresh')
    service.events.reserve()
    assert service.store.one('SELECT event_seq FROM mcp_event_deliveries WHERE subscription_id=? AND event_seq=?', (sub['id'], fresh['seq']))


@pytest.mark.parametrize('max_age,old_age,new_age', [(60000, 120, 10), (10**100, 8 * 86400, 86400)])
def test_explicit_cursor_replay_honors_age_and_retention_floor(wire, max_age, old_age, new_age):
    service = wire[0].state.runtime.collaboration
    now = time.time()
    service.clock = lambda: now
    initial = rpc(wire, 'events/subscribe', wire[3]).json()['result']
    older = emit_status(wire, 'old', age=old_age)
    fresh = emit_status(wire, 'recent', age=new_age)
    service.store.execute('UPDATE mcp_event_subscriptions SET expires_at=?', (now - 1,))
    response = rpc(wire, 'events/subscribe', {**wire[3], 'cursor': initial['cursor'], 'maxAgeMs': max_age})
    assert response.status_code == 200, response.text
    assert response.json()['result']['truncated'] is True
    sub = service.store.one('SELECT * FROM mcp_event_subscriptions')
    assert sub['ack_seq'] == older['seq']
    reserved = service.events.reserve()
    assert len(reserved) == 1 and reserved[0]['event_id'] == fresh['id']
    assert not service.store.one('SELECT id FROM mcp_event_deliveries WHERE event_id=?', (older['id'],))


def test_active_refresh_preserves_pending_watermark_and_validates_cursor_first(wire):
    service = wire[0].state.runtime.collaboration
    initial = rpc(wire, 'events/subscribe', wire[3]).json()['result']
    emit_status(wire, 'pending')
    service.events.reserve()
    before = service.store.one('SELECT ack_seq,scan_seq FROM mcp_event_subscriptions')
    valid = rpc(wire, 'events/subscribe', {**wire[3], 'cursor': initial['cursor'], 'maxAgeMs': 0})
    assert valid.status_code == 200
    assert service.store.one('SELECT ack_seq,scan_seq FROM mcp_event_subscriptions') == before
    calls = len(wire[4])
    bad = rpc(wire, 'events/subscribe', {**wire[3], 'cursor': 'damaged',
              'delivery': {**wire[3]['delivery'], 'secret': 'whsec_' + base64.b64encode(b'new-fixture-key-material-32bytes!!').decode()}})
    assert bad.status_code == 400 and bad.json()['error']['code'] == -32602
    assert len(wire[4]) == calls  # Even key rotation must not challenge a malformed request.


@pytest.mark.parametrize('age', [-1, True, '1', 0.5])
def test_max_age_rejects_invalid_types_and_negative_values(wire, age):
    response = rpc(wire, 'events/subscribe', {**wire[3], 'maxAgeMs': age})
    assert response.status_code == 422 and response.json()['error']['code'] == -32602
    assert not wire[4]


@pytest.mark.parametrize('delivery', [{'url': 'https://example.invalid/', 'secret': SECRET},
                                      {'mode': 'invented', 'url': 'https://example.invalid/', 'secret': SECRET}])
def test_subscribe_requires_known_delivery_mode(wire, delivery):
    response = rpc(wire, 'events/subscribe', {**wire[3], 'delivery': delivery})
    assert response.status_code == 422 and response.json()['error']['code'] == -32602
    assert not wire[4]


def test_failed_challenge_cleanup_reports_missing_subscription(wire):
    async def rejected(*_):
        return Reply(403, b'')
    wire[0].state.runtime.collaboration.events.sender = rejected
    assert rpc(wire, 'events/subscribe', wire[3]).json()['error']['code'] == -32015
    stop = {**wire[3], 'delivery': {'url': wire[3]['delivery']['url']}}
    response = rpc(wire, 'events/unsubscribe', stop)
    assert response.status_code == 404
    assert response.json()['error']['code'] == -32011
    assert response.json()['error']['data']['kind'] == 'subscription'
