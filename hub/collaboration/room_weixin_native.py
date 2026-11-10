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
import unicodedata
import uuid
from dataclasses import asdict

from hub.collaboration.projection_cache import memoized
from hub.principal import Principal, refresh_principal
from hub.collaboration.common import canonical, digest, redact
from hub.collaboration.weixin_protocol import API_BASE, ITEM_NAMES, approved_url, text, uint64
from hub.collaboration.qr_image import qr_svg
from hub.collaboration.room_waiting_inputs import (
    BLOCKED_MEMBERS, fence_follow_poll, record_input, recover_waiting, retire_input, route_input,
)
from hub.collaboration.weixin_protocol import incoming_text, incoming_items
from shared.util import DevError


PAIRING_CODE = re.compile(r'CPW-[A-F0-9]{32}')
MAX_PENDING_NATIVE_INPUTS = 200


def pairing_text(value):
    """Normalize how people actually type or paste the one-time pairing code.

    Phone keyboards lower-case letters, IMEs produce full-width dashes and a
    paste can carry spaces or line breaks. Only the 32 hex digits carry the
    secret; the hash comparison itself stays constant-time and exact.
    """
    raw = unicodedata.normalize('NFKC', value or '')
    compact = re.sub(r'[^0-9A-Za-z]', '', raw).upper()
    if compact.startswith('CPW'):
        compact = compact[3:]
    return 'CPW-' + compact if re.fullmatch(r'[0-9A-F]{32}', compact) else raw.strip()


