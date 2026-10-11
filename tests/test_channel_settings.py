"""Synthetic, persistent owner channel switches; no real provider or credentials."""
import asyncio
import base64
from dataclasses import replace

import pytest

from hub.collaboration.config import CollaborationConfig
from hub.collaboration.service import CollaborationService
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, no_work
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet, reply
from tests.test_dot_telegram_channel import setup as telegram_setup, update


def switch(c, channel, enabled, **extra):
    setting = c.s.channel_settings.setting(channel, c.owner)
    return c.s.channel_settings.control({**c.scope, 'conversation_id': c.slot['conversation_id'],
        'channel': channel, 'enabled': enabled, 'expected_version': setting['version'],
        'idempotency_key': key(), **extra}, c.owner)


def test_defaults_and_explicit_environment_override(monkeypatch):
    for name in ('CODEPIER_COLLABORATION_ENABLED', 'CODEPIER_WEIXIN_ENABLED', 'CODEPIER_TELEGRAM_ENABLED',
                 'CODEPIER_MCP_EVENTS_ENABLED', 'CODEPIER_MONITOR_COLLECTOR_ENABLED',
                 'CODEPIER_ANALYSIS_DISPATCH_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    assert not CollaborationConfig.from_env().enabled
    assert not CollaborationConfig.from_env().weixin_enabled
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    assert CollaborationConfig.from_env().weixin_enabled
    assert not CollaborationConfig.from_env().telegram_enabled
    monkeypatch.setenv('CODEPIER_WEIXIN_ENABLED', 'false')
    assert not CollaborationConfig.from_env().weixin_enabled
    monkeypatch.setenv('CODEPIER_WEIXIN_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'false')
    assert not CollaborationConfig.from_env().weixin_enabled  # Compose bootstrap cannot bypass master gate.


def test_panel_switch_is_persistent_independent_audited_and_read_only_until_changed(collab):
    c = duplex(collab)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    assert c.s.weixin.listing(c.scope, c.owner)['setting']['version'] == 0
    assert c.s.telegram.listing(c.scope, c.owner)['enabled'] is False
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    first = switch(c, 'weixin', False)
    assert first['version'] == 1 and first['source'] == 'panel'
    assert switch(c, 'telegram', True)['enabled']
    assert not c.s.channel_settings.enabled('weixin', c.owner)
    assert c.s.channel_settings.enabled('telegram', c.owner)
    assert c.s.store.one("SELECT COUNT(*) AS n FROM collaboration_audit WHERE action='channel.setting_changed'")['n'] == 2
    reopened = Store(c.s.store.directory)
    try:
        restored = CollaborationService(Runtime(reopened), CollaborationConfig(enabled=True))
        assert not restored.channel_settings.enabled('weixin', c.owner)
        assert restored.channel_settings.enabled('telegram', c.owner)
        assert restored.channel_settings.setting('weixin', c.owner)['version'] == 1
    finally:
        reopened.close()
    assert not c.s.store.all('SELECT * FROM weixin_accounts')
    assert not c.s.store.all('SELECT * FROM telegram_accounts')
    no_work(c)


def test_explicit_off_needs_panel_change_and_owner_scope_isolated(collab):
    c = duplex(collab)
    c.s.config = replace(c.s.config, weixin_enabled=False)
    assert not c.s.weixin.listing(c.scope, c.owner)['enabled']
    switch(c, 'weixin', True)
    assert c.s.channel_settings.enabled('weixin', c.owner)
    assert not c.s.channel_settings.enabled('weixin', {'space_id': c.owner.space_id, 'owner_user_id': 'other'})
    assert not c.s.channel_settings.enabled('weixin', {'space_id': 'other', 'owner_user_id': c.owner.user_id})


def test_stale_conflicting_repeated_and_unprivileged_switches(collab):
    c = duplex(collab)
    args = {**c.scope, 'channel': 'weixin', 'enabled': False, 'expected_version': 0, 'idempotency_key': key()}
    first = c.s.channel_settings.control(args, c.owner)
    assert c.s.channel_settings.control(args, c.owner) == first
    with pytest.raises(DevError) as error:
        c.s.channel_settings.control({**args, 'enabled': True}, c.owner)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(DevError) as error:
        c.s.channel_settings.control({**args, 'idempotency_key': key()}, c.owner)
    assert error.value.code == 'STALE_VERSION'
    with pytest.raises(DevError) as error:
        c.s.channel_settings.control({**args, 'idempotency_key': key()}, c.actor)
    assert error.value.status == 403
    with pytest.raises(DevError):
        c.s.channel_settings.control({**args, 'enabled': 'false', 'idempotency_key': key()}, c.owner)
    assert c.s.channel_settings.setting('weixin', c.owner)['version'] == 1


def test_weixin_pause_fences_poll_preserves_credentials_bindings_and_cursor(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    claim = w.claim_poll(account['account_id'])
    before = c.s.store.one('SELECT * FROM weixin_accounts WHERE id=?', (account['account_id'],))
    original_binding = w.binding(binding['binding_id'], c.owner, c.room)
    switch(c, 'weixin', False)
    with pytest.raises(DevError):
        w.commit_updates(claim, packet())
    after = c.s.store.one('SELECT * FROM weixin_accounts WHERE id=?', (account['account_id'],))
    assert all(after[k] == before[k] for k in ('secret', 'cursor', 'credential_version', 'generation', 'state'))
    assert w.binding(binding['binding_id'], c.owner, c.room) == original_binding
    w.client_factory = lambda *args: pytest.fail('Disabled channel must not contact provider')
    asyncio.run(w.tick())
    switch(c, 'weixin', True)
    with pytest.raises(DevError):
        w.commit_updates(claim, packet())  # Re-enable cannot restore the old lease.
    current = w.claim_poll(account['account_id'])
    assert w.commit_updates(current, packet())['message_ids']  # Original cursor can safely catch up.
    no_work(c)


def test_telegram_pause_fences_poll_and_runtime_enable_overrides_process_default(collab):
    c = duplex(collab)
    c, t, account, binding, scope = telegram_setup(c)
    claim = t.claim_poll(account['account_id'])
    original = t.binding(binding['binding_id'], c.owner, c.room)
    switch(c, 'telegram', False)
    with pytest.raises(DevError):
        t.commit_updates(claim, [update(2)])
    assert t.claim_poll(account['account_id']) is None
    c.s.config = replace(c.s.config, telegram_enabled=False)
    switch(c, 'telegram', True)
    assert t.binding(binding['binding_id'], c.owner, c.room) == original
    assert t.claim_poll(account['account_id'])
    assert c.s.channel_settings.enabled('weixin', c.owner)


def test_disable_then_enable_does_not_replay_queued_or_unknown_sends(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    _, _, delivery = reply(c, w, account)
    part = c.s.store.one('SELECT * FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))
    switch(c, 'weixin', False)
    assert c.s.store.one('SELECT state FROM weixin_parts WHERE id=?', (part['id'],))['state'] == 'cancelled'
    switch(c, 'weixin', True)
    w.client_factory = lambda *args: pytest.fail('Old unsent messages cannot automatically replay')
    asyncio.run(w.delivery.tick())
    posted = c.s.invoke('collaboration', {'action': 'dot_message_v2', **c.scope,
        'dot_id': c.slot['id'], 'body_text': 'A new reply after re-enable',
        'reply_to_id': delivery['source_message_id'], 'idempotency_key': key()}, c.actor)
    next_part = c.s.store.one("""SELECT p.id FROM weixin_parts p JOIN weixin_deliveries d
        ON d.id=p.delivery_id WHERE d.message_id=?""", (posted['message']['id'],))
    assert next_part and w.delivery.claim(next_part['id'])  # Cancelled old work cannot block the new lane.
    c.s.store.execute("UPDATE weixin_parts SET state='unknown' WHERE id=?", (part['id'],))
    switch(c, 'weixin', False)
    assert c.s.store.one('SELECT state FROM weixin_parts WHERE id=?', (part['id'],))['state'] == 'unknown'


def test_disable_after_prepare_prevents_external_send(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    _, _, delivery = reply(c, w, account)
    part = c.s.store.one('SELECT id FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))
    original = w.delivery.mark_sending
    def pause_after_preparing(*args):
        wire = original(*args)
        switch(c, 'weixin', False)
        return wire
    w.delivery.mark_sending = pause_after_preparing
    class Provider:
        async def send(self, *args):
            pytest.fail('Disabled before provider send')
    w.client_factory = lambda *args: Provider()
    asyncio.run(w.delivery.send_once(part['id']))
    assert c.s.store.one('SELECT state FROM weixin_parts WHERE id=?', (part['id'],))['state'] == 'unknown'


def test_switch_cancels_pending_qr_without_affecting_terminal_history(collab):
    c = duplex(collab)
    for state in ('waiting', 'scanned', 'confirmed', 'accepted'):
        c.s.store.execute("""INSERT INTO weixin_enrollments
            (id,space_id,owner_user_id,room_id,state,payload,expires_at,created,updated,conversation_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (state, c.owner.space_id, c.owner.user_id, c.room['id'], state, 'synthetic-payload',
             c.clock[0] + 100, c.clock[0], c.clock[0], c.slot['conversation_id']))
    switch(c, 'weixin', False)
    rows = {r['id']: r for r in c.s.store.all('SELECT * FROM weixin_enrollments')}
    for state in ('waiting', 'scanned', 'confirmed'):
        assert rows[state]['state'] == 'cancelled' and rows[state]['payload'] == ''
        assert rows[state]['version'] == 2
    assert rows['accepted']['state'] == 'accepted'


def test_runtime_loops_are_available_even_when_initial_channel_defaults_off(collab):
    from hub.collaboration.lifecycle import CollaborationLoops
    c = duplex(collab)
    c.s.config = replace(c.s.config, weixin_enabled=False, telegram_enabled=False)
    c.s.health = {}
    loops = CollaborationLoops(c.s)
    async def run():
        await loops.start()
        names = {t.get_name() for t in loops.tasks}
        await loops.stop()
        return names
    names = asyncio.run(run())
    assert {'collaboration:weixin_poll', 'collaboration:telegram_poll', 'collaboration:channel_mirrors'} <= names


def test_channel_switch_http_requires_session_and_csrf(collab):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from hub.auth import Auth
    from hub.collaboration.api import make_router
    from hub.collaboration.monitor import MonitorService
    c = duplex(collab)
    c.s.monitor = MonitorService(c.s)
    auth = Auth(c.s.store)
    with c.s.store.transaction():
        session = auth.new_session(c.owner.user_id)
    app = FastAPI()
    app.include_router(make_router(auth, SimpleNamespace(collaboration=c.s, store=c.s.store)))

    @app.exception_handler(DevError)
    async def error_handler(request, error):
        return JSONResponse({'error': {'code': error.code}}, status_code=error.status)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            path = '/api/collaboration/channel-settings'
            body = {**c.scope, 'channel': 'weixin', 'enabled': False,
                    'expected_version': 0, 'idempotency_key': key()}
            assert (await client.post(path, json=body)).status_code == 401
            client.cookies.set('rd_session', session['cookie'])
            assert (await client.post(path, json=body)).status_code == 403
            assert not c.s.store.all('SELECT * FROM collaboration_channel_settings')
            client.headers['X-RD-CSRF'] = session['csrf']
            response = await client.post(path, json=body)
            assert response.status_code == 200, response.text
            assert response.json()['enabled'] is False
            assert (await client.post(path, json=body)).json() == response.json()
            assert c.s.channel_settings.setting('weixin', c.owner)['version'] == 1
    asyncio.run(exercise())


def test_reenable_resumes_only_original_inbound_media_and_rechecks_fence(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    key_b64 = base64.b64encode(b'1234567890abcdef').decode()
    source = ingest(c, w, account, packet(2, item_list=[
        {'type': 4, 'file_item': {'file_name': 'fixture.txt',
         'media': {'aes_key': key_b64, 'encrypt_query_param': 'synthetic-param', 'encrypt_type': 1}}}
    ]))['message_ids'][0]
    aid = w.media.for_message(source)[0]['id']
    claim = w.media.claim(aid)
    switch(c, 'weixin', False)
    assert w.media.for_message(source)[0]['state'] == 'blocked'
    with pytest.raises(DevError):
        w.media.commit(claim, b'old bytes')
    switch(c, 'weixin', True)
    assert w.media.for_message(source)[0]['state'] == 'pending'
    with pytest.raises(DevError):
        w.media.commit(claim, b'old bytes')
    current = w.media.claim(aid)
    w.media.commit(current, b'new bytes')
    assert w.media.for_message(source)[0]['state'] == 'ready'
    # Outbound media and independent blocked reasons are deliberately not resumed.
    c.s.store.execute("UPDATE channel_attachments SET direction='outbound',state='blocked',reason_code='channel_disabled' WHERE id=?", (aid,))
    switch(c, 'weixin', True)
    assert w.media.for_message(source)[0]['state'] == 'blocked'

def test_telegram_disable_during_provider_verification_stops_following_poll(collab):
    c = duplex(collab)
    c, t, account, binding, scope = telegram_setup(c)
    calls = []
    class Provider:
        async def verify(self, bot_id):
            calls.append('verify')
            await c.s.store.run(switch, c, 'telegram', False)
        async def updates(self, offset):
            pytest.fail('Provider poll cannot start after channel disabled during verification')
    t.client_factory = lambda *args: Provider()
    asyncio.run(t.poll_once(account['account_id']))
    assert calls == ['verify']
    assert c.s.store.one('SELECT state FROM telegram_accounts WHERE id=?', (account['account_id'],))['state'] == 'active'
