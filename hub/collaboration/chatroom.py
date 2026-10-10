"""Ordinary room discussion; messages never implicitly schedule analysis."""
from __future__ import annotations

import json
import uuid

from shared import collaboration_contracts as contracts
from shared.util import DevError
from hub.collaboration.common import MESSAGE_EVENT, canonical, digest, read_cursor, redact, sign_cursor, validate


class ChatroomService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def capabilities(self):
        return {'task_dots': True, 'work_progress': True, 'coordination_goals': True, 'direct_delegation': True, 'message_remind': True, 'plain_messages': True, 'message_notifications': self.c.config.events_enabled,
                'task_assignment': True, 'message_search': True, 'incremental_messages': True,
                'read_cursors': True, 'attachments': False, 'human_memberships': False}

    @staticmethod
    def same_room(room, args):
        if args.get('room_id') and args['room_id'] != room['id']:
            raise DevError('ROOM_MISMATCH', '消息不属于当前协作室', 409)

    def writer(self, principal, room, conversation_id):
        if not principal.grant_id:
            self.c.owner(principal)
            return
        self.c.notification_reader(principal)
        access = self.store.one('''SELECT * FROM conversation_writers
            WHERE conversation_id=? AND room_id=? AND grant_id=? AND enabled=1 AND expires_at>?''',
            (conversation_id, room['id'], principal.grant_id, self.c.clock()))
        if not access:
            raise DevError('MESSAGE_ACCESS_REQUIRED', '需要房主明确允许这个连接在当前房间发言；加入通知不授予发言权限', 403)
        self.c.grant_reader(room, principal.grant_id, snapshot=json.loads(access['principal']))

    def access(self, raw, principal):
        args = validate(contracts.MessageAccess, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.same_room(room, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            args['conversation_id'] = conversation['id']
            def save():
                now = self.c.clock()
                current = self.store.one('SELECT version FROM conversation_writers WHERE conversation_id=? AND room_id=? AND grant_id=?',
                                         (conversation['id'], room['id'], args['grant_id']))
                version = current['version'] if current else 0
                if version != args['expected_version']:
                    raise DevError('STALE_VERSION', '连接发言权限已改变，请刷新后重新确认', 409)
                if args['enabled']:
                    self.c.live_room(room)
                    grant = self.c.grant_reader(room, args['grant_id'])
                    snapshot = canonical(self.c.principal_snapshot(grant))
                else:
                    snapshot = '{}'
                expires = now + args['expires_in_days'] * 86400
                self.store.execute('''INSERT INTO conversation_writers
                    (conversation_id,room_id,grant_id,enabled,expires_at,principal,updated) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(conversation_id,room_id,grant_id) DO UPDATE SET enabled=excluded.enabled,
                    expires_at=excluded.expires_at,principal=excluded.principal,updated=excluded.updated,version=version+1''',
                    (conversation['id'], room['id'], args['grant_id'], int(args['enabled']), expires, snapshot, now))
                return {'conversation_id': conversation['id'], 'room_id': room['id'], 'grant_id': args['grant_id'], 'enabled': args['enabled'],
                        'expires_at': expires, 'version': version + 1, 'chat_identity_verified': False}
            return self.c.mutation(principal, room, 'message_access:' + conversation['id'], args, save)

    def view(self, row, delivery_map=None):
        body = redact(json.loads(row['body']))
        if body.get('delegation'):
            body['delegation']['delivery_status'] = self.c.delegation.delivery_status(body['delegation']['delegation_id'])
            body['delegation']['progress'] = self.c.delegation.progress(body['delegation']['delegation_id'])
            body['delegation']['retry_blocked_eligible'] = self.c.delegation.retry_blocked_eligible(body['delegation']['delegation_id'])
        if delivery_map is None:
            delivery_map = self.deliveries([row])
        for receipt in body.get('notifications', []):
            receipt.update(delivery_map.get(receipt.get('event_id'), {}))
        text = body.get('body_text', body.get('summary', body.get('command', {}).get('request', '')))
        owner = row['origin'] in {'panel', 'panel_owner'}
        partition = self.store.one('SELECT project_id,environment_id FROM collaboration_rooms WHERE id=?', (row['room_id'],))
        result = {**row, **partition, 'source_room_id': row['room_id'], 'source_sequence': row['server_sequence'],
                  'server_sequence': row['conversation_sequence'], 'message_id': row['id'], 'body': body, 'body_text': text,
                  'author_kind': 'owner' if owner else 'connector',
                  'display_name': '房主' if owner else '助手连接',
                  'chat_identity_verified': False, 'mentions': body.get('mentions', []),
                  'client_message_id': row['source_id'], 'related_goal_id': row['goal_id'],
                  'job_id': body.get('job_id'), 'result_id': body.get('result_id'),
                  'source_message_id': body.get('source_message_id')}
        if row['kind'] in {'delegation_progress', 'delegation_result'}:
            bound = self.store.one('''SELECT s.label,s.id FROM delegation_requests d
                JOIN delegation_policies p ON p.id=d.policy_id JOIN collaboration_join_slots s ON s.id=p.slot_id
                WHERE d.id=? AND d.room_id=? AND p.grant_id=?''',
                (body.get('delegation_id', ''), row['room_id'], row['author']))
            if bound:
                result['display_name'] = bound['label']
                result['slot_id'] = bound['id']
        if row['kind'] in {'owner_command', 'agent_proposal'}:
            job = self.store.one('SELECT id FROM collaboration_jobs WHERE room_id=? AND business_key=?',
                                 (row['room_id'], 'command:' + row['id']))
            if job:
                result['job_id'] = job['id']
        return result

    def deliveries(self, rows):
        identifiers = {item.get('event_id') for row in rows
                       for item in json.loads(row['body']).get('notifications', []) if item.get('event_id')}
        if not identifiers:
            return {}
        placeholders = ','.join('?' for _ in identifiers)
        records = self.store.all(f'''SELECT e.id AS event_id,d.state,d.reason_code,d.accepted_at,
            e.seq AS event_seq,s.scan_seq,s.state AS subscription_state,s.expires_at,j.state AS slot_state,j.expires_at AS slot_expires,
            room.state AS room_state
            FROM mcp_event_outbox e JOIN collaboration_rooms room ON room.id=e.room_id
            LEFT JOIN collaboration_join_slots j ON j.id=json_extract(e.data,'$.recipient_slot_id') AND j.room_id=e.room_id
            LEFT JOIN collaboration_join_routes r ON r.slot_id=j.id
            LEFT JOIN mcp_event_subscriptions s ON s.id=r.subscription_id AND s.name=e.name
                AND s.grant_id=e.target_grant_id AND json_extract(s.arguments,'$.slot_id')=j.id
                AND COALESCE(NULLIF(json_extract(s.arguments,'$.conversation_id'),''),s.room_id)=COALESCE(NULLIF(json_extract(e.data,'$.conversation_id'),''),e.room_id)
            LEFT JOIN mcp_event_deliveries d ON d.event_id=e.id AND d.subscription_id=s.id
            WHERE e.id IN ({placeholders}) AND e.name=?''', (*identifiers, MESSAGE_EVENT))
        groups = {}
        for record in records:
            groups.setdefault(record['event_id'], []).append(record)
        result = {}
        now = self.c.clock()
        for identifier, attempts in groups.items():
            accepted = [item for item in attempts if item['state'] == 'accepted']
            active = [item for item in attempts if item['subscription_state'] == 'active'
                      and item['expires_at'] > now and item['slot_state'] == 'registered'
                      and item['slot_expires'] > now and item['room_state'] == 'active'
                      and (item['state'] is not None or item['scan_seq'] < item['event_seq'])]
            pending = [item for item in active if item['state'] not in {'accepted', 'dead_letter', 'abandoned'}]
            if accepted:
                result[identifier] = {'state': 'accepted', 'accepted_at': max(item['accepted_at'] or 0 for item in accepted),
                                      'read_verified': False}
            elif pending:
                result[identifier] = {'state': 'delivering' if any(item['state'] == 'leased' for item in pending) else 'queued'}
            elif active:
                result[identifier] = {'state': 'failed', 'reason_code': active[0]['reason_code'] or 'delivery_failed'}
            else:
                result[identifier] = {'state': 'recipient_unavailable'}
        return result

    def message_visible(self, principal, row):
        for project_id in json.loads(row['body']).get('provenance_project_ids', []):
            try:
                self.c.runtime.project(project_id, principal)
            except DevError:
                return False
        return True

    def require_message_visible(self, principal, row):
        if not self.message_visible(principal, row):
            raise DevError('NOT_FOUND', '当前授权下没有这条消息', 404)

    def views(self, rows, principal=None):
        if principal is not None:
            rows = [row for row in rows if self.message_visible(principal, row)]
        delivery_map = self.deliveries(rows)
        return [self.view(row, delivery_map) for row in rows]

    def notify(self, room, message, principal, mentions, generation=''):
        receipts = []
        for mention in mentions:
            slot = self.c.joining.slot(room, mention['slot_id'])
            state, event_id = 'not_subscribed', None
            try:
                self.c.joining.active(room, slot, registered=True)
                self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
            except DevError:
                state = 'recipient_unavailable'
            else:
                routes = self.store.all('''SELECT s.* FROM mcp_event_subscriptions s
                    JOIN collaboration_join_routes r ON r.subscription_id=s.id
                    WHERE r.slot_id=? AND s.room_id=? AND s.name=? AND s.state='active' AND s.expires_at>?
                    AND COALESCE(NULLIF(json_extract(s.arguments,'$.conversation_id'),''),s.room_id)=?''',
                    (slot['id'], room['id'], MESSAGE_EVENT, self.c.clock(), message['conversation_id']))
                if principal.grant_id:
                    state = 'suppressed_agent_reply'
                elif not self.c.config.events_enabled:
                    state = 'notifications_disabled'
                elif routes:
                    # Shared project budget survives new rooms, environments and retries.
                    count = self.store.one('''SELECT COUNT(*) AS n FROM mcp_event_outbox WHERE name=?
                        AND room_id IN (SELECT id FROM collaboration_rooms WHERE space_id=? AND owner_user_id=? AND project_id=?)
                        AND created>?''', (MESSAGE_EVENT, room['space_id'], room['owner_user_id'], room['project_id'], self.c.clock() - 60))['n']
                    if count >= 30:
                        state = 'rate_limited'
                    else:
                        object_id = message['id'] + ':' + slot['id'] + (':' + generation if generation else '')
                        self.c.emit(room, MESSAGE_EVENT, object_id, 1,
                            {'conversation_id': message['conversation_id'], 'room_id': room['id'], 'message_id': message['id'], 'message_version': message['version'],
                             'recipient_slot_id': slot['id'], 'thread_root_id': message['thread_root_id']},
                            grant_id=slot['grant_id'])
                        event_id = self.store.one('SELECT id FROM mcp_event_outbox WHERE name=? AND object_id=?',
                                                  (MESSAGE_EVENT, object_id))['id']
                        state = 'queued'
            receipts.append({'slot_id': slot['id'], 'state': state, 'event_id': event_id})
        return receipts

    def create(self, raw, principal):
        args = validate(contracts.MessageCreate, raw)
        # Omitted new fields must keep old ordinary-message replay fingerprints.
        if args['dispatch_mode'] == 'discussion':
            args.pop('dispatch_mode')
        if not args['automatic_policy_version']:
            args.pop('automatic_policy_version')
        if args['delegation'] is None:
            args.pop('delegation')  # Preserve pre-delegation ordinary-message replay fingerprints.
        if args.get('delegation'):
            # Omitted subset fields keep pre-upgrade idempotency fingerprints.
            args['delegation'] = {key: value for key, value in args['delegation'].items() if value is not None}
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.same_room(room, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            args['conversation_id'] = conversation['id']
            self.writer(principal, room, conversation['id'])
            if args.get('delegation') or args.get('dispatch_mode') == 'automatic':
                self.c.owner(principal)
            def save():
                self.c.live_room(room)
                origin = 'bound_connector' if principal.grant_id else 'panel_owner'
                author = principal.grant_id or principal.user_id
                fingerprint = digest({k: v for k, v in args.items() if k != 'idempotency_key'})
                old = self.store.one('''SELECT * FROM collaboration_messages
                    WHERE room_id=? AND origin=? AND author=? AND source_id=?''',
                    (room['id'], origin, author, args['client_message_id']))
                if old:
                    if old['conversation_id'] != conversation['id']:
                        raise DevError('SOURCE_MESSAGE_CONFLICT', '消息标识属于另一个聊天室', 409)
                    if json.loads(old['body']).get('request_digest') != fingerprint:
                        raise DevError('SOURCE_MESSAGE_CONFLICT', '同一消息标识不能替换已保存内容', 409)
                    view = self.view(old)
                    return {'message': view, 'notifications': view['body'].get('notifications', []),
                            **view['body'].get('delegation', {'scheduled': False})}
                count = self.store.one('''SELECT COUNT(*) AS n FROM collaboration_messages
                    WHERE author=? AND room_id IN (SELECT id FROM collaboration_rooms
                    WHERE space_id=? AND owner_user_id=? AND project_id=?) AND created>?''',
                    (author, room['space_id'], room['owner_user_id'], room['project_id'], self.c.clock() - 60))['n']
                if count >= 60:
                    raise DevError('MESSAGE_RATE_LIMIT', '发言过于频繁，请稍后再试', 429)
                # Resolve only after the saved-message check: retries always keep their original ID.
                automatic = self.c.delegation.automatic_request(principal, room, conversation, args)
                selected_delegation = args.get('delegation') or automatic
                identifier = uuid.uuid4().hex
                root = identifier
                if args['reply_to_id']:
                    parent = self.c.object('collaboration_messages', room, args['reply_to_id'])
                    if parent['conversation_id'] != conversation['id']:
                        raise DevError('THREAD_NOT_FOUND', '不能跨聊天室或项目回复，请另发一条消息', 409)
                    root = parent['thread_root_id'] or parent['id']
                mentions = []
                for item in args['mentions']:
                    slot = self.c.joining.slot(room, item['slot_id'])
                    mentions.append({'slot_id': slot['id'], 'display_snapshot': slot['label']})
                body = {'body_text': redact(args['body_text']), 'mentions': mentions, 'request_digest': fingerprint}
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, room['id'], conversation['id'], root, root, args['reply_to_id'], author, origin,
                     args['client_message_id'], 'reply' if args['reply_to_id'] else 'text', canonical(body), 'saved', self.c.clock()))
                message = self.c.object('collaboration_messages', room, identifier)
                receipt = {'scheduled': False}
                if selected_delegation:
                    receipt = self.c.delegation.send(principal, room, message, {**args, 'delegation': selected_delegation})
                    body['delegation'] = {**receipt, 'policy_id': selected_delegation['policy_id'],
                                          'policy_version': selected_delegation['policy_version'],
                                          'automatic': automatic is not None}
                    body['provenance_project_ids'] = [room['project_id']]
                    body['notifications'] = []
                else:
                    body['notifications'] = self.notify(room, message, principal, mentions)
                self.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), identifier))
                return {'message': self.view(self.c.object('collaboration_messages', room, identifier)),
                        'notifications': body['notifications'], **receipt}
            return self.c.mutation(principal, room, 'message_create:' + conversation['id'], args, save)

    def is_delegation_source(self, message):
        return bool(json.loads(message['body']).get('delegation') or self.store.one(
            'SELECT 1 FROM delegation_requests WHERE message_id=?', (message['id'],)))

    def remind(self, raw, principal):
        args = validate(contracts.MessageRemind, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.same_room(room, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            message = self.c.object('collaboration_messages', room, args['message_id'])
            self.require_message_visible(principal, message)
            if (message['conversation_id'] != conversation['id'] or message['origin'] != 'panel_owner'
                    or message['author'] != principal.user_id or message['kind'] not in {'text', 'reply'}):
                raise DevError('MESSAGE_OWNER_REQUIRED', '只能提醒自己在当前聊天室保存的普通消息', 403)
            if message['version'] != args['expected_message_version']:
                raise DevError('STALE_VERSION', '消息已改变，请重新读取', 409)
            body = json.loads(message['body'])
            if self.is_delegation_source(message):
                raise DevError('MESSAGE_KIND_INVALID', '已委托消息不能发送普通提醒；请使用 delegation-remind 继续派发原任务', 409)
            mentioned = {item['slot_id']: item for item in body.get('mentions', [])}
            if len(set(args['slot_ids'])) != len(args['slot_ids']) or not set(args['slot_ids']) <= set(mentioned):
                raise DevError('MESSAGE_MENTION_REQUIRED', '只能提醒原消息已明确提及的位置', 403)
            def save():
                self.c.live_room(room)
                receipts = {item['slot_id']: item for item in body.get('notifications', [])}
                delivery = self.deliveries([message])
                for slot_id in args['slot_ids']:
                    current = receipts.get(slot_id, {})
                    state = delivery.get(current.get('event_id'), {}).get('state', current.get('state'))
                    if state in {'accepted', 'queued', 'delivering'}:
                        continue
                    receipts[slot_id] = self.notify(room, message, principal, [mentioned[slot_id]], generation=uuid.uuid4().hex)[0]
                updated = {**body, 'notifications': list(receipts.values())}
                self.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(updated), message['id']))
                return {'message_id': message['id']}
            self.c.mutation(principal, room, 'message_remind:' + conversation['id'], args, save)
            current = self.view(self.c.object('collaboration_messages', room, message['id']))
            return {'message': current, 'notifications': current['body'].get('notifications', []), 'scheduled': False}

    def to_task(self, raw, principal):
        args = validate(contracts.MessageToTask, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.same_room(room, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            args['conversation_id'] = conversation['id']
            source = self.c.object('collaboration_messages', room, args['message_id'])
            if source['conversation_id'] != conversation['id']:
                raise DevError('CONVERSATION_MESSAGE_MISMATCH', '任务来源不属于此聊天室和项目', 409)
            if self.is_delegation_source(source):
                raise DevError('MESSAGE_ALREADY_DELEGATED', '这条消息已有直接委托；请查看原任务或用 delegation-remind 继续派发', 409)
            def save():
                self.c.live_room(room)
                source = self.c.object('collaboration_messages', room, args['message_id'])
                if source['conversation_id'] != conversation['id']:
                    raise DevError('CONVERSATION_MESSAGE_MISMATCH', '任务来源不属于此聊天室和项目', 409)
                if source['version'] != args['expected_message_version']:
                    raise DevError('STALE_VERSION', '来源消息已改变，请重新审阅后确认', 409)
                if source['kind'] not in {'text', 'reply'}:
                    raise DevError('MESSAGE_KIND_INVALID', '只能将普通消息明确转为任务', 409)
                # A source can have one conversion, even if a UI retries with a new key.
                old = self.store.one('SELECT * FROM collaboration_jobs WHERE room_id=? AND business_key=?',
                                     (room['id'], 'message:' + source['id']))
                snapshot = {'source_message_id': source['id'], 'source_message_version': source['version'],
                            'source_body_text': self.view(source)['body_text'], 'request': args['request'],
                            'acceptance': args['acceptance'], 'assignee_agent_id': args['assignee_agent_id'], 'kind': args['kind']}
                if old:
                    if json.loads(old['context']).get('conversion_digest') != digest(snapshot):
                        raise DevError('MESSAGE_ALREADY_CONVERTED', '此消息已有任务，请查看原任务', 409)
                    return {'message_id': source['id'], 'job_id': old['id'], 'goal_id': old['goal_id'], 'scheduled': True, 'replayed': True}
                agent = self.c.agent(room, args['assignee_agent_id'])
                goal = uuid.uuid4().hex
                self.store.execute('''INSERT INTO collaboration_goals
                    (id,room_id,source_message_id,request,acceptance,created,updated) VALUES (?,?,?,?,?,?,?)''',
                    (goal, room['id'], source['id'], redact(args['request']), redact(args['acceptance']), self.c.clock(), self.c.clock()))
                job = self.c.make_job(room, agent_id=agent['id'], kind=args['kind'], business_key='message:' + source['id'],
                    goal_id=goal, context={'request': args['request'], 'acceptance': args['acceptance'],
                    'origin_message_id': source['id'], 'source_snapshot': snapshot, 'conversion_digest': digest(snapshot), 'evidence_refs': []})
                identifier = uuid.uuid4().hex
                body = {'body_text': redact(args['request']), 'job_id': job['id'], 'source_message_id': source['id']}
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,goal_id,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, room['id'], conversation['id'], source['thread_root_id'], source['thread_root_id'], source['id'], principal.user_id,
                     'panel_owner', 'task:' + source['id'], 'task_reference', canonical(body), 'queued', goal, self.c.clock()))
                return {'message_id': source['id'], 'task_message_id': identifier, 'job_id': job['id'], 'goal_id': goal,
                        'scheduled': True, 'delivery_status': self.c.delivery_status(job)}
            return self.c.mutation(principal, room, 'message_to_task:' + conversation['id'], args, save)

    def set_read_cursor(self, raw, principal):
        args = validate(contracts.ReadCursor, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.same_room(room, args)
            conversation = self.c.conversations.resolve(principal, room, args)
            args['conversation_id'] = conversation['id']
            def save():
                latest = self.store.one('SELECT COALESCE(MAX(conversation_sequence),0) AS n FROM collaboration_messages WHERE conversation_id=?', (conversation['id'],))['n']
                if args['last_seen_sequence'] > latest:
                    raise DevError('INVALID_SEQUENCE', '已读位置不能超过房间最新消息', 422)
                self.store.execute('''INSERT INTO conversation_read_cursors(conversation_id,user_id,sequence,updated)
                    VALUES (?,?,?,?) ON CONFLICT(conversation_id,user_id) DO UPDATE
                    SET sequence=MAX(sequence,excluded.sequence),updated=excluded.updated''',
                    (conversation['id'], principal.user_id, max(args['last_seen_sequence'], max(conversation['history_sequence'], room['chat_history_sequence'] if conversation['id'] == room['id'] else 0)), self.c.clock()))
                return {'room_id': room['id'], 'conversation_id': conversation['id'], 'read_sequence': self.store.one(
                    'SELECT sequence FROM conversation_read_cursors WHERE conversation_id=? AND user_id=?', (conversation['id'], principal.user_id))['sequence']}
            return self.c.mutation(principal, room, 'read_cursor:' + conversation['id'], args, save)

    def latest(self, room):
        return self.store.one('SELECT COALESCE(MAX(server_sequence),0) AS n FROM collaboration_messages WHERE room_id=?', (room['id'],))['n']

    def read_sequence(self, room, principal):
        row = self.store.one('SELECT sequence FROM collaboration_read_cursors WHERE room_id=? AND user_id=?',
                             (room['id'], principal.user_id))
        return row['sequence'] if row else room['chat_history_sequence']

    def position(self, principal, room, mode, cursor, default=0):
        binding = digest([room['id'], principal.grant_id or principal.user_id, mode])
        value = read_cursor(self.c.secret, binding, cursor) if cursor else default
        if type(value) is not int or value < 0:
            raise DevError('INVALID_CURSOR', '消息游标格式不正确', 400)
        return value

    def cursor(self, principal, room, mode, value):
        return sign_cursor(self.c.secret, digest([room['id'], principal.grant_id or principal.user_id, mode]), value)

    def members(self, room, principal, conversation):
        items = []
        for slot in self.c.joining.list(room, principal, conversation['id']):
            raw = self.c.joining.slot(room, slot['id'])
            access = self.store.one('SELECT * FROM conversation_writers WHERE conversation_id=? AND room_id=? AND grant_id=?', (conversation['id'], room['id'], raw['grant_id']))
            active = bool(access and access['enabled'] and access['expires_at'] > self.c.clock())
            if active:
                try:
                    self.c.grant_reader(room, raw['grant_id'], snapshot=json.loads(access['principal']))
                except DevError:
                    active = False
            agents = self.store.all('SELECT * FROM collaboration_agents WHERE room_id=? AND grant_id=? AND kind=?', (room['id'], raw['grant_id'], raw['kind']))
            eligible = [a['id'] for a in agents if self.c.agent_view(room, a)['binding_status'] == 'enabled']
            routes = self.store.all('''SELECT s.* FROM mcp_event_subscriptions s
                JOIN collaboration_join_routes r ON r.subscription_id=s.id WHERE r.slot_id=? AND s.state='active'
                AND s.expires_at>? AND s.name=? AND COALESCE(NULLIF(json_extract(s.arguments,'$.conversation_id'),''),s.room_id)=?''',
                (slot['id'], self.c.clock(), MESSAGE_EVENT, conversation['id']))
            valid_routes = []
            if self.c.events and self.c.config.events_enabled:
                for route in routes:
                    try:
                        self.c.events.authorize(route, room)
                        valid_routes.append(route)
                    except DevError:
                        continue
            notifications = bool(valid_routes)
            items.append({**slot, 'member_ref': 'slot:' + slot['id'], 'slot_id': slot['id'], 'grant_id': raw['grant_id'],
                          'worker_agent_id': eligible[0] if len(eligible) == 1 else None,
                          'can_speak': active, 'speaking_version': access['version'] if access else 0, 'speaking_expires_at': access['expires_at'] if access else None,
                          'message_notification_state': 'active' if notifications else 'not_subscribed',
                          'message_notification_expires_at': min(raw['expires_at'], max(r['expires_at'] for r in valid_routes)) if valid_routes else None,
                          'message_subscription_request': {'name': MESSAGE_EVENT, 'arguments': {
                              'project_id': room['project_id'], 'environment_id': room['environment_id'], 'slot_id': slot['id'], 'conversation_id': conversation['id']}}})
        return {'items': items, 'next_cursor': None}

    def read(self, args, principal, room):
        self.same_room(room, args)
        conversation = self.c.conversations.resolve(principal, room, args)
        kind, limit = args['kind'], args['limit']
        if kind in {'timeline', 'thread', 'search'}:
            return self.c.conversations.read(args, principal, room)
        if kind == 'members':
            return self.members(room, principal, conversation)
        if kind == 'rooms':
            rows = self.store.all('''SELECT id,project_id,environment_id,state,version,title,topic FROM collaboration_rooms
                WHERE space_id=? AND owner_user_id=? AND project_id=? ORDER BY created,id LIMIT 64''',
                (room['space_id'], room['owner_user_id'], room['project_id']))
            return {'items': rows, 'next_cursor': None}
        if kind == 'message_status':
            if args['id']:
                row = self.c.object('collaboration_messages', room, args['id'])
            else:
                row = self.store.one('''SELECT * FROM collaboration_messages WHERE room_id=? AND author=? AND origin=? AND source_id=?''',
                    (room['id'], principal.grant_id or principal.user_id,
                     'bound_connector' if principal.grant_id else 'panel_owner', args['client_message_id']))
                if not row:
                    raise DevError('NOT_FOUND', '没有找到这次提交的消息', 404)
            if row['conversation_id'] != conversation['id']:
                raise DevError('NOT_FOUND', '此聊天室没有这条消息', 404)
            self.require_message_visible(principal, row)
            view = self.view(row)
            return {'message': view, 'notifications': view['body'].get('notifications', [])}
        if kind == 'changes':
            return self.c.conversations.changes(args, principal, room)
        if kind == '_legacy_changes':
            value = self.position(principal, room, 'changes', args['cursor'])
            rows = self.store.all('''SELECT sequence,kind,object_id,version FROM collaboration_room_changes
                WHERE room_id=? AND sequence>? ORDER BY sequence LIMIT ?''', (room['id'], value, limit + 1))
            selected = rows[:limit]
            return {'items': selected, 'next_cursor': self.cursor(principal, room, 'changes', selected[-1]['sequence'] if selected else value),
                    'has_more': len(rows) > limit, 'reset_required': False}
        if args['cursor'] and args['after']:
            raise DevError('INVALID_CURSOR', '不能同时向前和向后分页', 400)
        mode, params, clauses = 'timeline', [room['id']], ['room_id=?']
        if kind == 'thread':
            root = self.c.object('collaboration_messages', room, args['id'])['thread_root_id']
            clauses.append('thread_root_id=?')
            params.append(root)
            mode += ':thread:' + root
        if kind == 'search':
            query = args['query'].strip()
            if not query:
                raise DevError('INVALID_QUERY', '请输入搜索内容', 422)
            # JSON body is bounded text; escaping keeps wildcard input literal.
            clauses.append("body LIKE ? ESCAPE '\\'")
            params.append('%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            mode += ':search:' + digest(query)
        latest = self.latest(room)
        after = bool(args['after'])
        position = self.position(principal, room, mode, args['after'] or args['cursor'], default=latest + 1)
        clauses.append('server_sequence>?' if after else 'server_sequence<?')
        params.append(position)
        direction = 'ASC' if after else 'DESC'
        rows = self.store.all('SELECT * FROM collaboration_messages WHERE ' + ' AND '.join(clauses) +
                              ' ORDER BY server_sequence ' + direction + ' LIMIT ?', (*params, limit + 1))
        selected = rows[:limit]
        if not after:
            selected.reverse()
        high = selected[-1]['server_sequence'] if selected else (position if after else latest)
        return {'items': self.views(selected, principal=principal),
                'next_cursor': self.cursor(principal, room, mode, selected[0]['server_sequence']) if not after and len(rows) > limit else None,
                'after_cursor': self.cursor(principal, room, mode, high), 'has_more': len(rows) > limit,
                'latest_sequence': latest, 'read_sequence': self.read_sequence(room, principal)}

    def conversation_jobs(self, room, conversation_id, limit):
        rows = self.store.all('''SELECT j.* FROM collaboration_jobs j
            LEFT JOIN collaboration_goals g ON g.id=j.goal_id
            JOIN collaboration_messages m ON m.id=COALESCE(json_extract(j.context,'$.origin_message_id'),g.source_message_id)
            WHERE j.room_id=? AND m.conversation_id=? ORDER BY j.created DESC,j.id DESC LIMIT ?''',
            (room['id'], conversation_id, limit))
        return [self.c.job_view(row) for row in rows]

    def check_object_conversation(self, room, conversation_id, kind, identifier):
        row = self.c.object('collaboration_results' if kind == 'result' else 'collaboration_jobs', room, identifier)
        job = self.c.object('collaboration_jobs', room, row['job_id']) if kind == 'result' else row
        origin = json.loads(job['context']).get('origin_message_id')
        if not origin and job['goal_id']:
            origin = self.c.object('collaboration_goals', room, job['goal_id'])['source_message_id']
        source_conversation = self.c.object('collaboration_messages', room, origin)['conversation_id'] if origin else room['id']
        if source_conversation != conversation_id:
            raise DevError('NOT_FOUND', '此聊天室没有这条任务或结果', 404)

    def conversation_listing(self, principal, room, conversation_id, kind, limit, cursor=''):
        table = 'collaboration_jobs' if kind == 'jobs' else 'collaboration_goals'
        if kind == 'jobs':
            join = """LEFT JOIN collaboration_goals g ON g.id=o.goal_id JOIN collaboration_messages m
                ON m.id=COALESCE(json_extract(o.context,'$.origin_message_id'),g.source_message_id)"""
        else:
            join = 'JOIN collaboration_messages m ON m.id=o.source_message_id'
        binding = digest(['conversation_objects', room['id'], conversation_id, principal.grant_id or principal.user_id, kind])
        where, parameters = '', [room['id'], conversation_id]
        if cursor:
            position = read_cursor(self.c.secret, binding, cursor)
            if not isinstance(position, list) or len(position) != 2 or type(position[0]) not in {int, float} or not isinstance(position[1], str):
                raise DevError('INVALID_CURSOR', '分页游标格式不正确', 400)
            where = ' AND (o.created<? OR (o.created=? AND o.id<?))'
            parameters += [position[0], position[0], position[1]]
        rows = self.store.all(f'SELECT o.* FROM {table} o {join} WHERE o.room_id=? AND m.conversation_id=?' +
                             where + ' ORDER BY o.created DESC,o.id DESC LIMIT ?', (*parameters, limit + 1))
        selected = rows[:limit]
        following = sign_cursor(self.c.secret, binding, [selected[-1]['created'], selected[-1]['id']]) if len(rows) > limit else None
        return {'items': [self.c.job_view(row) for row in selected] if kind == 'jobs' else selected, 'next_cursor': following}
