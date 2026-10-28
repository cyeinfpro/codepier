"""Bounded synthetic server load, not a real-host or mobile performance claim."""
import asyncio
import base64
import json
import math
import platform
import time
from dataclasses import replace

import pytest

from hub.collaboration.common import MAILBOX_EVENT, canonical
from hub.collaboration.events import DISPATCH_CAPACITY, EventService
from hub.collaboration.network import Reply
from shared.public_collaboration import request as public_request
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


class LoadReceiver:
    def __init__(self):
        self.slow_dot = None
        self.received = []
        self.active = 0
        self.peak = 0
        self.by_dot = {}
        self.peaks = {}

    async def __call__(self, url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        dot_id = payload.get('data', {}).get('recipient_slot_id')
        if payload.get('data', {}).get('test'):
            return Reply(204)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.by_dot[dot_id] = self.by_dot.get(dot_id, 0) + 1
        self.peaks[dot_id] = max(self.peaks.get(dot_id, 0), self.by_dot[dot_id])
        try:
            if dot_id == self.slow_dot:
                await asyncio.sleep(.05)
            self.received.append((payload, time.time()))
            return Reply(204)
        finally:
            self.active -= 1
            self.by_dot[dot_id] -= 1


def percentiles(values):
    ordered = sorted(values)
    assert ordered
    return {label: round(ordered[max(0, math.ceil(len(ordered) * rank) - 1)], 3)
            for label, rank in (('p50', .50), ('p95', .95), ('p99', .99))}


@pytest.mark.asyncio
async def test_ten_dot_burst_is_bounded_and_reports_server_metrics(collab):
    service, owner, actor, _, room, _, _, scope = collab
    service.runtime.collaboration = service
    enrollment_clock = [time.time() - 120]
    service.clock = lambda: enrollment_clock[0]
    service.config = replace(service.config, events_enabled=True)
    actor = replace(actor, grant_id='broad', actor='mcp:broad:synthetic-load')
    receiver = LoadReceiver()
    service.events = EventService(service, receiver)
    bindings = []
    for index in range(10):
        if index == 6:
            enrollment_clock[0] += 61  # Honor the room's six-challenges-per-minute setup limit.
        created = await service.store.run(service.dots.create, {
            **scope, 'conversation_id': room['id'], 'label': 'Load Dot ' + str(index),
            'capabilities': ['read'], 'duplex': True,
            'confirm_tasks': True, 'idempotency_key': key()}, owner)
        slot = created['dot']
        await service.runtime.invoke('collaboration', {
            'action': 'join', 'code': slot['join_code'], 'idempotency_key': key()}, actor)
        configured = await service.runtime.invoke('collaboration', {
            'action': 'dot_mailbox_configure', **scope, 'dot_id': slot['id'],
            'confirm_consumer_protocol': True, 'idempotency_key': key()}, actor)
        await service.events.subscribe({**configured['subscription_request'], 'delivery': {
            'mode': 'webhook', 'url': 'https://load-' + str(index) + '.example.invalid/events',
            'secret': 'whsec_' + base64.b64encode(b'fixture-only-load-signing-key!!!!').decode()}}, actor)
        bindings.append((slot['id'], configured['binding_generation']))
    await service.events.tick()
    service.clock = time.time  # All measured work below uses wall time; setup used a virtual clock.
    receiver.slow_dot = bindings[0][0]
    before = service.store.stats_snapshot()
    started = time.perf_counter()

    async def emit(index):
        dot_id = bindings[index % len(bindings)][0]
        message = await service.store.run(service.chatroom.create, {
            **scope, 'room_id': room['id'], 'conversation_id': room['id'],
            'body_text': 'Synthetic independently preserved input ' + str(index),
            'mentions': [{'slot_id': dot_id}], 'client_message_id': key(),
            'idempotency_key': key()}, owner)
        return message['message']['id']

    outcomes = await asyncio.gather(*(emit(index) for index in range(100)), return_exceptions=True)
    rejected = [result for result in outcomes if isinstance(result, BaseException)]
    assert len(rejected) == 40
    assert all(isinstance(error, DevError) and error.code == 'MESSAGE_RATE_LIMIT' for error in rejected)
    source_ids = [result for result in outcomes if isinstance(result, str)]
    queued_seconds = time.perf_counter() - started
    dispatch_ticks, fallback_delays_ms = 0, []
    for _ in range(4):
        await service.events.tick()
        dispatch_ticks += 1
        seen = {payload['data']['recipient_slot_id'] for payload, _ in receiver.received}
        if seen == {dot_id for dot_id, _ in bindings}:
            break
        # Use Lifecycle's actual budget-refill deadline, not a faster test-only poll.
        delay = service.events.next_dispatch_delay(2)
        fallback_delays_ms.append(round(delay * 1000, 3))
        await asyncio.sleep(delay)
    dispatch_seconds = time.perf_counter() - started - queued_seconds
    latest = {}
    for payload, arrived_at in receiver.received:
        assert payload['name'] == MAILBOX_EVENT
        latest[payload['data']['recipient_slot_id']] = (payload, arrived_at)
    assert set(latest) == {dot_id for dot_id, _ in bindings}
    assert 0 < receiver.peak <= DISPATCH_CAPACITY
    assert set(receiver.peaks.values()) == {1}
    first_slow = next(index for index, (payload, _) in enumerate(receiver.received)
                      if payload['data']['recipient_slot_id'] == receiver.slow_dot)
    assert any(payload['data']['recipient_slot_id'] != receiver.slow_dot
               for payload, _ in receiver.received[:first_slow])

    event_latencies = []
    for payload, arrived_at in receiver.received:
        original = service.store.one('SELECT created FROM mcp_event_outbox WHERE id=?',
                                     (payload['eventId'],))
        event_latencies.append(max(0, arrived_at - original['created']) * 1000)

    async def consume(binding):
        dot_id, generation = binding
        payload, _ = latest[dot_id]
        request = public_request('collaboration_consumer_begin', {
            **scope, 'dot_id': dot_id, 'binding_generation': generation,
            'wake_ticket': payload['data']['wake_ticket'],
            'consumer_run_id': key(), 'idempotency_key': key()})
        began = time.perf_counter()
        result = await service.runtime.invoke(request['tool'], request['arguments'], actor)
        assert result['dot_id'] == dot_id and result['state'] == 'active'
        return (time.perf_counter() - began) * 1000

    begin_latencies = await asyncio.gather(*(consume(binding) for binding in bindings))
    after = service.store.stats_snapshot()
    assert after['worker']['queued'] == after['worker']['running'] == 0
    assert len(set(source_ids)) == 60
    assert len(service.store.all('SELECT * FROM collaboration_dot_messages')) == 60
    assert len(service.store.all('SELECT * FROM dot_mailbox_entries')) == 60
    assert len(service.store.all('SELECT * FROM mcp_event_subscriptions')) == 10
    assert len(service.store.all("SELECT * FROM dot_consumer_sessions WHERE state='active'")) == 10
    assert not service.store.all('SELECT * FROM operations')
    result = {
        'schema_version': 1, 'scenario': 'synthetic_single_owner_ten_dot_hundred_input_attempt_burst',
        'environment': {'system': platform.system(), 'machine': platform.machine(),
                        'python': platform.python_version(), 'sqlite_journal': 'wal'},
        'real_dot_host_verified': False, 'real_device_verified': False,
        'workload': {'dots': 10, 'owner_message_attempts': 100, 'admitted_owner_messages': 60,
                     'message_rate_limited': 40, 'parallel_input_calls': 100,
                     'native_subscriptions': 10, 'slow_callback_delay_ms': 50,
                     'delivery_capacity': DISPATCH_CAPACITY, 'endpoint_capacity': 1},
        'counts': {'delivered_hints': len(receiver.received), 'source_rows': 60,
                   'mailbox_entries': 60, 'consumer_sessions': 10,
                   'dispatch_ticks': dispatch_ticks, 'fallback_delays_ms': fallback_delays_ms,
                   'peak_callbacks': receiver.peak, 'peak_per_endpoint': max(receiver.peaks.values())},
        'input_batch_ms': round(queued_seconds * 1000, 3),
        'dispatch_drain_ms': round(dispatch_seconds * 1000, 3),
        'durable_event_to_synthetic_callback_ms': percentiles(event_latencies),
        'consumer_begin_request_to_server_receipt_ms': percentiles(begin_latencies),
        'store_before': before, 'store_after': after,
        'measurement_scope': 'One local isolated process. Store quantiles are bounded recent samples '
            'and include setup; before/after counts identify the interval. Callback timing includes '
            'intentional queued burst wait and synthetic endpoint delay. The existing owner message '
            'limit admits 60 and rejects 40 explicitly; it is not disabled. No model or mobile latency.',
    }
    print('MULTIDOT_SERVER_LOAD_MEASUREMENT=' + json.dumps(result, ensure_ascii=False, sort_keys=True))
