"""Recoverable room-facing WeChat enrollment over the existing consent ledgers.

This coordinator stores identity and step receipts, never provider credentials or
execution grants. A QR login, a verified peer and owner task consent remain
separate facts. Network calls use the original QR operation and request key.
"""
from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import Field
from shared.collaboration_contracts import Model, Key
from shared.room_runtime_contracts import ChannelConnect, ChannelAction
from hub.collaboration.common import canonical, digest, validate
from shared.util import DevError


class LegacyChannelPause(Model):
    channel: Literal['weixin','telegram']
    expected_version: int = Field(strict=True,ge=1)
    idempotency_key: Key


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_channel_flows(
        id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        owner_user_id TEXT NOT NULL,space_id TEXT NOT NULL,member_id TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',environment_id TEXT NOT NULL DEFAULT 'production',
        account_id TEXT NOT NULL DEFAULT '',enrollment_id TEXT NOT NULL DEFAULT '',
        binding_id TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'active',
        version INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,updated REAL NOT NULL)''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_channel_flow_active ON room_channel_flows(conversation_id) WHERE state='active'")
    db.execute('''CREATE TABLE IF NOT EXISTS room_runtime_receipts(
        owner_user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,action TEXT NOT NULL,
        request_key TEXT NOT NULL,fingerprint TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(owner_user_id,conversation_id,action,request_key))''')
    from hub.collaboration.room_weixin_native import migrate as migrate_native
    migrate_native(db)


