"""Audit regressions use synthetic identities and transports, never external I/O."""
import asyncio
import json
from types import SimpleNamespace

from hub.collaboration import channel_inbound
from hub.collaboration.events import EventService
from hub.collaboration.telegram_protocol import TelegramAdapter
from hub.collaboration.weixin_protocol import WireReply, incoming_text, upload_timeout
from shared.util import DevError
from tests.collaboration_support import collab as collab
from tests.test_collaboration_dot_chat import duplex
from tests.test_dot_telegram_channel import setup, update, ingest as tg_ingest
from tests.test_dot_weixin_channel import fixture_channel, message, ingest as wx_ingest


def test_telegram_poison_message_isolated_and_offset_advances(collab, monkeypatch):
    c, t, account, _, _ = setup(duplex(collab))
    original = t.ingest

    def ingest(account, update, eligible):
        if update['update_id'] == 2:
            with t.store.transaction():
                t.store.execute("UPDATE telegram_accounts SET label='rolled back' WHERE id=?", (account['id'],))
                raise DevError('TELEGRAM_REPLY_SCOPE', 'synthetic', 403)
        return original(account, update, eligible)

    monkeypatch.setattr(t, 'ingest', ingest)
    result = tg_ingest(c, t, account, [update(2), update(3)])
    assert result['offset'] == 4
    assert len(result['message_ids']) == 1
    assert t.store.one('SELECT label FROM telegram_accounts')['label'] == 'Fixture'
    assert t.store.one('SELECT state FROM telegram_inbox WHERE update_id=2')['state'] == 'quarantined'
    health = channel_inbound.summary(t.store, 'telegram', account['account_id'])
    assert health == [{'state': 'quarantined', 'reason_code': 'TELEGRAM_REPLY_SCOPE', 'count': 1}]


def test_telegram_deferred_body_replays_once_without_new_scope(collab, monkeypatch):
    c, t, account, _, _ = setup(duplex(collab))
    original = t.ingest
    def blocked(*args):
        raise DevError('TELEGRAM_DOT_QUOTA', 'synthetic backlog', 429)
    monkeypatch.setattr(t, 'ingest', blocked)
    assert tg_ingest(c, t, account, [update(2, 'keep this body')])['offset'] == 3
    row = t.store.one('SELECT * FROM channel_inbound_failures')
    assert row['state'] == 'deferred' and 'keep this body' not in row['body']
    monkeypatch.setattr(t, 'ingest', original)
    t.replay_deferred(account['account_id'])
    t.replay_deferred(account['account_id'])
    assert t.store.one('SELECT state FROM telegram_inbox WHERE update_id=2')['state'] == 'committed'
    assert t.store.one('SELECT COUNT(*) n FROM collaboration_messages WHERE origin=\'external_channel\'')['n'] == 1
    assert t.store.one('SELECT body FROM channel_inbound_failures')['body'] == ''


def test_weixin_poison_message_keeps_following_message_and_cursor(collab):
    c, w, account, _, _ = fixture_channel(collab)
    result = wx_ingest(c, w, account, {'ret': 0, 'msgs': [
        message(2, item_list=[None]), message(3, 'healthy')], 'get_updates_buf': 'after-poison'})
    assert len(result['message_ids']) == 1
    assert w.store.decrypt(w.store.one('SELECT cursor FROM weixin_accounts')['cursor']) == 'after-poison'
    assert w.store.one('SELECT state FROM weixin_accounts')['state'] == 'active'
    assert w.store.one('SELECT reason_code FROM channel_inbound_failures')['reason_code'] == 'WEIXIN_ITEMS_INVALID'


def test_weixin_deferred_body_cannot_cross_binding_version(collab, monkeypatch):
    c, w, account, binding, _ = fixture_channel(collab)
    original = w.ingest_one
    def blocked(*args):
        raise DevError('WEIXIN_INBOX_QUOTA', 'synthetic backlog', 429)
    monkeypatch.setattr(w, 'ingest_one', blocked)
    first = wx_ingest(c, w, account)
    assert not first['accepted'] and not first['cursor_persisted']
    w.store.execute('UPDATE weixin_bindings SET version=version+1 WHERE id=?', (binding['binding_id'],))
    w.replay_deferred(account['account_id'])
    assert w.store.one('SELECT reason_code FROM channel_inbound_failures')['reason_code'] == 'binding_changed_before_replay'
    monkeypatch.setattr(w, 'ingest_one', original)

    # An unadvanced provider cursor returns the original event again. A new
    # binding version or rotated transport context must not re-admit it.
    replay = wx_ingest(c, w, account, {'msgs': [message(2, context_token='rotated')], 'get_updates_buf': 'after-stale'})
    assert replay['cursor_persisted'] and not replay['message_ids']
    assert not w.store.one("SELECT 1 FROM weixin_inbound WHERE external_id='2'")
    assert w.store.one('SELECT COUNT(*) AS n FROM channel_inbound_failures')['n'] == 1


