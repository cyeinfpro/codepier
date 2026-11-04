"""Project-independent WeChat room binding using the original transport ledgers.

Accounts, inbound dedup, context tokens, ordered delivery parts and poll/send
leases remain in the established WeChat engine. This adapter supplies room and
member identity without manufacturing a project partition for empty-room chat.
"""
from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import uuid
from dataclasses import asdict

from hub.collaboration.projection_cache import memoized
from hub.principal import Principal, refresh_principal
from hub.collaboration.common import canonical, digest, redact
from hub.collaboration.weixin_protocol import API_BASE, ITEM_NAMES, approved_url, text, uint64
from hub.collaboration.qr_image import qr_svg
from hub.collaboration.weixin_protocol import incoming_text, incoming_items
from shared.util import DevError


def migrate(db):
    for table,name,decl in (
        ('room_channel_flows','native','INTEGER NOT NULL DEFAULT 0'),
        ('weixin_bindings','native_member_id',"TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in {row[1] for row in db.execute('PRAGMA table_info('+table+')')}:
            db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+decl)
    db.execute('''CREATE TABLE IF NOT EXISTS room_weixin_logins(
        id TEXT PRIMARY KEY,flow_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,space_id TEXT NOT NULL,
        state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,payload TEXT NOT NULL,
        expires_at REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_channel_owner_authorizations(
        id TEXT PRIMARY KEY,binding_id TEXT NOT NULL UNIQUE,member_id TEXT NOT NULL,
        state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,scope TEXT NOT NULL,scope_digest TEXT NOT NULL,
        owner_principal TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_channel_owner_inputs(
        message_id TEXT PRIMARY KEY,authorization_id TEXT NOT NULL,authorization_version INTEGER NOT NULL,
        message_digest TEXT NOT NULL,created REAL NOT NULL)''')


class RoomWeixin:
    def __init__(self,channels):
        self.channels,self.c,self.store=channels,channels.c,channels.store

    def require_room(self,conversation_id):
        room=self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(conversation_id,))
        if not room:
            raise DevError('ROOM_NOT_FOUND','此房间不存在',404)
        self.c.rooms.require_native(room)
        return room

    def login_view(self,row):
        body=json.loads(self.store.decrypt(row['payload'])) if row['payload'] else {}
        state=row['state']
        if state not in {'accepted','cancelled'} and row['expires_at']<=self.c.clock():
            state='expired'
        view={k:row[k] for k in ('version','expires_at')}
        view.update(enrollment_id=row['id'],state=state,reason_code=body.get('reason_code',''),credential_stored=state=='accepted',room_bound=False)
        if state in {'wait','scaned','need_verifycode'}:
            view['qrcode_image']=body.get('image','')
        if state=='confirmed':
            view.update(scanner_id=body['scanner_id'],bot_id=body['bot_id'])
            view['re_pair_required']=bool(body.get('renewal'))
        if state=='accepted':
            view['account_id']=body['account_id']
        return view

    async def network(self,flow,action,args,principal):
        row,claim=await self.store.run(self.prepare_login,flow,action,args,principal)
        if claim is None:
            return self.login_view(row)
        try:
            client=self.c.weixin.client_factory(claim.get('api_base',API_BASE),'')
            response=await (client.qr_start() if action=='start_qr' else client.qr_status(claim['qrcode'],claim.get('verify_code','')))
            error=None
        except Exception as exc:
            response=None
            error={'code':exc.code,'status':exc.status} if isinstance(exc,DevError) else {'code':'WEIXIN_QR_RESPONSE_UNCONFIRMED'}
        return await self.store.run(self.finish_login,row['id'],claim,response,error)

    def prepare_login(self,flow,action,args,principal):
        with self.store.transaction():
            current,principal,room=self.channels.get(flow['id'],principal)
            self.c.rooms.require_native(room)
            if current['state']!='active':
                raise DevError('CONNECT_FLOW_CLOSED','接入已取消',409)
            self.c.weixin.require_enabled(principal)
            now=self.c.clock()
            if action=='start_qr':
                if not args['confirm_credential_storage'] or not args['confirm_provider_terms']:
                    raise DevError('CHANNEL_CONSENT_REQUIRED','请确认凭据保存和微信服务使用',422)
                identifier='wxlogin_'+digest([flow['id'],args['idempotency_key']])[:32]
                row=self.store.one('SELECT * FROM room_weixin_logins WHERE id=?',(identifier,))
                if row:
                    return row,None  # An unknown external start is never repeated.
                count=self.store.one('SELECT COUNT(*) n FROM room_weixin_logins WHERE owner_user_id=? AND space_id=? AND created>?',
                    (principal.user_id,principal.space_id,now-3600))['n']
                if count>=20:
                    raise DevError('WEIXIN_QR_RATE_LIMIT','扫码请求过于频繁，请稍后再试',429)
                claim={'action':action,'fence':uuid.uuid4().hex,'api_base':API_BASE}
                payload={'attempted':True,'fence':claim['fence'],'api_base':API_BASE,
                         'consent':{'credential_storage':True,'provider_terms':True,'owner_user_id':principal.user_id}}
                if current['account_id']:
                    account=self.c.weixin.account(current['account_id'],principal)
                    payload['renewal']={k:account[k] for k in ('id','bot_id','credential_version','generation')}
                self.store.execute('INSERT INTO room_weixin_logins VALUES (?,?,?, ?,\'starting\',1,?,?,?,?)',
                    (identifier,flow['id'],principal.user_id,principal.space_id,self.store.encrypt(canonical(payload)),now+300,now,now))
                return self.store.one('SELECT * FROM room_weixin_logins WHERE id=?',(identifier,)),claim
            row=self.store.one('SELECT * FROM room_weixin_logins WHERE id=? AND flow_id=?',(current['enrollment_id'],flow['id']))
            if not row:
                raise DevError('WEIXIN_QR_NOT_FOUND','请先生成二维码',409)
            body=json.loads(self.store.decrypt(row['payload'])) if row['payload'] else {}
            if self.login_view(row)['state'] not in {'wait','scaned','need_verifycode'} or body.get('poll_until',0)>now:
                return row,None
            code=args['verify_code']
            if code and (row['state']!='need_verifycode' or not re.fullmatch(r'[0-9]{1,32}',code)):
                raise DevError('WEIXIN_VERIFY_CODE_INVALID','仅在微信要求时输入手机显示的数字',422)
            if code:
                code_digest=digest([row['id'],code])
                if body.get('verify_digest')==code_digest:
                    code=''
                else:
                    body['verify_digest']=code_digest
            body.update(poll_until=now+50,fence=uuid.uuid4().hex)
            self.store.execute('UPDATE room_weixin_logins SET payload=? WHERE id=?',(self.store.encrypt(canonical(body)),row['id']))
            return row,{'action':action,'fence':body['fence'],'qrcode':body['qrcode'],'verify_code':code,'api_base':body['api_base']}

    def finish_login(self,identifier,claim,response,error):
        with self.store.transaction():
            row=self.store.one('SELECT * FROM room_weixin_logins WHERE id=?',(identifier,))
            flow=self.store.one('SELECT * FROM room_channel_flows WHERE id=?',(row['flow_id'],))
            self.require_room(flow['conversation_id'])
            body=json.loads(self.store.decrypt(row['payload'])) if row['payload'] else {}
            if flow['state']!='active' or body.get('fence')!=claim['fence'] or row['expires_at']<=self.c.clock():
                return self.login_view(row)
            state=row['state']
            body['poll_until']=0
            if error:
                state='attention' if claim['action']=='start_qr' or error.get('status') in {401,403} else state
                body['reason_code']=error['code']
            else:
                try:
                    self.c.weixin.qr.provider_ok(response)
                    if claim['action']=='start_qr':
                        body.update(qrcode=text(response.get('qrcode'),8192),
                            image='data:image/svg+xml;base64,'+base64.b64encode(qr_svg(text(response.get('qrcode_img_content'),4096)).encode()).decode())
                        state='wait'
                    else:
                        state=response.get('status')
                        if state=='confirmed':
                            token=text(response.get('bot_token'),16384)
                            if any(ord(char)<33 or ord(char)>126 for char in token):
                                raise ValueError()
                            bot=text(response.get('ilink_bot_id'),256)
                            if body.get('renewal') and bot!=body['renewal']['bot_id']:
                                raise DevError('WEIXIN_RENEWAL_BOT_MISMATCH','扫码账号与原账号不同',409)
                            body.update(token=token,bot_id=bot,scanner_id=text(response.get('ilink_user_id'),256),
                                api_base=approved_url(response.get('baseurl') or body['api_base'],bases=self.c.weixin.allowed_api_bases,exact_base=True))
                            body.pop('qrcode',None)
                            body.pop('image',None)
                        elif state=='scaned_but_redirect':
                            host=text(response.get('redirect_host'),2048)
                            if any(value in host for value in ('/','?','#','@')):
                                raise ValueError()
                            body['api_base']=approved_url('https://'+host,bases=self.c.weixin.allowed_api_bases,exact_base=True)
                            state='scaned'
                        elif state in {'expired','verify_code_blocked','binded_redirect'}:
                            body={}
                        elif state not in {'wait','scaned','need_verifycode'}:
                            raise ValueError()
                except (DevError,ValueError,TypeError) as exc:
                    state='attention'
                    body={'reason_code':exc.code if isinstance(exc,DevError) else 'WEIXIN_QR_RESPONSE_UNSUPPORTED'}
            self.store.execute('UPDATE room_weixin_logins SET state=?,payload=?,version=version+1,updated=? WHERE id=?',
                (state,self.store.encrypt(canonical(body)),self.c.clock(),identifier))
            return self.login_view(self.store.one('SELECT * FROM room_weixin_logins WHERE id=?',(identifier,)))

    def accept_account(self,flow,args,principal):
        self.require_room(flow['conversation_id'])
        row=self.store.one('SELECT * FROM room_weixin_logins WHERE id=? AND flow_id=?',(flow['enrollment_id'],flow['id']))
        if not row or self.login_view(row)['state']!='confirmed':
            raise DevError('WEIXIN_QR_NOT_CONFIRMED','请先完成当前扫码',409)
        body=json.loads(self.store.decrypt(row['payload']))
        if not hmac.compare_digest(args['confirm_scanner_id'],body['scanner_id']):
            raise DevError('WEIXIN_SCANNER_MISMATCH','请确认当前扫码返回的真实身份',409)
        existing=self.store.one('SELECT * FROM weixin_accounts WHERE bot_id=?',(body['bot_id'],))
        if existing:
            if (existing['owner_user_id']!=principal.user_id or existing['space_id']!=principal.space_id
                    or not body.get('renewal') or body['renewal']['id']!=existing['id']
                    or body['renewal']['credential_version']!=existing['credential_version'] or not args['confirm_replace_existing']):
                raise DevError('WEIXIN_ACCOUNT_CONFLICT','请明确恢复原账号，不覆盖其他绑定',409)
            for binding in self.store.all("SELECT * FROM weixin_bindings WHERE account_id=? AND state!='revoked'",(existing['id'],)):
                if not binding.get('native_member_id'):
                    raise DevError('CHANNEL_ROUTE_CONFLICT','此账号仍保留旧连接，请先明确解除旧绑定再继续此房间接入',409)
                self.require_room(binding['conversation_id'])
        else:
            count=self.store.one('SELECT COUNT(*) n FROM weixin_accounts WHERE owner_user_id=? AND space_id=?',(principal.user_id,principal.space_id))['n']
            if count>=32:
                raise DevError('WEIXIN_ACCOUNT_QUOTA','已登记微信账号达到上限',409)
        identifier=existing['id'] if existing else uuid.uuid4().hex
        now=self.c.clock()
        self.store.execute('''INSERT INTO weixin_accounts
            (id,space_id,owner_user_id,bot_id,label,api_base,secret,state,credential_version,cursor,created,updated)
            VALUES (?,?,?,?,?,?,?,'active',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            label=excluded.label,api_base=excluded.api_base,secret=excluded.secret,state='active',
            credential_version=excluded.credential_version,generation=generation+1,poll_fence=poll_fence+1,poll_until=0,
            reason_code='',failures=0,next_poll=0,last_sync_at=NULL,updated=excluded.updated''',
            (identifier,principal.space_id,principal.user_id,body['bot_id'],args['label'],body['api_base'],self.store.encrypt(body['token']),
             existing['credential_version']+1 if existing else 1,self.store.encrypt(''),now,now))
        if existing:
            for binding in self.store.all("SELECT * FROM weixin_bindings WHERE account_id=? AND state!='revoked'",(identifier,)):
                # Same identity still requires the old peer to prove the new
                # credential generation. Unsent previous-generation parts stop.
                challenge='CPW-'+secrets.token_hex(16).upper()
                self.store.execute("UPDATE weixin_bindings SET state='pairing',generation=generation+1,credential_version=credential_version+1,pairing_hash=?,pairing_secret=?,pairing_until=?,pending_peer_id='',version=version+1,revalidation_required=1,updated=? WHERE id=?",
                    (digest(challenge),self.store.encrypt(challenge),now+900,now,binding['id']))
                self.c.weixin.delivery.quarantine_binding(binding['id'],'credential_generation_changed')
                self.store.execute("UPDATE room_channel_owner_authorizations SET state='pending_revalidation',updated=? WHERE binding_id=?",(now,binding['id']))
        self.store.execute("UPDATE room_weixin_logins SET state='accepted',payload=?,version=version+1,updated=? WHERE id=?",
            (self.store.encrypt(canonical({'account_id':identifier})),now,row['id']))
        return identifier

    def bind(self,flow,args,principal,conversation):
        self.c.rooms.require_native(conversation)
        member=self.channels.member(conversation,args['member_id'] or flow['member_id'])
        account=self.c.weixin.account(flow['account_id'],principal)
        if not args['confirm_external_data_ingress'] or not args['confirm_until_revoked']:
            raise DevError('CHANNEL_CONSENT_REQUIRED','请明确确认私聊入站和连接期限',422)
        if account['state']!='active' or self.store.one("SELECT 1 FROM weixin_bindings WHERE state!='revoked' AND (account_id=? OR conversation_id=?)",(account['id'],conversation['id'])):
            raise DevError('WEIXIN_BINDING_CONFLICT','此账号或房间已有绑定，请明确迁移原绑定',409)
        identifier,now=uuid.uuid4().hex,self.c.clock()
        challenge='CPW-'+secrets.token_hex(16).upper()
        owner=asdict(principal);owner.update(scopes=[],session_hash='',token_hash='')
        self.store.execute('UPDATE weixin_accounts SET generation=generation+1,poll_fence=poll_fence+1,poll_until=0,next_poll=0,last_sync_at=NULL,updated=? WHERE id=?',(now,account['id']))
        self.store.execute('''INSERT INTO weixin_bindings
            (id,account_id,space_id,owner_user_id,room_id,conversation_id,project_id,environment_id,dot_id,
             owner_principal,dot_principal,project_snapshot,credential_version,generation,state,pairing_hash,pairing_until,
             allow_replies,allow_media_outbound,expires_at,created,updated,pairing_secret,lifetime_mode,native_member_id)
            VALUES (?,?,?,?,'',?,'','',?,?,?,'{}',?,?,'pairing',?,?,?,0,?,?,?,?,'until_revoked',?)''',
            (identifier,account['id'],principal.space_id,principal.user_id,conversation['id'],member.get('legacy_dot_id',''),
             canonical(owner),member.get('principal','{}'),account['credential_version'],account['generation']+1,
             digest(challenge),now+900,int(args['allow_replies']),now+86400,now,now,self.store.encrypt(challenge),member['id']))
        if args['allow_owner_tasks']:
            self.approve_tasks(identifier,args,principal,pending=True)
        return {'binding_id':identifier,'member_id':member['id']}

    def valid(self,binding,*,pairing=False):
        return self._valid(binding,pairing=pairing,require_member=True)

    @memoized
    def _valid(self,binding,*,pairing=False,require_member):
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        allowed={'active','pairing','awaiting_owner'} if pairing else {'active'}
        if binding['state'] not in allowed:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已暂停或解除',403)
        if binding['state']=='pairing' and binding['pairing_until']<=self.c.clock():
            raise DevError('WEIXIN_PAIRING_EXPIRED','原配对码已到期，请刷新配对码',403)
        self.c.weixin.require_enabled(binding)
        saved=json.loads(binding['owner_principal'])
        owner=refresh_principal(self.store,Principal(**{**saved,'scopes':set()}))
        owner,room=self.c.rooms.owner(owner,binding['conversation_id'])
        self.c.rooms.require_native(room)
        if room['state']!='active':
            raise DevError('ROOM_PAUSED','原房间已暂停',409)
        member=self.c.rooms.member(binding['native_member_id'],room['id'])
        if require_member and member['status'] in {'removed','paused'}:
            raise DevError('WEIXIN_MEMBER_REMOVED','原主责 dot 已移出',403)
        account=self.c.weixin.account(binding['account_id'],owner)
        if account['state']!='active' or account['generation']!=binding['generation'] or account['credential_version']!=binding['credential_version']:
            raise DevError('WEIXIN_CREDENTIAL_CHANGED','原账号凭据或绑定世代已改变',403)
        return binding,account,owner,room,member

    @memoized
    def preview(self,binding,principal):
        member=self.c.rooms.member(binding['native_member_id'],binding['conversation_id'])
        scopes=self.c.room_work_bridge.scopes(member)
        scope={'binding_id':binding['id'],'account_id':binding['account_id'],'credential_version':binding['credential_version'],
            'generation':binding['generation'],'member_id':member['id'],'conversation_id':member['conversation_id'],
            'owner_user_id':principal.user_id,'space_id':principal.space_id,
            'projects':[{'project_id':s['project_id'],'environment_id':s['environment_id'],'version':s['version'],
                'capabilities':json.loads(s['capabilities']),'execution_targets':json.loads(s['execution_targets']),
                'scope_digest':self.c.room_work_bridge.fingerprint([s])} for s in scopes]}
        return {'scope':scope,'scope_digest':digest(scope),'text_version':'room-channel-consent-v1',
                'project_ids':[s['project_id'] for s in scopes],'projects':scope['projects'],
                'summary':'仅限此成员已获房主批准的项目和能力；连接持续至解除。','lifetime_mode':'until_revoked'}

    def approve_tasks(self,binding_id,args,principal,*,pending=False):
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(binding_id,))
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        self.require_room(binding['conversation_id'])
        preview=self.preview(binding,principal)
        if not preview['scope']['projects']:
            raise DevError('CHANNEL_TASK_PROJECT_REQUIRED','先明确此成员可工作的项目，再开通微信交办',409)
        if (not all(args.get('confirm_'+key) for key in ('owner_identity','future_tasks','result_delivery','until_revoked','pairing_notices'))
                or not binding['allow_replies']):
            raise DevError('CHANNEL_CONSENT_REQUIRED','本人任务范围和回传需明确确认',422)
        # Pre-binding preview freezes the account/member/scopes without a
        # binding ID. The final transaction compares that exact preview.
        expected=args['expected_scope_digest']
        current=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?',(binding_id,))
        if not pending and expected!=preview['scope_digest']:
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','微信任务范围已改变，请重新核对',409)
        if pending:
            preview_scope={**preview['scope'],'binding_id':'','generation':binding['generation']-1}
            if expected!=digest(preview_scope):
                raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','微信任务范围已改变，请重新核对',409)
        owner=asdict(principal);owner.update(scopes=[],session_hash='',token_hash='')
        self.store.execute('''INSERT INTO room_channel_owner_authorizations
            (id,binding_id,member_id,state,scope,scope_digest,owner_principal,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET state=excluded.state,
            scope=excluded.scope,scope_digest=excluded.scope_digest,owner_principal=excluded.owner_principal,
            version=version+1,created=excluded.created,updated=excluded.updated''',
            (current['id'] if current else uuid.uuid4().hex,binding_id,binding['native_member_id'],
             'pending_pairing' if pending else 'active',canonical(preview['scope']),preview['scope_digest'],canonical(owner),self.c.clock(),self.c.clock()))

    def control(self,binding,args,principal):
        action=args['action']
        if action not in {'pause','disconnect','migrate','cancel'}:
            self.require_room(binding['conversation_id'])
        if action=='confirm_owner':
            if binding['state']!='awaiting_owner' or not binding['pending_peer_id'] or not hmac.compare_digest(args['confirm_peer_id'],binding['pending_peer_id']):
                raise DevError('WEIXIN_PAIRING_UNVERIFIED','请确认当前配对返回的本人联系人',409)
            if binding['peer_id'] and binding['peer_id']!=binding['pending_peer_id']:
                raise DevError('CHANNEL_OWNER_PEER_CHANGED','恢复必须由原联系人验证',403)
            self.store.execute("UPDATE weixin_bindings SET state='active',peer_id=pending_peer_id,pending_peer_id='',revalidation_required=0,version=version+1,updated=? WHERE id=?",(self.c.clock(),binding['id']))
            self.store.execute("UPDATE room_channel_owner_authorizations SET state='active',updated=? WHERE binding_id=? AND state='pending_pairing'",(self.c.clock(),binding['id']))
        elif action=='refresh_pairing':
            if binding['state'] not in {'pairing','awaiting_owner','paused'}:
                raise DevError('WEIXIN_PAIRING_UNAVAILABLE','此绑定无需重新配对',409)
            challenge='CPW-'+secrets.token_hex(16).upper()
            self.store.execute("UPDATE weixin_bindings SET state='pairing',pairing_hash=?,pairing_secret=?,pairing_until=?,pending_peer_id='',version=version+1,updated=? WHERE id=?",
                (digest(challenge),self.store.encrypt(challenge),self.c.clock()+900,self.c.clock(),binding['id']))
        elif action=='resume':
            if binding['state']!='paused' or not binding['peer_id'] or binding['revalidation_required']:
                raise DevError('WEIXIN_RESUME_DENIED','此绑定需要先完成原联系人配对',409)
            self.store.execute("UPDATE weixin_bindings SET state='active',version=version+1,updated=? WHERE id=?",(self.c.clock(),binding['id']))
        else:
            state='revoked' if action in {'disconnect','migrate','cancel'} else 'paused'
            self.store.execute('UPDATE weixin_bindings SET state=?,version=version+1,updated=? WHERE id=?',(state,self.c.clock(),binding['id']))
            if state=='revoked':
                self.store.execute("UPDATE room_channel_owner_authorizations SET state='revoked',updated=? WHERE binding_id=?",(self.c.clock(),binding['id']))
                self.c.weixin.delivery.quarantine_binding(binding['id'],'binding_revoked')
        self.store.execute('UPDATE weixin_accounts SET poll_fence=poll_fence+1,poll_until=0,next_poll=0 WHERE id=?',(binding['account_id'],))

    def goal_source(self,goal,source,binding):
        """Freeze channel origin before considering an owner-approved role overlay."""
        native=self.c.room_work_bridge.native
        reference=native.reference(goal)
        if not reference or not binding:
            raise DevError('WEIXIN_RECOVERY_SOURCE_CHANGED','原工作没有此微信来源',403)
        original=native.original(reference)
        body=json.loads(source['body']);origin=body.get('external_origin',{})
        approval=self.store.one('SELECT * FROM coordination_approvals WHERE id=? AND goal_id=?',
            (goal['approval_id'],goal['id']))
        link=self.c.delegation.goal_link(goal)
        if (source['id']!=original['source_message_id'] or source['version']!=original['source_version']
                or digest(body.get('body_text',''))!=original['source_digest']
                or original['conversation_id']!=goal['conversation_id'] or source['conversation_id']!=binding['conversation_id']
                or goal['conversation_id']!=binding['conversation_id']
                or original['coordinator_member_id']!=binding['native_member_id']
                or not origin.get('native_room') or origin.get('binding_id')!=binding['id']
                or not link or link['message_id']!=source['id'] or link['approval_id']!=goal['approval_id']
                or link['goal_digest']!=goal['digest'] or not approval or approval['digest']!=goal['digest']):
            raise DevError('WEIXIN_RECOVERY_SOURCE_CHANGED','接替仅适用于此原工作冻结的微信来源与批准',403)
        if min(goal['expires_at'] or 0,approval['expires_at'])<=self.c.clock():
            raise DevError('ROOM_WORK_APPROVAL_EXPIRED','原工作批准已到期，接替不能延长',403)
        return native,reference,original

    def recovered_goal(self,goal,source,binding):
        if not goal or not self.c.room_work_bridge.native.reference(goal):
            return None
        native,reference,_=self.goal_source(goal,source,binding)
        revision=self.c.room_goal_recovery.revision(reference)
        if not revision:
            return None
        self.c.room_goal_recovery.authority(reference)
        spec=native.record(reference)
        lead=next((m for m in spec['members'] if m['member_id']==spec['coordinator_member_id']),None)
        if lead is None:
            raise DevError('ROOM_MEMBER_NOT_IN_APPROVAL','原主责成员不在工作批准名单中',403)
        current=self.c.rooms.member(lead['member_id'],goal['conversation_id'])
        if self.c.rooms.state(current)!='ready_work':
            raise DevError('ROOM_RECOVERY_MEMBER_UNAVAILABLE','当前接替主责的连接或批准已不可用',403)
        permitted=set()
        for rule in lead['projects']:
            try:
                native.validate_rule(lead,rule,goal['conversation_id'])
                permitted.add(rule['project_id'])
            except DevError:
                continue
        if not permitted:
            raise DevError('ROOM_WORK_SCOPE_CHANGED','当前接替主责没有仍有效的原工作范围',403)
        return {'scope':spec,'lead':lead,'project_ids':permitted}

    def recovery_author(self,goal,message,owner,*,project_ids):
        """Only the explicit owner recovery planner may precede a role overlay."""
        self.c.rooms.owner(owner,goal['conversation_id'])
        self.c.room_goal_recovery.eligible_base(goal,owner)
        return self._author(message,project_ids=project_ids,goal=goal,recovery_owner=owner)

    def author(self,message,*,project_ids=None,goal=None):
        return self._author(message,project_ids=project_ids,goal=goal)

    def _author(self,message,*,project_ids=None,goal=None,recovery_owner=None):
        body=json.loads(message['body']);origin=body.get('external_origin',{})
        record=self.store.one('SELECT * FROM room_channel_owner_inputs WHERE message_id=?',(message['id'],))
        auth=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE id=?',(record['authorization_id'],)) if record else None
        if not auth or auth['state']!='active' or auth['version']!=record['authorization_version'] or record['message_digest']!=digest(body.get('body_text','')):
            raise DevError('CHANNEL_OWNER_NOT_APPROVED','原私聊消息没有有效的本人任务批准',403)
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(auth['binding_id'],))
        if recovery_owner is not None:
            self.goal_source(goal,message,binding)
            recovered=True
        else:
            recovered=self.recovered_goal(goal,message,binding) if goal else None
        _,_,owner,_,_=self._valid(binding,require_member=not bool(recovered))
        preview=self.preview(binding,owner)
        saved=json.loads(auth['scope'])
        scope_matches=preview['scope_digest']==auth['scope_digest']
        if project_ids is not None:
            # Existing Goals may continue their unchanged project rules after
            # an unrelated rule is removed. This is never used to trust a new
            # inbound input or to add authority outside the original consent.
            current=preview['scope']
            old_rules={r['project_id']:r for r in saved['projects']}
            new_rules={r['project_id']:r for r in current['projects']}
            scope_matches=bool(project_ids) and ({k:v for k,v in current.items() if k!='projects'}==
                {k:v for k,v in saved.items() if k!='projects'}) and all(
                p in old_rules and p in new_rules and old_rules[p]==new_rules[p] for p in project_ids)
        if (not scope_matches or digest(saved)!=auth['scope_digest']
                or origin.get('peer_id')!=binding['peer_id'] or origin.get('generation')!=binding['generation']
                or message['conversation_id']!=binding['conversation_id']):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','原微信身份或任务范围已改变',403)
        latest=self.store.one('SELECT message_id FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?',
            (binding['account_id'],binding['generation'],binding['peer_id'],origin['external_message_id']))
        if latest and latest['message_id']!=message['id']:
            raise DevError('CHANNEL_OWNER_SOURCE_SUPERSEDED','原消息已有新版本',409)
        return {'kind':'verified_external_owner','authenticated':True,'channel':'weixin','user_id':owner.user_id,
                'authorization_id':auth['id'],'authorization_version':auth['version']}

    def revoke_tasks(self,binding_id,principal):
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(binding_id,))
        if not binding or not binding.get('native_member_id'):
            raise DevError('CHANNEL_BINDING_REQUIRED','此流程没有原房间微信绑定',409)
        self.c.rooms.owner(principal,binding['conversation_id'])
        auth=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?',(binding_id,))
        if not auth or auth['state']=='revoked':
            return {'work_permission_revoked':True,'chat_connected':binding['state']=='active','affected_work_ids':[]}
        self.store.execute("UPDATE room_channel_owner_authorizations SET state='revoked',version=version+1,updated=? WHERE id=?",
            (self.c.clock(),auth['id']))
        goals=self.store.all('''SELECT DISTINCT g.* FROM coordination_goals g JOIN delegation_requests d ON d.goal_id=g.id
            JOIN room_channel_owner_inputs i ON i.message_id=d.message_id WHERE i.authorization_id=?''',(auth['id'],))
        for goal in goals:
            self.c.coordination.stop_work(goal,'CHANNEL_OWNER_REVOKED')
            if goal['state'] not in {'cancelled','expired'}:
                self.store.execute("UPDATE coordination_goals SET state='paused',version=version+1,updated=? WHERE id=?",(self.c.clock(),goal['id']))
            self.c.rooms.change(binding['conversation_id'],'work',goal['id'],goal['version']+1)
        member=self.c.rooms.member(binding['native_member_id'],binding['conversation_id'])
        self.c.room_consumers.enqueue_ref(member,'control_notice',auth['id'],auth['version']+1,{'reason_code':'channel_owner_revoked'})
        return {'work_permission_revoked':True,'chat_connected':binding['state']=='active',
            'affected_work_ids':[goal['id'] for goal in goals]}

    def ingest(self,binding,account,owner,room,member,message):
        incoming_items(message)
        self.c.rooms.require_native(room)
        if not isinstance(message,dict):
            raise DevError('WEIXIN_MESSAGE_INVALID','微信消息必须为结构化对象',422)
        peer=text(message.get('from_user_id'),256);external_id=uint64(message.get('message_id'))
        created=uint64(message.get('create_time_ms',0));updated=uint64(message.get('update_time_ms',0));deleted=uint64(message.get('delete_time_ms',0))
        version=str(max(map(int,(created,updated,deleted))))
        source_hash=digest({k:v for k,v in message.items() if k not in {'context_token','seq'}})
        identity=(account['id'],binding['generation'],peer,external_id)
        prior=self.store.one('SELECT * FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?',identity)
        history=self.store.one('SELECT * FROM weixin_inbound_versions WHERE account_id=? AND generation=? AND peer_id=? AND external_id=? AND provider_version=?',(*identity,version))
        if history:
            if history['source_digest']!=source_hash:
                raise DevError('WEIXIN_DUPLICATE_CONFLICT','同一微信版本内容不同，保留原游标',409)
            return history['message_id']
        if prior and prior['source_digest']==source_hash:
            return prior['message_id']
        if prior and (not int(version) or not prior['provider_version'] or version==prior['provider_version']
                or created!='0' and prior['provider_created'] and created!=prior['provider_created']):
            raise DevError('WEIXIN_DUPLICATE_CONFLICT','原微信消息没有可比较版本',409)
        newest=not prior or int(version)>int(prior['provider_version'])
        count=self.store.one('SELECT COUNT(*) n FROM weixin_inbound_versions WHERE account_id=? AND created>?',
            (account['id'],self.c.clock()-86400))['n']
        if count>=5000:
            raise DevError('WEIXIN_DAILY_QUOTA','微信收件达到每日上限，原游标保留',429)
        items=message.get('item_list',[])
        if not isinstance(items,list) or len(items)>32:
            raise DevError('WEIXIN_ITEMS_INVALID','微信消息分项无效',422)
        echo=message.get('client_id') and (self.store.one('SELECT 1 FROM weixin_parts WHERE client_id=?',(message['client_id'],))
            or self.store.one("SELECT 1 FROM channel_onboarding_notices WHERE 'cpo-' || id=?",(message['client_id'],))
            or self.store.one("SELECT 1 FROM channel_mirror_parts WHERE 'cpm-' || id=?",(message['client_id'],)))
        direct=not message.get('group_id') and type(message.get('message_type')) is int and message['message_type']==1 and not echo and peer!=account['bot_id']
        if direct and message.get('to_user_id') not in (None,'',account['bot_id']):
            raise DevError('WEIXIN_RECEIVER_MISMATCH','当前微信消息不属于原机器人',409)
        content='\n'.join(incoming_text(item.get('text_item',{}).get('text'),12000,empty=True) for item in items if isinstance(item,dict) and item.get('type')==1)
        disposition,message_id='unpaired_peer_quarantined',None
        if not direct:
            disposition='group_or_echo_quarantined'
        elif binding['state']=='pairing' and binding['pairing_until']>self.c.clock():
            if (not prior and not int(updated) and not int(deleted) and int(created)>=int(binding['updated']*1000)
                    and hmac.compare_digest(digest(content.strip()),binding['pairing_hash'])):
                if binding['peer_id'] and binding['peer_id']!=peer:
                    disposition='different_peer_quarantined'
                else:
                    self.store.execute("UPDATE weixin_bindings SET state='awaiting_owner',pending_peer_id=?,pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?",
                        (peer,self.c.clock(),binding['id']))
                    disposition='pairing_waits_for_panel_owner'
        elif re.fullmatch(r'CPW-[A-F0-9]{32}',content.strip()):
            disposition='pairing_code_never_a_task'
        elif binding['state']=='active' and hmac.compare_digest(peer,binding['peer_id']):
            attachment_items=[]
            for ordinal,item in enumerate(items):
                if not isinstance(item,dict) or type(item.get('type')) is not int:
                    raise DevError('WEIXIN_ITEMS_INVALID','外部消息分项类型无效',422)
                if item['type'] in ITEM_NAMES:
                    attachment_items.append((ordinal,item))
                elif item['type']!=1:
                    content+='\n[未支持的外部消息分项]'
            pending=self.store.one("SELECT COUNT(*) n FROM dot_mailbox_entries WHERE member_id=? AND kind='message' AND acknowledged_at IS NULL",(member['id'],))['n']
            if newest and pending>=200:
                raise DevError('WEIXIN_INBOX_QUOTA','原 dot 未处理消息达到积压上限，保留原游标',429)
            message_id=uuid.uuid4().hex
            if int(deleted):
                content,attachment_items='[微信消息已删除]',[]
            if not content:
                content='[附件消息]'
            content=incoming_text(content,24000)
            body={'body_text':redact(content),'room_member_id':member['id'],'room_routing':{'state':'queued','member_id':member['id'],'reason_code':''},
                'external_origin':{'channel':'weixin','native_room':True,'binding_id':binding['id'],'account_id':account['id'],
                    'generation':binding['generation'],'peer_id':peer,'external_message_id':external_id,'provider_version':version,
                    'provider_created':created,'provider_deleted':bool(int(deleted)),'revision_is_latest_at_commit':newest},
                'requires_owner_execution_request':True}
            parent=prior['message_id'] if prior and prior['message_id'] else ''
            previous=self.store.one('SELECT thread_root_id FROM collaboration_messages WHERE id=? AND conversation_id=?',(parent,room['id'])) if parent else None
            root=previous['thread_root_id'] if previous else message_id
            self.store.execute('''INSERT INTO collaboration_messages
                (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created)
                VALUES (?,NULL,?,?,?,?,?,'external_channel',?,'text',?,'saved',?)''',
                (message_id,room['id'],root,root,parent,'weixin:'+account['id']+':'+digest(peer)[:20],
                 'generation:'+str(binding['generation'])+':'+external_id+':version:'+version,canonical(body),self.c.clock()))
            for ordinal,item in attachment_items:
                self.c.weixin.media.add_inbound(binding,message_id,ordinal,item)
            auth=self.store.one("SELECT * FROM room_channel_owner_authorizations WHERE binding_id=? AND state='active'",(binding['id'],))
            if auth and not prior and not int(updated) and not int(deleted) and int(created)>=int(auth['created']*1000):
                preview=self.preview(binding,owner)
                if preview['scope_digest']==auth['scope_digest']:
                    self.store.execute('INSERT INTO room_channel_owner_inputs VALUES (?,?,?,?,?)',(message_id,auth['id'],auth['version'],digest(body['body_text']),self.c.clock()))
            canonical_message=self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(message_id,))
            if newest:
                self.c.room_consumers.enqueue(canonical_message,member,owner)
            self.c.rooms.change(room['id'],'message',message_id,1)
            disposition='message_committed'
        context=message.get('context_token')
        if direct and newest and context and (peer==binding['peer_id'] or disposition=='pairing_waits_for_panel_owner'):
            text(context,65536)
            self.store.execute('''INSERT INTO weixin_contexts(account_id,generation,peer_id,token,observed_at) VALUES (?,?,?,?,?)
                ON CONFLICT(account_id,generation,peer_id) DO UPDATE SET token=excluded.token,observed_at=excluded.observed_at,version=version+1''',
                (account['id'],binding['generation'],peer,self.store.encrypt(context),self.c.clock()))
        self.store.execute('INSERT INTO weixin_inbound_versions VALUES (?,?,?,?,?,?,?,?)',(*identity,version,source_hash,message_id,self.c.clock()))
        if newest:
            self.store.execute('''INSERT INTO weixin_inbound(account_id,generation,peer_id,external_id,source_digest,message_id,disposition,created,provider_version,provider_created)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,generation,peer_id,external_id) DO UPDATE SET
                source_digest=excluded.source_digest,message_id=excluded.message_id,disposition=excluded.disposition,
                provider_version=excluded.provider_version,provider_created=excluded.provider_created''',
                (*identity,source_hash,message_id,disposition,self.c.clock(),version,created))
        return message_id

    def delivery_validate(self,delivery):
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(delivery['binding_id'],))
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        message=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',(delivery['message_id'],binding['conversation_id']))
        source=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',(delivery['source_message_id'],binding['conversation_id']))
        if not message or not source:
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED','原房间消息已不可用',403)
        binding,account,owner,room,member=self.reply_binding(binding,message,source)
        body=json.loads(message['body']);origin=json.loads(source['body']).get('external_origin',{})
        self.c.chatroom.require_message_visible(owner,message)
        if (not binding['allow_replies'] or delivery['generation']!=binding['generation'] or delivery['peer_id']!=binding['peer_id']
                or not self.sender_matches(binding,message,source) or message['version']!=delivery['message_version'] or digest(body)!=delivery['message_digest']
                or origin.get('binding_id')!=binding['id'] or origin.get('generation')!=binding['generation'] or origin.get('peer_id')!=binding['peer_id']):
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原微信回复内容或收件身份已改变',403)
        if self.store.one('SELECT 1 FROM room_channel_owner_inputs WHERE message_id=?',(source['id'],)):
            self.reply_author(message,source)
        return binding,account,owner,message

    def reply_binding(self,binding,message,source):
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(message.get('goal_id'),)) if message.get('goal_id') else None
        recovered=self.recovered_goal(goal,source,binding)
        return self._valid(binding,require_member=not bool(recovered))

    def reply_author(self,message,source):
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(message.get('goal_id'),)) if message.get('goal_id') else None
        native=self.c.room_work_bridge.native
        if goal and native.reference(goal):
            spec=native.record(native.reference(goal))
            if (source['id']!=spec['source_message_id'] or source['conversation_id']!=spec['conversation_id']
                    or source['version']!=spec['source_version'] or digest(json.loads(source['body']).get('body_text',''))!=spec['source_digest']):
                raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED','原工作回复的输入证据已改变',403)
            projects=set(json.loads(message['body']).get('provenance_project_ids',[]))
            if not projects or not projects<=set(spec['project_ids']):
                raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原工作回复缺少有效项目来源',403)
            return self.author(source,project_ids=projects,goal=goal)
        if self.store.one('SELECT 1 FROM room_channel_owner_inputs WHERE message_id=?',(source['id'],)):
            return self.author(source)
        return self.c.channel_owner.author(source)

    def sender_matches(self,binding,message,source=None):
        body=json.loads(message['body'])
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(message.get('goal_id'),)) if message.get('goal_id') else None
        if goal and self.c.room_work_bridge.native.reference(goal):
            source=source or self.c.weixin.delivery.root_source(message)
            if not source:
                return False
            try:
                recovered=self.recovered_goal(goal,source,binding)
            except DevError:
                return False
            if recovered:
                lead=recovered['lead'];revision=self.c.room_runtime.intent_revision(goal['id'])
                if (message['author']!=lead['grant_id'] or body.get('sender_member_id')!=lead['member_id']
                        or message['conversation_id']!=goal['conversation_id'] or body.get('intent_revision')!=revision
                        or body.get('coordinator_epoch',recovered['scope']['coordinator_epoch'])!=recovered['scope']['coordinator_epoch']
                        or not set(body.get('provenance_project_ids',[]))<=recovered['project_ids']):
                    return False
                if body.get('work_item_id'):
                    item=self.store.one('''SELECT w.goal_id,w.approval_id,a.member_id,a.intent_revision
                        FROM coordination_work w JOIN room_native_work_assignments a ON a.work_id=w.id WHERE w.id=?''',
                        (body['work_item_id'],))
                    return bool(item and item['goal_id']==goal['id'] and item['approval_id']==goal['approval_id']
                        and item['member_id']==lead['member_id'] and item['intent_revision']==revision)
                intent=self.store.one('SELECT * FROM room_work_intent_sources WHERE goal_id=? AND intent_revision=?',
                    (goal['id'],revision))
                return bool(intent and body.get('source_message_id')==intent['source_message_id']
                    and message['reply_to_id']==intent['source_message_id'])
        member=self.c.rooms.member(binding['native_member_id'],binding['conversation_id'])
        if message['author']!=member['grant_id'] or message['conversation_id']!=member['conversation_id']:
            return False
        if body.get('sender_member_id')==member['id']:
            return True
        bridge=self.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND legacy_dot_id=? AND state='active'",
            (member['id'],body.get('sender_dot_id','')))
        if not bridge or not message.get('goal_id') or bridge['binding_generation']!=member['binding_generation']:
            return False
        source=self.store.one('SELECT * FROM room_work_sources WHERE goal_id=? AND member_id=? AND legacy_dot_id=?',
            (message['goal_id'],member['id'],bridge['legacy_dot_id']))
        return bool(source and source['grant_id']==member['grant_id'] and source['scope_digest']==bridge['scope_digest'])
