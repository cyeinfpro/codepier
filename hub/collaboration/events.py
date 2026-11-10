"""MCP Events webhook adapter: transaction outbox, fenced delivery and replay.

All database phases run on Store.run. No database transaction spans a network
await. Payloads contain authorized object references, never callback credentials.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import math
import logging
import secrets
import time

import httpx
from hub.principal import refresh_principal
from hub.mcp_request_audit import mark
from hub.collaboration.event_errors import CallbackEndpointError, callback_reason
from hub.collaboration import network
from hub.collaboration.common import (TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT, MESSAGE_EVENT, WORK_EVENT, DELEGATION_EVENT, OPERATION_EVENT, MAILBOX_EVENT, ROOM_INBOX_EVENT,
    SEVERITIES, canonical, digest, read_cursor, sign_cursor, timestamp, validate)
from hub.collaboration.event_contracts import (FILTERS, PAYLOADS, Subscribe, Unsubscribe,
    UnifiedOperationPayload, definitions, event_help)
from shared.util import DevError

logger = logging.getLogger(__name__)

DISPATCH_CAPACITY = 4
MAX_ENVELOPE_BYTES = 16384
WIRE_HEADER_BYTES = 768  # Two signatures plus bounded webhook/HTTP header overhead.
# rate, burst, byte rate, byte burst; shared by all dispatcher processes.
DISPATCH_BUDGETS = {'global': (8., 16., 128 * 1024., 256 * 1024.),
                    'owner': (4., 8., 64 * 1024., 128 * 1024.)}
SEALED_BODY_PREFIX = 'sealed:v1:'


def authorization_transient(exc):
    # A changed operation binding cannot regain consent by waiting or by
    # restoring the old mapping. Require a newly authorized subscription.
    if exc.code in {'OPERATION_MAPPING_CHANGED', 'OPERATION_CONTEXT_INVALID', 'OPERATION_EVENT_OBSOLETE'}:
        return False
    return (exc.code in {'ROOM_PAUSED', 'WORKER_BINDING_REQUIRED', 'GOAL_INACTIVE'}
            or exc.status in {409, 429} or exc.status >= 500)


def migrate_dispatch(db):
    """Additive scheduling state; callback URLs never become metric keys."""
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_event_dispatch_routes (
        subscription_id TEXT PRIMARY KEY REFERENCES mcp_event_subscriptions(id) ON DELETE CASCADE,
        endpoint_key TEXT NOT NULL, owner_key TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_dispatch_routes_endpoint
        ON mcp_event_dispatch_routes(endpoint_key,subscription_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_dispatch_routes_owner
        ON mcp_event_dispatch_routes(owner_key,subscription_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_event_dispatch_endpoints (
        endpoint_key TEXT PRIMARY KEY, cooldown_until REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_event_dispatch_owners (
        owner_key TEXT PRIMARY KEY,after_subscription TEXT NOT NULL,
        updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_event_dispatch_budgets (
        scope_key TEXT PRIMARY KEY,tokens REAL NOT NULL,byte_tokens REAL NOT NULL,
        updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mcp_event_dispatch_cursor (
        id TEXT PRIMARY KEY CHECK(id='global'),after_owner TEXT NOT NULL,
        updated REAL NOT NULL)""")
    # These are inspection continuations, never evidence of a successful send.
    # They let bounded pages pass temporarily unauthorizable legacy heads.
    if 'scan_after_owner' not in {row[1] for row in db.execute('PRAGMA table_info(mcp_event_dispatch_cursor)')}:
        db.execute('ALTER TABLE mcp_event_dispatch_cursor ADD COLUMN scan_after_owner TEXT')
    # Preserve the original three-column owner service journal: released
    # dispatchers insert into it positionally. A read-only upgrade must not
    # invalidate their otherwise supported rollback before native writes.
    db.execute('''CREATE TABLE IF NOT EXISTS mcp_event_dispatch_owner_scans (
        owner_key TEXT PRIMARY KEY,after_subscription TEXT NOT NULL,
        updated REAL NOT NULL)''')
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_deliveries_head
        ON mcp_event_deliveries(subscription_id,event_seq)
        WHERE state IN ('pending','retry_wait','leased')""")
    # Global concurrency checks must visit only currently leased deliveries,
    # not all accepted/abandoned history retained for audit and replay.
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_deliveries_live_leases
        ON mcp_event_deliveries(lease_until,subscription_id)
        WHERE state='leased'""")


def signing_key(value):
    try:
        if not isinstance(value, str) or not value.startswith('whsec_'):
            raise ValueError()
        decoded = base64.b64decode(value[6:], validate=True)
        if not 24 <= len(decoded) <= 64:
            raise ValueError()
        return decoded
    except (ValueError, TypeError):
        raise DevError('INVALID_SIGNING_KEY', '签名材料必须符合 whsec_ 编码与长度契约', 400) from None


def signed_headers(identifier, subscription_id, body, keys, at):
    stamp = str(int(at))
    signed = identifier.encode() + b'.' + stamp.encode() + b'.' + body
    signatures = ['v1,' + base64.b64encode(hmac.new(signing_key(key), signed, hashlib.sha256).digest()).decode() for key in keys]
    return {'Content-Type': 'application/json', 'webhook-id': identifier, 'webhook-timestamp': stamp,
            'webhook-signature': ' '.join(signatures), 'X-MCP-Subscription-Id': subscription_id}


