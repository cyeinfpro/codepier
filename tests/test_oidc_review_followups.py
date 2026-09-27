"""Second-review regressions: real routes, SQL scheduler and signed provider."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import InvalidToken

from hub import iam
from hub.oidc_resilience import SyncScheduler
from shared.crypto import digest
from shared.util import DevError
from tests.test_oidc_integration import oidc as oidc, team as team, start, callback
from tests.test_oidc_review_regressions import seed_identity, run_sync, loaded


@pytest.mark.parametrize('bucket', ['client', 'provider', 'public', 'total', 'link'])
def test_consumed_transactions_do_not_use_pending_capacity(oidc, monkeypatch, bucket):
    import hub.oidc as module
    app, b, _, provider, _ = oidc
    service, store = app.state.oidc, app.state.store
    provider = service.provider(provider['id'])
    # Small bucket limits exercise the same count predicates as production.
    for name in ('LOGIN_CLIENT_LIMIT', 'LOGIN_PROVIDER_LIMIT', 'LOGIN_PUBLIC_LIMIT', 'LOGIN_TOTAL_LIMIT'):
        monkeypatch.setattr(module, name, 100)
    limit = 8 if bucket == 'link' else 3
    if bucket != 'link':
        monkeypatch.setattr(module, {'client': 'LOGIN_CLIENT_LIMIT', 'provider': 'LOGIN_PROVIDER_LIMIT',
                                   'public': 'LOGIN_PUBLIC_LIMIT', 'total': 'LOGIN_TOTAL_LIMIT'}[bucket], limit)
    session = store.one('SELECT * FROM sessions WHERE user_id=?', ('owner',)) if bucket == 'link' else None
    async def exercise():
        for n in range(limit):
            _, state, _ = await service.begin(provider, '/', client_key='proxy', link_session=session)
            # Retain the replay/link-race record, as the real callback does.
            store.execute('UPDATE oidc_transactions SET used=1 WHERE state_hash=?', (state,))
        assert store.one('SELECT count(*) AS n FROM oidc_transactions')['n'] == limit
        await service.begin(provider, '/', client_key='proxy', link_session=session)
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['success', 'invalid_id_token'])
def test_consumed_callback_frees_slot_and_replay_remains_rejected(oidc, monkeypatch, outcome):
    import hub.oidc as module
    app, b, _, provider, _ = oidc
    monkeypatch.setattr(module, 'LOGIN_CLIENT_LIMIT', 1)
    first, response = start(oidc)
    if outcome == 'invalid_id_token': oidc[2].overrides['aud'] = 'wrong-client'
    assert callback(oidc, first, response).status_code == (303 if outcome == 'success' else 401)
    assert callback(oidc, first, response).status_code == 400
    # Same trusted socket address is not blocked by its completed login.
    second = b['owner'].get('/auth/oidc/' + provider['id'] + '/start', follow_redirects=False)
    assert second.status_code == 303, second.text
    # Pending logins STILL consume capacity.
    third = b['owner'].get('/auth/oidc/' + provider['id'] + '/start', follow_redirects=False)
    assert third.status_code == 429 and third.json()['error']['code'] == 'OIDC_CLIENT_BUSY'


def test_corrupted_ciphertext_gets_backoff_without_stale_authority(oidc):
    app, _, fake, provider, _ = oidc
    row = seed_identity(app, provider, subject=fake.subject, upstream_tokens='corrupt-ciphertext')
    run_sync(app)
    store = app.state.store
    state = store.one('SELECT * FROM oidc_sync_state WHERE identity_id=?', (row['id'],))
    assert state['code'] == 'OIDC_RECORD_INVALID' and state['failures'] == 1
    assert state['next_attempt'] > time.time()
    assert store.one('SELECT fresh_until FROM external_identities')['fresh_until'] == row['fresh_until']
    assert len(store.all("SELECT id FROM audit WHERE actor='oidc-worker'")) == 1
    run_sync(app)
    assert store.one('SELECT failures FROM oidc_sync_state')['failures'] == 1


@pytest.mark.parametrize('failure', [InvalidToken, RuntimeError, AttributeError])
def test_failed_identity_does_not_release_lock_while_siblings_work(oidc, monkeypatch, failure):
    app, _, _, provider, _ = oidc
    seed_identity(app, provider, 'a-broken')
    seed_identity(app, provider, 'b-slow')
    service = app.state.oidc
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def sync(identifier):
            seen.append(identifier)
            if identifier == 'a-broken':
                await entered.wait()
                raise failure('PRIVATE-CREDENTIAL-DO-NOT-LOG')
            entered.set()
            await release.wait()
        monkeypatch.setattr(service, 'sync_identity', sync)
        batch = asyncio.create_task(service.reconcile())
        await entered.wait()
        # Let the sibling's exception and gather callbacks execute.
        for _ in range(5): await asyncio.sleep(0)
        was_pending = not batch.done() and service.sync_lock.locked()
        release.set()
        result = await asyncio.gather(batch, return_exceptions=True)
        assert was_pending, 'a per-record exception released the batch lock prematurely'
        assert result == [None]
        assert set(seen) == {'a-broken', 'b-slow'}
    asyncio.run(scenario())
    state = app.state.store.one('SELECT * FROM oidc_sync_state WHERE identity_id=?', ('a-broken',))
    assert state['failures'] == 1 and state['code'] == 'OIDC_RECORD_INVALID'
    assert 'PRIVATE-CREDENTIAL' not in json.dumps(app.state.store.all("SELECT detail FROM audit WHERE actor='oidc-worker'"))


def test_refresh_then_userinfo_error_keeps_generation_and_failure_history(oidc):
    app, _, fake, provider, _ = oidc
    store = app.state.store
    row = seed_identity(app, provider, subject=fake.subject)
    def expire():
        # Keep the record within the same generation for scheduler accounting.
        # The first token expires immediately; refresh returns a 1-second token
        # so each retry naturally refreshes without a test-side ciphertext edit.
        secured = json.loads(store.decrypt(row['upstream_tokens']))
        secured['expires_at'] = 0
        store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',
                      (store.encrypt(json.dumps(secured)), row['id']))
    expire()
    def provider_handle(request):
        response = fake.handle(request)
        if request.url.path == '/token':
            return httpx.Response(200, json={**response.json(), 'expires_in': 1})
        return response
    app.state.oidc.transport = httpx.MockTransport(provider_handle)
    fake.fail_userinfo = True
    for failures in (1, 2, 3):
        if failures > 1: store.execute('UPDATE oidc_sync_state SET next_attempt=0')
        run_sync(app)
        state = store.one('SELECT * FROM oidc_sync_state')
        current = store.one('SELECT * FROM external_identities')
        assert state['failures'] == failures
        assert state['code'] == 'OIDC_UPSTREAM_ERROR'
        assert state['credential_hash'] == digest(current['upstream_tokens'])
        assert current['fresh_until'] == row['fresh_until']
    events = store.all("SELECT * FROM audit WHERE actor='oidc-worker'")
    assert len(events) == 1  # failures were coalesced, not discarded
    fake.fail_userinfo = False
    store.execute('UPDATE oidc_sync_state SET next_attempt=0')
    run_sync(app)
    state = store.one('SELECT * FROM oidc_sync_state')
    assert state['failures'] == 0 and state['code'] == ''
    assert store.one('SELECT fresh_until FROM external_identities')['fresh_until'] > row['fresh_until']
    details = [json.loads(r['detail']) for r in store.all("SELECT detail FROM audit WHERE actor='oidc-worker'")]
    assert any(r['code'] == 'OIDC_SYNC_RECOVERED' for r in details)


def test_new_login_after_attempt_refresh_wins_over_stale_failure(oidc):
    app, _, fake, provider, _ = oidc
    store, service = app.state.store, app.state.oidc
    secured = store.encrypt(json.dumps({'access_token': fake.access_token,
                'refresh_token': fake.refresh_token, 'expires_at': 0}))
    row = seed_identity(app, provider, subject=fake.subject, upstream_tokens=secured)
    login_cipher = store.encrypt(json.dumps({'access_token': 'new-login', 'expires_at': time.time()+3600}))
    def login_during_userinfo():
        store.execute('UPDATE external_identities SET upstream_tokens=?,checked_at=?,fresh_until=? WHERE id=?',
                      (login_cipher, time.time(), time.time()+1200, row['id']))
    fake.on_userinfo, fake.fail_userinfo = login_during_userinfo, True
    run_sync(app)
    assert store.one('SELECT upstream_tokens FROM external_identities')['upstream_tokens'] == login_cipher
    assert store.one('SELECT failures FROM oidc_sync_state')['failures'] == 0
    assert not store.all("SELECT id FROM audit WHERE actor='oidc-worker'")


def test_cancellation_waits_for_siblings_and_preserves_retry_lease(oidc, monkeypatch):
    app, _, _, provider, _ = oidc
    seed_identity(app, provider)
    service, store = app.state.oidc, app.state.store
    async def scenario():
        entered, cleanup, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def sync(identifier):
            entered.set()
            try: await asyncio.Event().wait()
            finally:
                await cleanup.wait()
                stopped.set()
        monkeypatch.setattr(service, 'sync_identity', sync)
        batch = asyncio.create_task(service.reconcile())
        await entered.wait()
        batch.cancel()
        for _ in range(3): await asyncio.sleep(0)
        assert service.sync_lock.locked() and not batch.done()
        cleanup.set()
        with pytest.raises(asyncio.CancelledError): await batch
        assert stopped.is_set() and not service.sync_lock.locked()
    asyncio.run(scenario())
    state = store.one('SELECT * FROM oidc_sync_state')
    assert state['failures'] == 0 and state['next_attempt'] > time.time()


def rotate(fake, kid='key-2'):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                'kid': kid, 'alg': 'RS256', 'use': 'sig'}
    return jwt.encode(fake.claims(), key, algorithm='RS256', headers={'kid': kid})


def test_rotation_is_not_blocked_by_failed_discovery_with_good_cache(oidc):
    service, fake, provider, keys = loaded(oidc)
    previous = service.store.one('SELECT * FROM oidc_cache')
    calls = []
    def discovery_down(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/.well-known/openid-configuration'):
            return httpx.Response(503, json={})
        return fake.handle(request)
    service.transport = httpx.MockTransport(discovery_down)
    with pytest.raises(DevError): asyncio.run(service.metadata(provider, force=True))
    assert service.store.one('SELECT * FROM oidc_cache') == previous
    # Known keys remain usable immediately despite the discovery failure.
    assert asyncio.run(service.verify_claims(fake.sign(fake.claims()), provider, keys, nonce=''))
    new_token = rotate(fake)
    assert asyncio.run(service.verify_claims(new_token, provider, keys, nonce=''))['sub'] == fake.subject
    assert calls.count('/jwks') == 1


def test_one_garbage_kid_does_not_consume_entire_rotation_budget(oidc):
    import jwt
    service, fake, provider, keys = loaded(oidc)
    forged = jwt.encode(fake.claims(), fake.key, algorithm='RS256', headers={'kid': 'garbage'})
    with pytest.raises(DevError): asyncio.run(service.verify_claims(forged, provider, keys, nonce=''))
    new_token = rotate(fake)
    assert asyncio.run(service.verify_claims(new_token, provider, keys, nonce=''))['sub'] == fake.subject


def test_storage_completion_error_joins_siblings_before_unlocking(oidc, monkeypatch):
    app, _, _, provider, _ = oidc
    seed_identity(app, provider, 'a-fast')
    seed_identity(app, provider, 'b-slow')
    service = app.state.oidc
    real_finish = service.scheduler.finish
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def sync(identifier):
            if identifier == 'a-fast':
                await entered.wait()
                return
            entered.set()
            await release.wait()
        def finish(snapshot, attempt, code):
            if snapshot['id'] == 'a-fast': raise RuntimeError('storage failed')
            return real_finish(snapshot, attempt, code)
        monkeypatch.setattr(service, 'sync_identity', sync)
        monkeypatch.setattr(service.scheduler, 'finish', finish)
        batch = asyncio.create_task(service.reconcile())
        await entered.wait()
        for _ in range(5): await asyncio.sleep(0)
        was_pending = not batch.done() and service.sync_lock.locked()
        release.set()
        results = await asyncio.gather(batch, return_exceptions=True)
        assert was_pending
        assert isinstance(results[0], RuntimeError)
    asyncio.run(scenario())


def test_group_compatibility_warnings_are_admin_only_and_explain_entra(oidc):
    app, browsers, _, provider, _ = oidc
    service = app.state.oidc
    current = service.provider(provider['id'])
    assert service.group_compatibility(current)['warnings'] == []
    current['required_group'] = 'members'
    current['issuer'] = 'https://login.microsoftonline.com/tenant/v2.0'
    status = service.group_compatibility(current, {})
    assert status['group_policy_required'] and status['group_policy_source'] == 'userinfo'
    assert any('Entra' in message for message in status['warnings'])
    assert any('UserInfo' in message for message in status['warnings'])
    assert any('没有 UserInfo' in message for message in status['warnings'])
    app.state.store.execute('UPDATE oidc_providers SET required_group=? WHERE id=?', ('members', provider['id']))
    check = browsers['owner'].post('/api/iam/oidc/providers/' + provider['id'] + '/check')
    assert check.status_code == 200 and check.json()['warnings']
    assert browsers['bob'].post('/api/iam/oidc/providers/' + provider['id'] + '/check').status_code == 403
    public = browsers['bob'].get('/api/auth/providers').json()
    assert all(set(row) == {'id', 'label'} for row in public['providers'])


def test_startup_warns_existing_group_policy_without_logging_private_values(oidc, caplog):
    app, _, _, provider, _ = oidc
    service, store = app.state.oidc, app.state.store
    store.execute('UPDATE oidc_providers SET required_group=? WHERE id=?', ('PRIVATE-GROUP', provider['id']))
    with caplog.at_level('WARNING', logger='hub.oidc'):
        service.warn_group_compatibility()
    assert 'OIDC_GROUPS_USERINFO_REQUIRED' in caplog.text
    assert 'Microsoft Entra' in caplog.text
    assert 'PRIVATE-GROUP' not in caplog.text and provider['id'] not in caplog.text


def test_key_fetch_failure_uses_short_retry_without_poisoning_cached_keys(oidc):
    import jwt
    from hub.oidc import KEY_FAILURE_COOLDOWN, KEY_REFRESH_BURST
    service, fake, provider, keys = loaded(oidc)
    existing = service.store.one('SELECT * FROM oidc_cache')
    calls = []
    def broken(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={})
    service.transport = httpx.MockTransport(broken)
    token = jwt.encode(fake.claims(), fake.key, algorithm='RS256', headers={'kid': 'new-key'})
    with pytest.raises(DevError): asyncio.run(service.verify_claims(token, provider, keys, nonce=''))
    deadline = service.key_refresh_failures[provider['id']][1]
    assert 0 < deadline - time.monotonic() <= KEY_FAILURE_COOLDOWN
    assert service.key_refresh_after[provider['id']][2] == KEY_REFRESH_BURST
    assert service.store.one('SELECT * FROM oidc_cache') == existing
    assert asyncio.run(service.verify_claims(fake.sign(fake.claims()), provider, keys, nonce=''))
    with pytest.raises(DevError): asyncio.run(service.verify_claims(token, provider, keys, nonce=''))
    assert calls == ['/jwks']
    service.key_refresh_failures[provider['id']] = (provider['version'], 0)
    service.transport = httpx.MockTransport(fake.handle)
    valid = rotate(fake)
    assert asyncio.run(service.verify_claims(valid, provider, keys, nonce=''))
    assert provider['id'] not in service.key_refresh_failures
