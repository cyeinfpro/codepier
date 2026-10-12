"""Synthetic wake/read comparisons. No real host, WeChat or model is contacted."""
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.dot_wake_context import MAX_CONTEXT_BYTES, delivery_timing
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, inbox, no_work
from tests.test_collaboration_dot_relay import subscribe
from tests.test_collaboration_dots import call


def entries(page):
    return {entry['kind']: entry for entry in page['wake_context']['pages']}


def test_one_read_contains_same_authorized_pages_without_consumption(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = inbox(c)
    bundle = entries(page)
    assert set(bundle) == {'thread', 'work', 'recovery'}
    for entry in bundle.values():
        assert entry['included']
        assert entry['page'] == call(c, entry['request'])
    assert len(bundle['thread']['page']['items']) == 1
    assert bundle['thread']['page']['items'][0]['id'] == source['message']['id']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert page['items'][0]['state'] == 'pending'
    assert page['items'][0]['delivery_timing']['consumer_receipt_at'] is None
    assert page['wake_context']['read_only'] and not page['wake_context']['grants_authority']
    no_work(c)


def test_synthetic_round_trip_count_drops_from_four_to_one(collab):
    c = duplex(collab)
    owner_message(c)
    # Count consumer-facing reads, not in-process queries or model latency.
    old_requests = [c.connected['inbox_request']]
    old_page = call(c, old_requests[0])
    old_requests.extend([old_page['items'][0]['thread_request'],
                         old_page['work_inbox_request'], old_page['recovery_request']])
    for request in old_requests[1:]:
        call(c, request)
    new_requests = [c.connected['inbox_request']]
    new_page = call(c, new_requests[0])
    assert all(entry['included'] and not entry['next_page_request']
               for entry in new_page['wake_context']['pages'])
    assert len(old_requests) == 4 and len(new_requests) == 1
    assert new_page['wake_context']['host_wake_at'] is None
    assert new_page['wake_context']['host_scheduling_latency_ms'] is None


def test_thread_budget_preserves_fallback_and_all_thread_pages(collab):
    c = duplex(collab)
    root, _ = owner_message(c)
    for n in range(64):
        c.clock[0] += 2  # Respect the real message rate limit in the synthetic history.
        owner_message(c, f'原话题补充 {n}', reply_to_id=root['message']['id'])
    page = inbox(c)
    thread = entries(page)['thread']
    found = [row['id'] for row in thread['page']['items']]
    assert thread['next_page_request']
    request, pages = thread['next_page_request'], 1
    while request:
        older = call(c, request)
        pages += 1
        found += [row['id'] for row in older['items']]
        request = {**thread['request'], 'arguments': {
            **thread['request']['arguments'], 'cursor': older['next_cursor']}} if older['next_cursor'] else None
    assert pages == 3
    assert len(set(found)) == 65 and root['message']['id'] in found
    assert thread['resume_request'] == page['items'][0]['thread_request']


def test_work_and_recovery_pagination_survive_empty_message_inbox(collab):
    c = duplex(collab)
    expected = set()
    for n in range(12):
        owner_message(c, f'检查文件 {n}')
        item = inbox(c)['items'][0]
        expected.add(call(c, item['task_request'])['delegation_id'])
    page = inbox(c)
    assert not page['items']
    for kind in ('work', 'recovery'):
        entry = entries(page)[kind]
        assert entry['included'] and entry['next_page_request']
        rows = list(entry['page']['items'])
        request = entry['next_page_request']
        while request:
            following = call(c, request)
            rows.extend(following['items'])
            request = following['next_page_request']
        if kind == 'work':
            assert {row['delegation_id'] for row in rows} == expected
        else:
            assert len({row['work_item_id'] for row in rows}) == 12
        assert call(c, entry['resume_request'])['items']
    assert not c.s.store.all('SELECT * FROM operations')


def test_inbox_pagination_and_two_thread_bound_keep_original_requests(collab):
    c = duplex(collab)
    expected = [owner_message(c, f'话题 {n}')[0]['message']['id'] for n in range(5)]
    request = {**c.connected['inbox_request'], 'arguments': {
        **c.connected['inbox_request']['arguments'], 'limit': 3}}
    first = call(c, request)
    assert not first['wake_context']['inbox_pagination_complete']
    threads = [entry for entry in first['wake_context']['pages'] if entry['kind'] == 'thread']
    assert len(threads) == 2
    assert call(c, first['items'][2]['thread_request'])['items']
    second = call(c, first['next_page_request'])
    assert second['wake_context']['inbox_pagination_complete']
    assert not any(entry['kind'] in ('work', 'recovery') for entry in second['wake_context']['pages'])
    assert [item['message_id'] for item in first['items'] + second['items']] == expected
    assert call(c, second['resume_request'])['items'][0]['message_id'] == expected[0]


def test_large_context_is_bounded_and_never_silently_drops_request(collab):
    c = duplex(collab)
    root, _ = owner_message(c, '内容' * 1800)
    for _ in range(22):
        owner_message(c, '补充' * 1800, reply_to_id=root['message']['id'])
    page = inbox(c)
    assert len(canonical(page['wake_context']).encode('utf-8')) <= MAX_CONTEXT_BYTES
    thread = entries(page)['thread']
    assert not thread['included'] and thread['reason'] == 'context_byte_budget'
    assert len(call(c, thread['request'])['items']) == 23
    assert all(item['thread_request'] for item in page['items'])


def test_duplicate_wake_reuses_interim_and_observes_later_correction(collab):
    c = duplex(collab)
    source, _ = owner_message(c)
    first = inbox(c)['items'][0]
    publication = {'action': 'dot_message', **first['interim_reply_arguments'], 'body_text': '正在核对原话题。'}
    sent = c.s.invoke('collaboration', publication, c.actor)
    assert c.s.invoke('collaboration', publication, c.actor)['message']['id'] == sent['message']['id']
    again = inbox(c)['items'][0]
    assert again['existing_reply']['message_id'] == sent['message']['id']
    assert again['existing_reply']['update_arguments'] == sent['update_arguments']
    assert again['state'] == 'read' and not again['existing_reply']['complete']
    correction, _ = owner_message(c, '先不要修改，继续讨论。', reply_to_id=source['message']['id'])
    fresh = inbox(c)
    assert correction['message']['id'] in [item['message_id'] for item in fresh['items']]
    assert correction['message']['id'] in [
        row['id'] for row in entries(fresh)['thread']['page']['items']]
    assert len(c.s.store.all("""SELECT * FROM collaboration_messages
        WHERE reply_to_id=? AND origin='bound_connector'""", (source['message']['id'],))) == 1
    no_work(c)


@pytest.mark.asyncio
async def test_transport_acceptance_and_measured_segments_do_not_invent_wake(collab):
    c = duplex(collab)
    await subscribe(c)
    source, _ = owner_message(c)
    saved_at = c.clock[0]
    c.clock[0] += 2
    await c.s.events.tick()
    first = inbox(c)['items'][0]
    timing = first['delivery_timing']
    assert timing['callback_state'] == 'accepted'
    assert timing['saved_to_callback_accepted_ms'] == 2000
    assert timing['callback_acceptance_meaning'] == 'transport_only'
    assert timing['consumer_receipt_at'] is None
    assert timing['callback_accepted_to_consumer_receipt_ms'] is None
    c.clock[0] += 3
    call(c, first['ack_request'])
    c.clock[0] += 4
    c.s.invoke('collaboration', {'action': 'dot_message', **first['interim_reply_arguments'],
                                'body_text': '已核对。'}, c.actor)
    measured = inbox(c)['items'][0]['delivery_timing']
    assert measured['saved_at'] == saved_at
    assert measured['callback_accepted_to_consumer_receipt_ms'] == 3000
    assert measured['consumer_receipt_to_first_reply_ms'] == 4000
    assert measured['saved_to_first_reply_ms'] == 9000
    assert measured['host_wake_at'] is None and measured['model_online'] == 'unknown'
    # First reply without a separate ack is a receipt, not an observed wake time.
    assert not measured['read_is_consumption']
    row = c.s.store.one('SELECT * FROM collaboration_dot_messages WHERE message_id=?', (source['message']['id'],))
    c.s.store.execute('UPDATE mcp_event_deliveries SET accepted_at=?', (c.clock[0] + 10,))
    assert delivery_timing(c.s, c.room, c.s.dots.find(c.slot['id']), row)['callback_accepted_to_consumer_receipt_ms'] is None


def test_paused_work_policy_keeps_chat_and_untrusted_source_cannot_become_work(collab):
    c = duplex(collab)
    c.s.delegation.control({**c.scope, 'policy_id': c.slot['policy_id'], 'expected_version': 1,
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    source, _ = owner_message(c)
    page = inbox(c)
    assert page['work_inbox_request'] is None
    assert set(entries(page)) == {'thread', 'recovery'}
    assert page['items'][0]['task_request'] is None
    c.s.store.execute("UPDATE collaboration_messages SET origin='bound_connector' WHERE id=?", (source['message']['id'],))
    fresh = inbox(c)
    assert not fresh['items'][0]['trusted_author']['authenticated']
    assert fresh['items'][0]['task_request'] is None
    no_work(c)


def test_wrong_connector_and_tampered_cursor_cannot_read_bundle(collab):
    c = duplex(collab)
    owner_message(c)
    other = replace(c.actor, grant_id='second', actor='mcp:second:fixture')
    with pytest.raises(DevError):
        call(c, c.connected['inbox_request'], other)
    with pytest.raises(DevError):
        call(c, {**c.connected['inbox_request'], 'arguments': {
            **c.connected['inbox_request']['arguments'], 'cursor': 'tampered'}})
    no_work(c)


@pytest.mark.parametrize('kind', ['message', 'message_v2', 'update'])
def test_final_publication_returns_timing_after_inbox_is_handled(collab, kind):
    c = duplex(collab)
    owner_message(c)
    first = inbox(c)['items'][0]
    c.clock[0] += 2
    if kind == 'update':
        interim = c.s.invoke('collaboration', {'action': 'dot_message',
            **first['interim_reply_arguments'], 'body_text': '正在核对。'}, c.actor)
        c.clock[0] += 3
        result = c.s.invoke('collaboration', {'action': 'dot_update',
            **interim['update_arguments'], 'body_text': '核对完成。', 'complete': True}, c.actor)
    else:
        result = c.s.invoke('collaboration', {'action': 'dot_' + kind,
            **first['reply_v2_arguments' if kind == 'message_v2' else 'reply_arguments'],
            'body_text': '核对完成。'}, c.actor)
    assert not inbox(c)['items']
    assert result['delivery_timing']['message_handled_at'] == c.clock[0]
    assert result['delivery_timing']['saved_to_first_reply_ms'] == 2000
    assert result['delivery_timing']['host_wake_at'] is None
    no_work(c)


def test_context_budget_reserves_metadata_for_later_fallbacks(monkeypatch):
    from types import SimpleNamespace
    from hub.collaboration import dot_wake_context as module
    monkeypatch.setattr(module, 'MAX_CONTEXT_BYTES', 2048)
    request = {'tool': 'collaboration_query', 'arguments': {'action': 'thread', 'id': 'root'}}
    c = SimpleNamespace(clock=lambda: 1, invoke=lambda *args: {'items': [{'body_text': 'x' * 1200}]})
    context = module.wake_context(c, None, [{'thread_request': request}],
        {'tool': 'collaboration_query', 'arguments': {'action': 'inbox'}},
        {'tool': 'collaboration_query', 'arguments': {'action': 'dot_recovery'}}, first_page=True)
    assert len(canonical(context).encode('utf-8')) <= 2048
    assert len(context['pages']) == 3
    assert all(entry['request'] for entry in context['pages'])
    assert any(not entry['included'] for entry in context['pages'])


@pytest.mark.asyncio
async def test_timing_uses_indexed_source_with_large_unrelated_history(collab):
    from hub.collaboration.common import MESSAGE_EVENT
    from hub.collaboration.dot_chat import migrate
    c = duplex(collab)
    await subscribe(c)
    source, _ = owner_message(c)
    await c.s.events.tick()
    receipt = c.s.store.one('SELECT * FROM collaboration_dot_messages WHERE message_id=?',
                          (source['message']['id'],))
    subscription = c.s.store.one('SELECT id FROM mcp_event_subscriptions LIMIT 1')['id']

    def seed(start, end):
        with c.s.store.transaction() as db:
            db.executemany("""INSERT INTO mcp_event_outbox
                (id,room_id,name,object_id,object_version,data,created) VALUES (?,?,?,?,?,?,?)""",
                [(f'noise-{n}', c.room['id'], MESSAGE_EVENT, f'noise-{n}', 1,
                  canonical({'message_id': f'noise-{n}', 'recipient_slot_id': c.slot['id']}), c.clock[0])
                 for n in range(start, end)])
            db.executemany("""INSERT INTO mcp_event_deliveries
                (id,subscription_id,event_id,event_seq,state,status_code,created,accepted_at)
                SELECT ?,?,id,seq,'accepted',200,?,? FROM mcp_event_outbox WHERE id=?""",
                [(f'noise-delivery-{n}', subscription, c.clock[0], c.clock[0], f'noise-{n}')
                 for n in range(start, end)])

    def vm_steps():
        count = [0]
        def progress():
            count[0] += 1
            return 0
        import sqlite3
        from types import SimpleNamespace
        snapshot = sqlite3.connect(':memory:')
        snapshot.row_factory = sqlite3.Row
        with c.s.store.lock:
            c.s.store.db.backup(snapshot)
        adapter = SimpleNamespace(store=SimpleNamespace(
            one=lambda sql, args: dict(snapshot.execute(sql, args).fetchone())))
        snapshot.set_progress_handler(progress, 1)
        try:
            timing = delivery_timing(adapter, c.room, c.s.dots.find(c.slot['id']), receipt)
            assert timing['callback_state'] == 'accepted'
        finally:
            snapshot.set_progress_handler(None, 0)
            snapshot.close()
        return count[0]

    seed(0, 1000)
    small = vm_steps()
    seed(1000, 10000)
    large = vm_steps()
    # VM instruction counts avoid wall-clock assertions and prove the selected
    # source query does not linearly scan 10x more unrelated deliveries.
    assert large < small * 2 + 100, (small, large)
    before = c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox')['n']
    with c.s.store.transaction() as db:
        migrate(db)
        migrate(db)
    assert c.s.store.one('SELECT COUNT(*) AS n FROM mcp_event_outbox')['n'] == before
    assert inbox(c)['items'][0]['state'] == 'pending'
