"""Synthetic wire-to-pairing-to-dot-to-reply regression; never a live account."""
import asyncio
import json

import pytest
import httpx

from hub.collaboration.weixin_protocol import WeixinClient, WireReply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, inbox, no_work
from tests.test_dot_weixin_channel import fixture_channel, packet


def unpaired(fixture):
    c = duplex(fixture)
    w = c.s.weixin
    scope = {**c.scope, 'conversation_id': c.slot['conversation_id']}
    account = w.import_account({**scope, 'bot_id': 'synthetic-bot', 'token': 'synthetic-wire-token',
        'label': 'Synthetic pairing', 'confirm_credential_storage': True,
        'confirm_provider_terms': True, 'idempotency_key': key()}, c.owner)
    binding = w.bind({**scope, 'account_id': account['account_id'], 'dot_id': c.slot['id'],
        'confirm_external_data_ingress': True, 'allow_replies': True,
        'idempotency_key': key()}, c.owner)
    return c, w, account, binding, scope


def test_optional_wire_success_pairing_owner_confirmation_and_reply_close_the_loop(collab):
    c, w, account, binding, scope = unpaired(collab)
    calls, replies = [], []
    pair = packet(1, binding['pairing_challenge'])
    pair.pop('ret')
    inbound = packet(2, 'Only discuss the project, no execution.')
    inbound.pop('ret')
    responses = [httpx.ReadTimeout('synthetic idle'), {}, pair, inbound, inbound]
    async def transport(method, url, headers, body, **limits):
        assert headers['Authorization'] == 'Bearer synthetic-wire-token'
        calls.append(url.rsplit('/', 1)[-1])
        if url.endswith('/getupdates'):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return WireReply(200, json.dumps(response).encode())
        assert url.endswith('/sendmessage')
        replies.append(json.loads(body)['msg'])
        return WireReply(200, b'{}')
    w.client_factory = lambda base, token: WeixinClient(base, token=token, transport=transport)
    asyncio.run(w.poll_once(account['account_id']))
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert row['state'] == 'active' and row['failures'] == 0 and row['last_sync_at'] is None
    c.clock[0] += 3
    asyncio.run(w.poll_once(account['account_id']))
    assert w.binding(binding['binding_id'], c.owner, c.room)['state'] == 'pairing'
    c.clock[0] += 3
    asyncio.run(w.poll_once(account['account_id']))
    pending = w.binding(binding['binding_id'], c.owner, c.room)
    assert pending['state'] == 'awaiting_owner'
    assert pending['pending_peer_id'] == 'synthetic-peer'
    assert not pending['pairing_hash'] and not pending['pairing_secret']
    assert inbox(c)['items'] == []
    assert c.s.store.one('SELECT last_sync_at FROM weixin_accounts')['last_sync_at'] == c.clock[0]
    w.control({**scope, 'binding_id': pending['id'], 'expected_version': pending['version'],
        'action': 'activate', 'confirm_peer_id': 'synthetic-peer', 'idempotency_key': key()}, c.owner)
    c.clock[0] += 3
    asyncio.run(w.poll_once(account['account_id']))
    item = inbox(c)['items'][0]
    assert item['task_request'] is None
    assert item['trusted_author']['kind'] == 'external_channel'
    c.clock[0] += 3
    asyncio.run(w.poll_once(account['account_id']))
    assert len(inbox(c)['items']) == 1
    c.s.invoke('collaboration', {'action': 'dot_message_v2', **c.scope,
        'dot_id': c.slot['id'], 'body_text': 'Synthetic response', 'reply_to_id': item['message_id'],
        'idempotency_key': key()}, c.actor)
    asyncio.run(w.delivery.tick())
    asyncio.run(w.delivery.tick())
    assert len(replies) == 1
    assert replies[0]['to_user_id'] == 'synthetic-peer'
    assert replies[0]['context_token'] == 'private-context-token'
    assert c.s.store.one('SELECT state FROM weixin_deliveries')['state'] == 'accepted'
    assert len(c.s.store.all('SELECT * FROM weixin_inbound')) == 2
    assert calls == ['getupdates'] * 5 + ['sendmessage']
    no_work(c)


