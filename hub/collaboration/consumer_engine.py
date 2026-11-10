"""One fenced lease engine for legacy Dots and independent room members.

Only the identity column is selected by a fixed server adapter. Both protocols
share the original journal, idempotency records, tickets, fences and leases.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import uuid

from hub.collaboration.common import canonical, digest
from shared.util import DevError


class FencedConsumerEngine:
    subject_column = 'dot_id'

    def _sql(self, sql):
        if self.subject_column not in {'dot_id', 'member_id'}:
            raise RuntimeError('Unknown consumer identity column')
        return re.sub(r'\bdot_id\b', self.subject_column, sql)

    def _one(self, sql, arguments=()):
        return self.store.one(self._sql(sql), arguments)

    def _all(self, sql, arguments=()):
        return self.store.all(self._sql(sql), arguments)

    def _execute(self, sql, arguments=()):
        return self.store.execute(self._sql(sql), arguments)

    def activate(self, room, dot, slot, generation, subscription_id):
        """Internal hook, only after the native subscription has been verified."""
        with self.store.transaction():
            binding, _ = self.binding(room, dot, generation=generation)
            if binding['subscription_id'] != subscription_id or binding['grant_id'] != slot['grant_id']:
                raise DevError('DOT_CONSUMER_BINDING_CHANGED', '订阅不能激活其他消费者绑定', 409)
            self._execute('INSERT OR IGNORE INTO dot_consumer_fences(dot_id) VALUES (?)', (dot['id'],))
            self._execute("""UPDATE dot_consumer_sessions SET state='expired',updated=?
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
            previous = self._one("""SELECT * FROM dot_wake_tickets WHERE
                dot_id=? AND generation=? AND subscription_id=? AND delivery_key=?""",
                (dot['id'], generation, current['id'], delivery_key))
            if previous:
                # Preserve byte-identical event bodies on retries, even if stale.
                return self.store.decrypt(previous['ticket_secret'])
            now = self.c.clock()
            ticket = secrets.token_urlsafe(32)
            self._execute("""INSERT INTO dot_wake_tickets
                (ticket_hash,dot_id,generation,grant_id,subscription_id,delivery_key,ticket_secret,expires_at,created)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (digest(ticket), dot['id'], generation, binding['grant_id'], current['id'],
                 delivery_key, self.store.encrypt(ticket), min(now + ttl_seconds, current['expires_at']), now))
            return ticket


    def _revision(self, dot, binding):
        return self.mailbox().current_revision(dot['id'], binding['generation'])


    def _reply(self, row, *, include_proof=False):
        result = {'consumer_session_id': row['id'], 'consumer_fence': row['fence'],
                  self.subject_column: row[self.subject_column], 'binding_generation': row['generation'],
                  'state': row['state'] if row['state'] != 'active' or row['lease_until'] > self.c.clock() else 'expired',
                  'lease_until': row['lease_until'], 'grants_authority': False}
        if include_proof:
            result['consumer_proof'] = self.store.decrypt(row['proof_secret'])
        return result


    @staticmethod
    def begin_fingerprint(args):
        return digest({key: value for key, value in args.items() if key != 'wake_ticket'} |
                      {'wake_ticket_hash': digest(args['wake_ticket'])})

    def begin(self, raw, principal):
        with self.store.transaction():
            args, principal, room, _, dot, binding, sub = self.scope(raw, principal, self.begin_model)
            fingerprint = self.begin_fingerprint(args)
            ticket = self._one('SELECT * FROM dot_wake_tickets WHERE ticket_hash=?',
                                    (digest(args['wake_ticket']),))
            if (not ticket or ticket[self.subject_column] != dot['id'] or ticket['generation'] != binding['generation']
                    or ticket['grant_id'] != principal.grant_id or ticket['subscription_id'] != sub['id']):
                raise DevError('DOT_WAKE_TICKET_INVALID', '消费票据不属于这个 Dot 与原订阅', 403)
            previous = self._one("""SELECT * FROM dot_consumer_sessions
                WHERE dot_id=? AND generation=? AND (request_key=? OR run_id=?)""",
                (dot['id'], binding['generation'], args['idempotency_key'], args['consumer_run_id']))
            if previous:
                if previous['request_digest'] != fingerprint or ticket['redeemed_session_id'] != previous['id']:
                    raise DevError('IDEMPOTENCY_CONFLICT', '原消费请求标识不能用于不同参数', 409)
                if self.scope_digest(room, dot) != previous['scope_digest']:
                    raise DevError('DOT_CONSUMER_SCOPE_CHANGED', '原消费范围已改变，不能恢复旧消费凭证', 409)
                return {**self._reply(previous, include_proof=True),
                        'inbox_revision': self._revision(dot, binding), 'replayed': True}
            if ticket['redeemed_session_id']:
                raise DevError('DOT_WAKE_TICKET_REDEEMED', '原票据已被其他消费回合使用', 409)
            now = self.c.clock()
            if ticket['expires_at'] <= now:
                raise DevError('DOT_WAKE_TICKET_EXPIRED', '消费票据已过期，业务待办仍保留', 409)
            self._execute("""UPDATE dot_consumer_sessions SET state='expired',updated=?
                WHERE dot_id=? AND state='active' AND lease_until<=?""", (now, dot['id'], now))
            active = self._one("SELECT * FROM dot_consumer_sessions WHERE dot_id=? AND state='active'", (dot['id'],))
            if active:
                raise DevError('DOT_CONSUMER_BUSY', '这个 Dot 已有活跃消费回合，请使用原回合或等待恢复',
                               409, retry_after_seconds=max(1, int(active['lease_until'] - now)))
            self._execute('INSERT OR IGNORE INTO dot_consumer_fences(dot_id) VALUES (?)', (dot['id'],))
            self._execute('UPDATE dot_consumer_fences SET fence=fence+1 WHERE dot_id=?', (dot['id'],))
            fence = self._one('SELECT fence FROM dot_consumer_fences WHERE dot_id=?', (dot['id'],))['fence']
            identifier, proof = uuid.uuid4().hex, secrets.token_urlsafe(32)
            self._execute("""INSERT INTO dot_consumer_sessions
                (id,dot_id,generation,grant_id,subscription_id,run_id,request_key,request_digest,
                 proof_hash,proof_secret,scope_digest,fence,state,lease_until,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (identifier, dot['id'], binding['generation'], principal.grant_id, sub['id'],
                 args['consumer_run_id'], args['idempotency_key'], fingerprint, digest(proof),
                 self.store.encrypt(proof), self.scope_digest(room, dot),
                 fence, min(now + args['lease_seconds'], sub['expires_at']), now, now))
            self._execute('UPDATE dot_wake_tickets SET redeemed_session_id=? WHERE ticket_hash=?',
                               (identifier, ticket['ticket_hash']))
            self.consumer_audit(room, principal.actor, 'dot.consumer_started', identifier,
                         {'dot_id': dot['id'], 'fence': fence, 'binding_generation': binding['generation']})
            row = self._one('SELECT * FROM dot_consumer_sessions WHERE id=?', (identifier,))
            return {**self._reply(row, include_proof=True), 'inbox_revision': self._revision(dot, binding),
                    'replayed': False}


    def _credential(self, raw, principal, room, dot, binding, *, live=True):
        row = self._one('SELECT * FROM dot_consumer_sessions WHERE id=?',
                             (raw.get('consumer_session_id', ''),))
        proof = raw.get('consumer_proof', '')
        if (not row or row[self.subject_column] != dot['id'] or row['grant_id'] != principal.grant_id
                or row['generation'] != binding['generation']
                or row['subscription_id'] != binding['subscription_id']
                or not isinstance(raw.get('consumer_fence'), int) or isinstance(raw.get('consumer_fence'), bool)
                or row['fence'] != raw['consumer_fence'] or not isinstance(proof, str)
                or not hmac.compare_digest(row['proof_hash'], digest(proof))):
            raise DevError('DOT_CONSUMER_PROOF_REQUIRED', '当前 Dot 写入需要原消费回合的有效凭证与 fence', 403)
        if self.scope_digest(room, dot) != row['scope_digest']:
            raise DevError('DOT_CONSUMER_SCOPE_CHANGED', '消费期间授权或项目范围已改变，请恢复当前绑定', 409)
        if live:
            current = self._one('SELECT fence FROM dot_consumer_fences WHERE dot_id=?', (dot['id'],))
            if (row['state'] != 'active' or row['lease_until'] <= self.c.clock()
                    or not current or current['fence'] != row['fence']):
                raise DevError('DOT_CONSUMER_LEASE_EXPIRED', '原消费回合已让出或过期，不能继续写入', 409)
        return row


    def heartbeat(self, raw, principal):
        return self._lease_action(raw, principal, 'heartbeat')


    def yield_session(self, raw, principal):
        return self._lease_action(raw, principal, 'yield')


    def _lease_action(self, raw, principal, action):
        model = self.yield_model if action == 'yield' else self.lease_model
        with self.store.transaction():
            args, principal, room, _, dot, binding, sub = self.scope(raw, principal, model)
            row = self._credential(args, principal, room, dot, binding, live=False)
            fingerprint = digest({key: value for key, value in args.items() if key != 'consumer_proof'})
            old = self._one("""SELECT * FROM dot_consumer_mutations
                WHERE session_id=? AND action=? AND request_key=?""", (row['id'], action, args['idempotency_key']))
            if old:
                if old['request_digest'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '原消费租约请求标识不能更换参数', 409)
                from hub.collaboration.history_retention import replay_response
                return replay_response(old['result'])
            row = self._credential(args, principal, room, dot, binding)
            now = self.c.clock()
            revision = self._revision(dot, binding)
            more = False
            if action == 'yield':
                drained = self.mailbox().mark_drained(dot['id'], binding['generation'], args['expected_inbox_revision'])
                more = not drained
                if drained:
                    self._execute("UPDATE dot_consumer_sessions SET state='yielded',lease_until=?,updated=? WHERE id=?",
                                       (now, now, row['id']))
            else:
                self._execute('UPDATE dot_consumer_sessions SET lease_until=?,updated=? WHERE id=?',
                                   (min(now + args['lease_seconds'], sub['expires_at']), now, row['id']))
            current = self._one('SELECT * FROM dot_consumer_sessions WHERE id=?', (row['id'],))
            result = {**self._reply(current), 'inbox_revision': revision, 'more': more,
                      'read_is_consumption': False, 'work_lease_changed': False}
            self._execute('INSERT INTO dot_consumer_mutations VALUES (?,?,?,?,?,?)',
                (row['id'], action, args['idempotency_key'], fingerprint, canonical(result), now))
            return result
