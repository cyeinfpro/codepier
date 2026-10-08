"""Callback verification cannot revive a changed or revoked delegation authority."""
import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import DELEGATION_EVENT, canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401


def subscription_fixture(fixture, receive):
    service, owner, worker, _, room, _, _, scope = fixture
    service.config = replace(service.config, events_enabled=True)
    service.runtime.collaboration = service
    events = EventService(service, receive)
    service.events = events
    slot = service.joining.create({
        **scope, 'label': 'Challenge fixture', 'kind': 'dot', 'idempotency_key': key(),
    }, owner)['slot']
    service.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, worker)
    request = {
        **scope, 'conversation_id': room['id'], 'slot_id': slot['id'], 'expected_version': 0,
        'purpose': 'Read this isolated project and report evidence.', 'capabilities': ['read'],
        'idempotency_key': key(),
    }
    policy = service.delegation.set_policy(request, owner)['policy']
    args = {
        **policy['subscription_request'],
        'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example.invalid/challenge',
            'secret': 'whsec_' + base64.b64encode(b'isolated-challenge-fixture-key!!!').decode(),
        },
    }
    assert args['name'] == DELEGATION_EVENT
    return events, policy, request, args


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pause', 'revise', 'revoke'])
async def test_delegation_change_during_challenge_cannot_save_subscription(collab, change):
    service, owner, worker, _, _, _, _, scope = collab
    challenges = []

    def invalidate():
        # These owner/grant changes happen after prepare's authorization and
        # before the callback proves its challenge. No subscription exists yet.
        assert not service.store.all('SELECT * FROM mcp_event_subscriptions')
        if change == 'pause':
            service.delegation.control({
                **scope, 'policy_id': policy['id'], 'expected_version': policy['version'],
                'action': 'pause', 'idempotency_key': key(),
            }, owner)
        elif change == 'revise':
            revised = service.delegation.set_policy({
                **request, 'expected_version': policy['version'],
                'purpose': 'A newly approved purpose.', 'idempotency_key': key(),
            }, owner)['policy']
            assert revised['state'] == 'active' and revised['version'] > policy['version']
        else:
            service.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (worker.grant_id,))

    async def receive(url, body, headers):
        payload = json.loads(body)
        assert payload['type'] == 'verification'
        challenges.append(payload['challenge'])
        await service.store.run(invalidate)
        return Reply(200, canonical({'challenge': payload['challenge']}).encode())

    events, policy, request, args = subscription_fixture(collab, receive)
    with pytest.raises(DevError) as caught:
        await events.subscribe(args, worker)
    if change in {'pause', 'revise'}:
        assert caught.value.code == {
            'pause': 'DELEGATION_POLICY_INACTIVE', 'revise': 'DELEGATION_EVENT_DENIED',
        }[change]
    assert len(challenges) == 1
    assert not service.store.all('SELECT * FROM mcp_event_subscriptions')
    assert not service.store.all('SELECT * FROM collaboration_join_routes')
    assert not service.store.all("SELECT * FROM collaboration_audit WHERE action='subscription.saved'")


@pytest.mark.asyncio
async def test_unchanged_delegation_saves_subscription_after_valid_challenge(collab):
    challenges = []

    async def receive(url, body, headers):
        payload = json.loads(body)
        assert payload['type'] == 'verification'
        challenges.append(payload['challenge'])
        return Reply(200, canonical({'challenge': payload['challenge']}).encode())

    events, policy, _, args = subscription_fixture(collab, receive)
    saved = await events.subscribe(args, collab[2])
    row = collab[0].store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (saved['id'],))
    assert row['state'] == 'active'
    assert json.loads(row['arguments'])['policy_version'] == policy['version']
    assert collab[0].store.one('SELECT subscription_id FROM collaboration_join_routes')['subscription_id'] == saved['id']
    assert len(challenges) == 1
