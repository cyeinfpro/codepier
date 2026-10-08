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
import secrets
import time

import httpx
from hub.principal import refresh_principal
from hub.mcp_request_audit import mark
from hub.collaboration.event_errors import CallbackEndpointError, callback_reason
from hub.collaboration import network
from hub.collaboration.common import (TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT, MESSAGE_EVENT, WORK_EVENT, DELEGATION_EVENT,
    SEVERITIES, canonical, digest, read_cursor, sign_cursor, timestamp, validate)
from hub.collaboration.event_contracts import FILTERS, PAYLOADS, Subscribe, Unsubscribe, definitions
from shared.util import DevError



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
        self._network_limit = asyncio.Semaphore(2)
        self._scan_after = ''

    def guard(self):
        self.c.guard()
        if not self.c.config.events_enabled:
            raise DevError('EVENTS_DISABLED', 'MCP Events 未启用', 409)

    def catalog(self, raw, principal):
        self.guard()
        if set(raw) - {'cursor'} or raw.get('cursor') not in (None, ''):
            raise DevError('INVALID_CURSOR', '事件目录只有一页', 400)
        principal = refresh_principal(self.store, principal)
        if not principal.grant_id or 'read' not in principal.scopes:
            return {'events': []}
        return {'events': definitions() if self.c.runtime.list_projects(principal) else []}

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
        principal, room = self.c.scope(principal, {'project': filters['project_id'], 'environment_id': filters['environment_id']},
                                       create=not stopping, notification=True)
        self.c.notification_reader(principal)
        if args['name'] == MESSAGE_EVENT:
            self.c.conversations.resolve(principal, room, filters)
        if args['name'] == DELEGATION_EVENT and not stopping:
            self.c.delegation.authorize_event(principal, filters)
        if args['name'] == WORK_EVENT and not stopping:
            self.c.coordination.authorize_event(principal, filters)
        if not stopping:
            self.c.joining.subscription_guard(room, principal, filters, args['delivery'])
        if room['project_id'] != filters['project_id']:
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
        self.c.live_room(room)
        signing_key(args['delivery']['secret'])
        if existing and existing['state'] == 'paused':
            raise DevError('SUBSCRIPTION_PAUSED', '订阅已由面板暂停；自动续订不能恢复所有者暂停', 409)
        count = self.store.one("SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE room_id=? AND state IN ('active','paused')", (room['id'],))['n']
        if not existing and count >= 32:
            raise DevError('SUBSCRIPTION_LIMIT', '试点协作室订阅数量已达上限', 409)
        key_digest = digest(args['delivery']['secret'])
        cached = False
        for candidate in self.store.all('''SELECT secret FROM mcp_event_subscriptions
            WHERE room_id=? AND grant_id=? AND key_digest=? AND verified_until>? LIMIT 32''',
            (room['id'], principal.grant_id, key_digest, self.c.clock())):
            saved = json.loads(self.store.decrypt(candidate['secret']))
            if hmac.compare_digest(saved['url'].encode(), url.encode()):
                cached = True
                break
        if not cached:
            recent = self.store.one("SELECT COUNT(*) AS n FROM collaboration_audit WHERE room_id=? AND action='subscription.challenge' AND created>?", (room['id'], self.c.clock() - 60))['n']
            if recent >= 6:
                raise DevError('CHALLENGE_RATE_LIMIT', '回调验证请求过于频繁，请稍后重试', 429)
            self.c.audit(room, principal.actor, 'subscription.challenge', identity)
        return args, principal, room, identity, existing, cached

    async def subscribe(self, raw, principal):
        def begin():
            with self.store.transaction():
                return self.prepare(raw, principal)
        args, authenticated, room, identifier, previous, cached = await self.store.run(begin)
        verified_at = self.c.clock()
        if not cached:
            challenge = secrets.token_urlsafe(32)
            body = canonical({'type': 'verification', 'challenge': challenge}).encode()
            headers = signed_headers('msg_verification_' + secrets.token_hex(16), identifier, body,
                                     [args['delivery']['secret']], verified_at)
            reply_status = None
            try:
                try:
                    async with self._network_limit:
                        reply = await self.sender(args['delivery']['url'], body, headers)
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
        def commit():
            with self.store.transaction():
                # Current authorization and owner pause win over an in-flight challenge.
                current_principal, current_room = self.c.scope(principal, {'project': room['project_id'], 'environment_id': room['environment_id']})
                self.c.notification_reader(current_principal)
                self.c.live_room(current_room)
                current_filters = validate(FILTERS[args['name']], args['arguments'])
                self.c.joining.subscription_guard(current_room, current_principal, current_filters, args['delivery'])
                if args['name'] == MESSAGE_EVENT:
                    self.c.conversations.resolve(current_principal, current_room, current_filters)
                if args['name'] == DELEGATION_EVENT:
                    self.c.delegation.authorize_event(current_principal, current_filters)
                if args['name'] == WORK_EVENT:
                    self.c.coordination.authorize_event(current_principal, current_filters)
                if self.c.principal_snapshot(current_principal) != self.c.principal_snapshot(authenticated):
                    raise DevError('SUBSCRIPTION_IDENTITY_CHANGED', '回调验证期间身份发生变化，请重新确认连接', 409)
                old = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identifier,))
                if not old and self.store.one("SELECT COUNT(*) AS n FROM mcp_event_subscriptions WHERE room_id=? AND state IN ('active','paused')", (room['id'],))['n'] >= 32:
                    raise DevError('SUBSCRIPTION_LIMIT', '试点协作室订阅数量已达上限', 409)
                if (old['version'] if old else 0) != (previous['version'] if previous else 0):
                    raise DevError('STALE_VERSION', '订阅在验证期间已改变，请重新读取', 409)
                now = self.c.clock()
                ttl = 86400 if args['ttlMs'] is None else max(60, min(86400000, args['ttlMs']) / 1000)
                lifetime = now + ttl
                stored = {'url': args['delivery']['url'], 'current': args['delivery']['secret']}
                if old:
                    prior = json.loads(self.store.decrypt(old['secret']))
                    if prior['current'] != stored['current']:
                        stored.update(retiring=prior['current'], retire_at=now + 300)
                    elif prior.get('retire_at', 0) > now:
                        stored.update(retiring=prior['retiring'], retire_at=prior['retire_at'])
                active = old and old['state'] == 'active' and old['expires_at'] > now
                position, truncated = (old['ack_seq'] if old else 0), False
                if not active:
                    if args['cursor'] is None:
                        # Omitted/null means now, including expired/unsubscribed
                        # identities. Never replay history merely on enrollment.
                        position = self.store.one('SELECT MAX(seq) AS n FROM mcp_event_outbox WHERE room_id=?', (room['id'],))['n'] or 0
                    else:
                        position = read_cursor(self.c.secret, identifier, args['cursor'])
                        # Explicit replay alone observes maxAgeMs. Keep the seven
                        # day server ceiling, announcing every skipped position.
                        age_ms = 7 * 86400000
                        if args['maxAgeMs'] is not None:
                            age_ms = min(age_ms, args['maxAgeMs'])
                        floor = self.store.one('SELECT MAX(seq) AS n FROM mcp_event_outbox WHERE room_id=? AND created<?',
                                               (room['id'], now - age_ms / 1000))['n'] or 0
                        truncated = position < floor
                        position = max(position, floor)
                if old:
                    self.store.execute('''UPDATE mcp_event_subscriptions SET principal=?,secret=?,key_digest=?,
                        expires_at=?,verified_until=?,state='active',version=version+1,updated=? WHERE id=?''',
                        (canonical(self.c.principal_snapshot(current_principal)), self.store.encrypt(canonical(stored)),
                         digest(stored['current']), lifetime, min(lifetime, now + 1800), now, identifier))
                    if not active:
                        self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,reason_code='subscription_replay_reset' WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (identifier,))
                        self.store.execute('UPDATE mcp_event_subscriptions SET scan_seq=?,ack_seq=? WHERE id=?', (position, position, identifier))
                        # Historical accepted rows may be replayed after expiry when
                        # a client deliberately supplies an older signed position.
                        self.c.audit(room, current_principal.actor, 'subscription.replay_reset', identifier,
                                     {'position': position, 'previous_ack': old['ack_seq']})
                        self.store.execute("UPDATE mcp_event_deliveries SET state='pending',body=NULL,attempt=0,next_at=0,lease_until=NULL,fence=fence+1,reason_code='explicit_replay' WHERE subscription_id=? AND event_seq>?", (identifier, position))
                else:
                    self.store.execute('''INSERT INTO mcp_event_subscriptions
                        (id,room_id,principal,grant_id,name,arguments,secret,expires_at,verified_until,key_digest,
                         scan_seq,ack_seq,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identifier, room['id'], canonical(self.c.principal_snapshot(current_principal)), current_principal.grant_id,
                         args['name'], canonical(args['arguments']), self.store.encrypt(canonical(stored)), lifetime,
                         min(lifetime, now + 1800), digest(stored['current']), position, position, now, now))
                self.c.audit(room, current_principal.actor, 'subscription.saved', identifier, {'name': args['name']})
                saved = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (identifier,))
                self.c.joining.attach_subscription(current_room, current_principal,
                    validate(FILTERS[args['name']], args['arguments']), saved, reset_verification=not active)
                return {'id': identifier, 'refreshBefore': timestamp(lifetime),
                        'cursor': sign_cursor(self.c.secret, identifier, saved['ack_seq']), 'truncated': truncated}
        return await self.store.run(commit)

    def unsubscribe(self, raw, principal):
        with self.store.transaction():
            args, principal, room, identifier, existing, _ = self.prepare(raw, principal, stopping=True)
            if not existing:
                raise DevError('SUBSCRIPTION_NOT_FOUND', '没有匹配当前身份和过滤器的订阅', 404)
            if existing:
                self.store.execute("UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1,updated=? WHERE id=?", (self.c.clock(), identifier))
                self.store.execute("UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,reason_code='unsubscribed' WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (identifier,))
                self.c.audit(room, principal.actor, 'subscription.removed', identifier)
            return {}

    async def call(self, method, raw, principal):
        if method == 'events/subscribe':
            return await self.subscribe(raw, principal)
        handler = self.catalog if method == 'events/list' else self.unsubscribe
        return await self.store.run(handler, raw, principal)

    def authorize(self, subscription, room, payload=None):
        principal = self.c.grant_reader(room, subscription['grant_id'], snapshot=json.loads(subscription['principal']))
        self.c.live_room(room)
        filters = validate(FILTERS[subscription['name']], json.loads(subscription['arguments']))
        self.c.joining.subscription_guard(room, principal, filters)
        if subscription['name'] == MESSAGE_EVENT:
            self.c.conversations.resolve(principal, room, filters)
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
        if payload.get('test'):
            return payload.get('test_subscription_id') == subscription['id']
        if event['name'] == TASK_EVENT and filters['queue'] != event['queue']:
            return False
        if filters.get('severity_min') and SEVERITIES.get(payload.get('severity'), -1) < SEVERITIES[filters['severity_min']]:
            return False
        if filters.get('rule_ids') is not None and payload.get('rule_id') not in filters['rule_ids']:
            return False
        return True

    def advance(self, subscription_id):
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscription_id,))
        pending = self.store.one("SELECT MIN(event_seq) AS n FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')", (subscription_id,))['n']
        ack = min(sub['scan_seq'], pending - 1) if pending is not None else sub['scan_seq']
        self.store.execute('UPDATE mcp_event_subscriptions SET ack_seq=MAX(ack_seq,?) WHERE id=?', (ack, subscription_id))

    def reserve(self):
        self.guard()
        now, reserved = self.c.clock(), []
        with self.store.transaction():
            subscriptions = self.store.all("SELECT * FROM mcp_event_subscriptions WHERE state='active' AND id>? ORDER BY id LIMIT 32", (self._scan_after,))
            self._scan_after = subscriptions[-1]['id'] if len(subscriptions) == 32 else ''
            for sub in subscriptions:
                if sub['expires_at'] <= now:
                    self.store.execute("UPDATE mcp_event_subscriptions SET state='expired',version=version+1,updated=? WHERE id=?", (now, sub['id']))
                    continue
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (sub['room_id'],))
                try:
                    filters = self.authorize(sub, room)
                except DevError as exc:
                    if exc.code in {'ROOM_PAUSED', 'WORKER_BINDING_REQUIRED', 'GOAL_INACTIVE'}:
                        continue
                    self.store.execute("UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=? WHERE id=?", (now, sub['id']))
                    continue
                # Verification cache expiry only controls the next challenge.
                # The already-verified subscription remains valid until expires_at.
                rows = self.store.all('SELECT * FROM mcp_event_outbox WHERE room_id=? AND seq>? ORDER BY seq LIMIT 100', (room['id'], sub['scan_seq']))
                for event in rows:
                    if self.matches(sub, filters, event):
                        self.store.execute('''INSERT OR IGNORE INTO mcp_event_deliveries
                            (id,subscription_id,event_id,event_seq,created) VALUES (?,?,?,?,?)''',
                            (digest([sub['id'], event['id']]), sub['id'], event['id'], event['seq'], now))
                if rows:
                    self.store.execute('UPDATE mcp_event_subscriptions SET scan_seq=? WHERE id=?', (rows[-1]['seq'], sub['id']))
                self.advance(sub['id'])
                delivery = self.store.one("SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','retry_wait','leased') ORDER BY event_seq LIMIT 1", (sub['id'],))
                if not delivery or delivery['next_at'] > now or (delivery['state'] == 'leased' and (delivery['lease_until'] or 0) > now):
                    continue
                event = self.store.one('SELECT * FROM mcp_event_outbox WHERE id=?', (delivery['event_id'],))
                payload = validate(PAYLOADS[event['name']], json.loads(event['data']))
                if event['name'] in {WORK_EVENT, DELEGATION_EVENT}:
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
                body = delivery['body'] or canonical({'eventId': event['id'], 'name': event['name'],
                    'timestamp': timestamp(event['created']), 'data': payload,
                    'cursor': sign_cursor(self.c.secret, sub['id'], event['seq'])})
                if len(body.encode()) > 16384:
                    self.store.execute("UPDATE mcp_event_deliveries SET state='dead_letter',reason_code='payload_too_large' WHERE id=?", (delivery['id'],))
                    self.advance(sub['id'])
                    continue
                if delivery['attempt'] >= 5:
                    self.store.execute("UPDATE mcp_event_deliveries SET state='dead_letter',lease_until=NULL,reason_code='attempts_exhausted' WHERE id=?", (delivery['id'],))
                    self.advance(sub['id'])
                    continue
                fence = delivery['fence'] + 1
                self.store.execute("UPDATE mcp_event_deliveries SET state='leased',body=?,attempt=attempt+1,fence=?,lease_until=? WHERE id=?", (body, fence, now + 30, delivery['id']))
                reserved.append({'delivery_id': delivery['id'], 'subscription_id': sub['id'], 'fence': fence,
                                 'subscription_version': sub['version'], 'body': body.encode(), 'event_id': event['id']})
                if len(reserved) >= 2:
                    self._scan_after = sub['id']
                    break
        return reserved

    def ready(self, item):
        with self.store.transaction(immediate=False):
            sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (item['subscription_id'],))
            delivery = self.store.one('SELECT * FROM mcp_event_deliveries WHERE id=?', (item['delivery_id'],))
            if (not sub or sub['state'] != 'active' or sub['version'] != item['subscription_version']
                    or sub['expires_at'] <= self.c.clock() or not delivery or delivery['fence'] != item['fence']
                    or delivery['state'] != 'leased' or (delivery['lease_until'] or 0) <= self.c.clock()):
                return None
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (sub['room_id'],))
            event = self.store.one('SELECT * FROM mcp_event_outbox WHERE id=?', (delivery['event_id'],))
            self.authorize(sub, room, json.loads(event['data']) if event['name'] in {WORK_EVENT, DELEGATION_EVENT} else None)
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

    def finish(self, item, status=None, reason=''):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM mcp_event_deliveries WHERE id=?', (item['delivery_id'],))
            if not row or row['state'] != 'leased' or row['fence'] != item['fence']:
                return
            now = self.c.clock()
            sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (row['subscription_id'],))
            if sub['version'] != item['subscription_version']:
                # Preserve the old in-flight receipt as audit, not as ACK for a new
                # generation. Its lease will expire and replay with a current fence.
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (sub['room_id'],))
                self.c.audit(room, 'events:dispatcher', 'delivery.late_receipt', row['id'], {'status': status})
                return
            accepted = status is not None and 200 <= status < 300
            retryable = reason == 'network_error' or status in {408, 425, 429} or (status is not None and status >= 500)
            if accepted:
                state, next_at, reason = 'accepted', 0, ''
            elif sub['state'] != 'active' or sub['expires_at'] <= now or reason == 'authorization_stopped':
                state, next_at = 'abandoned', 0
            elif retryable and row['attempt'] < 5:
                state, next_at = 'retry_wait', now + (30, 120, 600, 1800)[min(row['attempt'] - 1, 3)]
            else:
                state, next_at = 'dead_letter', 0
            self.store.execute('''UPDATE mcp_event_deliveries SET state=?,next_at=?,lease_until=NULL,
                status_code=?,reason_code=?,accepted_at=? WHERE id=?''',
                (state, next_at, status, reason or ('http_' + str(status) if not accepted else ''), now if accepted else None, row['id']))
            if accepted:
                self.store.execute('UPDATE mcp_event_subscriptions SET last_accepted=? WHERE id=?', (now, sub['id']))
            self.advance(sub['id'])

    async def deliver(self, item):
        try:
            async with self._network_limit:
                outbound = await self.store.run(self.ready, item)
                if outbound is None:
                    await self.store.run(self.finish, item, None, 'authorization_stopped')
                    return
                url, headers = outbound
                reply = await self.sender(url, item['body'], headers)
            await self.store.run(self.finish, item, reply.status)
        except DevError as exc:
            policy = exc.code in {'CALLBACK_URL_REJECTED', 'CALLBACK_ADDRESS_REJECTED', 'HTTP_REDIRECT_REJECTED', 'HTTP_RESPONSE_TOO_LARGE', 'EVENT_TOO_LARGE'}
            await self.store.run(self.finish, item, None, 'callback_policy_rejected' if policy else 'authorization_stopped')
        except (TimeoutError, httpx.HTTPError, OSError):
            await self.store.run(self.finish, item, None, 'network_error')

    async def tick(self):
        items = await self.store.run(self.reserve)
        await asyncio.gather(*(self.deliver(item) for item in items))

    def queue_test(self, room, subscription):
        self.guard()
        if subscription['state'] != 'active' or subscription['expires_at'] <= self.c.clock():
            raise DevError('SUBSCRIPTION_INACTIVE', '订阅当前不可投递', 409)
        filters = self.authorize(subscription, room)
        identifier = 'test_' + secrets.token_hex(16)
        base = {'test': True, 'test_subscription_id': subscription['id']}
        if subscription['name'] == TASK_EVENT:
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
