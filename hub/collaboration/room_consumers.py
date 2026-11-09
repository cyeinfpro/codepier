"""Room-member adapter for the original mailbox, event and lease journals."""
from __future__ import annotations

import json

from hub.collaboration.common import ROOM_INBOX_EVENT, canonical, digest, read_cursor, sign_cursor, validate
from hub.collaboration.consumer_engine import FencedConsumerEngine
from hub.collaboration.dot_mailbox import KINDS, ITEM_BYTE_BUDGET, REFERENCE_KEYS
from shared import room_consumer_contracts as contracts
from shared.util import DevError


ROOM_HANDLING = (
    '用本次唤醒的 wake_ticket 领取消费租约并读取 room_inbox。'
    '已核验本人且在既有项目授权内的明确任务，直接用 room_work_start，无需再次向本人确认。'
    '若本人任务未获准，按 owner_task_blocker 或工具拒绝原因说明，用 room_reply 完成原消息的回复。'
    '用原消息编号、版本、摘要和当前消费凭据提交回复；微信输入已提供 reply_arguments。当前 ChatGPT 对话里的回答不会写回房间或微信。'
    '提交后查看原房间回复和渠道投递状态，再处理后续待办。'
)


class RoomConsumers(FencedConsumerEngine):
    subject_column = 'member_id'
    begin_model = contracts.RoomConsumerBegin
    lease_model = contracts.RoomConsumerLease
    yield_model = contracts.RoomConsumerYield

    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self._after = ''
        self._maintenance_after = ''

    @staticmethod
    def generation(member):
        return 'room:' + member['id'] + ':' + str(member['binding_generation'])

    def mailbox(self):
        return self

    def scope_digest(self, room, member):
        actor = self.c.rooms.principal(member)
        return digest([room['id'], room['state'], member['id'], member['status'],
                       member['scope_revision'], member['binding_generation'],
                       actor.grant_id, sorted(actor.scopes), sorted(actor.projects),
                       self.c.rooms.scopes(member, actor)])

    def consumer_audit(self, room, actor, action, target, details=None):
        self.store.audit(actor, action.replace('dot.', 'room_member.'), target, 'ok',
                         {'conversation_id': room['id'], **(details or {})})

    def scope_updated(self, member, previous_digest, *, additions_only=False):
        """Owner-approved additions keep existing fenced work usable.

        Only an exact still-current scope hash can advance. The identity,
        generation, grant and fence remain unchanged. Dirty revision forces a
        fresh inbox read; original native Goal rules still bound every effect.
        Shrinks/replacements expire the old turn and preserve durable work.
        """
        room=self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(member['conversation_id'],))
        current_digest=None
        try:
            current_digest=self.scope_digest(room,member)
        except DevError:
            pass
        if additions_only and previous_digest and current_digest:
            self.store.execute('''UPDATE dot_consumer_sessions SET scope_digest=?,updated=? WHERE member_id=?
                AND generation=? AND grant_id=? AND state='active' AND scope_digest=? AND lease_until>?''',
                (current_digest,self.c.clock(),member['id'],self.generation(member),member['grant_id'],previous_digest,self.c.clock()))
        else:
            self.store.execute("UPDATE dot_consumer_sessions SET state='expired',updated=? WHERE member_id=? AND state='active'",
                (self.c.clock(),member['id']))
        self.enqueue_ref(member,'control_notice',member['id'],member['scope_revision'],{'reason_code':'member_scope_changed'})

    def binding(self, room, member, principal=None, generation=None):
        pending = bool(member.get('_pending_binding'))
        current = (self.c.rooms.event_member(member['id'], room['id'], member['binding_generation'])
                   if pending else self.c.rooms.member(member['id'], room['id']))
        actor = principal or self.c.rooms.principal(current)
        self.c.rooms.member_scope(actor, room['id'], current['id'], current['binding_generation'],
                                  pending=True)
        internal = self.generation(current)
        if generation is not None and generation != internal:
            raise DevError('ROOM_BINDING_CHANGED', '消费者绑定已改变，请恢复当前成员连接', 409)
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (current['subscription_id'],))
        if (not sub or sub['state'] != 'active' or sub['expires_at'] <= self.c.clock()
                or sub['grant_id'] != actor.grant_id or sub['conversation_id'] != room['id']):
            raise DevError('ROOM_SUBSCRIPTION_INACTIVE', '原成员通知订阅尚未就绪或已经失效', 409)
        self.c.rooms.event_authorize(sub, json.loads(sub['arguments']))
        return {**current, 'generation': internal, 'grant_id': actor.grant_id}, sub

    def scope(self, raw, principal, model):
        args = validate(model, raw)
        actor, room, member = self.c.rooms.member_scope(principal, args['conversation_id'], args['member_id'],
            args['binding_generation'], args['connection_proof'], require_proof=True)
        binding, sub = self.binding(room, member, actor, self.generation(member))
        return args, actor, room, member, member, binding, sub

    def _reply(self, row, *, include_proof=False):
        result = super()._reply(row, include_proof=include_proof)
        result['binding_generation'] = int(row['generation'].rsplit(':', 1)[1])
        return result

    def current_revision(self, member_id, generation=None):
        member = self.c.rooms.member(member_id)
        if generation is not None and generation != self.generation(member):
            raise DevError('ROOM_BINDING_CHANGED', '收件箱绑定已改变', 409)
        return member['dirty_revision']

    def authorize_member(self, raw, principal, *, require_revision=True):
        """The caller holds the same transaction as the protected effect."""
        actor, room, member = self.c.rooms.member_scope(principal, raw.get('conversation_id'), raw.get('member_id'),
            raw.get('binding_generation'), raw.get('connection_proof'), require_proof=True)
        if not member['probe_replied_at']:
            raise DevError('ROOM_PROBE_REQUIRED', '先完成当前成员的真实通知读取和回复验证', 409)
        self.guard_session(raw, actor, room, member, require_revision=require_revision)
        return member

    def guard_session(self, raw, principal, room, member, *, require_revision=True):
        """For original work APIs, session proof also binds the stable member."""
        self.c.rooms.require_native(room)
        if not member['probe_replied_at']:
            raise DevError('ROOM_PROBE_REQUIRED', '先完成当前成员的真实通知读取和回复验证', 409)
        binding, _ = self.binding(room, member, principal)
        row = self._credential(raw, principal, room, member, binding)
        expected = raw.get('expected_inbox_revision')
        if require_revision and (isinstance(expected, bool) or not isinstance(expected, int)
                or expected != member['dirty_revision']):
            raise DevError('ROOM_INBOX_CHANGED', '房主输入或工作状态已更新，请先读取当前收件箱', 409,
                           inbox_revision=member['dirty_revision'])
        return row

    def verify_probe_ticket(self, raw, principal, member):
        room = self.c.conversations.object(principal, member['conversation_id'])
        binding, sub = self.binding(room, member, principal)
        ticket = self.store.one('SELECT * FROM dot_wake_tickets WHERE ticket_hash=?', (digest(raw['wake_ticket']),))
        if (not ticket or ticket['member_id'] != member['id'] or ticket['generation'] != binding['generation']
                or ticket['subscription_id'] != sub['id'] or ticket['grant_id'] != principal.grant_id
                or ticket['expires_at'] <= self.c.clock()):
            raise DevError('ROOM_PROBE_TICKET_INVALID', '连接检查需要当前成员实际收到的原通知票据', 403)
        return ticket

    def connection_probe(self, member):
        """An actual host callback, with no business message or work creation."""
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        binding, sub = self.binding(room, member)
        if member.get('_pending_binding') or not room['room_protocol']:
            self._wake(member, 'connection:' + sub['id'] + ':' + str(sub['version']))
            return
        self.activate(room, member, member, binding['generation'], sub['id'])
        self.catch_up(member)
        member = self.c.rooms.member(member['id'])
        self._wake(member, 'connection:' + sub['id'] + ':' + str(sub['version']))

    def binding_activated(self, member):
        """Called inside the atomic cutover, after the pending probe succeeded."""
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        if not room['room_protocol']:
            return  # Verified member, but the owner has not switched the room.
        generation = self.generation(member)
        self.activate(room, member, member, generation, member['subscription_id'])
        self.catch_up(member)
        self._wake(self.c.rooms.member(member['id']), 'binding-activated')

    def catch_up(self, member):
        """Transfer pending references to a new generation, never recreate work.

        Supersession is a routing receipt, separate from acknowledgement. It
        prevents a later rebind from resurrecting already forwarded input.
        """
        generation = self.generation(member)
        after = 0
        while True:
            rows = self.store.all('''SELECT * FROM dot_mailbox_entries WHERE member_id=?
                AND generation!=? AND acknowledged_at IS NULL AND superseded_by=''
                AND sequence>? ORDER BY sequence LIMIT 64''', (member['id'], generation, after))
            for row in rows:
                self.enqueue_ref(member, row['kind'], row['object_id'], row['object_version'], json.loads(row['reference']))
                forwarded = digest(['room-entry', member['id'], generation, row['kind'], row['object_id'], row['object_version']])
                self.store.execute('UPDATE dot_mailbox_entries SET superseded_by=? WHERE id=?', (forwarded, row['id']))
            if len(rows) < 64:
                return
            after = rows[-1]['sequence']

    def delivery_payload(self, subscription, payload, event_id):
        if payload.get('test'):
            return {**payload, 'wake_ticket': None}
        member = self.c.rooms.event_member(payload['member_id'], payload['conversation_id'], payload['binding_generation'])
        self.c.rooms.event_authorize(subscription, json.loads(subscription['arguments']), payload)
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        ticket = self.issue_ticket(room, member, subscription, self.generation(member), event_id)
        return {**payload, 'wake_ticket': ticket}

    def _wake(self, member, reason='dirty'):
        if not member['grant_id'] or member['status'] in {'paused', 'removed', 'legacy'}:
            return None
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        if not room or room['state'] != 'active':
            return None
        payload = {'schema_version': 1, 'owner_user_id': member['owner_user_id'],
            'conversation_id': member['conversation_id'], 'member_id': member['id'],
            'binding_generation': member['binding_generation'], 'dirty_revision': member['dirty_revision'],
            'test': False, 'test_subscription_id': ''}
        identifier = 'room_' + digest([member['id'], member['binding_generation'], member['dirty_revision'],
                                      member['wake_generation'], reason])
        self.store.execute('''INSERT OR IGNORE INTO mcp_event_outbox
            (id,room_id,conversation_id,name,object_id,object_version,target_grant_id,data,created)
            VALUES (?,NULL,?,?,?,?,?,?,?)''', (identifier, member['conversation_id'], ROOM_INBOX_EVENT,
                identifier, 1, member['grant_id'], canonical(payload), self.c.clock()))
        loops = getattr(self.c, 'loops', None)
        if loops is not None:
            self.store.after_commit(loops.wake_events)
        self.store.after_commit(self.c.relay_hints.wake)
        return identifier

    def enqueue(self, message, member, principal):
        if message['conversation_id'] != member['conversation_id']:
            raise DevError('ROOM_INPUT_SCOPE_CHANGED', '消息不属于此成员房间', 403)
        return self.enqueue_ref(member, 'message', message['id'], message['version'], {'message_id': message['id']})

    def enqueue_ref(self, member, kind, object_id, version, reference=None):
        member = self.c.rooms.member(member if isinstance(member, str) else member['id'])
        reference = reference or {}
        if (kind not in KINDS or not isinstance(object_id, str) or not 1 <= len(object_id) <= 128
                or isinstance(version, bool) or not isinstance(version, int) or version < 0
                or not isinstance(reference, dict) or set(reference) - REFERENCE_KEYS
                or any(not isinstance(v, str) or len(v) > 128 for v in reference.values())):
            raise DevError('ROOM_INBOX_REFERENCE_INVALID', '收件箱只接受原对象的有界引用', 422)
        with self.store.transaction():
            member = self.c.rooms.member(member['id'])
            if kind == 'message':
                message = self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                                         (object_id, member['conversation_id']))
                if not message:
                    raise DevError('ROOM_INPUT_SCOPE_CHANGED', '消息不属于此成员房间', 403)
                reference = {'message_id': message['id'], 'thread_root_id': message['thread_root_id'] or message['id']}
            elif kind == 'work_ready':
                work = self.store.one('SELECT * FROM coordination_work WHERE id=?', (object_id,))
                if work:
                    reference = {'goal_id': work['goal_id'], 'approval_id': work['approval_id'], 'work_item_id': work['id']}
            generation = self.generation(member)
            identifier = digest(['room-entry', member['id'], generation, kind, object_id, version])
            previous = self.store.one('SELECT reference FROM dot_mailbox_entries WHERE id=?', (identifier,))
            if previous:
                if previous['reference'] != canonical(reference):
                    raise DevError('ROOM_INBOX_ENTRY_CONFLICT', '原对象同一版本不能改成另一条引用', 409)
                return member['dirty_revision']
            now, revision = self.c.clock(), member['dirty_revision'] + 1
            self.store.execute('''INSERT INTO dot_mailbox_entries
                (id,member_id,generation,kind,object_id,object_version,reference,revision,created)
                VALUES (?,?,?,?,?,?,?,?,?)''', (identifier, member['id'], generation, kind, object_id,
                    version, canonical(reference), revision, now))
            self.store.execute('UPDATE room_members SET dirty_revision=?,wake_count=0,last_wake_at=?,updated_at=? WHERE id=?',
                               (revision, now, now, member['id']))
            self._wake(self.c.rooms.member(member['id']))
            return revision

    def _visible_message(self, member, principal, message):
        if not message or message['conversation_id'] != member['conversation_id']:
            return False
        if not self.c.chatroom.message_visible(principal, message):
            return False
        scopes=[s for s in self.c.rooms.scopes(member,principal) if 'read' in s['capabilities']]
        allowed = {s['project_id'] for s in scopes}
        body=json.loads(message['body'])
        provenance = set(body.get('provenance_project_ids', []))
        if not {(s['project_id'],s['environment_id']) for s in body.get('provenance_scopes',[])} <= {
                (scope['project_id'],scope['environment_id']) for scope in scopes}:
            return False
        if message.get('room_id'):
            partition=self.store.one('SELECT project_id,environment_id FROM collaboration_rooms WHERE id=?',(message['room_id'],))
            if not partition or (partition['project_id'],partition['environment_id']) not in {
                    (scope['project_id'],scope['environment_id']) for scope in scopes}:
                return False
            provenance.add(partition['project_id'])
        return provenance <= allowed

    def _entry(self, row, member, principal):
        if row['acknowledged_at'] is not None or row['superseded_by']:
            return None
        reference = json.loads(row['reference'])
        item = {'id': row['id'], 'kind': row['kind'], 'object_id': row['object_id'],
                'object_version': row['object_version'], 'revision': row['revision'],
                'reference': reference, 'ready': True}
        if row['kind'] == 'message':
            message = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (row['object_id'],))
            if not self._visible_message(member, principal, message):
                return None
            item['message'] = self.c.room_runtime.message_view(message, principal)
            item['source_version'] = message['version']
            item['source_digest'] = digest(json.loads(message['body']).get('body_text', ''))
            if item['message'].get('source_channel') == 'weixin':
                source = {'source_message_id': message['id'], 'source_version': item['source_version'],
                          'source_digest': item['source_digest'], 'expected_inbox_revision': member['dirty_revision']}
                item['reply_arguments'] = source
                if item['message'].get('external_execution_authority'):
                    item['work_start_arguments'] = {**source, 'confirm_task': True}
        elif row['kind'] in {'work_ready', 'result_ready', 'peer_handoff'}:
            goal_id = reference.get('goal_id')
            if goal_id:
                goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (goal_id,))
                if not goal or goal['conversation_id'] != member['conversation_id']:
                    return None
                try:
                    item['work'] = self.c.room_runtime.work_view(goal, principal,member_id=member['id'])
                    if row['kind']=='peer_handoff' and self.c.room_work_bridge.native.reference(goal):
                        item['handoff']=self.c.room_goal_handoffs.read(member,reference.get('handoff_id',row['object_id']),principal)
                except DevError:
                    return None
                native=self.c.room_work_bridge.native
                projects = set(json.loads(goal['spec'])['project_ids'])
                allowed = {s['project_id'] for s in self.c.rooms.scopes(member, principal) if s['capabilities']}
                if not native.reference(goal) and not projects <= allowed:
                    return None
                if row['kind'] == 'work_ready':
                    work = self.store.one('SELECT * FROM coordination_work WHERE id=?',
                                          (reference.get('work_item_id', row['object_id']),))
                    item['ready'] = bool(work and work['state'] in {'queued', 'leased', 'running'})
                    if native.reference(goal):
                        assignment=self.store.one('SELECT member_id FROM room_native_work_assignments WHERE work_id=?',(work['id'],)) if work else None
                        if not assignment or assignment['member_id']!=member['id'] or work['project_id'] not in allowed:
                            return None
                        item['work_item']=self.c.coordination.work_view(work)
                        environment=next((r['environment_id'] for r in native.record(native.reference(goal))['projects']
                            if r['project_id']==work['project_id']),None)
                        if environment is None:
                            raise DevError('ROOM_WORK_SCOPE_DENIED','工作项目不在原批准范围中',403)
                        item['work_route']={'project':work['project_id'],'environment_id':environment,
                            'goal_id':goal['id'],'work_item_id':work['id'],'expected_version':work['version'],
                            'coordinator_epoch':self.c.room_goal_recovery.epoch(goal['id']),
                            'expected_intent_revision':self.c.room_runtime.intent_revision(goal['id'])}
        return item

    def _ready(self, member, principal):
        after = 0
        while True:
            rows = self.store.all('''SELECT * FROM dot_mailbox_entries WHERE member_id=? AND generation=?
                AND acknowledged_at IS NULL AND superseded_by='' AND sequence>? ORDER BY sequence LIMIT 64''',
                (member['id'], self.generation(member), after))
            for row in rows:
                item = self._entry(row, member, principal)
                if item and item['ready']:
                    return True
            if len(rows) < 64:
                return False
            after = rows[-1]['sequence']

    def mark_drained(self, member_id, generation, expected_revision):
        member = self.c.rooms.member(member_id)
        if generation != self.generation(member) or member['dirty_revision'] != expected_revision:
            return False
        if self._ready(member, self.c.rooms.principal(member)):
            return False
        return bool(self.store.execute('''UPDATE room_members SET drained_revision=?,updated_at=?
            WHERE id=? AND binding_generation=? AND dirty_revision=?''',
            (expected_revision, self.c.clock(), member_id, member['binding_generation'], expected_revision)).rowcount)

    def ack_message(self, member, message_id):
        """Internal only, after a durable reply or managed work admission."""
        self.store.execute('''UPDATE dot_mailbox_entries SET acknowledged_at=COALESCE(acknowledged_at,?)
            WHERE member_id=? AND kind='message' AND object_id=?''',
            (self.c.clock(), member['id'], message_id))

    def read(self, raw, principal):
        with self.store.transaction(immediate=False):
            _, _, _, _, member, _, _ = self.scope(raw, principal, contracts.RoomConsumerRead)
            latest = self.store.one('SELECT * FROM dot_consumer_sessions WHERE member_id=? ORDER BY fence DESC LIMIT 1', (member['id'],))
            return {'member_id': member['id'], 'conversation_id': member['conversation_id'],
                'binding_generation': member['binding_generation'], 'inbox_revision': member['dirty_revision'],
                'consumer': self._reply(latest) if latest else None, 'read_only': True, 'grants_authority': False}

    def inbox(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, actor, room, _, member, binding, _ = self.scope(raw, principal, contracts.RoomInboxRead)
            cursor_scope = digest(['room-inbox', member['id'], binding['generation'], self.scope_digest(room, member)])
            after = read_cursor(self.c.secret, cursor_scope, args['cursor']) if args['cursor'] else 0
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise DevError('INVALID_CURSOR', '收件箱游标无效', 400)
            rows = self.store.all('''SELECT * FROM dot_mailbox_entries WHERE member_id=? AND generation=?
                AND acknowledged_at IS NULL AND superseded_by=''
                AND sequence>? ORDER BY sequence LIMIT ?''', (member['id'], binding['generation'], after, args['limit'] + 1))
            items, count, scanned = [], 0, after
            limited = False
            for row in rows[:args['limit']]:
                item = self._entry(row, member, actor)
                if item:
                    size = len(canonical(item).encode())
                    if items and count + size > ITEM_BYTE_BUDGET:
                        limited = True
                        break
                    if size > ITEM_BYTE_BUDGET:
                        # Return the durable reference; never drop/ack an input
                        # simply because its complete view needs a separate read.
                        item = {k: v for k, v in item.items() if k not in {'message', 'work', 'handoff'}}
                        item['content_requires_read'] = True
                        item['read_action'] = ('room_message_read' if row['kind'] == 'message' else
                            'room_work_handoff_read' if row['kind']=='peer_handoff' else 'goal')
                        if row['kind']=='peer_handoff':
                            item['read_arguments']={'conversation_id':member['conversation_id'],'member_id':member['id'],
                                'binding_generation':member['binding_generation'],'handoff_id':row['object_id']}
                            item['credential_required']='current_member_connection_proof'
                    items.append(item)
                    count += len(canonical(item).encode())
                scanned = row['sequence']
            more = limited or len(rows) > args['limit']
            context_reference={'action':'room_context',
                **{k:args[k] for k in ('conversation_id','member_id','binding_generation')},
                'credential_required':'current_member_connection_proof'}
            return {'conversation_id': room['id'], 'member_id': member['id'],
                'binding_generation': member['binding_generation'], 'inbox_revision': member['dirty_revision'],
                'drained_revision': member['drained_revision'], 'items': items,
                'next_cursor': sign_cursor(self.c.secret, cursor_scope, scanned) if more else None,
                'pagination_complete': not more, 'read_only': True, 'read_is_consumption': False,
                'grants_authority': False, 'context_reference':context_reference,
                'context_version':self.c.room_context.version(room['id']),
                'handling_instructions': ROOM_HANDLING,
                'actions': {'reply': 'room_reply', 'work': 'room_work_start',
                    'ack_notice': 'room_inbox_ack', 'yield': 'room_consumer_yield'}}

    def message(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, actor, _, _, member, _, _ = self.scope(raw, principal, contracts.RoomMessageRead)
            row = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (args['message_id'],))
            if not self._visible_message(member, actor, row):
                raise DevError('ROOM_SOURCE_NOT_FOUND', '当前成员范围没有这条消息', 404)
            return {'message': self.c.room_runtime.message_view(row, actor), 'source_version': row['version'],
                    'source_digest': digest(json.loads(row['body']).get('body_text', '')),
                    'read_only': True, 'read_is_consumption': False, 'grants_authority': False}

    def ack(self, raw, principal):
        with self.store.transaction():
            args, actor, room, _, member, _, _ = self.scope(raw, principal, contracts.RoomInboxAck)
            self.guard_session(args, actor, room, member)
            rows = []
            for identifier in dict.fromkeys(args['entry_ids']):
                row = self.store.one('SELECT * FROM dot_mailbox_entries WHERE id=? AND member_id=? AND generation=?',
                    (identifier, member['id'], self.generation(member)))
                if not row or row['kind'] not in {'peer_handoff', 'result_ready', 'control_notice'}:
                    raise DevError('ROOM_INBOX_ACK_DENIED', '用户输入和工作必须通过原处理流程完成', 403)
                if row['acknowledged_at'] is None and self._entry(row, member, actor) is None:
                    raise DevError('ROOM_INBOX_ENTRY_OBSOLETE', '原提示已失效，请重新读取', 409)
                rows.append(row)
            def save():
                for row in rows:
                    self.store.execute('UPDATE dot_mailbox_entries SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?',
                                       (self.c.clock(), row['id']))
                return {'entry_ids': [row['id'] for row in rows], 'acknowledged': True, 'work_completed': False}
            mutation = {k: v for k, v in args.items() if k not in {'consumer_proof', 'connection_proof'}}
            return self.c.rooms.mutation(actor, 'room_inbox_ack', mutation, save)

    def _rejected(self, subscription_id):
        return self.c.dot_mailbox._permanently_rejected(subscription_id)

    def request_recovery(self, raw, principal):
        with self.store.transaction():
            args, actor, _, _, member, _, sub = self.scope(raw, principal, contracts.RoomInboxRecover)
            def save():
                now = self.c.clock()
                if (not self._ready(member, actor) or self.store.one('''SELECT 1 FROM dot_consumer_sessions
                    WHERE member_id=? AND state='active' AND lease_until>?''', (member['id'], now))):
                    return {'wakeup_queued': False, 'state': 'no_idle_input'}
                count = member['manual_wakes'] if now - member['manual_window_at'] < 3600 else 0
                if now - member['last_manual_wake_at'] < 600 or count >= 3 or self._rejected(sub['id']):
                    raise DevError('ROOM_RECOVERY_UNAVAILABLE', '请核对原订阅或稍后恢复，输入仍保留', 409)
                self.store.execute('''UPDATE room_members SET wake_generation=wake_generation+1,
                    manual_wakes=?,manual_window_at=?,last_manual_wake_at=?,last_wake_at=? WHERE id=?''',
                    (count + 1, member['manual_window_at'] if count else now, now, now, member['id']))
                self._wake(self.c.rooms.member(member['id']), 'manual')
                return {'wakeup_queued': True, 'delivery_scope': 'original_subscription'}
            return self.c.rooms.mutation(actor, 'room_inbox_recover', args, save)

    def recover(self, *, max_wakes=4):
        """Bounded deterministic reminders; no idle model polling or execution."""
        if not self.c.config.enabled or not self.c.config.events_enabled or max_wakes <= 0:
            return 0
        with self.store.transaction():
            count = self._maintain(max_wakes)
            if count >= max_wakes:
                return count
            rows = self.store.all('''SELECT * FROM room_members WHERE status='bound' AND id>?
                AND dirty_revision>drained_revision AND wake_count<3 ORDER BY id LIMIT 64''', (self._after,))
            self._after = rows[-1]['id'] if len(rows) == 64 else ''
            now = self.c.clock()
            for member in rows:
                if count >= max_wakes:
                    break
                if now - member['last_wake_at'] < (30, 120, 600)[member['wake_count']]:
                    continue
                if self.store.one("SELECT 1 FROM dot_consumer_sessions WHERE member_id=? AND state='active' AND lease_until>?", (member['id'], now)):
                    continue
                try:
                    actor = self.c.rooms.principal(member)
                    room = self.c.conversations.object(actor, member['conversation_id'])
                    _, sub = self.binding(room, member, actor)
                    if self._rejected(sub['id']) or not self._ready(member, actor):
                        continue
                except DevError:
                    continue
                self.store.execute('UPDATE room_members SET wake_count=wake_count+1,last_wake_at=? WHERE id=?', (now, member['id']))
                self._wake(self.c.rooms.member(member['id']), 'recovery:' + str(member['wake_count'] + 1))
                count += 1
            return count

    def _maintain(self, maximum):
        now = self.c.clock()
        rows = self.store.all('''SELECT m.*,s.version AS subscription_version,s.expires_at,s.updated
            FROM room_members m JOIN mcp_event_subscriptions s ON s.id=m.subscription_id
            WHERE m.status='bound' AND s.state='active' AND s.expires_at>?
              AND s.expires_at<=? AND m.id>? ORDER BY m.id LIMIT 64''',
            (now, now + 3600, self._maintenance_after))
        self._maintenance_after = rows[-1]['id'] if len(rows) == 64 else ''
        count = 0
        for member in rows:
            if count >= maximum:
                break
            window = min(3600, max(15, (member['expires_at'] - member['updated']) * .2))
            if member['expires_at'] - now > window:
                continue
            object_id = 'subscription-expiry:' + member['subscription_id']
            if self.store.one('''SELECT 1 FROM dot_mailbox_entries WHERE member_id=? AND generation=?
                AND kind='control_notice' AND object_id=? AND object_version=?''',
                (member['id'], self.generation(member), object_id, member['subscription_version'])):
                continue
            try:
                actor = self.c.rooms.principal(member)
                room = self.c.conversations.object(actor, member['conversation_id'])
                _, sub = self.binding(room, member, actor)
                if self._rejected(sub['id']):
                    continue
            except DevError:
                continue
            self.enqueue_ref(member, 'control_notice', object_id, member['subscription_version'],
                             {'reason_code': 'subscription_expiring'})
            count += 1
        return count

    def invoke(self, action, raw, principal):
        if action=='room_context':
            return self.c.room_context.read(raw,principal)
        if action=='room_context_update':
            return self.c.room_context.update(raw,principal)
        return {'room_consumer': self.read, 'room_inbox': self.inbox, 'room_message_read': self.message, 'room_consumer_begin': self.begin,
                'room_attachment_read': self.c.room_attachments.read,
                'room_consumer_heartbeat': self.heartbeat, 'room_consumer_yield': self.yield_session,
                'room_inbox_ack': self.ack, 'room_inbox_recover': self.request_recovery}[action](raw, principal)
