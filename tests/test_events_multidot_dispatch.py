"""Fenced fair dispatch with synthetic callbacks; no real host or network."""
import asyncio
import base64
import json
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from hub.collaboration.common import STATUS_EVENT, canonical
from hub.collaboration.events import EventService
from hub.collaboration import network
from hub.collaboration.network import Reply
from tests.collaboration_support import collab  # noqa: F401


class Receiver:
    def __init__(self):
        self.callback = None
        self.requests = []

    async def __call__(self, url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        self.requests.append((url, body, headers))
        if self.callback:
            return await self.callback(url, payload)
        return Reply(204)


async def subscriptions(c, count=1, *, shared_endpoint=False):
    service, _, actor, _, room, _, _, _ = c
    service.config = replace(service.config, events_enabled=True)
    receiver = Receiver()
    service.events = events = EventService(service, receiver)
    subs = []
    severities = [None, 'low', 'medium', 'high', 'critical']
    for index in range(count):
        args = {'name': STATUS_EVENT,
                'arguments': {'project_id': room['project_id'], 'environment_id': 'production',
                              'severity_min': severities[index]},
                'delivery': {'mode': 'webhook',
                             'url': f'https://receiver-{0 if shared_endpoint else index}.example.invalid/events',
                             'secret': 'whsec_' + base64.b64encode(b'fixture-only-secret-material-32!!').decode()}}
        sub = await events.subscribe(args, actor)
        subs.append(service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],)))
    for sub in subs:
        with service.store.transaction():
            events.queue_test(room, sub)
    return events, receiver, sorted(subs, key=lambda row: row['id'])


@pytest.mark.asyncio
async def test_healthy_recipients_finish_while_first_callback_stays_blocked(collab):
    events, receiver, subs = await subscriptions(collab, 5)
    slow_id = subs[0]['id']
    entered, release, healthy_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = set()
    async def receive(url, payload):
        selected = payload['data']['test_subscription_id']
        if selected == slow_id:
            entered.set()
            await release.wait()
        else:
            seen.add(selected)
            if len(seen) == 4:
                healthy_done.set()
        return Reply(204)
    receiver.callback = receive
    task = asyncio.create_task(events.tick())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(healthy_done.wait(), .7)
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert len(receiver.requests) == 5
    assert {row['state'] for row in collab[0].store.all('SELECT state FROM mcp_event_deliveries')} == {'accepted'}


@pytest.mark.asyncio
async def test_new_committed_event_can_use_free_capacity_while_old_callback_waits(collab):
    events, receiver, subs = await subscriptions(collab, 2)
    slow_id = subs[0]['id']
    entered, release, late_done, wake = (asyncio.Event() for _ in range(4))
    # Leave the other subscription empty until the dispatcher is in flight.
    collab[0].store.execute('DELETE FROM mcp_event_outbox WHERE json_extract(data,\'$.test_subscription_id\')=?',
                            (subs[1]['id'],))
    async def receive(url, payload):
        if payload['data']['test_subscription_id'] == slow_id:
            entered.set()
            await release.wait()
        else:
            late_done.set()
        return Reply(204)
    receiver.callback = receive
    task = asyncio.create_task(events.tick(wake=wake))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with collab[0].store.transaction():
            events.queue_test(collab[4], subs[1])
        wake.set()
        await asyncio.wait_for(late_done.wait(), .7)
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_endpoint_serialization_and_durable_global_capacity(collab):
    events, receiver, _ = await subscriptions(collab, 5, shared_endpoint=True)
    first = events.reserve()
    assert len(first) == 1
    rebuilt = EventService(collab[0], receiver)
    assert rebuilt.reserve() == []
    events.finish(first[0], 204)
    assert len(rebuilt.reserve()) == 1


@pytest.mark.asyncio
async def test_global_reservation_capacity_is_shared_between_dispatchers(collab):
    events, receiver, _ = await subscriptions(collab, 5)
    first = events.reserve()
    assert len(first) == 4
    rebuilt = EventService(collab[0], receiver)
    assert rebuilt.reserve() == []
    events.finish(first[0], 204)
    assert len(rebuilt.reserve()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429, 503])
async def test_transient_cooldown_survives_restart_and_applies_to_other_subscriptions(collab, status):
    events, receiver, subs = await subscriptions(collab, 2, shared_endpoint=True)
    item = events.reserve()[0]
    now = collab[6][0]
    events.finish(item, status, retry_after=600)
    delivery = collab[0].store.one('SELECT * FROM mcp_event_deliveries WHERE id=?', (item['delivery_id'],))
    assert delivery['state'] == 'retry_wait' and delivery['next_at'] == now + 600
    rebuilt = EventService(collab[0], receiver)
    collab[6][0] += 599
    assert rebuilt.reserve() == []
    collab[6][0] += 1
    assert len(rebuilt.reserve()) == 1
    assert len(subs) == 2