def test_weixin_long_text_is_bounded_not_account_failure(collab):
    c, w, account, _, _ = fixture_channel(collab)
    result = wx_ingest(c, w, account, {'msgs': [message(2, '汉' * 10000)], 'get_updates_buf': 'long'})
    assert len(result['message_ids']) == 1
    body = json.loads(w.store.one('SELECT body FROM collaboration_messages WHERE id=?', (result['message_ids'][0],))['body'])
    assert '[已截断]' in body['body_text']
    assert len(body['body_text'].encode()) <= 24000


def test_upload_deadlines_scale_without_unbounded_timeout():
    assert upload_timeout(0) == 40
    assert 150 < upload_timeout(32 * 1024 * 1024) < 180
    assert upload_timeout(1024 ** 3) == 900
    assert len(incoming_text('汉' * 10000, 12000).encode()) <= 12000

    async def scenario():
        calls = []
        async def transport(method, url, headers, body, **kwargs):
            calls.append(kwargs)
            return WireReply(200, b'{"ok":true,"result":{"message_id":5,"chat":{"id":101}}}')
        client = TelegramAdapter('900001:synthetic_telegram_token_fixture_only', transport=transport)
        assert await client.send_document('101', b'x' * (12 * 1024 * 1024), 'test.bin') == '5'
        assert calls[0]['timeout'] > 70
    asyncio.run(scenario())


def test_unknown_mirror_history_is_not_refreshed_each_tick(collab, monkeypatch):
    c = duplex(collab)
    router = c.s.channels
    seen = []
    monkeypatch.setattr(router, 'refresh', seen.append)
    router.store.execute("""INSERT INTO channel_mirror_parts
        (id,delivery_id,ordinal,kind,body,state,created,updated) VALUES
        ('old','old-delivery',0,'text','{}','unknown',0,0),
        ('new','new-delivery',0,'text','{}','sending',0,0)""")
    router.reconcile()
    assert seen == ['new-delivery']
    seen.clear()
    router.reconcile()
    assert not seen


def test_event_internal_failure_is_finished_without_escaping():
    finishes = []
    class Store:
        async def run(self, fn, *args):
            return fn(*args)
    c = SimpleNamespace(store=Store())
    service = EventService(c)
    def broken(item):
        raise KeyError('synthetic-corrupt-key')
    service.ready = broken
    service.finish = lambda *args: finishes.append(args)
    asyncio.run(service.deliver({'body': b'{}'}))
    assert finishes[0][-1] == 'delivery_internal_error'


def test_mirror_claim_failure_has_terminal_visible_reason(collab, monkeypatch):
    c = duplex(collab)
    router = c.s.channels
    router.store.execute("""INSERT INTO channel_mirror_parts
        (id,delivery_id,ordinal,kind,body,state,created,updated)
        VALUES ('bad','missing',0,'text','{}','queued',0,0)""")
    def broken(pid):
        raise ValueError('synthetic')
    monkeypatch.setattr(router, 'claim', broken)
    assert router.safe_claim('bad') is None
    row = router.store.one('SELECT state,reason_code FROM channel_mirror_parts WHERE id=\'bad\'')
    assert row == {'state': 'rejected', 'reason_code': 'claim_internal_error'}


def test_unidentified_pairing_senders_do_not_trigger_bot_replies(collab):
    from tests.test_channel_enrollment_flow import prepare, send, incoming
    c, transport, _, account, raw = prepare(collab, 'weixin')
    transport.bind(raw, c.owner)
    for i in range(1, 5):
        send(c, transport, account, incoming(c, 'weixin', i, 'hello stranger'))
    assert not c.s.store.all('SELECT * FROM channel_onboarding_notices')


def test_known_pairing_peer_gets_one_notice_for_entire_window(collab):
    from tests.test_channel_enrollment_flow import prepare, send, incoming
    c, transport, _, account, raw = prepare(collab, 'telegram')
    raw['owner_preapproval']['expected_peer_id'] = '101'
    transport.bind(raw, c.owner)
    for i in range(1, 5):
        send(c, transport, account, incoming(c, 'telegram', i, 'hello'))
    assert c.s.store.one("SELECT COUNT(*) n FROM channel_onboarding_notices WHERE kind='pairing_required'")['n'] == 1


