"""Durable Weixin replies. Unknown send outcomes are never retried."""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

from hub.collaboration.common import canonical, digest
from hub.collaboration import weixin_delivery_order as order
from hub.collaboration import weixin_schema as models
from hub.collaboration.weixin_protocol import encode_request, send_payload
from shared.util import DevError

DONE = frozenset({'accepted', 'owner_confirmed', 'cancelled', 'rejected'})
READY = frozenset({'queued', 'waiting_context', 'waiting_media'})
TASK_STATUSES = {
    'CHANNEL_OWNER_REVOKED': ('authorization_revoked', '任务授权已撤销，后续操作已停止。'),
    'CHANNEL_OWNER_NOT_APPROVED': ('authorization_revoked', '任务授权已撤销，后续操作已停止。'),
    'CHANNEL_OWNER_SCOPE_CHANGED': ('scope_changed', '任务权限范围已变化，超出当前授权的操作已停止。'),
    'CHANNEL_OWNER_SCOPE_DENIED': ('scope_changed', '任务权限范围已变化，超出当前授权的操作已停止。'),
    'ROOM_WORK_SCOPE_CHANGED': ('scope_changed', '任务权限范围已变化，超出当前授权的操作已停止。'),
    'ROOM_WORK_SCOPE_DENIED': ('scope_changed', '任务权限范围已变化，超出当前授权的操作已停止。'),
    'ROOM_WORK_APPROVAL_EXPIRED': ('authorization_expired', '任务授权已到期，后续操作已停止。'),
}
STATUS_TEXT = dict(TASK_STATUSES.values())
TERMINAL_SOURCE = frozenset(TASK_STATUSES) | {
    'CHANNEL_OWNER_SOURCE_CHANGED', 'CHANNEL_OWNER_SOURCE_SUPERSEDED', 'CHANNEL_OWNER_PEER_CHANGED',
    'WEIXIN_DELIVERY_SOURCE_CHANGED', 'WEIXIN_DELIVERY_CONTENT_CHANGED', 'WEIXIN_DELIVERY_SCOPE_CHANGED',
    'WEIXIN_BINDING_INACTIVE', 'WEIXIN_CREDENTIAL_CHANGED', 'WEIXIN_RECOVERY_SOURCE_CHANGED',
}
RATE_LIMITED = 'WEIXIN_RATE_LIMITED'
REQUEST_LIMIT = 'WEIXIN_REQUEST_LIMIT'
MAX_SEND_ATTEMPTS = 6


@dataclass(frozen=True)
class EnqueueResult:
    disposition: Literal['queued', 'existing', 'not_requested', 'blocked', 'rejected']
    reason_code: str
    binding_id: str = ''
    delivery: dict | None = None

    def view(self):
        result = {'channel': 'weixin', 'disposition': self.disposition, 'reason_code': self.reason_code}
        if self.binding_id:
            result['binding_id'] = self.binding_id
        if self.delivery:
            result['delivery_id'] = self.delivery['id']
        return result


def text_items(value):
    return [{'type': 1, 'text_item': {'text': value}}]


def text_chunks(value):
    pieces, chunk, count = [], [], 0
    for char in value:
        size = len(char.encode())
        if count + size > 3000:
            pieces.append(''.join(chunk))
            chunk, count = [], 0
        chunk.append(char)
        count += size
    if chunk:
        pieces.append(''.join(chunk))
    return pieces


