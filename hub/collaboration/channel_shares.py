"""Explicit, single-source artifact sharing grants for versioned Dot replies.

These records do not broaden a project grant or permit arbitrary file reads.
The authenticated owner selects actual immutable artifacts and one exact peer.
"""
from __future__ import annotations

import json
import uuid

from pydantic import Field

from hub.collaboration.common import canonical
from hub.collaboration import weixin_schema as models
from hub.collaboration.weixin_protocol import MAX_MEDIA, safe_filename
from shared.collaboration_contracts import Identifier
from shared.util import DevError


class Approve(models.Mutation):
    source_message_id: Identifier
    expected_source_version: int = Field(ge=1)
    artifact_ids: list[Identifier] = Field(min_length=1, max_length=16)
    confirm_file_egress: bool


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS channel_attachment_approvals(
        id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,generation INTEGER NOT NULL,
        source_message_id TEXT NOT NULL,source_version INTEGER NOT NULL,dot_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,artifact_sha256 TEXT NOT NULL,artifact_bytes INTEGER NOT NULL,
        space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,peer_id TEXT NOT NULL,
        expires_at REAL NOT NULL,used_message_id TEXT,created REAL NOT NULL)''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(channel_attachment_approvals)')}
    if 'ordinal' not in columns:
        db.execute('ALTER TABLE channel_attachment_approvals ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE INDEX IF NOT EXISTS channel_share_source ON channel_attachment_approvals(source_message_id,dot_id,expires_at)')