def test_empty_success_preserves_committed_cursor(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    old = c.s.store.one('SELECT cursor FROM weixin_accounts')['cursor']
    result = w.commit_updates(w.claim_poll(account['account_id']), {})
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert result == {'committed': True, 'message_ids': [], 'cursor_persisted': False}
    assert c.s.store.decrypt(row['cursor']) == c.s.store.decrypt(old)
    assert row['last_sync_at'] == c.clock[0] and row['failures'] == 0
    no_work(c)


def test_idle_poll_never_exponentially_backs_off_or_fakes_success(collab):
    c, w, account, binding, scope = unpaired(collab)
    for _ in range(12):
        claim = w.claim_poll(account['account_id'])
        assert claim
        w.poll_failed(claim, DevError('WEIXIN_POLL_IDLE', 'synthetic idle', 503))
        row = c.s.store.one('SELECT * FROM weixin_accounts')
        assert row['state'] == 'active' and row['failures'] == 0
        assert row['last_sync_at'] is None
        assert 0 < row['next_poll'] - c.clock[0] <= 5
        c.clock[0] = row['next_poll'] + 0.01
    assert w.binding(binding['binding_id'], c.owner, c.room)['state'] == 'pairing'
    no_work(c)


def test_expired_poll_lease_recovers_without_account_attention(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    claim = w.claim_poll(account['account_id'])
    c.clock[0] += 56
    with pytest.raises(DevError) as caught:
        w.commit_updates(claim, packet(2))
    assert caught.value.code == 'WEIXIN_POLL_FENCE_LOST'
    w.poll_failed(claim, caught.value)
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert row['state'] == 'active'
    assert row['failures'] == 0 and row['next_poll'] <= c.clock[0] + 5
    c.clock[0] = row['next_poll'] + 0.01
    replacement = w.claim_poll(account['account_id'])
    assert replacement and replacement['poll_fence'] > claim['poll_fence']
    w.commit_updates(replacement, packet(2))
    assert len(inbox(c)['items']) == 1
    no_work(c)


def test_superseded_poll_error_never_changes_new_claim(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    old = w.claim_poll(account['account_id'])
    c.clock[0] += 56
    fresh = w.claim_poll(account['account_id'])
    before = c.s.store.one('SELECT * FROM weixin_accounts')
    w.poll_failed(old, DevError('WEIXIN_POLL_FENCE_LOST', 'old lease', 409))
    assert c.s.store.one('SELECT * FROM weixin_accounts') == before
    w.commit_updates(fresh, packet(2))
    no_work(c)


@pytest.mark.parametrize('code,status', [
    ('WEIXIN_AUTH_REJECTED', 403), ('WEIXIN_SESSION_EXPIRED', 403),
    ('WEIXIN_RECEIVER_MISMATCH', 409), ('WEIXIN_DUPLICATE_CONFLICT', 409),
])
def test_security_and_identity_errors_still_stop_the_account(collab, code, status):
    c, w, account, binding, scope = fixture_channel(collab)
    w.poll_failed(w.claim_poll(account['account_id']), DevError(code, 'synthetic denied', status))
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert row['state'] == 'attention' and row['reason_code'] == code
    c.clock[0] += 4000
    assert w.claim_poll(account['account_id']) is None
    no_work(c)


def test_pairing_message_for_another_recipient_cannot_bind(collab):
    c, w, account, binding, scope = unpaired(collab)
    before = c.s.store.one('SELECT cursor FROM weixin_accounts')['cursor']
    with pytest.raises(DevError) as caught:
        w.commit_updates(w.claim_poll(account['account_id']),
            packet(1, binding['pairing_challenge'], to_user_id='another-bot'))
    assert caught.value.code == 'WEIXIN_RECEIVER_MISMATCH'
    pending = w.binding(binding['binding_id'], c.owner, c.room)
    assert pending['state'] == 'pairing' and not pending['pending_peer_id']
    assert c.s.store.one('SELECT cursor FROM weixin_accounts')['cursor'] == before
    assert not c.s.store.all('SELECT * FROM weixin_inbound')
    no_work(c)


def test_expired_challenge_stops_polling_without_replacing_login_or_binding(collab):
    c, w, account, binding, scope = unpaired(collab)
    original = c.s.store.one('SELECT secret,credential_version,generation,cursor FROM weixin_accounts')
    c.clock[0] = binding['pairing_until'] + 0.01
    w.client_factory = lambda *args: pytest.fail('Expired pairing must not make provider requests')
    asyncio.run(w.poll_once(account['account_id']))
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert row['state'] == 'active' and row['reason_code'] == 'WEIXIN_PAIRING_EXPIRED'
    assert {k: row[k] for k in original} == original
    pending = w.binding(binding['binding_id'], c.owner, c.room)
    assert pending['state'] == 'pairing' and not pending['pending_peer_id']
    assert not c.s.store.all('SELECT * FROM weixin_inbound')
    no_work(c)


def test_poll_crossing_pairing_deadline_preserves_account_for_explicit_rebind(collab):
    c, w, account, binding, scope = unpaired(collab)
    c.clock[0] = binding['pairing_until'] - 2
    claim = w.claim_poll(account['account_id'])
    original = c.s.store.one('SELECT secret,credential_version,generation,cursor FROM weixin_accounts')
    # The 55-second lease remains current after the pairing deadline passes.
    c.clock[0] = binding['pairing_until'] + 0.01
    with pytest.raises(DevError) as caught:
        w.commit_updates(claim, packet(1, binding['pairing_challenge']))
    assert caught.value.code == 'WEIXIN_PAIRING_EXPIRED'
    w.poll_failed(claim, caught.value)
    row = c.s.store.one('SELECT * FROM weixin_accounts')
    assert row['state'] == 'active' and row['reason_code'] == 'WEIXIN_PAIRING_EXPIRED'
    assert {k: row[k] for k in original} == original
    assert not c.s.store.all('SELECT * FROM weixin_inbound')
    c.clock[0] += 3
    w.client_factory = lambda *args: pytest.fail('Expired pairing must not make provider requests')
    asyncio.run(w.poll_once(account['account_id']))
    no_work(c)
