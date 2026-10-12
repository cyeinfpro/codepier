"""Explicit, revocable owner identity for one WeChat task channel.

Pairing alone never authenticates a room owner. Only the authenticated panel
owner can add this record. Existing messages, grants and scopes are unchanged.
"""
from __future__ import annotations

import json
import hmac
import uuid
from typing import Literal

from pydantic import Field, model_validator

from hub.collaboration.common import canonical, digest
from hub.collaboration.weixin_schema import Mutation
from shared.util import DevError


class OwnerAuthority(Mutation):
    binding_id: str = Field(min_length=1, max_length=100)
    expected_binding_version: int = Field(ge=1)
    expected_authority_version: int = Field(default=0, ge=0)
    action: Literal['approve', 'revoke']
    confirm_peer_id: str = Field(default='', max_length=256)
    confirm_owner_tasks: bool = False
    confirm_until_revoked: bool = False
    budget_mode: Literal['lifetime', 'rolling_24h'] = 'lifetime'
    confirm_daily_budget: bool = False
    expected_scope_digest: str = Field(default='', max_length=64)

    @model_validator(mode='after')
    def explicit_consent(self):
        if self.action == 'approve':
            if not self.confirm_owner_tasks or not self.confirm_until_revoked or not self.confirm_peer_id:
                raise ValueError('Explicit owner identity, task scope and until-revoked consent are required')
            if self.budget_mode == 'rolling_24h' and not self.confirm_daily_budget:
                raise ValueError('A recurring daily task budget requires explicit approval')
        return self


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS channel_owner_authorizations(
        id TEXT PRIMARY KEY,binding_id TEXT NOT NULL UNIQUE,version INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL,owner_user_id TEXT NOT NULL,space_id TEXT NOT NULL,
        room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,dot_id TEXT NOT NULL,policy_id TEXT NOT NULL,
        policy_version INTEGER NOT NULL,scope TEXT NOT NULL,scope_digest TEXT NOT NULL,
        first_sequence INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS channel_owner_messages(
        message_id TEXT PRIMARY KEY,authorization_id TEXT NOT NULL,authorization_version INTEGER NOT NULL,
        message_digest TEXT NOT NULL,created REAL NOT NULL)""")
    for table in ('weixin_bindings', 'collaboration_join_slots', 'delegation_policies'):
        columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
        if 'lifetime_mode' not in columns:
            db.execute("ALTER TABLE " + table + " ADD COLUMN lifetime_mode TEXT NOT NULL DEFAULT 'timed'")
        if table != 'weixin_bindings' and 'lifetime_authorization_id' not in columns:
            db.execute("ALTER TABLE " + table + " ADD COLUMN lifetime_authorization_id TEXT NOT NULL DEFAULT ''")


class ChannelOwnerAuthority:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def current(self, binding_id):
        return self.store.one('SELECT * FROM channel_owner_authorizations WHERE binding_id=?', (binding_id,))

    def persistent(self, row):
        if row.get('lifetime_mode') != 'until_revoked':
            return False
        identifier = row.get('lifetime_authorization_id', '')
        if identifier.startswith('weixin-binding:'):
            binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (identifier.split(':', 1)[1],))
            return bool(binding and row['id'] == binding['dot_id'] and binding['state'] != 'revoked'
                        and binding['lifetime_mode'] == 'until_revoked' and row.get('principal') == binding['dot_principal'])
        if not identifier:
            return False
        auth = self.store.one("SELECT * FROM channel_owner_authorizations WHERE id=? AND state IN ('active','pending_revalidation')", (identifier,))
        if not auth:
            return False
        scope = json.loads(auth['scope'])
        if digest(scope) != auth['scope_digest']:
            return False
        if row['id'] not in (auth['dot_id'], auth['policy_id']):
            return False
        if row['id'] == auth['policy_id'] and (row['version'] != auth['policy_version'] or row['digest'] != scope['policy_digest']):
            return False
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (auth['binding_id'],))
        return bool(binding and binding['state'] != 'revoked' and (auth['state'] == 'pending_revalidation' or binding['generation'] == scope['generation'])
                    and binding['account_id'] == scope['account_id'] and binding['peer_id'] == scope['peer_id'])

    def expired(self, row):
        return row['expires_at'] <= self.c.clock() and not self.persistent(row)

    def view(self, auth):
        if not auth:
            return None
        scope = json.loads(auth['scope'])
        return {k: auth[k] for k in ('id', 'version', 'state', 'policy_id', 'created')} | {
            'lifetime_mode': 'until_revoked', 'project_id': scope['project_id'],
            'capabilities': scope['capabilities'], 'execution_targets': scope['execution_targets'],
            'budget_mode': scope['budget_mode'], 'max_delegations': scope['max_delegations'],
            'host_approval_required': True, 'changes_underlying_grant': False}

    def approval_digest(self, policy, dot, slot):
        return digest([policy['id'], policy['version'], policy['digest'], dot['setup_digest'], slot['principal']])

    def options(self, binding):
        try:
            if not binding['allow_replies']:
                raise DevError('CHANNEL_OWNER_REPLIES_REQUIRED', '原绑定尚未批准回复，请明确重新建立允许回复的绑定', 409)
            dot = self.c.dots.find(binding['dot_id'])
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (binding['room_id'],))
            policy = self.c.dots.policy(room, dot)
            spec = json.loads(policy['spec'])
            return {'binding_id': binding['id'], 'available': binding['state'] == 'active',
                    'policy_id': policy['id'], 'policy_version': policy['version'],
                    'scope_digest': self.approval_digest(policy, dot, self.c.joining.slot(room, dot['id'])),
                    'project_id': binding['project_id'], 'capabilities': spec['capabilities'],
                    'execution_targets': spec['execution_targets'], 'max_delegations': spec['max_delegations']}
        except (DevError, TypeError, KeyError) as exc:
            return {'binding_id': binding['id'], 'available': False,
                    'reason_code': exc.code if isinstance(exc, DevError) else 'DOT_JOIN_REQUIRED'}

    def control(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.c.weixin.owner(raw, principal, OwnerAuthority, enabled=False)
            binding = self.c.weixin.binding(args['binding_id'], principal, room)
            def save():
                if binding['version'] != args['expected_binding_version']:
                    raise DevError('STALE_VERSION', '微信绑定已改变，请重新核对本人和范围', 409)
                old = self.current(binding['id'])
                if (old['version'] if old else 0) != args['expected_authority_version']:
                    raise DevError('STALE_VERSION', '微信本人任务授权已改变', 409)
                if args['action'] == 'revoke':
                    if not old:
                        raise DevError('CHANNEL_OWNER_NOT_APPROVED', '没有可撤销的本人任务授权', 409)
                    self.revoke(binding['id'], 'owner_revoked')
                    return {'authorization_id': old['id']}
                if old and old['state'] == 'active':
                    raise DevError('CHANNEL_OWNER_ALREADY_APPROVED', '现有授权有效；更改范围需先明确撤销', 409)
                if binding['state'] != 'active' or not hmac.compare_digest(binding['peer_id'].encode(), args['confirm_peer_id'].encode()):
                    raise DevError('WEIXIN_PAIRING_UNVERIFIED', '必须核对原绑定的真实本人私聊编号', 409)
                if not binding['allow_replies']:
                    raise DevError('CHANNEL_OWNER_REPLIES_REQUIRED', '原绑定尚未批准回复，请明确重新建立允许回复的绑定', 409)
                self.c.weixin.require_enabled(principal)
                dot = self.c.dots.find(binding['dot_id'])
                slot = self.c.joining.slot(room, binding['dot_id'])
                if not self.c.dot_chat.enabled(dot) or slot['state'] != 'registered':
                    raise DevError('DOT_JOIN_REQUIRED', '本人任务只使用原已加入的双向 dot', 409)
                actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
                self.c.dots.approved_owner(room, dot, actor)
                policy = self.c.dots.policy(room, dot)
                if policy['state'] != 'active':
                    raise DevError('DELEGATION_POLICY_INACTIVE', '不能恢复已暂停或撤销的任务范围', 409)
                if self.approval_digest(policy, dot, slot) != args['expected_scope_digest']:
                    raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '显示的项目、能力或目标范围已变化，请重新审阅', 409)
                spec = json.loads(policy['spec'])
                account = self.c.weixin.account(binding['account_id'], principal)
                if (account['state'] != 'active' or account['generation'] != binding['generation']
                        or account['credential_version'] != binding['credential_version']):
                    raise DevError('WEIXIN_CREDENTIAL_CHANGED', '原账号凭据或世代已改变，需重新绑定', 403)
                identifier, now = old['id'] if old else uuid.uuid4().hex, self.c.clock()
                scope = {'account_id': account['id'], 'peer_id': binding['peer_id'],
                         'credential_version': binding['credential_version'], 'generation': binding['generation'],
                         'project_id': binding['project_id'], 'environment_id': binding['environment_id'],
                         'project_snapshot': json.loads(binding['project_snapshot']),
                         'dot_setup_digest': dot['setup_digest'], 'grant_id': slot['grant_id'],
                         'policy_digest': policy['digest'], 'capabilities': spec['capabilities'],
                         'execution_targets': spec['execution_targets'], 'target_snapshots': spec['target_snapshots'],
                         'budget_mode': args['budget_mode'], 'max_delegations': spec['max_delegations']}
                sequence = self.store.one('SELECT COALESCE(MAX(server_sequence),0) AS n FROM collaboration_messages WHERE room_id=?', (room['id'],))['n']
                version = (old['version'] if old else 0) + 1
                self.store.execute("""INSERT INTO channel_owner_authorizations
                    (id,binding_id,version,state,owner_user_id,space_id,room_id,conversation_id,dot_id,policy_id,
                     policy_version,scope,scope_digest,first_sequence,created,updated)
                    VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET
                    version=excluded.version,state='active',policy_id=excluded.policy_id,policy_version=excluded.policy_version,
                    scope=excluded.scope,scope_digest=excluded.scope_digest,first_sequence=excluded.first_sequence,
                    created=excluded.created,updated=excluded.updated""",
                    (identifier, binding['id'], version, principal.user_id, principal.space_id, room['id'],
                     binding['conversation_id'], dot['id'], policy['id'], policy['version'],
                     canonical(scope), digest(scope), sequence, now, now))
                self.store.execute("UPDATE weixin_bindings SET lifetime_mode='until_revoked',version=version+1,updated=? WHERE id=?", (now, binding['id']))
                for table, key in (('collaboration_join_slots', dot['id']), ('delegation_policies', policy['id'])):
                    self.store.execute("UPDATE " + table + " SET lifetime_mode='until_revoked',lifetime_authorization_id=? WHERE id=?", (identifier, key))
                # Full existing validation happens after the timing-only transition,
                # inside this transaction: any changed identity/scope rolls it back.
                self.c.weixin.valid(binding['id'])
                self.c.delegation.authorize_policy(actor, room, self.c.delegation.policy(room, policy['id']))
                self.c.audit(room, principal.actor, 'weixin.owner_tasks_approved', identifier,
                             {'binding_id': binding['id'], 'scope_digest': digest(scope),
                              'lifetime_mode': 'until_revoked', 'budget_mode': args['budget_mode'],
                              'creates_grant': False, 'historical_execution': False})
                return {'authorization_id': identifier}
            receipt = self.c.mutation(principal, room, 'weixin_owner_authority', args, save)
            auth = self.store.one('SELECT * FROM channel_owner_authorizations WHERE id=?', (receipt['authorization_id'],))
            return {'owner_authorization': self.view(auth), 'binding_id': binding['id'],
                    'binding_version': self.c.weixin.binding(binding['id'], principal, room)['version']}

    def revoke(self, binding_id, reason):
        auth = self.current(binding_id)
        if not auth or auth['state'] == 'revoked':
            return
        self.store.execute("UPDATE channel_owner_authorizations SET state='revoked',version=version+1,updated=? WHERE id=?", (self.c.clock(), auth['id']))
        for table in ('collaboration_join_slots', 'delegation_policies'):
            self.store.execute("UPDATE " + table + " SET lifetime_mode='timed',lifetime_authorization_id='' WHERE lifetime_authorization_id=?", (auth['id'],))
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (binding_id,))
        if binding and binding['lifetime_mode'] == 'until_revoked' and binding['state'] != 'revoked':
            self.store.execute("UPDATE collaboration_join_slots SET lifetime_mode='until_revoked',lifetime_authorization_id=? WHERE id=?",
                               ('weixin-binding:' + binding_id, binding['dot_id']))
        # An already-started operation has its own bounded cancellation protocol.
        # Stop new steps immediately; never claim this kills the OS process.
        links = self.store.all("""SELECT d.* FROM delegation_requests d JOIN channel_owner_messages m ON m.message_id=d.message_id
            WHERE m.authorization_id=?""", (auth['id'],))
        for link in links:
            self.store.execute("""UPDATE coordination_work SET state='blocked',lease_until=NULL,updated=?
                WHERE id=? AND state NOT IN ('succeeded','failed','cancelled')""", (self.c.clock(), link['work_item_id']))
        # Only definitely-unsent parts can be terminalized automatically.
        # Sending/unknown retain their ambiguity and are never resent.
        unsent = self.store.all("""SELECT p.id,p.delivery_id FROM weixin_parts p
            JOIN weixin_deliveries d ON d.id=p.delivery_id
            JOIN channel_owner_messages m ON m.message_id=d.source_message_id
            WHERE m.authorization_id=? AND p.state IN ('queued','waiting_context','waiting_media','preparing')""", (auth['id'],))
        for part in unsent:
            self.store.execute("UPDATE weixin_parts SET state='cancelled',reason_code='CHANNEL_OWNER_REVOKED',lease_fence=lease_fence+1,lease_until=0,updated=? WHERE id=?",
                               (self.c.clock(), part['id']))
            self.c.weixin.delivery.refresh(part['delivery_id'])
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (auth['room_id'],))
        self.c.audit(room, 'panel-owner:' + auth['owner_user_id'], 'weixin.owner_tasks_revoked', auth['id'], {'reason': reason})

    def pending_revalidation(self, binding_id):
        auth = self.current(binding_id)
        if auth and auth['state'] == 'active':
            self.store.execute("UPDATE channel_owner_authorizations SET state='pending_revalidation',version=version+1,updated=? WHERE id=?",
                               (self.c.clock(), auth['id']))

    def restore_verified_peer(self, binding):
        auth = self.current(binding['id'])
        if not auth or auth['state'] != 'pending_revalidation':
            return
        scope = json.loads(auth['scope'])
        if not hmac.compare_digest(scope['peer_id'].encode(), binding['peer_id'].encode()):
            raise DevError('CHANNEL_OWNER_PEER_CHANGED', '必须由原本人联系人重新配对；不同联系人不能继承原授权', 403)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (auth['room_id'],))
        dot = self.c.dots.find(auth['dot_id'])
        policy = self.c.dots.policy(room, dot)
        if (digest(scope) != auth['scope_digest'] or dot['setup_digest'] != scope['dot_setup_digest']
                or policy['id'] != auth['policy_id'] or policy['version'] != auth['policy_version']
                or policy['digest'] != scope['policy_digest']):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '原本人授权范围已变化，不能自动恢复', 403)
        scope.update(credential_version=binding['credential_version'], generation=binding['generation'])
        now = self.c.clock()
        sequence = self.store.one('SELECT COALESCE(MAX(server_sequence),0) AS n FROM collaboration_messages WHERE room_id=?', (room['id'],))['n']
        self.store.execute("UPDATE channel_owner_authorizations SET state='active',version=version+1,scope=?,scope_digest=?,first_sequence=?,created=?,updated=? WHERE id=?",
                           (canonical(scope), digest(scope), sequence, now, now, auth['id']))
        actor = self.c.grant_reader(room, scope['grant_id'])
        self.c.dots.approved_owner(room, dot, actor)
        self.c.delegation.authorize_policy(actor, room, policy)

    def require_reply_source(self, source):
        if source and self.store.one('SELECT 1 FROM channel_owner_messages WHERE message_id=?', (source['id'],)):
            self.trusted(source)

    def attest(self, message):
        body = json.loads(message['body'])
        origin = body.get('external_origin', {})
        auth = self.current(origin.get('binding_id', ''))
        if not auth or auth['state'] != 'active':
            return
        # Old backlog/revisions/deletions never become a fresh execution request.
        created = origin.get('provider_created')
        if (not created or int(created) < int(auth['created'] * 1000)
                or message['server_sequence'] <= auth['first_sequence']
                or origin.get('provider_deleted') or not origin.get('revision_is_latest_at_commit')
                or origin.get('supersedes_message_id') or origin.get('related_current_message_id')):
            return
        self.store.execute("""INSERT OR IGNORE INTO channel_owner_messages
            (message_id,authorization_id,authorization_version,message_digest,created) VALUES (?,?,?,?,?)""",
            (message['id'], auth['id'], auth['version'], digest(body['body_text']), self.c.clock()))
        try:
            self.trusted(message)
        except DevError:
            self.store.execute('DELETE FROM channel_owner_messages WHERE message_id=?', (message['id'],))

    def trusted(self, message):
        evidence = self.store.one('SELECT * FROM channel_owner_messages WHERE message_id=?', (message['id'],))
        if not evidence:
            raise DevError('CHANNEL_OWNER_NOT_APPROVED', '此消息没有已验证的本人任务授权', 403)
        auth = self.store.one('SELECT * FROM channel_owner_authorizations WHERE id=?', (evidence['authorization_id'],))
        if not auth or auth['state'] != 'active' or auth['version'] != evidence['authorization_version']:
            raise DevError('CHANNEL_OWNER_REVOKED', '原微信本人任务授权已撤销或更改', 403)
        scope, body = json.loads(auth['scope']), json.loads(message['body'])
        origin = body.get('external_origin', {})
        if (digest(scope) != auth['scope_digest'] or message['origin'] != 'external_channel'
                or origin.get('channel') != 'weixin' or message['room_id'] != auth['room_id']
                or message['conversation_id'] != auth['conversation_id']
                or message['author'] != 'weixin:' + scope['account_id'] + ':' + digest(scope['peer_id'])[:20]
                or origin.get('binding_id') != auth['binding_id'] or origin.get('account_id') != scope['account_id']
                or origin.get('generation') != scope['generation'] or origin.get('peer_id') != scope['peer_id']
                or digest(body['body_text']) != evidence['message_digest']):
            raise DevError('CHANNEL_OWNER_SOURCE_CHANGED', '原微信消息来源或正文已变化', 409)
        binding, account, owner, room, dot = self.c.weixin.valid(auth['binding_id'])
        if (binding['peer_id'] != scope['peer_id'] or binding['account_id'] != scope['account_id']
                or binding['generation'] != scope['generation'] or binding['dot_id'] != auth['dot_id']
                or binding['room_id'] != auth['room_id'] or room['id'] != auth['room_id']
                or binding['conversation_id'] != auth['conversation_id']
                or binding['project_id'] != scope['project_id'] or binding['environment_id'] != scope['environment_id']
                or json.loads(binding['project_snapshot']) != scope['project_snapshot']
                or owner.user_id != auth['owner_user_id'] or owner.space_id != auth['space_id']
                or dot['setup_digest'] != scope['dot_setup_digest'] or dot['id'] != auth['dot_id']
                or account['credential_version'] != scope['credential_version']
                or account['generation'] != scope['generation']):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '原本人任务身份或范围已变化', 403)
        current = self.store.one("""SELECT message_id FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?""",
            (scope['account_id'], scope['generation'], scope['peer_id'], origin['external_message_id']))
        # During the atomic ingest transaction the inbound ledger is committed
        # immediately after attestation; later callers always see the latest ID.
        if current and current['message_id'] != message['id']:
            raise DevError('CHANNEL_OWNER_SOURCE_SUPERSEDED', '原微信请求已被修改或删除，请重新明确发消息', 409)
        policy = self.c.dots.policy(room, dot)
        if (policy['id'] != auth['policy_id'] or policy['version'] != auth['policy_version']
                or policy['digest'] != scope['policy_digest']):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED', '原本人任务委托范围已改变', 403)
        return {'kind': 'verified_external_owner', 'authenticated': True, 'user_id': owner.user_id,
                'channel': 'weixin', 'authorization_id': auth['id'], 'authorization_version': auth['version']}

    def author(self, message):
        if message['origin'] == 'panel_owner':
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (message['room_id'],))
            if message['author'] == room['owner_user_id']:
                return {'kind': 'panel_owner', 'authenticated': True}
        if message['origin'] == 'external_channel':
            try:
                return self.trusted(message)
            except DevError:
                return {'kind': 'external_channel', 'authenticated': False}
        return {'kind': 'unverified', 'authenticated': False}

    def budget_mode(self, policy):
        if self.persistent(policy):
            auth = self.store.one('SELECT * FROM channel_owner_authorizations WHERE id=?', (policy['lifetime_authorization_id'],))
            return json.loads(auth['scope'])['budget_mode']
        return 'lifetime'

    def budget_used(self, policy):
        if self.budget_mode(policy) == 'rolling_24h':
            return self.store.one('SELECT COUNT(*) AS n FROM delegation_requests WHERE policy_id=? AND created>?',
                                  (policy['id'], self.c.clock() - 86400))['n']
        return policy['uses']
