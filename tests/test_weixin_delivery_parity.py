"""Weixin parity transport regressions using the real persisted outbox."""
import asyncio
import sqlite3

import pytest

from hub.collaboration import weixin_protocol, weixin_schema
from hub.collaboration.weixin_delivery import text_items
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet, reply
from tests.test_room_weixin_native import setup_channel


def post_reply(c, source, text):
    return c.s.invoke('collaboration', {'action': 'dot_message_v2', **c.scope,
        'dot_id': c.slot['id'], 'body_text': text, 'reply_to_id': source, 'idempotency_key': key()}, c.actor)


def part_for(store, delivery):
    return store.one('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal LIMIT 1', (delivery['id'],))


def test_rate_limit_respects_retry_after_and_freezes_wire(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    part = part_for(c.s.store, delivery)
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((peer, context, client_id, items))
            if len(calls) == 1:
                raise DevError('WEIXIN_RATE_LIMITED', 'rate limited', 429, retry_after_seconds=120)
            return {'provider_state': 'accepted'}

    channel.client_factory = lambda *_: Provider()
    asyncio.run(channel.delivery.tick())
    queued = part_for(c.s.store, delivery)
    assert (queued['state'], queued['next_attempt'], queued['attempts']) == ('queued', c.clock[0] + 120, 1)
    with c.s.store.transaction():
        c.s.store.execute('DELETE FROM weixin_contexts')
    asyncio.run(channel.delivery.tick())
    assert len(calls) == 1
    c.clock[0] = queued['next_attempt']
    asyncio.run(channel.delivery.tick())
    waiting = part_for(c.s.store, delivery)
    assert waiting['state'] == 'waiting_context' and waiting['retry_rejected'] == 1
    ingest(c, channel, account, packet(3, 'fresh context', context_token='new-private-context'))
    c.clock[0] = waiting['next_attempt']
    asyncio.run(channel.delivery.tick())
    assert calls[0] == calls[1] and calls[0][2] == part['client_id']
    assert part_for(c.s.store, delivery)['state'] == 'accepted'


def test_rate_limit_exhaustion_skips_unsent_tail_and_releases_peer(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account, '正文' * 550)
    independent = ingest(c, channel, account, packet(3, 'unrelated'))['message_ids'][0]
    post_reply(c, independent, '独立回复')
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((client_id, items[0]['text_item']['text']))
            if items[0]['text_item']['text'] == '独立回复':
                return {'provider_state': 'accepted'}
            raise DevError('WEIXIN_RATE_LIMITED', 'rate limited', 429, retry_after_seconds=0)

    channel.client_factory = lambda *_: Provider()
    for attempt in range(1, 7):
        asyncio.run(channel.delivery.tick())
        part = part_for(c.s.store, delivery)
        assert part['attempts'] == attempt
        if attempt == 1:
            asyncio.run(channel.delivery.tick())  # The unrelated reply can pass a retrying group.
        if attempt < 6:
            assert part['state'] == 'queued'
            c.clock[0] = part['next_attempt']
    parts = c.s.store.all('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (delivery['id'],))
    assert [(p['state'], p['attempts'], p['reason_code']) for p in parts] == [
        ('rejected', 6, 'WEIXIN_RATE_LIMITED'), ('rejected', 0, 'reply_part_failed')]
    assert [client for client, value in calls if value != '独立回复'] == [part['client_id']] * 6
    assert [value for _, value in calls].count('独立回复') == 1


def test_unknown_blocks_related_replies_only_and_late_receipt_is_fenced(collab):
    c, channel, account, _, _ = fixture_channel(collab)
    source, _, delivery = reply(c, channel, account, '正文' * 550)
    post_reply(c, source, '同一来源的下一条回复')
    independent = ingest(c, channel, account, packet(3, 'unrelated'))['message_ids'][0]
    post_reply(c, independent, '独立回复')
    part = part_for(c.s.store, delivery)
    old = channel.delivery.claim(part['id'])
    channel.delivery.mark_sending(old, text_items('正文'))
    assert not channel.delivery.candidates(c.clock[0])
    c.clock[0] += 181
    channel.delivery.reconcile()
    channel.delivery.finish({**old, 'lease_owner': 'unrelated-worker'}, {'provider_state': 'accepted'})
    assert not c.s.store.all('SELECT * FROM weixin_late_observations')
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((client_id, items[0]['text_item']['text']))
            return {'provider_state': 'accepted'}

    channel.client_factory = lambda *_: Provider()
    asyncio.run(channel.delivery.tick())
    assert [value for _, value in calls] == ['独立回复']
    assert part_for(c.s.store, delivery)['state'] == 'unknown'

    channel.delivery.finish(old, {'provider_state': 'accepted', 'provider_message_id': 'late-receipt'})
    assert part_for(c.s.store, delivery)['state'] == 'unknown'
    assert len(c.s.store.all('SELECT * FROM weixin_late_observations')) == 1
    asyncio.run(channel.delivery.tick())
    asyncio.run(channel.delivery.tick())
    saved = part_for(c.s.store, delivery)
    assert (saved['state'], saved['attempts'], saved['reason_code']) == ('accepted', 1, 'provider_accepted_late')
    assert all(client_id != part['client_id'] for client_id, _ in calls)
    assert calls[-1][1] == '同一来源的下一条回复'


def test_encoded_budget_splits_unattempted_unicode_and_preserves_order(collab, monkeypatch):
    c, channel, account, _, _ = fixture_channel(collab)
    original = '甲\"乙\\🙂' * 17
    _, _, delivery = reply(c, channel, account, original)
    part = part_for(c.s.store, delivery)
    fixed = len(weixin_protocol.encode_request(weixin_protocol.send_payload(
        'synthetic-peer', 'private-context-token', part['client_id'], text_items(''))))
    monkeypatch.setattr(weixin_protocol, 'MAX_JSON', fixed + 27)
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            weixin_protocol.encode_request(weixin_protocol.send_payload(peer, context, client_id, items))
            calls.append((client_id, items[0]['text_item']['text']))
            return {'provider_state': 'accepted'}

    channel.client_factory = lambda *_: Provider()
    for _ in range(40):
        asyncio.run(channel.delivery.tick())
    assert ''.join(value for _, value in calls) == original
    assert len({client for client, _ in calls}) == len(calls) > 1
    assert c.s.store.one('SELECT state FROM weixin_deliveries WHERE id=?', (delivery['id'],))['state'] == 'accepted'


def test_fixed_overhead_limit_fails_before_send(collab, monkeypatch):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, delivery = reply(c, channel, account)
    monkeypatch.setattr(weixin_protocol, 'MAX_JSON', 20)

    class Provider:
        async def send(self, *args):
            pytest.fail('oversized fixed provider fields should never reach send')

    channel.client_factory = lambda *_: Provider()
    asyncio.run(channel.delivery.tick())
    part = part_for(c.s.store, delivery)
    assert (part['state'], part['attempts'], part['reason_code']) == ('rejected', 0, 'WEIXIN_REQUEST_LIMIT')


@pytest.mark.asyncio
async def test_revoked_task_notice_has_fixed_payload_and_never_reuses_rebound_chat(collab):
    (s, owner, worker, clock, identity, credentials), flow, _, action = await setup_channel(collab, tasks=True)
    source = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '原本人任务', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    entry = next(item for item in s.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == source)
    posted = s.room_runtime.reply({**identity, **credentials, **entry['reply_arguments'],
        'body_text': '不可外发的项目内容', 'idempotency_key': key()}, worker)['message']
    s.room_channels.native.revoke_tasks(flow['binding']['id'], owner)
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((peer, items[0]['text_item']['text']))
            return {'provider_state': 'accepted'}

    s.weixin.client_factory = lambda *_: Provider()
    await s.weixin.delivery.tick()
    notices = s.weixin.delivery.for_messages([posted['id']])[posted['id']]
    assert {row['kind'] for row in notices} == {'reply', 'status'}
    assert next(row for row in notices if row['kind'] == 'reply')['parts'][0]['state'] == 'rejected'
    await s.weixin.delivery.tick()
    assert calls == [('synthetic-peer', '任务授权已撤销，后续操作已停止。')]
    source_row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (source,))
    assert s.weixin.delivery.queue_task_status(source_row, 'CHANNEL_OWNER_REVOKED')['state'] == 'accepted'
    assert len(s.store.all("SELECT id FROM weixin_deliveries WHERE kind='status'")) == 1
    # Unbinding terminates even fixed state notices before dispatch.
    queued = s.weixin.delivery.queue_task_status(source_row, 'ROOM_WORK_APPROVAL_EXPIRED')
    current = s.room_channels.read(flow['id'], owner)['flow']
    await action(current, 'disconnect')
    assert part_for(s.store, queued)['state'] == 'cancelled'
    await s.weixin.delivery.tick()
    assert len(calls) == 1


def test_historical_order_migration_uses_proven_work_and_parent_only():
    with sqlite3.connect(':memory:') as db:
        db.executescript("""
            CREATE TABLE collaboration_messages(
                id TEXT,conversation_id TEXT,goal_id TEXT,thread_root_id TEXT,origin TEXT,reply_to_id TEXT);
            CREATE TABLE delegation_requests(message_id TEXT,goal_id TEXT);
            CREATE TABLE weixin_deliveries(
                id TEXT,binding_id TEXT,generation INTEGER,message_id TEXT,message_version INTEGER,
                source_message_id TEXT,peer_id TEXT,created REAL);
            INSERT INTO collaboration_messages VALUES
                ('source','room',NULL,'thread','external_channel',''),
                ('reply','room',NULL,'thread','bound_connector','source'),
                ('later','room','goal','thread','bound_connector','reply');
            INSERT INTO delegation_requests VALUES ('source','goal');
            INSERT INTO weixin_deliveries VALUES
                ('first','b',1,'reply',1,'source','peer',1),
                ('second','b',1,'later',1,'source','peer',2),
                ('missing','b',1,'missing-reply',1,'source','peer',3);
        """)
        weixin_schema.migrate_delivery_order(db)
        weixin_schema.migrate_delivery_order(db)
        assert db.execute('SELECT id,ordering_key,depends_on_delivery_id FROM weixin_deliveries ORDER BY rowid').fetchall() == [
            ('first', 'work:goal', None), ('second', 'work:goal', 'first'), ('missing', '', None)]