class WeixinDelivery:
    def __init__(self, channel):
        self.w, self.c, self.store = channel, channel.c, channel.store

    def root_source(self, message):
        current, seen = message, set()
        for _ in range(16):
            if current['id'] in seen:
                return None
            seen.add(current['id'])
            if current['origin'] == 'external_channel':
                return current
            if not current['reply_to_id']:
                if json.loads(message['body']).get('publish_work') is False:
                    return None
                return self.work_source(message, current)
            current = self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                                    (current['reply_to_id'], message['conversation_id']))
            if not current:
                return None
        return None

    def work_source(self, message, intent_source):
        if not message.get('goal_id'):
            return None
        goal = self.store.one('SELECT * FROM coordination_goals WHERE id=? AND conversation_id=?',
                              (message['goal_id'], message['conversation_id']))
        native = self.c.room_work_bridge.native
        reference = native.reference(goal) if goal else None
        if not reference:
            return None
        body = json.loads(message['body'])
        intent = self.store.one('''SELECT * FROM room_work_intent_sources
            WHERE goal_id=? AND intent_revision=? AND source_message_id=?''',
            (goal['id'], body.get('intent_revision'), intent_source['id']))
        if (not intent or intent['source_version'] != intent_source['version']
                or intent['source_digest'] != digest(json.loads(intent_source['body']).get('body_text', ''))):
            return None
        try:
            original = native.original(reference)
        except DevError:
            return None
        source = self.store.one('''SELECT * FROM collaboration_messages
            WHERE id=? AND conversation_id=? AND origin='external_channel' AND version=?''',
            (original['source_message_id'], message['conversation_id'], original['source_version']))
        if not source:
            return None
        original_body = json.loads(source['body'])
        if (not original_body.get('external_origin', {}).get('native_room')
                or original['source_digest'] != digest(original_body.get('body_text', ''))):
            return None
        return source

    def related_goal(self, message, source):
        goal_id = message.get('goal_id') or source.get('goal_id')
        if goal_id:
            return goal_id
        row = self.store.one("""SELECT MIN(goal_id) AS goal_id FROM delegation_requests
            WHERE message_id=? AND COALESCE(goal_id,'')!='' HAVING COUNT(DISTINCT goal_id)=1""", (source['id'],))
        return row['goal_id'] if row else None

    def task_author(self, message, source, body):
        native = self.c.room_channels.native
        author = native.reply_author(message, source)
        if (message.get('goal_id') and body.get('publish_work') is not False
                or body.get('provenance_project_ids') or body.get('dot_reply_complete') is False):
            if not author['authenticated']:
                raise DevError('CHANNEL_OWNER_NOT_APPROVED', '此微信来源没有有效的工作回传授权', 403)
        return author

    def queue_reply(self, message):
        return self.queue_reply_result(message).delivery

    def queue_reply_result(self, message):
        self.store.require_transaction()
        body = json.loads(message['body'])
        if message['origin'] != 'bound_connector' or not (body.get('sender_dot_id') or body.get('sender_member_id')):
            return EnqueueResult('not_requested', 'no_external_reply')
        source = self.root_source(message)
        origin = json.loads(source['body']).get('external_origin', {}) if source else {}
        if origin.get('channel') != 'weixin':
            return EnqueueResult('not_requested', 'no_weixin_source')
        binding_id = origin.get('binding_id', '')
        if not self.c.config.enabled:
            return EnqueueResult('blocked', 'channel_disabled', binding_id)
        native = self.c.room_channels.native if origin.get('native_room') else None
        try:
            if native:
                candidate = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (binding_id,))
                binding, account, binding_owner, _, member = native.reply_binding(
                    candidate, message, source, allow_disconnected=True)
                self.c.chatroom.require_message_visible(binding_owner, message)
                if not native.sender_matches(binding, message, source, allow_disconnected=True):
                    return EnqueueResult('blocked', 'sender_changed', binding_id)
                sender = member['id']
            else:
                binding, account, _, _, member = self.w.valid(binding_id)
                sender = body.get('sender_dot_id')
            if not binding['allow_replies']:
                return EnqueueResult('blocked', 'reply_permission_revoked', binding_id)
            if (origin.get('generation') != binding['generation'] or origin.get('peer_id') != binding['peer_id']
                    or sender != member['id']):
                return EnqueueResult('blocked', 'recipient_changed', binding_id)
        except DevError as exc:
            return EnqueueResult('blocked', exc.code, binding_id)

        try:
            if native:
                self.task_author(message, source, body)
            else:
                self.c.channel_owner.require_reply_source(source)
                author = self.c.channel_owner.author(source)
                if body.get('dot_reply_complete') is False and not author['authenticated']:
                    return EnqueueResult('blocked', 'reply_permission_revoked', binding_id)
        except DevError as exc:
            if native:
                self.queue_task_status(source, exc.code, projection_message_id=message['id'])
            return EnqueueResult('blocked', exc.code, binding_id)

        old = self.store.one('''SELECT * FROM weixin_deliveries
            WHERE binding_id=? AND generation=? AND message_id=? AND message_version=?''',
            (binding['id'], binding['generation'], message['id'], message['version']))
        if old:
            return EnqueueResult('existing', '', binding_id, old)
        ordering, evidence, predecessor = order.reply_ordering(self.store, message, source, binding)
        now, identifier = self.c.clock(), uuid.uuid4().hex
        pending = self.store.one('''SELECT COUNT(*) AS n FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
            WHERE b.account_id=? AND d.created>? AND d.state NOT IN ('accepted','owner_confirmed','cancelled')''',
            (account['id'], now - 86400))['n']
        self.store.execute('''INSERT INTO weixin_deliveries
            (id,binding_id,generation,message_id,message_version,message_digest,source_message_id,peer_id,
             state,created,updated,ordering_key,depends_on_delivery_id,ordering_evidence,
             ordering_schema_version,ordering_checked_version)
            VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)''',
            (identifier, binding['id'], binding['generation'], message['id'], message['version'], digest(body),
             source['id'], binding['peer_id'], now, now, ordering, predecessor, evidence,
             order.ORDERING_VERSION, order.ORDERING_VERSION))
        if pending >= 1000:
            self.add_part(identifier, 0, 'text', {'text': ''})
            self.store.execute("UPDATE weixin_parts SET state='rejected',reason_code='daily_reply_quota' WHERE delivery_id=?", (identifier,))
            self.refresh(identifier)
            delivery = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))
            return EnqueueResult('rejected', 'daily_reply_quota', binding_id, delivery)
        pieces = text_chunks(body.get('body_text', ''))
        for ordinal, piece in enumerate(pieces):
            self.add_part(identifier, ordinal, 'text', {'text': piece})
        reason = ''
        if not pieces:
            self.store.execute("UPDATE weixin_deliveries SET state='waiting_media' WHERE id=?", (identifier,))
        elif account['state'] != 'active' or not self.store.one("""SELECT 1 FROM weixin_contexts
                WHERE account_id=? AND generation=? AND peer_id=?""",
                (account['id'], binding['generation'], binding['peer_id'])):
            reason = 'context_missing' if account['state'] == 'active' else 'account_waiting_reconnect'
            self.store.execute("UPDATE weixin_parts SET state='waiting_context',reason_code=? WHERE delivery_id=?",
                               (reason, identifier))
            self.refresh(identifier)
        delivery = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))
        return EnqueueResult('queued', reason, binding_id, delivery)

    def status_source(self, source):
        body = json.loads(source['body'])
        if source['origin'] != 'external_channel' or not body.get('external_origin', {}).get('native_room'):
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED', '原任务通知没有可核验的微信来源', 403)
        binding, account, owner, *_ = self.c.room_channels.native.source_binding(source)
        if not binding['allow_replies']:
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED', '原微信回复许可已撤销', 403)
        source_digest = digest(body.get('body_text', ''))
        proof = self.store.one('SELECT message_digest FROM room_channel_owner_inputs WHERE message_id=?', (source['id'],))
        if proof and proof['message_digest'] == source_digest:
            return binding, account, owner, source_digest

        # An explicitly continued historical source may lack an intake task
        # proof. The approved continuation still binds the same original chat.
        goal_id = self.related_goal({}, source)
        goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (goal_id,)) if goal_id else None
        authority = self.c.room_work_bridge.native.input_authority
        evidence = authority.proof(source, goal=goal) or {}
        channel = evidence.get('channel') or {}
        if (not evidence.get('explicit_continuation')
                or any(evidence.get(k) != v for k, v in authority.source_key(source).items())
                or channel.get('binding_id') != binding['id']
                or any(channel.get(k) != binding[k] for k in ('account_id','peer_id','generation','credential_version'))):
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED', '原任务通知没有可核验的微信来源', 403)
        return binding, account, owner, source_digest

    def queue_task_status(self, source, reason, *, projection_message_id=None):
        status = TASK_STATUSES.get(reason)
        if not status or not source or not self.c.config.enabled:
            return None
        with self.store.transaction():
            try:
                binding, _, _, source_digest = self.status_source(source)
            except DevError:
                return None
            code, notice = status
            identifier = digest(['weixin-task-status-v1', binding['id'], binding['generation'],
                                 source['id'], source['version'], code])[:32]
            old = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))
            if old:
                return old
            now = self.c.clock()
            work_id = self.related_goal({}, source)
            revision = self.store.one('SELECT intent_revision FROM room_work_intents WHERE goal_id=?',
                                      (work_id,)) if work_id else None
            evidence = order.status_evidence(source, work_id, revision['intent_revision'] if revision else None, now)
            self.store.execute('''INSERT INTO weixin_deliveries
                (id,binding_id,generation,message_id,message_version,message_digest,source_message_id,peer_id,
                 state,created,updated,ordering_key,kind,status_code,projection_message_id,ordering_evidence,
                 ordering_schema_version,ordering_checked_version)
                VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?,'status',?,?,?,?,?)''',
                (identifier, binding['id'], binding['generation'], 'status:' + identifier, source['version'],
                 source_digest, source['id'], binding['peer_id'], now, now, 'status:' + source['id'],
                 code, projection_message_id or source['id'], evidence, order.ORDERING_VERSION, order.ORDERING_VERSION))
            self.add_part(identifier, 0, 'text', {'text': notice})
            self.c.rooms.change(source['conversation_id'], 'message', projection_message_id or source['id'])
            return self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))

    def add_part(self, delivery_id, ordinal, kind, body, *, identifier=None):
        identifier = identifier or digest(['weixin-part-v1', delivery_id, ordinal])[:32]
        now = self.c.clock()
        self.store.execute('''INSERT INTO weixin_parts
            (id,delivery_id,ordinal,client_id,kind,body,body_digest,state,created,updated)
            VALUES (?,?,?,?,?,?,?,'queued',?,?)''',
            (identifier, delivery_id, ordinal, 'cpw-' + identifier, kind, self.store.encrypt(canonical(body)), digest(body), now, now))
        return identifier

    def add_attachment_parts(self, delivery, attachment_ids):
        if delivery['kind'] != 'reply':
            raise DevError('ATTACHMENT_REPLY_REQUIRED', '系统状态通知不能包含附件', 403)
        value = self.store.one('SELECT COALESCE(MAX(ordinal),-1) AS last FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))['last']
        for index, identifier in enumerate(attachment_ids, value + 1):
            part_id = self.add_part(delivery['id'], index, 'attachment', {'attachment_id': identifier})
            self.store.execute("""INSERT OR IGNORE INTO channel_attachment_transfers(attachment_id,channel,part_id,binding_version)
                SELECT ?,'weixin',?,version FROM weixin_bindings WHERE id=?""",
                (identifier, part_id, delivery['binding_id']))
        self.refresh(delivery['id'])

    def validate(self, delivery):
        if delivery['kind'] == 'status':
            source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (delivery['source_message_id'],))
            if not source:
                raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED', '原微信任务通知的输入已不可用', 403)
            binding, account, owner, source_digest = self.status_source(source)
            if (delivery['status_code'] not in STATUS_TEXT or binding['id'] != delivery['binding_id']
                    or binding['generation'] != delivery['generation'] or binding['peer_id'] != delivery['peer_id']
                    or source['version'] != delivery['message_version'] or source_digest != delivery['message_digest']):
                raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED', '原微信任务通知的来源已变化', 403)
            return binding, account, owner, source

        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (delivery['binding_id'],))
        if binding and binding.get('native_member_id'):
            valid = self.c.room_channels.native.delivery_validate(
                delivery, require_task_authority=False, allow_disconnected=True)
            if valid[1]['state'] != 'active':
                raise DevError('WEIXIN_WAITING_RECONNECT', '原微信账号恢复连接后继续投递', 409)
            message = valid[-1]
            source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (delivery['source_message_id'],))
            self.task_author(message, source, json.loads(message['body']))
            return valid
        binding, account, owner, room, dot = self.w.valid(delivery['binding_id'])
        if (not binding['allow_replies'] or binding['generation'] != delivery['generation']
                or binding['peer_id'] != delivery['peer_id']):
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED', '原收件人或回复批准已变化，不跨账号重发', 403)
        message = self.c.object('collaboration_messages', room, delivery['message_id'])
        self.c.chatroom.require_message_visible(owner, message)
        body = json.loads(message['body'])
        if (message['conversation_id'] != binding['conversation_id'] or message['version'] != delivery['message_version']
                or digest(body) != delivery['message_digest'] or body.get('sender_dot_id') != dot['id']):
            raise DevError('WEIXIN_DELIVERY_CONTENT_CHANGED', '原消息内容或版本已变化，不覆盖已排定的微信分项', 409)
        source = self.c.object('collaboration_messages', room, delivery['source_message_id'])
        self.c.channel_owner.require_reply_source(source)
        if source['origin'] != 'external_channel' or source['conversation_id'] != binding['conversation_id']:
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED', '原外部消息来源已变化', 403)
        return binding, account, owner, message

    def refresh(self, delivery_id):
        self.store.execute("""UPDATE weixin_parts SET state='rejected',reason_code='reply_part_failed',updated=?
            WHERE delivery_id=? AND state IN ('queued','waiting_context','waiting_media','attention')
            AND (attempts=0 OR reason_code='WEIXIN_RATE_LIMITED')
            AND ordinal>(SELECT MIN(ordinal) FROM weixin_parts
                WHERE delivery_id=? AND state IN ('rejected','cancelled'))""",
            (self.c.clock(), delivery_id, delivery_id))
        rows = self.store.all('SELECT state FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (delivery_id,))
        states = {r['state'] for r in rows}
        state = ('needs_attention' if states & {'unknown', 'attention', 'rejected'} else 'accepted' if states and states <= {'accepted', 'owner_confirmed'}
                 else 'cancelled' if states and states <= DONE else 'sending' if 'sending' in states
                 else 'waiting_context' if 'waiting_context' in states else 'queued' if states else 'waiting_media')
        self.store.execute('UPDATE weixin_deliveries SET state=?,version=version+1,updated=? WHERE id=?', (state, self.c.clock(), delivery_id))
        native = self.store.one('''SELECT b.conversation_id,COALESCE(d.projection_message_id,d.message_id) AS message_id,
                m.version AS message_version
            FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
            JOIN collaboration_messages m ON m.id=COALESCE(d.projection_message_id,d.message_id)
            WHERE d.id=? AND b.native_member_id!='' ''', (delivery_id,))
        if native:
            self.c.rooms.change(native['conversation_id'], 'message', native['message_id'], native['message_version'])

    def candidates(self, now, *, binding_id=None, peer_id=None):
        # Unknown external results block only established reply dependencies.
        # The separate in-flight check fences every request to the same peer.
        return self.store.all(f'''WITH sendable AS (
            SELECT p.id,d.created,d.rowid AS delivery_order,p.ordinal,
                ROW_NUMBER() OVER (PARTITION BY b.account_id,d.peer_id
                    ORDER BY d.created,d.rowid,p.ordinal) AS peer_rank
            FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id
            JOIN weixin_bindings b ON b.id=d.binding_id
            WHERE p.state IN ('queued','waiting_context','waiting_media') AND p.next_attempt<=?
                AND (? IS NULL OR (d.binding_id=? AND d.peer_id=?))
                AND NOT EXISTS (
                    SELECT 1 FROM weixin_parts first WHERE first.delivery_id=d.id AND first.ordinal<p.ordinal
                        AND first.state NOT IN ('accepted','owner_confirmed','cancelled','rejected'))
                AND (NOT ({order.WAITING_PREDECESSOR}) OR ({order.FAILED_PREDECESSOR}))
                AND NOT EXISTS (
                    SELECT 1 FROM weixin_deliveries held JOIN weixin_bindings hb ON hb.id=held.binding_id
                    JOIN weixin_parts sender ON sender.delivery_id=held.id
                    WHERE hb.account_id=b.account_id AND held.peer_id=d.peer_id
                        AND sender.state IN ('preparing','sending'))
            ) SELECT id FROM sendable WHERE peer_rank=1
                ORDER BY created,delivery_order,ordinal LIMIT 16''', (now, binding_id, binding_id, peer_id))

    def claim(self, identifier):
        with self.store.transaction():
            part = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (identifier,))
            now = self.c.clock()
            if not part or part['state'] not in READY or part['next_attempt'] > now:
                return None
            delivery = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (part['delivery_id'],))
            candidate = self.candidates(now, binding_id=delivery['binding_id'], peer_id=delivery['peer_id'])
            if not candidate or candidate[0]['id'] != identifier:
                return None
            failed = self.store.one("""SELECT 1 FROM weixin_parts
                WHERE delivery_id=? AND ordinal<? AND state IN ('rejected','cancelled') LIMIT 1""",
                (delivery['id'], part['ordinal']))
            if failed:
                self.refresh(delivery['id'])
                return None
            dependency = order.dependency_state(self.store, delivery['id'])
            if dependency == 'failed':
                self.reject_failed_dependency(delivery['id'], now)
                return None
            if dependency == 'waiting':
                return None
            binding, account, owner, message = self.validate(delivery)
            context = self.store.one('SELECT * FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?',
                                     (account['id'], binding['generation'], delivery['peer_id']))
            if not context:
                self.store.execute("UPDATE weixin_parts SET state='waiting_context',next_attempt=?,reason_code='context_missing',updated=? WHERE id=?", (now + 5, now, identifier))
                self.refresh(delivery['id'])
                return None
            if part['kind'] == 'attachment':
                source = json.loads(self.store.decrypt(part['body']))
                try:
                    attachment = self.w.media.row(source['attachment_id'], owner)
                except DevError as exc:
                    if exc.code not in {'ATTACHMENT_EXPIRED', 'ATTACHMENT_NOT_FOUND'}:
                        raise
                    self.store.execute("UPDATE weixin_parts SET state='rejected',reason_code=?,updated=? WHERE id=?", (exc.code, now, identifier))
                    self.refresh(delivery['id'])
                    return None
                if attachment['state'] == 'unavailable':
                    self.store.execute("UPDATE weixin_parts SET state='rejected',reason_code='attachment_unavailable',updated=? WHERE id=?", (now, identifier))
                    self.refresh(delivery['id'])
                    return None
                if attachment['state'] != 'ready':
                    self.store.execute("UPDATE weixin_parts SET state='waiting_media',next_attempt=?,updated=? WHERE id=?", (now + 5, now, identifier))
                    self.refresh(delivery['id'])
                    return None
            claim_owner, fence = uuid.uuid4().hex, part['lease_fence'] + 1
            self.store.execute("UPDATE weixin_parts SET state='preparing',lease_owner=?,lease_fence=?,lease_until=?,updated=? WHERE id=?",
                               (claim_owner, fence, now + (960 if part['kind'] == 'attachment' else 180), now, identifier))
            return {**part, 'lease_owner': claim_owner, 'lease_fence': fence, 'delivery': delivery,
                    'binding': binding, 'account': account, 'context': context}

    def reject_failed_dependency(self, delivery_id, now):
        self.store.execute("""UPDATE weixin_parts SET state='rejected',reason_code='dependency_failed',
            lease_fence=lease_fence+1,lease_until=0,updated=? WHERE delivery_id=?
            AND state IN ('queued','waiting_context','waiting_media','attention','preparing')
            AND (attempts=0 OR reason_code='WEIXIN_RATE_LIMITED')""", (now, delivery_id))
        self.refresh(delivery_id)

    def current(self, claim):
        part = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (claim['id'],))
        if (not part or part['lease_owner'] != claim['lease_owner'] or part['lease_fence'] != claim['lease_fence']
                or (part['lease_until'] or 0) <= self.c.clock() or part['state'] not in {'preparing', 'sending'}):
            raise DevError('WEIXIN_SEND_FENCE_LOST', '原微信投递世代已失效，不重发同一分项', 409)
        binding, *_ = self.validate(claim['delivery'])
        if binding['version'] != claim['binding']['version']:
            raise DevError('WEIXIN_BINDING_CHANGED', '发送准备期间原绑定版本已变化，旧工作者不能继续发送', 409)
        return part

    def upload_identity(self, claim):
        with self.store.transaction():
            part = self.current(claim)
            saved = json.loads(self.store.decrypt(part['wire_body'])) if part['wire_body'] else {}
            if not saved:
                saved = {'upload_key': secrets.token_hex(16), 'filekey': 'cpw-' + part['id']}
                self.store.execute('UPDATE weixin_parts SET wire_body=? WHERE id=?', (self.store.encrypt(canonical(saved)), part['id']))
            return saved

    def check_upload(self, claim):
        with self.store.transaction():
            self.current(claim)

    def record_upload(self, claim):
        # Upload success is an observation, not send authority. A revoked
        # binding can still have a successful upload followed by a blocked send.
        with self.store.transaction():
            part = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (claim['id'],))
            if not part or part['lease_owner'] != claim['lease_owner'] or part['lease_fence'] != claim['lease_fence']:
                return
            self.store.execute("""UPDATE channel_attachment_transfers
                SET upload_state='accepted',upload_observed_at=? WHERE channel='weixin' AND part_id=?""",
                (self.c.clock(), claim['id']))

    def split_unsent_text(self, part, wire):
        value = wire['items'][0]['text_item']['text']
        later_attempted = self.store.one('''SELECT 1 FROM weixin_parts
            WHERE delivery_id=? AND ordinal>? AND attempts>0 LIMIT 1''', (part['delivery_id'], part['ordinal']))
        if len(value) < 2 or later_attempted:
            return False
        # Request overhead is stable for these text parts. Find the largest
        # Unicode prefix that fits the exact encoded provider request.
        low, high = 0, len(value)
        while high - low > 1:
            prefix = (low + high) // 2
            try:
                encode_request(send_payload(wire['peer'], wire['context'], wire['client_id'], text_items(value[:prefix])))
            except DevError as exc:
                if exc.code != REQUEST_LIMIT:
                    raise
                high = prefix
            else:
                low = prefix
        if not low:
            return False
        # A multibyte or JSON-escaped character can make later pieces larger
        # than the prefix. Each piece is checked again before its own send.
        body = {'text': value[:low]}
        self.store.execute("""UPDATE weixin_parts SET state='queued',body=?,body_digest=?,
            lease_until=0,wire_body=NULL,reason_code='',updated=? WHERE id=?""",
            (self.store.encrypt(canonical(body)), digest(body), self.c.clock(), part['id']))
        offset = self.store.one('SELECT MAX(ordinal)+1 AS n FROM weixin_parts WHERE delivery_id=?',
                                (part['delivery_id'],))['n']
        self.store.execute('UPDATE weixin_parts SET ordinal=ordinal+? WHERE delivery_id=? AND ordinal>?',
                           (offset, part['delivery_id'], part['ordinal']))
        self.store.execute('UPDATE weixin_parts SET ordinal=ordinal-?+1 WHERE delivery_id=? AND ordinal>?',
                           (offset, part['delivery_id'], offset + part['ordinal']))
        identifier = digest(['weixin-split-part-v1', part['id'], part['body_digest']])[:32]
        self.add_part(part['delivery_id'], part['ordinal'] + 1, 'text', {'text': value[low:]}, identifier=identifier)
        self.refresh(part['delivery_id'])
        return True

    def mark_sending(self, claim, items):
        with self.store.transaction():
            part = self.current(claim)
            delivery = claim['delivery']
            dependency = order.dependency_state(self.store, delivery['id'])
            version = self.store.one('SELECT ordering_revision FROM weixin_deliveries WHERE id=?', (delivery['id'],))
            if dependency == 'failed':
                self.reject_failed_dependency(delivery['id'], self.c.clock())
                return None
            if dependency == 'waiting' or version['ordering_revision'] != delivery['ordering_revision']:
                self.store.execute("""UPDATE weixin_parts SET state='queued',lease_fence=lease_fence+1,
                    lease_until=0,updated=? WHERE id=?""", (self.c.clock(), part['id']))
                self.refresh(delivery['id'])
                return None
            if part['attempts'] and part['reason_code'] == RATE_LIMITED:
                wire = json.loads(self.store.decrypt(part['wire_body']))
                context_version = part['context_version']
            else:
                context = self.store.one('SELECT * FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?',
                    (claim['account']['id'], claim['binding']['generation'], claim['delivery']['peer_id']))
                if not context:
                    raise DevError('WEIXIN_CONTEXT_CHANGED', '原收件上下文已不可用，没有发送微信消息', 409)
                wire = {'peer': claim['delivery']['peer_id'], 'context': self.store.decrypt(context['token']),
                        'client_id': part['client_id'], 'items': items}
                context_version = context['version']
            if wire['peer'] != claim['delivery']['peer_id'] or wire['client_id'] != part['client_id']:
                raise DevError('WEIXIN_PART_CORRUPT', '原微信发送内容与收件记录不匹配', 409)
            status = claim['delivery']['kind'] == 'status'
            if status and wire['items'] != text_items(STATUS_TEXT[claim['delivery']['status_code']]):
                raise DevError('WEIXIN_PART_CORRUPT', '微信状态通知正文与系统原因不匹配', 409)
            try:
                encode_request(send_payload(wire['peer'], wire['context'], wire['client_id'], wire['items']))
            except DevError as exc:
                if (exc.code == REQUEST_LIMIT and not status and part['kind'] == 'text' and not part['attempts']
                        and self.split_unsent_text(part, wire)):
                    return None
                raise
            self.store.execute("UPDATE weixin_parts SET state='sending',wire_body=?,context_version=?,attempts=attempts+1,updated=? WHERE id=?",
                (self.store.encrypt(canonical(wire)), context_version, self.c.clock(), part['id']))
            self.refresh(part['delivery_id'])
            return wire

    def finish(self, claim, result=None, error=None):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (claim['id'],))
            if (not row or row['lease_owner'] != claim['lease_owner'] or row['lease_fence'] != claim['lease_fence']
                    or row['state'] not in {'preparing','sending'}):
                return
            now = self.c.clock()
            code = error.code if isinstance(error, DevError) else type(error).__name__ if error else ''
            next_attempt = 0
            if result is not None and row['state'] == 'sending':
                state = 'accepted'
            elif code == REQUEST_LIMIT:
                state = 'rejected'
            elif row['state'] == 'sending' and code == RATE_LIMITED and error.status == 429:
                state = 'rejected'
                if row['attempts'] < MAX_SEND_ATTEMPTS:
                    delay = error.details.get('retry_after_seconds')
                    if type(delay) is not int or not 0 <= delay <= 999999999:
                        delay = 60
                    next_attempt = now + max(min(900, 15 * 2 ** row['attempts']), delay)
                    state = 'queued'
            elif row['state'] == 'sending':
                # A send may have committed despite a lost, malformed or late
                # response. Preserve its identity for owner review.
                state = 'unknown'
            elif code in TERMINAL_SOURCE or (row['kind'] == 'attachment' and code in {
                    'WEIXIN_MEDIA_LIMIT', 'WEIXIN_VOICE_SEND_UNVERIFIED', 'WEIXIN_UPLOAD_REJECTED',
                    'ATTACHMENT_EXPIRED', 'ATTACHMENT_HASH_MISMATCH', 'WEIXIN_MEDIA_INVALID'}):
                state = 'rejected'
            else:
                state = 'attention'
            self.store.execute('''UPDATE weixin_parts SET state=?,reason_code=?,provider_receipt=?,
                next_attempt=?,lease_until=0,updated=? WHERE id=?''',
                (state, code, canonical(result) if state == 'accepted' else None, next_attempt, now, row['id']))
            self.refresh(row['delivery_id'])
            if code in TASK_STATUSES and claim['delivery']['kind'] == 'reply':
                self.blocked_status(claim['delivery'], code)
            if code in {'WEIXIN_AUTH_REJECTED', 'WEIXIN_SESSION_EXPIRED'}:
                self.store.execute("UPDATE weixin_accounts SET state='attention',reason_code=?,poll_fence=poll_fence+1,poll_until=0 WHERE id=?",
                                   (code, claim['account']['id']))

    def blocked_status(self, delivery, code):
        source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (delivery['source_message_id'],))
        self.queue_task_status(source, code, projection_message_id=delivery['message_id'])

    async def send_once(self, identifier):
        try:
            claim = await self.store.run(self.claim, identifier)
        except DevError as exc:
            # Preserve a visible reason instead of leaving an unreachable queue
            # item indefinitely looking healthy. No failed scope check sends data.
            def blocked(code=exc.code):
                with self.store.transaction():
                    row = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (identifier,))
                    if row and row['state'] in READY:
                        terminal_source = code in TERMINAL_SOURCE
                        self.store.execute("UPDATE weixin_parts SET state=?,reason_code=?,next_attempt=?,updated=? WHERE id=?",
                                           ('rejected' if terminal_source else row['state'], code, self.c.clock() + 30, self.c.clock(), identifier))
                        self.refresh(row['delivery_id'])
                        if code in TASK_STATUSES:
                            delivery = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (row['delivery_id'],))
                            if delivery['kind'] == 'reply':
                                self.blocked_status(delivery, code)
            await self.store.run(blocked)
            return
        if not claim:
            return
        try:
            body = json.loads(self.store.decrypt(claim['body']))
            if digest(body) != claim['body_digest']:
                raise DevError('WEIXIN_PART_CORRUPT', '微信分项正文摘要不匹配', 409)
            client = self.w.client_factory(claim['account']['api_base'], self.store.decrypt(claim['account']['secret']))
            if claim['attempts'] and claim['reason_code'] == RATE_LIMITED:
                items = None  # Retry the frozen encoded message, including any uploaded item.
            elif claim['kind'] == 'text':
                items = text_items(body['text'])
            else:
                saved = await self.store.run(self.upload_identity, claim)
                content, attachment = await self.store.run(self.w.media.outbound_content, body['attachment_id'], claim['binding']['id'])
                await self.store.run(self.check_upload, claim)
                item = await client.upload_media(claim['delivery']['peer_id'], content, attachment['kind'], attachment['name'],
                                                 filekey=saved['filekey'], key=bytes.fromhex(saved['upload_key']))
                await self.store.run(self.record_upload, claim)
                items = [item]
            await self.store.run(self.w.require_enabled, claim['account'])
            wire = await self.store.run(self.mark_sending, claim, items)
            if wire is None:
                return
            result = await client.send(wire['peer'], wire['context'], wire['client_id'], wire['items'])
            await self.store.run(self.finish, claim, result)
        except asyncio.CancelledError:
            await self.store.run(self.finish, claim, None, DevError('WEIXIN_SEND_INTERRUPTED', '发送期间宿主退出，结果需核验', 503))
            raise
        except Exception as exc:
            await self.store.run(self.finish, claim, None, exc)

    def reconcile(self):
        with self.store.transaction():
            rows = self.store.all("SELECT * FROM weixin_parts WHERE state IN ('preparing','sending') AND lease_until<=? ORDER BY lease_until LIMIT 100", (self.c.clock(),))
            for row in rows:
                state = 'unknown' if row['state'] == 'sending' else 'queued'
                reason = RATE_LIMITED if state == 'queued' and row['reason_code'] == RATE_LIMITED else 'host_restarted_before_receipt'
                self.store.execute('UPDATE weixin_parts SET state=?,reason_code=?,lease_fence=lease_fence+1,lease_until=0,updated=? WHERE id=?',
                                   (state, reason, self.c.clock(), row['id']))
                self.refresh(row['delivery_id'])
            return [r['id'] for r in rows]

    async def tick(self):
        if not self.c.config.enabled:
            return
        await self.store.run(self.reconcile)
        await self.store.run(order.backfill, self.store, self.c.clock())
        rows = await self.store.run(self.candidates, self.c.clock())
        # At most four concurrent peers, with the same fenced check in claim.
        semaphore = asyncio.Semaphore(4)
        async def run(row):
            async with semaphore:
                await self.send_once(row['id'])
        await asyncio.gather(*(run(row) for row in rows))

    def quarantine_binding(self, identifier, reason):
        rows = self.store.all("SELECT p.* FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id WHERE d.binding_id=? AND p.state NOT IN ('accepted','owner_confirmed','cancelled','rejected')", (identifier,))
        for row in rows:
            # Fence stale workers and release only parts proven never sent.
            # Unknown sends keep their ordering barrier across re-login.
            never_sent = row['attempts'] == 0 or row['reason_code'] == RATE_LIMITED
            state = ('unknown' if row['state'] in {'sending', 'unknown'} else 'cancelled'
                     if row['state'] in READY | {'preparing', 'attention'} and never_sent else 'attention')
            self.store.execute("UPDATE weixin_parts SET state=?,reason_code=?,lease_fence=lease_fence+1,lease_until=0,updated=? WHERE id=?",
                               (state, reason, self.c.clock(), row['id']))
            self.refresh(row['delivery_id'])

    def quarantine_account(self, identifier, reason):
        for row in self.store.all('SELECT id FROM weixin_bindings WHERE account_id=?', (identifier,)):
            self.quarantine_binding(row['id'], reason)

    def control(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.DeliveryControl, enabled=False)
            row = self.store.one('SELECT d.* FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id WHERE d.id=? AND b.room_id=? AND b.conversation_id=?',
                                (args['delivery_id'], room['id'], args['conversation_id']))
            if not row:
                raise DevError('WEIXIN_DELIVERY_NOT_FOUND', '当前话题没有这条投递', 404)
            def save():
                current = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (row['id'],))
                if current['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '原投递分项状态已变化', 409)
                if args['action'] == 'confirm_received':
                    if not args['confirmation_note'].strip():
                        raise DevError('WEIXIN_OWNER_CONFIRMATION_REQUIRED', '请明确记录人工核对依据；这不等同平台送达回执', 422)
                    self.store.execute("UPDATE weixin_parts SET state='owner_confirmed',reason_code='owner_attested_not_provider_verified',updated=? WHERE delivery_id=? AND state='unknown'",
                                       (self.c.clock(), row['id']))
                else:
                    self.store.execute("UPDATE weixin_parts SET state='cancelled',lease_fence=lease_fence+1,lease_until=0,updated=? WHERE delivery_id=? AND state IN ('queued','preparing','waiting_context','waiting_media','attention')",
                                       (self.c.clock(), row['id']))
                self.refresh(row['id'])
                self.c.audit(room, principal.actor, 'weixin.delivery_' + args['action'], row['id'],
                             {'note': args['confirmation_note'], 'provider_delivery_verified': False})
                return {'delivery_id': row['id'], 'provider_delivery_verified': False, 'automatic_resend': False}
            return self.c.mutation(principal, room, 'weixin_delivery_control', args, save)

    def retry_room(self, conversation_id, delivery_id, args, principal):
        """Requeue only parts whose send was never attempted."""
        with self.store.transaction():
            principal, room = self.c.rooms.owner(principal, conversation_id)
            self.c.rooms.require_native(room)
            if room['state'] != 'active':
                raise DevError('ROOM_PAUSED', '恢复房间后可以重试。', 409)

            def retry():
                delivery = self.store.one("""SELECT d.* FROM weixin_deliveries d
                    JOIN weixin_bindings b ON b.id=d.binding_id
                    WHERE d.id=? AND b.conversation_id=? AND b.native_member_id!=''""",
                    (delivery_id, conversation_id))
                if not delivery:
                    raise DevError('WEIXIN_DELIVERY_NOT_FOUND', '此房间没有这条消息的发送记录。', 404)
                if delivery['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '发送状态已变化，请刷新。', 409)
                self.validate(delivery)
                parts = self.store.all("""SELECT id FROM weixin_parts
                    WHERE delivery_id=? AND state='attention' AND attempts=0""", (delivery_id,))
                if not parts:
                    raise DevError('WEIXIN_RETRY_UNAVAILABLE', '没有可以安全重试的分项。', 409)
                now = self.c.clock()
                self.store.execute("""UPDATE weixin_parts SET state='queued',next_attempt=?,reason_code='',
                    lease_fence=lease_fence+1,lease_until=0,updated=?
                    WHERE delivery_id=? AND state='attention' AND attempts=0""", (now, now, delivery_id))
                self.refresh(delivery_id)
                self.c.rooms.change(conversation_id, 'message', delivery['message_id'])
                return {'delivery_id': delivery_id, 'queued_parts': len(parts)}

            return self.c.room_channels.receipt(principal, room, 'weixin_delivery_retry',
                {**args, 'delivery_id': delivery_id}, retry)

    def for_messages(self, identifiers):
        identifiers = list(dict.fromkeys(identifiers))[:124]
        if not identifiers:
            return {}
        keys = ','.join('?' for _ in identifiers)
        rows = self.store.all(f'''SELECT id,COALESCE(projection_message_id,message_id) AS message_id,state,version,
            message_version,updated,kind,status_code,ordering_key,ordering_revision,ordering_evidence FROM weixin_deliveries
            WHERE message_id IN ({keys}) OR projection_message_id IN ({keys})
            ORDER BY message_id,created,id''', (*identifiers, *identifiers))
        grouped = {}
        if not rows:
            return grouped
        parts = self.store.all('SELECT id,delivery_id,ordinal,kind,state,reason_code,attempts FROM weixin_parts WHERE delivery_id IN (' + ','.join('?' for _ in rows) + ') ORDER BY delivery_id,ordinal', tuple(r['id'] for r in rows))
        by_delivery = {}
        for part in parts:
            by_delivery.setdefault(part['delivery_id'], []).append(part)
        for row in rows:
            ordering_reason = order.ordering_reason(row.pop('ordering_key'), row.pop('ordering_evidence'))
            grouped.setdefault(row['message_id'], []).append({**row, 'parts': by_delivery.get(row['id'], []),
                'ordering_reason': ordering_reason, 'provider_delivery_verified': False, 'read_verified': False})
        return grouped

    def listing(self, room_id, conversation_id):
        rows = self.store.all('''SELECT d.* FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
            WHERE b.room_id=? AND b.conversation_id=? ORDER BY d.created DESC,d.id DESC LIMIT 50''', (room_id, conversation_id))
        return [{k: row[k] for k in ('id', 'binding_id', 'message_version', 'state', 'version',
                                      'created', 'updated', 'kind', 'status_code', 'ordering_revision')}
                | {'message_id': row['projection_message_id'] or row['message_id'],
                   'ordering_reason': order.ordering_reason(row['ordering_key'], row['ordering_evidence']),
                   'parts': self.store.all('''SELECT id,ordinal,client_id,kind,state,attempts,reason_code,updated
                    FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal''', (row['id'],)),
                   'provider_delivery_verified': False, 'read_verified': False} for row in rows]
