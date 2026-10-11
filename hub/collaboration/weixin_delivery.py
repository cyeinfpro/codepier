"""Ordered immutable Weixin parts. Unknown send outcomes are NEVER retried."""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid

from hub.collaboration.common import canonical, digest
from hub.collaboration import weixin_schema as models
from shared.util import DevError

DONE = frozenset({'accepted', 'owner_confirmed', 'cancelled', 'rejected'})
READY = frozenset({'queued', 'waiting_context', 'waiting_media'})


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
                return None
            current = self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND room_id=? AND conversation_id=?',
                                    (current['reply_to_id'], message['room_id'], message['conversation_id']))
            if not current:
                return None
        return None

    def queue_reply(self, message):
        if not self.c.config.enabled:
            return None
        body = json.loads(message['body'])
        if message['origin'] != 'bound_connector' or not body.get('sender_dot_id') or body.get('dot_reply_complete') is False:
            return None
        source = self.root_source(message)
        if not source:
            return None
        origin = json.loads(source['body']).get('external_origin', {})
        try:
            binding, account, owner, room, dot = self.w.valid(origin.get('binding_id', ''))
        except DevError:
            return None  # Chat persistence is independent of channel availability.
        if (not binding['allow_replies'] or origin.get('generation') != binding['generation']
                or origin.get('peer_id') != binding['peer_id'] or body['sender_dot_id'] != dot['id']):
            return None
        old = self.store.one('SELECT * FROM weixin_deliveries WHERE binding_id=? AND generation=? AND message_id=? AND message_version=?',
                             (binding['id'], binding['generation'], message['id'], message['version']))
        if old:
            return old
        pending = self.store.one('''SELECT COUNT(*) AS n FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
            WHERE b.account_id=? AND d.created>?''', (account['id'], self.c.clock() - 86400))['n']
        if pending >= 1000:
            # No network work and no claim of successful relay. The owner can
            # see the canonical reply; a later explicit reconciliation can queue it.
            return None
        now, identifier = self.c.clock(), uuid.uuid4().hex
        self.store.execute('''INSERT INTO weixin_deliveries
            (id,binding_id,generation,message_id,message_version,message_digest,source_message_id,peer_id,state,created,updated)
            VALUES (?,?,?,?,?,?,?,?, 'queued',?,?)''',
            (identifier, binding['id'], binding['generation'], message['id'], message['version'], digest(body),
             source['id'], binding['peer_id'], now, now))
        value = body.get('body_text', '')
        # Stable Unicode boundaries; byte-bounded chunks preserve all prose.
        pieces, chunk, count = [], [], 0
        for char in value:
            size = len(char.encode())
            if count + size > 3000:
                pieces.append(''.join(chunk)); chunk, count = [], 0
            chunk.append(char); count += size
        if chunk:
            pieces.append(''.join(chunk))
        for ordinal, piece in enumerate(pieces):
            self.add_part(identifier, ordinal, 'text', {'text': piece})
        if not pieces:
            self.store.execute("UPDATE weixin_deliveries SET state='waiting_media' WHERE id=?", (identifier,))
        return self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))

    def add_part(self, delivery_id, ordinal, kind, body):
        identifier = digest(['weixin-part-v1', delivery_id, ordinal])[:32]
        now = self.c.clock()
        self.store.execute('''INSERT INTO weixin_parts
            (id,delivery_id,ordinal,client_id,kind,body,body_digest,state,created,updated)
            VALUES (?,?,?,?,?,?,?,'queued',?,?)''',
            (identifier, delivery_id, ordinal, 'cpw-' + identifier, kind, self.store.encrypt(canonical(body)), digest(body), now, now))
        return identifier

    def add_attachment_parts(self, delivery, attachment_ids):
        value = self.store.one('SELECT COALESCE(MAX(ordinal),-1) AS last FROM weixin_parts WHERE delivery_id=?', (delivery['id'],))['last']
        for index, identifier in enumerate(attachment_ids, value + 1):
            self.add_part(delivery['id'], index, 'attachment', {'attachment_id': identifier})
        self.refresh(delivery['id'])

    def validate(self, delivery):
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
        if source['origin'] != 'external_channel' or source['conversation_id'] != binding['conversation_id']:
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED', '原外部消息来源已变化', 403)
        return binding, account, owner, message

    def refresh(self, delivery_id):
        rows = self.store.all('SELECT state FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (delivery_id,))
        states = {r['state'] for r in rows}
        state = ('needs_attention' if states & {'unknown', 'attention', 'rejected'} else 'accepted' if states and states <= {'accepted', 'owner_confirmed'}
                 else 'cancelled' if states and states <= DONE else 'sending' if 'sending' in states
                 else 'waiting_context' if 'waiting_context' in states else 'queued' if states else 'waiting_media')
        self.store.execute('UPDATE weixin_deliveries SET state=?,version=version+1,updated=? WHERE id=?', (state, self.c.clock(), delivery_id))

    def claim(self, identifier):
        with self.store.transaction():
            part = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (identifier,))
            now = self.c.clock()
            if not part or part['state'] not in READY or part['next_attempt'] > now:
                return None
            delivery = self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (part['delivery_id'],))
            binding, account, owner, message = self.validate(delivery)
            earlier = self.store.one("SELECT 1 FROM weixin_parts WHERE delivery_id=? AND ordinal<? AND state NOT IN ('accepted','owner_confirmed','cancelled','rejected') LIMIT 1", (delivery['id'], part['ordinal']))
            occupied = self.store.one('''SELECT 1 FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id
                WHERE d.binding_id=? AND d.peer_id=? AND p.state IN ('preparing','sending') AND p.lease_until>? LIMIT 1''',
                (binding['id'], delivery['peer_id'], now))
            if earlier or occupied:
                return None
            # One sender per peer across all messages; order by original commit.
            first = self.store.one('''SELECT p.id FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id
                WHERE d.binding_id=? AND d.peer_id=? AND p.state NOT IN ('accepted','owner_confirmed','cancelled','rejected')
                ORDER BY d.created,d.id,p.ordinal LIMIT 1''', (binding['id'], delivery['peer_id']))
            if first and first['id'] != identifier:
                return None
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
                               (claim_owner, fence, now + 180, now, identifier))
            return {**part, 'lease_owner': claim_owner, 'lease_fence': fence, 'delivery': delivery,
                    'binding': binding, 'account': account, 'context': context}

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

    def mark_sending(self, claim, items):
        with self.store.transaction():
            part = self.current(claim)
            context = self.store.one('SELECT * FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?',
                (claim['account']['id'], claim['binding']['generation'], claim['delivery']['peer_id']))
            if not context:
                raise DevError('WEIXIN_CONTEXT_CHANGED', '原收件上下文已不可用，没有发送微信消息', 409)
            wire = {'peer': claim['delivery']['peer_id'], 'context': self.store.decrypt(context['token']),
                    'client_id': part['client_id'], 'items': items}
            self.store.execute("UPDATE weixin_parts SET state='sending',wire_body=?,context_version=?,attempts=attempts+1,updated=? WHERE id=?",
                (self.store.encrypt(canonical(wire)), context['version'], self.c.clock(), part['id']))
            self.refresh(part['delivery_id'])
            return wire

    def finish(self, claim, result=None, error=None):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM weixin_parts WHERE id=?', (claim['id'],))
            if not row or row['lease_owner'] != claim['lease_owner'] or row['lease_fence'] != claim['lease_fence']:
                return
            code = error.code if isinstance(error, DevError) else type(error).__name__ if error else ''
            if result is not None:
                try:
                    self.current(claim)
                except DevError as exc:
                    error, result, code = exc, None, exc.code
            # No success/receipt guarantee exists after an uncertain send. The
            # immutable body remains available to owner review, never auto-retry.
            definite_media_failure = (row['state'] == 'preparing' and row['kind'] == 'attachment'
                and isinstance(error, DevError) and error.code in {
                    'WEIXIN_MEDIA_LIMIT', 'WEIXIN_VOICE_SEND_UNVERIFIED', 'WEIXIN_UPLOAD_REJECTED',
                    'ATTACHMENT_EXPIRED', 'ATTACHMENT_HASH_MISMATCH', 'WEIXIN_MEDIA_INVALID'})
            state = ('accepted' if result is not None else 'unknown' if row['state'] == 'sending'
                     else 'rejected' if definite_media_failure else 'attention')
            self.store.execute('''UPDATE weixin_parts SET state=?,reason_code=?,provider_receipt=?,lease_until=0,updated=? WHERE id=?''',
                (state, code, canonical(result) if result is not None else None, self.c.clock(), row['id']))
            self.refresh(row['delivery_id'])
            if code in {'WEIXIN_AUTH_REJECTED', 'WEIXIN_SESSION_EXPIRED'}:
                self.store.execute("UPDATE weixin_accounts SET state='attention',reason_code=?,poll_fence=poll_fence+1,poll_until=0 WHERE id=?",
                                   (code, claim['account']['id']))

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
                        self.store.execute("UPDATE weixin_parts SET reason_code=?,next_attempt=?,updated=? WHERE id=?", (code, self.c.clock() + 30, self.c.clock(), identifier))
            await self.store.run(blocked)
            return
        if not claim:
            return
        try:
            body = json.loads(self.store.decrypt(claim['body']))
            if digest(body) != claim['body_digest']:
                raise DevError('WEIXIN_PART_CORRUPT', '微信分项正文摘要不匹配', 409)
            client = self.w.client_factory(claim['account']['api_base'], self.store.decrypt(claim['account']['secret']))
            if claim['kind'] == 'text':
                items = [{'type': 1, 'text_item': {'text': body['text']}}]
            else:
                saved = await self.store.run(self.upload_identity, claim)
                content, attachment = await self.store.run(self.w.media.outbound_content, body['attachment_id'], claim['binding']['id'])
                item = await client.upload_media(claim['delivery']['peer_id'], content, attachment['kind'], attachment['name'],
                                                 filekey=saved['filekey'], key=bytes.fromhex(saved['upload_key']))
                items = [item]
            wire = await self.store.run(self.mark_sending, claim, items)
            await self.store.run(self.w.require_enabled, claim['account'])
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
                self.store.execute('UPDATE weixin_parts SET state=?,reason_code=?,lease_fence=lease_fence+1,lease_until=0,updated=? WHERE id=?',
                                   (state, 'host_restarted_before_receipt', self.c.clock(), row['id']))
                self.refresh(row['delivery_id'])
            return [r['id'] for r in rows]

    async def tick(self):
        if not self.c.config.enabled:
            return
        await self.store.run(self.reconcile)
        # Select only the first unfinished part of each lane. A blocked lane's
        # backlog must not occupy the global batch and starve unrelated peers.
        rows = await self.store.run(lambda: self.store.all('''SELECT p.id
            FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id
            WHERE p.state IN ('queued','waiting_context','waiting_media') AND p.next_attempt<=?
            AND NOT EXISTS (
                SELECT 1 FROM weixin_parts earlier JOIN weixin_deliveries prior ON prior.id=earlier.delivery_id
                WHERE prior.binding_id=d.binding_id AND prior.peer_id=d.peer_id
                AND earlier.state NOT IN ('accepted','owner_confirmed','cancelled','rejected')
                AND (prior.created,prior.id,earlier.ordinal)<(d.created,d.id,p.ordinal))
            ORDER BY d.created,d.id,p.ordinal LIMIT 16''', (self.c.clock(),)))
        # At most four concurrent peers; each claim additionally serializes its lane.
        semaphore = asyncio.Semaphore(4)
        async def run(row):
            async with semaphore:
                await self.send_once(row['id'])
        await asyncio.gather(*(run(row) for row in rows))

    def quarantine_binding(self, identifier, reason):
        rows = self.store.all("SELECT p.* FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id WHERE d.binding_id=? AND p.state NOT IN ('accepted','owner_confirmed','cancelled')", (identifier,))
        for row in rows:
            self.store.execute("UPDATE weixin_parts SET state=CASE WHEN state='sending' THEN 'unknown' ELSE 'attention' END,reason_code=?,lease_fence=lease_fence+1,lease_until=0,updated=? WHERE id=?", (reason, self.c.clock(), row['id']))
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

    def for_messages(self, identifiers):
        identifiers = list(dict.fromkeys(identifiers))[:124]
        if not identifiers:
            return {}
        rows = self.store.all('SELECT id,message_id,state,version,message_version,updated FROM weixin_deliveries WHERE message_id IN (' + ','.join('?' for _ in identifiers) + ') ORDER BY message_id,created,id', tuple(identifiers))
        grouped = {}
        if not rows:
            return grouped
        parts = self.store.all('SELECT id,delivery_id,ordinal,kind,state,reason_code,attempts FROM weixin_parts WHERE delivery_id IN (' + ','.join('?' for _ in rows) + ') ORDER BY delivery_id,ordinal', tuple(r['id'] for r in rows))
        by_delivery = {}
        for part in parts:
            by_delivery.setdefault(part['delivery_id'], []).append(part)
        for row in rows:
            grouped.setdefault(row['message_id'], []).append({**row, 'parts': by_delivery.get(row['id'], []),
                'provider_delivery_verified': False, 'read_verified': False})
        return grouped

    def listing(self, room_id, conversation_id):
        rows = self.store.all('''SELECT d.* FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
            WHERE b.room_id=? AND b.conversation_id=? ORDER BY d.created DESC,d.id DESC LIMIT 50''', (room_id, conversation_id))
        return [{k: row[k] for k in ('id', 'binding_id', 'message_id', 'message_version', 'state', 'version', 'created', 'updated')}
                | {'parts': self.store.all('''SELECT id,ordinal,client_id,kind,state,attempts,reason_code,updated
                    FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal''', (row['id'],)),
                   'provider_delivery_verified': False, 'read_verified': False} for row in rows]