def migrate(db):
    for table,name,decl in (
        ('room_channel_flows','native','INTEGER NOT NULL DEFAULT 0'),
        ('weixin_bindings','native_member_id',"TEXT NOT NULL DEFAULT ''"),
        ('weixin_bindings','auto_confirm_pairing','INTEGER NOT NULL DEFAULT 0'),
        ('weixin_bindings','routing_mode',"TEXT NOT NULL DEFAULT 'member'"),
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
    columns={row[1] for row in db.execute('PRAGMA table_info(room_channel_owner_inputs)')}
    for name in ('scope','scope_digest'):
        if name not in columns:
            db.execute("ALTER TABLE room_channel_owner_inputs ADD COLUMN "+name+" TEXT NOT NULL DEFAULT ''")


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
            renewal=body.get('renewal')
            if existing['owner_user_id']!=principal.user_id or existing['space_id']!=principal.space_id:
                raise DevError('WEIXIN_ACCOUNT_CONFLICT','此微信已由其他账号登记，不能在这里接入',409)
            if renewal and (renewal['id']!=existing['id'] or renewal['credential_version']!=existing['credential_version']):
                raise DevError('WEIXIN_ACCOUNT_CONFLICT','请明确恢复原账号，不覆盖其他绑定',409)
            if not args['confirm_replace_existing']:
                # Re-scanning a WeChat this owner already registered is the
                # common path after a room migration. It reuses the original
                # account identity, but only with an explicit confirmation.
                raise (DevError('WEIXIN_ACCOUNT_CONFLICT','请明确恢复原账号，不覆盖其他绑定',409) if renewal else
                    DevError('WEIXIN_ACCOUNT_REUSE_CONFIRM','此微信已登记过；请勾选“确认替换此账号的原连接”后继续',409))
            for binding in self.store.all("SELECT * FROM weixin_bindings WHERE account_id=? AND state!='revoked'",(existing['id'],)):
                if not binding.get('native_member_id'):
                    # A legacy route (often left paused by room migration)
                    # stops with the original safe-stop path; no work replays.
                    self.channels.stop_legacy(binding['conversation_id'],binding['id'],'weixin','revoke',binding['version'],
                        'reuse-account:'+digest([row['id'],binding['id'],binding['version']]),principal)
                    continue
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
            for binding in self.store.all("SELECT * FROM weixin_bindings WHERE account_id=? AND state!='revoked' AND native_member_id!=''",(identifier,)):
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
        member=(self.channels.member(conversation,args['member_id']) if args['member_id']
                else self.channels.flow_member(conversation,flow))
        if not member:
            raise DevError('CHANNEL_MEMBER_REQUIRED','请选择此房间的可用主责 dot，再继续连接',409)
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
             allow_replies,allow_media_outbound,expires_at,created,updated,pairing_secret,lifetime_mode,native_member_id,auto_confirm_pairing)
            VALUES (?,?,?,?,'',?,'','',?,?,?,'{}',?,?,'pairing',?,?,?,0,?,?,?,?,'until_revoked',?,?)''',
            (identifier,account['id'],principal.space_id,principal.user_id,conversation['id'],member.get('legacy_dot_id',''),
             canonical(owner),member.get('principal','{}'),account['credential_version'],account['generation']+1,
             digest(challenge),now+900,int(args['allow_replies']),now+86400,now,now,self.store.encrypt(challenge),member['id'],int(args.get('confirm_pairing_sender', False))))
        if args['allow_owner_tasks']:
            self.approve_tasks(identifier,args,principal,pending=True)
        elif args.get('confirm_follow_room'):
            binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (identifier,))
            self.consent_preview(binding, args, principal, pending=True)
            self.enable_follow_room(binding)
        return {'binding_id':identifier,'member_id':member['id']}

    def valid(self,binding,*,pairing=False):
        return self._valid(binding,pairing=pairing,require_member=True)

    @memoized
    def _binding_identity(self, binding, *, pairing=False, allow_disconnected=False):
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        allowed={'active','pairing','awaiting_owner'} if pairing else {'active'}
        if (binding['state'] not in allowed or
                (binding.get('lifetime_mode') != 'until_revoked' and binding['expires_at'] <= self.c.clock())
                or (binding['state'] == 'active' and (not binding['peer_id'] or binding['revalidation_required']))):
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已暂停、到期或解除',403)
        if binding['state']=='pairing' and binding['pairing_until']<=self.c.clock():
            raise DevError('WEIXIN_PAIRING_EXPIRED','原配对码已到期，请刷新配对码',403)
        self.c.weixin.require_enabled(binding)
        saved=json.loads(binding['owner_principal'])
        owner=refresh_principal(self.store,Principal(**{**saved,'scopes':set()}))
        owner,room=self.c.rooms.owner(owner,binding['conversation_id'])
        self.c.rooms.require_native(room)
        if room['state']!='active':
            raise DevError('ROOM_PAUSED','原房间已暂停',409)
        account=self.c.weixin.account(binding['account_id'],owner)
        allowed_account_states = {'active', 'attention'} if allow_disconnected else {'active'}
        if (binding['owner_user_id']!=owner.user_id or binding['space_id']!=owner.space_id
                or account['state'] not in allowed_account_states or account['generation']!=binding['generation']
                or account['credential_version']!=binding['credential_version']):
            raise DevError('WEIXIN_CREDENTIAL_CHANGED','原账号凭据或绑定世代已改变',403)
        return binding,account,owner,room

    def follows_room(self, binding):
        policy = getattr(self.c, 'room_permissions', None)
        return (binding.get('routing_mode') == 'follow_room'
                and (not policy or policy.channel_mode(binding['conversation_id'], 'weixin') != 'custom'))

    def bound_member(self, binding):
        return self.store.one('SELECT * FROM room_members WHERE id=? AND conversation_id=?',
                              (binding['native_member_id'], binding['conversation_id']))

    def recipient(self, binding, room):
        if self.follows_room(binding):
            return self.c.room_runtime.recipient(room, '')
        return self.bound_member(binding)

    def _valid(self, binding, *, pairing=False, require_member):
        identity = self._binding_identity(binding, pairing=pairing)
        member = self.bound_member(binding)
        if require_member and (not member or member['status'] in {'removed', 'paused'}):
            raise DevError('WEIXIN_MEMBER_REMOVED','原主责 dot 已移出或暂停',403)
        return (*identity, member)

    def inbound(self, binding_id, *, pairing=False):
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (binding_id,))
        identity = self._binding_identity(binding, pairing=pairing)
        # The actual pairing exchange retains the original member checks.
        # Ordinary active private chat intake keeps working while routing waits.
        if binding['state'] != 'active':
            return self._valid(binding, pairing=pairing, require_member=True)
        return (*identity, self.recipient(binding, identity[3]))

    def source_binding(self, source, *, allow_disconnected=False):
        body = json.loads(source['body'])
        origin = body.get('external_origin', {})
        binding = self.store.one('SELECT * FROM weixin_bindings WHERE id=?', (origin.get('binding_id'),))
        if binding and binding['state'] == 'paused':
            raise DevError('WEIXIN_BINDING_PAUSED','原微信绑定已暂停',409)
        identity = self._binding_identity(binding, allow_disconnected=allow_disconnected)
        if (source['origin'] != 'external_channel' or source['state'] != 'saved'
                or origin.get('channel') != 'weixin' or not origin.get('native_room')
                or origin.get('provider_deleted') or source['conversation_id'] != binding['conversation_id']
                or not binding['peer_id'] or origin.get('peer_id') != binding['peer_id']
                or origin.get('account_id') != binding['account_id']
                or origin.get('generation') != binding['generation']):
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原微信来源或私聊身份已改变',403)
        latest = self.store.one('''SELECT message_id,provider_version FROM weixin_inbound
            WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?''',
            (binding['account_id'], binding['generation'], binding['peer_id'], origin.get('external_message_id')))
        if (not latest or latest['message_id'] != source['id']
                or latest['provider_version'] != origin.get('provider_version')):
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED','原微信消息已有其他版本',403)
        member = self.store.one('SELECT * FROM room_members WHERE id=? AND conversation_id=?',
                                (body.get('room_member_id', ''), source['conversation_id']))
        return (*identity, member or self.recipient(binding, identity[3]))

    @memoized
    def preview(self,binding,principal):
        follow = self.follows_room(binding)
        conversation_id = binding['conversation_id']
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (conversation_id,))
        member = self.recipient(binding, room)
        scopes = self.c.room_work_bridge.scopes(member) if member else []
        policy=getattr(self.c,'room_permissions',None)
        configured=bool(policy and policy.is_configured(conversation_id))
        mode=policy.channel_mode(conversation_id,'weixin') if configured else 'room_member'
        if configured:
            rules={(r['project_id'],r['environment_id']):r for r in policy.effective_scopes(binding['conversation_id'],'weixin')}
            effective=[]
            for row in scopes:
                rule=rules.get((row['project_id'],row['environment_id']))
                if not rule:
                    continue
                caps=sorted(set(json.loads(row['capabilities'])) & set(rule['capabilities']))
                targets=sorted(set(json.loads(row['execution_targets'])) & set(rule['execution_targets'])) if 'execute' in caps else []
                if caps:
                    scoped={**row,'capabilities':canonical(caps),'execution_targets':canonical(targets)}
                    deadline=rule['lifetime']['expires_at']
                    if deadline is not None:
                        scoped['expires_at']=min(row['expires_at'],deadline) if row['lifetime_mode']=='timed' else deadline
                        scoped['lifetime_mode']='timed'
                    effective.append(scoped)
            scopes=effective
        projects = [{'project_id': s['project_id'], 'environment_id': s['environment_id'], 'version': s['version'],
            'capabilities': json.loads(s['capabilities']), 'execution_targets': json.loads(s['execution_targets']),
            'scope_digest': self.c.room_work_bridge.fingerprint([s])} for s in scopes]
        if follow and not member and policy:
            policy_row = policy.row(conversation_id)
            projects = [{**r, 'version': policy_row['version'] if policy_row else 0, 'scope_digest': digest(r)}
                        for r in policy.effective_scopes(conversation_id, 'weixin')]
        scope={'scope_mode':'room_member','binding_id':binding['id'],'account_id':binding['account_id'],'credential_version':binding['credential_version'],
            'generation':binding['generation'],'member_id':binding['native_member_id'],'conversation_id':conversation_id,
            'owner_user_id':principal.user_id,'space_id':principal.space_id,'projects':projects}
        if binding.get('routing_mode') == 'follow_room':
            scope['routing_mode'] = 'follow_room'
        display_projects = [{**project, 'expires_at': row['expires_at'] if row['lifetime_mode'] == 'timed' else None}
                            for project, row in zip(projects, scopes)] if member else projects
        return {'scope':scope,'scope_digest':digest(scope),'text_version':'room-channel-consent-v2',
                'permission_mode':mode,'project_ids':[s['project_id'] for s in projects],'projects':display_projects,
                'summary':('使用房间权限设置中的微信独立权限，并保留原指定 dot。' if mode=='custom' else
                    '跟随此房间当前主责 dot 和房间权限。工作结果回到已配对的本人私聊，连接持续至解除。' if follow else
                    '跟随房间权限设置，并保留原指定 dot。' if configured else
                    '使用微信绑定成员已批准的项目权限，连接持续至解除。'),
                'lifetime_mode':'until_revoked'}

    @staticmethod
    def scope_identity(scope):
        return {k:v for k,v in scope.items() if k not in {'projects', 'routing_mode'}}

    def current_scope(self,binding,principal,saved):
        current=dict(self.preview(binding,principal)['scope'])
        # Older authorizations keep their exact frozen scope until the owner
        # explicitly approves the new inheritance contract in the panel.
        if 'scope_mode' not in saved:
            current.pop('scope_mode',None)
        return current

    def approved_scope(self,auth,binding,principal):
        saved=json.loads(auth['scope'])
        if digest(saved)!=auth['scope_digest']:
            return None
        current=self.current_scope(binding,principal,saved)
        policy=getattr(self.c,'room_permissions',None)
        configured=bool(policy and policy.is_configured(binding['conversation_id']))
        if saved.get('scope_mode')=='room_member' or configured:
            # Saving the canonical room policy explicitly controls project
            # permissions. The channel record continues to prove identity,
            # lifetime and revocation, not a second editable project policy.
            return current if self.scope_identity(current)==self.scope_identity(saved) else None
        return current if current==saved else None

    def input_scope(self,message,auth):
        saved=json.loads(auth['scope'])
        record=self.store.one('SELECT * FROM room_channel_owner_inputs WHERE message_id=?',(message['id'],))
        if saved.get('scope_mode')!='room_member' and record and not record['scope']:
            return saved
        scope=json.loads(record['scope']) if record and record['scope'] else {}
        if (not record or digest(scope)!=record['scope_digest']
                or self.scope_identity(scope)!=self.scope_identity(saved)):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','原微信消息的房间权限记录已不可用',403)
        return scope

    def consent_preview(self, binding, args, principal, *, pending):
        follow = bool(args.get('confirm_follow_room'))
        proposed = {**binding, 'routing_mode': 'follow_room'} if follow else binding
        if follow and not self.follows_room(proposed):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','微信独立权限模式保留原指定 dot',409)
        preview = self.preview(proposed, principal)
        # At the pre-binding step, the binding ID does not exist yet and
        # the displayed account is still on its prior provider generation.
        scope = preview['scope']
        if pending:
            scope = {**scope, 'binding_id': '', 'generation': binding['generation'] - 1}
        if args['expected_scope_digest'] != digest(scope):
            raise DevError('CHANNEL_OWNER_SCOPE_CHANGED','微信任务范围已改变，请重新核对',409)
        return preview

    def enable_follow_room(self, binding):
        if binding.get('routing_mode') != 'follow_room':
            self.store.execute("UPDATE weixin_bindings SET routing_mode='follow_room' WHERE id=?", (binding['id'],))
            fence_follow_poll(self.c, binding['conversation_id'])

    def approve_tasks(self,binding_id,args,principal,*,pending=False):
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(binding_id,))
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        self.require_room(binding['conversation_id'])
        follow = bool(args.get('confirm_follow_room'))
        preview = self.consent_preview(binding, args, principal, pending=pending)
        if not preview['scope']['projects']:
            if follow and not pending:
                self.enable_follow_room(binding)
                return
            raise DevError('CHANNEL_TASK_PROJECT_REQUIRED','先明确此成员可工作的项目，再开通微信交办',409)
        if (not all(args.get('confirm_'+key) for key in ('owner_identity','future_tasks','result_delivery','until_revoked','pairing_notices'))
                or not binding['allow_replies']):
            raise DevError('CHANNEL_CONSENT_REQUIRED','本人任务范围和回传需明确确认',422)
        current=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?',(binding_id,))
        if follow:
            self.enable_follow_room(binding)
        # An explicit routing opt-in changes only new routing. Preserve an
        # uninterrupted existing task approval for already assigned inputs.
        previous_scope = json.loads(current['scope']) if current else {}
        preserve = (not pending and current and current['state'] == 'active'
                    and previous_scope.get('scope_mode') == 'room_member'
                    and digest(previous_scope) == current['scope_digest']
                    and self.scope_identity(previous_scope) == self.scope_identity(preview['scope']))
        owner=asdict(principal);owner.update(scopes=[],session_hash='',token_hash='')
        self.store.execute('''INSERT INTO room_channel_owner_authorizations
            (id,binding_id,member_id,state,scope,scope_digest,owner_principal,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET state=excluded.state,
            scope=excluded.scope,scope_digest=excluded.scope_digest,owner_principal=excluded.owner_principal,
            version=version+?,created=CASE WHEN ? THEN room_channel_owner_authorizations.created ELSE excluded.created END,
            updated=excluded.updated''',
            (current['id'] if current else uuid.uuid4().hex,binding_id,binding['native_member_id'],
             'pending_pairing' if pending else 'active',canonical(preview['scope']),preview['scope_digest'],canonical(owner),
             self.c.clock(),self.c.clock(),int(not preserve),bool(preserve)))

    def pairing_scope_matches(self, binding, principal):
        authorization = self.store.one(
            "SELECT scope_digest FROM room_channel_owner_authorizations WHERE binding_id=? AND state='pending_pairing'",
            (binding['id'],))
        return not authorization or authorization['scope_digest'] == self.preview(binding, principal)['scope_digest']

    def activate_peer(self, binding, peer):
        now = self.c.clock()
        self.store.execute("""UPDATE weixin_bindings SET state='active',peer_id=?,pending_peer_id='',
            pairing_hash='',pairing_secret='',revalidation_required=0,version=version+1,updated=? WHERE id=?""",
            (peer, now, binding['id']))
        self.store.execute("""UPDATE room_channel_owner_authorizations SET state='active',updated=?
            WHERE binding_id=? AND state='pending_pairing'""", (now, binding['id']))

    def control(self,binding,args,principal):
        action=args['action']
        if action not in {'pause','disconnect','migrate','cancel'}:
            self.require_room(binding['conversation_id'])
        if action=='confirm_owner':
            if binding['state']!='awaiting_owner' or not binding['pending_peer_id'] or not hmac.compare_digest(args['confirm_peer_id'],binding['pending_peer_id']):
                raise DevError('WEIXIN_PAIRING_UNVERIFIED','请确认当前配对返回的本人联系人',409)
            if binding['peer_id'] and binding['peer_id']!=binding['pending_peer_id']:
                raise DevError('CHANNEL_OWNER_PEER_CHANGED','恢复必须由原联系人验证',403)
            if not self.pairing_scope_matches(binding, principal):
                self.revoke_tasks(binding['id'], principal)
            self.activate_peer(binding, binding['pending_peer_id'])
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
            recover_waiting(self.c, conversation_id=binding['conversation_id'])
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
                or original['coordinator_member_id']!=body.get('room_member_id', binding['native_member_id'])
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

    def author(self,message,*,project_ids=None,goal=None,allow_disconnected=False):
        authority = self.c.room_work_bridge.native.input_authority
        if authority.proof(message, goal=goal):
            if not goal:
                *_, member = self.source_binding(message, allow_disconnected=allow_disconnected)
                if not member or member['status'] in {'removed', 'paused'}:
                    raise DevError('WEIXIN_MEMBER_REMOVED','原主责 dot 已暂停或移出',403)
            return authority.author(message, project_ids=project_ids, goal=goal)
        return self._author(message,project_ids=project_ids,goal=goal,allow_disconnected=allow_disconnected)

    def _author(self,message,*,project_ids=None,goal=None,recovery_owner=None,allow_disconnected=False):
        body=json.loads(message['body']);origin=body.get('external_origin',{})
        record=self.store.one('SELECT * FROM room_channel_owner_inputs WHERE message_id=?',(message['id'],))
        auth=self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE id=?',(record['authorization_id'],)) if record else None
        if not record:
            reason=body.get('owner_task_blocker','CHANNEL_OWNER_NOT_APPROVED')
            detail={
                'WEIXIN_SOURCE_TIME_MISSING':'微信没有提供这条原消息的发送时间，无法核验它是在授权后发送的。',
                'WEIXIN_SOURCE_BEFORE_APPROVAL':'这条消息早于微信本人交办授权。请在原微信发送一条新请求。',
                'CHANNEL_OWNER_SOURCE_CHANGED':'这条微信消息已修订或删除。请在原微信发送一条新请求。',
                'CHANNEL_OWNER_SCOPE_DENIED':'微信入口当时没有可交办的项目。请查看房间权限设置。',
            }
            raise DevError(reason,detail.get(reason,'原私聊消息没有有效的本人任务批准'),403)
        if not auth or auth['state']!='active' or auth['version']!=record['authorization_version']:
            raise DevError('CHANNEL_OWNER_NOT_APPROVED','原微信本人任务批准已撤销或更改',403)
        if record['message_digest']!=digest(body.get('body_text','')):
            raise DevError('CHANNEL_OWNER_NOT_APPROVED','原微信消息正文已改变',403)
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(auth['binding_id'],))
        if recovery_owner is not None:
            self.goal_source(goal,message,binding)
            recovered=True
        else:
            recovered=self.recovered_goal(goal,message,binding) if goal else None
        _,_,owner,_,member=self.source_binding(message,allow_disconnected=allow_disconnected)
        if not recovered and (not member or member['status'] in {'removed','paused'}):
            raise DevError('WEIXIN_MEMBER_REMOVED','原消息指定的成员已暂停或移出',403)
        authorized=json.loads(auth['scope'])
        current=self.current_scope(binding,owner,authorized)
        saved=self.input_scope(message,auth)
        scope_matches=current==saved
        if project_ids is not None:
            # Existing Goals may continue their unchanged project rules after
            # an unrelated rule is removed. This is never used to trust a new
            # inbound input or to add authority outside the original consent.
            old_rules={(r['project_id'],r['environment_id']):r for r in saved['projects'] if r['project_id'] in project_ids}
            new_rules={(r['project_id'],r['environment_id']):r for r in current['projects'] if r['project_id'] in project_ids}
            scope_matches=(bool(project_ids) and self.scope_identity(current)==self.scope_identity(saved)
                and set(project_ids)<={key[0] for key in old_rules} and old_rules==new_rules)
        if (not scope_matches or digest(authorized)!=auth['scope_digest']
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
        for source in self.store.all('''SELECT DISTINCT m.* FROM collaboration_messages m
                JOIN room_channel_owner_inputs i ON i.message_id=m.id
                JOIN delegation_requests d ON d.message_id=m.id
                JOIN coordination_goals g ON g.id=d.goal_id
                WHERE i.authorization_id=? AND g.state='paused' ''', (auth['id'],)):
            self.c.weixin.delivery.queue_task_status(source, 'CHANNEL_OWNER_REVOKED')
        members = self.store.all('''SELECT DISTINCT m.* FROM room_members m
            JOIN dot_mailbox_entries e ON e.member_id=m.id AND e.kind='message'
            JOIN room_channel_owner_inputs i ON i.message_id=e.object_id
            WHERE i.authorization_id=? AND m.conversation_id=? AND m.status NOT IN ('removed','legacy')''',
            (auth['id'], binding['conversation_id']))
        for member in members:
            self.c.room_consumers.enqueue_ref(member,'control_notice',auth['id'],auth['version']+1,
                                              {'reason_code':'channel_owner_revoked'})
        return {'work_permission_revoked':True,'chat_connected':binding['state']=='active',
            'affected_work_ids':[goal['id'] for goal in goals]}

    def ingest(self,binding,account,owner,room,member,message,*,deferred=None):
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
                or created!='0' and prior['provider_created'] not in ('', '0') and created!=prior['provider_created']):
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
            # The fresh one-time secret is itself the freshness proof. Provider
            # update stamps and Hub/provider clock skew must not reject it.
            if not prior and not int(deleted) and hmac.compare_digest(digest(pairing_text(content)),binding['pairing_hash']):
                if binding['peer_id'] and binding['peer_id']!=peer:
                    disposition='different_peer_quarantined'
                elif binding.get('auto_confirm_pairing') and self.pairing_scope_matches(binding, owner):
                    self.activate_peer(binding, peer)
                    disposition='pairing_confirmed'
                else:
                    self.store.execute("UPDATE weixin_bindings SET state='awaiting_owner',pending_peer_id=?,pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?",
                        (peer,self.c.clock(),binding['id']))
                    disposition='pairing_waits_for_panel_owner'
            else:
                disposition='pairing_code_mismatch'
        elif PAIRING_CODE.fullmatch(pairing_text(content)):
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
            original = newest and not (prior or int(updated) or int(deleted))
            replacement = None
            if newest and prior and prior['message_id'] and not int(deleted):
                # An already-delivered conversation can show its newest
                # revision as chat. This replaces an unhandled slot and
                # preserves the original recipient across lead changes.
                replacement = self.store.one('''SELECT m.* FROM dot_mailbox_entries e
                    JOIN room_members m ON m.id=e.member_id
                    WHERE e.object_id=? AND e.kind='message' AND e.acknowledged_at IS NULL
                    AND e.superseded_by='' AND m.conversation_id=?
                    AND e.generation=('room:' || m.id || ':' || m.binding_generation)
                    ORDER BY e.sequence LIMIT 1''', (prior['message_id'], room['id']))
                if replacement:
                    member = replacement
            dispatchable = original or bool(replacement)
            if original:
                pending = self.store.one('''SELECT COUNT(*) n FROM (
                    SELECT w.message_id FROM room_waiting_inputs w WHERE w.conversation_id=?
                    UNION
                    SELECT e.object_id FROM dot_mailbox_entries e JOIN room_members m ON m.id=e.member_id
                    WHERE m.conversation_id=? AND e.kind='message' AND e.acknowledged_at IS NULL AND e.superseded_by=''
                    AND e.generation=('room:' || m.id || ':' || m.binding_generation))''', (room['id'], room['id']))['n']
                if pending >= MAX_PENDING_NATIVE_INPUTS:
                    raise DevError('WEIXIN_INBOX_QUOTA','原 dot 未处理消息达到积压上限，保留原游标',429)
            message_id=uuid.uuid4().hex
            if int(deleted):
                content,attachment_items='[微信消息已删除]',[]
            if not content:
                content='[附件消息]'
            content=incoming_text(content,24000)
            route = route_input(self.c, room, member)
            if not dispatchable:
                route = {'state': 'terminal', 'member_id': route['member_id'], 'reason_code': 'source_unavailable'}
            body={'body_text':redact(content),'room_member_id':route['member_id'],'room_routing':route,
                'external_origin':{'channel':'weixin','native_room':True,'binding_id':binding['id'],'account_id':account['id'],
                    'generation':binding['generation'],'peer_id':peer,'external_message_id':external_id,'provider_version':version,
                    'provider_created':created,'provider_deleted':bool(int(deleted)),'revision_is_latest_at_commit':newest},
                'requires_owner_execution_request':True}
            auth=self.store.one("SELECT * FROM room_channel_owner_authorizations WHERE binding_id=? AND state='active'",(binding['id'],))
            scope=None
            if not auth:
                body['owner_task_blocker']='CHANNEL_OWNER_NOT_APPROVED'
            elif prior or not newest or int(updated) or int(deleted):
                body['owner_task_blocker']='CHANNEL_OWNER_SOURCE_CHANGED'
            elif not int(created):
                body['owner_task_blocker']='WEIXIN_SOURCE_TIME_MISSING'
            elif int(created)<int(auth['created']*1000):
                body['owner_task_blocker']='WEIXIN_SOURCE_BEFORE_APPROVAL'
            else:
                if deferred is None:
                    scope = self.approved_scope(auth, binding, owner)
                elif (deferred.get('evidence') and deferred.get('authorization_id') == auth['id']
                        and deferred.get('authorization_version') == auth['version']):
                    scope = deferred.get('scope')
                if not scope or not scope['projects']:
                    body['owner_task_blocker']='CHANNEL_OWNER_SCOPE_DENIED'
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
            if scope and scope['projects']:
                # Freeze the room-approved scope for this input. Later
                # room expansion applies only to new messages, never to
                # queued inputs or an already approved Goal.
                self.store.execute('''INSERT INTO room_channel_owner_inputs
                    (message_id,authorization_id,authorization_version,message_digest,created,scope,scope_digest)
                    VALUES (?,?,?,?,?,?,?)''',(message_id,auth['id'],auth['version'],digest(body['body_text']),
                    self.c.clock(),canonical(scope),digest(scope)))
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
        if message_id:
            canonical_message = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message_id,))
            follow = self.follows_room(binding) and not replacement
            if newest and scope and scope['projects']:
                authority = self.c.room_work_bridge.native.input_authority
                proof_member = self.evidence_recipient(member, follow)
                if deferred is None:
                    authority.capture(canonical_message, owner, proof_member)
                else:
                    authority.capture(canonical_message, owner, proof_member, evidence=deferred['evidence'])
            if newest and prior:
                retire_input(self.c, prior['message_id'], message_id)
            record_input(self.c, canonical_message, member, owner, follow_lead=follow, allow_dispatch=dispatchable)
            self.c.rooms.change(room['id'], 'message', message_id, 1)
        return message_id

    @staticmethod
    def evidence_recipient(member, follow):
        return None if follow and member and member['status'] in BLOCKED_MEMBERS else member

    def deferred_evidence(self, binding, owner, room, member, message):
        created = int(uint64(message.get('create_time_ms', 0)))
        peer = message.get('from_user_id')
        auth = self.store.one('''SELECT * FROM room_channel_owner_authorizations
            WHERE binding_id=? AND state='active' ''', (binding['id'],))
        if (not auth or not isinstance(peer, str) or not hmac.compare_digest(peer, binding['peer_id'])
                or not created or created < int(auth['created'] * 1000)
                or int(uint64(message.get('update_time_ms', 0))) or int(uint64(message.get('delete_time_ms', 0)))):
            return {}
        scope = self.approved_scope(auth, binding, owner)
        if not scope or not scope['projects']:
            return {}
        authority = self.c.room_work_bridge.native.input_authority
        proof_member = self.evidence_recipient(member, self.follows_room(binding))
        evidence = authority.ingress_evidence(owner, room, proof_member, binding=binding, channel_scope=scope, auth=auth)
        return {'authorization_id': auth['id'], 'authorization_version': auth['version'],
                'scope': scope, 'evidence': evidence}

    def delivery_validate(self,delivery,*,require_task_authority=True,allow_disconnected=False):
        binding=self.store.one('SELECT * FROM weixin_bindings WHERE id=?',(delivery['binding_id'],))
        if not binding:
            raise DevError('WEIXIN_BINDING_INACTIVE','原微信绑定已不可用',403)
        message=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',(delivery['message_id'],binding['conversation_id']))
        source=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',(delivery['source_message_id'],binding['conversation_id']))
        if not message or not source:
            raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED','原房间消息已不可用',403)
        binding,account,owner,room,member=self.reply_binding(
            binding,message,source,allow_disconnected=allow_disconnected)
        body=json.loads(message['body']);origin=json.loads(source['body']).get('external_origin',{})
        self.c.chatroom.require_message_visible(owner,message)
        if (not binding['allow_replies'] or delivery['generation']!=binding['generation'] or delivery['peer_id']!=binding['peer_id']
                or not self.sender_matches(binding,message,source,allow_disconnected=allow_disconnected)
                or message['version']!=delivery['message_version'] or digest(body)!=delivery['message_digest']
                or origin.get('binding_id')!=binding['id'] or origin.get('generation')!=binding['generation'] or origin.get('peer_id')!=binding['peer_id']):
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原微信回复内容或收件身份已改变',403)
        if require_task_authority and self.store.one('SELECT 1 FROM room_channel_owner_inputs WHERE message_id=?',(source['id'],)):
            self.reply_author(message,source)
        return binding,account,owner,message

    def reply_binding(self,binding,message,source,*,allow_disconnected=False):
        identity = self.source_binding(source, allow_disconnected=allow_disconnected)
        if not binding or identity[0]['id'] != binding['id']:
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原微信回复绑定已改变',403)
        member = identity[-1]
        if not member or member['status'] in {'removed', 'paused'}:
            goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (message['goal_id'],)) if message.get('goal_id') else None
            if not self.recovered_goal(goal, source, binding):
                raise DevError('WEIXIN_MEMBER_REMOVED','原主责 dot 已暂停或移出',403)
        return identity

    def current_conversation_reply(self, message, source):
        """The explicit protocol can reply to a new input about existing work."""
        body = json.loads(message['body'])
        if (type(body.get('publish_work')) is not bool
                or source['id'] != message.get('reply_to_id')
                or source['id'] != body.get('source_message_id')):
            return False
        if not body['publish_work']:
            return True
        goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (message.get('goal_id'),))
        native = self.c.room_work_bridge.native
        reference = native.reference(goal) if goal else None
        return bool(reference and native.original(reference)['source_message_id'] != source['id'])

    def conversation_reply_author(self, body, source, *, allow_disconnected=False):
        projects = set(body.get('provenance_project_ids', []))
        scopes = {(scope['project_id'], scope['environment_id']) for scope in body.get('provenance_scopes', [])}
        author = self.author(source, project_ids=projects or None, allow_disconnected=allow_disconnected)
        if not author['authenticated']:
            raise DevError('CHANNEL_OWNER_NOT_APPROVED', '此微信来源没有有效的工作回传授权', 403)
        if not projects:
            return author
        binding, _, owner, _, member = self.source_binding(source, allow_disconnected=allow_disconnected)
        authority = self.c.room_work_bridge.native.input_authority
        proof = authority.proof(source, member)
        if proof:
            rules = authority.rules(source, member, proof=proof, keys=scopes)
            readable = {key for key, rule in rules.items() if 'read' in rule['capabilities']}
        else:
            auth = self.store.one('SELECT * FROM room_channel_owner_authorizations WHERE id=?',
                                  (author['authorization_id'],))
            current = self.current_scope(binding, owner, json.loads(auth['scope']))
            readable = {(rule['project_id'], rule['environment_id']) for rule in current['projects']
                        if 'read' in rule['capabilities']}
        if projects != {project for project, _ in scopes} or not scopes <= readable:
            raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED', '原微信回复未获这些项目环境的外发许可', 403)
        return author

    def reply_author(self,message,source,*,allow_disconnected=False):
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(message.get('goal_id'),)) if message.get('goal_id') else None
        native=self.c.room_work_bridge.native
        if goal and (reference := native.reference(goal)):
            original = native.original(reference)
            if (source['id']!=original['source_message_id'] or source['conversation_id']!=original['conversation_id']
                    or source['version']!=original['source_version']
                    or digest(json.loads(source['body']).get('body_text',''))!=original['source_digest']):
                raise DevError('WEIXIN_DELIVERY_SOURCE_CHANGED','原工作回复的输入证据已改变',403)
            projects = set(json.loads(message['body']).get('provenance_project_ids', []))
            if not projects or not projects <= set(native.record(reference)['project_ids']):
                raise DevError('WEIXIN_DELIVERY_SCOPE_CHANGED','原工作回复缺少有效项目来源',403)
            if native.input_authority.proof(source, goal=goal):
                native.require_delivery_projects(goal, source, projects)
            return native.owner_author(goal, source)
        if self.store.one('SELECT 1 FROM room_channel_owner_inputs WHERE message_id=?',(source['id'],)):
            return self.author(source, allow_disconnected=allow_disconnected)
        return self.c.channel_owner.author(source)

    def sender_matches(self,binding,message,source=None,*,allow_disconnected=False):
        body=json.loads(message['body'])
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(message.get('goal_id'),)) if message.get('goal_id') else None
        if (goal and self.c.room_work_bridge.native.reference(goal)
                and not (source and self.current_conversation_reply(message, source))):
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
                if (body.get('publish_work') is True and message['reply_to_id'] == body.get('source_message_id')
                        and self.store.one('''SELECT 1 FROM room_goal_publications
                            WHERE goal_id=? AND intent_revision=? AND message_id=?''',
                            (goal['id'], revision, message['id']))):
                    return True
                return bool(intent and body.get('source_message_id')==intent['source_message_id']
                    and message['reply_to_id']==intent['source_message_id'])
        if source and json.loads(source['body']).get('external_origin', {}).get('native_room'):
            try:
                *_, member = self.source_binding(source, allow_disconnected=allow_disconnected)
            except DevError:
                return False
        else:
            member = self.bound_member(binding)
        if not member or message['author']!=member['grant_id'] or message['conversation_id']!=member['conversation_id']:
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
