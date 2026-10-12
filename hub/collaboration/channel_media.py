"""Ordered scoped attachment manifests and encrypted, resumable Hub cache.

No public CDN bearer URL, AES key, arbitrary local path or generation command
enters a public manifest. Existing artifacts resume by immutable ID and hash.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import uuid
from urllib.parse import quote

from hub.artifacts import byte_range
from hub.collaboration.common import canonical, digest
from hub.collaboration import weixin_schema as models
from hub.collaboration.weixin_protocol import ITEM_NAMES, MAX_MEDIA, safe_filename
from hub.principal import refresh_principal
from shared.util import DevError

MAX_CACHE = 256 * 1024 * 1024
PUBLIC_FIELDS = ('id', 'schema_version', 'message_id', 'ordinal', 'direction', 'kind', 'name', 'mime',
                 'size', 'sha256', 'artifact_id', 'state', 'reason_code', 'expires_at', 'created', 'updated')


def migrate(db):
    from hub.collaboration.channel_shares import migrate as migrate_shares
    migrate_shares(db)
    db.execute('''CREATE TABLE IF NOT EXISTS channel_attachment_chunks(
        attachment_id TEXT NOT NULL,offset INTEGER NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,
        content TEXT NOT NULL,PRIMARY KEY(attachment_id,offset))''')


class ChannelMedia:
    def __init__(self, channel):
        self.w, self.c, self.store = channel, channel.c, channel.store
        from hub.collaboration.channel_shares import ChannelShares
        self.shares = ChannelShares(self)
        self.approve = self.shares.approve
        self.attach_approved = self.shares.attach

    def public(self, row):
        return {k: row[k] for k in PUBLIC_FIELDS} | {'authentication': 'current_room_and_project_authority',
            'download_path': '/api/collaboration/attachments/' + row['id'] + '/content?space_id=' + quote(row.get('space_id', 'legacy'), safe=''),
            'immutable': True, 'range_supported': True,
            'plaintext_integrity': 'sha256_recorded' if row['sha256'] else 'not_yet_downloaded'}

    def for_message(self, identifier):
        return self.for_messages([identifier]).get(identifier, [])

    def for_messages(self, identifiers):
        identifiers = list(dict.fromkeys(identifiers))[:124]
        if not identifiers:
            return {}
        # Status polling must never load encrypted file bytes or CDN secrets.
        rows = self.store.all('SELECT ' + ','.join((*PUBLIC_FIELDS, 'space_id')) +
            ' FROM channel_attachments WHERE message_id IN (' + ','.join('?' for _ in identifiers) +
            ') ORDER BY message_id,ordinal,id', tuple(identifiers))
        result = {}
        for row in rows:
            result.setdefault(row['message_id'], []).append(self.public(row))
        return result

    def row(self, identifier, principal):
        principal = refresh_principal(self.store, principal)
        row = self.store.one('SELECT * FROM channel_attachments WHERE id=? AND space_id=? AND owner_user_id=?',
                             (identifier, principal.space_id, principal.user_id))
        if not row:
            raise DevError('ATTACHMENT_NOT_FOUND', '当前身份不能访问这个附件', 404)
        project = self.c.runtime.project(row['project_id'], principal)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['room_id'],))
        from hub.room_scope import require_room
        require_room(self.store, principal, room, row['conversation_id'])
        self.c.conversations.object(principal, row['conversation_id'])
        message = self.c.object('collaboration_messages', room, row['message_id'])
        self.c.chatroom.require_message_visible(principal, message)
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (row['binding_id'],))
        from hub.room_scope import snapshot
        if (not binding or row['generation'] != binding['generation'] or binding['state'] != 'active'
                or snapshot(project) != json.loads(binding['project_snapshot'])):
            raise DevError('ATTACHMENT_SCOPE_CHANGED', '附件原绑定或项目映射已改变', 403)
        self.w.valid(binding['id'])
        if row['expires_at'] <= self.c.clock() or row['state'] == 'expired':
            raise DevError('ATTACHMENT_EXPIRED', '附件保留期限已到；不会重跑原生成任务', 410)
        return row

    def add_inbound(self, binding, message_id, ordinal, item):
        kind = ITEM_NAMES[item['type']]
        source = item.get(kind + '_item')
        if not isinstance(source, dict) or not isinstance(source.get('media'), dict):
            source = {'invalid_source': True}
        identifier, now = uuid.uuid4().hex, self.c.clock()
        name = safe_filename(source.get('file_name', kind + '-attachment'))
        media = dict(source.get('media', {}))
        # Tencent accepts both base64(raw16) and base64(hex32). Its image field
        # aeskey is a separate hexadecimal alternative; preserve it privately.
        if kind == 'image' and source.get('aeskey'):
            media['aes_key'] = base64.b64encode(str(source['aeskey']).encode()).decode()
        state = 'pending' if media and not source.get('invalid_source') else 'unavailable'
        reason = '' if state == 'pending' else 'media_reference_invalid'
        self.store.execute('''INSERT INTO channel_attachments
            (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,
             binding_id,generation,peer_id,kind,name,mime,state,private_source,reason_code,expires_at,created,updated)
            VALUES (?,?,?,?,?,?,?,?,'inbound',?,?,?,?,?,?,?, ?,?,?,?,?)''',
            (identifier, binding['space_id'], binding['owner_user_id'], binding['room_id'], binding['conversation_id'],
             binding['project_id'], message_id, ordinal, binding['id'], binding['generation'], binding['peer_id'],
             kind, name, 'application/octet-stream', state, self.store.encrypt(canonical({'media': media})), reason,
             now + 7 * 86400 if binding.get('lifetime_mode') == 'until_revoked' else min(binding['expires_at'], now + 7 * 86400), now, now))
        return identifier

    def add_artifacts(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.AttachmentAdd)
            if len(set(args['artifact_ids'])) != len(args['artifact_ids']):
                raise DevError('ATTACHMENT_DUPLICATE', '附件列表不能重复，顺序会原样保存', 422)
            message = self.c.object('collaboration_messages', room, args['message_id'])
            if message['version'] != args['expected_message_version'] or message['conversation_id'] != args['conversation_id']:
                raise DevError('STALE_VERSION', '附件所属的回复版本或话题已变化', 409)
            self.c.chatroom.require_message_visible(principal, message)
            source = self.w.delivery.root_source(message)
            origin = json.loads(source['body']).get('external_origin', {}) if source else {}
            binding, *_ = self.w.valid(origin.get('binding_id', ''))
            if not binding['allow_media_outbound'] or not binding['allow_replies']:
                raise DevError('ATTACHMENT_EGRESS_NOT_APPROVED', '此绑定未获文件外发权限', 403)
            artifacts = []
            for identifier in args['artifact_ids']:
                artifact, _ = self.c.runtime.artifacts.row(identifier, principal)
                if (artifact['project_id'] != binding['project_id'] or artifact['space_id'] != binding['space_id']
                        or artifact['bytes'] > MAX_MEDIA or artifact['expires'] <= self.c.clock()):
                    raise DevError('ATTACHMENT_ARTIFACT_SCOPE', '只能外发当前项目中未过期、预算内的已生成产物', 403)
                artifacts.append(artifact)

            def save():
                delivery = self.w.delivery.queue_reply(message)
                if not delivery:
                    raise DevError('ATTACHMENT_REPLY_REQUIRED', '需要当前绑定 dot 对原外部消息的最终回复，不能广播或转发任意文件', 403)
                now = self.c.clock()
                existing = self.store.one("SELECT COUNT(*) AS n FROM channel_attachments WHERE message_id=? AND direction='outbound'", (message['id'],))['n']
                if existing + len(artifacts) > 16:
                    raise DevError('ATTACHMENT_COUNT_LIMIT', '每条原回复最多16件附件，不能拆分调用绕过完整清单预算', 409)
                last = self.store.one("SELECT COALESCE(MAX(ordinal),-1) AS n FROM channel_attachments WHERE message_id=? AND direction='outbound'", (message['id'],))['n']
                identifiers = []
                for ordinal, artifact in enumerate(artifacts, last + 1):
                    identifier, name = uuid.uuid4().hex, safe_filename(artifact['name'])
                    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                    kind = 'image' if mime in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'} else 'video' if mime == 'video/mp4' else 'file'
                    self.store.execute('''INSERT INTO channel_attachments
                        (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,binding_id,
                         generation,peer_id,kind,name,mime,size,sha256,artifact_id,state,private_source,expires_at,created,updated)
                        VALUES (?,?,?,?,?,?,?,?,'outbound',?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)''',
                        (identifier, binding['space_id'], binding['owner_user_id'], room['id'], binding['conversation_id'],
                         binding['project_id'], message['id'], ordinal, binding['id'], binding['generation'], binding['peer_id'],
                         kind, name, mime, artifact['bytes'], artifact['sha256'], artifact['id'],
                         self.store.encrypt(canonical({'artifact_id': artifact['id']})),
                         artifact['expires'] if binding.get('lifetime_mode') == 'until_revoked' else min(artifact['expires'], binding['expires_at']), now, now))
                    identifiers.append(identifier)
                self.w.delivery.add_attachment_parts(delivery, identifiers)
                self.c.audit(room, principal.actor, 'weixin.files_egress_approved', message['id'],
                             {'attachment_ids': identifiers, 'generation': binding['generation']})
                return {'attachment_ids': identifiers, 'delivery_id': delivery['id'], 'queued': True, 'delivered_verified': False}
            return self.c.mutation(principal, room, 'weixin_attachments', args, save)

    def claim(self, identifier):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM channel_attachments WHERE id=?', (identifier,))
            now = self.c.clock()
            if not row or row.get('channel', 'weixin') != 'weixin' or row['state'] != 'pending' or row['next_attempt'] > now or (row['lease_until'] or 0) > now:
                return None
            binding, account, owner, *_ = self.w.valid(row['binding_id'])
            self.row(identifier, owner)
            used = self.store.one("SELECT COALESCE(SUM(size),0) AS n FROM channel_attachments WHERE state='ready'")['n']
            chunks = self.store.one('SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks')['n']
            own_chunks = self.store.one('SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks WHERE attachment_id=?', (identifier,))['n']
            remaining = max(0, (row['size'] if row['size'] is not None else MAX_MEDIA) - own_chunks)
            if used + chunks + remaining > MAX_CACHE:
                self.store.execute("UPDATE channel_attachments SET next_attempt=?,reason_code='cache_quota',updated=? WHERE id=?", (now + 300, now, identifier))
                return None
            claim_owner, fence = uuid.uuid4().hex, row['lease_fence'] + 1
            self.store.execute('UPDATE channel_attachments SET lease_owner=?,lease_fence=?,lease_until=?,attempts=attempts+1 WHERE id=?',
                               (claim_owner, fence, now + 180, identifier))
            return {**row, 'lease_owner': claim_owner, 'lease_fence': fence, 'account': account, 'owner': owner,
                    'binding': binding}

    def current(self, claim):
        row = self.row(claim['id'], claim['owner'])
        self.w.valid(claim['binding_id'])
        if (row['lease_owner'] != claim['lease_owner'] or row['lease_fence'] != claim['lease_fence']
                or (row['lease_until'] or 0) <= self.c.clock() or row['state'] != 'pending'):
            raise DevError('ATTACHMENT_FENCE_LOST', '附件原恢复世代已失效', 409)
        return row

    def save_chunk(self, claim, offset, content):
        with self.store.transaction():
            self.current(claim)
            checksum = hashlib.sha256(content).hexdigest()
            old = self.store.one('SELECT * FROM channel_attachment_chunks WHERE attachment_id=? AND offset=?', (claim['id'], offset))
            if old and old['sha256'] != checksum:
                raise DevError('ATTACHMENT_CHUNK_CONFLICT', '同一原产物位置出现不同字节，未覆盖已缓存分段', 409)
            if not old:
                used = self.store.one("SELECT COALESCE(SUM(size),0) AS n FROM channel_attachments WHERE state='ready'")['n']
                chunks = self.store.one('SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks')['n']
                if used + chunks + len(content) > MAX_CACHE:
                    raise DevError('ATTACHMENT_CACHE_QUOTA', '加密附件缓存配额已用尽；保留原分段', 429)
            self.store.execute('INSERT OR IGNORE INTO channel_attachment_chunks VALUES (?,?,?,?,?)',
                (claim['id'], offset, len(content), checksum, self.store.encrypt(base64.b64encode(content).decode())))
            self.store.execute('UPDATE channel_attachments SET lease_until=? WHERE id=?', (self.c.clock() + 60, claim['id']))

    def saved_chunks(self, claim):
        self.current(claim)
        values, offset = [], 0
        for row in self.store.all('SELECT * FROM channel_attachment_chunks WHERE attachment_id=? ORDER BY offset', (claim['id'],)):
            data = base64.b64decode(self.store.decrypt(row['content']), validate=True)
            if row['offset'] != offset or len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise DevError('ATTACHMENT_CACHE_CORRUPT', '原产物分段缓存不连续或摘要不匹配', 409)
            values.append(data); offset += len(data)
        return values, offset

    def commit(self, claim, content):
        with self.store.transaction():
            row = self.current(claim)
            if not isinstance(content, bytes) or len(content) > MAX_MEDIA:
                raise DevError('ATTACHMENT_SIZE_LIMIT', '附件超出不可变缓存预算', 413)
            checksum = hashlib.sha256(content).hexdigest()
            if row['sha256'] and (checksum != row['sha256'] or len(content) != row['size']):
                raise DevError('ATTACHMENT_HASH_MISMATCH', '附件与原产物摘要或大小不一致', 409)
            used = self.store.one("SELECT COALESCE(SUM(size),0) AS n FROM channel_attachments WHERE state='ready'")['n']
            other_chunks = self.store.one('SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks WHERE attachment_id!=?', (row['id'],))['n']
            if used + other_chunks + len(content) > MAX_CACHE:
                raise DevError('ATTACHMENT_CACHE_QUOTA', '加密附件缓存配额已用尽；保留原分段', 429)
            kind, mime = self.content_type(content)
            self.store.execute("UPDATE channel_attachments SET state='ready',size=?,sha256=?,content=?,kind=?,mime=?,lease_until=0,reason_code='',updated=? WHERE id=?",
                (len(content), checksum, self.store.encrypt(base64.b64encode(content).decode()), kind, mime, self.c.clock(), row['id']))
            self.store.execute('DELETE FROM channel_attachment_chunks WHERE attachment_id=?', (row['id'],))

    @staticmethod
    def content_type(content):
        # Signature classification only, not a malware scan or an executable
        # preview. HTTP always serves octet-stream with attachment + nosniff.
        if content.startswith(b'\x89PNG\r\n\x1a\n') and len(content) >= 24 and content[12:16] == b'IHDR':
            return 'image', 'image/png'
        if content.startswith(b'\xff\xd8\xff') and content.endswith(b'\xff\xd9'):
            return 'image', 'image/jpeg'
        if content.startswith((b'GIF87a', b'GIF89a')) and len(content) >= 13:
            return 'image', 'image/gif'
        if content.startswith(b'RIFF') and content[8:12] == b'WEBP' and len(content) >= 20:
            return 'image', 'image/webp'
        if len(content) >= 12 and content[4:8] == b'ftyp' and content[8:12] in {b'isom', b'iso2', b'mp41', b'mp42'}:
            return 'video', 'video/mp4'
        return 'file', 'application/octet-stream'

    def failed(self, claim, error):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM channel_attachments WHERE id=?', (claim['id'],))
            if not row or row['lease_owner'] != claim['lease_owner'] or row['lease_fence'] != claim['lease_fence']:
                return
            permanent = isinstance(error, DevError) and error.status in {400, 401, 403, 404, 409, 410, 413, 422}
            terminal = permanent or row['attempts'] >= 5
            self.store.execute('UPDATE channel_attachments SET state=?,reason_code=?,next_attempt=?,lease_until=0,updated=? WHERE id=?',
                ('unavailable' if terminal else 'pending', error.code if isinstance(error, DevError) else type(error).__name__,
                 self.c.clock() + min(300, 5 * 2 ** min(row['attempts'], 6)), self.c.clock(), row['id']))

    async def materialize(self, identifier):
        try:
            claim = await self.store.run(self.claim, identifier)
        except DevError as exc:
            # Keep the original item and authorization boundary. An inaccessible
            # oldest attachment must not monopolize the bounded global scan.
            def blocked(code=exc.code):
                with self.store.transaction():
                    now = self.c.clock()
                    self.store.execute('''UPDATE channel_attachments SET reason_code=?,next_attempt=?,updated=?
                        WHERE id=? AND state='pending' AND COALESCE(lease_until,0)<=?''',
                        (code, now + 60, now, identifier, now))
            await self.store.run(blocked)
            return
        if not claim:
            return
        try:
            if claim['direction'] == 'inbound':
                source = json.loads(self.store.decrypt(claim['private_source']))
                client = self.w.client_factory(claim['account']['api_base'], self.store.decrypt(claim['account']['secret']))
                content = await client.download_media(source['media'])
            else:
                chunks, offset = await self.store.run(self.saved_chunks, claim)
                while offset < claim['size']:
                    def inspect():
                        self.current(claim)
                        return self.c.runtime.artifacts.row(claim['artifact_id'], claim['owner'])
                    artifact, project = await self.store.run(inspect)
                    chunk = await self.c.runtime.artifacts.chunk(artifact, project, offset)
                    await self.store.run(self.save_chunk, claim, offset, chunk)
                    chunks.append(chunk); offset += len(chunk)
                content = b''.join(chunks)
            await self.store.run(self.commit, claim, content)
        except asyncio.CancelledError:
            await self.store.run(self.failed, claim, DevError('ATTACHMENT_INTERRUPTED', '原附件传输暂时中断，可按原位置恢复', 503))
            raise
        except Exception as exc:
            await self.store.run(self.failed, claim, exc)

    def content(self, identifier, principal, range_header=None):
        row = self.row(identifier, principal)
        if row['state'] != 'ready' or not row['content']:
            raise DevError('ATTACHMENT_NOT_READY', '原附件尚不可用；此读取不会重跑生成任务', 409)
        data = base64.b64decode(self.store.decrypt(row['content']), validate=True)
        if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise DevError('ATTACHMENT_CACHE_CORRUPT', '附件缓存完整性校验失败', 409)
        start, end, partial = byte_range(range_header, len(data))
        return data[start:end + 1], row, start, end, partial

    def outbound_content(self, identifier, binding_id):
        binding, _, owner, *_ = self.w.valid(binding_id)
        row = self.row(identifier, owner)
        if (not binding['allow_media_outbound'] or row['binding_id'] != binding_id
                or row['generation'] != binding['generation'] or row['peer_id'] != binding['peer_id'] or row['direction'] != 'outbound'):
            raise DevError('ATTACHMENT_EGRESS_NOT_APPROVED', '附件不属于原收件人和已批准的外发绑定', 403)
        content, row, *_ = self.content(identifier, owner)
        return content, row

    def expire(self):
        with self.store.transaction():
            rows = self.store.all("SELECT id FROM channel_attachments WHERE state!='expired' AND expires_at<=? ORDER BY expires_at LIMIT 100", (self.c.clock(),))
            for row in rows:
                # Keep immutable metadata/reason and delivery history; remove
                # only expired encrypted bytes and invalidate active fetches.
                self.store.execute("UPDATE channel_attachments SET state='expired',content=NULL,private_source='',lease_fence=lease_fence+1,lease_until=0,reason_code='retention_expired',updated=? WHERE id=?", (self.c.clock(), row['id']))
                self.store.execute('DELETE FROM channel_attachment_chunks WHERE attachment_id=?', (row['id'],))

    async def tick(self):
        if not self.c.config.enabled:
            return
        await self.store.run(self.expire)
        rows = await self.store.run(lambda: self.store.all("SELECT id FROM channel_attachments WHERE channel='weixin' AND state='pending' AND next_attempt<=? AND COALESCE(lease_until,0)<=? ORDER BY created,id LIMIT 2", (self.c.clock(), self.c.clock())))
        await asyncio.gather(*(self.materialize(row['id']) for row in rows))
