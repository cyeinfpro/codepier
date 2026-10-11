"""Read-only recovery projections and bounded wakes over original work identities.

A status read is never a consumer heartbeat. Agent observation times are written
only by authenticated operation packets after the existing journal fence. This
module neither starts operations nor changes credentials or native subscriptions.
"""
from __future__ import annotations

import json

from hub.collaboration.common import MESSAGE_EVENT, digest, read_cursor, sign_cursor, validate
from shared import collaboration_contracts as contracts
from shared.public_collaboration import request as public_request
from shared.util import DevError

UNCERTAIN = frozenset({'unknown', 'needs_review', 'reconnecting'})
SETTLED = frozenset({'succeeded', 'failed', 'cancelled'})
WAKE_DELAYS = (30, 120, 600)


def migrate(db):
    # Null means no historical proof. Do not backfill with migration/read time.
    additions = {
        'operations': {'last_operation_verified_at': 'REAL', 'last_output_at': 'REAL'},
        'coordination_work': {'last_consumer_heartbeat_at': 'REAL', 'last_progress_at': 'REAL', 'last_consumer_contact_at': 'REAL',
                              'last_progress_summary': "TEXT NOT NULL DEFAULT ''"},
        'coordination_operations': {'operation_role': "TEXT NOT NULL DEFAULT 'required'",
                                    'supersedes_operation_id': "TEXT NOT NULL DEFAULT ''",
                                    'execution_budget': 'TEXT'},
    }
    for table, fields in additions.items():
        existing = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
        # Standalone legacy collaboration migrations have no Runtime tables.
        # The real Store creates operations before calling this additive upgrade.
        if table == 'operations' and not existing:
            continue
        for name, declaration in fields.items():
            if name not in existing:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + declaration)
    db.execute('CREATE INDEX IF NOT EXISTS coordination_operations_work ON coordination_operations(work_item_id,operation_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS dot_delivery_receipts (
        dot_id TEXT NOT NULL, kind TEXT NOT NULL, request_key_hash TEXT NOT NULL,
        request_digest TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES collaboration_messages(id),
        message_version INTEGER NOT NULL, committed_at REAL NOT NULL,
        PRIMARY KEY(dot_id,kind,request_key_hash))''')
    db.execute('''CREATE TABLE IF NOT EXISTS dot_work_recovery_wakes (
        dot_id TEXT NOT NULL, work_item_id TEXT NOT NULL REFERENCES coordination_work(id),
        episode TEXT NOT NULL, wake_count INTEGER NOT NULL DEFAULT 0,
        last_wake_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(dot_id,work_item_id))''')


def freshness(observed_at, now, seconds):
    if observed_at is None or observed_at <= 0 or observed_at > now:
        return 'unknown'
    return 'fresh' if now - observed_at < seconds else 'stale'


class TaskLiveness:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def record_receipt(self, dot, args, kind, message):
        """Called inside the same transaction as the actual accepted publication."""
        key_hash, fingerprint = digest(args['idempotency_key']), digest(args)
        old = self.store.one('''SELECT * FROM dot_delivery_receipts
            WHERE dot_id=? AND kind=? AND request_key_hash=?''', (dot['id'], kind, key_hash))
        if old:
            if old['request_digest'] != fingerprint or old['message_id'] != message['id']:
                raise DevError('IDEMPOTENCY_CONFLICT', '原投递键不能替换已确认内容', 409)
            return
        self.store.execute('INSERT INTO dot_delivery_receipts VALUES (?,?,?,?,?,?,?)',
            (dot['id'], kind, key_hash, fingerprint, message['id'], message['version'],
             message['created'] if kind in {'message', 'message_v2'} else self.c.clock()))

    def receipt(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, slot, dot = self.c.dot_chat.scope(raw, principal, contracts.DotDeliveryReceipt)
            row = self.store.one('''SELECT * FROM dot_delivery_receipts
                WHERE dot_id=? AND kind=? AND request_key_hash=?''',
                (dot['id'], args['kind'], digest(args['request_key'])))
            if row:
                message_id = row['message_id']
            elif args['kind'] == 'message':
                legacy = self.store.one('''SELECT id FROM collaboration_messages WHERE room_id=?
                    AND conversation_id=? AND author=? AND origin='bound_connector' AND source_id=?''',
                    (room['id'], dot['conversation_id'], slot['grant_id'], 'dot:' + digest([dot['id'], args['request_key']])))
                message_id = legacy['id'] if legacy else None
            elif args['kind'] == 'update':
                mutation_key = digest([room['space_id'], room['owner_user_id'], slot['grant_id'],
                    'dot_reply_update:' + dot['id'], room['id'], args['request_key']])
                legacy = self.store.one('SELECT response FROM collaboration_idempotency WHERE id=? AND room_id=?',
                                        (mutation_key, room['id']))
                message_id = json.loads(legacy['response']).get('message_id') if legacy else None
            else:
                message_id = None  # v2 has no pre-receipt legacy records.
            result = {'dot_id': dot['id'], 'kind': args['kind'], 'committed': bool(message_id),
                      'read_only': True, 'grants_authority': False, 'server_now': self.c.clock(),
                      'host_outbox_durability': 'not_verified',
                      'receipt_state': 'committed' if message_id else 'not_committed',
                      'authoritative_lookup': True, 'safe_to_resend_same_request': not bool(message_id)}
            if not message_id:
                return result
            message = self.c.object('collaboration_messages', room, message_id)
            self.c.chatroom.require_message_visible(principal, message)
            if (message['conversation_id'] != dot['conversation_id'] or message['author'] != slot['grant_id']
                    or json.loads(message['body']).get('sender_dot_id') != dot['id']):
                raise DevError('DOT_RECEIPT_SCOPE_CHANGED', '投递回执不再属于当前 dot', 403)
            return {**result, 'message_id': message_id, 'current_version': message['version'],
                    'committed_version': row['message_version'] if row else None,
                    'request_digest': row['request_digest'] if row else None,
                    'committed_at': row['committed_at'] if row else None, 'legacy_receipt': row is None}

    def work_summary(self, items):
        """Two batched queries; no operation output, command, file path or credential."""
        if not items:
            return {}
        ids = [item['id'] for item in items]
        placeholders = ','.join('?' for _ in ids)
        aggregates = self.store.all('''SELECT c.work_item_id,COUNT(*) AS operation_count,
            SUM(CASE WHEN o.state NOT IN ('succeeded','failed','cancelled','queued','running','cancelling') THEN 1 ELSE 0 END) AS uncertain_count,
            SUM(CASE WHEN o.state IN ('queued','running','cancelling') THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN o.state='failed' THEN 1 ELSE 0 END) AS failed_count,
            MAX(o.last_operation_verified_at) AS last_operation_verified_at,
            MIN(CASE WHEN o.state NOT IN ('succeeded','failed','cancelled')
                THEN COALESCE(o.last_operation_verified_at,0) ELSE NULL END) AS oldest_pending_verified_at,
            MAX(o.last_output_at) AS last_output_at,
            MAX(CASE WHEN o.state IN ('succeeded','failed','cancelled') THEN o.updated ELSE NULL END) AS last_result_recorded_at,
            MAX(d.last_seen) AS last_node_contact_at
            FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
            LEFT JOIN devices d ON d.id=o.device_id WHERE c.work_item_id IN (''' + placeholders + ''')
            GROUP BY c.work_item_id''', tuple(ids))
        by_id, now, result = {row['work_item_id']: row for row in aggregates}, self.c.clock(), {}
        for item in items:
            proof = by_id.get(item['id'], {'operation_count': 0, 'uncertain_count': 0, 'pending_count': 0,
                'failed_count': 0, 'last_operation_verified_at': None, 'last_output_at': None,
                'last_result_recorded_at': None, 'last_node_contact_at': None, 'oldest_pending_verified_at': None})
            proof = {k: v for k, v in proof.items() if k != 'work_item_id'}
            if proof['oldest_pending_verified_at'] == 0:
                proof['oldest_pending_verified_at'] = None
            verified = (proof['oldest_pending_verified_at']
                        if proof['pending_count'] or proof['uncertain_count'] else proof['last_operation_verified_at'])
            last_heartbeat = item.get('last_consumer_heartbeat_at')
            if item['result']:
                waiting = 'result_committed'
            elif proof['uncertain_count']:
                waiting = 'original_operation_uncertain'
            elif proof['operation_count'] and not proof['pending_count']:
                waiting = 'awaiting_result_publication'
            elif proof['pending_count']:
                waiting = 'original_operation_pending'
            else:
                waiting = 'awaiting_consumer'
            body = json.loads(item['result']) if item['result'] else {}
            result[item['id']] = {**proof, 'work_item_id': item['id'], 'goal_id': item['goal_id'],
                'project_id': item['project_id'], 'execution_state': item['state'],
                'delivery_state': 'committed' if item['result'] else 'pending',
                'execution_health': body.get('execution_health', 'unknown'),
                'acceptance_status': body.get('acceptance_status', 'unverified'),
                'attempt': item['attempt'], 'fencing_token': item['fencing_token'], 'lease_until': item['lease_until'],
                'last_consumer_heartbeat_at': last_heartbeat, 'last_progress_at': item.get('last_progress_at'),
                'last_progress_summary': item.get('last_progress_summary', ''),
                'last_dot_contact_at': item.get('last_consumer_contact_at'),
                'operation_freshness': freshness(verified, now, 45),
                'node_contact_freshness': freshness(proof['last_node_contact_at'], now, 45),
                'consumer_freshness': freshness(last_heartbeat, now, 90), 'wait_reason': waiting,
                'reason_code': item['reason_code'], 'model_online': 'unknown',
                'evidence_source': 'durable_original_operation_and_consumer_receipts',
                'observed_at': max((v for v in [verified, proof['last_output_at'], proof['last_result_recorded_at'],
                                                last_heartbeat, item.get('last_progress_at')] if v is not None), default=None)}
        return result

    def for_messages(self, messages, principal):
        if not messages:
            return []
        ids = [row['id'] for row in messages][:124]
        rows = self.store.all('''SELECT w.*,r.message_id,p.slot_id AS dot_id
            FROM delegation_requests r JOIN delegation_policies p ON p.id=r.policy_id
            JOIN coordination_work w ON w.id=r.work_item_id
            WHERE r.message_id IN (''' + ','.join('?' for _ in ids) + ')', tuple(ids))
        permitted = []
        for row in rows:
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=(SELECT room_id FROM coordination_goals WHERE id=?)',
                                  (row['goal_id'],))
            goal = self.c.coordination.object(room, row['goal_id'])
            try:
                self.c.coordination.authorize(principal, room, goal)
            except DevError:
                continue
            permitted.append(row)
        summaries = self.work_summary(permitted)
        scopes = {}
        for row in permitted:
            if row['dot_id'] not in scopes:
                dot = self.c.dots.find(row['dot_id'])
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
                scopes[row['dot_id']] = self.c.dots.scope_manifest(room, dot)
        return [{**summaries[row['id']], 'message_id': row['message_id'], 'dot_id': row['dot_id'],
                 'scope_boundary': scopes[row['dot_id']]} for row in permitted]

    def recovery(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, _, dot = self.c.dot_chat.scope(raw, principal, contracts.DotRecovery)
            binding = digest(['dot-work-recovery-v1', room['id'], dot['id'], dot['setup_digest'],
                              principal.grant_id or principal.user_id, args['include_completed']])
            after = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else ''
            if not isinstance(after, str) or len(after) > 128:
                raise DevError('INVALID_CURSOR', '恢复游标无效', 400)
            rows = self.store.all('''SELECT w.*,r.id AS delegation_id,r.message_id FROM coordination_work w
                JOIN delegation_requests r ON r.work_item_id=w.id
                JOIN delegation_policies p ON p.id=r.policy_id WHERE p.slot_id=? AND p.room_id=?
                AND p.conversation_id=? AND w.id>? AND (? OR w.result IS NULL)
                ORDER BY w.id LIMIT ?''',
                (dot['id'], room['id'], dot['conversation_id'], after, args['include_completed'], args['limit'] + 1))
            selected, permitted = rows[:args['limit']], []
            for row in selected:
                message = self.c.object('collaboration_messages', room, row['message_id'])
                try:
                    self.c.chatroom.require_message_visible(principal, message)
                    goal = self.c.coordination.object(room, row['goal_id'])
                    self.c.coordination.authorize(principal, room, goal)
                except DevError:
                    continue
                permitted.append(row)
            summaries = self.work_summary(permitted)
            base = {'project': room['project_id'], 'environment_id': room['environment_id'], 'dot_id': dot['id'],
                    'limit': args['limit'], 'include_completed': args['include_completed']}
            cursor = sign_cursor(self.c.secret, binding, selected[-1]['id']) if len(rows) > args['limit'] else None
            items = [{**summaries[row['id']], 'message_id': row['message_id'],
                'read_request': self.c.delegation.read_request(room, row['delegation_id']),
                'operations_request': public_request('collaboration_work_evidence', {
                    'project': room['project_id'], 'environment_id': room['environment_id'],
                    'goal_id': row['goal_id'], 'work_item_id': row['id']})} for row in permitted]
            return {'items': items, 'next_cursor': cursor, 'server_now': self.c.clock(), 'dot_id': dot['id'],
                'next_page_request': public_request('collaboration_dot_recovery', {**base, 'cursor': cursor}) if cursor else None,
                'resume_request': public_request('collaboration_dot_recovery', base),
                'read_only': True, 'grants_authority': False,
                'scope_boundary': self.c.dots.scope_manifest(room, dot)}

    def evidence(self, raw, principal):
        args = validate(contracts.WorkEvidence, raw)
        with self.store.transaction(immediate=False):
            principal, room, goal = self.c.coordination.scope(principal, args)
            item = self.c.coordination.work(goal, args['work_item_id'])
            binding = digest(['work-evidence-v1', goal['id'], item['id'], principal.grant_id or principal.user_id])
            after = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else ''
            if not isinstance(after, str) or len(after) > 128:
                raise DevError('INVALID_CURSOR', '操作证据游标无效', 400)
            rows = self.store.all('''SELECT o.id,o.state,o.last_operation_verified_at,o.last_output_at,
                c.attempt,c.fencing_token,c.operation_role,c.supersedes_operation_id,c.execution_budget FROM coordination_operations c
                JOIN operations o ON o.id=c.operation_id WHERE c.work_item_id=? AND o.id>? ORDER BY o.id LIMIT ?''',
                (item['id'], after, args['limit'] + 1))
            selected = rows[:args['limit']]
            cursor = sign_cursor(self.c.secret, binding, selected[-1]['id']) if len(rows) > args['limit'] else None
            base = {key: args[key] for key in ('project', 'environment_id', 'goal_id', 'work_item_id', 'limit')}
            return {'items': [{**row, 'operation_id': row['id'],
                              'execution_budget': json.loads(row['execution_budget']) if row['execution_budget'] else None}
                             for row in selected],
                'next_cursor': cursor,
                'next_page_request': public_request('collaboration_work_evidence', {**base, 'cursor': cursor}) if cursor else None,
                'resume_request': public_request('collaboration_work_evidence', base),
                'read_only': True, 'grants_authority': False, 'server_now': self.c.clock()}

    def recover(self, max_wakes=4):
        """Wakes are hints to read the original inbox, never automatic execution."""
        if max_wakes <= 0 or not self.c.config.enabled or not self.c.events or not self.c.config.events_enabled:
            return 0
        now, sent, awakened = self.c.clock(), 0, set()
        with self.store.transaction():
            after = getattr(self, '_after', '')
            rows = self.store.all('''SELECT w.*,r.message_id,p.slot_id AS dot_id,p.room_id
                FROM coordination_work w JOIN delegation_requests r ON r.work_item_id=w.id
                JOIN delegation_policies p ON p.id=r.policy_id
                WHERE w.result IS NULL AND w.state IN ('queued','leased','running','blocked') AND w.id>?
                ORDER BY w.id LIMIT 64''', (after,))
            self._after = rows[-1]['id'] if len(rows) == 64 else ''
            for item in rows:
                if item['dot_id'] in awakened:
                    continue
                activity = max(item.get('last_consumer_contact_at') or 0, item.get('last_consumer_heartbeat_at') or 0,
                               item.get('last_progress_at') or 0, item['created'])
                if now - activity < 300:
                    continue
                episode = digest([item['id'], item['attempt'], activity])
                saved = self.store.one('SELECT * FROM dot_work_recovery_wakes WHERE dot_id=? AND work_item_id=?',
                                       (item['dot_id'], item['id']))
                count = saved['wake_count'] if saved and saved['episode'] == episode else 0
                last = saved['last_wake_at'] if saved and saved['episode'] == episode else activity
                if count >= len(WAKE_DELAYS) or now - last < WAKE_DELAYS[count]:
                    continue
                try:
                    dot = self.c.dots.find(item['dot_id'])
                    room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (item['room_id'],))
                    slot = self.c.joining.slot(room, dot['id'])
                    actor = self.c.grant_reader(room, slot['grant_id'])
                    self.c.dot_chat.authorize(actor, room, dot, connector=True)
                    goal = self.c.coordination.object(room, item['goal_id'])
                    self.c.coordination.authorize(actor, room, goal, active=True)
                    message = self.c.object('collaboration_messages', room, item['message_id'])
                    self.c.chatroom.require_message_visible(actor, message)
                    routes = self.store.all('''SELECT * FROM mcp_event_subscriptions WHERE room_id=? AND grant_id=?
                        AND name=? AND state='active' AND expires_at>?''', (room['id'], slot['grant_id'], MESSAGE_EVENT, now))
                    live = []
                    for route in routes:
                        try:
                            filters = self.c.events.authorize(route, room)
                        except DevError:
                            continue
                        if filters.get('slot_id') == dot['id'] and filters.get('conversation_id') == dot['conversation_id']:
                            live.append(route)
                    if not live:
                        continue
                    refused = self.store.one('''SELECT 1 FROM mcp_event_deliveries d
                        JOIN mcp_event_outbox e ON e.id=d.event_id JOIN mcp_event_subscriptions s ON s.id=d.subscription_id
                        WHERE e.room_id=? AND e.name=? AND json_extract(e.data,'$.message_id')=?
                        AND json_extract(e.data,'$.recipient_slot_id')=? AND d.created>=s.updated
                        AND (d.status_code IN (410,413) OR d.reason_code='callback_policy_rejected') LIMIT 1''',
                        (room['id'], MESSAGE_EVENT, message['id'], dot['id']))
                    if refused:
                        continue
                    budget = self.store.one('''SELECT COUNT(*) AS n FROM mcp_event_outbox e JOIN collaboration_rooms r ON r.id=e.room_id
                        WHERE r.space_id=? AND r.owner_user_id=? AND r.project_id=? AND e.name=? AND e.created>?''',
                        (room['space_id'], room['owner_user_id'], room['project_id'], MESSAGE_EVENT, now - 60))['n']
                    if budget >= 30:
                        continue
                    generation = count + 1
                    self.c.emit(room, MESSAGE_EVENT, 'dot-work-recover:' + digest([dot['id'], item['id'], episode, generation]), generation,
                        {'conversation_id': dot['conversation_id'], 'room_id': room['id'], 'message_id': message['id'],
                         'message_version': message['version'], 'recipient_slot_id': dot['id'],
                         'thread_root_id': message['thread_root_id']}, grant_id=slot['grant_id'])
                    # Coalesce all currently eligible work for this dot under this wake.
                    for peer in rows:
                        if peer['dot_id'] != dot['id']:
                            continue
                        pa = max(peer.get('last_consumer_contact_at') or 0, peer.get('last_consumer_heartbeat_at') or 0, peer.get('last_progress_at') or 0, peer['created'])
                        if now - pa < 300:
                            continue
                        ep = digest([peer['id'], peer['attempt'], pa])
                        self.store.execute('''INSERT INTO dot_work_recovery_wakes VALUES (?,?,?,?,?)
                            ON CONFLICT(dot_id,work_item_id) DO UPDATE SET episode=excluded.episode,
                            wake_count=CASE WHEN episode=excluded.episode THEN MIN(wake_count+1,3) ELSE 1 END,
                            last_wake_at=excluded.last_wake_at''', (dot['id'], peer['id'], ep, 1, now))
                    awakened.add(dot['id'])
                    sent += 1
                except DevError:
                    continue
                if sent >= min(4, max_wakes):
                    break
        return sent