@pytest.mark.asyncio
async def test_retry_jitter_is_bounded_and_keeps_original_body(collab):
    events, _, _ = await subscriptions(collab)
    item = events.reserve()[0]
    original = item['body']
    now = collab[6][0]
    events.finish(item, 503)
    row = collab[0].store.one('SELECT * FROM mcp_event_deliveries')
    assert now + 24 <= row['next_at'] <= now + 30
    collab[6][0] += 31
    next_item = events.reserve()[0]
    assert next_item['body'] == original and next_item['fence'] > item['fence']


@pytest.mark.asyncio
async def test_retry_budget_cannot_be_extended_by_restarting_dispatcher(collab):
    events, receiver, _ = await subscriptions(collab)
    original_body = None
    for attempt in range(5):
        item = events.reserve()[0]
        original_body = original_body or item['body']
        assert item['body'] == original_body
        events.finish(item, 503)
        row = collab[0].store.one('SELECT * FROM mcp_event_deliveries')
        assert row['attempt'] == attempt + 1
        if attempt < 4:
            assert row['state'] == 'retry_wait'
            collab[6][0] = row['next_at'] + 1
            events = EventService(collab[0], receiver)
    assert row['state'] == 'dead_letter'
    collab[6][0] += 1801
    assert EventService(collab[0], receiver).reserve() == []


@pytest.mark.asyncio
async def test_cancelling_dispatcher_closes_children_but_keeps_recoverable_lease(collab):
    events, receiver, _ = await subscriptions(collab, 2)
    active = 0
    entered, stopped, forever = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def receive(url, payload):
        nonlocal active
        active += 1
        entered.set()
        try:
            await forever.wait()
        finally:
            active -= 1
            if active == 0:
                stopped.set()
    receiver.callback = receive
    task = asyncio.create_task(events.tick())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), 1)
    assert active == 0
    assert collab[0].store.one("SELECT COUNT(*) AS n FROM mcp_event_deliveries WHERE state='leased'")['n'] >= 1
    # An unknown HTTP outcome must not become an immediate, unfenced resend.
    assert events.reserve() == []
    collab[6][0] += 31
    assert events.reserve()


@pytest.mark.parametrize('value', [None, '', '-1', 'nan', '3.2', 'x' * 200, 'Wed, 99 Oct 2026 99:99:99 GMT'])
def test_retry_after_rejects_malformed_values(value):
    assert network.retry_after_seconds(value, now=1000) is None


def test_retry_after_clamps_numeric_and_http_dates():
    assert network.retry_after_seconds('60', now=1000) == 60
    assert network.retry_after_seconds('999999999', now=1000) == 1800
    assert network.retry_after_seconds('0', now=1000) == 0
    date = format_datetime(datetime.fromtimestamp(1060, timezone.utc), usegmt=True)
    assert network.retry_after_seconds(date, now=1000) == 60
    assert network.retry_after_seconds(date, now=2000) == 0


@pytest.mark.asyncio
async def test_busy_subscriptions_round_robin_on_the_same_endpoint_across_dispatchers(collab):
    events, receiver, subs = await subscriptions(collab, 5, shared_endpoint=True)
    with collab[0].store.transaction():
        # A hot first subscription must not monopolize its shared endpoint.
        for _ in range(4):
            events.queue_test(collab[4], subs[0])
    seen = []
    for _ in range(5):
        items = events.reserve()
        assert len(items) == 1
        seen.append(items[0]['subscription_id'])
        events.finish(items[0], 204)
        events = EventService(collab[0], receiver)
    assert len(set(seen)) == 5


@pytest.mark.asyncio
async def test_each_owner_gets_capacity_before_one_owner_fills_the_pool(collab):
    from tests.legacy_iam_fixture import seed_grant

    service, owner, worker, dot, _, agents, clock, scope = collab
    await subscriptions(collab, 3)
    service.store.execute("UPDATE memberships SET level='admin' WHERE user_id='other' AND space_id='legacy'")
    service.store.execute("UPDATE projects SET owner_user_id='other' WHERE id='otherproj'")
    seed_grant(service.store, 'other_worker', 'other', scopes=('read',), projects=('otherproj',))
    scope = {**scope, 'project': 'otherproj'}
    other_owner = replace(owner, actor='panel:other', user_id='other')
    other_actor = replace(worker, actor='mcp:other_worker:fixture',
                          user_id='other', grant_id='other_worker', projects=['otherproj'])
    other_room = service.room_create({**scope, 'idempotency_key': 'other-owner-room'}, other_owner)['room']
    other = (service, other_owner, other_actor, dot, other_room, agents, clock, scope)
    events, _, _ = await subscriptions(other, 2)
    items = events.reserve()
    assert len(items) == 4
    counts = service.store.all("""SELECT r.owner_key,COUNT(*) AS n
        FROM mcp_event_deliveries d JOIN mcp_event_dispatch_routes r ON r.subscription_id=d.subscription_id
        WHERE d.state='leased' GROUP BY r.owner_key""")
    assert sorted(row['n'] for row in counts) == [2, 2]


