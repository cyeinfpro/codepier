"""Bounded sustained MCP Events exercise in a new, private fixture directory.

The Hub services, SQLite ledger, dispatcher and public consumer tools are real.
The loopback HTTP receiver and consumers are fixtures, not a native Dot host.
No production state, account, callback, Agent, model or external endpoint is used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, deque
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import platform
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import httpx

from hub.collaboration.common import MAILBOX_EVENT, canonical
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import DISPATCH_BUDGETS, DISPATCH_CAPACITY, EventService, WIRE_HEADER_BYTES
from hub.collaboration.lifecycle import CollaborationLoops
from hub.collaboration.monitor import MonitorService
from hub.collaboration.network import Reply, retry_after_seconds
from hub.collaboration.service import CollaborationService
from hub import iam
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.public_collaboration import request as public_request
from shared.util import DevError

SAMPLE_LIMIT = 65536
SIGNING_BYTES = b'isolated-soak-fixture-signing-key!'
SIGNING_SECRET = 'whsec_' + base64.b64encode(SIGNING_BYTES).decode()
CREDENTIAL_FIELDS = ('consumer_session_id', 'consumer_fence', 'consumer_proof')


def key():
    return uuid.uuid4().hex


@dataclass(frozen=True)
class RunConfig:
    duration_seconds: float = 900
    owners: int = 3
    dots_per_owner: int = 4
    batch_interval_seconds: float = 5
    slow_callback_seconds: float = .10
    fault_every_seconds: float = 180
    fault_duration_seconds: float = 8
    drain_timeout_seconds: float = 120
    sample_interval_seconds: float = 15
    rss_growth_limit_mib: float = 128

    def validate(self):
        ranges = {
            'duration_seconds': (.1, 1800), 'owners': (2, 4), 'dots_per_owner': (1, 6),
            'batch_interval_seconds': (1, 60), 'slow_callback_seconds': (0, 1),
            'fault_every_seconds': (0, 900), 'fault_duration_seconds': (0, 15),
            'drain_timeout_seconds': (5, 180), 'sample_interval_seconds': (1, 60),
            'rss_growth_limit_mib': (1, 1024),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be finite within {low}..{high}')
        if type(self.owners) is not int or type(self.dots_per_owner) is not int:
            raise ValueError('Owner and Dot counts must be integers')
        if self.fault_every_seconds and (self.fault_every_seconds < 5 or self.fault_duration_seconds == 0):
            raise ValueError('Enabled faults need a positive duration and a period of at least five seconds')
        if self.fault_every_seconds and self.fault_every_seconds < 2 * self.fault_duration_seconds:
            raise ValueError('Fault windows must leave a recovery interval')
        return self


class Distribution:
    def __init__(self):
        self.values = deque(maxlen=SAMPLE_LIMIT)
        self.count = 0
        self.maximum = 0.

    def observe(self, milliseconds):
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError('Invalid timing sample')
        self.values.append(milliseconds)
        self.count += 1
        self.maximum = max(self.maximum, milliseconds)

    def snapshot(self):
        values = sorted(self.values)
        return {'count': self.count, 'sample_count': len(values), 'storage_limit': SAMPLE_LIMIT,
                'percentile_method': 'nearest_rank', 'window': 'all' if self.count <= SAMPLE_LIMIT else 'recent',
                'max_ms': round(self.maximum, 3),
                **{label: round(values[math.ceil(len(values) * rank) - 1], 3) if values else None
                   for label, rank in [('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99)]}}


def source_snapshot():
    root = Path(__file__).resolve().parents[1]
    files = sorted([*root.joinpath('hub').rglob('*.py'), *root.joinpath('shared').rglob('*.py'),
                    Path(__file__).resolve()])
    rows = [(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in files]
    encoded = json.dumps(rows, separators=(',', ':')).encode()
    return {'scope': 'all_hub_and_shared_python_and_this_runner',
            'sha256': hashlib.sha256(encoded).hexdigest(), 'files': dict(rows)}


def rss_bytes():
    if os.name == 'nt':
        return None
    try:
        value = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(os.getpid())],
                                        text=True, timeout=5).strip()
        return int(value) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def atomic_json(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(data)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def maximum_window(samples, seconds, *, owner=None, byte_count=False):
    selected = [(at, size) for at, who, size in samples if owner is None or who == owner]
    selected.sort()
    left, total, maximum = 0, 0, 0
    for right, (at, size) in enumerate(selected):
        total += size if byte_count else 1
        while left < right and at - selected[left][0] >= seconds:
            total -= selected[left][1] if byte_count else 1
            left += 1
        maximum = max(maximum, total)
    return maximum


def budget_evidence(samples, owners):
    output = {}
    for name, owner in [('global', None), *[(f'owner_{n}', n) for n in range(owners)]]:
        limits = DISPATCH_BUDGETS['global' if owner is None else 'owner']
        count, size = maximum_window(samples, 1, owner=owner), maximum_window(samples, 1, owner=owner, byte_count=True)
        output[name] = {'maximum_attempts_in_one_second': count, 'maximum_accounted_bytes_in_one_second': size,
                        'count_envelope': limits[0] + limits[1],
                        'byte_envelope': limits[2] + limits[3],
                        'within_envelopes': count <= limits[0] + limits[1] and size <= limits[2] + limits[3]}
    return output


class LoopbackReceiver:
    """Explicit test adapter: approved-looking fixture URL -> owned loopback HTTP.

    This does not alter or claim to test the public-DNS/TLS callback policy.
    The signed production body and headers cross an actual local HTTP socket.
    """
    def __init__(self, harness):
        self.h = harness
        self.server = self.client = None
        self.handlers = set()
        self.active = 0
        self.peak = 0
        self.endpoint_active = Counter()
        self.endpoint_peak = Counter()
        self.owner_active = Counter()
        self.owner_peak = Counter()
        self.attempts = deque(maxlen=SAMPLE_LIMIT)
        self.attempt_count = 0
        self.failure_statuses = Counter()
        self.body_digests = {}
        self.accepted = Counter()
        self.duplicates = 0
        self.challenges = 0
        self.accepted_event_ids = set()
        self.accepted_samples = deque(maxlen=SAMPLE_LIMIT)
        self.fault_windows = Counter()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=32768)
        self.port = self.server.sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(trust_env=False, timeout=10, follow_redirects=False,
                                       limits=httpx.Limits(max_connections=8, max_keepalive_connections=0))

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.client:
            await self.client.aclose()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)

    async def send(self, url, body, headers):
        parsed = urlsplit(url)
        route = parsed.hostname.removesuffix('.soak.example.invalid')
        if (parsed.scheme != 'https' or parsed.hostname != route + '.soak.example.invalid'
                or parsed.path != '/events' or route not in self.h.routes):
            raise RuntimeError('Fixture sender refuses a non-fixture route')
        data = body.encode() if isinstance(body, str) else body
        response = await self.client.post(f'http://127.0.0.1:{self.port}/{route}',
                                         content=data, headers=headers)
        return Reply(response.status_code, response.content,
                     retry_after_seconds(response.headers.get('Retry-After')))

    def fault_active(self, binding, elapsed):
        every, duration = self.h.config.fault_every_seconds, self.h.config.fault_duration_seconds
        return bool(binding['owner_index'] == 0 and binding['dot_index'] == 0
                    and every and elapsed >= every and elapsed % every < duration
                    and elapsed < self.h.config.duration_seconds)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        binding = None
        entered = False
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            lines = header.decode('ascii').split('\r\n')
            method, path, _ = lines[0].split(' ')
            headers = {name.lower(): value.strip() for name, value in
                       (line.split(':', 1) for line in lines[1:] if line)}
            length = int(headers.get('content-length', '-1'))
            if method != 'POST' or not 0 <= length <= 16384 or path[1:] not in self.h.routes:
                raise RuntimeError('Invalid fixture HTTP request')
            body = await asyncio.wait_for(reader.readexactly(length), 10)
            message_id = headers['webhook-id']
            signed = message_id.encode() + b'.' + headers['webhook-timestamp'].encode() + b'.' + body
            expected = 'v1,' + base64.b64encode(hmac.new(SIGNING_BYTES, signed, hashlib.sha256).digest()).decode()
            if not any(hmac.compare_digest(expected, value) for value in headers['webhook-signature'].split()):
                raise RuntimeError('Webhook signature mismatch')
            payload = json.loads(body)
            binding = self.h.routes[path[1:]]
            if payload.get('type') == 'verification':
                self.challenges += 1
                await self.respond(writer, 200, canonical({'challenge': payload['challenge']}).encode())
                return
            if payload['name'] != MAILBOX_EVENT or payload['data']['recipient_slot_id'] != binding['dot_id']:
                raise RuntimeError('Callback target or event mismatch')
            if payload['eventId'] != message_id:
                raise RuntimeError('Webhook event identity mismatch')
            now = time.monotonic()
            elapsed = now - self.h.started if self.h.started is not None else -1
            digest = hashlib.sha256(body).hexdigest()
            if message_id not in self.body_digests and len(self.body_digests) >= SAMPLE_LIMIT:
                raise RuntimeError('Fixture retry-body retention bound exceeded')
            previous = self.body_digests.setdefault(message_id, digest)
            if previous != digest:
                raise RuntimeError('Retried body changed')
            self.active += 1
            self.peak = max(self.peak, self.active)
            route = binding['route']
            owner = binding['owner_index']
            self.endpoint_active[route] += 1
            self.endpoint_peak[route] = max(self.endpoint_peak[route], self.endpoint_active[route])
            self.owner_active[owner] += 1
            self.owner_peak[owner] = max(self.owner_peak[owner], self.owner_active[owner])
            entered = True
            if elapsed >= 0:
                self.attempt_count += 1
                self.attempts.append((elapsed, owner, len(body) + WIRE_HEADER_BYTES))
            if self.fault_active(binding, elapsed):
                self.failure_statuses[503] += 1
                self.fault_windows[int(elapsed // self.h.config.fault_every_seconds)] += 1
                await self.respond(writer, 503, b'', retry_after='2')
                return
            if owner == 0 and binding['dot_index'] == 1:
                await asyncio.sleep(self.h.config.slow_callback_seconds)
            received_at = time.time()
            if message_id not in self.accepted_event_ids:
                self.accepted_event_ids.add(message_id)
                if len(self.accepted_event_ids) > SAMPLE_LIMIT:
                    raise RuntimeError('Fixture event retention bound exceeded')
                event = await self.h.store.run(self.h.store.one,
                    'SELECT created FROM mcp_event_outbox WHERE id=?', (message_id,))
                if not event:
                    raise RuntimeError('Delivered event is absent from the durable outbox')
                if self.h.started_wall is not None and event['created'] >= self.h.started_wall:
                    latency = max(0, received_at - event['created']) * 1000
                    self.h.event_latency['overall'].observe(latency)
                    self.h.event_latency[self.h.endpoint_class(binding)].observe(latency)
                    self.h.owner_event_latency[owner].observe(latency)
                self.accepted[owner] += 1
                if elapsed >= 0:
                    self.accepted_samples.append((elapsed, owner))
                binding['hints'].put_nowait(payload)
            else:
                self.duplicates += 1
            await self.respond(writer, 204, b'')
        except Exception as exc:
            self.h.error('receiver', exc)
            try:
                await self.respond(writer, 500, b'')
            except (OSError, ConnectionError):
                pass
        finally:
            if entered:
                self.active -= 1
                self.endpoint_active[binding['route']] -= 1
                self.owner_active[binding['owner_index']] -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.handlers.discard(task)

    @staticmethod
    async def respond(writer, status, body, retry_after=None):
        extras = f'Retry-After: {retry_after}\r\n' if retry_after else ''
        writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{extras}\r\n'.encode() + body)
        await writer.drain()



class LifecycleWarnings(logging.Handler):
    """Observe fixed, already-sanitized lifecycle failures without changing it."""
    COMPONENTS = frozenset({'reconcile', 'events', 'collector', 'weixin_poll',
        'weixin_delivery', 'weixin_media', 'telegram_poll', 'telegram_media', 'channel_mirrors'})

    def __init__(self, errors):
        super().__init__(logging.WARNING)
        self.errors = errors
        self.observed = 0

    def emit(self, record):
        if record.name != 'hub.collaboration.lifecycle' or record.levelno < logging.WARNING:
            return
        self.observed += 1
        component, exception_type = 'unknown', 'LifecycleWarning'
        if (record.msg == 'Collaboration component %s degraded (%s)'
                and isinstance(record.args, tuple) and len(record.args) == 2):
            name, kind = record.args
            if isinstance(name, str) and name in self.COMPONENTS:
                component = name
            if (isinstance(kind, str) and 0 < len(kind) <= 64
                    and kind.isascii() and kind.replace('_', '').isalnum()):
                exception_type = kind
        if len(self.errors) < 64:
            self.errors.append({'component': 'lifecycle:' + component, 'type': exception_type, 'code': ''})


class SoakHarness:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config = config.validate()
        self.store = self.service = self.runtime = None
        self.created = False
        self.receiver = LoopbackReceiver(self)
        self.bindings = []
        self.routes = {}
        self.started = self.started_wall = None
        self.errors = []
        self.lifecycle_observer = LifecycleWarnings(self.errors)
        self.lifecycle_logger = logging.getLogger('hub.collaboration.lifecycle')
        self.source_ids = set()
        self.reply_ids = set()
        self.admitted = Counter()
        self.replied = Counter()
        self.rate_limited = Counter()
        self.attempted = Counter()
        self.consumer_starts = Counter()
        self.consumer_yields = Counter()
        self.consumer_inflight = 0
        self.races = 0
        self.queue_peak = 0
        self.memory = deque(maxlen=2048)
        self.event_latency = {name: Distribution() for name in ('overall', 'healthy', 'slow', 'faulted')}
        self.owner_event_latency = {n: Distribution() for n in range(config.owners)}
        self.input_latency = Distribution()
        self.reply_latency = {name: Distribution() for name in ('overall', 'healthy', 'slow', 'faulted')}
        self.begin_latency = Distribution()
        self.tasks = []
        self.sampling_done = asyncio.Event()
        self.measurement_ended = None
        self.last_progress_print = -60.
        self.before = None
        self.source_before = source_snapshot()

    @staticmethod
    def endpoint_class(binding):
        if binding['owner_index'] == 0 and binding['dot_index'] == 0:
            return 'faulted'
        if binding['owner_index'] == 0 and binding['dot_index'] == 1:
            return 'slow'
        return 'healthy'

    def error(self, where, error):
        if len(self.errors) < 64:
            self.errors.append({'component': where, 'type': type(error).__name__,
                                'code': getattr(error, 'code', '')})

    async def invoke(self, binding, alias, arguments):
        request = public_request(alias, arguments)
        return await self.runtime.invoke(request['tool'], request['arguments'], binding['actor'])

    @staticmethod
    def scope(binding):
        return {**binding['scope'], 'dot_id': binding['dot_id']}

    async def setup(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.created = True
        os.chmod(self.directory, 0o700)
        self.store = Store(self.directory / 'fixture-hub')
        self.runtime = Runtime(self.store)
        self.service = CollaborationService(self.runtime, CollaborationConfig(
            enabled=True, events_enabled=True, weixin_enabled=False, telegram_enabled=False))
        self.runtime.collaboration = self.service
        self.store.collaboration = self.service
        self.service.events = EventService(self.service, self.receiver.send)
        self.service.monitor = MonitorService(self.service)
        self.service.health = {}
        self.service.loops = CollaborationLoops(self.service)
        await self.receiver.start()
        for owner_index in range(self.config.owners):
            user_id, project_id, grant_id = [f'soak-{kind}-{owner_index}' for kind in ('owner', 'project', 'grant')]
            root = self.directory / project_id
            root.mkdir()
            with self.store.transaction():
                self.store.execute("INSERT INTO users VALUES(?,?,'!fixture-only',?)", (user_id, user_id, time.time()))
                space_id = iam.create_personal_space(self.store, user_id, 'Isolated soak owner')
                device_id = 'soak-device-' + str(owner_index)
                self.store.execute("""INSERT INTO devices(id,name,secret,created,space_id,owner_user_id)
                    VALUES(?,'fixture',?,?,?,?)""",
                    (device_id, self.store.encrypt('isolated-fixture-device'), time.time(), space_id, user_id))
                self.store.execute("""INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id)
                    VALUES(?,?,?,?,?,'write',1,?,?,?)""",
                    (project_id, project_id, project_id, device_id, str(root.resolve()), time.time(), space_id, user_id))
                self.store.execute("""INSERT INTO grants(id,user_id,label,scopes,projects,created,space_id,owner_user_id)
                    VALUES(?,?,'isolated-soak',?,?,?,?,?)""",
                    (grant_id, user_id, json.dumps(['read', 'write', 'execute']), json.dumps([project_id]), time.time(), space_id, user_id))
            owner = Principal('panel:' + user_id, user_id, {'read', 'write'}, ['*'], admin=True, space_id=space_id)
            actor = Principal('mcp:' + grant_id + ':isolated-soak', user_id,
                              {'read', 'write', 'execute'}, [project_id], grant_id=grant_id, space_id=space_id)
            scope = {'project': project_id, 'environment_id': 'soak-fixture'}
            room = (await self.store.run(self.service.room_create, {**scope, 'idempotency_key': key()}, owner))['room']
            for dot_index in range(self.config.dots_per_owner):
                created = await self.store.run(self.service.dots.create, {
                    **scope, 'conversation_id': room['id'], 'label': f'Fixture Dot {owner_index}-{dot_index}',
                    'capabilities': ['read'], 'duplex': True, 'confirm_tasks': True, 'idempotency_key': key()}, owner)
                dot_id = created['dot']['id']
                await self.runtime.invoke('collaboration', {'action': 'join',
                    'code': created['dot']['join_code'], 'idempotency_key': key()}, actor)
                binding = {'owner_index': owner_index, 'dot_index': dot_index, 'route': f'o{owner_index}d{dot_index}',
                           'owner': owner, 'actor': actor, 'scope': scope, 'room_id': room['id'],
                           'dot_id': dot_id, 'hints': asyncio.Queue(maxsize=16)}
                self.bindings.append(binding)
                self.routes[binding['route']] = binding
                configured = await self.invoke(binding, 'collaboration_dot_mailbox_configure',
                    {**self.scope(binding), 'confirm_consumer_protocol': True, 'idempotency_key': key()})
                binding['generation'] = configured['binding_generation']
                await self.service.events.subscribe({**configured['subscription_request'], 'delivery': {
                    'mode': 'webhook', 'url': f'https://{binding["route"]}.soak.example.invalid/events',
                    'secret': SIGNING_SECRET}}, actor)
        await self.service.events.tick()
        if self.errors:
            raise RuntimeError('Fixture enrollment failed')
        self.before = self.store.stats_snapshot()
        self.started, self.started_wall = time.monotonic(), time.time()
        self.lifecycle_logger.addHandler(self.lifecycle_observer)
        await self.service.loops.start()
        self.tasks = [asyncio.create_task(self.consumer(binding), name='soak-consumer-' + binding['route'])
                      for binding in self.bindings]
        self.tasks.append(asyncio.create_task(self.sample(), name='soak-sample'))

    async def produce_one(self, binding, round_number):
        owner = binding['owner_index']
        self.attempted[owner] += 1
        began = time.monotonic()
        try:
            result = await self.store.run(self.service.chatroom.create, {
                **binding['scope'], 'room_id': binding['room_id'], 'conversation_id': binding['room_id'],
                'body_text': f'Isolated soak input {round_number} {binding["route"]}',
                'mentions': [{'slot_id': binding['dot_id']}],
                'client_message_id': key(), 'idempotency_key': key()}, binding['owner'])
        except DevError as exc:
            if exc.code != 'MESSAGE_RATE_LIMIT':
                raise
            self.rate_limited[owner] += 1
        else:
            identifier = result['message']['id']
            if identifier in self.source_ids or len(self.source_ids) >= SAMPLE_LIMIT:
                raise RuntimeError('Source identity or retention invariant failed')
            self.source_ids.add(identifier)
            self.admitted[owner] += 1
        finally:
            self.input_latency.observe((time.monotonic() - began) * 1000)

    async def produce(self):
        round_number, deadline = 0, self.started + self.config.duration_seconds
        while time.monotonic() < deadline and not self.errors:
            await asyncio.gather(*(self.produce_one(binding, round_number) for binding in self.bindings))
            round_number += 1
            next_at = self.started + round_number * self.config.batch_interval_seconds
            remaining = min(next_at, deadline) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
        self.measurement_ended = time.monotonic()

    async def consumer(self, binding):
        while True:
            payload = await binding['hints'].get()
            self.consumer_inflight += 1
            try:
                began = time.monotonic()
                session = await self.invoke(binding, 'collaboration_consumer_begin', {
                    **self.scope(binding), 'binding_generation': binding['generation'],
                    'wake_ticket': payload['data']['wake_ticket'], 'consumer_run_id': key(),
                    'lease_seconds': 120, 'idempotency_key': key()})
                self.begin_latency.observe((time.monotonic() - began) * 1000)
                self.consumer_starts[binding['owner_index']] += 1
                credentials = {name: session[name] for name in CREDENTIAL_FIELDS}
                for _ in range(100):
                    inbox = await self.invoke(binding, 'collaboration_dot_inbox', {**self.scope(binding), 'limit': 100})
                    for item in inbox['items']:
                        mailbox = await self.invoke(binding, 'collaboration_dot_mailbox', {
                            **self.scope(binding), 'binding_generation': binding['generation'], 'limit': 100})
                        source_id = item['message_id']
                        try:
                            reply = await self.invoke(binding, 'collaboration_dot_message', {
                                **self.scope(binding), **credentials, 'expected_inbox_revision': mailbox['dirty_revision'],
                                'reply_to_id': source_id, 'body_text': 'Synthetic fixture reply; no Agent execution.',
                                'complete': True, 'idempotency_key': 'soak-reply-' + source_id})
                        except DevError as exc:
                            if exc.code == 'DOT_INBOX_CHANGED':
                                self.races += 1
                                break
                            raise
                        reply_id = reply['message']['id']
                        if reply_id not in self.reply_ids:
                            self.reply_ids.add(reply_id)
                            self.replied[binding['owner_index']] += 1
                            row = await self.store.run(self.store.one,
                                'SELECT created FROM collaboration_messages WHERE id=?', (source_id,))
                            latency = max(0, time.time() - row['created']) * 1000
                            self.reply_latency['overall'].observe(latency)
                            self.reply_latency[self.endpoint_class(binding)].observe(latency)
                    mailbox = await self.invoke(binding, 'collaboration_dot_mailbox', {
                        **self.scope(binding), 'binding_generation': binding['generation'], 'limit': 100})
                    yielded = await self.invoke(binding, 'collaboration_consumer_yield', {
                        **self.scope(binding), **credentials, 'expected_inbox_revision': mailbox['dirty_revision'],
                        'idempotency_key': key()})
                    if not yielded['more']:
                        self.consumer_yields[binding['owner_index']] += 1
                        break
                else:
                    raise RuntimeError('Consumer exceeded its bounded pass count')
            except Exception as exc:
                self.error('consumer', exc)
            finally:
                self.consumer_inflight -= 1
                binding['hints'].task_done()

    async def sample(self):
        while not self.sampling_done.is_set():
            rss = await asyncio.to_thread(rss_bytes)
            state = self.store.stats_snapshot()
            self.queue_peak = max(self.queue_peak, state['worker']['queued'])
            self.memory.append({'elapsed_seconds': round(time.monotonic() - self.started, 3),
                                'rss_bytes': rss, 'queued': state['worker']['queued'],
                                'running': state['worker']['running']})
            atomic_json(self.directory / 'progress.json', {
                'state': 'running', 'elapsed_seconds': round(time.monotonic() - self.started, 3),
                'admitted': sum(self.admitted.values()), 'replied': sum(self.replied.values()),
                'callback_attempts': self.receiver.attempt_count, 'injected_503': self.receiver.failure_statuses[503],
                'errors': self.errors, 'last_sample': self.memory[-1]})
            elapsed = time.monotonic() - self.started
            if elapsed - self.last_progress_print >= 60:
                self.last_progress_print = elapsed
                print('MULTIDOT_SOAK_PROGRESS=' + json.dumps({
                    'elapsed_seconds': round(elapsed, 3), 'admitted': sum(self.admitted.values()),
                    'replied': sum(self.replied.values()), 'callback_attempts': self.receiver.attempt_count,
                    'injected_503': self.receiver.failure_statuses[503], 'errors': self.errors}), flush=True)
            try:
                await asyncio.wait_for(self.sampling_done.wait(), self.config.sample_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def persisted(self):
        rows = self.store.all('SELECT message_id,state FROM collaboration_dot_messages')
        reply_counts = {row['message_id']: row['n'] for row in self.store.all('''
            SELECT d.message_id,COUNT(r.id) AS n FROM collaboration_dot_messages d
            JOIN collaboration_messages m ON m.id=d.message_id
            JOIN collaboration_join_slots s ON s.id=d.dot_id
            LEFT JOIN collaboration_messages r ON r.reply_to_id=m.id AND r.origin='bound_connector'
                AND r.room_id=m.room_id AND r.conversation_id=m.conversation_id AND r.author=s.grant_id
            GROUP BY d.message_id''')}
        delivery_counts = {row['state']: row['n'] for row in self.store.all(
            'SELECT state,COUNT(*) AS n FROM mcp_event_deliveries GROUP BY state')}
        return {'source_ids': {row['message_id'] for row in rows}, 'source_count': len(rows),
                'reply_counts': reply_counts,
                'unhandled': sum(row['state'] != 'handled' for row in rows),
                'dirty_bindings': self.store.one(
                    'SELECT COUNT(*) AS n FROM dot_mailbox_bindings WHERE dirty_revision!=drained_revision')['n'],
                'active_sessions': self.store.one(
                    "SELECT COUNT(*) AS n FROM dot_consumer_sessions WHERE state='active'")['n'],
                'delivery_states': delivery_counts,
                'subscriptions': self.store.one('SELECT COUNT(*) AS n FROM mcp_event_subscriptions')['n'],
                'operations': self.store.one('SELECT COUNT(*) AS n FROM operations')['n'],
                'budgets': self.store.all('SELECT scope_key,tokens,byte_tokens FROM mcp_event_dispatch_budgets'),
                'sqlite_integrity': self.store.one('PRAGMA quick_check')['quick_check']}

    async def drain(self):
        deadline = time.monotonic() + self.config.drain_timeout_seconds
        while time.monotonic() < deadline and not self.errors:
            state = await self.store.run(self.persisted)
            pending = sum(state['delivery_states'].get(name, 0) for name in ('pending', 'retry_wait', 'leased'))
            fixture_busy = (self.consumer_inflight or self.receiver.active or self.receiver.handlers
                            or any(binding['hints'].qsize() for binding in self.bindings))
            if not pending and not fixture_busy and not state['unhandled'] and not state['dirty_bindings'] and not state['active_sessions']:
                return state
            await asyncio.sleep(.25)
        return await self.store.run(self.persisted)

    async def result(self, persisted):
        after = self.store.stats_snapshot()
        final_rss = await asyncio.to_thread(rss_bytes)
        self.memory.append({'elapsed_seconds': round(time.monotonic() - self.started, 3),
                            'rss_bytes': final_rss, 'queued': after['worker']['queued'],
                            'running': after['worker']['running']})
        source_after = source_snapshot()
        rss = [row['rss_bytes'] for row in self.memory if row['rss_bytes'] is not None]
        growth = max(rss) - rss[0] if rss else None
        count_interval = {name: after['worker'][name] - self.before['worker'][name]
                          for name in ('submitted', 'completed', 'failed', 'rejected')}
        transaction_interval = {name: after['transactions'][name] - self.before['transactions'][name]
                                for name in ('count', 'committed', 'rolled_back', 'commit_failed', 'rollback_failed')}
        worker_window_clean = count_interval['completed'] >= after['worker']['sample_count']
        transaction_window_clean = transaction_interval['count'] >= after['transactions']['sample_count']
        source_equal = source_after == self.source_before
        budgets = []
        for row in persisted.pop('budgets'):
            limits = DISPATCH_BUDGETS['global' if row['scope_key'] == 'global' else 'owner']
            budgets.append(-1e-6 <= row['tokens'] <= limits[1] + 1e-6 and
                           -1e-6 <= row['byte_tokens'] <= limits[3] + 1e-6)
        source_ids_equal = persisted.pop('source_ids') == self.source_ids
        reply_counts = persisted.pop('reply_counts')
        exact_replies = set(reply_counts) == self.source_ids and all(n == 1 for n in reply_counts.values())
        persisted['sources_without_exact_bound_reply'] = sum(n != 1 for n in reply_counts.values())
        envelopes = budget_evidence(list(self.receiver.attempts), self.config.owners)
        pending = sum(persisted['delivery_states'].get(name, 0) for name in ('pending', 'retry_wait', 'leased'))
        elapsed = self.measurement_ended - self.started
        fault_progress = []
        every = self.config.fault_every_seconds
        if every:
            for number in range(1, int(self.config.duration_seconds // every) + 1):
                start = number * every
                end = start + self.config.fault_duration_seconds + self.config.batch_interval_seconds
                if end > self.config.duration_seconds:
                    continue
                counts = {f'owner_{n}': sum(start <= at < end and who == n for at, who in self.receiver.accepted_samples)
                          for n in range(1, self.config.owners)}
                fault_progress.append({'window': number, 'start_seconds': start,
                                       'healthy_observation_end_seconds': end,
                                       'injected_503_count': self.receiver.fault_windows[number],
                                       'other_owner_accepted': counts})
        fixture_queues = {'consumer_inflight': self.consumer_inflight,
                          'callback_active': self.receiver.active,
                          'hints_queued': sum(binding['hints'].qsize() for binding in self.bindings),
                          'http_handlers': len(self.receiver.handlers)}
        checks = {
            'source_files_unchanged': source_equal,
            'requested_wall_duration_observed': elapsed >= self.config.duration_seconds,
            'every_owner_admitted_and_replied': all(self.admitted[n] > 0 and self.admitted[n] == self.replied[n]
                                                  for n in range(self.config.owners)),
            'source_identity_set_preserved': source_ids_equal,
            'exactly_one_reply_per_admitted_source': exact_replies and len(self.reply_ids) == len(self.source_ids),
            'all_admitted_sources_handled': persisted['unhandled'] == 0,
            'mailboxes_drained': persisted['dirty_bindings'] == 0,
            'consumers_yielded': persisted['active_sessions'] == 0,
            'fixture_consumers_and_hint_queues_drained': not any(fixture_queues.values()),
            'delivery_backlog_empty': pending == 0,
            'no_dead_letters': persisted['delivery_states'].get('dead_letter', 0) == 0,
            'all_subscriptions_stable': persisted['subscriptions'] == len(self.bindings),
            'no_execution_operations': persisted['operations'] == 0,
            'database_integrity': persisted['sqlite_integrity'] == 'ok',
            'callback_global_capacity': self.receiver.peak <= DISPATCH_CAPACITY,
            'callback_endpoint_capacity': max(self.receiver.endpoint_peak.values(), default=0) <= 1,
            'persisted_budget_bounds': bool(budgets) and all(budgets),
            'observed_budget_envelopes': all(row['within_envelopes'] for row in envelopes.values()),
            'complete_attempt_sample': self.receiver.attempt_count == len(self.receiver.attempts),
            'no_unexpected_errors': not self.errors and self.lifecycle_observer.observed == 0,
            'lifecycle_failures_observable': self.lifecycle_logger.isEnabledFor(logging.WARNING)
                and self.lifecycle_observer in self.lifecycle_logger.handlers,
            'scheduled_fault_exercised': not fault_progress or any(row['injected_503_count'] > 0 for row in fault_progress),
            'other_owners_progress_during_injected_faults': all(
                not row['injected_503_count'] or all(row['other_owner_accepted'].values()) for row in fault_progress),
            'worker_idle': after['worker']['queued'] == after['worker']['running'] == 0,
            'no_commit_or_rollback_failure': transaction_interval['commit_failed'] == transaction_interval['rollback_failed'] == 0,
            'rss_sampled_within_fixture_budget': growth is not None and growth <= self.config.rss_growth_limit_mib * 1024 ** 2,
        }
        owners = {f'owner_{n}': {
            'attempted': self.attempted[n], 'admitted': self.admitted[n], 'rate_limited': self.rate_limited[n],
            'replied': self.replied[n], 'accepted_callbacks': self.receiver.accepted[n],
            'consumer_starts': self.consumer_starts[n], 'consumer_yields': self.consumer_yields[n],
            'event_to_callback_ms': self.owner_event_latency[n].snapshot()}
            for n in range(self.config.owners)}
        return {
            'schema_version': 1, 'state': 'passed' if all(checks.values()) else 'failed',
            'scenario': 'isolated_multiowner_continuous_inputs_loopback_signed_http_public_consumers',
            'duration_class': 'sustained_15_minute' if elapsed >= 900 and self.config.duration_seconds >= 900 else 'short_self_check',
            'real_dot_host_verified': False, 'real_device_verified': False, 'grants_authority': False,
            'environment': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
            'config': asdict(self.config), 'measured_seconds': round(elapsed, 3),
            'drain_seconds': round(time.monotonic() - self.measurement_ended, 3),
            'source_before': self.source_before, 'source_after_sha256': source_after['sha256'],
            'checks': checks, 'owners': owners, 'persisted': persisted, 'errors': self.errors,
            'lifecycle_warning_count': self.lifecycle_observer.observed,
            'fixture_queues': fixture_queues,
            'transport': {'kind': 'actual_loopback_http_injected_fixture_sender',
                'native_public_dns_tls_policy_tested': False, 'signature_verification': True,
                'challenge_count': self.receiver.challenges, 'peak_callbacks': self.receiver.peak,
                'peak_per_endpoint': max(self.receiver.endpoint_peak.values(), default=0),
                'peak_per_owner': {f'owner_{owner}': value for owner, value in self.receiver.owner_peak.items()}, 'attempts': self.receiver.attempt_count,
                'duplicate_http_events': self.receiver.duplicates, 'injected_statuses': {str(status): count for status, count in self.receiver.failure_statuses.items()},
                'retry_body_digest_unchanged': not any(row['component'] == 'receiver' for row in self.errors)},
            'durable_event_to_callback_ms': {name: dist.snapshot() for name, dist in self.event_latency.items()},
            'admitted_input_to_fixture_reply_ms': {name: dist.snapshot() for name, dist in self.reply_latency.items()},
            'input_call_ms': self.input_latency.snapshot(), 'consumer_begin_call_ms': self.begin_latency.snapshot(),
            'expected_revision_races': self.races, 'budget_envelopes': envelopes,
            'fault_window_progress': fault_progress,
            'store_interval_counts': {'worker': count_interval, 'transactions': transaction_interval},
            'store_final': {
                'worker': after['worker'] if worker_window_clean else {
                    **{name: value for name, value in after['worker'].items() if name not in {'queue_ms', 'phase_ms'}},
                    'quantiles_omitted': 'recent window still includes setup'},
                'transactions': after['transactions'] if transaction_window_clean else {
                    **{name: value for name, value in after['transactions'].items() if name != 'duration_ms'},
                    'quantiles_omitted': 'recent window still includes setup'}},
            'memory': {'measurement': 'whole_fixture_process_current_rss_includes_harness_and_sqlite',
                       'sample_count': len(rss), 'sample_storage_limit': self.memory.maxlen,
                       'start_bytes': rss[0] if rss else None, 'last_bytes': rss[-1] if rss else None,
                       'peak_bytes': max(rss) if rss else None, 'peak_growth_bytes': growth,
                       'samples': list(self.memory), 'queue_sample_peak': self.queue_peak,
                       'interpretation': 'Bounded fixture budget, not a leak proof. Append-only business rows and SQLite cache grow during the run.'},
            'measurement_scope': 'The budget envelope checks only observed one-second windows, not arbitrary-interval token-bucket conformance. Input, callback, begin and reply timings exclude enrollment. Store quantiles are bounded recent windows, included only once all setup samples have left that window. Healthy, slow and fault-injected endpoints are separate. Faulted tail includes unchanged production 24-30 second first retry and later backoff. No real model, host, device, deployment or process-fault proof.',
        }

    async def close(self):
        self.sampling_done.set()
        if self.service and getattr(self.service, 'loops', None):
            await self.service.loops.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.receiver.close()
        self.lifecycle_logger.removeHandler(self.lifecycle_observer)
        self.lifecycle_observer.close()
        if self.store:
            self.store.close()


async def run(directory, config):
    harness = SoakHarness(directory, config)
    try:
        await harness.setup()
        await harness.produce()
        persisted = await harness.drain()
        harness.sampling_done.set()
        await asyncio.gather(*(task for task in harness.tasks if task.get_name() == 'soak-sample'))
        await harness.service.loops.stop()
        result = await harness.result(persisted)
        atomic_json(harness.directory / 'summary.json', result)
        atomic_json(harness.directory / 'progress.json', {
            'state': result['state'], 'measured_seconds': result['measured_seconds'],
            'admitted': sum(harness.admitted.values()), 'replied': sum(harness.replied.values()),
            'failed_checks': [name for name, value in result['checks'].items() if not value]})
        return result
    except BaseException as exc:
        if harness.created:
            atomic_json(harness.directory / 'failure.json', {'state': 'failed',
                'type': type(exc).__name__, 'code': getattr(exc, 'code', ''), 'errors': harness.errors})
        raise
    finally:
        await harness.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private fixture directory; never an existing Hub directory')
    for name, field in RunConfig.__dataclass_fields__.items():
        parser.add_argument('--' + name.replace('_', '-'), type=int if name in {'owners', 'dots_per_owner'} else float,
                            default=field.default)
    args = vars(parser.parse_args(argv))
    output = args.pop('output')
    config = RunConfig(**args).validate()
    result = asyncio.run(run(output, config))
    print(json.dumps({'state': result['state'], 'duration_class': result['duration_class'],
                      'measured_seconds': result['measured_seconds'],
                      'admitted': sum(item['admitted'] for item in result['owners'].values()),
                      'replied': sum(item['replied'] for item in result['owners'].values()),
                      'failed_checks': [name for name, value in result['checks'].items() if not value],
                      'report': str(output / 'summary.json')}, sort_keys=True))
    return 0 if result['state'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