class RoomChannels:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        from hub.collaboration.room_weixin_native import RoomWeixin
        self.native = RoomWeixin(self)

    def owner(self, principal, conversation_id):
        return self.c.rooms.owner(principal, conversation_id)

    def get(self, identifier, principal):
        row = self.store.one('SELECT * FROM room_channel_flows WHERE id=?', (identifier,))
        if not row:
            raise DevError('CONNECT_FLOW_NOT_FOUND', '没有这个微信接入流程', 404)
        principal, conversation = self.owner(principal, row['conversation_id'])
        if row['owner_user_id'] != principal.user_id or row['space_id'] != principal.space_id:
            raise DevError('CONNECT_FLOW_NOT_FOUND', '没有这个微信接入流程', 404)
        return row, principal, conversation

    def scope(self, row):
        if not row['project_id']:
            raise DevError('CHANNEL_PROJECT_REQUIRED', '先为此房间关联一个项目，再继续微信接入', 409)
        return {'project': row['project_id'], 'environment_id': row['environment_id'],
                'conversation_id': row['conversation_id']}

    def receipt(self, principal, conversation, action, args, perform):
        fingerprint = digest({k: v for k, v in args.items() if k != 'idempotency_key'})
        identity = (principal.user_id, conversation['id'], action, args['idempotency_key'])
        prior = self.store.one('''SELECT * FROM room_runtime_receipts
            WHERE owner_user_id=? AND conversation_id=? AND action=? AND request_key=?''', identity)
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '同一请求标识不能用于另一项操作', 409)
            return json.loads(prior['result'])
        result = perform()
        self.store.execute('INSERT INTO room_runtime_receipts VALUES (?,?,?,?,?,?,?)',
                           (*identity, fingerprint, canonical(result), self.c.clock()))
        return result

    def create(self, raw, principal):
        args = validate(ChannelConnect, raw)
        with self.store.transaction():
            principal, conversation = self.owner(principal, args['conversation_id'])
            self.c.weixin.require_enabled(principal)
            def save():
                self.c.rooms.require_native(conversation)
                current = self.store.one("SELECT * FROM room_channel_flows WHERE conversation_id=? AND state='active'", (conversation['id'],))
                if current:
                    if args['account_id'] and current['account_id'] and args['account_id'] != current['account_id']:
                        raise DevError('CHANNEL_FLOW_CONFLICT', '此房间已有另一个微信接入流程，请先完成或取消', 409)
                    return {'flow_id': current['id']}
                member = self.member(conversation, args['member_id'], required=False)
                anchor = self.store.one('''SELECT r.project_id,r.environment_id FROM conversation_projects p
                    JOIN collaboration_rooms r ON r.id=p.room_id WHERE p.conversation_id=?
                    ORDER BY p.created,p.room_id LIMIT 1''', (conversation['id'],))
                if args['account_id']:
                    self.c.weixin.account(args['account_id'], principal)
                now, identifier = self.c.clock(), uuid.uuid4().hex
                self.store.execute('''INSERT INTO room_channel_flows
                    (id,conversation_id,owner_user_id,space_id,member_id,project_id,environment_id,account_id,created,updated,native)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, conversation['id'], principal.user_id, principal.space_id,
                     member['id'] if member else '', anchor['project_id'] if anchor else '',
                     anchor['environment_id'] if anchor else 'production', args['account_id'], now, now,
                     int(not member or member['status']!='legacy')))
                return {'flow_id': identifier}
            result = self.receipt(principal, conversation, 'channel_create', args, save)
            return self.read(result['flow_id'], principal)

    def legacy_stop_scope(self,channel,raw,principal,model,binding_id):
        """Private safe-stop authorization; this grants no project access."""
        args=validate(model,raw)
        if channel not in {'weixin','telegram'} or args.get('action') not in {'pause','revoke'} or args.get('binding_id')!=binding_id:
            raise DevError('LEGACY_CHANNEL_STOP_REQUIRED','此内部入口仅允许精确暂停或撤销原渠道',403)
        principal,conversation=self.c.rooms.owner(principal,args['conversation_id'],include_archived=True)
        binding=self.store.one('SELECT * FROM '+channel+'_bindings WHERE id=? AND conversation_id=? AND owner_user_id=? AND space_id=?',
            (binding_id,conversation['id'],principal.user_id,principal.space_id))
        if (not binding or channel=='weixin' and binding.get('native_member_id')
                or args['project']!=binding['project_id'] or args['environment_id']!=binding['environment_id']):
            raise DevError('LEGACY_CHANNEL_NOT_FOUND','此房间没有这条旧渠道连接',404)
        partition=self.store.one('''SELECT * FROM collaboration_rooms WHERE id=? AND owner_user_id=? AND space_id=?
            AND project_id=? AND environment_id=?''',(binding['room_id'],principal.user_id,principal.space_id,
                binding['project_id'],binding['environment_id']))
        if not partition:
            raise DevError('LEGACY_CHANNEL_NOT_FOUND','原渠道的历史分区记录不存在',404)
        return args,principal,partition,binding

    def stop_legacy(self,conversation_id,binding_id,channel,action,expected_version,request_key,principal):
        self.store.require_transaction()
        principal,conversation=self.c.rooms.owner(principal,conversation_id,include_archived=True)
        if channel not in {'weixin','telegram'} or action not in {'pause','revoke'}:
            raise DevError('LEGACY_CHANNEL_STOP_REQUIRED','仅允许安全暂停或撤销旧渠道',403)
        binding=self.store.one('SELECT * FROM '+channel+'_bindings WHERE id=? AND conversation_id=? AND owner_user_id=? AND space_id=?',
            (binding_id,conversation_id,principal.user_id,principal.space_id))
        if not binding or channel=='weixin' and binding.get('native_member_id'):
            raise DevError('LEGACY_CHANNEL_NOT_FOUND','此房间没有这条旧渠道连接',404)
        result=getattr(self.c,channel).control({'project':binding['project_id'],
            'environment_id':binding['environment_id'],'conversation_id':conversation_id,
            'binding_id':binding_id,'expected_version':expected_version,'action':action,'idempotency_key':request_key},
            principal,room_owner_binding=binding_id)
        # Stop only original Goals whose source belongs to this exact channel.
        # A private adapter or unrelated work on the same project is not enough.
        goals=self.store.all('''SELECT DISTINCT g.* FROM coordination_goals g
            JOIN delegation_requests d ON d.goal_id=g.id JOIN collaboration_messages m ON m.id=d.message_id
            LEFT JOIN collaboration_messages parent ON parent.id=m.reply_to_id
            LEFT JOIN channel_owner_messages proof ON proof.message_id=m.id
            LEFT JOIN channel_owner_authorizations a ON a.id=proof.authorization_id
            WHERE g.conversation_id=? AND (a.binding_id=?
                OR json_extract(m.body,'$.external_origin.binding_id')=?
                OR json_extract(parent.body,'$.external_origin.binding_id')=?)''',
            (conversation_id,binding_id,binding_id,binding_id))
        for goal in goals:
            if goal['state'] in {'active','paused'}:
                self.c.coordination.stop_work(goal,'ROOM_LEGACY_CHANNEL_STOPPED')
                self.store.execute("UPDATE coordination_goals SET state='paused',version=version+1,updated=? WHERE id=? AND state='active'",
                    (self.c.clock(),goal['id']))
        self.c.channel_owner.revoke(binding_id,'room_migration_'+action)
        return result

    def pause_legacy(self,conversation_id,binding_id,raw,principal):
        """Exact owner-only stop for migrated history that has no new flow."""
        args=validate(LegacyChannelPause,raw)
        with self.store.transaction():
            principal,conversation=self.c.rooms.owner(principal,conversation_id,include_archived=True)
            table=args['channel']+'_bindings'
            binding=self.store.one('SELECT * FROM '+table+' WHERE id=? AND conversation_id=? AND owner_user_id=? AND space_id=?',
                (binding_id,conversation_id,principal.user_id,principal.space_id))
            if not binding or args['channel']=='weixin' and binding.get('native_member_id'):
                raise DevError('LEGACY_CHANNEL_NOT_FOUND','此房间没有这条旧渠道连接',404)
            def stop():
                result=self.stop_legacy(conversation_id,binding_id,args['channel'],'pause',args['expected_version'],
                    args['idempotency_key'],principal)
                current=self.store.one('SELECT version FROM '+table+' WHERE id=?',(binding_id,))
                self.c.rooms.change(conversation_id,'channel_binding',binding_id,current['version'])
                return result
            self.receipt(principal,conversation,'legacy_channel_pause:'+args['channel']+':'+binding_id,args,stop)
            current=self.store.one('SELECT id,state,version FROM '+table+' WHERE id=?',(binding_id,))
            return {'conversation_id':conversation_id,'binding':{**current,'channel':args['channel']},
                'paused':current['state']=='paused','automatic_replay':False}

    def cancel_legacy_login(self,flow,principal):
        """Close only this owner's original QR without reviving project access."""
        self.store.require_transaction()
        principal,conversation=self.c.rooms.owner(principal,flow['conversation_id'],include_archived=True)
        current=self.store.one('''SELECT * FROM room_channel_flows WHERE id=? AND conversation_id=?
            AND owner_user_id=? AND space_id=?''',
            (flow['id'],conversation['id'],principal.user_id,principal.space_id))
        if not current or current['native'] or current['enrollment_id']!=flow['enrollment_id']:
            raise DevError('CONNECT_FLOW_NOT_FOUND','此房间没有这条原扫码流程',404)
        partition=self.store.one('''SELECT r.* FROM collaboration_rooms r JOIN weixin_enrollments e ON e.room_id=r.id
            WHERE e.id=? AND e.conversation_id=? AND e.owner_user_id=? AND e.space_id=?
            AND r.owner_user_id=? AND r.space_id=? AND r.project_id=? AND r.environment_id=?''',
            (current['enrollment_id'],conversation['id'],principal.user_id,principal.space_id,
             principal.user_id,principal.space_id,current['project_id'],current['environment_id']))
        if not partition:
            raise DevError('WEIXIN_ENROLLMENT_NOT_FOUND','当前房主与话题没有此原扫码请求',404)
        enrollment=self.c.weixin.qr.row(current['enrollment_id'],principal,partition,conversation['id'])
        return self.c.weixin.qr.cancel_record(enrollment,enrollment['version'])

    def member(self, conversation, identifier='', *, required=True):
        explicit=bool(identifier)
        identifier = identifier or conversation.get('lead_member_id', '')
        member = self.store.one('SELECT * FROM room_members WHERE id=? AND conversation_id=?',
                                (identifier, conversation['id'])) if identifier else None
        if not member and explicit:
            raise DevError('CHANNEL_MEMBER_REQUIRED','所选 dot 不属于此房间',409)
        if not member:
            rows = self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('removed','paused') ORDER BY created_at,id LIMIT 2", (conversation['id'],))
            member = rows[0] if len(rows) == 1 else None
        if required and (not member or member['status'] in {'removed', 'paused'}):
            raise DevError('CHANNEL_MEMBER_REQUIRED', '请选择此房间的可用主责 dot，再继续连接', 409)
        return member

    def read(self, identifier, principal):
        with self.store.transaction(immediate=False):
            row, principal, conversation = self.get(identifier, principal)
            return {'flow': self.view(row, principal, conversation)}

    def snapshot(self, principal, conversation_id):
        row = self.store.one("SELECT * FROM room_channel_flows WHERE conversation_id=? ORDER BY created DESC LIMIT 1", (conversation_id,))
        return self.read(row['id'], principal)['flow'] if row else None

    def view(self, row, principal, conversation):
        stage, action = 'choose_account', 'start_qr'
        value = {'id': row['id'], 'kind': 'weixin', 'conversation_id': row['conversation_id'],
                 'member_id': row['member_id'], 'account_id': row['account_id'],
                 'version': row['version'], 'created_at': row['created'], 'updated_at': row['updated'],
                 'accounts': self.store.all('''SELECT id,label,bot_id,state,credential_version FROM weixin_accounts
                    WHERE owner_user_id=? AND space_id=? ORDER BY created,id''', (principal.user_id, principal.space_id)),
                 'account_connected': False, 'peer_verified': False, 'work_authorized': False}
        from hub.collaboration import channel_inbound
        from hub.collaboration.weixin_protocol import MAX_MEDIA, upload_estimate
        health = channel_inbound.summaries(self.store, 'weixin', [account['id'] for account in value['accounts']])
        for account in value['accounts']:
            account['inbound_health'] = health.get(account['id'], [])
        value['upload_estimate'] = {**upload_estimate([MAX_MEDIA]), 'reference_bytes': MAX_MEDIA}
        if row['enrollment_id']:
            qr = self.store.one('SELECT * FROM '+('room_weixin_logins' if row.get('native') else 'weixin_enrollments')+' WHERE id=?', (row['enrollment_id'],))
            if qr:
                value['qr'] = self.native.login_view(qr) if row.get('native') else self.c.weixin.qr.view(qr)
                if qr['expires_at'] <= self.c.clock() and qr['state'] not in {'accepted','cancelled'}:
                    value['qr'] = {**value['qr'], 'state': 'expired'}
                    value['qr'].pop('qrcode_image', None)
                state = value['qr']['state']
                stage, action = ('confirm_account', 'accept_account') if state == 'confirmed' else ('scan', 'poll_qr')
                if state in {'expired','cancelled','attention'}:
                    stage, action = 'needs_reconnect', 'start_qr'
                    value['reason_code'] = value['qr'].get('reason_code') or 'WEIXIN_QR_' + state.upper()
        if row['account_id']:
            account = self.c.weixin.account(row['account_id'], principal)
            value['account'] = {k: account[k] for k in ('id','label','bot_id','state','credential_version')}
            value['account']['inbound_health'] = health.get(account['id'], [])
            value['account_connected'] = account['state'] == 'active'
            stage, action = ('confirm_binding','bind') if account['state'] == 'active' else ('needs_reconnect','start_qr')
            if account['state'] == 'active':
                conflict = self.store.one("SELECT id,conversation_id,version FROM weixin_bindings WHERE account_id=? AND state!='revoked' AND id!=?", (account['id'], row['binding_id']))
                if conflict:
                    value['existing_binding'] = conflict
                    stage, action = 'route_conflict', 'migrate'
        if row['binding_id']:
            binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (row['binding_id'],))
            if binding:
                value['binding'] = {k: binding[k] for k in ('id','state','version','peer_id','pending_peer_id','pairing_until','generation','allow_replies')}
                if binding['state'] == 'pairing':
                    stage, action = 'pairing','refresh'
                    if binding['pairing_until'] > self.c.clock() and binding.get('pairing_secret'):
                        value['binding']['pairing_challenge'] = self.store.decrypt(binding['pairing_secret'])
                    else:
                        stage, action = 'pairing_expired','refresh_pairing'
                elif binding['state'] == 'awaiting_owner':
                    stage, action = 'confirm_owner','confirm_owner'
                elif binding['state'] == 'active':
                    stage, action = 'ready','refresh'
                    value['peer_verified'] = True
                    if binding.get('native_member_id'):
                        auth=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?',(binding['id'],))
                        value['task_scope']=self.native.preview(binding,principal)
                        value['work_authorized']=bool(auth and auth['state']=='active' and auth['scope_digest']==value['task_scope']['scope_digest'])
                        if not value['work_authorized'] and value['task_scope']['project_ids']:
                            action='approve_tasks'
                    else:
                        auth = self.c.channel_owner.current(binding['id'])
                        try:
                            value['work_authorized'] = bool(auth and auth['state'] == 'active' and self.c.channel_owner.live(auth))
                        except DevError as exc:
                            value['task_scope_error']=exc.code
                elif binding['state'] == 'paused':
                    stage, action = 'paused', 'resume'
                else:
                    stage, action = 'disconnected','bind'
        if row['account_id'] and value.get('account',{}).get('state')!='active':
            stage,action='needs_reconnect','start_qr'
            value.update(account_connected=False,peer_verified=False,work_authorized=False)
            value['reason_code']='WEIXIN_CREDENTIAL_CHANGED'
        # A renewal attempt remains the foreground flow until accepted; the
        # retained old account/binding must not conceal a newly issued QR.
        if value.get('qr',{}).get('state') not in {None,'accepted','cancelled'}:
            state=value['qr']['state']
            stage,action=('confirm_account','accept_account') if state=='confirmed' else ('scan','poll_qr')
            if state in {'expired','attention','verify_code_blocked','binded_redirect'}:
                stage,action='needs_reconnect','start_qr'
        if stage == 'confirm_binding':
            member = self.member(conversation, row['member_id'], required=False)
            if not member or (not row.get('native') and not member.get('legacy_dot_id')):
                stage, action = 'waiting_member','refresh'
            else:
                try:
                    if row.get('native'):
                        account=self.c.weixin.account(row['account_id'],principal)
                        value['task_scope']=self.native.preview({'id':'','account_id':account['id'],
                            'credential_version':account['credential_version'],'generation':account['generation'],
                            'native_member_id':member['id'],'conversation_id':conversation['id']},principal)
                        if not value['task_scope']['project_ids']:
                            value.pop('task_scope')
                    else:
                        value['task_scope'] = self.c.channel_enrollment.preview({**self.scope(row), 'channel':'weixin',
                            'account_id':row['account_id'], 'dot_id':member['legacy_dot_id']}, principal)
                except DevError as exc:
                    value['task_scope_error'] = exc.code
        if not row['project_id'] and not row.get('native'):
            stage, action = 'waiting_project','refresh'
        if row['state'] != 'active':
            stage, action = row['state'], ''
        value.update(stage=stage, primary_action=action)
        title={'choose_account':'连接微信','scan':'等待微信扫码','confirm_account':'确认扫码身份',
            'confirm_binding':'确认此房间的私聊范围','waiting_member':'先为此房间添加 dot',
            'waiting_project':'先为原连接关联项目','pairing':'在本人微信私聊发送配对码',
            'pairing_expired':'配对码已过期','confirm_owner':'确认本人联系人','ready':'微信已连接',
            'needs_reconnect':'微信需要重新连接','route_conflict':'此微信已有连接，需明确迁移',
            'paused':'微信已暂停','disconnected':'微信已断开','cancelled':'接入已取消','closed':'微信已解除'}.get(stage,'等待连接')
        value['display']={'title':title,'primary_action':action,'reason':value.get('reason_code','')}
        value['actions'] = [{'action': action, 'label': self.action_label(action)}] if action else []
        if row['state'] == 'active':
            if row['binding_id']:
                authority=self.store.one('SELECT state,version FROM '+('room_channel_owner_authorizations' if row.get('native') else 'channel_owner_authorizations')+' WHERE binding_id=?',(row['binding_id'],))
                if authority:
                    value['task_permission']={'state':authority['state'],'version':authority['version']}
                    if authority['state'] in {'active','pending_pairing','pending_revalidation'}:
                        value['actions'].append({'action':'revoke_tasks','label':'撤销工作许可'})
            if stage=='choose_account' and value['accounts']:
                value['actions'].insert(0,{'action':'select_account','label':'使用已有微信'})
            if value.get('binding',{}).get('state')=='active' and stage=='ready':
                value['actions'].append({'action':'pause','label':'暂停微信'})
            retained=value.get('binding',{}).get('state') in {'active','paused'}
            if retained and value.get('qr',{}).get('state') not in {None,'accepted','cancelled'}:
                value['actions'].append({'action':'cancel','label':'取消本次扫码'})
            value['actions'].append({'action':'disconnect' if retained else 'cancel','label':'解除连接' if retained else '取消'})
        if not conversation.get('room_protocol') or not row.get('native'):
            value['actions']=[item for item in value['actions'] if item['action'] in {'pause','disconnect','cancel','revoke_tasks'}]
            action=value['actions'][0]['action'] if value['actions'] else ''
            value['primary_action']=action
            value['display'].update(primary_action=action,reason='现有连接仅可安全停用；请完成房间切换后使用新连接。')
        value['allowed_actions']=[item['action'] for item in value['actions']]
        value['next_request'] = {'method':'POST','path':'/api/collaboration/channel-connect-flows/' + row['id'] + '/actions',
            'body':{'action':action,'expected_version':row['version'],
                    **({'expected_scope_digest':value['task_scope']['scope_digest']} if value.get('task_scope') else {}),
                    **({'expected_binding_version':value['existing_binding']['version']} if value.get('existing_binding') else {})},
            'form_fields':self.fields(action, value)} if action else None
        return value

    @staticmethod
    def action_label(action):
        return {'start_qr':'扫码连接','poll_qr':'检查扫码结果','accept_account':'确认此微信',
                'bind':'确认连接','confirm_owner':'确认本人','refresh_pairing':'刷新配对码',
                'refresh':'刷新状态','resume':'恢复连接','migrate':'迁移到此房间',
                'approve_tasks':'允许本人交办工作','revoke_tasks':'撤销工作许可','select_account':'选择已有微信'}.get(action, action)

    @staticmethod
    def fields(action, value):
        def field(name, label, kind='checkbox', **extra):
            return {'name':name,'type':kind,'label':label,'required':True,**extra}
        if action == 'start_qr':
            return [field('confirm_credential_storage','允许加密保存此微信凭据'),field('confirm_provider_terms','确认使用微信机器人服务')]
        if action == 'accept_account':
            return [field('label','显示名称','text',value='微信'),
                    field('confirm_scanner_id','确认扫码身份','text',value=value.get('qr',{}).get('scanner_id',''))]
        if action in {'bind','approve_tasks'}:
            fields = [field('confirm_external_data_ingress','允许此私聊进入当前房间'),
                      field('allow_replies','允许回复回到原私聊'),field('confirm_until_revoked','连接持续至我解除')]
            if value.get('task_scope'):
                fields += [{'name':'allow_owner_tasks','type':'checkbox','label':'允许本人在所列范围内交办工作','required':False}]
                fields += [{**field('confirm_' + key,label),'required':False,'depends_on':'allow_owner_tasks'} for key,label in (
                    ('owner_identity','发送配对码的是我本人'),('future_tasks','允许所列项目的后续工作'),
                    ('result_delivery','允许工作结果回到此私聊'),('pairing_notices','允许必要的配对和阻塞提示'))]
            if action=='approve_tasks':
                fields=[{k:v for k,v in f.items() if k!='depends_on'}|{'required':True}
                    for f in fields if f['name'] not in {'confirm_external_data_ingress','allow_replies','allow_owner_tasks'}]
            return fields
        if action=='select_account':
            return [field('account_id','已连接微信','select',options=[{'value':a['id'],'label':a['label']} for a in value['accounts']])]
        if action == 'confirm_owner':
            return [field('confirm_peer_id','确认本人联系人','text',value=value.get('binding',{}).get('pending_peer_id',''))]
        if action == 'poll_qr' and value.get('qr',{}).get('state') == 'need_verifycode':
            return [field('verify_code','微信验证码','text')]
        if action == 'migrate':
            return [field('confirm_migrate_binding_id','确认迁移原绑定','text',value=value.get('existing_binding',{}).get('id',''))]
        return []

    async def action(self, raw, principal):
        args = validate(ChannelAction, raw)
        row, principal, conversation = await self.store.run(self.get, args['flow_id'], principal)
        # QR operations own durable attempt markers and never hold SQLite while
        # waiting for the provider. Their original key survives lost responses.
        if args['action'] in {'start_qr','poll_qr'}:
            prepared = await self.store.run(self.prepare_network, args, principal)
            if prepared is None:
                return await self.store.run(self.read, row['id'], principal)
            request, generation = prepared
            if row.get('native'):
                result=await self.native.network(row,args['action'],args,principal)
            else:
                result = await (self.c.weixin.qr.start(request, principal) if args['action'] == 'start_qr'
                                else self.c.weixin.qr.status(request, principal))
            return await self.store.run(self.finish_network, args, principal, generation, result)
        return await self.store.run(self.local_action, args, principal)

    def check_action(self, row, principal, conversation, args):
        self.write_guard(row,conversation,args['action'])
        if row['version'] != args['expected_version']:
            raise DevError('STALE_VERSION','接入状态已更新，请保留当前选择并刷新',409)
        if row['state'] != 'active':
            raise DevError('CONNECT_FLOW_CLOSED','此流程已结束，不接受迟到操作',409)

    def write_guard(self,row,conversation,action):
        if action in {'pause','disconnect','cancel','revoke_tasks'}:
            return
        self.c.rooms.require_native(conversation)
        if not row.get('native'):
            self.c.rooms.migration.legacy_write(conversation['id'])

    def prepare_network(self, args, principal):
        with self.store.transaction():
            row, principal, conversation = self.get(args['flow_id'], principal)
            prior = self.store.one('''SELECT * FROM room_runtime_receipts WHERE owner_user_id=? AND conversation_id=?
                AND action=? AND request_key=?''', (principal.user_id,conversation['id'],'channel_network:'+row['id'],args['idempotency_key']))
            fingerprint = digest(args)
            if prior:
                saved = json.loads(prior['result'])
                if prior['fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT','同一请求标识不能用于不同接入动作',409)
                if saved.get('complete'):
                    return None
                self.write_guard(row,conversation,args['action'])
                return saved['request'], saved['generation']
            self.check_action(row,principal,conversation,args)
            view = self.view(row,principal,conversation)
            if view['primary_action'] != args['action']:
                raise DevError('CONNECT_STEP_INVALID','请完成当前步骤后继续',409)
            request = {} if row.get('native') else {**self.scope(row)}
            if row.get('native'):
                pass  # Native QR identity is room-scoped and needs no project.
            elif args['action'] == 'start_qr':
                request.update(idempotency_key='room-qr:'+row['id']+':'+args['idempotency_key'][:64],
                    confirm_credential_storage=args['confirm_credential_storage'],confirm_provider_terms=args['confirm_provider_terms'])
                if row['account_id']:
                    account = self.c.weixin.account(row['account_id'],principal)
                    request.update(renew_account_id=account['id'],expected_credential_version=account['credential_version'])
            else:
                qr = self.store.one('SELECT * FROM weixin_enrollments WHERE id=?',(row['enrollment_id'],))
                if not qr:
                    raise DevError('CONNECT_STEP_INVALID','请先生成二维码',409)
                request.update(enrollment_id=qr['id'],expected_version=qr['version'],verify_code=args['verify_code'])
            generation = row['version']
            self.store.execute('INSERT INTO room_runtime_receipts VALUES (?,?,?,?,?,?,?)',
                (principal.user_id,conversation['id'],'channel_network:'+row['id'],args['idempotency_key'],fingerprint,
                 canonical({'request':{k:v for k,v in request.items() if k!='verify_code'},'generation':generation,'complete':False}),self.c.clock()))
            return request,generation

    def finish_network(self,args,principal,generation,result):
        with self.store.transaction():
            row,principal,conversation=self.get(args['flow_id'],principal)
            if row['state']=='active' and row['version']==generation:
                self.write_guard(row,conversation,args['action'])
                self.store.execute('UPDATE room_channel_flows SET enrollment_id=?,version=version+1,updated=? WHERE id=?',
                                   (result['enrollment_id'],self.c.clock(),row['id']))
            self.store.execute('''UPDATE room_runtime_receipts SET result=? WHERE owner_user_id=? AND conversation_id=? AND action=? AND request_key=?''',
                (canonical({'complete':True,'enrollment_id':result['enrollment_id']}),principal.user_id,conversation['id'],'channel_network:'+row['id'],args['idempotency_key']))
            return self.read(row['id'],principal)

    def local_action(self,args,principal):
        with self.store.transaction():
            row,principal,conversation=self.get(args['flow_id'],principal)
            def save():
                self.check_action(row,principal,conversation,args)
                action=args['action']
                view=self.view(row,principal,conversation)
                if action not in view['allowed_actions']:
                    raise DevError('CONNECT_STEP_INVALID','请完成当前步骤后继续',409)
                updates={}
                scope=self.scope(row) if row['project_id'] else None
                request_key='room-channel:'+digest([row['id'],args['idempotency_key']])
                if action=='select_account':
                    if row['binding_id']:
                        raise DevError('CHANNEL_FLOW_CONFLICT','已有绑定必须先明确解除',409)
                    self.c.weixin.account(args['account_id'],principal)
                    updates['account_id']=args['account_id']
                elif action=='accept_account':
                    if row.get('native'):
                        updates['account_id']=self.native.accept_account(row,args,principal)
                    else:
                        qr=self.store.one('SELECT * FROM weixin_enrollments WHERE id=?',(row['enrollment_id'],))
                        qr_view=self.c.weixin.qr.view(qr)
                        result=self.c.weixin.qr.accept({**scope,'enrollment_id':qr['id'],'expected_version':qr['version'],
                            'idempotency_key':request_key,'label':args['label'],'confirm_scanner_id':args['confirm_scanner_id'],
                            'consent_receipt_id':qr_view['consent_receipt_id'],'confirm_replace_existing':args['confirm_replace_existing']},principal)
                        updates['account_id']=result['account_id']
                elif action=='bind':
                    if row.get('native'):
                        updates.update(self.native.bind(row,args,principal,conversation))
                    else:
                        updates.update(self.legacy_bind(row,args,principal,conversation,scope,request_key))
                elif action=='approve_tasks':
                    self.native.approve_tasks(row['binding_id'],args,principal)
                elif action=='revoke_tasks':
                    if row.get('native'):
                        self.native.revoke_tasks(row['binding_id'],principal)
                    else:
                        self.c.channel_owner.revoke(row['binding_id'],'owner_revoked')
                elif action in {'confirm_owner','refresh_pairing','pause','resume','disconnect'}:
                    binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(row['binding_id'],))
                    if not binding:
                        raise DevError('CHANNEL_BINDING_REQUIRED','此流程还没有微信绑定',409)
                    if binding.get('native_member_id'):
                        self.native.control(binding,args,principal)
                    elif action in {'pause','disconnect'}:
                        self.stop_legacy(binding['conversation_id'],binding['id'],'weixin',
                            'revoke' if action=='disconnect' else 'pause',binding['version'],request_key,principal)
                    else:
                        self.c.weixin.control({**scope,'binding_id':binding['id'],'expected_version':binding['version'],
                            'action':{'confirm_owner':'activate','disconnect':'revoke'}.get(action,action),
                            'confirm_peer_id':args['confirm_peer_id'],'idempotency_key':request_key},principal)
                    if action=='disconnect':
                        updates['state']='closed'
                elif action=='migrate':
                    binding=self.store.one("SELECT * FROM weixin_bindings WHERE account_id=? AND state!='revoked'",(row['account_id'],))
                    if not binding or args['confirm_migrate_binding_id']!=binding['id'] or args['expected_binding_version']!=binding['version']:
                        raise DevError('CHANNEL_ROUTE_CONFLICT','请明确核对原房间与原绑定版本',409)
                    self.c.rooms.owner(principal,binding['conversation_id'],include_archived=True)
                    if binding.get('native_member_id'):
                        self.native.control(binding,args,principal)
                    else:
                        self.stop_legacy(binding['conversation_id'],binding['id'],'weixin','revoke',
                            args['expected_binding_version'],request_key,principal)
                elif action=='cancel':
                    retained=None
                    if row['enrollment_id']:
                        if row.get('native'):
                            self.store.execute("UPDATE room_weixin_logins SET state='cancelled',payload='',version=version+1,updated=? WHERE id=? AND state!='accepted'",(self.c.clock(),row['enrollment_id']))
                        else:
                            self.cancel_legacy_login(row,principal)
                    if row['binding_id']:
                        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(row['binding_id'],))
                        retained=binding if binding and binding['state'] in {'active','paused'} else None
                        if binding and binding['state'] in {'pairing','awaiting_owner'}:
                            if binding.get('native_member_id'):
                                self.native.control(binding,args,principal)
                            else:
                                self.stop_legacy(binding['conversation_id'],binding['id'],'weixin','revoke',
                                    binding['version'],request_key,principal)
                    if retained:
                        updates['enrollment_id']=''
                    else:
                        updates['state']='cancelled'
                elif action=='refresh' and not row['project_id'] and not row.get('native'):
                    anchor=self.store.one('''SELECT r.project_id,r.environment_id FROM conversation_projects p
                        JOIN collaboration_rooms r ON r.id=p.room_id WHERE p.conversation_id=? ORDER BY p.created,p.room_id LIMIT 1''',(conversation['id'],))
                    if anchor:
                        updates.update(project_id=anchor['project_id'],environment_id=anchor['environment_id'])
                updates.update(version=row['version']+1,updated=self.c.clock())
                self.store.execute('UPDATE room_channel_flows SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),row['id']))
                return {'flow_id':row['id']}
            result=self.receipt(principal,conversation,'channel_action:'+row['id'],args,save)
            return self.read(result['flow_id'],principal)

    def legacy_bind(self,row,args,principal,conversation,scope,request_key):
                    member=self.member(conversation,args['member_id'] or row['member_id'])
                    dot_id=member.get('legacy_dot_id')
                    if not dot_id:
                        raise DevError('CHANNEL_MEMBER_REQUIRED','此 dot 尚未完成工作连接，请先继续 dot 接入',409)
                    request={**scope,'account_id':row['account_id'],'dot_id':dot_id,
                        'confirm_external_data_ingress':args['confirm_external_data_ingress'],'allow_replies':args['allow_replies'],
                        'lifetime_mode':'until_revoked','confirm_until_revoked':args['confirm_until_revoked'],'idempotency_key':request_key}
                    if args['allow_owner_tasks']:
                        preview=self.c.channel_enrollment.preview({**scope,'channel':'weixin','account_id':row['account_id'],'dot_id':dot_id},principal)
                        if args['expected_scope_digest']!=preview['scope_digest']:
                            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','请核对当前显示的工作范围',409)
                        request['owner_preapproval']={'text_version':preview['text_version'],'expected_scope_digest':preview['scope_digest'],
                            **{'confirm_'+key:args['confirm_'+key] for key in ('owner_identity','future_tasks','result_delivery','until_revoked','pairing_notices')}}
                    result=self.c.weixin.bind(request,principal)
                    return {'binding_id':result['binding_id'],'member_id':member['id']}
