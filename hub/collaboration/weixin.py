"""Owner-approved external channel; messages are never execution authority."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import hmac
import json
import os
import secrets
import uuid

from hub.collaboration.common import canonical, digest, redact, validate
from hub.collaboration import weixin_schema as models
from hub.collaboration.weixin_protocol import API_BASE, ITEM_NAMES, MAX_JSON, WeixinClient, approved_url, safe_filename, text, uint64, validate_updates
from hub.principal import Principal, refresh_principal
from hub.room_scope import snapshot as project_snapshot
from shared.util import DevError

MAX_OWNER_ACCOUNTS = 32
MAX_INSTANCE_ACCOUNTS = 256
MAX_DAILY_INBOUND_VERSIONS = 5000
MAX_PENDING_INBOUND = 500


def principal_record(principal):
    value = asdict(principal)
    value['scopes'] = sorted(value['scopes'])
    # Owner consent is durable, not a stolen panel session or another token.
    value['session_hash'] = value['token_hash'] = ''
    return canonical(value)


class WeixinService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        bases = (API_BASE, *os.environ.get('CODEPIER_WEIXIN_APPROVED_API_BASES', '').split())
        self.allowed_api_bases = tuple(dict.fromkeys(approved_url(v, bases=(v,), exact_base=True) for v in bases))
        self.client_factory = lambda base, token: WeixinClient(base, token=token, allowed_api_bases=self.allowed_api_bases)
        from hub.collaboration.channel_media import ChannelMedia
        from hub.collaboration.weixin_delivery import WeixinDelivery
        self.media = ChannelMedia(self)
        self.delivery = WeixinDelivery(self)
        from hub.collaboration.weixin_qr import WeixinEnrollment
        self.qr = WeixinEnrollment(self)

    def require_enabled(self, owner):
        self.c.channel_settings.require_enabled('weixin', owner)

    def owner(self, raw, principal, model, *, enabled=True):
        args = validate(model, raw)
        principal, room = self.c.scope(principal, args)
        self.c.owner(principal)
        conversation = self.c.conversations.resolve(principal, room, args)
        args['conversation_id'] = conversation['id']
        if enabled:
            self.c.live_room(room)
            self.require_enabled(principal)
        return args, principal, room

    def account(self, identifier, principal):
        row = self.store.one('SELECT * FROM weixin_accounts WHERE id=? AND space_id=? AND owner_user_id=?',
                             (identifier, principal.space_id, principal.user_id))
        if not row:
            raise DevError('WEIXIN_ACCOUNT_NOT_FOUND', '当前房主未登记这个微信账号', 404)
        return row

    def binding(self, identifier, principal, room):
        row = self.store.one('SELECT * FROM weixin_bindings WHERE id=? AND room_id=? AND owner_user_id=? AND space_id=?',
                             (identifier, room['id'], principal.user_id, principal.space_id))
        if not row:
            raise DevError('WEIXIN_BINDING_NOT_FOUND', '当前房间没有这个微信绑定', 404)
        self.c.conversations.object(principal, row['conversation_id'])
        return row

    def valid(self, identifier, *, pairing=False):
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (identifier,))
        if binding:
            self.require_enabled(binding)
        allowed = {'active', 'pairing', 'awaiting_owner'} if pairing else {'active'}
        if not binding or binding['state'] not in allowed or binding['expires_at'] <= self.c.clock():
            raise DevError('WEIXIN_BINDING_INACTIVE', '原微信绑定已暂停、撤销或到期', 403)
        if binding['state'] == 'pairing' and binding['pairing_until'] <= self.c.clock():
            raise DevError('WEIXIN_PAIRING_EXPIRED', '配对码已到期；原登录凭据保留，请在面板明确撤销后重新绑定', 403)
        value = json.loads(binding['owner_principal'])
        value['scopes'] = set(value['scopes'])
        owner = refresh_principal(self.store, Principal(**value))
        self.c.owner(owner)
        account = self.account(binding['account_id'], owner)
        if (account['state'] != 'active' or account['credential_version'] != binding['credential_version']
                or account['generation'] != binding['generation']):
            raise DevError('WEIXIN_CREDENTIAL_CHANGED', '原微信账号凭据或绑定世代已变化，不继承新授权', 403)
        _, room = self.c.scope(owner, {'project': binding['project_id'], 'environment_id': binding['environment_id'],
                                       'conversation_id': binding['conversation_id']})
        self.c.live_room(room)
        project = self.c.runtime.project(binding['project_id'], owner)
        if (project_snapshot(project) != json.loads(binding['project_snapshot'])
                or room['id'] != binding['room_id'] or room['owner_user_id'] != binding['owner_user_id']):
            raise DevError('WEIXIN_MAPPING_CHANGED', '原微信房间项目、节点或目录已变化，需重新确认', 403)
        dot = self.c.dots.find(binding['dot_id'])
        if not dot or dot['room_id'] != room['id'] or dot['conversation_id'] != binding['conversation_id']:
            raise DevError('WEIXIN_DOT_CHANGED', '原微信目标 dot 已变化', 403)
        slot = self.c.dot_chat.authorize(owner, room, dot)
        self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(binding['dot_principal']))
        return binding, account, owner, room, dot

    def import_account(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.AccountImport)
            token = args.pop('token').get_secret_value()
            text(token, 16384)
            if any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise DevError('WEIXIN_CREDENTIAL_INVALID', '微信凭据不能包含控制字符', 422)
            args['credential_digest'] = hashlib.sha256(token.encode()).hexdigest()
            approved_url(args['api_base'], bases=self.allowed_api_bases, exact_base=True)

            def save():
                old = self.store.one('SELECT * FROM weixin_accounts WHERE bot_id=?', (args['bot_id'],))
                if old and (old['space_id'] != principal.space_id or old['owner_user_id'] != principal.user_id):
                    raise DevError('WEIXIN_ACCOUNT_CONFLICT', '此机器人已绑定另一个认证身份，不可复用其账号', 409)
                if not old:
                    owned = self.store.one('SELECT COUNT(*) AS n FROM weixin_accounts WHERE owner_user_id=? AND space_id=?', (principal.user_id, principal.space_id))['n']
                    total = self.store.one('SELECT COUNT(*) AS n FROM weixin_accounts')['n']
                    if owned >= MAX_OWNER_ACCOUNTS or total >= MAX_INSTANCE_ACCOUNTS:
                        raise DevError('WEIXIN_ACCOUNT_QUOTA', '已登记机器人数量达到明确上限；不覆盖其他账号或绕过房间预算', 409)
                if (old['credential_version'] if old else 0) != args['expected_version']:
                    raise DevError('STALE_VERSION', '微信账号版本已变化', 409)
                now, identifier = self.c.clock(), old['id'] if old else uuid.uuid4().hex
                self.store.execute('''INSERT INTO weixin_accounts
                    (id,space_id,owner_user_id,bot_id,label,api_base,secret,state,credential_version,cursor,created,updated)
                    VALUES (?,?,?,?,?,?,?,'active',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label,api_base=excluded.api_base,secret=excluded.secret,state='active',
                    credential_version=excluded.credential_version,poll_fence=poll_fence+1,poll_until=0,
                    reason_code='',failures=0,next_poll=0,last_sync_at=NULL,updated=excluded.updated''',
                    (identifier, principal.space_id, principal.user_id, args['bot_id'], args['label'], args['api_base'],
                     self.store.encrypt(token), args['expected_version'] + 1, self.store.encrypt(''), now, now))
                if old:
                    self.store.execute("UPDATE weixin_bindings SET state='revoked',version=version+1,updated=? WHERE account_id=? AND state!='revoked'", (now, identifier))
                    self.delivery.quarantine_account(identifier, 'credential_changed')
                self.c.audit(room, principal.actor, 'weixin.account_approved', identifier,
                    {'credential_version': args['expected_version'] + 1, 'creates_mcp_grant': False})
                return {'account_id': identifier, 'credential_version': args['expected_version'] + 1,
                        'bot_id': args['bot_id'], 'state': 'active', 'credential_stored': True, 'room_bound': False}
            return self.c.mutation(principal, room, 'weixin_account', args, save)

    def bind(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.Bind)
            account = self.account(args['account_id'], principal)
            dot = self.c.dots.find(args['dot_id'])
            if not dot or dot['room_id'] != room['id'] or dot['conversation_id'] != args['conversation_id']:
                raise DevError('WEIXIN_DOT_SCOPE', '必须选择此话题已经登记的双向 dot', 403)
            slot = self.c.dot_chat.authorize(principal, room, dot)
            project = self.c.runtime.project(room['project_id'], principal)

            def save():
                if account['state'] != 'active' or self.store.one("SELECT 1 FROM weixin_bindings WHERE state!='revoked' AND (account_id=? OR conversation_id=?)", (account['id'], dot['conversation_id'])):
                    raise DevError('WEIXIN_BINDING_CONFLICT', '一个话题仅绑定一个机器人；当前机器人不跨话题复用。先明确撤销旧绑定', 409)
                identifier, now = uuid.uuid4().hex, self.c.clock()
                challenge = 'CPW-' + secrets.token_hex(16).upper()
                generation = account['generation'] + 1
                self.store.execute('UPDATE weixin_accounts SET generation=?,poll_fence=poll_fence+1,poll_until=0,next_poll=0,last_sync_at=NULL,updated=? WHERE id=?',
                                   (generation, now, account['id']))
                self.store.execute('''INSERT INTO weixin_bindings
                    (id,account_id,space_id,owner_user_id,room_id,conversation_id,project_id,environment_id,dot_id,
                     owner_principal,dot_principal,project_snapshot,credential_version,generation,state,pairing_hash,
                     pairing_until,allow_replies,allow_media_outbound,expires_at,created,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pairing',?,?,?,?,?,?,?)''',
                    (identifier, account['id'], principal.space_id, principal.user_id, room['id'], dot['conversation_id'],
                     room['project_id'], room['environment_id'], dot['id'], principal_record(principal), slot['principal'],
                     canonical(project_snapshot(project)), account['credential_version'], generation, digest(challenge),
                     now + 900, int(args['allow_replies']), int(args['allow_media_outbound']), now + args['expires_seconds'], now, now))
                self.store.execute('UPDATE weixin_bindings SET pairing_secret=? WHERE id=?', (self.store.encrypt(challenge), identifier))
                self.c.audit(room, principal.actor, 'weixin.binding_pairing_requested', identifier,
                    {'generation': generation, 'dot_id': dot['id'], 'allow_replies': args['allow_replies'],
                     'allow_media_outbound': args['allow_media_outbound'], 'automatic_execution': False})
                return {'binding_id': identifier, 'version': 1, 'generation': generation, 'state': 'pairing',
                        'pairing_challenge': challenge, 'pairing_until': now + 900,
                        'instructions': '在准备绑定的微信私聊中发送此一次性配对码；回到面板核对真实 peer 编号后明确激活。扫码人身份不自动等同房主。'}
            return self.c.mutation(principal, room, 'weixin_bind', args, save)

    def control(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.Control, enabled=False)
            binding = self.binding(args['binding_id'], principal, room)

            def save():
                current = self.binding(binding['id'], principal, room)
                if current['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '微信绑定已变化，请重新核对', 409)
                action = args['action']
                if action == 'activate':
                    self.valid(current['id'], pairing=True)
                    if (current['state'] != 'awaiting_owner' or not current['pending_peer_id']
                            or not hmac.compare_digest(current['pending_peer_id'], args['confirm_peer_id'])):
                        raise DevError('WEIXIN_PAIRING_UNVERIFIED', '必须核对当前配对挑战实际返回的微信 peer 编号', 409)
                    state, peer = 'active', current['pending_peer_id']
                elif action == 'resume':
                    self.require_enabled(principal)
                    if current['state'] != 'paused' or not current['peer_id'] or current['expires_at'] <= self.c.clock():
                        raise DevError('WEIXIN_RESUME_DENIED', '只有原配对仍有效的暂停绑定可以恢复；不会重放历史消息', 409)
                    state, peer = 'active', current['peer_id']
                elif action == 'pause':
                    if current['state'] == 'revoked':
                        raise DevError('WEIXIN_BINDING_INACTIVE', '已撤销绑定不能恢复为暂停', 409)
                    state, peer = 'paused', current['peer_id']
                else:
                    state, peer = 'revoked', current['peer_id']
                now = self.c.clock()
                self.store.execute('UPDATE weixin_bindings SET state=?,peer_id=?,pairing_hash=\'\',pairing_secret=\'\',version=version+1,updated=? WHERE id=? AND version=?',
                    (state, peer, now, current['id'], args['expected_version']))
                self.store.execute('UPDATE weixin_accounts SET poll_fence=poll_fence+1,poll_until=0,next_poll=0 WHERE id=?', (current['account_id'],))
                if state == 'revoked':
                    self.delivery.quarantine_binding(current['id'], 'binding_revoked')
                    self.store.execute('DELETE FROM weixin_contexts WHERE account_id=? AND generation=?', (current['account_id'], current['generation']))
                if state == 'active':
                    self.valid(current['id'])
                self.c.audit(room, principal.actor, 'weixin.binding_' + action, current['id'],
                             {'generation': current['generation'], 'automatic_replay': False})
                return {'binding_id': current['id'], 'version': args['expected_version'] + 1, 'state': state,
                        'peer_id': peer, 'automatic_execution': False, 'historical_replay': False}
            return self.c.mutation(principal, room, 'weixin_control', args, save)

    def owner_request(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.OwnerRequest)
            source = self.c.object('collaboration_messages', room, args['message_id'])
            self.c.chatroom.require_message_visible(principal, source)
            origin = json.loads(source['body']).get('external_origin', {})
            if (source['origin'] != 'external_channel' or source['version'] != args['expected_message_version']
                    or source['conversation_id'] != args['conversation_id'] or not origin.get('binding_id')):
                raise DevError('WEIXIN_SOURCE_CHANGED', '外部原消息或话题已变化', 409)
            binding, *_ = self.valid(origin['binding_id'])
            # A NEW explicit panel-owner message carries only the owner's newly
            # reviewed text. The external source keeps its untrusted identity.
            return self.c.chatroom.create({'project': args['project'], 'environment_id': args['environment_id'],
                'room_id': room['id'], 'conversation_id': args['conversation_id'], 'body_text': args['request_text'],
                'reply_to_id': source['id'], 'mentions': [{'slot_id': binding['dot_id']}],
                'client_message_id': 'weixin-owner:' + digest([source['id'], args['idempotency_key']]),
                'idempotency_key': args['idempotency_key']}, principal)

    def listing(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room = self.owner(raw, principal, models.Scope, enabled=False)
            accounts = self.store.all('''SELECT id,bot_id,label,state,credential_version,reason_code,last_sync_at,next_poll,updated FROM weixin_accounts
                WHERE space_id=? AND owner_user_id=? ORDER BY created DESC LIMIT 100''', (principal.space_id, principal.user_id))
            bindings = self.store.all('''SELECT id,account_id,dot_id,conversation_id,generation,version,state,peer_id,pending_peer_id,
                pairing_until,allow_replies,allow_media_outbound,expires_at,updated FROM weixin_bindings
                WHERE room_id=? AND conversation_id=? ORDER BY created DESC LIMIT 100''', (room['id'], args['conversation_id']))
            for binding in bindings:
                if binding['state'] == 'pairing' and binding['pairing_until'] > self.c.clock():
                    secret = self.store.one('SELECT pairing_secret FROM weixin_bindings WHERE id=?', (binding['id'],))['pairing_secret']
                    binding['pairing_challenge'] = self.store.decrypt(secret) if secret else None
            artifacts = self.c.runtime.artifacts.list({'project': room['project_id'], 'limit': 50, 'cursor': ''}, principal)
            candidates = []
            for message in self.store.all('''SELECT * FROM collaboration_messages WHERE room_id=? AND conversation_id=?
                AND origin='bound_connector' ORDER BY created DESC,id DESC LIMIT 50''', (room['id'], args['conversation_id'])):
                self.c.chatroom.require_message_visible(principal, message)
                body = json.loads(message['body'])
                if body.get('sender_dot_id') and body.get('dot_reply_complete') is not False and (source := self.delivery.root_source(message)) and json.loads(source['body']).get('external_origin', {}).get('channel') == 'weixin':
                    candidates.append({'id': message['id'], 'version': message['version'], 'body_text': body.get('body_text', '')[:120]})
                if len(candidates) == 20:
                    break
            deliveries = self.delivery.listing(room['id'], args['conversation_id'])
            setting = self.c.channel_settings.setting('weixin', principal)
            return {'enabled': setting['enabled'], 'setting': setting, 'accounts': accounts,
                    'bindings': bindings, 'deliveries': deliveries, 'read_only': True,
                    'artifact_candidates': [r for r in artifacts['artifacts'] if not r['expired'] and r['bytes'] <= 32 * 1024 * 1024],
                    'artifact_candidates_more': bool(artifacts['next_cursor']), 'reply_candidates': candidates,
                    'candidate_scope': 'most_recent_50_artifacts_and_20_replies',
                    'capabilities': {'direct_chat': True, 'native_groups': False, 'automatic_execution': False,
                        'provider_receipt_lookup': False, 'delivery_verified': False, 'external_account_poc_verified': False,
                        'received_attachments': ['image', 'video', 'voice', 'file'], 'sent_attachments': ['image', 'video', 'file']},
                    'warning': '仅机器人私聊桥接；通道已接收不代表用户已读，未知发送不会自动重发。'}

    def claim_poll(self, account_id):
        with self.store.transaction():
            account = self.store.one('SELECT * FROM weixin_accounts WHERE id=?', (account_id,))
            now = self.c.clock()
            if not account or account['state'] != 'active' or (account['poll_until'] or 0) > now or account['next_poll'] > now:
                return None
            binding = self.store.one("SELECT id FROM weixin_bindings WHERE account_id=? AND state IN ('pairing','awaiting_owner','active')", (account_id,))
            if not binding:
                return None
            try:
                current, account, *_ = self.valid(binding['id'], pairing=True)
            except DevError as exc:
                # Persist backoff even when authorization fails before acquiring
                # a poll. Otherwise a few invalid accounts monopolize every scan.
                self.store.execute('UPDATE weixin_accounts SET reason_code=?,next_poll=?,updated=? WHERE id=?',
                    (exc.code, now + 60, now, account_id))
                return None
            owner, fence = uuid.uuid4().hex, account['poll_fence'] + 1
            self.store.execute('UPDATE weixin_accounts SET poll_owner=?,poll_fence=?,poll_until=? WHERE id=?', (owner, fence, now + 55, account_id))
            return {**account, 'poll_owner': owner, 'poll_fence': fence, 'binding_id': current['id'],
                    'binding_version': current['version'], 'generation': current['generation']}

    def poll_current(self, claim):
        self.require_enabled(claim)
        account = self.store.one('SELECT * FROM weixin_accounts WHERE id=?', (claim['id'],))
        if (not account or account['poll_owner'] != claim['poll_owner'] or account['poll_fence'] != claim['poll_fence']
                or account['state'] != 'active' or account['poll_until'] <= self.c.clock()):
            raise DevError('WEIXIN_POLL_FENCE_LOST', '原微信轮询世代已失效；不能提交游标或外部消息', 409)
        return account

    def commit_updates(self, claim, response):
        with self.store.transaction():
            self.poll_current(claim)
            binding, account, owner, room, dot = self.valid(claim['binding_id'], pairing=True)
            if binding['version'] != claim['binding_version'] or binding['generation'] != claim['generation']:
                raise DevError('WEIXIN_BINDING_CHANGED', '轮询期间房间绑定变化，丢弃旧批次且不前移游标', 409)
            validate_updates(response)
            messages, cursor = response.get('msgs', []), response.get('get_updates_buf', '')
            if len(canonical(response).encode()) > MAX_JSON:
                raise DevError('WEIXIN_BATCH_LIMIT', '收件批次字节超过上限', 413)
            received = []
            for message in messages:
                binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (binding['id'],))
                receipt = self.ingest_one(binding, account, owner, room, dot, message)
                if receipt:
                    received.append(receipt)
            # Canonical message, ordered attachment jobs, Dot inbox, dedup and
            # opaque cursor commit together. Network/media work never runs here.
            self.store.execute('''UPDATE weixin_accounts SET cursor=?,poll_until=0,failures=0,reason_code='',
                next_poll=?,updated=?,last_sync_at=? WHERE id=? AND poll_owner=? AND poll_fence=?''',
                (self.store.encrypt(cursor or self.store.decrypt(account['cursor'])), self.c.clock() + 2,
                 self.c.clock(), self.c.clock(), account['id'], claim['poll_owner'], claim['poll_fence']))
            return {'committed': True, 'message_ids': received, 'cursor_persisted': bool(cursor)}

    def ingest_one(self, binding, account, owner, room, dot, message):
        if not isinstance(message, dict):
            raise DevError('WEIXIN_MESSAGE_INVALID', '消息不是结构化对象；整批保留原游标', 422)
        peer = text(message.get('from_user_id'), 256)
        identifier = uint64(message.get('message_id'))
        source = {k: v for k, v in message.items() if k not in {'context_token', 'seq'}}
        source_hash = digest(source)
        identity = (account['id'], binding['generation'], peer, identifier)
        created = uint64(message.get('create_time_ms', 0))
        updated = uint64(message.get('update_time_ms', 0))
        deleted = uint64(message.get('delete_time_ms', 0))
        version = str(max(int(created), int(updated), int(deleted)))
        version = '' if version == '0' else version
        created = '' if created == '0' else created
        old = self.store.one('SELECT * FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?', identity)
        historical = self.store.one('''SELECT * FROM weixin_inbound_versions WHERE account_id=? AND generation=?
            AND peer_id=? AND external_id=? AND provider_version=?''', (*identity, version))
        if historical:
            if historical['source_digest'] != source_hash:
                raise DevError('WEIXIN_DUPLICATE_CONFLICT', '同一微信消息版本出现不同内容，保留原游标和历史供核对', 409)
            return historical['message_id']
        if old and old['source_digest'] == source_hash:
            return old['message_id']
        if old and (not version or not old['provider_version']
                    or version == old['provider_version']
                    or created and old['provider_created'] and created != old['provider_created']):
            raise DevError('WEIXIN_DUPLICATE_CONFLICT', '同一微信消息编号出现不同内容但没有可比较的版本，保留原游标和历史', 409)
        newest = not old or int(version) > int(old['provider_version'])
        daily = self.store.one('SELECT COUNT(*) AS n FROM weixin_inbound_versions WHERE account_id=? AND created>?',
                               (account['id'], self.c.clock() - 86400))['n']
        if daily >= MAX_DAILY_INBOUND_VERSIONS:
            raise DevError('WEIXIN_DAILY_QUOTA', '微信每日持久收件预算用尽，保留原游标；已提交的相同版本仍可核对', 429)
        disposition, message_id = 'unpaired_peer_quarantined', None
        items = message.get('item_list', [])
        if not isinstance(items, list) or len(items) > 32:
            raise DevError('WEIXIN_ITEMS_INVALID', '外部消息分项超过上限', 422)
        echo = bool(message.get('client_id') and (self.store.one('SELECT 1 FROM weixin_parts WHERE client_id=?', (message['client_id'],)) or self.store.one("SELECT 1 FROM channel_mirror_parts WHERE 'cpm-' || id=?", (message['client_id'],))))
        direct = not message.get('group_id') and type(message.get('message_type')) is int and message['message_type'] == 1 and not echo and peer != account['bot_id']
        if direct and message.get('to_user_id') not in (None, '', account['bot_id']):
            raise DevError('WEIXIN_RECEIVER_MISMATCH', '微信应答接收机器人不属于原绑定', 409)
        if not direct:
            disposition = 'group_or_echo_quarantined'
        elif binding['state'] == 'pairing' and binding['pairing_until'] > self.c.clock():
            body = '\n'.join(text(i.get('text_item', {}).get('text'), 12000, empty=True) for i in items if isinstance(i, dict) and i.get('type') == 1)
            if hmac.compare_digest(digest(body.strip()), binding['pairing_hash']):
                self.store.execute("UPDATE weixin_bindings SET state='awaiting_owner',pending_peer_id=?,pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?",
                                   (peer, self.c.clock(), binding['id']))
                disposition = 'pairing_waits_for_panel_owner'
        elif binding['state'] == 'active' and hmac.compare_digest(peer, binding['peer_id']):
            body_text, attachment_items = [], []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or type(item.get('type')) is not int:
                    raise DevError('WEIXIN_ITEMS_INVALID', '外部消息分项类型无效', 422)
                if item['type'] == 1:
                    body_text.append(text(item.get('text_item', {}).get('text'), 12000, empty=True))
                elif item['type'] in ITEM_NAMES:
                    attachment_items.append((index, item))
                else:
                    body_text.append('[未支持的外部消息分项]')
            rendered = '\n'.join(body_text) or '[附件消息]'
            if int(deleted):
                rendered, attachment_items = '[微信上游标记消息已删除；旧版本保留为审计历史。]', []
            text(rendered, 24000)
            if newest:
                pending = self.store.one('''SELECT COUNT(*) AS n FROM weixin_inbound_versions v
                    JOIN collaboration_dot_messages m ON m.message_id=v.message_id
                    WHERE v.account_id=? AND v.generation=? AND m.dot_id=? AND m.state!='handled' ''',
                    (account['id'], binding['generation'], dot['id']))['n']
                if pending >= MAX_PENDING_INBOUND:
                    raise DevError('WEIXIN_INBOX_QUOTA', '原 dot 未处理消息达到积压上限；保留原游标，不丢弃正文', 429)
            message_id = uuid.uuid4().hex
            previous_id = old['message_id'] if old and old['message_id'] else ''
            previous = self.c.object('collaboration_messages', room, previous_id) if previous_id else None
            thread_root = previous['thread_root_id'] if previous else message_id
            body = {'body_text': redact(rendered), 'mentions': [{'slot_id': dot['id']}],
                    'duplex_recipients': [dot['id']], 'provenance_project_ids': [room['project_id']],
                    'external_origin': {'schema_version': 1, 'channel': 'weixin', 'binding_id': binding['id'],
                        'generation': binding['generation'], 'account_id': account['id'], 'external_message_id': identifier,
                        'peer_id': peer, 'peer_pairing_verified': True, 'execution_authority': False,
                        'provider_version': version or None, 'revision_is_latest_at_commit': newest,
                        'provider_deleted': bool(int(deleted)),
                        'supersedes_message_id': previous_id if newest else '',
                        'related_current_message_id': previous_id if not newest else ''},
                    'requires_owner_execution_request': True}
            self.store.execute('''INSERT INTO collaboration_messages
                (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created)
                VALUES (?,?,?,?,?,?,?,'external_channel',?,'text',?,'saved',?)''',
                (message_id, room['id'], binding['conversation_id'], thread_root, thread_root, previous_id,
                 'weixin:' + account['id'] + ':' + digest(peer)[:20],
                 'generation:' + str(binding['generation']) + ':' + identifier + ':version:' + (version or 'initial'),
                 canonical(body), self.c.clock()))
            for ordinal, item in attachment_items:
                self.media.add_inbound(binding, message_id, ordinal, item)
            if newest:
                self.store.execute('''INSERT INTO collaboration_dot_messages(dot_id,message_id,message_version,message_digest,created)
                    VALUES (?,?,1,?,?)''', (dot['id'], message_id, digest(body['body_text']), self.c.clock()))
            canonical_message = self.c.object('collaboration_messages', room, message_id)
            # Reuse only the bounded event delivery machinery. The stored author
            # remains external_channel, so inbox never issues an owner task_request.
            body['notifications'] = self.c.chatroom.notify(room, canonical_message, owner, body['mentions']) if newest else []
            self.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), message_id))
            context = message.get('context_token')
            if context and newest:
                text(context, 65536)
                self.store.execute('''INSERT INTO weixin_contexts(account_id,generation,peer_id,token,observed_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(account_id,generation,peer_id) DO UPDATE SET
                    token=excluded.token,observed_at=excluded.observed_at,version=version+1''',
                    (account['id'], binding['generation'], peer, self.store.encrypt(context), self.c.clock()))
            self.c.channels.queue(self.c.object('collaboration_messages', room, message_id))
            disposition = 'message_committed'
        self.store.execute('INSERT INTO weixin_inbound_versions VALUES (?,?,?,?,?,?,?,?)',
            (*identity, version, source_hash, message_id, self.c.clock()))
        if newest:
            self.store.execute('''INSERT INTO weixin_inbound
                (account_id,generation,peer_id,external_id,source_digest,message_id,disposition,created,provider_version,provider_created)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,generation,peer_id,external_id) DO UPDATE SET
                source_digest=excluded.source_digest,message_id=excluded.message_id,disposition=excluded.disposition,
                provider_version=excluded.provider_version,provider_created=excluded.provider_created''',
                (*identity, source_hash, message_id, disposition, self.c.clock(), version,
                 created or (old['provider_created'] if old else '')))
        return message_id

    def poll_failed(self, claim, error):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM weixin_accounts WHERE id=?', (claim['id'],))
            if not row or row['poll_owner'] != claim['poll_owner'] or row['poll_fence'] != claim['poll_fence']:
                return
            code = error.code if isinstance(error, DevError) else type(error).__name__
            if code in {'WEIXIN_POLL_IDLE', 'WEIXIN_POLL_FENCE_LOST', 'WEIXIN_POLL_CANCELLED',
                        'WEIXIN_BINDING_CHANGED', 'WEIXIN_PAIRING_EXPIRED'}:
                # Expected idle/read timeout and obsolete local leases are not
                # credential failures. Never advance a cursor, fabricate sync
                # success, or let an old claim overwrite its replacement.
                self.store.execute('UPDATE weixin_accounts SET reason_code=?,next_poll=?,poll_until=0,updated=? WHERE id=?',
                    (code, self.c.clock() + 2, self.c.clock(), row['id']))
                return
            permanent = isinstance(error, DevError) and error.status in {401, 403, 409, 422}
            failures = min(row['failures'] + 1, 10)
            delay = min(3600, 5 * 2 ** failures) + secrets.randbelow(6)
            if isinstance(error, DevError):
                delay = max(delay, error.details.get('retry_after_seconds', 0))
            self.store.execute('UPDATE weixin_accounts SET state=?,reason_code=?,failures=?,next_poll=?,poll_until=0,updated=? WHERE id=?',
                ('attention' if permanent else row['state'], code, failures, self.c.clock() + delay, self.c.clock(), row['id']))

    async def poll_once(self, account_id):
        try:
            claim = await self.store.run(self.claim_poll, account_id)
        except DevError:
            # One unavailable account cannot stop all other independent lanes.
            return
        if not claim:
            return
        try:
            await self.store.run(self.poll_current, claim)
            client = self.client_factory(claim['api_base'], self.store.decrypt(claim['secret']))
            response = await client.updates(self.store.decrypt(claim['cursor']))
            await self.store.run(self.commit_updates, claim, response)
        except asyncio.CancelledError:
            await self.store.run(self.poll_failed, claim, DevError('WEIXIN_POLL_CANCELLED', '本轮收件已停止，原游标不变', 503))
            raise
        except Exception as exc:
            await self.store.run(self.poll_failed, claim, exc)

    async def tick(self):
        if getattr(self, 'qr', None):
            await self.store.run(self.qr.expire)
        if not self.c.config.enabled:
            return
        gate = self.c.channel_settings.sql_enabled('weixin', 'a')
        accounts = await self.store.run(lambda: self.store.all(f'''SELECT a.id FROM weixin_accounts a
            JOIN weixin_bindings b ON b.account_id=a.id AND b.state IN ('pairing','awaiting_owner','active')
            WHERE {gate} AND a.state='active' AND a.next_poll<=? AND COALESCE(a.poll_until,0)<=?
            ORDER BY a.next_poll,a.updated,a.id LIMIT 4''', (self.c.clock(), self.c.clock())))
        # Send/media run in independent lifecycle loops, not behind this long poll.
        await asyncio.gather(*(self.poll_once(row['id']) for row in accounts))