def test_weixin_reply_quota_visible_and_success_history_excluded(collab):
    from tests.test_dot_weixin_channel import reply
    c, w, account, _, _ = fixture_channel(collab)
    _, posted, delivery = reply(c, w, account)
    original = w.store.one('SELECT * FROM collaboration_messages WHERE id=?', (delivery['message_id'],))
    columns = list(delivery)
    with w.store.transaction():
        for n in range(1000):
            row = {**delivery, 'id': 'history-' + str(n), 'message_id': 'history-message-' + str(n), 'state': 'accepted'}
            w.store.execute('INSERT INTO weixin_deliveries (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                            tuple(row[k] for k in columns))
        allowed = w.delivery.queue_reply({**original, 'id': 'allowed'})
        assert allowed['state'] == 'queued'
        w.store.execute("UPDATE weixin_deliveries SET state='queued' WHERE id LIKE 'history-%'")
        rejected = w.delivery.queue_reply({**original, 'id': 'quota-overflow'})
        assert rejected['state'] == 'needs_attention'
    assert w.store.one('SELECT reason_code FROM weixin_parts WHERE delivery_id=?', (rejected['id'],))['reason_code'] == 'daily_reply_quota'


def test_event_tick_keeps_other_inflight_delivery_when_child_crashes():
    class Store:
        async def run(self, fn, *args):
            return fn(*args)
    c = SimpleNamespace(store=Store(), clock=lambda: 0)
    service = EventService(c)
    service._relay_recover_after = 1
    batches = iter([[{'id': 1}, {'id': 2}]])
    service.reserve = lambda _: next(batches, [])
    completed = []

    async def deliver(item):
        if item['id'] == 1:
            raise ValueError('synthetic child crash')
        await asyncio.sleep(.02)
        completed.append(item['id'])
    service.deliver = deliver
    asyncio.run(service.tick())
    assert completed == [2]


def test_inbound_health_summary_only_includes_authorized_account_ids(collab):
    c = duplex(collab)
    for account in ('visible', 'other-owner'):
        c.s.store.execute("""INSERT INTO channel_inbound_failures
            (id,channel,account_id,scope,body,state,reason_code,created,updated)
            VALUES (?,'weixin',?,'{}','','deferred','BACKLOG',0,0)""", (account, account))
    result = channel_inbound.summaries(c.s.store, 'weixin', ['visible'])
    assert list(result) == ['visible']
    assert result['visible'] == [{'state': 'deferred', 'reason_code': 'BACKLOG', 'count': 1}]


def test_upload_preview_estimate_has_no_authorization_effect():
    from hub.collaboration.weixin_protocol import upload_estimate
    estimate = upload_estimate([32 * 1024 * 1024, 1024])
    assert estimate['estimated_seconds'] == 198
    assert '未知' in estimate['note']
    assert 'approved' not in estimate and 'queued' not in estimate


def test_nested_weixin_items_are_isolated_from_valid_messages(collab):
    c, w, account, _, _ = fixture_channel(collab)
    bad_items = [
        {'type': 1, 'text_item': None},
        {'type': 1, 'text_item': []},
        {'type': 1, 'text_item': {'text': None}},
        {'type': 2, 'image_item': []},
        {'type': 4, 'file_item': {'media': None}},
        {'type': 5, 'video_item': {'media': []}},
    ]
    messages = [message(i + 2, item_list=[item]) for i, item in enumerate(bad_items)]
    messages.append(message(100, 'valid after malformed nesting'))
    result = wx_ingest(c, w, account, {'msgs': messages, 'get_updates_buf': 'nested-after'})
    assert len(result['message_ids']) == 1
    assert w.store.decrypt(w.store.one('SELECT cursor FROM weixin_accounts')['cursor']) == 'nested-after'
    assert w.store.one("SELECT COUNT(*) n FROM channel_inbound_failures WHERE reason_code='WEIXIN_ITEMS_INVALID'")['n'] == len(bad_items)


def test_nested_telegram_reply_isolated_from_valid_messages(collab):
    c, t, account, _, _ = setup(duplex(collab))
    result = tg_ingest(c, t, account, [
        update(2, reply_to_message=None), update(3, reply_to_message=[]), update(4)])
    assert result['offset'] == 5 and len(result['message_ids']) == 1
    assert t.store.one("SELECT COUNT(*) n FROM channel_inbound_failures WHERE reason_code='TELEGRAM_MESSAGE_INVALID'")['n'] == 2


def test_committed_quarantine_history_does_not_exhaust_unresolved_budget(collab):
    c = duplex(collab)
    account = {'id': 'synthetic-account', 'generation': 1, 'credential_version': 1}
    service = SimpleNamespace(store=c.s.store, c=c.s)
    with c.s.store.transaction():
        c.s.store.db.executemany("""INSERT INTO channel_inbound_failures
            (id,channel,account_id,scope,body,state,reason_code,created,updated)
            VALUES (?,'weixin','synthetic-account','{}','','committed','',0,0)""",
            [(str(i),) for i in range(5000)])
        def backlog():
            raise DevError('WEIXIN_INBOX_QUOTA', 'synthetic backlog', 429)
        _, reason = channel_inbound.isolate(service, 'weixin', account, {}, {'id': 'new'}, backlog)
    assert reason == 'deferred:WEIXIN_INBOX_QUOTA'
    assert c.s.store.one('SELECT COUNT(*) n FROM channel_inbound_failures')['n'] == 5001


def test_deferred_payload_survives_offline_synthetic_key_rotation(tmp_path):
    from hub.keyring import rotate_key
    from hub.store import Store

    directory = tmp_path / 'synthetic-hub'
    account = {'id': 'synthetic-account', 'generation': 1, 'credential_version': 1}
    scope, source = {'binding_id': 'synthetic-binding', 'version': 1}, {'body': 'preserve deferred source'}
    store = Store(directory)
    service = SimpleNamespace(store=store, c=SimpleNamespace(clock=lambda: 1))
    try:
        with store.transaction():
            def backlog():
                raise DevError('WEIXIN_INBOX_QUOTA', 'synthetic backlog', 429)
            channel_inbound.isolate(service, 'weixin', account, scope, source, backlog)
        ciphertext = store.one('SELECT body FROM channel_inbound_failures')['body']
    finally:
        store.close()
    for _ in range(2):
        receipt = rotate_key(directory)
        store = Store(directory)
        try:
            service.store = store
            row = channel_inbound.pending(service, 'weixin', account['id'])[0]
            assert row['body'] != ciphertext
            assert row['body'].startswith('cp1:' + receipt['key_id'] + ':')
            assert channel_inbound.unpack(service, row)['source'] == source
            ciphertext = row['body']
        finally:
            store.close()
    store = Store(directory)
    try:
        service.store = store
        saved = channel_inbound.unpack(service, channel_inbound.pending(service, 'weixin', account['id'])[0])
        with store.transaction():
            result, reason = channel_inbound.isolate(service, 'weixin', account, saved['scope'],
                saved['source'], lambda: 'canonical-message-created', replay=True)
        assert result == 'canonical-message-created' and not reason
        row = store.one('SELECT state,body FROM channel_inbound_failures')
        assert row == {'state': 'committed', 'body': ''}
    finally:
        store.close()


def test_quarantine_updates_only_unsent_parts_even_in_mixed_attention_delivery(collab, monkeypatch):
    c = duplex(collab)
    store, router = c.s.store, c.s.channels
    for did, state in [('mixed', 'needs_attention'), ('done', 'accepted')]:
        store.execute("""INSERT INTO channel_mirrors
            (id,channel,binding_id,binding_version,generation,credential_version,message_id,message_version,
             message_digest,room_id,conversation_id,state,created,updated)
            VALUES (?,'telegram','binding',1,1,1,?,1,'digest','room','conversation',?,0,0)""",
            (did, did, state))
    for pid, did, ordinal, state in [('unknown', 'mixed', 0, 'unknown'),
                                     ('queued', 'mixed', 1, 'queued'), ('done-part', 'done', 0, 'accepted')]:
        store.execute("""INSERT INTO channel_mirror_parts
            (id,delivery_id,ordinal,kind,body,state,created,updated) VALUES (?,?,?,'text','{}',?,0,0)""",
            (pid, did, ordinal, state))
    refresh = router.refresh
    seen = []
    def recording(did):
        seen.append(did)
        refresh(did)
    monkeypatch.setattr(router, 'refresh', recording)
    with store.transaction():
        router.quarantine('telegram', 'binding', 'channel_disabled')
    assert seen == ['mixed']
    assert store.one("SELECT state FROM channel_mirror_parts WHERE id='unknown'")['state'] == 'unknown'
    assert store.one("SELECT state FROM channel_mirror_parts WHERE id='queued'")['state'] == 'rejected'
