"""A passive, durable relay: recovery notifications and versioned dot replies.

No model calls, task admission, credentials or execution live here. Delivery is
not consumption. Every recovery wake revalidates the original connection.
"""
from __future__ import annotations

import json

from hub.collaboration.common import MESSAGE_EVENT, canonical, digest, redact
from hub.collaboration.dot_wake_context import delivery_timing
from shared import collaboration_contracts as contracts
from shared.util import DevError


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(collaboration_dot_messages)')}
    for name, declaration in (
        ('wake_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_wake_at', 'REAL NOT NULL DEFAULT 0'),
        ('last_activity_at', 'REAL NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE collaboration_dot_messages ADD COLUMN {name} {declaration}')
    db.execute('CREATE INDEX IF NOT EXISTS dot_relay_pending ON collaboration_dot_messages(state,wake_count,created)')


class DotRelay:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self._after = 0

    def update_arguments(self, room, dot, message):
        return {'project': room['project_id'], 'environment_id': room['environment_id'],
                'dot_id': dot['id'], 'message_id': message['id'], 'expected_version': message['version'],
                'idempotency_key': 'dot-update:' + digest([dot['id'], message['id'], message['version']])}

    def touch(self, dot_id, source_id):
        self.store.execute('UPDATE collaboration_dot_messages SET last_activity_at=? WHERE dot_id=? AND message_id=?',
                           (self.c.clock(), dot_id, source_id))

    def update(self, raw, principal):
        with self.store.transaction():
            args, principal, room, _, dot = self.c.dot_chat.scope(raw, principal, contracts.DotReplyUpdate, connector=True)
            message = self.c.object('collaboration_messages', room, args['message_id'])
            body = json.loads(message['body'])
            self.c.chatroom.require_message_visible(principal, message)
            if (message['conversation_id'] != dot['conversation_id'] or message['origin'] != 'bound_connector'
                    or message['author'] != principal.grant_id or body.get('sender_dot_id') != dot['id']):
                raise DevError('DOT_REPLY_OWNER_REQUIRED', '只能更新此 dot 自己在当前话题的阶段回复', 403)

            def save():
                if message['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '回复已更新，请读取原消息当前版本，不要覆盖较新的内容', 409)
                if body.get('dot_reply_complete') is not False:
                    raise DevError('DOT_REPLY_FINISHED', '这条回复已经结束；后续内容请另发消息', 409)
                updated = {**body, 'body_text': redact(args['body_text']),
                           'dot_reply_complete': args['complete'], 'dot_updated_at': self.c.clock()}
                self.store.execute('UPDATE collaboration_messages SET body=?,version=version+1 WHERE id=?',
                                   (canonical(updated), message['id']))
                if message['reply_to_id']:
                    self.touch(dot['id'], message['reply_to_id'])
                    if args['complete']:
                        self.store.execute("""UPDATE collaboration_dot_messages SET state='handled',
                            received_at=COALESCE(received_at,?),handled_at=COALESCE(handled_at,?)
                            WHERE dot_id=? AND message_id=?""",
                            (self.c.clock(), self.c.clock(), dot['id'], message['reply_to_id']))
                stored = self.c.object('collaboration_messages', room, message['id'])
                self.c.task_liveness.record_receipt(dot, args, 'update', stored)
                if args['complete'] and hasattr(self.c, 'message_handling'):
                    self.c.message_handling.record_reply(room, dot, principal, stored)
                self.c.channels.queue_reply(stored)
                return {'message_id': message['id']}

            receipt = self.c.mutation(principal, room, 'dot_reply_update:' + dot['id'], args, save)
            current = self.c.object('collaboration_messages', room, receipt['message_id'])
            view = self.c.chatroom.view(current)
            from shared.public_collaboration import request as public_request
            inbound = self.store.one('SELECT * FROM collaboration_dot_messages WHERE dot_id=? AND message_id=?',
                (dot['id'], current['reply_to_id'])) if current['reply_to_id'] else None
            return {'dot_id': dot['id'], 'message': view, 'scheduled': False,
                    'delivery_timing': delivery_timing(self.c, room, dot, inbound) if inbound else None,
                    'receipt_request': public_request('collaboration_dot_delivery_receipt', {
                        'project': room['project_id'], 'environment_id': room['environment_id'], 'dot_id': dot['id'],
                        'kind': 'update', 'request_key': args['idempotency_key']}),
                    'complete': view['body'].get('dot_reply_complete') is not False,
                    'update_arguments': self.update_arguments(room, dot, current)}

    def receipts(self, message):
        return self.receipts_many([message]).get(message['id'], [])

    def receipts_many(self, messages, principal=None):
        """Bounded request-local batches. Never reuse authority across requests."""
        from hub.collaboration import experience_projection as projection
        unique = {m['id']: m for m in messages}
        if len(unique) > 124:
            raise DevError('RELAY_BATCH_LIMIT', '一次最多读取124条可见消息回执', 422)
        if not unique:
            return {}
        if not self.c.config.relay_batch_enabled:
            return {identifier: self._legacy_receipts(row) for identifier, row in unique.items()}
        include_experience = bool(self.c.config.experience_projection_enabled and principal is not None and not principal.grant_id)
        ids, now = tuple(unique), self.c.clock()
        marks = ','.join('?' for _ in ids)
        rows = self.store.all(f'''SELECT m.*,s.label,s.state AS slot_state,s.expires_at AS slot_expires,
            s.grant_id FROM collaboration_dot_messages m JOIN collaboration_join_slots s ON s.id=m.dot_id
            WHERE message_id IN ({marks}) ORDER BY sequence''', ids)
        if not rows:
            return {identifier: [] for identifier in ids}
        reply_rows = self.store.all(f'''SELECT * FROM (
            SELECT m.*,json_extract(body,'$.sender_dot_id') AS sender_dot,
                ROW_NUMBER() OVER (PARTITION BY reply_to_id,json_extract(body,'$.sender_dot_id')
                    ORDER BY created DESC,id DESC) AS rank
            FROM collaboration_messages m WHERE reply_to_id IN ({marks}))
            WHERE rank=1''', ids)
        replies = {(r['reply_to_id'], r['sender_dot']): r for r in reply_rows}
        works = self.store.all(f'''SELECT w.*,d.message_id,p.slot_id AS dot_id
            FROM delegation_requests d JOIN delegation_policies p ON p.id=d.policy_id
            JOIN coordination_work w ON w.id=d.work_item_id WHERE d.message_id IN ({marks})''', ids)
        work_map = {(w['message_id'], w['dot_id']): w for w in works}
        # Detailed operation evidence is only included after existing goal access
        # checks. Internal callers without a principal get the old state only.
        summaries = {(w['message_id'], w['dot_id']): w for w in self.c.task_liveness.for_messages(
            list(unique.values()), principal)} if principal is not None and include_experience else {}
        handling_service = getattr(self.c, 'message_handling', None)
        handling = {}
        if handling_service is not None and include_experience:
            for offset in range(0, len(ids), 100):
                handling.update(handling_service.summaries(ids[offset:offset + 100], principal=principal))
        coverage_ids = {h.get('reply_id') for h in handling.values() if h.get('reply_id') and h.get('source_current', True)}
        coverage = {}
        if coverage_ids:
            coverage = {r['id']: r for r in self.store.all('SELECT * FROM collaboration_messages WHERE id IN ('
                + ','.join('?' for _ in coverage_ids) + ')', tuple(coverage_ids))}
        all_reply_ids = list(dict.fromkeys([r['id'] for r in reply_rows] + list(coverage)))
        channels = self.c.channels.for_messages(all_reply_ids) if include_experience else {}
        dots = tuple(dict.fromkeys(row['dot_id'] for row in rows))
        active_rows = self.store.all('''SELECT r.slot_id,json_extract(s.arguments,'$.conversation_id') AS conversation_id
            FROM mcp_event_subscriptions s JOIN collaboration_join_routes r ON r.subscription_id=s.id
            WHERE r.slot_id IN (''' + ','.join('?' for _ in dots) + ''')
            AND s.name=? AND s.state='active' AND s.expires_at>?''', (*dots, MESSAGE_EVENT, now))
        active_pairs = {(r['slot_id'], r['conversation_id']) for r in active_rows}
        authorized, result = {}, {identifier: [] for identifier in ids}
        for row in rows:
            message = unique[row['message_id']]
            pair = (message['id'], row['dot_id'])
            reply, work = replies.get(pair), work_map.get(pair)
            state = 'saved'
            if work:
                if work['state'] == 'succeeded':
                    state = 'completed'
                elif work['state'] in ('failed', 'blocked', 'cancelled', 'expired'):
                    state = 'needs_attention'
                elif work['state'] in ('running', 'leased') and (work['lease_until'] or 0) > now:
                    state = 'working'
                else:
                    state = 'waiting_dot'
            elif reply:
                body = json.loads(reply['body'])
                state = 'replied' if body.get('dot_reply_complete') is not False else (
                    'replying' if now - max(reply['created'], body.get('dot_updated_at', 0)) < 90 else 'waiting_dot')
            elif row['state'] == 'handled':
                state = 'handled'
            elif row['state'] == 'read':
                state = 'received' if now - max(row['received_at'] or 0, row['last_activity_at']) < 90 else 'waiting_dot'
            if state in ('saved', 'waiting_dot'):
                active = ((row['dot_id'], message['conversation_id']) in active_pairs
                          and row['slot_state'] == 'registered' and row['slot_expires'] > now
                          and self.c.config.events_enabled)
                if active:
                    if row['dot_id'] not in authorized:
                        try:
                            dot = self.c.dots.find(row['dot_id'])
                            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
                            slot = self.c.joining.slot(room, dot['id'])
                            actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
                            self.c.dot_chat.authorize(actor, room, dot)
                            self.c.live_room(room)
                            authorized[row['dot_id']] = actor
                        except DevError:
                            authorized[row['dot_id']] = None
                    actor = authorized[row['dot_id']]
                    active = actor is not None and self.c.chatroom.message_visible(actor, message)
                if not active:
                    state = 'waiting_connection'
            summary = summaries.get(pair)
            if summary is None and work:
                summary = {'work_item_id': None, 'execution_state': work['state'],
                           'acceptance_status': 'unverified', 'observed_at': None}
            handling_row = handling.get((row['dot_id'], message['id']))
            projected_reply = reply if reply and (principal is None or self.c.chatroom.message_visible(principal, reply)) else None
            if handling_row and handling_row.get('source_current', True):
                candidate = coverage.get(handling_row.get('reply_id'))
                if (candidate and candidate['room_id'] == message['room_id']
                        and candidate['conversation_id'] == message['conversation_id']
                        and (principal is None or self.c.chatroom.message_visible(principal, candidate))):
                    projected_reply = candidate
                elif candidate:
                    handling_row = {'state': 'awaiting', 'evidence_current': False, 'revision': handling_row.get('revision')}
            if projected_reply:
                projected_reply = {**projected_reply, 'body': json.loads(projected_reply['body'])}
            value = {'dot_id': row['dot_id'], 'label': row['label'], 'state': state,
                'received_at': row['received_at'], 'handled_at': row['handled_at'],
                'recovery_wakes': row['wake_count'], 'model_online': 'unknown'}
            if include_experience:
                value['experience'] = projection.message(message, {**row, 'relay_state': state},
                    reply=projected_reply, work=summary, handling=handling_row,
                    channels=channels.get(projected_reply['id'], []) if projected_reply else [])
            result[message['id']].append(value)
        if self.c.config.relay_batch_shadow:
            baseline = {identifier: self._legacy_receipts(row) for identifier, row in unique.items()}
            compared = {identifier: [{k: v for k, v in r.items() if k != 'experience'} for r in values]
                        for identifier, values in result.items()}
            if compared != baseline:
                return {identifier: [{**r, 'projection_comparison': 'mismatch_fallback'} for r in values]
                        for identifier, values in baseline.items()}
        if not self.c.config.experience_projection_enabled:
            for values in result.values():
                for value in values:
                    value.pop('experience', None)
        return result

    def _legacy_receipts(self, message):
        """One truthful status per recipient, rather than contradictory UI badges."""
        rows = self.store.all('''SELECT m.*,s.label,s.state AS slot_state,s.expires_at AS slot_expires,
            s.grant_id FROM collaboration_dot_messages m JOIN collaboration_join_slots s ON s.id=m.dot_id
            WHERE message_id=? ORDER BY sequence''', (message['id'],))
        result, now = [], self.c.clock()
        for row in rows:
            reply = self.store.one('''SELECT body,created FROM collaboration_messages WHERE reply_to_id=?
                AND json_extract(body,'$.sender_dot_id')=? ORDER BY created DESC,id DESC LIMIT 1''',
                (message['id'], row['dot_id']))
            work = self.store.one('''SELECT w.state,w.updated,w.lease_until,w.reason_code
                FROM delegation_requests d JOIN delegation_policies p ON p.id=d.policy_id
                JOIN coordination_work w ON w.id=d.work_item_id WHERE d.message_id=? AND p.slot_id=?''',
                (message['id'], row['dot_id']))
            state = 'saved'
            if work:
                if work['state'] == 'succeeded':
                    state = 'completed'
                elif work['state'] in ('failed', 'blocked', 'cancelled', 'expired'):
                    state = 'needs_attention'
                elif work['state'] in ('running', 'leased') and (work['lease_until'] or 0) > now:
                    state = 'working'
                else:
                    state = 'waiting_dot'
            elif reply:
                body = json.loads(reply['body'])
                state = 'replied' if body.get('dot_reply_complete') is not False else (
                    'replying' if now - max(reply['created'], body.get('dot_updated_at', 0)) < 90 else 'waiting_dot')
            elif row['state'] == 'handled':
                state = 'handled'
            elif row['state'] == 'read':
                state = 'received' if now - max(row['received_at'] or 0, row['last_activity_at']) < 90 else 'waiting_dot'
            if state in ('saved', 'waiting_dot'):
                if row['slot_state'] != 'registered' or row['slot_expires'] <= now or not self.c.config.events_enabled:
                    state = 'waiting_connection'
                else:
                    active = self.store.one('''SELECT 1 FROM mcp_event_subscriptions s
                        JOIN collaboration_join_routes r ON r.subscription_id=s.id
                        WHERE r.slot_id=? AND s.name=? AND s.state='active' AND s.expires_at>?
                        AND json_extract(s.arguments,'$.conversation_id')=? LIMIT 1''',
                        (row['dot_id'], MESSAGE_EVENT, now, message['conversation_id']))
                    if active:
                        try:
                            dot = self.c.dots.find(row['dot_id'])
                            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
                            slot = self.c.joining.slot(room, dot['id'])
                            actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
                            self.c.dot_chat.authorize(actor, room, dot)
                            self.c.live_room(room)
                        except DevError:
                            active = False
                    if not active:
                        state = 'waiting_connection'
            result.append({'dot_id': row['dot_id'], 'label': row['label'], 'state': state,
                           'received_at': row['received_at'], 'handled_at': row['handled_at'],
                           'recovery_wakes': row['wake_count'], 'model_online': 'unknown'})
        return result

    def recover(self):
        """Bounded, fair scan of unconsumed messages. Never reissue business work.

        Three coalesced wake attempts (30/120/600 seconds) cover no-subscription,
        rate-limited and accepted-but-unconsumed gaps. Read/partial replies delay
        recovery; completed messages never wake. Paused/revoked routes stay so.
        """
        if not self.c.config.enabled or not self.c.config.events_enabled:
            return 0
        with self.store.transaction():
            rows = self.store.all('''SELECT * FROM collaboration_dot_messages WHERE state!='handled'
                AND wake_count<3 AND sequence>? ORDER BY sequence LIMIT 64''', (self._after,))
            self._after = rows[-1]['sequence'] if len(rows) == 64 else 0
            now, awakened = self.c.clock(), set()
            for row in rows:
                delay = (30, 120, 600)[row['wake_count']]
                idle_since = max(row['created'], row['last_wake_at'], row['last_activity_at'], row['received_at'] or 0)
                if now - idle_since < (max(300, delay) if row['state'] == 'read' else delay) or row['dot_id'] in awakened:
                    continue
                dot = self.c.dots.find(row['dot_id'])
                if not self.c.dot_chat.enabled(dot):
                    continue
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
                slot = self.c.joining.slot(room, dot['id'])
                try:
                    actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
                    self.c.dot_chat.authorize(actor, room, dot)
                    self.c.live_room(room)
                    message = self.c.object('collaboration_messages', room, row['message_id'])
                    self.c.chatroom.require_message_visible(actor, message)
                    routes = self.store.all('''SELECT s.* FROM mcp_event_subscriptions s
                        JOIN collaboration_join_routes r ON r.subscription_id=s.id WHERE r.slot_id=?
                        AND s.name=? AND s.state='active' AND s.expires_at>?''', (dot['id'], MESSAGE_EVENT, now))
                    valid = False
                    for route in routes:
                        try:
                            filters = self.c.events.authorize(route, room)
                            valid |= filters.get('conversation_id') == dot['conversation_id']
                        except DevError:
                            continue
                    if not valid:
                        continue
                except DevError:
                    continue
                # An automatic recovery hint must not bypass a permanent callback
                # refusal, or increase the existing shared project notification budget.
                stopped = self.store.one("""SELECT 1 FROM mcp_event_deliveries d
                    JOIN mcp_event_outbox e ON e.id=d.event_id JOIN mcp_event_subscriptions s ON s.id=d.subscription_id
                    WHERE e.room_id=? AND e.name=? AND json_extract(e.data,'$.message_id')=?
                    AND json_extract(e.data,'$.recipient_slot_id')=? AND d.created>=s.updated
                    AND (d.status_code IN (410,413) OR d.reason_code='callback_policy_rejected') LIMIT 1""",
                    (room['id'], MESSAGE_EVENT, message['id'], dot['id']))
                budget = self.store.one("""SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?
                    AND room_id IN (SELECT id FROM collaboration_rooms WHERE space_id=? AND owner_user_id=? AND project_id=?)
                    AND created>?""", (MESSAGE_EVENT, room['space_id'], room['owner_user_id'], room['project_id'], now-60))['n']
                if stopped or budget >= 30:
                    continue
                generation = row['wake_count'] + 1
                self.c.emit(room, MESSAGE_EVENT, 'dot-recover:' + digest([dot['id'], row['message_id'], generation]), generation,
                    {'conversation_id': dot['conversation_id'], 'room_id': room['id'],
                     'message_id': message['id'], 'message_version': message['version'],
                     'recipient_slot_id': dot['id'], 'thread_root_id': message['thread_root_id']}, grant_id=slot['grant_id'])
                # One wake drains all pending items of this dot; do not send N identical hints.
                self.store.execute('''UPDATE collaboration_dot_messages SET wake_count=wake_count+1,last_wake_at=?
                    WHERE dot_id=? AND state!='handled' AND wake_count<3 AND created<=?''', (now, dot['id'], now))
                awakened.add(dot['id'])
                if len(awakened) >= 4:
                    break
            return len(awakened)

    def pending_scope_revision(self, principal, room, conversation, slots=None):
        """Opaque generation for optional local pending intents, not a grant.

        Include current effective authority and exact project mappings. Never
        include credentials, query time or model liveness in this stable digest.
        """
        from hub.room_scope import snapshot
        if principal.grant_id or not principal.admin:
            return None
        rooms = self.c.conversations.visible(principal, conversation)
        mappings = []
        for selected in rooms:
            project = self.c.runtime.project(selected['project_id'], principal)
            mappings.append([selected['id'], selected['version'], selected['state'], snapshot(project)])
        if slots is None:
            slots = self.c.joining.list(room, principal, conversation['id'])
        return digest(['pending-scope-v1',
            {key: getattr(principal, key) for key in ('user_id', 'space_id', 'user_epoch', 'identity_id',
                'admin', 'profile_id', 'role_id', 'authorization_mode')},
            sorted(principal.scopes), sorted(principal.projects), conversation['id'], conversation['version'],
            sorted(mappings, key=lambda value: value[0]),
            [{key: slot.get(key) for key in ('id', 'version', 'state', 'status', 'policy_id', 'policy_version',
                'capabilities', 'execution_target', 'scope_manifest')}
             for slot in sorted(slots, key=lambda value: value['id'])]])

    def message_receipt(self, raw, principal):
        """Authoritative absence only after exact current sender/scope checks."""
        from hub.collaboration.common import validate
        args = validate(contracts.Read, {**raw, 'kind': 'message_status'})
        if not args['client_message_id'] or args['id']:
            raise DevError('INVALID_ARGUMENTS', '核对提交需要原 client_message_id', 422)
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            row = self.store.one('''SELECT * FROM collaboration_messages
                WHERE room_id=? AND author=? AND origin=? AND source_id=?''',
                (room['id'], principal.grant_id or principal.user_id,
                 'bound_connector' if principal.grant_id else 'panel_owner', args['client_message_id']))
            if row:
                if row['conversation_id'] != conversation['id']:
                    raise DevError('SOURCE_MESSAGE_CONFLICT', '原提交标识属于另一话题', 409)
                self.c.chatroom.require_message_visible(principal, row)
            return {'schema_version': 1, 'read_only': True, 'authoritative': True,
                    'state': 'committed' if row else 'not_committed',
                    'client_message_id': args['client_message_id'], 'conversation_id': conversation['id'],
                    'pending_scope_revision': self.pending_scope_revision(principal, room, conversation),
                    'message': self.c.chatroom.view(row) if row else None,
                    'retry_rule': 'same_original_request_key_only_after_not_committed'}

    def diagnostics(self, raw, principal):
        from hub.collaboration.channel_capabilities import diagnostic_summary
        from hub.collaboration.common import validate
        from shared.util import VERSION
        args = validate(contracts.Read, {**raw, 'kind': 'overview'})
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            result = self.sync(raw, principal, '')['diagnostic_summary']
            descriptors, truncated = [], False
            for channel in ('weixin', 'telegram'):
                bindings = self.store.all('SELECT id FROM ' + channel + '''_bindings
                    WHERE room_id=? AND conversation_id=? AND space_id=? AND owner_user_id=?
                    ORDER BY id LIMIT 33''', (room['id'], conversation['id'], principal.space_id, principal.user_id))
                truncated |= len(bindings) > 32
                for binding in bindings[:32]:
                    descriptors.append(diagnostic_summary(
                        self.c.channels.capabilities.for_binding(channel, binding['id'])))
            return {**result, 'codepier_version': VERSION, 'channel_capabilities': descriptors,
                    'channel_capabilities_truncated': truncated,
                    'layers': ['current_account_and_scope', 'current_channel_capabilities',
                               'host_subscription', 'consumption_and_original_work', 'publication_and_channel_receipts']}

    def sync(self, raw, principal, tracked):
        from hub.collaboration.common import validate
        args = validate(contracts.Read, {**raw, 'kind': 'timeline', 'limit': 100})
        ids = tracked.split(',') if tracked else []
        if len(ids) > 24 or any(not value or len(value) > 128 for value in ids):
            raise DevError('INVALID_ARGUMENTS', '一次最多同步24条已展示消息', 422)
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            rooms = self.c.conversations.visible(principal, conversation) if args['conversation_id'] else [room]
            visible = {item['id'] for item in rooms}
            timeline = self.c.conversations.read(args, principal, room)
            rows = []
            for identifier in dict.fromkeys(ids):
                row = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (identifier,))
                if (row and row['conversation_id'] == conversation['id'] and row['room_id'] in visible
                        and self.c.chatroom.message_visible(principal, row)):
                    rows.append(row)
            # Initial overview supplies joining instructions once. Poll only the
            # state needed for this conversation, not repeated consumer prompts.
            keys = {'id', 'dot_id', 'label', 'duplex', 'task_dot', 'kind', 'conversation_id', 'capabilities', 'execution_target', 'default_dispatch', 'state', 'status', 'version', 'expires_at', 'code_expires_at',
                    'policy_id', 'policy_version', 'blocked_reason', 'task_blocked_reason', 'task_status',
                    'chat_status', 'subscription_count', 'scope_manifest', 'operation_timeout_seconds'}
            from hub.collaboration import experience_projection as projection
            full_slots = self.c.joining.list(room, principal, conversation['id'])
            slots = [{key: value for key, value in slot.items() if key in keys} for slot in full_slots]
            readiness = {slot['id']: projection.connection(slot) for slot in full_slots} if self.c.config.experience_projection_enabled else {}
            task_states = self.c.task_liveness.for_messages([*rows, *timeline.get('items', [])], principal)
            updates = self.c.chatroom.views(rows, principal=principal)
            loaded = {item['id']: item for item in [*timeline.get('items', []), *updates]}
            projections = [receipt['experience'] for item in loaded.values()
                           for receipt in item.get('relay_receipts', []) if receipt.get('experience')]
            return {**timeline, 'task_states': task_states, 'server_now': self.c.clock(),
                    'connection_readiness': readiness,
                    'pending_scope_revision': self.pending_scope_revision(principal, room, conversation, full_slots),
                    'diagnostic_summary': projection.diagnostic(projections, list(readiness.values())),
                    'updates': updates,
                    'join_slots': slots, 'room_state': room['state'],
                    'can_manage': bool(principal.admin and not principal.grant_id), 'read_only': True}
