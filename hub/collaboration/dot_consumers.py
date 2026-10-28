"""Fenced Dot consumer sessions; a session never grants work authority.

The unified mailbox owns the binding and dirty revision. This module owns only
short-lived delivery tickets, consumer ownership, and idempotent lease actions.
No read returns a ticket/proof, acknowledges input, or renews a lease.
"""
from __future__ import annotations

import hmac
import json
import secrets
import uuid

from hub.collaboration.common import canonical, digest
from shared import dot_consumer_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_fences (
        dot_id TEXT PRIMARY KEY REFERENCES collaboration_dots(id),
        fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0))""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_sessions (
        id TEXT PRIMARY KEY, dot_id TEXT NOT NULL REFERENCES collaboration_dots(id),
        generation TEXT NOT NULL, grant_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
        run_id TEXT NOT NULL, request_key TEXT NOT NULL, request_digest TEXT NOT NULL,
        proof_hash TEXT NOT NULL, proof_secret TEXT NOT NULL, scope_digest TEXT NOT NULL,
        fence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','yielded','expired')),
        lease_until REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(dot_id,generation,request_key), UNIQUE(dot_id,generation,run_id),
        UNIQUE(dot_id,fence))""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS dot_consumer_single_owner
        ON dot_consumer_sessions(dot_id) WHERE state='active'""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_consumer_session_scope
        ON dot_consumer_sessions(dot_id,generation,state,lease_until)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_wake_tickets (
        ticket_hash TEXT PRIMARY KEY, dot_id TEXT NOT NULL REFERENCES collaboration_dots(id),
        generation TEXT NOT NULL, grant_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
        delivery_key TEXT NOT NULL, ticket_secret TEXT NOT NULL, expires_at REAL NOT NULL,
        redeemed_session_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        UNIQUE(dot_id,generation,subscription_id,delivery_key))""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_wake_ticket_expiry
        ON dot_wake_tickets(expires_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_mutations (
        session_id TEXT NOT NULL REFERENCES dot_consumer_sessions(id),
        action TEXT NOT NULL, request_key TEXT NOT NULL, request_digest TEXT NOT NULL,
        result TEXT NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(session_id,action,request_key))""")


class DotConsumers:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def mailbox(self):
        mailbox = getattr(self.c, 'dot_mailbox', None)
        if mailbox is None:
            raise DevError('DOT_CONSUMER_UNAVAILABLE', '当前服务未启用统一收件箱', 409)
        return mailbox

    def active_binding(self, dot_id):
        mailbox = getattr(self.c, 'dot_mailbox', None)
        row = mailbox.binding(dot_id) if mailbox is not None else None
        # Expiration/revocation must not silently restore an unfenced legacy
        # write path. The mailbox keeps activation as a durable protocol choice.
        return row if row and row['state'] != 'prepared' else None

    def binding(self, room, dot, principal=None, generation=None):
        row = self.active_binding(dot['id'])
        if not row or row['state'] != 'active' or (generation is not None and row['generation'] != generation):
            raise DevError('DOT_CONSUMER_BINDING_CHANGED', '消费者绑定未启用或已改变，请恢复原连接', 409)
        if (row['room_id'] != room['id'] or row['conversation_id'] != dot['conversation_id']
                or (principal is not None and row['grant_id'] != principal.grant_id)):
            raise DevError('DOT_CONSUMER_BINDING_DENIED', '消费者不属于当前认证连接与房间', 403)
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (row['subscription_id'],))
        if (not sub or sub['state'] != 'active' or sub['expires_at'] <= self.c.clock()
                or sub['grant_id'] != row['grant_id'] or sub['room_id'] != room['id']):
            raise DevError('DOT_CONSUMER_SUBSCRIPTION_INACTIVE', '原订阅未就绪或已失效，请恢复原订阅', 409)
        self.c.events.authorize(sub, room)
        return row, sub

    def activate(self, room, dot, slot, generation, subscription_id):
        """Internal hook, only after the native subscription has been verified."""
        with self.store.transaction():
            binding, _ = self.binding(room, dot, generation=generation)
            if binding['subscription_id'] != subscription_id or binding['grant_id'] != slot['grant_id']:
                raise DevError('DOT_CONSUMER_BINDING_CHANGED', '订阅不能激活其他消费者绑定', 409)
            self.store.execute('INSERT OR IGNORE INTO dot_consumer_fences(dot_id) VALUES (?)', (dot['id'],))
            self.store.execute("""UPDATE dot_consumer_sessions SET state='expired',updated=?
                WHERE dot_id=? AND state='active' AND (generation!=? OR subscription_id!=?)""",
                (self.c.clock(), dot['id'], generation, subscription_id))

    def issue_ticket(self, room, dot, subscription, generation, delivery_key, *, ttl_seconds=600):
        """Internal immutable delivery body input; never expose from read tools."""
        if not isinstance(delivery_key, str) or not delivery_key or len(delivery_key) > 256:
            raise ValueError('A bounded durable delivery identity is required')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 600:
            raise ValueError('Wake ticket lifetime must be within 1..600 seconds')
        with self.store.transaction():
            binding, current = self.binding(room, dot, generation=generation)
            if current['id'] != subscription['id']:
                raise DevError('DOT_CONSUMER_BINDING_CHANGED', '不能向其他订阅签发消费票据', 409)
            previous = self.store.one("""SELECT * FROM dot_wake_tickets WHERE
                dot_id=? AND generation=? AND subscription_id=? AND delivery_key=?""",
                (dot['id'], generation, current['id'], delivery_key))
            if previous:
                # Preserve byte-identical event bodies on retries, even if stale.
                return self.store.decrypt(previous['ticket_secret'])
            now = self.c.clock()
            ticket = secrets.token_urlsafe(32)
            self.store.execute("""INSERT INTO dot_wake_tickets
                (ticket_hash,dot_id,generation,grant_id,subscription_id,delivery_key,ticket_secret,expires_at,created)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (digest(ticket), dot['id'], generation, binding['grant_id'], current['id'],
                 delivery_key, self.store.encrypt(ticket), min(now + ttl_seconds, current['expires_at']), now))
            return ticket

    def scope(self, raw, principal, model):
        args, principal, room, slot, dot = self.c.dot_chat.scope(raw, principal, model, connector=True)
        self.c.live_room(room)
        binding, sub = self.binding(room, dot, principal, args.get('binding_generation'))
        return args, principal, room, slot, dot, binding, sub

    def _revision(self, dot, binding):
        return self.mailbox().current_revision(dot['id'], binding['generation'])

    def _reply(self, row, *, include_proof=False):
        result = {'consumer_session_id': row['id'], 'consumer_fence': row['fence'],
                  'dot_id': row['dot_id'], 'binding_generation': row['generation'],
                  'state': row['state'] if row['state'] != 'active' or row['lease_until'] > self.c.clock() else 'expired',
                  'lease_until': row['lease_until'], 'grants_authority': False}
        if include_proof:
            result['consumer_proof'] = self.store.decrypt(row['proof_secret'])
        return result

    def begin(self, raw, principal):
        with self.store.transaction():
            args, principal, room, _, dot, binding, sub = self.scope(raw, principal, contracts.ConsumerBegin)
            fingerprint = digest({key: value for key, value in args.items() if key != 'wake_ticket'} |
                                 {'wake_ticket_hash': digest(args['wake_ticket'])})
            ticket = self.store.one('SELECT * FROM dot_wake_tickets WHERE ticket_hash=?',
                                    (digest(args['wake_ticket']),))
            if (not ticket or ticket['dot_id'] != dot['id'] or ticket['generation'] != binding['generation']
                    or ticket['grant_id'] != principal.grant_id or ticket['subscription_id'] != sub['id']):
                raise DevError('DOT_WAKE_TICKET_INVALID', '消费票据不属于这个 Dot 与原订阅', 403)
            previous = self.store.one("""SELECT * FROM dot_consumer_sessions
                WHERE dot_id=? AND generation=? AND (request_key=? OR run_id=?)""",
                (dot['id'], binding['generation'], args['idempotency_key'], args['consumer_run_id']))
            if previous:
                if previous['request_digest'] != fingerprint or ticket['redeemed_session_id'] != previous['id']:
                    raise DevError('IDEMPOTENCY_CONFLICT', '原消费请求标识不能用于不同参数', 409)
                if self.c.message_handling.scope_digest(room, dot) != previous['scope_digest']:
                    raise DevError('DOT_CONSUMER_SCOPE_CHANGED', '原消费范围已改变，不能恢复旧消费凭证', 409)
                return {**self._reply(previous, include_proof=True),
                        'inbox_revision': self._revision(dot, binding), 'replayed': True}
            if ticket['redeemed_session_id']:
                raise DevError('DOT_WAKE_TICKET_REDEEMED', '原票据已被其他消费回合使用', 409)
            now = self.c.clock()
            if ticket['expires_at'] <= now:
                raise DevError('DOT_WAKE_TICKET_EXPIRED', '消费票据已过期，业务待办仍保留', 409)
            self.store.execute("""UPDATE dot_consumer_sessions SET state='expired',updated=?
                WHERE dot_id=? AND state='active' AND lease_until<=?""", (now, dot['id'], now))
            active = self.store.one("SELECT * FROM dot_consumer_sessions WHERE dot_id=? AND state='active'", (dot['id'],))
            if active:
                raise DevError('DOT_CONSUMER_BUSY', '这个 Dot 已有活跃消费回合，请使用原回合或等待恢复',
                               409, retry_after_seconds=max(1, int(active['lease_until'] - now)))
            self.store.execute('INSERT OR IGNORE INTO dot_consumer_fences(dot_id) VALUES (?)', (dot['id'],))
            self.store.execute('UPDATE dot_consumer_fences SET fence=fence+1 WHERE dot_id=?', (dot['id'],))
            fence = self.store.one('SELECT fence FROM dot_consumer_fences WHERE dot_id=?', (dot['id'],))['fence']
            identifier, proof = uuid.uuid4().hex, secrets.token_urlsafe(32)
            self.store.execute("""INSERT INTO dot_consumer_sessions
                (id,dot_id,generation,grant_id,subscription_id,run_id,request_key,request_digest,
                 proof_hash,proof_secret,scope_digest,fence,state,lease_until,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (identifier, dot['id'], binding['generation'], principal.grant_id, sub['id'],
                 args['consumer_run_id'], args['idempotency_key'], fingerprint, digest(proof),
                 self.store.encrypt(proof), self.c.message_handling.scope_digest(room, dot),
                 fence, min(now + args['lease_seconds'], sub['expires_at']), now, now))
            self.store.execute('UPDATE dot_wake_tickets SET redeemed_session_id=? WHERE ticket_hash=?',
                               (identifier, ticket['ticket_hash']))
            self.c.audit(room, principal.actor, 'dot.consumer_started', identifier,
                         {'dot_id': dot['id'], 'fence': fence, 'binding_generation': binding['generation']})
            row = self.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?', (identifier,))
            return {**self._reply(row, include_proof=True), 'inbox_revision': self._revision(dot, binding),
                    'replayed': False}

    def _credential(self, raw, principal, room, dot, binding, *, live=True):
        row = self.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',
                             (raw.get('consumer_session_id', ''),))
        proof = raw.get('consumer_proof', '')
        if (not row or row['dot_id'] != dot['id'] or row['grant_id'] != principal.grant_id
                or row['generation'] != binding['generation']
                or row['subscription_id'] != binding['subscription_id']
                or not isinstance(raw.get('consumer_fence'), int) or isinstance(raw.get('consumer_fence'), bool)
                or row['fence'] != raw['consumer_fence'] or not isinstance(proof, str)
                or not hmac.compare_digest(row['proof_hash'], digest(proof))):
            raise DevError('DOT_CONSUMER_PROOF_REQUIRED', '当前 Dot 写入需要原消费回合的有效凭证与 fence', 403)
        if self.c.message_handling.scope_digest(room, dot) != row['scope_digest']:
            raise DevError('DOT_CONSUMER_SCOPE_CHANGED', '消费期间授权或项目范围已改变，请恢复当前绑定', 409)
        if live:
            current = self.store.one('SELECT fence FROM dot_consumer_fences WHERE dot_id=?', (dot['id'],))
            if (row['state'] != 'active' or row['lease_until'] <= self.c.clock()
                    or not current or current['fence'] != row['fence']):
                raise DevError('DOT_CONSUMER_LEASE_EXPIRED', '原消费回合已让出或过期，不能继续写入', 409)
        return row

    def guard(self, raw, principal, room, dot, *, require_revision=False):
        """Call inside the same transaction as the protected mutation."""
        if not self.active_binding(dot['id']):
            return None
        self.c.dot_chat.authorize(principal, room, dot, connector=True)
        self.c.live_room(room)
        binding, _ = self.binding(room, dot, principal)
        row = self._credential(raw, principal, room, dot, binding)
        expected = raw.get('expected_inbox_revision')
        if require_revision and (not isinstance(expected, int) or isinstance(expected, bool)
                or expected != self._revision(dot, binding)):
            raise DevError('DOT_INBOX_CHANGED', '有新的输入或工作状态，请读取当前收件箱后继续原工作', 409,
                           inbox_revision=self._revision(dot, binding))
        return row

    def heartbeat(self, raw, principal):
        return self._lease_action(raw, principal, 'heartbeat')

    def yield_session(self, raw, principal):
        return self._lease_action(raw, principal, 'yield')

    def _lease_action(self, raw, principal, action):
        model = contracts.ConsumerYield if action == 'yield' else contracts.ConsumerLease
        with self.store.transaction():
            args, principal, room, _, dot, binding, sub = self.scope(raw, principal, model)
            row = self._credential(args, principal, room, dot, binding, live=False)
            fingerprint = digest({key: value for key, value in args.items() if key != 'consumer_proof'})
            old = self.store.one("""SELECT * FROM dot_consumer_mutations
                WHERE session_id=? AND action=? AND request_key=?""", (row['id'], action, args['idempotency_key']))
            if old:
                if old['request_digest'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '原消费租约请求标识不能更换参数', 409)
                return json.loads(old['result'])
            row = self._credential(args, principal, room, dot, binding)
            now = self.c.clock()
            revision = self._revision(dot, binding)
            more = False
            if action == 'yield':
                drained = self.mailbox().mark_drained(dot['id'], binding['generation'], args['expected_inbox_revision'])
                more = not drained
                if drained:
                    self.store.execute("UPDATE dot_consumer_sessions SET state='yielded',lease_until=?,updated=? WHERE id=?",
                                       (now, now, row['id']))
            else:
                self.store.execute('UPDATE dot_consumer_sessions SET lease_until=?,updated=? WHERE id=?',
                                   (min(now + args['lease_seconds'], sub['expires_at']), now, row['id']))
            current = self.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?', (row['id'],))
            result = {**self._reply(current), 'inbox_revision': revision, 'more': more,
                      'read_is_consumption': False, 'work_lease_changed': False}
            self.store.execute('INSERT INTO dot_consumer_mutations VALUES (?,?,?,?,?,?)',
                (row['id'], action, args['idempotency_key'], fingerprint, canonical(result), now))
            return result

    def read(self, raw, principal):
        """Passive status only; no original or other consumer bearer material."""
        with self.store.transaction(immediate=False):
            args, principal, room, _, dot = self.c.dot_chat.scope(raw, principal, contracts.ConsumerRead, connector=True)
            binding = self.active_binding(dot['id'])
            rows = self.store.all("""SELECT * FROM dot_consumer_sessions
                WHERE dot_id=? ORDER BY fence DESC LIMIT 1""", (dot['id'],))
            return {'dot_id': dot['id'], 'consumer_protocol': 2 if binding else 1,
                    'binding_generation': binding['generation'] if binding else None,
                    'inbox_revision': self._revision(dot, binding) if binding else None,
                    'consumer': self._reply(rows[0]) if rows else None,
                    'read_only': True, 'grants_authority': False}

    def guard_work(self, raw, principal, goal, item=None, *, require_revision=True):
        """Locate the responsible Dot from server facts, never a display name."""
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
        dot_id = ''
        delegation_slot = False
        if item:
            if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_work_assignments'"):
                assignment = self.store.one('SELECT dot_id FROM team_work_assignments WHERE work_id=?', (item['id'],))
                dot_id = assignment['dot_id'] if assignment else ''
            if not dot_id:
                source = self.store.one("""SELECT p.slot_id FROM delegation_requests d
                    JOIN delegation_policies p ON p.id=d.policy_id
                    WHERE d.work_item_id=? AND d.goal_id=?""", (item['id'], goal['id']))
                dot_id = source['slot_id'] if source else ''
                delegation_slot = bool(dot_id)
        if not dot_id:
            source = self.store.one("""SELECT p.slot_id FROM delegation_requests d
                JOIN delegation_policies p ON p.id=d.policy_id WHERE d.goal_id=?""", (goal['id'],))
            dot_id = source['slot_id'] if source else ''
            delegation_slot = bool(dot_id)
        session = self.store.one('SELECT dot_id FROM dot_consumer_sessions WHERE id=?',
                                 (raw.get('consumer_session_id', ''),)) if raw.get('consumer_session_id') else None
        if session and raw.get('dot_id') and raw['dot_id'] != session['dot_id']:
            raise DevError('DOT_CONSUMER_WORK_MISMATCH', '显式 Dot 与消费回合不一致', 403)
        if dot_id:
            if ((session and session['dot_id'] != dot_id)
                    or (raw.get('dot_id') and raw['dot_id'] != dot_id)):
                raise DevError('DOT_CONSUMER_WORK_MISMATCH', '工作不属于这个消费回合的 Dot', 403)
        elif session:
            dot_id = session['dot_id']
        else:
            mailbox = getattr(self.c, 'dot_mailbox', None)
            bindings = mailbox.for_grant(principal.grant_id, goal['conversation_id']) if mailbox else []
            if bindings:
                raise DevError('DOT_CONSUMER_PROOF_REQUIRED', '当前连接已启用消费凭证，不能走旧工作入口', 403)
            if not raw.get('dot_id'):
                return None
            dot_id = raw['dot_id']
        dot = self.c.dots.find(dot_id)
        if not dot and delegation_slot and not self.active_binding(dot_id):
            # CPJ delegation slots predate task/duplex Dot records. Their
            # original policy, goal approval and work lease still authorize
            # effects; a different v2 slot sharing the grant does not upgrade
            # this exact legacy route. Never infer this exception from raw IDs.
            legacy = self.store.one("""SELECT 1 FROM collaboration_join_slots s
                JOIN delegation_policies p ON p.slot_id=s.id AND p.room_id=s.room_id
                JOIN delegation_requests d ON d.policy_id=p.id
                WHERE s.id=? AND s.room_id=? AND p.conversation_id=? AND d.goal_id=?""",
                (dot_id, goal['room_id'], goal['conversation_id'], goal['id']))
            if legacy:
                return None
        if not dot or dot['conversation_id'] != goal['conversation_id']:
            raise DevError('DOT_CONSUMER_WORK_MISMATCH', '消费者不属于原工作会话', 403)
        return self.guard(raw, principal, room, dot, require_revision=require_revision)