class EventService:
    def __init__(self, collaboration, sender=None):
        self.c, self.store = collaboration, collaboration.store
        self.sender = sender or network.webhook
        self._network_limit = asyncio.Semaphore(DISPATCH_CAPACITY)
        self._challenge_limit = asyncio.Semaphore(2)
        self._tick_lock = asyncio.Lock()
        self._scan_after = ''
        self._relay_recover_after = 0
        self._budget_retry_at = None
        from hub.operation_events import OperationEvents
        self.operations = OperationEvents(self)

    def operation_completed(self, operation_id):
        """Join the caller's completion transaction; never perform network I/O."""
        return self.operations.completed(operation_id)

    def subscription_snapshot(self, principal, args):
        snapshot = self.c.principal_snapshot(principal)
        if args['name'] == OPERATION_EVENT:
            filters = validate(FILTERS[OPERATION_EVENT], args['arguments'])
            snapshot['operation_event_binding'] = self.operations.authorize(principal, filters)
        return snapshot

    def guard(self):
        self.c.guard()
        if not self.c.config.events_enabled:
            raise DevError('EVENTS_DISABLED', 'MCP Events 未启用', 409)

    @staticmethod
    def subscription_limit(room):
        # A full native room needs one verification-only replacement per
        # member while its current route remains active. Rooms still permits
        # only one current and one pending binding for each of 32 members.
        return 64 if room.get('_event_conversation') else 32

    def available_events(self, principal):
        self.guard()
        principal = refresh_principal(self.store, principal)
        if not principal.grant_id or 'read' not in principal.scopes:
            return ()
        return tuple(PAYLOADS) if self.c.runtime.list_projects(principal) else (ROOM_INBOX_EVENT,)

    def catalog(self, raw, principal, *, unified=False):
        if set(raw) - {'cursor'} or raw.get('cursor') not in (None, ''):
            raise DevError('INVALID_CURSOR', '事件目录只有一页', 400)
        names = self.available_events(principal)
        if not names:
            return {'events': []}
        from hub.collaboration.channel_capabilities import subscription_proposal
        metadata = {'codepier/subscription_lifetime': subscription_proposal()}
        if unified:
            metadata['codepier/eventHelp'] = {'name': 'query', 'arguments': {'action': 'help', 'args': {'event': 'list'}}}
        return {'events': definitions(names=names, unified=unified), '_meta': metadata}

    def help(self, name, principal, detail):
        names = self.available_events(principal)
        if name == 'list':
            from hub.collaboration.event_contracts import DESCRIPTIONS
            return {'events': {name: DESCRIPTIONS[name] for name in names},
                    'note': 'Use args.event with an event name; args.detail=schema returns exact filters and payload.'}
        if name not in names:
            raise DevError('EVENT_NOT_FOUND', '此事件不在当前订阅目录', 404)
        return event_help(name, detail=detail)

    @staticmethod
    def scope_column(room):
        return 'conversation_id' if room.get('_event_conversation') else 'room_id'

    def event_room(self, subscription):
        if subscription.get('conversation_id'):
            room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?',
                                  (subscription['conversation_id'],))
            if room is None:
                raise DevError('ROOM_NOT_FOUND', '房间不存在', 404)
            return {**room, '_event_conversation': True}
        return self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',
                              (subscription['room_id'],))

    def current_scope(self, principal, args, *, stopping=False, create=True):
        filters = validate(FILTERS[args['name']], args['arguments'])
        if args['name'] == ROOM_INBOX_EVENT:
            principal, room = self.c.rooms.event_scope(principal, filters, stopping=stopping)
            return principal, {**room, '_event_conversation': True}
        return self.c.scope(principal, {
            'project': filters['project_id'], 'environment_id': filters['environment_id']},
            create=create and not stopping, notification=True)

    def live_scope(self, room):
        if room.get('_event_conversation'):
            current = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (room['id'],))
            if not current or current['state'] != 'active':
                raise DevError('ROOM_PAUSED', '房间已暂停或归档', 409)
            return current
        return self.c.live_room(room)

    def binding_guard(self, room, principal, filters, delivery=None):
        if room.get('_event_conversation'):
            return self.c.rooms.event_subscription_guard(principal, filters, delivery)
        return self.c.joining.subscription_guard(room, principal, filters, delivery)

    def event_audit(self, room, actor, action, target, details=None):
        if room.get('_event_conversation'):
            self.store.audit(actor, action, target, detail={
                'conversation_id': room['id'], **(details or {})})
        else:
            self.c.audit(room, actor, action, target, details)

    def prepare(self, raw, principal, *, stopping=False):
        self.guard()
        args = validate(Unsubscribe if stopping else Subscribe, raw)
        if args['delivery']['mode'] != 'webhook':
            raise DevError('UNSUPPORTED_DELIVERY_MODE', '此事件仅支持 webhook 投递', 400,
                           feature='deliveryMode', value=args['delivery']['mode'])
        if args['name'] not in FILTERS:
            raise DevError('EVENT_NOT_FOUND', '没有可订阅的此事件', 404)
        filters = validate(FILTERS[args['name']], args['arguments'])
        network.callback_url(args['delivery']['url'])
        if not stopping:
            signing_key(args['delivery']['secret'])
        principal, room = self.current_scope(principal, args, stopping=stopping)
        self.c.notification_reader(principal)
        if args['name'] == OPERATION_EVENT and not stopping:
            self.operations.authorize(principal, filters)
        if args['name'] in {MESSAGE_EVENT, MAILBOX_EVENT}:
            conversation = self.c.conversations.resolve(principal, room, filters)
            if not stopping:
                self.c.rooms.migration.legacy_write(conversation['id'])
        if args['name'] == MAILBOX_EVENT and not stopping:
            self.c.dot_mailbox.authorize_event(principal, filters)
        if args['name'] == DELEGATION_EVENT and not stopping:
            self.c.delegation.authorize_event(principal, filters)
        if args['name'] == WORK_EVENT and not stopping:
            self.c.coordination.authorize_event(principal, filters)
        if not stopping:
            self.binding_guard(room, principal, filters, args['delivery'])
        if args['name'] != ROOM_INBOX_EVENT and room['project_id'] != filters['project_id']:
            raise DevError('PROJECT_ID_REQUIRED', '事件过滤器需要不可变项目 ID，而非别名', 400)
        url = args['delivery']['url']
        network.callback_url(url)
        identity = digest([self.c.principal_snapshot(principal), url, args['name'], args['arguments']])
        if not stopping and args['cursor'] is not None:
            position = read_cursor(self.c.secret, identity, args['cursor'])
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise DevError('INVALID_CURSOR', '事件游标格式不正确', 400)
        existing = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identity,))
        if stopping:
            return args, principal, room, identity, existing, False
        self.live_scope(room)
        signing_key(args['delivery']['secret'])
        if existing and existing['state'] == 'paused':
            raise DevError('SUBSCRIPTION_PAUSED', '订阅已由面板暂停；自动续订不能恢复所有者暂停', 409)
        scope_column = self.scope_column(room)
        count = self.store.one(f"SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE {scope_column}=? AND state IN ('active','paused')", (room['id'],))['n']
        if not existing and count >= self.subscription_limit(room):
            raise DevError('SUBSCRIPTION_LIMIT', '试点协作室订阅数量已达上限', 409)
        key_digest = digest(args['delivery']['secret'])
        cached = False
        for candidate in self.store.all(f'''SELECT secret FROM mcp_event_subscriptions
            WHERE {scope_column}=? AND grant_id=? AND key_digest=? AND verified_until>? LIMIT 32''',
            (room['id'], principal.grant_id, key_digest, self.c.clock())):
            saved = json.loads(self.store.decrypt(candidate['secret']))
            if hmac.compare_digest(saved['url'].encode(), url.encode()):
                cached = True
                break
        if not cached:
            recent = (self.store.one("SELECT COUNT(*) AS n FROM audit WHERE actor=? AND action='subscription.challenge' AND at>?",
                                     (principal.actor, self.c.clock() - 60))['n']
                      if room.get('_event_conversation') else
                      self.store.one("SELECT COUNT(*) AS n FROM collaboration_audit WHERE room_id=? AND action='subscription.challenge' AND created>?",
                                     (room['id'], self.c.clock() - 60))['n'])
            if recent >= 6:
                raise DevError('CHALLENGE_RATE_LIMIT', '回调验证请求过于频繁，请稍后重试', 429)
            self.event_audit(room, principal.actor, 'subscription.challenge', identity)
        return args, principal, room, identity, existing, cached

    async def verify_callback(self, url, secret, identifier):
        verified_at = self.c.clock()
        challenge = secrets.token_urlsafe(32)
        body = canonical({'type': 'verification', 'challenge': challenge}).encode()
        headers = signed_headers('msg_verification_' + secrets.token_hex(16), identifier, body,
                                 [secret], verified_at)
        reply_status = None
        try:
            try:
                async with self._challenge_limit:
                    async with asyncio.timeout(12):
                        reply = await self.sender(url, body, headers)
                reply_status = reply.status
                if 400 <= reply.status < 500:
                    raise CallbackEndpointError('http_4xx', reply.status)
                if 500 <= reply.status < 600:
                    raise CallbackEndpointError('http_5xx', reply.status)
                answer = json.loads(reply.body)
                if (not 200 <= reply.status < 300 or not isinstance(answer, dict)
                        or not isinstance(answer.get('challenge'), str)
                        or not hmac.compare_digest(answer['challenge'].encode(), challenge.encode())
                        or self.c.clock() - verified_at > 30):
                    raise CallbackEndpointError('challenge_failed', reply.status)
            except (TimeoutError, ValueError, UnicodeError, httpx.HTTPError, OSError, DevError) as exc:
                raise CallbackEndpointError(callback_reason(exc), reply_status) from None
        except CallbackEndpointError as exc:
            # Fixed categories only: never log callback URLs, bodies or secrets.
            mark('event_callback_failed', callback_reason=exc.reason,
                 callback_http_status=exc.http_status)
            raise

    async def subscribe(self, raw, principal, *, unified=False):
        def begin():
            with self.store.transaction():
                prepared = self.prepare(raw, principal)
                return (*prepared, self.subscription_snapshot(prepared[1], prepared[0]))
        args, authenticated, room, identifier, previous, cached, authenticated_snapshot = await self.store.run(begin)
        if not cached:
            await self.verify_callback(args['delivery']['url'], args['delivery']['secret'], identifier)
        def commit():
            with self.store.transaction():
                # Current authorization and owner pause win over an in-flight challenge.
                current_principal, current_room = self.current_scope(principal, args, create=False)
                self.c.notification_reader(current_principal)
                self.live_scope(current_room)
                current_filters = validate(FILTERS[args['name']], args['arguments'])
                self.binding_guard(current_room, current_principal, current_filters, args['delivery'])
                if args['name'] in {MESSAGE_EVENT, MAILBOX_EVENT}:
                    conversation = self.c.conversations.resolve(current_principal, current_room, current_filters)
                    self.c.rooms.migration.legacy_write(conversation['id'])
                if args['name'] == MAILBOX_EVENT:
                    self.c.dot_mailbox.authorize_event(current_principal, current_filters)
                if args['name'] == DELEGATION_EVENT:
                    self.c.delegation.authorize_event(current_principal, current_filters)
                if args['name'] == WORK_EVENT:
                    self.c.coordination.authorize_event(current_principal, current_filters)
                current_snapshot = self.subscription_snapshot(current_principal, args)
                if current_snapshot != authenticated_snapshot:
                    raise DevError('SUBSCRIPTION_IDENTITY_CHANGED', '回调验证期间身份发生变化，请重新确认连接', 409)
                old = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identifier,))
                scope_column = self.scope_column(current_room)
                if not old and self.store.one(f"SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE {scope_column}=? AND state IN ('active','paused')", (room['id'],))['n'] >= self.subscription_limit(current_room):
                    raise DevError('SUBSCRIPTION_LIMIT', '试点协作室订阅数量已达上限', 409)
                if (old['version'] if old else 0) != (previous['version'] if previous else 0):
                    raise DevError('STALE_VERSION', '订阅在验证期间已改变，请重新读取', 409)
                now = self.c.clock()
                ttl = 86400 if args['ttlMs'] is None else max(60, min(86400000, args['ttlMs']) / 1000)
                lifetime = now + ttl
                stored = {'url': args['delivery']['url'], 'current': args['delivery']['secret']}
                if args['name'] == ROOM_INBOX_EVENT:
                    stored['room_ttl_seconds'] = ttl
                if old:
                    prior = json.loads(self.store.decrypt(old['secret']))
                    if prior.get('recovery_view') == 'unified':
                        stored['recovery_view'] = 'unified'
                    if prior['current'] != stored['current']:
                        stored.update(retiring=prior['current'], retire_at=now + 300)
                    elif prior.get('retire_at', 0) > now:
                        stored.update(retiring=prior['retiring'], retire_at=prior['retire_at'])
                elif unified and args['name'] == OPERATION_EVENT:
                    stored['recovery_view'] = 'unified'
                active = old and old['state'] == 'active' and old['expires_at'] > now
                same_room_connection = bool(args['name'] == ROOM_INBOX_EVENT and old
                    and old['state'] in {'active', 'expired'} and prior['current'] == stored['current']
                    and prior['url'] == stored['url'])
                position, truncated = (old['ack_seq'] if old else 0), False
                if not active:
                    if args['cursor'] is None and not same_room_connection:
                        # A new identity enrolls from now. Restoring the same
                        # room binding keeps its durable acknowledged position.
                        position = self.store.one(f'SELECT MAX(seq) AS n FROM mcp_event_outbox WHERE {scope_column}=?', (room['id'],))['n'] or 0
                    else:
                        if args['cursor'] is not None:
                            position = read_cursor(self.c.secret, identifier, args['cursor'])
                        # Explicit replay alone observes maxAgeMs. Keep the seven
                        # day server ceiling, announcing every skipped position.
                        age_ms = 7 * 86400000
                        if args['maxAgeMs'] is not None:
                            age_ms = min(age_ms, args['maxAgeMs'])
                        floor = self.store.one(f'SELECT MAX(seq) AS n FROM mcp_event_outbox WHERE {scope_column}=? AND created<?',
                                               (room['id'], now - age_ms / 1000))['n'] or 0
                        truncated = position < floor
                        position = max(position, floor)
                if old:
                    self.store.execute('''UPDATE mcp_event_subscriptions SET principal=?,secret=?,key_digest=?,
                        expires_at=?,verified_until=?,state='active',version=version+1,updated=? WHERE id=?''',
                        (canonical(current_snapshot), self.store.encrypt(canonical(stored)),
                         digest(stored['current']), lifetime, min(lifetime, now + 1800), now, identifier))
                    if not active:
                        self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,reason_code='subscription_replay_reset' WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (identifier,))
                        self.store.execute('UPDATE mcp_event_subscriptions SET scan_seq=?,ack_seq=? WHERE id=?', (position, position, identifier))
                        # Historical accepted rows may be replayed after expiry when
                        # a client deliberately supplies an older signed position.
                        self.event_audit(room, current_principal.actor, 'subscription.replay_reset', identifier,
                                     {'position': position, 'previous_ack': old['ack_seq']})
                        self.store.execute("UPDATE mcp_event_deliveries SET state='pending',body=NULL,attempt=0,next_at=0,lease_until=NULL,fence=fence+1,reason_code='explicit_replay' WHERE subscription_id=? AND event_seq>?", (identifier, position))
                else:
                    self.store.execute('''INSERT INTO mcp_event_subscriptions
                        (id,room_id,conversation_id,principal,grant_id,name,arguments,secret,expires_at,verified_until,key_digest,
                         scan_seq,ack_seq,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identifier, None if current_room.get('_event_conversation') else room['id'],
                         room['id'] if current_room.get('_event_conversation') else None,
                         canonical(current_snapshot), current_principal.grant_id,
                         args['name'], canonical(args['arguments']), self.store.encrypt(canonical(stored)), lifetime,
                         min(lifetime, now + 1800), digest(stored['current']), position, position, now, now))
                self.event_audit(room, current_principal.actor, 'subscription.saved', identifier, {'name': args['name']})
                saved = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identifier,))
                if args['name'] == ROOM_INBOX_EVENT:
                    self.c.rooms.event_subscription_attached(current_principal, current_filters, saved,
                        reset_verification=not same_room_connection, restored=not active and same_room_connection,
                        ttl=ttl)
                else:
                    self.c.joining.attach_subscription(current_room, current_principal,
                        validate(FILTERS[args['name']], args['arguments']), saved, reset_verification=not active)
                self.dispatch_route(saved, current_room)
                if args['name'] == OPERATION_EVENT:
                    self.operations.catch_up(saved, current_room, current_principal, current_filters)
                result = {'id': identifier, 'refreshBefore': timestamp(lifetime),
                          'cursor': sign_cursor(self.c.secret, identifier, saved['ack_seq']), 'truncated': truncated}
                if args['name'] == ROOM_INBOX_EVENT:
                    member = self.c.rooms.event_member(current_filters['member_id'],
                        current_filters['conversation_id'], current_filters['binding_generation'])
                    result['renewal'] = self.c.rooms.renewals.view(member)
                return result
        return await self.store.run(commit)

    def unsubscribe(self, raw, principal):
        with self.store.transaction():
            args, principal, room, identifier, existing, _ = self.prepare(raw, principal, stopping=True)
            if not existing:
                raise DevError('SUBSCRIPTION_NOT_FOUND', '没有匹配当前身份和过滤器的订阅', 404)
            if existing:
                self.store.execute("UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1,updated=? WHERE id=?", (self.c.clock(), identifier))
                self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,reason_code='unsubscribed' WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (identifier,))
                if existing['name'] == ROOM_INBOX_EVENT:
                    self.c.rooms.renewals.stop(identifier, 'unsubscribed')
                self.event_audit(room, principal.actor, 'subscription.removed', identifier)
            return {}

    async def call(self, method, raw, principal, *, unified=False):
        if method == 'events/subscribe':
            return await self.subscribe(raw, principal, unified=unified)
        if method == 'events/list':
            return await self.store.run(self.catalog, raw, principal, unified=unified)
        return await self.store.run(self.unsubscribe, raw, principal)

    def authorize(self, subscription, room, payload=None):
        if subscription['name'] == ROOM_INBOX_EVENT:
            filters = validate(FILTERS[ROOM_INBOX_EVENT], json.loads(subscription['arguments']))
            self.c.rooms.event_authorize(subscription, filters, payload)
            self.live_scope(room)
            return filters
        principal = self.c.grant_reader(room, subscription['grant_id'], snapshot=json.loads(subscription['principal']))
        self.live_scope(room)
        filters = validate(FILTERS[subscription['name']], json.loads(subscription['arguments']))
        self.c.joining.subscription_guard(room, principal, filters)
        if subscription['name'] == OPERATION_EVENT:
            self.operations.authorize(principal, filters, snapshot=json.loads(subscription['principal']), payload=payload)
        if subscription['name'] in {MESSAGE_EVENT, MAILBOX_EVENT}:
            conversation = self.c.conversations.resolve(principal, room, filters)
            self.c.rooms.migration.legacy_write(conversation['id'])
        if subscription['name'] == MAILBOX_EVENT:
            self.c.dot_mailbox.authorize_event(principal, filters, payload)
        if subscription['name'] == DELEGATION_EVENT:
            self.c.delegation.authorize_event(principal, filters, payload)
        if subscription['name'] == WORK_EVENT:
            self.c.coordination.authorize_event(principal, filters, payload)
        # Subscription consent binds notification filters, not a worker lease.
        # Matching still enforces the event's target grant and queue below.
        return filters

    @staticmethod
    def matches(subscription, filters, event):
        if event['name'] != subscription['name']:
            return False
        if event['target_grant_id'] and event['target_grant_id'] != subscription['grant_id']:
            return False
        payload = json.loads(event['data'])
        if event['name'] == MESSAGE_EVENT and (not event['target_grant_id'] or not filters.get('slot_id')
                or payload.get('recipient_slot_id') != filters['slot_id']
                or not payload.get('conversation_id') or not filters.get('conversation_id')
                or payload['conversation_id'] != filters['conversation_id']):
            return False
        if event['name'] == ROOM_INBOX_EVENT and (not event['target_grant_id']
                or any(payload.get(key) != filters.get(key) for key in
                       ('owner_user_id', 'conversation_id', 'member_id', 'binding_generation'))):
            return False
        if event['name'] == MAILBOX_EVENT and (not event['target_grant_id']
                or payload.get('recipient_grant_id') != subscription['grant_id']
                or payload.get('recipient_slot_id') != filters.get('slot_id')
                or any(payload.get(key) != filters.get(key)
                       for key in ('project_id', 'environment_id', 'conversation_id', 'binding_generation'))):
            return False
        if event['name'] == WORK_EVENT and (not event['target_grant_id']
                or payload.get('recipient_grant_id') != subscription['grant_id']
                or any(not filters.get(key) or payload.get(key) != filters[key]
                       for key in ('project_id', 'environment_id', 'conversation_id', 'goal_id', 'approval_id'))):
            return False
        if event['name'] == DELEGATION_EVENT and (not event['target_grant_id']
                or payload.get('recipient_grant_id') != subscription['grant_id']
                or payload.get('recipient_slot_id') != filters.get('slot_id')
                or any(payload.get(key) != filters.get(key)
                       for key in ('project_id', 'environment_id', 'conversation_id', 'policy_id', 'policy_version'))):
            return False
        if event['name'] == OPERATION_EVENT:
            from hub.operation_events import OperationEvents
            if (not event['target_grant_id'] or payload.get('recipient_grant_id') != subscription['grant_id']
                    or payload.get('operation_id') not in filters['operation_ids']
                    or any(payload.get(key) != filters[key] for key in ('project_id', 'environment_id'))
                    or (not payload.get('test') and event['object_id'] != OperationEvents.object_id(subscription['id'], payload))):
                return False
        if payload.get('test'):
            return payload.get('test_subscription_id') == subscription['id']
        if event['name'] == TASK_EVENT and filters['queue'] != event['queue']:
            return False
        if filters.get('severity_min') and SEVERITIES.get(payload.get('severity'), -1) < SEVERITIES[filters['severity_min']]:
            return False
        if filters.get('rule_ids') is not None and payload.get('rule_id') not in filters['rule_ids']:
            return False
        return True

    def mailbox_owns(self, event):
        """An active v2 route consumes new addressed inputs through its mailbox."""
        if event['name'] not in {MESSAGE_EVENT, DELEGATION_EVENT}:
            return False
        payload = json.loads(event['data'])
        mailbox = getattr(self.c, 'dot_mailbox', None)
        return bool(mailbox and not payload.get('test') and payload.get('recipient_slot_id')
                    and mailbox.enabled(payload['recipient_slot_id']))

    def advance(self, subscription_id):
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscription_id,))
        pending = self.store.one("SELECT MIN(event_seq) AS n FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (subscription_id,))['n']
        ack = min(sub['scan_seq'], pending - 1) if pending is not None else sub['scan_seq']
        self.store.execute('UPDATE mcp_event_subscriptions SET ack_seq=MAX(ack_seq,?) WHERE id=?', (ack, subscription_id))

    def dispatch_route(self, subscription, room=None):
        """Internal stable recipient key, not caller-supplied priority or authority."""
        existing = self.store.one('SELECT * FROM mcp_event_dispatch_routes WHERE subscription_id=?',
                                  (subscription['id'],))
        if existing:
            return existing
        room = room or self.event_room(subscription)
        destination = json.loads(self.store.decrypt(subscription['secret']))['url']
        network.callback_url(destination)
        owner_key = digest([room['space_id'], room['owner_user_id']])
        endpoint_key = digest([owner_key, str(httpx.URL(destination))])
        self.store.execute('INSERT OR IGNORE INTO mcp_event_dispatch_routes VALUES (?,?,?)',
                           (subscription['id'], endpoint_key, owner_key))
        return {'subscription_id': subscription['id'], 'endpoint_key': endpoint_key, 'owner_key': owner_key}

    @staticmethod
    def retry_delay(delivery, retry_after=None):
        # Stable 80-100% jitter spreads a recovered backlog without exceeding the
        # existing backoff ceilings. Retry-After can only delay, never accelerate.
        ceiling = (30, 120, 600, 1800)[min(delivery['attempt'] - 1, 3)]
        fraction = int(digest([delivery['id'], delivery['attempt']])[:8], 16) / 0xffffffff
        delay = ceiling * (.8 + .2 * fraction)
        if (isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
                and math.isfinite(retry_after) and retry_after >= 0):
            delay = max(delay, min(1800, retry_after))
        return delay

    def budget_accounts(self, owner_key, byte_count, now):
        """Return an atomic debit, or the bucket that prevents reservation."""
        self.store.require_transaction()
        accounts = []
        buckets = [('global', DISPATCH_BUDGETS['global'])]
        if owner_key is not None:
            buckets.append(('owner:' + owner_key, DISPATCH_BUDGETS['owner']))
        for key, limits in buckets:
            rate, burst, byte_rate, byte_burst = limits
            row = self.store.one('SELECT * FROM mcp_event_dispatch_budgets WHERE scope_key=?', (key,))
            elapsed = max(0, now - row['updated']) if row else 0
            tokens = min(burst, row['tokens'] + elapsed * rate) if row else burst
            byte_tokens = min(byte_burst, row['byte_tokens'] + elapsed * byte_rate) if row else byte_burst
            if tokens < 1 or byte_tokens < byte_count:
                delay = max(0, (1 - tokens) / rate, (byte_count - byte_tokens) / byte_rate)
                due = max(now, row['updated']) + delay if row else now + delay
                self._budget_retry_at = (due if self._budget_retry_at is None
                                         else min(self._budget_retry_at, due))
                return None, key
            accounts.append((key, tokens - 1, byte_tokens - byte_count,
                             max(now, row['updated']) if row else now))
        return accounts, None

    def send_budget(self, owner_key, byte_count, now, *, spend=False):
        """Atomically share global/owner send and byte buckets with all pumps."""
        accounts, _ = self.budget_accounts(owner_key, byte_count, now)
        if accounts is None:
            return False
        if spend:
            for account in accounts:
                self.store.execute("""INSERT INTO mcp_event_dispatch_budgets VALUES (?,?,?,?)
                    ON CONFLICT(scope_key) DO UPDATE SET tokens=excluded.tokens,
                    byte_tokens=excluded.byte_tokens,updated=excluded.updated""", account)
        return True

    def next_dispatch_delay(self, default=2):
        if self._budget_retry_at is None:
            return default
        return min(default, max(.01, self._budget_retry_at - self.c.clock()))

    def coalesce_mailbox(self, subscription, filters):
        if subscription['name'] not in {MAILBOX_EVENT, ROOM_INBOX_EVENT}:
            return
        # Only unsent, unsealed hints may be superseded. Every typed mailbox
        # entry/revision remains durable; latest wake reads the current ledger.
        member_key = 'member_id' if subscription['name'] == ROOM_INBOX_EVENT else 'recipient_slot_id'
        eligible = f"""FROM mcp_event_deliveries d JOIN mcp_event_outbox e ON e.id=d.event_id
            WHERE d.subscription_id=? AND d.state='pending' AND d.attempt=0 AND d.body IS NULL
            AND e.name=? AND COALESCE(json_extract(e.data,'$.test'),0)=0
            AND json_extract(e.data,'$.{member_key}')=?
            AND json_extract(e.data,'$.binding_generation')=?"""
        parameters = (subscription['id'], subscription['name'],
                      filters.get('member_id') or filters.get('slot_id'), filters['binding_generation'])
        latest = self.store.one('SELECT MAX(d.event_seq) AS n ' + eligible, parameters)['n']
        if latest is None:
            return
        old = self.store.all('SELECT d.id ' + eligible + ' AND d.event_seq<? ORDER BY d.event_seq LIMIT 100',
                             (*parameters, latest))
        if old:
            self.store.execute("""UPDATE mcp_event_deliveries SET state='abandoned',
                reason_code='mailbox_coalesced',fence=fence+1
                WHERE id IN (SELECT value FROM json_each(?))""", (canonical([row['id'] for row in old]),))

    def discover_deliveries(self, now):
        """Materialize one bounded outbox page, independently of send fairness.

        Advancing this scan says nothing about a recipient having been served.
        In particular, an empty global bucket must not discard that recipient's
        place in the durable owner/subscription service rings.
        """
        subscriptions = self.store.all("""SELECT * FROM mcp_event_subscriptions s
            WHERE s.state='active' AND s.id>? AND (s.expires_at<=?
                OR (s.room_id IS NOT NULL AND EXISTS (SELECT 1 FROM mcp_event_outbox e
                    WHERE e.room_id=s.room_id AND e.seq>s.scan_seq))
                OR (s.conversation_id IS NOT NULL AND EXISTS (SELECT 1 FROM mcp_event_outbox e
                    WHERE e.conversation_id=s.conversation_id AND e.seq>s.scan_seq)))
            ORDER BY s.id LIMIT 32""", (self._scan_after, now))
        self._scan_after = subscriptions[-1]['id'] if len(subscriptions) == 32 else ''
        for sub in subscriptions:
            if sub['expires_at'] <= now:
                self.store.execute("UPDATE mcp_event_subscriptions SET state='expired',version=version+1,updated=? WHERE id=?", (now, sub['id']))
                continue
            room = self.event_room(sub)
            try:
                filters = self.authorize(sub, room)
            except DevError as exc:
                if not authorization_transient(exc):
                    self.store.execute("UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=? WHERE id=?", (now, sub['id']))
                continue
            self.dispatch_route(sub, room)
            scope_column = self.scope_column(room)
            rows = self.store.all(f'SELECT * FROM mcp_event_outbox WHERE {scope_column}=? AND seq>? ORDER BY seq LIMIT 100', (room['id'], sub['scan_seq']))
            for event in rows:
                if self.matches(sub, filters, event) and not self.mailbox_owns(event):
                    self.store.execute('''INSERT OR IGNORE INTO mcp_event_deliveries
                        (id,subscription_id,event_id,event_seq,created) VALUES (?,?,?,?,?)''',
                        (digest([sub['id'], event['id']]), sub['id'], event['id'], event['seq'], now))
            if rows:
                self.store.execute('UPDATE mcp_event_subscriptions SET scan_seq=? WHERE id=?', (rows[-1]['seq'], sub['id']))
            self.coalesce_mailbox(sub, filters)
            self.advance(sub['id'])

    def dispatch_candidates(self, now, owner_limit):
        """Bounded ready candidates across both complete persistent rings.

        Eligibility excludes idle routes and blocked *heads*, never skips a
        retry/lease to send a later event. The SQL limits bound fetched owners
        and subscriptions; no active-subscription inventory is loaded here.
        Authorization, budgets and lease fencing are checked again below.
        """
        ready = """FROM mcp_event_dispatch_routes r
            JOIN mcp_event_subscriptions s ON s.id=r.subscription_id
            JOIN mcp_event_deliveries d ON d.id=(
                SELECT h.id FROM mcp_event_deliveries h
                WHERE h.subscription_id=s.id AND h.state IN ('pending','retry_wait','leased')
                ORDER BY h.event_seq LIMIT 1)
            LEFT JOIN collaboration_rooms lr ON lr.id=s.room_id
            LEFT JOIN conversation_rooms cr ON cr.id=s.conversation_id
            LEFT JOIN mcp_event_dispatch_endpoints ep ON ep.endpoint_key=r.endpoint_key
            WHERE s.state='active' AND s.expires_at>:now
                AND COALESCE(cr.state,lr.state)='active'
                AND d.next_at<=:now
                AND (d.state!='leased' OR COALESCE(d.lease_until,0)<=:now)
                AND COALESCE(ep.cooldown_until,0)<=:now
                AND NOT EXISTS (
                    SELECT 1 FROM mcp_event_dispatch_routes br
                    JOIN mcp_event_deliveries bd ON bd.subscription_id=br.subscription_id
                    WHERE br.endpoint_key=r.endpoint_key
                        AND bd.state='leased' AND bd.lease_until>:now)
                AND (SELECT COUNT(*) FROM mcp_event_dispatch_routes br
                    JOIN mcp_event_deliveries bd ON bd.subscription_id=br.subscription_id
                    WHERE br.owner_key=r.owner_key
                        AND bd.state='leased' AND bd.lease_until>:now)<:owner_limit"""
        parameters = {'now': now, 'owner_limit': owner_limit}
        cursor = self.store.one("SELECT * FROM mcp_event_dispatch_cursor WHERE id='global'")
        after = (cursor['scan_after_owner'] or cursor['after_owner']) if cursor else ''
        owners = []
        for comparison in ('>', '<='):
            owners += self.store.all('SELECT DISTINCT r.owner_key ' + ready
                + f' AND r.owner_key{comparison}:after ORDER BY r.owner_key LIMIT :limit',
                {**parameters, 'after': after, 'limit': 32 - len(owners)})
        groups = []
        for owner in owners:
            key = owner['owner_key']
            cursor = self.store.one('SELECT * FROM mcp_event_dispatch_owners WHERE owner_key=?', (key,))
            inspected = self.store.one('SELECT after_subscription FROM mcp_event_dispatch_owner_scans WHERE owner_key=?', (key,))
            after = inspected['after_subscription'] if inspected else cursor['after_subscription'] if cursor else ''
            members = []
            for comparison in ('>', '<='):
                members += self.store.all('SELECT s.*,r.owner_key,r.endpoint_key ' + ready
                    + f' AND r.owner_key=:owner AND s.id{comparison}:after ORDER BY s.id LIMIT :limit',
                    {**parameters, 'owner': key, 'after': after, 'limit': owner_limit - len(members)})
            groups.append(members)
        # Each owner gets a turn before any owner gets its second turn. Owners
        # and their subscriptions can live on arbitrary discovery SQL pages.
        return [members[index] for index in range(owner_limit)
                for members in groups if index < len(members)]

    def reserve(self, capacity=DISPATCH_CAPACITY):
        self.guard()
        now, reserved = self.c.clock(), []
        capacity = max(0, min(DISPATCH_CAPACITY, capacity))
        if not capacity:
            return reserved
        with self.store.transaction():
            # Include leases created before this migration and other dispatchers.
            leased = self.store.all("""SELECT s.* FROM mcp_event_subscriptions s
                JOIN mcp_event_deliveries d ON d.subscription_id=s.id
                WHERE d.state='leased' AND d.lease_until>?""", (now,))
            for subscription in leased:
                self.dispatch_route(subscription)
            capacity = min(capacity, max(0, DISPATCH_CAPACITY - len(leased)))
            if not capacity:
                return reserved
            self.discover_deliveries(now)
            # A missing global send token (or even the minimum wire overhead)
            # rules out every delivery. Do not scan, skip or mutate either
            # service/inspection ring while waiting for this shared refill.
            accounts, _ = self.budget_accounts(None, WIRE_HEADER_BYTES, now)
            if accounts is None:
                return reserved
            owner_count = self.store.one("""SELECT COUNT(*) AS n FROM (
                SELECT COALESCE(r.space_id,c.space_id),COALESCE(r.owner_user_id,c.owner_user_id)
                FROM mcp_event_subscriptions s
                LEFT JOIN collaboration_rooms r ON s.room_id=r.id
                LEFT JOIN conversation_rooms c ON s.conversation_id=c.id
                WHERE s.state='active' AND s.expires_at>? GROUP BY 1,2)""", (now,))['n']
            owner_limit = 2 if owner_count > 1 else DISPATCH_CAPACITY
            candidates = self.dispatch_candidates(now, owner_limit)
            inspected, served, budget_waiting, global_blocked = {}, set(), set(), False
            for sub in candidates:
                # Until a send or budget gate below, this records only a head
                # proved unavailable. It is a separate inspection continuation.
                inspected[sub['owner_key']] = sub['id']
                room = self.event_room(sub)
                try:
                    self.authorize(sub, room)
                except DevError as exc:
                    if authorization_transient(exc):
                        continue
                    self.store.execute("UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=? WHERE id=?", (now, sub['id']))
                    continue
                delivery = self.store.one("SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','retry_wait','leased') ORDER BY event_seq LIMIT 1", (sub['id'],))
                if not delivery or delivery['next_at'] > now or (delivery['state'] == 'leased' and (delivery['lease_until'] or 0) > now):
                    continue
                route = sub
                cooldown = self.store.one('SELECT cooldown_until FROM mcp_event_dispatch_endpoints WHERE endpoint_key=?',
                                          (route['endpoint_key'],))
                if cooldown and cooldown['cooldown_until'] > now:
                    continue
                busy = self.store.one("""SELECT COUNT(*) AS n FROM mcp_event_deliveries d
                    JOIN mcp_event_dispatch_routes r ON r.subscription_id=d.subscription_id
                    WHERE d.state='leased' AND d.lease_until>? AND r.endpoint_key=?""",
                    (now, route['endpoint_key']))['n']
                occupied = self.store.one("""SELECT COUNT(*) AS n FROM mcp_event_deliveries d
                    JOIN mcp_event_dispatch_routes r ON r.subscription_id=d.subscription_id
                    WHERE d.state='leased' AND d.lease_until>? AND r.owner_key=?""",
                    (now, route['owner_key']))['n']
                if busy or occupied >= owner_limit:
                    continue
                event = self.store.one('SELECT * FROM mcp_event_outbox WHERE id=?', (delivery['event_id'],))
                payload = validate(PAYLOADS[event['name']], json.loads(event['data']))
                if event['name'] in {WORK_EVENT, DELEGATION_EVENT, OPERATION_EVENT, MAILBOX_EVENT, ROOM_INBOX_EVENT}:
                    try:
                        self.authorize(sub, room, payload)
                    except DevError:
                        self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',reason_code='obsolete_work' WHERE id=?", (delivery['id'],))
                        self.advance(sub['id'])
                        continue
                if event['name'] == TASK_EVENT and not payload.get('test'):
                    if not self.c.config.analysis_dispatch_enabled:
                        continue
                    job = self.store.one('SELECT * FROM collaboration_jobs WHERE id=? AND room_id=?', (event['object_id'], room['id']))
                    if not job or job['state'] != 'queued' or job['version'] != event['object_version'] or job['deadline_at'] <= now:
                        self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',reason_code='obsolete_task' WHERE id=?", (delivery['id'],))
                        self.advance(sub['id'])
                        continue
                stored_body = delivery['body']
                body = ((self.store.decrypt(stored_body[len(SEALED_BODY_PREFIX):])
                         if stored_body.startswith(SEALED_BODY_PREFIX) else stored_body)
                        if stored_body else None)
                # Reserve headroom before issuing a one-time mailbox ticket.
                # Actual bytes alone are charged after the body is frozen.
                headroom = (len(body.encode()) if body is not None else MAX_ENVELOPE_BYTES) + WIRE_HEADER_BYTES
                accounts, blocked_by = self.budget_accounts(route['owner_key'], headroom, now)
                if accounts is None:
                    # A budget wait must not skip an otherwise valid member in
                    # its owner's ring. Other genuinely unavailable heads can
                    # still be passed on the next bounded inspection page.
                    inspected.pop(sub['owner_key'], None)
                    budget_waiting.add(sub['owner_key'])
                    if blocked_by == 'global':
                        # Nothing can be sent until refill. Keep both service
                        # cursors at the last actual lease, even across pumps.
                        global_blocked = True
                        break
                    continue
                if body is None:
                    if event['name'] == MAILBOX_EVENT:
                        payload = validate(PAYLOADS[event['name']],
                                           self.c.dot_mailbox.delivery_payload(sub, payload, event['id']))
                    elif event['name'] == ROOM_INBOX_EVENT:
                        payload = validate(PAYLOADS[event['name']],
                                           self.c.room_consumers.delivery_payload(sub, payload, event['id']))
                    elif event['name'] == OPERATION_EVENT and payload.get('next_call'):
                        stored = json.loads(self.store.decrypt(sub['secret']))
                        if stored.get('recovery_view') == 'unified':
                            from shared.unified_contracts import recovery_call
                            payload['next_call'] = recovery_call(payload['operation_id'], project=payload['project_id'],
                                                                 workspace_id=payload['workspace_id'])
                            payload = validate(UnifiedOperationPayload, payload)
                            # Preserve the omission of the optional workspace ID
                            # in the call for the main project checkout.
                            if not payload['workspace_id']:
                                payload['next_call']['arguments'].pop('workspace_id', None)
                    body = canonical({'eventId': event['id'], 'name': event['name'],
                        'timestamp': timestamp(event['created']), 'data': payload,
                        'cursor': sign_cursor(self.c.secret, sub['id'], event['seq'])})
                    stored_body = (SEALED_BODY_PREFIX + self.store.encrypt(body)
                                   if event['name'] in {MAILBOX_EVENT, ROOM_INBOX_EVENT} else body)
                if len(body.encode()) > MAX_ENVELOPE_BYTES:
                    self.store.execute("UPDATE mcp_event_deliveries SET state='dead_letter',reason_code='payload_too_large' WHERE id=?", (delivery['id'],))
                    self.advance(sub['id'])
                    continue
                if delivery['attempt'] >= 5:
                    self.store.execute("UPDATE mcp_event_deliveries SET state='dead_letter',lease_until=NULL,reason_code='attempts_exhausted' WHERE id=?", (delivery['id'],))
                    self.advance(sub['id'])
                    continue
                if not self.send_budget(route['owner_key'], len(body.encode()) + WIRE_HEADER_BYTES, now, spend=True):
                    raise RuntimeError('Dispatcher budget changed within its reservation transaction')
                fence = delivery['fence'] + 1
                self.store.execute("UPDATE mcp_event_deliveries SET state='leased',body=?,attempt=attempt+1,fence=?,lease_until=? WHERE id=?", (stored_body, fence, now + 30, delivery['id']))
                reserved.append({'delivery_id': delivery['id'], 'subscription_id': sub['id'], 'fence': fence,
                                 'subscription_version': sub['version'], 'body': body.encode(), 'event_id': event['id']})
                self.store.execute("""INSERT INTO mcp_event_dispatch_cursor (id,after_owner,updated)
                    VALUES ('global',?,?)
                    ON CONFLICT(id) DO UPDATE SET after_owner=excluded.after_owner,
                    updated=excluded.updated,scan_after_owner=NULL""", (route['owner_key'], now))
                self.store.execute("""INSERT INTO mcp_event_dispatch_owners (owner_key,after_subscription,updated)
                    VALUES (?,?,?)
                    ON CONFLICT(owner_key) DO UPDATE SET after_subscription=excluded.after_subscription,
                    updated=excluded.updated""", (route['owner_key'], sub['id'], now))
                self.store.execute('DELETE FROM mcp_event_dispatch_owner_scans WHERE owner_key=?', (route['owner_key'],))
                served.add(route['owner_key'])
                if len(reserved) >= capacity:
                    break
            for owner_key, subscription_id in inspected.items():
                if owner_key not in served | budget_waiting:
                    self.store.execute("""INSERT INTO mcp_event_dispatch_owner_scans VALUES (?,?,?)
                        ON CONFLICT(owner_key) DO UPDATE SET after_subscription=excluded.after_subscription,
                        updated=excluded.updated""", (owner_key, subscription_id, now))
            if candidates and not reserved and not global_blocked:
                self.store.execute("""INSERT INTO mcp_event_dispatch_cursor
                    (id,after_owner,updated,scan_after_owner) VALUES ('global','',?,?)
                    ON CONFLICT(id) DO UPDATE SET scan_after_owner=excluded.scan_after_owner""",
                    (now, candidates[-1]['owner_key']))
        return reserved

    def ready(self, item):
        with self.store.transaction(immediate=False):
            sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (item['subscription_id'],))
            delivery = self.store.one('SELECT * FROM mcp_event_deliveries WHERE id=?', (item['delivery_id'],))
            if (not sub or sub['state'] != 'active' or sub['version'] != item['subscription_version']
                    or sub['expires_at'] <= self.c.clock() or not delivery or delivery['fence'] != item['fence']
                    or delivery['state'] != 'leased' or (delivery['lease_until'] or 0) <= self.c.clock()):
                return None
            room = self.event_room(sub)
            event = self.store.one('SELECT * FROM mcp_event_outbox WHERE id=?', (delivery['event_id'],))
            self.authorize(sub, room, json.loads(event['data']) if event['name'] in {WORK_EVENT, DELEGATION_EVENT, OPERATION_EVENT, MAILBOX_EVENT, ROOM_INBOX_EVENT} else None)
            if event['name'] in {MAILBOX_EVENT, ROOM_INBOX_EVENT} and not json.loads(event['data']).get('test'):
                ticket = self.store.one('SELECT expires_at FROM dot_wake_tickets WHERE delivery_key=? AND subscription_id=?',
                                        (event['id'], sub['id']))
                if not ticket or ticket['expires_at'] <= self.c.clock():
                    raise DevError('EVENT_WAKE_TICKET_EXPIRED', '唤醒票据已过期，等待原邮箱恢复生成新提示', 409)
            if event['name'] == TASK_EVENT and not json.loads(event['data']).get('test'):
                job = self.store.one('SELECT * FROM collaboration_jobs WHERE id=? AND room_id=?', (event['object_id'], room['id']))
                if (not self.c.config.analysis_dispatch_enabled or not job or job['state'] != 'queued'
                        or job['version'] != event['object_version'] or job['deadline_at'] <= self.c.clock()):
                    return None
                self.c.check_plan(room, job)
            stored = json.loads(self.store.decrypt(sub['secret']))
            keys = [stored['current']]
            if stored.get('retire_at', 0) > self.c.clock():
                keys.append(stored['retiring'])
            return stored['url'], signed_headers(item['event_id'], sub['id'], item['body'], keys, self.c.clock())

    def finish(self, item, status=None, reason='', retry_after=None):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM mcp_event_deliveries WHERE id=?', (item['delivery_id'],))
            if not row or row['state'] != 'leased' or row['fence'] != item['fence']:
                return
            now = self.c.clock()
            sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (row['subscription_id'],))
            if sub['version'] != item['subscription_version']:
                # Preserve the old in-flight receipt as audit, not as ACK for a new
                # generation. Its lease will expire and replay with a current fence.
                room = self.event_room(sub)
                self.event_audit(room, 'events:dispatcher', 'delivery.late_receipt', row['id'], {'status': status})
                return
            accepted = status is not None and 200 <= status < 300
            retryable = reason == 'network_error' or status in {408, 425, 429} or (status is not None and status >= 500)
            if accepted:
                state, next_at, reason = 'accepted', 0, ''
            elif reason == 'authorization_stopped' and sub['expires_at'] > now:
                # No send occurred. Keep the original event pending rather than
                # consuming delivery attempts or advancing its consumer cursor.
                state, next_at = 'retry_wait', now + 30
                self.store.execute('UPDATE mcp_event_deliveries SET attempt=MAX(0,attempt-1) WHERE id=?', (row['id'],))
            elif sub['state'] != 'active' or sub['expires_at'] <= now or reason == 'authorization_revoked':
                state, next_at = 'abandoned', 0
            elif retryable and row['attempt'] < 5:
                state, next_at = 'retry_wait', now + self.retry_delay(row, retry_after)
            else:
                state, next_at = 'dead_letter', 0
            self.store.execute('''UPDATE mcp_event_deliveries SET state=?,next_at=?,lease_until=NULL,
                status_code=?,reason_code=?,accepted_at=? WHERE id=?''',
                (state, next_at, status, reason or ('http_' + str(status) if not accepted else ''), now if accepted else None, row['id']))
            if retryable and sub['state'] == 'active' and sub['expires_at'] > now:
                route = self.dispatch_route(sub)
                self.store.execute("""INSERT INTO mcp_event_dispatch_endpoints VALUES (?,?,?)
                    ON CONFLICT(endpoint_key) DO UPDATE SET
                    cooldown_until=MAX(cooldown_until,excluded.cooldown_until),updated=excluded.updated""",
                    (route['endpoint_key'], now + self.retry_delay(row, retry_after), now))
            if accepted:
                self.store.execute('UPDATE mcp_event_subscriptions SET last_accepted=? WHERE id=?', (now, sub['id']))
            self.advance(sub['id'])
            hints = getattr(self.c, 'relay_hints', None)
            if hints is not None:
                self.store.after_commit(hints.wake)

    async def deliver(self, item):
        try:
            async with self._network_limit:
                outbound = await self.store.run(self.ready, item)
                if outbound is None:
                    await self.store.run(self.finish, item, None, 'authorization_stopped')
                    return
                url, headers = outbound
                # Also bound injected/adapted transports; the native transport has
                # its own stricter 10-second DNS/connect/body deadline.
                async with asyncio.timeout(12):
                    reply = await self.sender(url, item['body'], headers)
            await self.store.run(self.finish, item, reply.status, '', getattr(reply, 'retry_after_seconds', None))
        except DevError as exc:
            policy = exc.code in {'CALLBACK_URL_REJECTED', 'CALLBACK_ADDRESS_REJECTED', 'HTTP_REDIRECT_REJECTED', 'HTTP_RESPONSE_TOO_LARGE', 'EVENT_TOO_LARGE'}
            transient = authorization_transient(exc)
            reason = ('wake_ticket_expired' if exc.code == 'EVENT_WAKE_TICKET_EXPIRED'
                      else 'callback_policy_rejected' if policy
                      else 'authorization_stopped' if transient else 'authorization_revoked')
            await self.store.run(self.finish, item, None, reason)
        except (TimeoutError, httpx.HTTPError, OSError):
            await self.store.run(self.finish, item, None, 'network_error')
        except Exception as exc:
            logger.warning('Event delivery failed: %s', type(exc).__name__)
            await self.store.run(self.finish, item, None, 'delivery_internal_error')

    async def tick(self, *, wake=None):
        """One owned pump, bounded children, replenished on each completion/hint.

        A slow callback never holds the entire batch hostage. Leases remain in
        SQLite on cancellation: an ambiguous HTTP outcome must be fenced and
        retried with the same event body after its original lease expires.
        """
        async with self._tick_lock:
            self._budget_retry_at = None
            pending = set()
            waiter = None
            empty_pages = 0
            try:
                while True:
                    # Clear before scanning; a commit during Store.run remains
                    # visible and is consumed by the following wait.
                    if wake is not None:
                        wake.clear()
                    if self.c.clock() >= self._relay_recover_after:
                        self._relay_recover_after = self.c.clock() + 5
                        await self.store.run(self.c.rooms.recover_paused_work)
                        recovery = getattr(getattr(self.c, 'room_goal_recovery', None), 'recover_continuations', None)
                        if recovery is not None:
                            await self.store.run(lambda: recovery(limit=8))
                        chat_wakes = await self.store.run(self.c.dot_relay.recover)
                        mailbox = getattr(self.c, 'dot_mailbox', None)
                        mailbox_wakes = (await self.store.run(lambda: mailbox.recover(max_wakes=max(0, 4 - chat_wakes)))
                                         if mailbox is not None else 0)
                        room_consumers = getattr(self.c, 'room_consumers', None)
                        room_wakes = (await self.store.run(lambda: room_consumers.recover(
                            max_wakes=max(0, 4 - chat_wakes - mailbox_wakes))) if room_consumers is not None else 0)
                        await self.store.run(lambda: self.c.task_liveness.recover(
                            max_wakes=max(0, 4 - chat_wakes - mailbox_wakes - room_wakes)))
                    items = await self.store.run(self.reserve, DISPATCH_CAPACITY - len(pending))
                    pending.update(asyncio.create_task(self.deliver(item), name='collaboration:event-delivery')
                                   for item in items)
                    if not pending:
                        # Bound scans even when a room has many idle subscribers.
                        empty_pages += 1
                        if self._scan_after and empty_pages < 8:
                            continue
                        return
                    empty_pages = 0
                    waiting = set(pending)
                    if wake is not None:
                        waiter = asyncio.create_task(wake.wait(), name='collaboration:event-wake')
                        waiting.add(waiter)
                    done, _ = await asyncio.wait(waiting, timeout=.25, return_when=asyncio.FIRST_COMPLETED)
                    if waiter is not None:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                        waiter = None
                    completed = done & pending
                    pending.difference_update(completed)
                    for task in completed:
                        if not task.cancelled() and task.exception() is not None:
                            logger.warning("Event delivery task failed: %s", type(task.exception()).__name__)
            finally:
                if waiter is not None:
                    pending.add(waiter)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    def queue_test(self, room, subscription):
        self.guard()
        if subscription['name'] == ROOM_INBOX_EVENT:
            return self.c.rooms.event_test(subscription)
        if subscription['state'] != 'active' or subscription['expires_at'] <= self.c.clock():
            raise DevError('SUBSCRIPTION_INACTIVE', '订阅当前不可投递', 409)
        filters = self.authorize(subscription, room)
        identifier = 'test_' + secrets.token_hex(16)
        base = {'test': True, 'test_subscription_id': subscription['id']}
        if subscription['name'] == MAILBOX_EVENT:
            base.update(schema_version=2, conversation_id=filters['conversation_id'], room_id=room['id'],
                        recipient_slot_id=filters['slot_id'], recipient_grant_id=subscription['grant_id'],
                        binding_generation=filters['binding_generation'], dirty_revision=0)
        elif subscription['name'] == OPERATION_EVENT:
            principal = self.c.grant_reader(room, subscription['grant_id'], snapshot=json.loads(subscription['principal']))
            operation = self.operations.read(filters['operation_ids'][0], principal, filters)
            base.update(operation_id=operation['id'], state='succeeded', output_seq=operation['output_seq'],
                        workspace_id=operation['workspace_id'], recipient_grant_id=subscription['grant_id'], next_call=None)
        elif subscription['name'] == TASK_EVENT:
            agent = self.store.one('SELECT id,kind FROM collaboration_agents WHERE room_id=? AND grant_id=? AND queue=? AND enabled=1 AND expires_at>?', (room['id'], subscription['grant_id'], filters['queue'], self.c.clock()))
            base.update(job_id=identifier, job_version=1, queue=filters['queue'], assignee_agent_id=agent['id'] if agent else identifier,
                        kind='analyze_incident' if filters['queue'] == 'work-analysis' else 'summarize_result', reason_code='subscription_test')
        elif subscription['name'] == DELEGATION_EVENT:
            base.update(conversation_id=filters['conversation_id'], policy_id=filters['policy_id'],
                        policy_version=filters['policy_version'], delegation_id=identifier,
                        message_id=identifier, message_version=1, goal_id=identifier, approval_id=identifier,
                        work_item_id=identifier, work_item_version=1, recipient_slot_id=filters['slot_id'],
                        recipient_grant_id=subscription['grant_id'])
        elif subscription['name'] == WORK_EVENT:
            base.update(conversation_id=filters['conversation_id'], goal_id=filters['goal_id'],
                        approval_id=filters['approval_id'], work_item_id=identifier, work_item_version=1,
                        target_project_id=room['project_id'], recipient_grant_id=subscription['grant_id'],
                        reason='work_available', message_id=None)
        elif subscription['name'] == MESSAGE_EVENT:
            base.update(conversation_id=filters.get('conversation_id') or room['id'], room_id=room['id'], message_id=identifier, message_version=1,
                        recipient_slot_id=filters['slot_id'], thread_root_id=identifier)
        elif subscription['name'] == RESULT_EVENT:
            base.update(result_id=identifier, job_id=identifier, job_version=1, incident_id=None, incident_version=None,
                        outcome='inconclusive', requires_decision=False, evidence_refs=[])
        elif subscription['name'] == INCIDENT_EVENT:
            base.update(incident_id=identifier, transition='test', severity='low', state='test', version=1,
                        episode=1, rule_id=identifier, evidence_ref=identifier)
        else:
            base.update(component='subscription', status='test', reason_code='subscription_test',
                        observed_at=timestamp(self.c.clock()), recovery_state='not_applicable')
        self.c.emit(room, subscription['name'], identifier, 1, base, grant_id=subscription['grant_id'], queue=filters.get('queue', ''))
        event = self.store.one('SELECT id FROM mcp_event_outbox WHERE object_id=? AND name=?', (identifier, subscription['name']))
        return {'id': subscription['id'], 'test_event_id': event['id'], 'state': 'test_queued', 'chat_response_verified': False}