class ChannelShares:
    def __init__(self, media):
        self.media, self.w, self.c, self.store = media, media.w, media.c, media.store

    def approve(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, Approve)
            if args['confirm_file_egress'] is not True or len(set(args['artifact_ids'])) != len(args['artifact_ids']):
                raise DevError('ATTACHMENT_EGRESS_CONFIRMATION', '需要明确确认完整、有序、不重复的文件分享清单', 422)
            source = self.c.object('collaboration_messages', room, args['source_message_id'])
            self.c.chatroom.require_message_visible(principal, source)
            origin = json.loads(source['body']).get('external_origin', {})
            if (source['origin'] != 'external_channel' or source['version'] != args['expected_source_version']
                    or source['conversation_id'] != args['conversation_id']):
                raise DevError('ATTACHMENT_SOURCE_CHANGED', '原外部消息或版本已变化', 409)
            binding, _, owner, _, dot = self.w.valid(origin.get('binding_id', ''))
            if (not binding['allow_media_outbound'] or not binding['allow_replies']
                    or origin.get('generation') != binding['generation'] or origin.get('peer_id') != binding['peer_id']):
                raise DevError('ATTACHMENT_EGRESS_NOT_APPROVED', '当前绑定未批准向这个原收件人分享文件', 403)
            artifacts = []
            for identifier in args['artifact_ids']:
                artifact, _ = self.c.runtime.artifacts.row(identifier, principal)
                if (artifact['project_id'] != binding['project_id'] or artifact['bytes'] > MAX_MEDIA
                        or artifact['space_id'] != binding['space_id'] or artifact['expires'] <= self.c.clock()):
                    raise DevError('ATTACHMENT_ARTIFACT_SCOPE', '文件必须是当前项目已有、未过期、大小受限的真实产物', 403)
                artifacts.append(artifact)

            def save():
                ids = []
                existing = self.store.one('SELECT COUNT(*) AS n FROM channel_attachment_approvals WHERE source_message_id=? AND used_message_id IS NULL AND expires_at>?', (source['id'], self.c.clock()))['n']
                if existing + len(artifacts) > 16:
                    raise DevError('ATTACHMENT_APPROVAL_LIMIT', '原消息最多等待16件已批准附件；先处理现有清单，不静默截断', 409)
                start = self.store.one('SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM channel_attachment_approvals WHERE source_message_id=?', (source['id'],))['n']
                for ordinal, artifact in enumerate(artifacts, start):
                    identifier = uuid.uuid4().hex
                    self.store.execute('''INSERT INTO channel_attachment_approvals
                        (id,binding_id,generation,source_message_id,source_version,dot_id,artifact_id,
                         artifact_sha256,artifact_bytes,space_id,owner_user_id,peer_id,expires_at,created)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identifier, binding['id'], binding['generation'], source['id'], source['version'], dot['id'],
                         artifact['id'], artifact['sha256'], artifact['bytes'], principal.space_id, principal.user_id,
                         binding['peer_id'], min(binding['expires_at'], artifact['expires']), self.c.clock()))
                    self.store.execute('UPDATE channel_attachment_approvals SET ordinal=? WHERE id=?', (ordinal, identifier))
                    ids.append(identifier)
                self.c.audit(room, principal.actor, 'weixin.attachment_shares_approved', source['id'],
                             {'approval_ids': ids, 'artifact_ids': args['artifact_ids'], 'binding_id': binding['id']})
                return {'attachment_approval_ids': ids, 'source_message_id': source['id'], 'dot_id': dot['id'],
                        'publication_action': 'dot_message_v2', 'automatic_publication': False}
            return self.c.mutation(principal, room, 'weixin_approve_files', args, save)

    def pending(self, source_id, dot_id):
        return [r['id'] for r in self.store.all('''SELECT id FROM channel_attachment_approvals
            WHERE source_message_id=? AND dot_id=? AND used_message_id IS NULL AND expires_at>?
            ORDER BY ordinal,created,id LIMIT 16''', (source_id, dot_id, self.c.clock()))]

    def attach(self, identifiers, message, principal):
        self.store.require_transaction()
        body = json.loads(message['body'])
        source = self.w.delivery.root_source(message)
        if not source or not body.get('sender_dot_id') or body.get('dot_reply_complete') is False:
            raise DevError('ATTACHMENT_REPLY_REQUIRED', '预批附件只能随原 dot 的最终回复一起提交', 403)
        origin = json.loads(source['body']).get('external_origin', {})
        binding, _, owner, room, dot = self.w.valid(origin.get('binding_id', ''))
        self.c.dot_chat.authorize_sender(principal, room,
            self.c.conversations.object(principal, message['conversation_id']), dot['id'])
        if not binding['allow_media_outbound'] or not binding['allow_replies']:
            raise DevError('ATTACHMENT_EGRESS_NOT_APPROVED', '原分享方向已暂停或撤销', 403)
        attachments, new_ids = [], []
        for identifier in identifiers:
            approval = self.store.one('SELECT * FROM channel_attachment_approvals WHERE id=?', (identifier,))
            if (not approval or approval['expires_at'] <= self.c.clock()
                    or approval['binding_id'] != binding['id'] or approval['generation'] != binding['generation']
                    or approval['peer_id'] != binding['peer_id'] or approval['dot_id'] != dot['id']
                    or approval['source_message_id'] != source['id'] or approval['source_version'] != source['version']
                    or approval['owner_user_id'] != principal.user_id or approval['space_id'] != principal.space_id):
                raise DevError('ATTACHMENT_APPROVAL_INVALID', '文件分享批准不属于原消息、dot、收件人或当前版本', 403)
            if approval['used_message_id']:
                if approval['used_message_id'] != message['id']:
                    raise DevError('ATTACHMENT_APPROVAL_USED', '这份批准已用于另一条原回复，不可再次转发', 409)
                continue
            artifact, _ = self.c.runtime.artifacts.row(approval['artifact_id'], owner)
            if (artifact['sha256'] != approval['artifact_sha256'] or artifact['bytes'] != approval['artifact_bytes']
                    or artifact['project_id'] != binding['project_id'] or artifact['expires'] <= self.c.clock()):
                raise DevError('ATTACHMENT_ARTIFACT_CHANGED', '原产物已改变或到期，不重新生成文件', 409)
            attachments.append((approval, artifact))
        existing = self.store.one("SELECT COUNT(*) AS n FROM channel_attachments WHERE message_id=? AND direction='outbound'", (message['id'],))['n']
        if existing + len(attachments) > 16:
            raise DevError('ATTACHMENT_COUNT_LIMIT', '同一原回复的完整附件清单不能超过16件', 409)
        delivery = self.w.delivery.queue_reply(message)
        if not delivery:
            raise DevError('ATTACHMENT_DELIVERY_UNAVAILABLE', '原文件投递当前不能持久接纳；完整回帖未提交', 409)
        ordinal = self.store.one("SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM channel_attachments WHERE message_id=? AND direction='outbound'", (message['id'],))['n']
        for approval, artifact in attachments:
            identifier, now = uuid.uuid4().hex, self.c.clock()
            # Until bytes are verified, file is the safe type. The media worker
            # upgrades only recognized byte signatures, never the filename alone.
            record = {'id': identifier, 'space_id': binding['space_id'], 'owner_user_id': binding['owner_user_id'],
                'room_id': room['id'], 'conversation_id': binding['conversation_id'], 'project_id': binding['project_id'],
                'message_id': message['id'], 'ordinal': ordinal, 'direction': 'outbound', 'binding_id': binding['id'],
                'generation': binding['generation'], 'peer_id': binding['peer_id'], 'kind': 'file',
                'name': safe_filename(artifact['name']), 'mime': 'application/octet-stream', 'size': artifact['bytes'],
                'sha256': artifact['sha256'], 'artifact_id': artifact['id'], 'state': 'pending',
                'private_source': self.store.encrypt(canonical({'artifact_id': artifact['id'], 'approval_id': approval['id']})),
                'expires_at': approval['expires_at'], 'created': now, 'updated': now}
            self.store.execute('INSERT INTO channel_attachments (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
            self.store.execute('UPDATE channel_attachment_approvals SET used_message_id=? WHERE id=? AND used_message_id IS NULL', (message['id'], approval['id']))
            new_ids.append(identifier); ordinal += 1
        if new_ids:
            self.w.delivery.add_attachment_parts(delivery, new_ids)
        return {'attachment_ids': new_ids, 'delivery_id': delivery['id']}
