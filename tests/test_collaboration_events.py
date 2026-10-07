"""Protocol adapters use an injected receiver; no real chat or callback is contacted."""
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import replace

import pytest
from hub.collaboration.common import TASK_EVENT, RESULT_EVENT, STATUS_EVENT, canonical, read_cursor
from hub.collaboration.events import EventService, CallbackEndpointError, signing_key
from hub.collaboration.network import Reply
from hub.collaboration import network
from shared.util import DevError
from tests.test_collaboration_service import collab, key, command


class Receiver:
    def __init__(self):
        self.requests = []
        self.status = 204
        self.verify = True

    async def __call__(self, url, body, headers):
        self.requests.append((url, body, headers))
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge'] if self.verify else 'wrong'}).encode())
        return Reply(self.status)


def setup_events(c):
    service = c[0]
    service.config = replace(service.config, events_enabled=True, analysis_dispatch_enabled=True)
    receiver = Receiver()
    events = EventService(service, receiver)
    service.events = events
    args = {'name': TASK_EVENT, 'arguments': {'project_id': c[4]['project_id'],
            'environment_id': 'production', 'queue': 'work-analysis'},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/fixture',
                         'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}
    return events, receiver, args


@pytest.mark.asyncio
async def test_subscription_identity_signatures_encryption_and_transport_not_claim(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    subscribed = await events.subscribe(args, worker)
    repeated = await events.subscribe(args, worker)
    assert subscribed['id'] == repeated['id']
    assert len(receiver.requests) == 1
    row = s.store.one('SELECT * FROM mcp_event_subscriptions')
    assert args['delivery']['url'] not in row['secret'] and args['delivery']['secret'] not in row['secret']
    created = command(collab)[1]
    await events.tick()
    delivery = s.store.one('SELECT * FROM mcp_event_deliveries')
    assert delivery['state'] == 'accepted'
    assert s.object('collaboration_jobs', room, created['job_id'])['state'] == 'queued'
    url, body, headers = receiver.requests[-1]
    payload = json.loads(body)
    signed = headers['webhook-id'].encode() + b'.' + headers['webhook-timestamp'].encode() + b'.' + body
    expected = base64.b64encode(hmac.new(signing_key(args['delivery']['secret']), signed, hashlib.sha256).digest()).decode()
    assert headers['webhook-signature'] == 'v1,' + expected
    assert headers['X-MCP-Subscription-Id'] == subscribed['id']
    assert payload['eventId'] == headers['webhook-id'] and payload['data']['job_id'] == created['job_id']
    before = s.store.one('SELECT ack_seq FROM mcp_event_subscriptions')['ack_seq']
    await events.subscribe({**args, 'cursor': subscribed['cursor']}, worker)
    assert s.store.one('SELECT ack_seq FROM mcp_event_subscriptions')['ack_seq'] == before
    await events.tick()
    assert len(receiver.requests) == 2
    distinct = await events.subscribe({**args, 'arguments': {**args['arguments'], 'severity_min': None}}, worker)
    assert distinct['id'] != subscribed['id']  # omitted and explicit default differ


@pytest.mark.asyncio
async def test_retry_exact_body_new_signature_and_bounded_failures(collab):
    events, receiver, args = setup_events(collab)
    await events.subscribe(args, collab[2])
    command(collab)
    receiver.status = 503
    await events.tick()
    first = receiver.requests[-1]
    for delay in (31, 121, 601, 1801):
        collab[6][0] += delay
        # Use a synthetic subscription test event for a long transport retry;
        # the ordinary task expires at its original deadline instead of waking late.
        await events.tick()
    row = collab[0].store.one('SELECT * FROM mcp_event_deliveries')
    assert row['state'] in {'dead_letter', 'abandoned'} and row['attempt'] <= 5
    requests = [item for item in receiver.requests if 'eventId' in json.loads(item[1])]
    assert len(requests) >= 2
    assert requests[0][1] == requests[1][1]
    assert requests[0][2]['webhook-signature'] != requests[1][2]['webhook-signature']
    assert collab[0].store.one('SELECT attempt FROM collaboration_jobs')['attempt'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [410, 413])
async def test_permanent_http_errors_are_not_unsubscribe_or_retried(collab, status):
    events, receiver, args = setup_events(collab)
    subscription = await events.subscribe(args, collab[2])
    command(collab)
    receiver.status = status
    await events.tick()
    await events.tick()
    assert collab[0].store.one('SELECT state FROM mcp_event_subscriptions')['state'] == 'active'
    assert collab[0].store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'dead_letter'
    assert len(receiver.requests) == 2


@pytest.mark.asyncio
async def test_challenge_failure_rotation_and_cache_expiry_is_not_subscription_expiry(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    receiver.verify = False
    with pytest.raises(CallbackEndpointError):
        await events.subscribe(args, worker)
    assert s.store.all('SELECT * FROM mcp_event_subscriptions') == []
    receiver.verify = True
    sub = await events.subscribe(args, worker)
    rotated = {**args, 'delivery': {**args['delivery'], 'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}
    assert (await events.subscribe(rotated, worker))['id'] == sub['id']
    command(collab)
    item = events.reserve()[0]
    assert len(events.ready(item)[1]['webhook-signature'].split()) == 2
    events.finish(item, 204)
    clock[0] += 1801
    # The 30-minute verification cache expiry must not stop the 24-hour grant.
    s.config = replace(s.config, analysis_dispatch_enabled=False)
    subrow = s.store.one('SELECT * FROM mcp_event_subscriptions')
    with s.store.transaction():
        events.queue_test(room, subrow)
    await events.tick()
    assert s.store.all('SELECT state FROM mcp_event_deliveries ORDER BY created')[-1]['state'] == 'accepted'
    assert len(receiver.requests[-1][2]['webhook-signature'].split()) == 1


@pytest.mark.asyncio
async def test_restart_expired_subscription_fences_old_receipt_and_scopes_cursor(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    args['ttlMs'] = 60000
    subscribed = await events.subscribe(args, worker)
    command(collab)
    old = events.reserve()[0]
    clock[0] += 61
    rebuilt = EventService(s, receiver)
    await rebuilt.subscribe({**args, 'cursor': subscribed['cursor']}, worker)
    current = rebuilt.reserve()[0]
    assert current['fence'] > old['fence']
    rebuilt.finish(old, 204)
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'leased'
    rebuilt.finish(current, 204)
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'accepted'
    with pytest.raises(DevError):
        read_cursor(s.secret, 'different-subscription', subscribed['cursor'])


@pytest.mark.asyncio
async def test_revoke_between_reserve_and_send_and_unsubscribe_identity(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    await events.subscribe(args, worker)
    command(collab)
    item = events.reserve()[0]
    s.store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    await events.deliver(item)
    assert len(receiver.requests) == 1
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state'] == 'abandoned'
    with pytest.raises(DevError):
        await events.subscribe(args, worker)


@pytest.mark.asyncio
async def test_public_address_resolution_is_pinned_and_private_denied(monkeypatch):
    for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fe80::1', 'fc00::1', '224.0.0.1', '::ffff:127.0.0.1'):
        assert not network.public_address(ip)
    async def public(host, port):
        return ['8.8.8.8']
    monkeypatch.setattr(network, 'resolve', public)
    pinned, host, name = await network.callback_destination('https://example.invalid/path?q=1')
    assert pinned.host == '8.8.8.8' and host == name == 'example.invalid'
    async def rebound(host, port):
        return ['8.8.8.8', '127.0.0.1']
    monkeypatch.setattr(network, 'resolve', rebound)
    with pytest.raises(DevError):
        await network.callback_destination('https://example.invalid/path')
    for url in ('http://example.invalid', 'https://user:pass@example.invalid', 'https://example.invalid/#fragment'):
        with pytest.raises(DevError):
            network.callback_url(url)
