"""QR login against deterministic provider replies; never a real account."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from hub.collaboration.weixin_protocol import API_BASE
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex


@pytest.fixture
def enrollment(collab):
    c = duplex(collab)
    c.s.config = replace(c.s.config, weixin_enabled=True)
    scope = {**c.scope, 'conversation_id': c.slot['conversation_id']}
    provider = SimpleNamespace(starts=0, polls=[], response={'status': 'wait'}, start_error=False, status_error=False)

    class Provider:
        async def qr_start(self):
            provider.starts += 1
            if provider.start_error:
                raise TimeoutError('synthetic-secret-must-not-leak')
            return {'qrcode': 'private-synthetic-qr-handle',
                    'qrcode_img_content': 'https://weixin.qq.com/synthetic-qr-only'}
        async def qr_status(self, qr, code=''):
            provider.polls.append((qr, code))
            if provider.status_error:
                raise TimeoutError('synthetic-secret-must-not-leak')
            return provider.response
    c.s.weixin.client_factory = lambda *_: Provider()
    return c, c.s.weixin.qr, scope, provider


def start(q, scope, owner, **extra):
    args = {**scope, 'idempotency_key': key(), 'confirm_credential_storage': True,
            'confirm_provider_terms': True, **extra}
    return args, asyncio.run(q.start(args, owner))


def status(q, scope, owner, view, **extra):
    return asyncio.run(q.status({**scope, 'enrollment_id': view['enrollment_id'],
        'expected_version': view['version'], **extra}, owner))


def confirm(provider):
    provider.response = {'status': 'confirmed', 'bot_token': 'synthetic-qr-bot-secret',
        'ilink_bot_id': 'synthetic-bot', 'ilink_user_id': 'synthetic-scanner', 'baseurl': API_BASE}


def accept_args(scope, view):
    return {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version'],
            'idempotency_key': key(), 'label': 'Synthetic QR', 'confirm_scanner_id': view['scanner_id'],
            'confirm_credential_storage': True, 'confirm_provider_terms': True}


def test_qr_start_is_explicit_idempotent_and_scoped(enrollment):
    c, q, scope, p = enrollment
    args, view = start(q, scope, c.owner)
    replay = asyncio.run(q.start(args, c.owner))
    assert replay == view and p.starts == 1
    assert view['state'] == 'wait' and view['qrcode_image'].startswith('data:image/svg+xml;base64,')
    assert 'private-synthetic' not in json.dumps(view)
    _, again = start(q, scope, c.owner)
    assert again['enrollment_id'] == view['enrollment_id'] and p.starts == 1
    assert not c.s.store.all('SELECT * FROM weixin_accounts')
    with pytest.raises(DevError):
        asyncio.run(q.start(args, c.actor))
    with pytest.raises(DevError):
        start(q, scope, c.owner, confirm_credential_storage=False)
    assert p.starts == 1


def test_confirmed_credentials_stay_encrypted_until_exact_owner_accepts(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    confirm(p)
    view = status(q, scope, c.owner, view)
    assert view['state'] == 'confirmed' and view['scanner_id'] == 'synthetic-scanner'
    assert not c.s.store.all('SELECT * FROM weixin_accounts')
    assert 'synthetic-qr-bot-secret' not in json.dumps(view)
    stored = c.s.store.one('SELECT payload FROM weixin_enrollments')['payload']
    assert 'synthetic-qr-bot-secret' not in stored
    args = accept_args(scope, view)
    with pytest.raises(DevError, match='实际返回'):
        q.accept({**args, 'confirm_scanner_id': 'wrong'}, c.owner)
    result = q.accept(args, c.owner)
    assert q.accept(args, c.owner) == result
    assert result['state'] == 'accepted' and result['room_bound'] is False
    recovered = status(q, scope, c.owner, view)
    assert recovered['state'] == 'accepted' and recovered['credential_stored'] is True
    assert recovered['account_id'] == result['account_id']
    cancelled = q.cancel({**scope, 'enrollment_id': view['enrollment_id'],
        'expected_version': view['version'], 'idempotency_key': key()}, c.owner)
    assert cancelled['state'] == 'accepted' and cancelled['account_id'] == result['account_id']
    assert len(c.s.store.all('SELECT * FROM weixin_accounts')) == 1
    assert not c.s.store.all('SELECT * FROM weixin_bindings')
    assert not c.s.store.all('SELECT * FROM operations')
    assert 'synthetic-qr-bot-secret' not in json.dumps(c.s.store.all('SELECT * FROM collaboration_audit'))
    assert 'synthetic-qr-bot-secret' not in json.dumps(c.s.store.all('SELECT * FROM collaboration_idempotency'))
    assert 'token' not in q.payload(c.s.store.one('SELECT * FROM weixin_enrollments'))


def test_expiry_erases_qr_and_pending_credential_without_accept(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    confirm(p)
    view = status(q, scope, c.owner, view)
    c.clock[0] += 301
    with pytest.raises(DevError):
        q.accept(accept_args(scope, view), c.owner)
    # Failed transactional accept rolls back; the lifecycle sweeper is authoritative.
    q.expire()
    row = c.s.store.one('SELECT * FROM weixin_enrollments')
    assert row['state'] == 'expired' and row['payload'] == ''
    assert not c.s.store.all('SELECT * FROM weixin_accounts')


def test_cancel_wins_against_inflight_confirm_and_refresh_uses_new_session(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    raw = {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']}
    args, actor, _, claim = q.prepare_status(raw, c.owner)
    cancel = q.cancel({**raw, 'idempotency_key': key()}, c.owner)
    confirm(p)
    late = q.finish_status(args, actor, claim, p.response)
    assert late['state'] == cancel['state'] == 'cancelled'
    assert c.s.store.one('SELECT payload FROM weixin_enrollments')['payload'] == ''
    _, fresh = start(q, scope, c.owner)
    assert fresh['enrollment_id'] != view['enrollment_id'] and p.starts == 2
    assert not c.s.store.all('SELECT * FROM weixin_accounts')


def test_repeated_status_has_one_inflight_poll_and_lost_response_recovers(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    raw = {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']}
    args, actor, _, claim = q.prepare_status(raw, c.owner)
    assert q.prepare_status(raw, c.owner)[-1] is None
    done = q.finish_status(args, actor, claim, {'status': 'scaned'})
    replay = status(q, scope, c.owner, view)
    assert replay == done and not p.polls
    # An old lease cannot commit over a replacement request after process loss.
    c.clock[0] += 51
    raw['expected_version'] = done['version']
    _, _, _, claim2 = q.prepare_status(raw, c.owner)
    c.clock[0] += 51
    args3, actor3, _, claim3 = q.prepare_status(raw, c.owner)
    assert claim3['poll_fence'] > claim2['poll_fence']
    assert q.finish_status(raw, c.owner, claim2, {'status': 'expired'})['state'] == 'scaned'
    assert q.finish_status(args3, actor3, claim3, {'status': 'wait'})['state'] == 'scaned'


def test_verification_digits_never_enter_receipts_or_replay(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    p.response = {'status': 'need_verifycode'}
    view = status(q, scope, c.owner, view)
    with pytest.raises(DevError):
        status(q, scope, c.owner, view, verify_code='bad')
    view = status(q, scope, c.owner, view, verify_code='432198')
    view = status(q, scope, c.owner, view, verify_code='432198')
    assert [code for _, code in p.polls].count('432198') == 1
    for table in ('weixin_enrollments', 'collaboration_audit', 'collaboration_idempotency'):
        assert '432198' not in json.dumps(c.s.store.all('SELECT * FROM ' + table))


@pytest.mark.parametrize('reply', [
    {'status': 'confirmed', 'bot_token': 'secret'},
    {'status': 'confirmed', 'bot_token': 'secret', 'ilink_bot_id': 'b', 'ilink_user_id': 'u', 'baseurl': 'https://evil.example'},
    {'status': 'scaned_but_redirect', 'redirect_host': 'evil.example'},
    {'status': 'scaned_but_redirect', 'redirect_host': 'ilinkai.weixin.qq.com@evil.example'},
    {'status': 'new-unknown-state'},
    {'status': 'wait', 'ret': True},
])
def test_untrusted_provider_values_fail_closed(enrollment, reply):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    p.response = reply
    view = status(q, scope, c.owner, view)
    assert view['state'] == 'attention' and 'qrcode_image' not in view
    assert not c.s.store.all('SELECT * FROM weixin_accounts')


@pytest.mark.parametrize('state', ['expired', 'verify_code_blocked', 'binded_redirect'])
def test_provider_terminal_states_never_register_an_account(enrollment, state):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    p.response = {'status': state}
    view = status(q, scope, c.owner, view)
    assert view['state'] == state
    assert c.s.store.one('SELECT payload FROM weixin_enrollments')['payload'] == ''
    assert not c.s.store.all('SELECT * FROM weixin_accounts')


def test_start_transport_uncertainty_does_not_issue_new_qr_on_retry(enrollment):
    c, q, scope, p = enrollment
    p.start_error = True
    args, view = start(q, scope, c.owner)
    assert view['state'] == 'attention'
    assert asyncio.run(q.start(args, c.owner)) == view and p.starts == 1
    assert 'synthetic-secret' not in json.dumps(view)


def test_status_failure_preserves_original_session(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    p.status_error = True
    failed = status(q, scope, c.owner, view)
    assert failed['state'] == 'wait' and failed['enrollment_id'] == view['enrollment_id']
    p.status_error = False
    c.clock[0] += 3
    confirm(p)
    done = status(q, scope, c.owner, failed)
    assert done['state'] == 'confirmed' and p.starts == 1


def test_project_change_and_other_conversation_block_existing_qr(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    other = c.s.conversations.create({'title': 'Other', 'projects': [c.scope],
        'idempotency_key': key()}, c.owner)
    conversation_id = other['conversation']['id']
    with pytest.raises(DevError):
        status(q, {**scope, 'conversation_id': conversation_id}, c.owner, view)
    c.s.store.execute("UPDATE projects SET root='/tmp/changed' WHERE id='proj'")
    with pytest.raises(DevError, match='范围变化'):
        status(q, scope, c.owner, view)
    assert not p.polls


def test_account_already_present_is_not_silently_replaced(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    confirm(p)
    view = status(q, scope, c.owner, view)
    old = c.s.weixin.import_account({**scope, 'idempotency_key': key(), 'bot_id': 'synthetic-bot',
        'label': 'Existing', 'token': 'old-synthetic-secret', 'confirm_credential_storage': True,
        'confirm_provider_terms': True}, c.owner)
    with pytest.raises(DevError, match='已有登记'):
        q.accept(accept_args(scope, view), c.owner)
    row = c.s.store.one('SELECT * FROM weixin_accounts WHERE id=?', (old['account_id'],))
    assert row['credential_version'] == 1 and c.s.store.decrypt(row['secret']) == 'old-synthetic-secret'


def test_disable_channel_cancels_pending_login_and_does_not_create_account(enrollment):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    raw = {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']}
    args, actor, _, claim = q.prepare_status(raw, c.owner)
    c.s.channel_settings.control({**scope, 'channel': 'weixin', 'enabled': False,
        'expected_version': 0, 'idempotency_key': key()}, c.owner)
    confirm(p)
    with pytest.raises(DevError):
        q.finish_status(args, actor, claim, p.response)
    row = c.s.store.one('SELECT * FROM weixin_enrollments')
    assert row['state'] == 'cancelled' and row['payload'] == ''
    assert not c.s.store.all('SELECT * FROM weixin_accounts')


def test_real_panel_route_checks_session_csrf_and_hides_credentials(enrollment):
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from hub.auth import Auth
    from hub.collaboration.api import make_router
    c, q, scope, p = enrollment
    from hub.collaboration.monitor import MonitorService
    c.s.monitor = MonitorService(c.s)
    auth = Auth(c.s.store)
    with c.s.store.transaction():
        session = auth.new_session(c.owner.user_id)
    app = FastAPI()
    app.include_router(make_router(auth, SimpleNamespace(collaboration=c.s, store=c.s.store)))

    @app.exception_handler(DevError)
    async def error_handler(request, error):
        return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            args = {**scope, 'idempotency_key': key(), 'confirm_credential_storage': True, 'confirm_provider_terms': True}
            route = '/api/collaboration/weixin-qr-start'
            assert (await client.post(route, json=args)).status_code == 401
            client.cookies.set('rd_session', session['cookie'])
            assert (await client.post(route, json=args)).status_code == 403
            client.headers['X-RD-CSRF'] = session['csrf']
            assert (await client.post(route, json={**args, 'confirm_credential_storage': False})).status_code == 422
            response = await client.post(route, json=args)
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'private, no-store'
            view = response.json()
            assert view['state'] == 'wait' and p.starts == 1
            confirm(p)
            response = await client.post('/api/collaboration/weixin-qr-status', json={
                **scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']})
            assert response.status_code == 200, response.text
            view = response.json()
            assert view['state'] == 'confirmed'
            assert 'synthetic-qr-bot-secret' not in response.text
            response = await client.post('/api/collaboration/weixin-qr-accept', json=accept_args(scope, view))
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'private, no-store'
            assert response.json()['room_bound'] is False
    asyncio.run(exercise())


@pytest.mark.parametrize('code,http_status', [('WEIXIN_AUTH_REJECTED', 403), ('WEIXIN_RATE_LIMITED', 429)])
def test_qr_provider_rejection_stops_or_backs_off(enrollment, code, http_status):
    c, q, scope, p = enrollment
    _, view = start(q, scope, c.owner)
    calls = []
    class Provider:
        async def qr_status(self, *args):
            calls.append(args)
            raise DevError(code, 'private-upstream-error', http_status, retry_after_seconds=90)
    c.s.weixin.client_factory = lambda *_: Provider()
    view = status(q, scope, c.owner, view)
    assert view['state'] == ('attention' if http_status == 403 else 'wait')
    status(q, scope, c.owner, view)
    assert len(calls) == 1
    assert 'private-upstream-error' not in json.dumps(view)


def test_feature_views_follow_owner_channel_overrides(enrollment):
    c, q, scope, p = enrollment
    c.s.channel_settings.control({**scope, 'channel': 'weixin', 'enabled': False,
        'expected_version': 0, 'idempotency_key': key()}, c.owner)
    assert not c.s.features(c.owner)['weixin_enabled']
    assert not c.s.chatroom.capabilities(c.owner)['weixin_enabled']
    overview = c.s.read({**scope, 'kind': 'overview'}, c.owner)
    assert not overview['features']['weixin_enabled']
    assert not overview['capabilities']['weixin_enabled']