@pytest.mark.asyncio
async def test_recovery_sources_share_one_bounded_wake_budget(collab, monkeypatch):
    events, _, _ = await subscriptions(collab)
    observed = []
    monkeypatch.setattr(collab[0].dot_relay, 'recover', lambda: 3)
    def mailbox_recover(*, max_wakes):
        observed.append(('mailbox', max_wakes))
        return max_wakes
    def task_recover(*, max_wakes):
        observed.append(('tasks', max_wakes))
        return 0
    monkeypatch.setattr(collab[0].dot_mailbox, 'recover', mailbox_recover)
    monkeypatch.setattr(collab[0].task_liveness, 'recover', task_recover)
    await events.tick()
    assert observed == [('mailbox', 1), ('tasks', 0)]


@pytest.mark.asyncio
async def test_owner_send_budget_survives_restart_without_spending_pending_attempts(collab):
    events, receiver, subs = await subscriptions(collab)
    with collab[0].store.transaction():
        for _ in range(8):
            events.queue_test(collab[4], subs[0])
    for _ in range(8):
        items = events.reserve()
        assert len(items) == 1
        events.finish(items[0], 204)
        events = EventService(collab[0], receiver)
    assert events.reserve() == []
    pending = collab[0].store.one("SELECT * FROM mcp_event_deliveries WHERE state='pending'")
    assert pending['attempt'] == 0 and pending['body'] is None
    assert .24 <= events.next_dispatch_delay() <= .26
    collab[6][0] += .249
    assert events.reserve() == []
    collab[6][0] += .002
    assert len(events.reserve()) == 1


@pytest.mark.asyncio
async def test_budget_charges_actual_utf8_body_plus_header_allowance(collab):
    from hub.collaboration.events import DISPATCH_BUDGETS, WIRE_HEADER_BYTES

    events, _, subs = await subscriptions(collab)
    item = events.reserve()[0]
    wire_bytes = len(item['body']) + WIRE_HEADER_BYTES
    budget = collab[0].store.one("SELECT * FROM mcp_event_dispatch_budgets WHERE scope_key='global'")
    assert budget['byte_tokens'] == DISPATCH_BUDGETS['global'][3] - wire_bytes
    events.finish(item, 204)
    with collab[0].store.transaction():
        events.queue_test(collab[4], subs[0])
        collab[0].store.execute("UPDATE mcp_event_dispatch_budgets SET byte_tokens=0 WHERE scope_key='global'")
    assert events.reserve() == []
    pending = collab[0].store.one("SELECT * FROM mcp_event_deliveries WHERE state='pending'")
    assert pending['attempt'] == 0 and pending['body'] is None
    collab[6][0] += 1
    assert len(events.reserve()) == 1


@pytest.mark.asyncio
async def test_global_bucket_blocks_a_fresh_owner_and_refills_after_restart(collab):
    events, receiver, _ = await subscriptions(collab)
    with collab[0].store.transaction():
        for owner in ('a', 'b'):
            for _ in range(8):
                assert events.send_budget(owner, 1000, collab[6][0], spend=True)
    rebuilt = EventService(collab[0], receiver)
    with collab[0].store.transaction():
        assert not rebuilt.send_budget('fresh-owner', 1000, collab[6][0], spend=True)
        assert not rebuilt.send_budget('fresh-owner', 1000, collab[6][0] + .124, spend=True)
        assert rebuilt.send_budget('fresh-owner', 1000, collab[6][0] + .126, spend=True)


@pytest.mark.asyncio
async def test_owner_byte_bucket_can_block_before_the_send_count_is_used(collab):
    from hub.collaboration.events import MAX_ENVELOPE_BYTES, WIRE_HEADER_BYTES

    events, receiver, _ = await subscriptions(collab)
    cost = MAX_ENVELOPE_BYTES + WIRE_HEADER_BYTES
    with collab[0].store.transaction():
        for _ in range(7):
            assert events.send_budget('large-envelope-owner', cost, collab[6][0], spend=True)
    rebuilt = EventService(collab[0], receiver)
    with collab[0].store.transaction():
        assert not rebuilt.send_budget('large-envelope-owner', cost, collab[6][0], spend=True)
    row = collab[0].store.one(
        "SELECT * FROM mcp_event_dispatch_budgets WHERE scope_key='owner:large-envelope-owner'")
    assert row['tokens'] == 1 and row['byte_tokens'] < cost


@pytest.mark.asyncio
async def test_lifecycle_uses_refill_deadline_instead_of_idle_fallback():
    from types import SimpleNamespace
    import time
    from hub.collaboration.lifecycle import CollaborationLoops

    done = asyncio.Event()
    calls = 0
    async def operation():
        nonlocal calls
        calls += 1
        if calls == 2:
            done.set()
    service = SimpleNamespace(clock=time.time, health={},
                              events=SimpleNamespace(next_dispatch_delay=lambda interval: .03))
    loops = CollaborationLoops(service)
    loops._event_wake = asyncio.Event()
    task = asyncio.create_task(loops.loop('events', operation, 2))
    try:
        await asyncio.wait_for(done.wait(), .4)
        assert calls == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
