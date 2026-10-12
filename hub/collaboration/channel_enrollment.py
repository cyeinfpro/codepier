"""Panel preapproval + one-time private-message proof, never a new MCP grant.

All identity, scope, consent, activation and notice enqueue changes join the
inbound transaction. Network sends are isolated in channel_onboarding_notices.
Legacy bindings without this record retain explicit panel activation.
"""
from __future__ import annotations

import hmac
import json
import uuid
from typing import Literal

from pydantic import Field

from hub.collaboration.channel_consent import CONSENT_VERSION, OWNER_PURPOSES
from hub.collaboration.common import canonical, digest
from hub.collaboration.weixin_schema import Scope
from hub.room_scope import snapshot as project_snapshot
from shared.util import DevError

REVOKE_PATH = '协作中心 → 连接 → 对应通道 → 撤销任务授权或撤销绑定'


class Preview(Scope):
    idempotency_key: str = Field(default='', max_length=128)
    channel: Literal['weixin', 'telegram']
    account_id: str = Field(min_length=1, max_length=100)
    dot_id: str = Field(min_length=1, max_length=100)


class ChannelEnrollment:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        from hub.collaboration.channel_onboarding_notices import OnboardingNotices
        self.notices = OnboardingNotices(self)

    def transport(self, channel):
        if channel not in ('weixin', 'telegram'):
            raise DevError('CHANNEL_UNSUPPORTED', '不支持此通道', 422)
        return getattr(self.c, channel)

    def quarantine_input(self, channel, account, peer, external_id):
        # Called inside the inbound cursor transaction. An already observed
        # unbound message cannot become a new task after authorization, even
        # when a provider redelivers it under a different envelope identifier.
        self.store.execute('''INSERT OR IGNORE INTO channel_enrollment_inputs
            (channel,account_id,generation,peer_id,external_id,created) VALUES (?,?,?,?,?,?)''',
            (channel, account['id'], account['generation'], peer, str(external_id), self.c.clock()))

    def input_quarantined(self, channel, account, peer, external_id):
        return bool(self.store.one('''SELECT 1 FROM channel_enrollment_inputs
            WHERE channel=? AND account_id=? AND generation=? AND peer_id=? AND external_id=?''',
            (channel, account['id'], account['generation'], peer, str(external_id))))

    def evidence(self, channel, subject, principal, room, conversation, purposes, detail=None):
        if not purposes or any(value is not True for value in purposes.values()):
            raise DevError('CHANNEL_CONSENT_REQUIRED', '授权事项必须分别明确同意', 422)
        receipt = uuid.uuid4().hex
        for purpose in purposes:
            self.store.execute('''INSERT INTO channel_consent_evidence VALUES (?,?,?,?,?,?,?,?,?,1,?,?)''',
                (receipt, purpose, channel, principal.user_id, principal.space_id, room['id'], conversation,
                 subject, CONSENT_VERSION, canonical(detail or {}), self.c.clock()))
        return receipt

    def require_evidence(self, receipt, channel, subject, principal, room, conversation, purposes):
        rows = self.store.all('SELECT * FROM channel_consent_evidence WHERE receipt_id=?', (receipt,))
        approved = {row['purpose'] for row in rows if row['channel'] == channel
                    and row['subject_id'] == subject and row['owner_user_id'] == principal.user_id
                    and row['space_id'] == principal.space_id and row['room_id'] == room['id']
                    and row['conversation_id'] == conversation and row['text_version'] == CONSENT_VERSION
                    and row['approved'] == 1}
        if not set(purposes).issubset(approved):
            raise DevError('CHANNEL_CONSENT_REQUIRED', '原授权证据不完整或不属于此账号、项目和接入请求', 403)

    def snapshot(self, channel, account, dot, room, principal):
        self.transport(channel).require_enabled(principal)
        self.c.live_room(room)
        if (account['state'] != 'active' or account['owner_user_id'] != principal.user_id
                or account['space_id'] != principal.space_id or dot['room_id'] != room['id']):
            raise DevError('CHANNEL_ENROLLMENT_SCOPE', '接入账号或目标范围不匹配', 403)
        slot = self.c.dot_chat.authorize(principal, room, dot)
        actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
        self.c.dots.approved_owner(room, dot, actor)
        policy = self.c.dots.policy(room, dot)
        self.c.delegation.authorize_policy(actor, room, policy)
        spec = json.loads(policy['spec'])
        return {'channel': channel, 'owner_user_id': principal.user_id, 'space_id': principal.space_id,
                'account': {k: account[k] for k in ('id', 'bot_id', 'credential_version', 'generation')},
                'room_id': room['id'], 'conversation_id': dot['conversation_id'], 'project_id': room['project_id'],
                'environment_id': room['environment_id'],
                'project_snapshot': project_snapshot(self.c.runtime.project(room['project_id'], principal)),
                'dot_id': dot['id'], 'dot_setup_digest': dot['setup_digest'],
                'grant_id': slot['grant_id'], 'dot_principal': slot['principal'],
                'policy_id': policy['id'], 'policy_version': policy['version'], 'policy_digest': policy['digest'],
                'capabilities': spec['capabilities'], 'execution_targets': spec['execution_targets'],
                'target_snapshots': spec['target_snapshots'], 'max_delegations': spec['max_delegations'],
                'approval_digest': self.c.channel_owner.approval_digest(policy, dot, slot),
                'delivery_mode': 'full_mirror' if channel == 'telegram' else 'final_replies'}

    def preview(self, raw, principal):
        with self.store.transaction(immediate=False):
            # Both transports use exactly the same authenticated panel boundary.
            args, principal, room = self.c.weixin.owner(raw, principal, Preview, enabled=False)
            transport = self.transport(args['channel'])
            account = transport.account(args['account_id'], principal)
            dot = self.c.dots.find(args['dot_id'])
            if not dot or dot['conversation_id'] != args['conversation_id'] or dot['room_id'] != room['id']:
                raise DevError('CHANNEL_ENROLLMENT_SCOPE', '请选择此话题的已接入 dot', 403)
            scope = self.snapshot(args['channel'], account, dot, room, principal)
            return {'scope_digest': digest(scope), 'text_version': CONSENT_VERSION,
                    **{k: scope[k] for k in ('channel', 'project_id', 'environment_id', 'conversation_id', 'dot_id',
                       'policy_id', 'policy_version', 'capabilities', 'execution_targets', 'max_delegations', 'delivery_mode')},
                    'account_id': account['id'], 'bot_id': account['bot_id'], 'account_label': account['label'],
                    'lifetime_mode': 'until_revoked', 'budget_mode': 'lifetime', 'revoke_path': REVOKE_PATH,
                    'recipient_rule': '仅本人在所选机器人私聊发送此一次性授权码；该私聊成为授权联系人，勿转发授权码',
                    'result_destination': '原私聊及当前协作话题', 'host_approval_required': True,
                    'changes_underlying_grant': False, 'authorizes_file_egress': False}

    def prepare(self, channel, args, principal, room, account, dot):
        consent = args.get('owner_preapproval')
        if consent is None:
            return
        scope = self.snapshot(channel, account, dot, room, principal)
        if digest(scope) != consent['expected_scope_digest']:
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '展示后账号、项目、能力或执行目标已变化，请重新核对授权摘要', 409)
        if channel == 'weixin' and not args['allow_replies']:
            raise DevError('CHANNEL_OWNER_REPLIES_REQUIRED', '任务开通需要明确允许结果回传', 422)

    def record_binding(self, channel, args, principal, room, identifier):
        consent = args.get('owner_preapproval')
        if consent is None:
            return
        transport = self.transport(channel)
        binding = transport.binding(identifier, principal, room)
        account = transport.account(binding['account_id'], principal)
        dot = self.c.dots.find(binding['dot_id'])
        scope = self.snapshot(channel, account, dot, room, principal)
        scope['binding'] = {k: binding[k] for k in ('id', 'version', 'generation', 'credential_version',
                                                  'pairing_hash', 'pairing_until', 'expires_at', 'allow_replies',
                                                  'allow_media_outbound', 'mirror_enabled', 'disable_notification',
                                                  'lifetime_mode') if k in binding}
        receipt = self.evidence(channel, identifier, principal, room, binding['conversation_id'],
            {purpose: consent['confirm_' + purpose] for purpose in OWNER_PURPOSES} |
            {'external_data_ingress': args['confirm_external_data_ingress']},
            {'scope_digest': digest(scope), 'scope': scope, 'expected_peer_id': consent['expected_peer_id'],
             'recipient_rule': 'first_valid_private_code_holder_is_owner', 'revoke_path': REVOKE_PATH})
        self.store.execute('''INSERT INTO channel_enrollment_authorizations
            (binding_id,channel,receipt_id,state,scope,scope_digest,expected_peer_id,expires_at,created)
            VALUES (?,?,?,'pending',?,?,?,?,?)''',
            (identifier, channel, receipt, canonical(scope), digest(scope), consent['expected_peer_id'],
             binding['pairing_until'], self.c.clock()))
        self.c.audit(room, principal.actor, channel + '.owner_tasks_preapproved', identifier,
                     {'receipt_id': receipt, 'scope_digest': digest(scope), 'active': False, 'creates_grant': False})

    def current(self, identifier):
        return self.store.one('SELECT * FROM channel_enrollment_authorizations WHERE binding_id=?', (identifier,))

    def invalidate(self, identifier, reason):
        self.store.execute("UPDATE channel_enrollment_authorizations SET state='invalidated',reason_code=? WHERE binding_id=? AND state='pending'",
                           (reason, identifier))
        self.store.execute("UPDATE channel_onboarding_notices SET state='cancelled',reason_code=?,updated=? WHERE binding_id=? AND state IN ('queued','waiting_context')",
                           (reason, self.c.clock(), identifier))

    def validate_pending(self, channel, binding, owner, room, dot, peer=''):
        record = self.current(binding['id'])
        if not record or record['state'] != 'pending' or record['channel'] != channel:
            raise DevError('CHANNEL_PREAPPROVAL_INACTIVE', '没有本次接入的有效预授权', 403)
        if self.c.clock() >= record['expires_at'] or binding['state'] != 'pairing':
            raise DevError('CHANNEL_PREAPPROVAL_EXPIRED', '本次接入预授权已过期或停止', 403)
        scope = json.loads(record['scope'])
        current = self.snapshot(channel, self.transport(channel).account(binding['account_id'], owner), dot, room, owner)
        current['binding'] = {key: binding[key] for key in scope['binding']}
        if digest(scope) != record['scope_digest'] or current != scope:
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '原预授权账号、项目或任务范围已变化，不能自动开通', 403)
        self.require_evidence(record['receipt_id'], channel, binding['id'], owner, room, binding['conversation_id'], OWNER_PURPOSES)
        if peer and record['expected_peer_id'] and not hmac.compare_digest(peer.encode(), record['expected_peer_id'].encode()):
            raise DevError('CHANNEL_OWNER_PEER_CHANGED', '授权码必须由预先选定的本人联系人发送', 403)
        return record, scope

    def complete(self, channel, binding, owner, room, dot, peer, external_id):
        record, scope = self.validate_pending(channel, binding, owner, room, dot, peer)
        if channel == 'telegram':
            if self.store.one("SELECT 1 FROM telegram_bindings WHERE account_id=? AND user_id=? AND chat_id=? AND state='active' AND id!=?",
                              (binding['account_id'], peer, peer, binding['id'])):
                raise DevError('TELEGRAM_ROUTE_CONFLICT', '此私聊已经连接其他协作话题', 409)
            self.store.execute("UPDATE telegram_bindings SET state='active',user_id=?,chat_id=?,pending_user_id='',pending_chat_id='',pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?",
                               (peer, peer, self.c.clock(), binding['id']))
        else:
            self.store.execute("UPDATE weixin_bindings SET state='active',peer_id=?,pending_peer_id='',pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?",
                               (peer, self.c.clock(), binding['id']))
        result = self.c.channel_owner.control({
            'project': binding['project_id'], 'environment_id': binding['environment_id'],
            'conversation_id': binding['conversation_id'], 'binding_id': binding['id'],
            'expected_binding_version': binding['version'] + 1, 'expected_authority_version': 0,
            'action': 'approve', 'confirm_peer_id': peer, 'confirm_owner_tasks': True,
            'confirm_until_revoked': True, 'expected_scope_digest': scope['approval_digest'],
            'idempotency_key': 'enrollment-owner-' + record['receipt_id']}, owner, channel=channel)
        self.store.execute("UPDATE channel_enrollment_authorizations SET state='completed',completed_at=? WHERE binding_id=? AND state='pending'",
                           (self.c.clock(), binding['id']))
        current = self.transport(channel).binding(binding['id'], owner, room)
        value = ('已连接，可在此私聊交办任务。\n项目：' + scope['project_id'] + ' / ' + scope['environment_id']
                 + '\n允许能力：' + '、'.join(scope['capabilities']) + '\n执行目标：' + '、'.join(scope['execution_targets'])
                 + '\n沿用原任务数量上限：' + str(scope['max_delegations'])
                 + '；每项任务限制和宿主必要确认仍有效，文件需单独批准。\n进度与结果回到此私聊及原协作话题。'
                 + '\n授权持续至撤销。撤销位置：' + REVOKE_PATH)
        self.notices.queue(channel, current, peer, external_id, 'connected', value,
                           authorization_version=result['owner_authorization']['version'])
        self.c.audit(room, 'private-code-proof:' + channel, channel + '.onboarding_completed', binding['id'],
                     {'receipt_id': record['receipt_id'], 'scope_digest': record['scope_digest'],
                      'peer_digest': digest(peer), 'historical_execution': False, 'receipt_state': 'queued'})

    def incoming(self, channel, binding, account, owner, room, dot, peer, external_id, *, matched, context='', fresh=True):
        record = self.current(binding['id'])
        if not record or record['state'] != 'pending':
            return False
        # Legacy/manual pairing remains available, but only a current pending
        # record can automatically activate any task authority.
        try:
            self.validate_pending(channel, binding, owner, room, dot, peer)
        except DevError as exc:
            if exc.code != 'CHANNEL_OWNER_PEER_CHANGED':
                self.invalidate(binding['id'], exc.code)
            return True
        if not fresh:
            return True
        if channel == 'weixin' and context:
            from hub.collaboration.weixin_protocol import text
            text(context, 65536)
            self.store.execute('''INSERT INTO weixin_contexts(account_id,generation,peer_id,token,observed_at)
                VALUES (?,?,?,?,?) ON CONFLICT(account_id,generation,peer_id) DO UPDATE SET
                token=excluded.token,observed_at=excluded.observed_at,version=version+1''',
                (account['id'], binding['generation'], peer, self.store.encrypt(context), self.c.clock()))
        if matched:
            # Failure here must roll back the entire inbox/cursor transaction.
            self.complete(channel, binding, owner, room, dot, peer, external_id)
        else:
            self.notices.queue(channel, binding, peer, external_id, 'pairing_required',
                '尚未完成授权验证，此消息不会执行任务。请仅在本人与此机器人的私聊发送面板显示的一次性授权码；不要转发授权码。')
        return True

    def view(self, binding_id):
        record = self.current(binding_id)
        if not record:
            return None
        state = record['state']
        if state == 'pending' and record['expires_at'] <= self.c.clock():
            state = 'expired'
        notices = self.store.all('SELECT kind,state,reason_code FROM channel_onboarding_notices WHERE binding_id=? ORDER BY created DESC LIMIT 8', (binding_id,))
        return {'state': state, 'receipt_id': record['receipt_id'], 'expires_at': record['expires_at'],
                'reason_code': record['reason_code'], 'auto_complete': state == 'pending',
                'notice_state': notices[0]['state'] if notices else 'waiting_inbound',
                'notices': notices, 'delivered_verified': False, 'read_verified': False}
