"""Weixin parity transport regressions using the real persisted outbox."""
import asyncio
import json
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


@pytest.mark.asyncio
async def test_native_reply_waits_for_original_account_and_keeps_same_outbox(collab):
    (s, _, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab, tasks=True)
    account_id = flow['account_id']
    source = s.weixin.commit_updates(s.weixin.claim_poll(account_id),
        packet(2, '说明这条微信怎么处理。', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    entry = next(item for item in s.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == source)
    with s.store.transaction():
        s.store.execute("UPDATE weixin_accounts SET state='attention' WHERE id=?", (account_id,))
    posted = s.room_runtime.reply({**identity, **credentials, **entry['reply_arguments'],
        'body_text': '原回复已保存。', 'idempotency_key': key()}, worker)['message']
    delivery = s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (posted['id'],))
    part = part_for(s.store, delivery)
    assert (delivery['state'], part['reason_code'], part['attempts']) == ('waiting_context', 'account_waiting_reconnect', 0)
    assert not s.weixin.delivery.candidates(clock[0])
    assert delivery['source_message_id'] == source and delivery['peer_id'] == 'synthetic-peer'
    calls = []

    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append((peer, client_id))
            return {'provider_state': 'accepted'}

    s.weixin.client_factory = lambda *_: Provider()
    await s.weixin.delivery.tick()
    assert not calls
    with s.store.transaction():
        s.store.execute("UPDATE weixin_accounts SET state='active' WHERE id=?", (account_id,))
    await s.weixin.delivery.tick()
    assert calls == [('synthetic-peer', part['client_id'])]
    assert part_for(s.store, delivery)['state'] == 'accepted'
    assert s.store.one('SELECT COUNT(*) AS n FROM weixin_deliveries WHERE message_id=?', (posted['id'],))['n'] == 1


@pytest.mark.asyncio
async def test_followup_has_own_reply_lane_and_honors_explicit_success_dependency(collab):
    (s, _, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab)
    transport = s.weixin.delivery
    account_id = flow['account_id']
    old_source, independent_source, dependent_source, blocked_source = s.weixin.commit_updates(
        s.weixin.claim_poll(account_id), {
            **packet(2, '原进展'),
            'msgs': [packet(n, label)['msgs'][0] for n, label in enumerate(
                ['原进展', '新追问', '必须引用已送出的上文', '需要第二段内容'], 2)],
        })['message_ids']

    def queue(source_id, text, dependency_id=None):
        entry = next(item for item in s.room_consumers.inbox(identity, worker)['items']
                     if item['object_id'] == source_id)
        args = {**identity, **credentials, **entry['reply_arguments'], 'body_text': text,
                'publish_work': False, 'input_handling': 'answered', 'idempotency_key': key()}
        if dependency_id:
            args.update(depends_on_delivery_id=dependency_id, dependency_mode='requires_success')
        posted = s.room_runtime.reply(args, worker)
        disposition, = posted['channel_dispositions']
        assert disposition['disposition'] == 'queued'
        delivery = s.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (disposition['delivery_id'],))
        return delivery, part_for(s.store, delivery)

    prior, prior_part = queue(old_source, '原来工作进展')
    independent, next_part = queue(independent_source, '工作中的独立问答')
    dependent, dependent_part = queue(dependent_source, '需要原进展已经送达', prior['id'])
    assert prior['ordering_key'] == 'reply:' + old_source
    assert independent['ordering_key'] == 'reply:' + independent_source
    assert json.loads(dependent['ordering_evidence'])['dependency_mode'] == 'requires_success'
    blocked, blocked_part = queue(blocked_source, '第二段内容已受理后才能发送', independent['id'])

    claim = transport.claim(prior_part['id'])
    transport.mark_sending(claim, text_items('原来工作进展'))
    assert not transport.candidates(clock[0])
    clock[0] += 181
    transport.reconcile()
    assert [row['id'] for row in transport.candidates(clock[0])] == [next_part['id']]
    assert part_for(s.store, prior)['state'] == 'unknown'

    # When the exact earlier effect is proven accepted, its dependency can run.
    transport.finish(claim, {'provider_state': 'accepted'})
    transport.reconcile()
    assert part_for(s.store, prior)['state'] == 'accepted'
    followup = transport.claim(next_part['id'])
    transport.mark_sending(followup, text_items('工作中的独立问答'))
    transport.finish(followup, error=DevError('WEIXIN_REQUEST_LIMIT', 'synthetic provider rejected', 422))
    admitted = transport.claim(dependent_part['id'])
    assert admitted
    transport.mark_sending(admitted, text_items('需要原进展已经送达'))
    transport.finish(admitted, {'provider_state': 'accepted'})
    assert transport.claim(blocked_part['id']) is None
    blocked_part = part_for(s.store, blocked)
    assert (blocked_part['state'], blocked_part['reason_code'], blocked_part['attempts']) == ('rejected', 'dependency_failed', 0)


def test_ready_weixin_peers_receive_persistent_round_robin_admission(collab, monkeypatch):
    c, channel, account, _, _ = fixture_channel(collab)
    _, _, first = reply(c, channel, account)
    store = c.s.store
    first_account = store.one('SELECT * FROM weixin_accounts WHERE id=?', (account['account_id'],))
    first_binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (first['binding_id'],))
    first_context = store.one('SELECT * FROM weixin_contexts WHERE account_id=?', (first_account['id'],))

    def insert(table, original, **fields):
        row = {**original, **fields}
        store.execute('INSERT INTO ' + table + ' (' + ','.join(row) + ') VALUES ('
                      + ','.join('?' for _ in row) + ')', tuple(row.values()))

    with store.transaction():
        for index in range(64):
            account_id = first_account['id'] if index == 0 else f'account-{index}'
            binding_id = first_binding['id'] if index == 0 else f'binding-{index}'
            peer_id = first['peer_id'] if index == 0 else f'peer-{index}'
            if index:
                insert('weixin_accounts', first_account, id=account_id, bot_id=f'bot-{index}')
                insert('weixin_bindings', first_binding, id=binding_id, account_id=account_id, peer_id=peer_id,
                       conversation_id=f'room-{index}')
                insert('weixin_contexts', first_context, account_id=account_id, peer_id=peer_id)
            for ordinal in range(2):
                delivery_id = f'delivery-{index}-{ordinal}'
                insert('weixin_deliveries', first, id=delivery_id, binding_id=binding_id,
                       message_id=f'reply-{index}-{ordinal}', peer_id=peer_id,
                       created=first['created'] + index, ordering_key=f'reply:{index}-{ordinal}')
                channel.delivery.add_part(delivery_id, 0, 'text', {'text': 'synthetic'})

    def validate_route(self, delivery, *, part=None):
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (delivery['binding_id'],))
        account = self.store.one('SELECT * FROM weixin_accounts WHERE id=?', (binding['account_id'],))
        return binding, account, c.owner, None

    monkeypatch.setattr(type(channel.delivery), 'validate', validate_route)
    seen = set()
    for batch in range(4):
        if batch == 2:
            channel.delivery = type(channel.delivery)(channel)
        candidates = channel.delivery.candidates(c.clock[0])
        assert len(candidates) == 16
        for candidate in candidates:
            claim = channel.delivery.claim(candidate['id'])
            assert claim and claim['delivery']['peer_id'] not in seen
            seen.add(claim['delivery']['peer_id'])
            channel.delivery.mark_sending(claim, text_items('synthetic'))
            channel.delivery.finish(claim, {'provider_state': 'accepted'})
    assert len(seen) == 64
