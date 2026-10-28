"""Stable, level-triggered Dot inbox; notifications never own task authority.

The original message/work ledgers remain authoritative. This index stores only
typed references and a monotonic dirty revision. Reads never acknowledge,
activate a protocol, renew a subscription, or create work.
"""
from __future__ import annotations

import json

from hub.collaboration.common import (
    DELEGATION_EVENT, MAILBOX_EVENT, MESSAGE_EVENT, OPERATION_EVENT, WORK_EVENT,
    canonical, digest, read_cursor, sign_cursor, validate,
)
from shared.dot_mailbox_contracts import DotMailboxAck, DotMailboxConfigure, DotMailboxRead, DotMailboxRecover
from shared.util import DevError

KINDS = frozenset({'message', 'work_ready', 'peer_handoff', 'result_ready', 'control_notice'})
REFERENCE_KEYS = frozenset({'goal_id', 'approval_id', 'work_item_id', 'message_id',
                           'result_id', 'operation_id', 'recipient_slot_id',
                           'thread_root_id', 'reason_code', 'source_project_id'})
PAGE_BYTE_BUDGET = 48 * 1024
ITEM_BYTE_BUDGET = PAGE_BYTE_BUDGET - 4096
CONTRACT_HASH = digest({'event': MAILBOX_EVENT, 'schema_version': 2,
    'entry_kinds': sorted(KINDS), 'reference_keys': sorted(REFERENCE_KEYS),
    'fields': ['id', 'kind', 'object_id', 'object_version', 'revision', 'reference',
               'ready', 'read_request', 'ack_request', 'handoff'],
    'page_limit': 100, 'page_byte_budget': PAGE_BYTE_BUDGET,
    'request_schemas': {model.__name__: model.model_json_schema()
                        for model in (DotMailboxConfigure, DotMailboxRead, DotMailboxAck, DotMailboxRecover)},
    'read_is_consumption': False, 'binding_generation_stable_on_renewal': True})


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dot_mailbox_bindings (
        dot_id TEXT PRIMARY KEY REFERENCES collaboration_dots(id),
        room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,grant_id TEXT NOT NULL,
        generation TEXT NOT NULL UNIQUE,subscription_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','active')),
        dirty_revision INTEGER NOT NULL DEFAULT 0 CHECK(dirty_revision>=0),
        drained_revision INTEGER NOT NULL DEFAULT 0 CHECK(drained_revision>=0),
        wake_generation INTEGER NOT NULL DEFAULT 0,
        manual_wakes INTEGER NOT NULL DEFAULT 0,manual_window_at REAL NOT NULL DEFAULT 0,
        last_manual_wake_at REAL NOT NULL DEFAULT 0,
        wake_count INTEGER NOT NULL DEFAULT 0,last_wake_at REAL NOT NULL DEFAULT 0,
        created REAL NOT NULL,updated REAL NOT NULL)""")
    columns = {row[1] for row in db.execute('PRAGMA table_info(dot_mailbox_bindings)')}
    for name, declaration in (('wake_generation', 'INTEGER NOT NULL DEFAULT 0'),
                              ('manual_wakes', 'INTEGER NOT NULL DEFAULT 0'),
                              ('manual_window_at', 'REAL NOT NULL DEFAULT 0'),
                              ('last_manual_wake_at', 'REAL NOT NULL DEFAULT 0')):
        if name not in columns:
            db.execute(f'ALTER TABLE dot_mailbox_bindings ADD COLUMN {name} {declaration}')
    db.execute("""CREATE INDEX IF NOT EXISTS dot_mailbox_binding_route
        ON dot_mailbox_bindings(room_id,conversation_id,grant_id,state)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_mailbox_entries (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        dot_id TEXT NOT NULL REFERENCES collaboration_dots(id),generation TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('message','work_ready','peer_handoff','result_ready','control_notice')),
        object_id TEXT NOT NULL,object_version INTEGER NOT NULL CHECK(object_version>=0),
        reference TEXT NOT NULL,revision INTEGER NOT NULL,acknowledged_at REAL,
        created REAL NOT NULL,
        UNIQUE(dot_id,generation,kind,object_id,object_version))""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_mailbox_entries_scan
        ON dot_mailbox_entries(dot_id,generation,sequence)""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_mailbox_entries_ready
        ON dot_mailbox_entries(dot_id,generation,acknowledged_at,revision)""")


class DotMailbox:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self._after = ''
        self._maintenance_after = ''

    def binding(self, dot_id):
        return self.store.one('SELECT * FROM dot_mailbox_bindings WHERE dot_id=?', (dot_id,))

    def for_grant(self, grant_id, conversation_id):
        return self.store.all("""SELECT * FROM dot_mailbox_bindings
            WHERE grant_id=? AND conversation_id=? AND state='active' ORDER BY dot_id""",
                              (grant_id, conversation_id))

    def enabled(self, dot_id):
        row = self.binding(dot_id)
        # Expiration does not reopen an unfenced legacy write path.
        return bool(row and row['state'] == 'active')

    def current_revision(self, dot_id, generation=None):
        row = self.binding(dot_id)
        if not row:
            return 0
        self.check_generation(row, generation)
        return row['dirty_revision']

    @staticmethod
    def check_generation(row, generation):
        if generation and generation != row['generation']:
            raise DevError('DOT_MAILBOX_GENERATION_CHANGED', '收件箱绑定已变化，请恢复原连接配置', 409)

    def _identity(self, room, dot, slot):
        return digest(['dot-mailbox-v2', dot['id'], dot['setup_digest'], slot['grant_id'],
                       slot['principal'], room['id'], dot['conversation_id']])

    def configure(self, raw, principal):
        args = validate(DotMailboxConfigure, raw)
        with self.store.transaction():
            _, principal, room, slot, dot = self.c.dots.scope(
                {key: args[key] for key in ('project', 'environment_id', 'dot_id')}, principal)
            if not self.c.dot_chat.enabled(dot):
                raise DevError('DOT_MAILBOX_DUPLEX_REQUIRED', '只有已批准双向通信的 dot 可选择统一收件箱；旧通知用途保持不变', 409)
            self.c.dot_chat.authorize(principal, room, dot, connector=bool(principal.grant_id))
            self.c.joining.active(room, slot, registered=True)
            generation = self._identity(room, dot, slot)

            def save():
                old = self.binding(dot['id'])
                if old:
                    self.check_generation(old, generation)
                    if old['grant_id'] != slot['grant_id']:
                        raise DevError('DOT_MAILBOX_BINDING_CHANGED', '原收件箱连接已变化', 409)
                    return {'dot_id': dot['id']}
                now = self.c.clock()
                self.store.execute("""INSERT INTO dot_mailbox_bindings
                    (dot_id,room_id,conversation_id,grant_id,generation,created,updated)
                    VALUES (?,?,?,?,?,?,?)""", (dot['id'], room['id'], dot['conversation_id'],
                                               slot['grant_id'], generation, now, now))
                self.catch_up(room, dot)
                return {'dot_id': dot['id']}

            self.c.mutation(principal, room, 'dot_mailbox_configure:' + dot['id'], args, save)
            return self.configuration(room, dot) | {
                'permissions_changed': False, 'subscription_created': False,
                'protocol_activated': self.enabled(dot['id']),
                'next_step': '由原宿主创建返回的原生订阅；回调成功后仍需实际消费验证，不重新交办任务。'}

    def subscription(self, room, dot):
        row = self.binding(dot['id'])
        if not row:
            return None
        return {'name': MAILBOX_EVENT, 'arguments': {
            'project_id': room['project_id'], 'environment_id': room['environment_id'],
            'conversation_id': dot['conversation_id'], 'slot_id': dot['id'],
            'binding_generation': row['generation']}}

    def configuration(self, room, dot):
        row = self.binding(dot['id'])
        if not row:
            return {'supported_protocol_versions': [1, 2], 'selected_protocol_version': 1,
                    'configure_action': 'dot_mailbox_configure', 'read_only': True}
        request = {'tool': 'collaboration_query', 'arguments': {
            'action': 'dot_mailbox', 'project': room['project_id'],
            'environment_id': room['environment_id'], 'dot_id': dot['id'],
            'binding_generation': row['generation']}}
        return {'schema_version': 2, 'selected_protocol_version': 2,
                'contract_hash': CONTRACT_HASH, 'page_byte_budget': PAGE_BYTE_BUDGET,
                'active_protocol_version': 2 if row['state'] == 'active' else 1,
                'binding_generation': row['generation'], 'state': row['state'],
                'subscription_request': self.subscription(room, dot),
                'mailbox_request': request, 'dirty_revision': row['dirty_revision'],
                'drained_revision': row['drained_revision'], 'read_only': True,
                'subscription_health': self.health(row),
                'recovery_request': {'tool': 'collaboration', 'arguments': {
                    'action': 'dot_mailbox_recover', 'project': room['project_id'],
                    'environment_id': room['environment_id'], 'dot_id': dot['id'],
                    'binding_generation': row['generation'],
                    'idempotency_key': 'mailbox-recover:' + digest([dot['id'], row['generation'], row['wake_generation']])}},
                'grants_authority': False, 'consumer_receipt_verified': False,
                'wake_instructions': 'On every wake retain the exact binding generation and wake ticket. '
                    'Redeem the ticket with consumer_begin before any reply or managed step; read dot_mailbox '
                    'and every next_page_request, then read the existing dot_inbox and original work references. '
                    'Attach only your own consumer_session_id, consumer_fence and consumer_proof to each mutation. '
                    'Before important actions and final replies, re-read the current expected_inbox_revision. '
                    'Acknowledge peer/control references explicitly; do not acknowledge a user message as completed. '
                    'Use consumer_yield with the observed dirty revision after draining ready items. '
                    'Test events never issue a ticket, create work, or prove model consumption.'}

    def health(self, binding):
        """Truthful route health; observation never renews or repairs a route."""
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',
                             (binding['subscription_id'],)) if binding['subscription_id'] else None
        state = 'awaiting_native_subscription'
        available, reason = False, ''
        if sub:
            state = sub['state']
            if sub['expires_at'] <= self.c.clock():
                state = 'expired'
            elif state == 'active':
                if not self.c.config.events_enabled or self.c.events is None:
                    state = 'events_unavailable'
                else:
                    try:
                        _, room, _ = self._actor(binding)
                        self.c.events.authorize(sub, room)
                        available = True
                        if sub['expires_at'] - self.c.clock() <= 3600:
                            state = 'expiring'
                    except DevError as exc:
                        state, reason = 'authorization_unavailable', exc.code
        return {'state': state, 'available': available, 'reason_code': reason,
                'subscription_id': binding['subscription_id'] or None,
                'expires_at': sub['expires_at'] if sub else None,
                'recovery_wakes': binding['wake_count'],
                'recovery_budget_remaining': max(0, 3 - binding['wake_count']),
                'restoration_scope': 'original_binding_only', 'auto_renewed': False,
                'host_consumer_state': 'unknown'}

    def authorize_event(self, principal, filters, payload=None):
        _, principal, room, slot, dot = self.c.dots.scope({
            'project': filters['project_id'], 'environment_id': filters['environment_id'],
            'dot_id': filters['slot_id']}, principal)
        self.c.dot_chat.authorize(principal, room, dot, connector=True)
        row = self.binding(dot['id'])
        if not row:
            raise DevError('DOT_MAILBOX_NOT_CONFIGURED', '请先明确选择统一收件箱协议', 409)
        self.check_generation(row, filters['binding_generation'])
        if (filters['conversation_id'] != dot['conversation_id'] or
                row['generation'] != self._identity(room, dot, slot) or
                row['grant_id'] != principal.grant_id):
            raise DevError('DOT_MAILBOX_SCOPE_CHANGED', '收件箱身份或范围已变化', 409)
        if payload is not None:
            if any(payload.get(key) != value for key, value in {
                'project_id': room['project_id'], 'environment_id': room['environment_id'],
                'room_id': room['id'],
                'conversation_id': dot['conversation_id'], 'recipient_slot_id': dot['id'],
                'recipient_grant_id': row['grant_id'], 'binding_generation': row['generation']}.items()):
                raise DevError('DOT_MAILBOX_EVENT_DENIED', '提示不属于当前 dot 收件箱', 403)
            if not payload.get('test') and (row['state'] != 'active' or
                    payload['dirty_revision'] > row['dirty_revision']):
                raise DevError('DOT_MAILBOX_EVENT_OBSOLETE', '提示的收件箱代际或版本不再有效', 409)
        return row

    def activate_subscription(self, room, principal, filters, subscription, *, restoring=False):
        row = self.authorize_event(principal, filters)
        if row['subscription_id'] and row['subscription_id'] != subscription['id']:
            raise DevError('DOT_MAILBOX_ORIGINAL_ROUTE_REQUIRED',
                           '只能恢复原宿主订阅；到期不会允许同授权的其他宿主替换此 dot', 409)
        dot = self.c.dots.find(row['dot_id'])
        self.store.execute("""UPDATE dot_mailbox_bindings SET subscription_id=?,state='active',updated=?
            WHERE dot_id=? AND generation=?""",
                           (subscription['id'], self.c.clock(), row['dot_id'], row['generation']))
        if row['state'] == 'active' and restoring:
            self.store.execute("""UPDATE dot_mailbox_bindings SET wake_generation=wake_generation+1,
                wake_count=0,last_wake_at=? WHERE dot_id=?""", (self.c.clock(), dot['id']))
        consumers = getattr(self.c, 'dot_consumers', None)
        if consumers is None:
            raise DevError('DOT_CONSUMER_PROTOCOL_UNAVAILABLE', '消费者身份组件尚未就绪，不能激活新协议', 409)
        consumers.activate(room, dot, self.c.joining.slot(room, dot['id']),
                           row['generation'], subscription['id'])
        self.catch_up(room, dot)
        fresh = self.binding(dot['id'])
        if fresh['dirty_revision'] > fresh['drained_revision']:
            self._wake(room, dot, fresh)

    def delivery_payload(self, subscription, payload, event_id):
        if payload.get('test'):
            return {**payload, 'wake_ticket': None}
        row = self.binding(payload['recipient_slot_id'])
        if not row or row['subscription_id'] != subscription['id']:
            raise DevError('DOT_MAILBOX_ROUTE_CHANGED', '原提示订阅已变化', 409)
        self.check_generation(row, payload['binding_generation'])
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['room_id'],))
        dot = self.c.dots.find(row['dot_id'])
        ticket = self.c.dot_consumers.issue_ticket(
            room, dot, subscription, row['generation'], event_id, ttl_seconds=600)
        return {**payload, 'wake_ticket': ticket}

    def notification_receipt(self, dot_id):
        binding = self.binding(dot_id)
        if not binding or not self.health(binding)['available']:
            return {'state': 'unavailable', 'event_id': None, 'delivered': False}
        event = self.store.one("""SELECT id FROM mcp_event_outbox WHERE room_id=? AND name=?
            AND target_grant_id=? AND json_extract(data,'$.recipient_slot_id')=?
            AND json_extract(data,'$.binding_generation')=? AND json_extract(data,'$.dirty_revision')=?
            AND json_extract(data,'$.test')=0 ORDER BY seq DESC LIMIT 1""",
            (binding['room_id'], MAILBOX_EVENT, binding['grant_id'], dot_id,
             binding['generation'], binding['dirty_revision']))
        return {'state': 'queued' if event else 'unavailable',
                'event_id': event['id'] if event else None, 'delivered': False}

    def _reference(self, value):
        value = value or {}
        if (not isinstance(value, dict) or set(value) - REFERENCE_KEYS or
                any(not isinstance(v, str) or len(v) > 128 for v in value.values())):
            raise DevError('DOT_MAILBOX_REFERENCE_INVALID', '统一收件箱仅接受有界的原对象引用', 422)
        return value

    def mark_dirty(self, dot, kind, object_id, object_version, reference=None):
        dot = self.c.dots.find(dot) if isinstance(dot, str) else dot
        if not dot:
            return 0
        row = self.binding(dot['id'])
        if not row:
            return 0
        if kind not in KINDS or isinstance(object_version, bool) or not isinstance(object_version, int) or object_version < 0:
            raise DevError('DOT_MAILBOX_ENTRY_INVALID', '收件箱类型或对象版本无效', 422)
        if not isinstance(object_id, str) or not object_id or len(object_id) > 128:
            raise DevError('DOT_MAILBOX_ENTRY_INVALID', '收件箱对象标识无效', 422)
        reference = self._reference(reference)
        identifier = digest([dot['id'], row['generation'], kind, object_id, object_version])
        old = self.store.one('SELECT reference FROM dot_mailbox_entries WHERE id=?', (identifier,))
        if old:
            if old['reference'] != canonical(reference):
                raise DevError('DOT_MAILBOX_ENTRY_CONFLICT', '同一原对象版本不能改写为其他交接引用', 409)
            return row['dirty_revision']
        revision, now = row['dirty_revision'] + 1, self.c.clock()
        self.store.execute("""INSERT INTO dot_mailbox_entries
            (id,dot_id,generation,kind,object_id,object_version,reference,revision,created)
            VALUES (?,?,?,?,?,?,?,?,?)""", (identifier, dot['id'], row['generation'],
                                           kind, object_id, object_version, canonical(reference), revision, now))
        self.store.execute("""UPDATE dot_mailbox_bindings SET dirty_revision=?,wake_count=0,
            last_wake_at=?,updated=? WHERE dot_id=? AND generation=?""",
                           (revision, now, now, dot['id'], row['generation']))
        if row['state'] == 'active':
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['room_id'],))
            self._wake(room, dot, self.binding(dot['id']))
        return revision

    def _wake(self, room, dot, binding, *, recovery=0):
        self.c.emit(room, MAILBOX_EVENT,
                    'mailbox:' + digest([dot['id'], binding['generation'], binding['dirty_revision'],
                                         binding['wake_generation'], recovery]),
                    binding['dirty_revision'], {
                        'schema_version': 2, 'room_id': room['id'], 'conversation_id': dot['conversation_id'],
                        'recipient_slot_id': dot['id'], 'recipient_grant_id': binding['grant_id'],
                        'binding_generation': binding['generation'],
                        'dirty_revision': binding['dirty_revision']},
                    grant_id=binding['grant_id'])

    def _scan(self, sql, arguments):
        cursor = self.store.execute(sql, arguments)
        while rows := cursor.fetchmany(64):
            for row in rows:
                yield dict(row)

    def catch_up(self, room, dot):
        # Exact addressed inputs and exact slot policy only; never replay an
        # arbitrary room history at enrollment. Unique keys make renewals inert.
        for message in self._scan("""SELECT m.* FROM collaboration_dot_messages d
                JOIN collaboration_messages m ON m.id=d.message_id
                WHERE d.dot_id=? AND d.state!='handled' ORDER BY d.sequence""", (dot['id'],)):
            self.mark_dirty(dot, 'message', message['id'], message['version'],
                            {'message_id': message['id'], 'thread_root_id': message['thread_root_id'] or message['id']})
        for work in self._scan("""SELECT w.* FROM coordination_work w
                JOIN delegation_requests d ON d.work_item_id=w.id
                JOIN delegation_policies p ON p.id=d.policy_id
                WHERE p.slot_id=? AND w.state='queued' ORDER BY w.created,w.id""", (dot['id'],)):
            self.mark_dirty(dot, 'work_ready', work['id'], work['version'],
                            {'goal_id': work['goal_id'], 'approval_id': work['approval_id'],
                             'work_item_id': work['id']})
        for work in self._scan("""SELECT w.* FROM coordination_work w
                JOIN team_work_assignments a ON a.work_id=w.id
                WHERE a.dot_id=? AND w.state='queued' ORDER BY w.created,w.id""", (dot['id'],)):
            self.mark_dirty(dot, 'work_ready', work['id'], work['version'],
                            {'goal_id': work['goal_id'], 'approval_id': work['approval_id'],
                             'work_item_id': work['id']})
        for handoff in self._scan("""SELECT h.* FROM team_handoffs h
                JOIN coordination_goals g ON g.id=h.goal_id JOIN team_goals t ON t.goal_id=g.id
                WHERE h.recipient_dot_id=? AND g.state='active' AND h.intent_revision=t.intent_revision
                ORDER BY h.created,h.id""", (dot['id'],)):
            self.mark_dirty(dot, 'peer_handoff', handoff['id'], 1, {'goal_id': handoff['goal_id']})
        for operation in self._scan("""SELECT o.* FROM operations o
                JOIN coordination_operations co ON co.operation_id=o.id
                WHERE o.state IN ('succeeded','failed','cancelled','needs_review','interrupted')
                AND (EXISTS(SELECT 1 FROM team_work_assignments a WHERE a.work_id=co.work_item_id AND a.dot_id=?)
                    OR EXISTS(SELECT 1 FROM delegation_requests d JOIN delegation_policies p ON p.id=d.policy_id
                        WHERE d.work_item_id=co.work_item_id AND p.slot_id=?))
                ORDER BY o.created,o.id""", (dot['id'], dot['id'])):
            self.operation_completed(operation)

    def operation_completed(self, operation):
        """Route only a durable original work operation, never its output."""
        if operation.get('state') not in {'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'}:
            return 0
        link = self.store.one('SELECT * FROM coordination_operations WHERE operation_id=?', (operation['id'],))
        if not link:
            return 0
        assignment = self.store.one('SELECT dot_id FROM team_work_assignments WHERE work_id=?', (link['work_item_id'],))
        if not assignment:
            assignment = self.store.one("""SELECT p.slot_id AS dot_id FROM delegation_requests d
                JOIN delegation_policies p ON p.id=d.policy_id WHERE d.work_item_id=?""", (link['work_item_id'],))
        binding = self.binding(assignment['dot_id']) if assignment else None
        if not binding:
            return 0
        try:
            actor, room, dot = self._actor(binding)
            goal = self.c.coordination.object(room, link['goal_id'])
            self.c.coordination.authorize(actor, room, goal, active=False)
            work = self.c.coordination.work(goal, link['work_item_id'])
            self.c.coordination.work_capabilities(actor, goal, work)
            if (goal['conversation_id'] != dot['conversation_id'] or goal['approval_id'] != link['approval_id']
                    or operation.get('grant_id') != binding['grant_id'] or link['grant_id'] != binding['grant_id']
                    or operation.get('space_id', 'legacy') != room['space_id']
                    or operation.get('owner_user_id') != room['owner_user_id']
                    or operation.get('project_id') != link['project_id'] or work['project_id'] != link['project_id']):
                return 0
        except DevError:
            return 0
        return self.mark_dirty(dot, 'result_ready', 'operation:' + operation['id'] + ':' + operation['state'],
            operation.get('output_seq', 0), {'goal_id': goal['id'], 'approval_id': link['approval_id'],
                'work_item_id': work['id'], 'operation_id': operation['id'], 'reason_code': operation['state']})

    def on_event(self, room, name, object_id, version, payload, grant_id=''):
        if name == MAILBOX_EVENT or payload.get('test'):
            return
        if name == MESSAGE_EVENT:
            dot = self.c.dots.find(payload['recipient_slot_id'])
            if dot and dot['room_id'] == room['id']:
                self.mark_dirty(dot, 'message', payload['message_id'], payload['message_version'],
                                {'message_id': payload['message_id'], 'thread_root_id': payload['thread_root_id']})
            return
        if name == DELEGATION_EVENT:
            dot = self.c.dots.find(payload['recipient_slot_id'])
            if dot and dot['room_id'] == room['id']:
                self.mark_dirty(dot, 'work_ready', payload['work_item_id'], payload['work_item_version'],
                                {key: payload[key] for key in ('goal_id', 'approval_id', 'work_item_id')})
            return
        if name == OPERATION_EVENT:
            operation = self.store.one('SELECT * FROM operations WHERE id=?', (payload['operation_id'],))
            if operation:
                self.operation_completed(operation)
            return
        if name not in {WORK_EVENT, OPERATION_EVENT} or payload.get('test'):
            return
        conversation_id = payload.get('conversation_id', '')
        rows = self.store.all("""SELECT * FROM dot_mailbox_bindings
            WHERE room_id=? AND grant_id=? AND (?='' OR conversation_id=?)""",
                              (room['id'], grant_id, conversation_id, conversation_id))
        # Same-grant teams must supply their explicit participant slot through
        # mark_dirty; broadcasting by grant would misattribute a work item.
        if len(rows) != 1:
            return
        dot = self.c.dots.find(rows[0]['dot_id'])
        if payload['reason'] == 'peer_message':
            self.mark_dirty(dot, 'peer_handoff', payload['message_id'], version,
                            {key: payload[key] for key in ('goal_id', 'approval_id', 'message_id')})
        else:
            self.mark_dirty(dot, 'work_ready', payload['work_item_id'], payload['work_item_version'],
                            {key: payload[key] for key in ('goal_id', 'approval_id', 'work_item_id')})

    def _actor(self, binding):
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (binding['room_id'],))
        dot = self.c.dots.find(binding['dot_id'])
        slot = self.c.joining.slot(room, dot['id'])
        actor = self.c.grant_reader(room, binding['grant_id'], snapshot=json.loads(slot['principal']))
        self.c.dot_chat.authorize(actor, room, dot, connector=True)
        return actor, room, dot

    def _message_summaries(self, rows, principal):
        ids = list(dict.fromkeys(row['object_id'] for row in rows if row['kind'] == 'message'))
        if not ids:
            return {}
        messages = self.store.all('SELECT * FROM collaboration_messages WHERE id IN ('
                                  + ','.join('?' for _ in ids) + ')', tuple(ids))
        visible = [message['id'] for message in messages if self.c.chatroom.message_visible(principal, message)]
        return self.c.message_handling.summaries(visible, principal)

    def _entry(self, row, principal, room, dot, *, handling_summaries=None):
        if row['acknowledged_at'] is not None:
            return None
        ref = json.loads(row['reference'])
        result = {'id': row['id'], 'kind': row['kind'], 'object_id': row['object_id'],
                  'object_version': row['object_version'], 'revision': row['revision'],
                  'reference': ref, 'ready': True}
        scope = {'project': room['project_id'], 'environment_id': room['environment_id']}
        if row['kind'] == 'message':
            message = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (row['object_id'],))
            receipt = self.store.one('SELECT * FROM collaboration_dot_messages WHERE dot_id=? AND message_id=?',
                                     (dot['id'], row['object_id']))
            if not message or not receipt or message['version'] != row['object_version']:
                return None
            self.c.chatroom.require_message_visible(principal, message)
            summaries = (self.c.message_handling.summaries([message['id']], principal)
                         if handling_summaries is None else handling_summaries)
            handling = summaries.get((dot['id'], message['id']))
            if handling:
                if handling['state'] in {'waiting_input', 'waiting_authorization', 'work_linked'}:
                    result['ready'] = False
                elif handling['content_completed'] or handling['state'] == 'legacy_handled':
                    return None
            result['read_request'] = self.c.dots.request(room, dot)
        elif row['kind'] in {'work_ready', 'peer_handoff', 'result_ready'}:
            goal = self.c.coordination.object(room, ref.get('goal_id', ''))
            self.c.coordination.authorize(principal, room, goal, active=row['kind'] != 'result_ready')
            if ref.get('approval_id') and goal['approval_id'] != ref['approval_id']:
                return None
            if row['kind'] == 'work_ready':
                work = self.c.coordination.work(goal, row['object_id'])
                if (work['state'] != 'queued' or work['version'] != row['object_version'] or
                        work['assignee_grant_id'] != principal.grant_id):
                    return None
                self.c.coordination.work_capabilities(principal, goal, work)
                assigned = self.store.one('SELECT dot_id FROM team_work_assignments WHERE work_id=?', (work['id'],))
                if assigned and assigned['dot_id'] != dot['id']:
                    return None
                delegated = self.store.one("""SELECT p.slot_id FROM delegation_requests d
                    JOIN delegation_policies p ON p.id=d.policy_id WHERE d.work_item_id=?""", (work['id'],))
                if delegated and delegated['slot_id'] != dot['id']:
                    return None
                result['ready'] = all(self.c.coordination.work(goal, identifier)['state'] == 'succeeded'
                                      for identifier in json.loads(work['dependencies']))
            elif row['kind'] == 'peer_handoff':
                handoff = self.store.one('SELECT 1 FROM team_handoffs WHERE id=?', (row['object_id'],))
                if handoff:
                    result['handoff'] = self.c.coordination.teams.read_handoff(room, dot, row['object_id'], principal)
                    result['read_request'] = {'tool': 'collaboration_query',
                        'arguments': {**scope, 'action': 'goal', 'goal_id': goal['id']}}
                    result['ack_request'] = {'tool': 'collaboration', 'arguments': {
                        **scope, 'action': 'dot_mailbox_ack', 'dot_id': dot['id'],
                        'binding_generation': row['generation'], 'entry_ids': [row['id']],
                        'idempotency_key': 'mailbox-ack:' + row['id']}}
                    return result
                message = self.store.one('SELECT * FROM coordination_messages WHERE id=? AND goal_id=?',
                                         (row['object_id'], goal['id']))
                if not message or principal.grant_id not in json.loads(message['mention_grant_ids']):
                    return None
            elif ref.get('operation_id'):
                operation = self.store.one('SELECT * FROM operations WHERE id=?', (ref['operation_id'],))
                if not operation or operation['state'] not in {'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'}:
                    return None
                if ref.get('reason_code') and ref['reason_code'] != operation['state']:
                    return None
            elif ref.get('work_item_id'):
                work = self.c.coordination.work(goal, ref['work_item_id'])
                if work['state'] not in {'succeeded', 'failed', 'blocked', 'cancelled', 'expired'}:
                    return None
                if row['object_id'] == work['id'] and row['object_version'] != work['version']:
                    return None
            result['read_request'] = {'tool': 'collaboration_query',
                                      'arguments': {**scope, 'action': 'goal', 'goal_id': goal['id']}}
        else:
            if ref.get('reason_code') == 'subscription_expiring':
                binding = self.binding(dot['id'])
                sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',
                                     (binding['subscription_id'],))
                if not sub or sub['version'] != row['object_version']:
                    return None
                result['maintenance'] = {'kind': 'renew_original_native_subscription',
                    'expires_at': sub['expires_at'], 'subscription_request': self.subscription(room, dot),
                    'permissions_changed': False, 'automatically_renewed': False}
            result['read_request'] = self.c.dots.request(room, dot, 'dot_connection')
        if row['kind'] in {'peer_handoff', 'result_ready', 'control_notice'}:
            result['ack_request'] = {'tool': 'collaboration', 'arguments': {
                **scope, 'action': 'dot_mailbox_ack', 'dot_id': dot['id'],
                'binding_generation': row['generation'], 'entry_ids': [row['id']],
                'idempotency_key': 'mailbox-ack:' + row['id']}}
        return result

    def has_ready(self, dot_id, generation=None, after_revision=None):
        binding = self.binding(dot_id)
        if not binding:
            return False
        self.check_generation(binding, generation)
        try:
            actor, room, dot = self._actor(binding)
        except DevError:
            return False
        after = 0
        while True:
            rows = self.store.all("""SELECT * FROM dot_mailbox_entries
                WHERE dot_id=? AND generation=? AND acknowledged_at IS NULL AND revision>?
                AND sequence>? ORDER BY sequence LIMIT 64""",
                (dot_id, binding['generation'], after_revision or 0, after))
            summaries = self._message_summaries(rows, actor)
            for row in rows:
                try:
                    item = self._entry(row, actor, room, dot, handling_summaries=summaries)
                except DevError:
                    continue
                if item and item['ready']:
                    return True
            if len(rows) < 64:
                break
            after = rows[-1]['sequence']
        return False

    def recover(self, *, max_wakes=4):
        """Three bounded reminders for an idle, still-authorized exact route.

        A permanent rejection, owner pause, expired subscription or live
        consumer lease is never bypassed with a new event identity.
        """
        if not self.c.config.enabled or not self.c.config.events_enabled or max_wakes <= 0:
            return 0
        with self.store.transaction():
            awakened = self._maintain(max_wakes)
            if awakened >= max_wakes:
                return awakened
            rows = self.store.all("""SELECT * FROM dot_mailbox_bindings
                WHERE state='active' AND dirty_revision>drained_revision
                AND wake_count<3 AND dot_id>? ORDER BY dot_id LIMIT 64""", (self._after,))
            self._after = rows[-1]['dot_id'] if len(rows) == 64 else ''
            now = self.c.clock()
            for binding in rows:
                if awakened >= max_wakes:
                    break
                if now - binding['last_wake_at'] < (30, 120, 600)[binding['wake_count']]:
                    continue
                if self.store.one("""SELECT 1 FROM dot_consumer_sessions
                        WHERE dot_id=? AND state='active' AND lease_until>?""", (binding['dot_id'], now)):
                    continue
                sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',
                                     (binding['subscription_id'],))
                if not sub or sub['state'] != 'active' or sub['expires_at'] <= now:
                    continue
                rejected = self._permanently_rejected(sub['id'])
                if rejected:
                    continue
                try:
                    actor, room, dot = self._actor(binding)
                    self.c.live_room(room)
                    self.c.events.authorize(sub, room)
                    if not self.has_ready(dot['id'], binding['generation']):
                        continue
                except DevError:
                    continue
                count = binding['wake_count'] + 1
                self.store.execute("""UPDATE dot_mailbox_bindings SET wake_count=?,last_wake_at=?,updated=?
                    WHERE dot_id=? AND generation=? AND dirty_revision=?""",
                                   (count, now, now, dot['id'], binding['generation'], binding['dirty_revision']))
                self._wake(room, dot, binding, recovery=count)
                awakened += 1
            return awakened

    def _maintain(self, max_wakes):
        """Prompt the exact host before expiry, once per subscription version.

        Native renewal still belongs to that host. For short TTLs the prompt
        starts in the final fifth of the lease, avoiding an immediate loop.
        """
        now = self.c.clock()
        rows = self.store.all("""SELECT b.dot_id,s.id AS subscription_id,s.version,s.expires_at,s.updated
            FROM dot_mailbox_bindings b JOIN mcp_event_subscriptions s ON s.id=b.subscription_id
            WHERE b.state='active' AND s.state='active' AND s.expires_at>? AND s.expires_at<=?
            AND b.dot_id>? ORDER BY b.dot_id LIMIT 64""", (now, now + 3600, self._maintenance_after))
        self._maintenance_after = rows[-1]['dot_id'] if len(rows) == 64 else ''
        awakened = 0
        for row in rows:
            if awakened >= max_wakes:
                break
            window = min(3600, max(15, (row['expires_at'] - row['updated']) * .2))
            if row['expires_at'] - now > window:
                continue
            object_id = 'subscription-expiry:' + row['subscription_id']
            if self.store.one("""SELECT 1 FROM dot_mailbox_entries WHERE dot_id=? AND kind='control_notice'
                    AND object_id=? AND object_version=?""", (row['dot_id'], object_id, row['version'])):
                continue
            binding = self.binding(row['dot_id'])
            if not self.health(binding)['available'] or self._permanently_rejected(row['subscription_id']):
                continue
            self.mark_dirty(row['dot_id'], 'control_notice', object_id, row['version'],
                            {'reason_code': 'subscription_expiring', 'recipient_slot_id': row['dot_id']})
            awakened += 1
        return awakened

    def request_recovery(self, raw, principal):
        """Explicit recovery sends to the original route, never to this caller."""
        args = validate(DotMailboxRecover, raw)
        with self.store.transaction():
            _, principal, room, _, dot = self.c.dots.scope(
                {key: args[key] for key in ('project', 'environment_id', 'dot_id')}, principal)
            self.c.dot_chat.authorize(principal, room, dot, connector=bool(principal.grant_id))
            binding = self.binding(dot['id'])
            if not binding or binding['state'] != 'active':
                raise DevError('DOT_MAILBOX_NOT_ACTIVE', '原生统一收件箱订阅尚未激活', 409)
            self.check_generation(binding, args['binding_generation'])
            health = self.health(binding)
            if not health['available']:
                raise DevError('DOT_MAILBOX_ROUTE_UNAVAILABLE', '请先由宿主恢复原订阅；补发不会恢复暂停或扩大范围', 409)

            def save():
                now = self.c.clock()
                if (not self.has_ready(dot['id'], binding['generation']) or self.store.one("""SELECT 1
                        FROM dot_consumer_sessions WHERE dot_id=? AND state='active' AND lease_until>?""",
                        (dot['id'], now))):
                    return {'dot_id': dot['id'], 'wakeup_queued': False, 'consumer_state': 'no_idle_ready_work'}
                count = binding['manual_wakes'] if now - binding['manual_window_at'] < 3600 else 0
                if now - binding['last_manual_wake_at'] < 600 or count >= 3:
                    raise DevError('DOT_MAILBOX_RECOVERY_BUDGET', '原订阅补发提示暂受限；保留待办并稍后恢复', 429)
                rejected = self._permanently_rejected(binding['subscription_id'])
                if rejected:
                    raise DevError('DOT_MAILBOX_ROUTE_REJECTED', '原回调已永久拒绝；请由宿主核对原订阅', 409)
                self.store.execute("""UPDATE dot_mailbox_bindings SET wake_generation=wake_generation+1,
                    manual_wakes=?,manual_window_at=?,last_manual_wake_at=?,last_wake_at=?,updated=? WHERE dot_id=?""",
                    (count + 1, binding['manual_window_at'] if count else now, now, now, now, dot['id']))
                self._wake(room, dot, self.binding(dot['id']))
                return {'dot_id': dot['id'], 'wakeup_queued': True,
                        'delivery_scope': 'original_subscription', 'consumer_verified': False}

            return self.c.mutation(principal, room, 'dot_mailbox_recover:' + dot['id'], args, save)

    def _permanently_rejected(self, subscription_id):
        return self.store.one("""SELECT 1 FROM mcp_event_deliveries
            WHERE subscription_id=? AND state='dead_letter'
            AND ((status_code>=300 AND status_code<500 AND status_code NOT IN (408,425,429))
                OR reason_code IN ('callback_policy_rejected','payload_too_large')) LIMIT 1""", (subscription_id,))

    def mark_drained(self, dot_id, generation, expected_revision):
        row = self.binding(dot_id)
        if not row:
            return False
        self.check_generation(row, generation)
        if row['dirty_revision'] != expected_revision or self.has_ready(dot_id, generation):
            return False
        return bool(self.store.execute("""UPDATE dot_mailbox_bindings SET drained_revision=?,updated=?
            WHERE dot_id=? AND generation=? AND dirty_revision=?""",
                                       (expected_revision, self.c.clock(), dot_id, generation, expected_revision)).rowcount)

    def read(self, raw, principal):
        args = validate(DotMailboxRead, raw)
        with self.store.transaction(immediate=False):
            _, principal, room, _, dot = self.c.dots.scope(
                {key: args[key] for key in ('project', 'environment_id', 'dot_id')}, principal)
            self.c.dot_chat.authorize(principal, room, dot, connector=bool(principal.grant_id))
            binding = self.binding(dot['id'])
            if not binding:
                raise DevError('DOT_MAILBOX_NOT_CONFIGURED', '此 dot 仍使用原消息收件箱', 409)
            self.check_generation(binding, args['binding_generation'])
            cursor_binding = digest(['dot-mailbox-page-v2', binding['generation'],
                                     principal.grant_id or principal.user_id,
                                     self.c.message_handling.scope_digest(room, dot)])
            after = read_cursor(self.c.secret, cursor_binding, args['cursor']) if args['cursor'] else 0
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise DevError('INVALID_CURSOR', '收件箱游标无效', 400)
            rows = self.store.all("""SELECT * FROM dot_mailbox_entries
                WHERE dot_id=? AND generation=? AND sequence>? ORDER BY sequence LIMIT ?""",
                                  (dot['id'], binding['generation'], after, args['limit'] + 1))
            selected, items, scanned_after, byte_count = rows[:args['limit']], [], after, 0
            budget_limited = False
            summaries = self._message_summaries(selected, principal)
            for row in selected:
                try:
                    item = self._entry(row, principal, room, dot, handling_summaries=summaries)
                except DevError as exc:
                    if exc.status not in {403, 404, 409}:
                        raise
                    scanned_after = row['sequence']
                    continue
                if item:
                    size = len(canonical(item).encode())
                    if size > ITEM_BYTE_BUDGET:
                        item = {key: value for key, value in item.items() if key != 'handoff'}
                        item['details_omitted'] = True
                        size = len(canonical(item).encode())
                    if byte_count + size > ITEM_BYTE_BUDGET:
                        budget_limited = True
                        break
                    items.append(item)
                    byte_count += size
                scanned_after = row['sequence']
            more = budget_limited or len(rows) > args['limit']
            cursor = sign_cursor(self.c.secret, cursor_binding, scanned_after) if more else None
            resume = {'tool': 'collaboration_query', 'arguments': {
                'action': 'dot_mailbox', 'project': room['project_id'],
                'environment_id': room['environment_id'], 'dot_id': dot['id'],
                'binding_generation': binding['generation'], 'limit': args['limit']}}
            return {'schema_version': 2, 'dot_id': dot['id'], 'binding_generation': binding['generation'],
                    'contract_hash': CONTRACT_HASH, 'page_byte_budget': PAGE_BYTE_BUDGET,
                    'items_bytes': byte_count, 'budget_limited': budget_limited,
                    'dirty_revision': binding['dirty_revision'], 'drained_revision': binding['drained_revision'],
                    'items': items, 'next_cursor': cursor,
                    'next_page_request': {'tool': resume['tool'], 'arguments': {**resume['arguments'], 'cursor': cursor}} if cursor else None,
                    'resume_request': resume, 'message_inbox_request': self.c.dots.request(room, dot),
                    'pagination_complete': cursor is None, 'read_only': True,
                    'grants_authority': False, 'read_is_consumption': False}

    def ack(self, raw, principal):
        args = validate(DotMailboxAck, raw)
        with self.store.transaction():
            _, principal, room, _, dot = self.c.dots.scope(
                {key: args[key] for key in ('project', 'environment_id', 'dot_id')}, principal)
            self.c.dot_chat.authorize(principal, room, dot, connector=True)
            binding = self.binding(dot['id'])
            if not binding or binding['state'] != 'active':
                raise DevError('DOT_MAILBOX_NOT_ACTIVE', '原生统一收件箱订阅尚未激活', 409)
            self.check_generation(binding, args['binding_generation'])
            self.c.dot_consumers.guard(args, principal, room, dot, require_revision=False)
            rows = []
            for identifier in dict.fromkeys(args['entry_ids']):
                row = self.store.one('SELECT * FROM dot_mailbox_entries WHERE id=? AND dot_id=? AND generation=?',
                                     (identifier, dot['id'], binding['generation']))
                if not row or row['kind'] not in {'peer_handoff', 'result_ready', 'control_notice'}:
                    raise DevError('DOT_MAILBOX_ACK_DENIED', '只可确认本 dot 的协作提示；用户输入及工作必须走原处理流程', 403)
                if row['acknowledged_at'] is None and self._entry(row, principal, room, dot) is None:
                    raise DevError('DOT_MAILBOX_ENTRY_OBSOLETE', '原协作提示已失效，请重新读取', 409)
                rows.append(row)

            def save():
                for row in rows:
                    self.store.execute("""UPDATE dot_mailbox_entries SET acknowledged_at=COALESCE(acknowledged_at,?)
                        WHERE id=? AND dot_id=? AND generation=?""",
                                       (self.c.clock(), row['id'], dot['id'], binding['generation']))
                return {'dot_id': dot['id'], 'entry_ids': [row['id'] for row in rows],
                        'acknowledged': True, 'work_completed': False}
            # Proof is a transport credential, not part of the permanent retry
            # fingerprint or audit. Current proof was checked before this call.
            mutation = {key: value for key, value in args.items()
                        if key not in {'consumer_session_id', 'consumer_fence', 'consumer_proof'}}
            return self.c.mutation(principal, room, 'dot_mailbox_ack:' + dot['id'], mutation, save)
