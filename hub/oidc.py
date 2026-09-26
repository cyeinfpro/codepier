"""OIDC relying party for human login, separate from CodePier's MCP issuer.

Discovery and Tokens use configured origins, TLS, bounded HTTP and no redirects.
PyJWT/cryptography verify asymmetric signatures; this module enforces the OIDC
transaction, claims, account-linking and entitlement lifecycle around that check.
Provider credentials and upstream Tokens are encrypted with the existing key.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import Field, model_validator
from typing import Literal

from hub.access import AccessModel
from hub import iam
from shared.crypto import digest, token
from shared.util import DevError

ALGORITHMS = {'RS256','RS384','RS512','ES256','ES384','ES512'}
MAX_HTTP_BYTES = 1024 * 1024
SKEW = 30
LOGIN_TTL = 600
LOGOUT_EVENT = 'http://schemas.openid.net/event/backchannel-logout'
KEY_REFRESH_COOLDOWN = 60
METADATA_FAILURE_COOLDOWN = 30
LOGIN_CLIENT_LIMIT = 32
LOGIN_PROVIDER_LIMIT = 256
LOGIN_PUBLIC_LIMIT = 872
LOGIN_TOTAL_LIMIT = 1000


class SigningKeyRefreshRequired(DevError):
    """Internal retry category; semantic Token failures never request new keys."""
    def __init__(self):
        super().__init__('OIDC_TOKEN_INVALID','身份提供者返回的身份凭据未通过校验',401)



def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def endpoint(value):
    if not isinstance(value,str) or not value or len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value) or '\\' in value:
        raise ValueError('Invalid OIDC URL')
    u=urlsplit(value)
    if not u.hostname or u.username is not None or u.password is not None or '#' in value or (u.port is not None and not 1<=u.port<=65535):raise ValueError('Invalid OIDC URL')
    test_http=(os.getenv('CODEPIER_OIDC_INSECURE_TEST_LOOPBACK')=='1' and u.scheme=='http' and u.hostname in {'127.0.0.1','localhost','::1'})
    if u.scheme!='https' and not test_http:raise ValueError('OIDC endpoints require HTTPS')
    return u


def origin(value):
    u=endpoint(value)
    return f'{u.scheme}://{u.netloc}'


def safe_return(value):
    # Only the existing panel OAuth continuation and hash navigation are valid.
    if not isinstance(value,str) or len(value)>1000 or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c)<32 for c in value):
        raise DevError('INVALID_RETURN','无效登录返回路径',400)
    u=urlsplit(value)
    if u.scheme or u.netloc or u.path!='/':raise DevError('INVALID_RETURN','只能返回 CodePier 面板',400)
    if u.query and not re.fullmatch(r'authorize=[A-Za-z0-9_-]{1,200}',u.query):raise DevError('INVALID_RETURN','无效授权继续参数',400)
    return value


class ProviderInput(AccessModel):
    label: str = Field(min_length=1,max_length=80)
    issuer: str = Field(min_length=8,max_length=2048)
    client_id: str = Field(min_length=1,max_length=500)
    client_secret: str | None = Field(default=None,min_length=1,max_length=4000,repr=False)
    discovery_url: str = Field(default='',max_length=2048)
    enabled: bool = False
    admission: Literal['closed','jit'] = 'closed'
    group_claim: str = Field(default='groups',min_length=1,max_length=100)
    required_group: str = Field(default='',max_length=200)
    scopes: str = Field(default='openid profile',max_length=500)
    endpoint_origins: list[str] = Field(default_factory=list,max_length=10)
    freshness_seconds: int = Field(default=900,ge=60,le=86400)
    expected_version: int | None = Field(default=None,ge=1)

    @model_validator(mode='after')
    def urls(self):
        endpoint(self.issuer)
        if '?' in self.issuer:raise ValueError('Issuer must be exact, without query')
        self.discovery_url=self.discovery_url or self.issuer.rstrip('/')+'/.well-known/openid-configuration'
        endpoint(self.discovery_url)
        allowed={origin(self.issuer),*self.endpoint_origins}
        for value in self.endpoint_origins:
            if origin(value)!=value or urlsplit(value).path:raise ValueError('Use exact origins, not paths/wildcards')
        if origin(self.discovery_url) not in allowed:raise ValueError('Discovery origin is not approved')
        scopes=self.scopes.split()
        if 'openid' not in scopes or any(not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}',s) for s in scopes):raise ValueError('Invalid OIDC scopes')
        self.scopes=' '.join(dict.fromkeys(scopes))
        return self


class GroupMappingInput(AccessModel):
    group_name: str = Field(min_length=1,max_length=200)
    space_id: str = Field(min_length=1,max_length=100)
    level: Literal['admin','member','guest'] = 'member'
    role_id: str | None = Field(default=None,max_length=100)
    may_delegate: bool = False
    expected_version: int | None = Field(default=None,ge=1)


class LinkInput(AccessModel):
    return_to: str = '/#identity'


def public_provider(row):
    return {k:row[k] for k in ('id','label','issuer','client_id','discovery_url','enabled','admission','group_claim','required_group','scopes','freshness_seconds','version')} | {
        'endpoint_origins':json.loads(row['endpoint_origins']), 'has_client_secret':bool(row['client_secret'])}


def validate_claims(encoded,provider,jwks,*,nonce=None,access_token=None,logout=False):
    """Never accept a client algorithm, key URL, issuer or audience as policy."""
    try:
        if not isinstance(encoded,str) or not 1<=len(encoded)<=65536:raise ValueError('Invalid token size')
        header=jwt.get_unverified_header(encoded)
        algorithm=header.get('alg')
        if algorithm not in ALGORITHMS or any(k in header for k in ('jku','x5u','crit')):raise ValueError('Unsupported JWT header')
        kid = header.get('kid')
        if kid is not None and (not isinstance(kid,str) or not 1<=len(kid)<=256):
            raise ValueError('Invalid key ID')
        keys=jwks.get('keys') if isinstance(jwks,dict) else None
        if not isinstance(keys,list) or not 1<=len(keys)<=32:raise ValueError('Invalid keyset')
        candidates=[k for k in keys if isinstance(k,dict) and k.get('use','sig')=='sig' and (not k.get('key_ops') or 'verify' in k['key_ops']) and (not k.get('alg') or k['alg']==algorithm) and (not header.get('kid') or k.get('kid')==header['kid'])]
        if not candidates and kid:
            raise SigningKeyRefreshRequired()
        if len(candidates)!=1:raise ValueError('Ambiguous or missing signing key')
        jwk=candidates[0]
        if any(k in jwk for k in ('d','p','q','dp','dq','qi','k')):raise ValueError('Public verification key required')
        key=jwt.PyJWK.from_dict(jwk,algorithm=algorithm).key
        if algorithm.startswith('RS') and getattr(key,'key_size',0)<2048:raise ValueError('RSA key too small')
        required=['iss','aud','iat']+(['jti'] if logout else ['exp','sub'])
        claims=jwt.decode(encoded,key=key,algorithms=[algorithm],issuer=provider['issuer'],audience=provider['client_id'],leeway=SKEW,options={'require':required})
        now=time.time()
        for name in ('iat','exp','nbf','auth_time'):
            if name in claims and (type(claims[name]) not in (int,float) or not math.isfinite(claims[name])):raise ValueError('Invalid time claim')
        if claims['iat']>now+SKEW:raise ValueError('Future issued-at')
        audiences=claims['aud']
        if not isinstance(audiences,(str,list)) or isinstance(audiences,list) and (not audiences or any(not isinstance(v,str) for v in audiences)):raise ValueError('Invalid audience')
        if isinstance(audiences,list) and len(audiences)>1 and claims.get('azp')!=provider['client_id']:raise ValueError('Missing authorized party')
        if 'azp' in claims and claims['azp']!=provider['client_id']:raise ValueError('Wrong authorized party')
        if 'sub' in claims:
            sub=claims['sub']
            if not isinstance(sub,str) or not 1<=len(sub)<=255 or not sub.isascii() or any(ord(c)<32 or ord(c)==127 for c in sub):raise ValueError('Invalid subject')
        if 'sid' in claims and (not isinstance(claims['sid'],str) or not 1<=len(claims['sid'])<=500 or any(ord(c)<32 for c in claims['sid'])):raise ValueError('Invalid session ID')
        if logout:
            if claims['iat']<now-LOGIN_TTL or 'nonce' in claims or not isinstance(claims.get('events'),dict) or claims['events'].get(LOGOUT_EVENT)!= {}:raise ValueError('Invalid logout event')
            if not claims.get('sub') and not claims.get('sid'):raise ValueError('Logout needs session or subject')
            if not isinstance(claims['jti'],str) or not 1<=len(claims['jti'])<=256:raise ValueError('Invalid logout ID')
        elif nonce is not None:
            if not isinstance(claims.get('nonce'),str) or not hmac.compare_digest(claims['nonce'],nonce):raise ValueError('Invalid nonce')
        if not logout and 'at_hash' in claims:
            if not isinstance(access_token,str):raise ValueError('Missing access token')
            bits=algorithm[-3:];hashed=getattr(hashlib,'sha'+bits)(access_token.encode('ascii')).digest()
            expected=b64(hashed[:len(hashed)//2])
            if not isinstance(claims['at_hash'],str) or not hmac.compare_digest(claims['at_hash'],expected):raise ValueError('Invalid access token hash')
        return claims
    except jwt.InvalidSignatureError as exc:
        raise SigningKeyRefreshRequired() from exc
    except (jwt.PyJWTError,ValueError,TypeError,KeyError,AttributeError,UnicodeError) as exc:
        # Never echo claims, Tokens or provider response bodies.
        raise DevError('OIDC_TOKEN_INVALID','身份提供者返回的身份凭据未通过校验',401) from exc


class OIDCService:
    def __init__(self,auth,runtime,public_url,*,transport=None):
        self.auth,self.runtime,self.store,self.public_url=auth,runtime,runtime.store,public_url
        self.transport=transport
        self.sync_lock=asyncio.Lock()
        from hub.oidc_resilience import SyncScheduler
        self.scheduler=SyncScheduler(self)
        self.metadata_locks={};self.key_locks={}
        self.metadata_failures={};self.key_refresh_after={}
        self.worker_audit_after=0.0
        self.inflight=asyncio.Semaphore(4)
        self.stop_event=asyncio.Event();self.worker=None
        self.router=APIRouter();self.routes()

    def provider(self,identifier,*,enabled=True):
        row=self.store.one('SELECT * FROM oidc_providers WHERE id=?',(identifier,))
        if not row or enabled and not row['enabled']:raise DevError('OIDC_PROVIDER_DISABLED','身份提供者不可用',404)
        return row

    def check_endpoint(self,provider,url):
        try:
            if origin(url) not in {origin(provider['issuer']),*json.loads(provider['endpoint_origins'])}:raise ValueError('Unapproved origin')
        except (ValueError,TypeError) as exc:raise DevError('OIDC_ENDPOINT_INVALID','身份提供者端点不在已批准来源范围',400) from exc
        return url

    async def http_json(self,provider,method,url,**kwargs):
        self.check_endpoint(provider,url)
        try:
            # Per-read deadlines alone allow an endless slow-drip response.
            async with asyncio.timeout(15):
                async with httpx.AsyncClient(transport=self.transport,timeout=httpx.Timeout(12,connect=5),follow_redirects=False,trust_env=False) as client:
                    async with client.stream(method,url,**kwargs) as response:
                        raw=bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw)>MAX_HTTP_BYTES:raise DevError('OIDC_UPSTREAM_ERROR','身份提供者响应超出限制',502)
                        if not 200<=response.status_code<300:
                            try:error=json.loads(raw).get('error')
                            except (ValueError,AttributeError):error=None
                            if ((response.status_code==400 and error=='invalid_grant')
                                    or (response.status_code==401 and error=='invalid_token'
                                        and 'Authorization' in kwargs.get('headers',{}))):
                                raise DevError('OIDC_CREDENTIAL_REJECTED','身份提供者已明确拒绝当前身份凭据',401)
                            raise DevError('OIDC_UPSTREAM_ERROR','身份提供者暂不可用或拒绝请求',502)
                        value=json.loads(raw)
                        if not isinstance(value,dict):raise ValueError('Object required')
                        return value
        except (httpx.HTTPError,ValueError,UnicodeError,TimeoutError) as exc:
            raise DevError('OIDC_UPSTREAM_ERROR','身份提供者通信失败',502) from exc

    async def metadata(self,provider,*,force=False):
        identifier=provider['id'];version=provider['version']
        async with self.metadata_locks.setdefault(identifier,asyncio.Lock()):
            cached=self.store.one('SELECT * FROM oidc_cache WHERE provider_id=? AND version=? AND expires>?',
                                  (identifier,version,time.time()))
            if cached and not force:
                return json.loads(cached['metadata']),json.loads(cached['jwks'])
            failed=self.metadata_failures.get(identifier)
            if failed and failed[0]==version and failed[1]>time.monotonic():
                raise DevError('OIDC_UPSTREAM_ERROR','身份提供者暂不可用，请稍后重试',502)
            try:
                result=await self._load_metadata(provider)
            except DevError:
                self.metadata_failures[identifier]=(version,time.monotonic()+METADATA_FAILURE_COOLDOWN)
                raise
            self.metadata_failures.pop(identifier,None)
            return result

    async def verify_claims(self,encoded,provider,keys,**kwargs):
        try:
            return validate_claims(encoded,provider,keys,**kwargs)
        except SigningKeyRefreshRequired as original:
            identifier=provider['id'];version=provider['version']
            async with self.key_locks.setdefault(identifier,asyncio.Lock()):
                # Another waiter may already have refreshed. Use its keys without
                # extending the cooldown or producing duplicate network requests.
                _,current=await self.metadata(provider)
                if current != keys:
                    try:return validate_claims(encoded,provider,current,**kwargs)
                    except SigningKeyRefreshRequired:pass
                last=self.key_refresh_after.get(identifier)
                if last and last[0]==version and last[1]>time.monotonic():
                    raise original
                # Reserve BEFORE the await, even when the network refresh fails.
                # Arbitrarily many invented kids cannot force unbounded requests.
                self.key_refresh_after[identifier]=(version,time.monotonic()+KEY_REFRESH_COOLDOWN)
                _,current=await self.metadata(provider,force=True)
                return validate_claims(encoded,provider,current,**kwargs)

    async def _load_metadata(self,provider):
        meta=await self.http_json(provider,'GET',provider['discovery_url'])
        if meta.get('issuer')!=provider['issuer']:raise DevError('OIDC_ISSUER_MISMATCH','Discovery issuer 与配置不一致',400)
        for field in ('authorization_endpoint','token_endpoint','jwks_uri'):
            self.check_endpoint(provider,meta.get(field))
        for field in ('userinfo_endpoint','end_session_endpoint'):
            if meta.get(field):self.check_endpoint(provider,meta[field])
        if 'code' not in meta.get('response_types_supported',[]):raise DevError('OIDC_FLOW_UNSUPPORTED','身份提供者未支持授权码流程',400)
        if not set(meta.get('id_token_signing_alg_values_supported',[])) & ALGORITHMS:raise DevError('OIDC_ALGORITHM_UNSUPPORTED','身份提供者未支持已批准的签名算法',400)
        methods=meta.get('token_endpoint_auth_methods_supported',['client_secret_basic'])
        if 'client_secret_basic' not in methods and 'client_secret_post' not in methods:raise DevError('OIDC_CLIENT_AUTH_UNSUPPORTED','身份提供者未支持机密客户端认证',400)
        if meta.get('code_challenge_methods_supported') and 'S256' not in meta['code_challenge_methods_supported']:raise DevError('OIDC_PKCE_REQUIRED','身份提供者未支持 S256 PKCE',400)
        keys=await self.http_json(provider,'GET',meta['jwks_uri'])
        if not isinstance(keys.get('keys'),list) or not 1<=len(keys['keys'])<=32:raise DevError('OIDC_KEYS_INVALID','身份提供者公钥格式无效',400)
        with self.store.transaction():
            current=self.provider(provider['id'])
            if current['version']!=provider['version']:
                raise DevError('OIDC_CONFIG_CHANGED','身份提供者配置已变化，请重新发起请求',409)
            self.store.db.execute('INSERT INTO oidc_cache VALUES(?,?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET version=excluded.version,metadata=excluded.metadata,jwks=excluded.jwks,expires=excluded.expires',(provider['id'],provider['version'],json.dumps(meta),json.dumps(keys),time.time()+300))
        return meta,keys

    async def exchange(self,provider,meta,form):
        secret=self.store.decrypt(provider['client_secret'])
        if 'client_secret_basic' in meta.get('token_endpoint_auth_methods_supported',['client_secret_basic']):
            from urllib.parse import quote_plus
            return await self.http_json(provider,'POST',meta['token_endpoint'],data=form,auth=httpx.BasicAuth(quote_plus(provider['client_id']),quote_plus(secret)))
        return await self.http_json(provider,'POST',meta['token_endpoint'],data={**form,'client_id':provider['client_id'],'client_secret':secret})

    def groups(self,provider,claims):
        groups=claims.get(provider['group_claim'],[])
        if not isinstance(groups,list) or len(groups)>1000 or any(not isinstance(g,str) or not 1<=len(g)<=200 for g in groups):raise DevError('OIDC_GROUPS_INVALID','身份群组格式无效',401)
        if provider['required_group'] and provider['required_group'] not in groups:raise DevError('OIDC_ADMISSION_DENIED','账号不属于允许登录的群组',403)
        return sorted(set(groups))

    def userinfo_groups(self,provider,info):
        policy_required=bool(provider['required_group'] or self.store.one(
            'SELECT 1 AS ok FROM group_mappings WHERE provider_id=? LIMIT 1',(provider['id'],)))
        if policy_required and (not isinstance(info,dict) or provider['group_claim'] not in info):
            # Missing claims are not evidence of group removal. Reject a new
            # login consistently; existing identities fail closed at freshness
            # expiry without spuriously deleting their sessions on this response.
            raise DevError('OIDC_GROUPS_UNAVAILABLE',
                           '群组授权要求 UserInfo 明确返回已配置的群组字段；请检查身份提供者映射',403)
        return self.groups(provider,info or {})

    def reconcile_groups(self,identity,groups,provider):
        """Called in a write transaction. Remove only this identity's sources."""
        store=self.store;prefix='oidc:'+identity['id']+':'
        store.db.execute('UPDATE memberships SET active=0,version=version+1 WHERE user_id=? AND substr(source,1,?)=?',(identity['user_id'],len(prefix),prefix))
        store.db.execute('UPDATE role_assignments SET active=0,version=version+1 WHERE user_id=? AND substr(source,1,?)=?',(identity['user_id'],len(prefix),prefix))
        expires=time.time()+provider['freshness_seconds']
        for mapping in store.all('SELECT * FROM group_mappings WHERE provider_id=?',(provider['id'],)):
            if mapping['group_name'] not in groups:continue
            source=prefix+mapping['id']
            store.db.execute('INSERT INTO memberships(space_id,user_id,source,level,active,expires) VALUES(?,?,?,?,1,?) ON CONFLICT(space_id,user_id,source) DO UPDATE SET level=excluded.level,active=1,expires=excluded.expires,version=memberships.version+1',(mapping['space_id'],identity['user_id'],source,mapping['level'],expires))
            if mapping['role_id']:
                store.db.execute('INSERT INTO role_assignments(role_id,user_id,space_id,source,may_delegate,active,expires) VALUES(?,?,?,?,?,1,?) ON CONFLICT(role_id,user_id,source) DO UPDATE SET may_delegate=excluded.may_delegate,active=1,expires=excluded.expires,version=role_assignments.version+1',(mapping['role_id'],identity['user_id'],mapping['space_id'],source,mapping['may_delegate'],expires))
        store.db.execute("UPDATE external_identities SET groups_json=?,checked_at=?,fresh_until=?,enabled=1,disabled_reason='' WHERE id=?",(json.dumps(groups),time.time(),expires,identity['id']))
        self.runtime.wake.set();self.runtime.publish('iam',{'user_id':identity['user_id']})

    def disable_identity(self, identity, reason, *, revoke=False):
        """Keep a subject tombstone: unlink must never permit email-style takeover.

        This method must run in a write transaction. Only the identity's own
        derived assignment sources are removed; manual assignments survive.
        """
        prefix='oidc:'+identity['id']+':'
        self.store.db.execute('UPDATE external_identities SET enabled=0,disabled_reason=?,fresh_until=0 WHERE id=?',(reason,identity['id']))
        for table in ('memberships','role_assignments'):
            self.store.db.execute(f'UPDATE {table} SET active=0,version=version+1 WHERE user_id=? AND substr(source,1,?)=?',(identity['user_id'],len(prefix),prefix))
        self.store.db.execute('DELETE FROM sessions WHERE id_hash IN (SELECT session_hash FROM session_security WHERE identity_id=?)',(identity['id'],))
        if revoke:
            self.store.db.execute('UPDATE grants SET revoked=1 WHERE identity_id=?',(identity['id'],))
            self.store.db.execute('UPDATE external_identities SET upstream_tokens=NULL WHERE id=?',(identity['id'],))
            # Cancel link attempts begun before unlink, including a callback
            # currently awaiting the provider. provision() rechecks this row in
            # its final transaction. A new explicit link gets a new state.
            self.store.db.execute('DELETE FROM oidc_transactions WHERE provider_id=? AND link_user_id=?',
                                  (identity['provider_id'],identity['user_id']))
        self.runtime.publish('iam',{'user_id':identity['user_id']});self.runtime.wake.set()

    def provision(self,provider,claims,groups,txn,tokens):
        store=self.store;subject=claims['sub'];now=time.time()
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE')
            live_txn=store.one('SELECT 1 AS ok FROM oidc_transactions WHERE state_hash=? AND provider_id=? AND used=1 AND expires>?',
                               (txn['state_hash'],provider['id'],now))
            if not live_txn:
                raise DevError('OIDC_STATE_INVALID','登录或关联已过期或被取消，请重新发起',400)
            current=self.provider(provider['id'])
            if current['version']!=txn['provider_version']:raise DevError('OIDC_CONFIG_CHANGED','身份配置已改变，请重新登录',409)
            identity=store.one('SELECT * FROM external_identities WHERE issuer=? AND subject=?',(provider['issuer'],subject))
            link_user=txn.get('link_user_id')
            if link_user:
                session=store.one('SELECT * FROM sessions WHERE id_hash=? AND user_id=? AND expires>?',(txn['link_session_hash'],link_user,now))
                security=store.one('SELECT * FROM session_security WHERE session_hash=?',(txn['link_session_hash'],))
                user=iam.user_security(store,link_user)
                if not session or not security or security['epoch']!=user['epoch']:raise DevError('LINK_SESSION_EXPIRED','原账号登录已失效',401)
                if identity and identity['user_id']!=link_user:raise DevError('IDENTITY_ALREADY_LINKED','该外部身份已绑定其他账号',409)
            if identity and identity['disabled_reason']=='unlinked' and link_user!=identity['user_id']:
                raise DevError('IDENTITY_UNLINKED','此身份已解除关联；请登录原账号后明确重新关联',403)
            if not identity:
                if not link_user and provider['admission']!='jit':raise DevError('OIDC_ADMISSION_CLOSED','未开放自动加入；请由管理员先安排账号关联',403)
                if not store.one('SELECT 1 AS ok FROM iam_users WHERE instance_admin=1 AND active=1'):raise DevError('BOOTSTRAP_REQUIRED','必须先初始化本地恢复管理员',403)
                if not link_user and store.one('SELECT count(*) AS n FROM users')['n']>=10000:raise DevError('USER_LIMIT','用户数量已达到实例限制',409)
                user_id=link_user or 'usr_'+uuid.uuid4().hex
                if not link_user:
                    # An unusable password marker: external accounts are NEVER
                    # assigned a generated local password or linked by email.
                    store.db.execute('INSERT INTO users(id,username,password_hash,created) VALUES(?,?,?,?)',(user_id,'oidc-'+uuid.uuid4().hex,'!oidc-only',now))
                    store.db.execute('UPDATE iam_users SET local_login=0,instance_admin=0,display_name=? WHERE user_id=?',(str(claims.get('name') or claims.get('preferred_username') or 'OIDC user')[:100],user_id))
                    store.db.execute("DELETE FROM memberships WHERE user_id=? AND space_id='legacy'",(user_id,))
                    iam.create_personal_space(store,user_id,'Personal')
                iid='idn_'+uuid.uuid4().hex
                store.db.execute('INSERT INTO external_identities(id,issuer,subject,provider_id,user_id,checked_at,fresh_until,created) VALUES(?,?,?,?,?,?,?,?)',(iid,provider['issuer'],subject,provider['id'],user_id,now,now+provider['freshness_seconds'],now))
                identity=store.one('SELECT * FROM external_identities WHERE id=?',(iid,))
            user=iam.user_security(store,identity['user_id'])
            self.reconcile_groups(identity,groups,provider)
            expires_in=tokens.get('expires_in',300)
            if type(expires_in) not in (int,float) or not math.isfinite(expires_in) or not 1<=expires_in<=30*86400:raise DevError('OIDC_TOKEN_INVALID','上游令牌有效期无效',401)
            secured={'access_token':tokens.get('access_token'),'refresh_token':tokens.get('refresh_token'),
                     'expires_at':now+expires_in,'id_token':tokens.get('id_token')}
            store.db.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',(store.encrypt(json.dumps(secured)),identity['id']))
            result=self.auth.new_session(user['id'],identity_id=identity['id'],provider_sid=claims.get('sid'))
            store.audit('panel:'+user['username'],'oidc.linked' if link_user else 'oidc.login',identity['id'],detail={'provider_id':provider['id']},commit=False)
        return result

    async def begin(self,provider,return_to,*,link_session=None,client_key='internal'):
        return_to=safe_return(return_to)
        state,browser,nonce,verifier=token(),token(),token(),token(48)
        state_hash=digest(state);now=time.time()
        client_hash=digest(('link:'+link_session['user_id']) if link_session else 'login:'+client_key)
        with self.store.transaction():
            self.store.db.execute('DELETE FROM oidc_transactions WHERE expires<=?',(now,))
            limit=8 if link_session else LOGIN_CLIENT_LIMIT
            if self.store.one('SELECT count(*) AS n FROM oidc_transactions WHERE client_hash=?',(client_hash,))['n']>=limit:
                raise DevError('OIDC_CLIENT_BUSY','此客户端已有过多登录请求；请完成现有请求或稍后重试',429)
            if self.store.one('SELECT count(*) AS n FROM oidc_transactions')['n']>=LOGIN_TOTAL_LIMIT:
                raise DevError('OIDC_BUSY','登录请求过多',429)
            if not link_session:
                public=self.store.one('SELECT count(*) AS n FROM oidc_transactions WHERE link_user_id IS NULL')['n']
                provider_count=self.store.one('SELECT count(*) AS n FROM oidc_transactions WHERE provider_id=? AND link_user_id IS NULL',(provider['id'],))['n']
                if public>=LOGIN_PUBLIC_LIMIT or provider_count>=LOGIN_PROVIDER_LIMIT:
                    raise DevError('OIDC_BUSY','登录请求过多',429)
            self.store.db.execute('''INSERT INTO oidc_transactions
                (state_hash,provider_id,browser_hash,nonce,verifier,return_to,link_user_id,link_session_hash,provider_version,expires,used,client_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,0,?)''',
                (state_hash,provider['id'],digest(browser),nonce,self.store.encrypt(verifier),return_to,
                 link_session['user_id'] if link_session else None,link_session['id_hash'] if link_session else None,
                 provider['version'],now+LOGIN_TTL,client_hash))
        try:
            meta,_=await self.metadata(provider)
        except BaseException:
            self.store.execute('DELETE FROM oidc_transactions WHERE state_hash=?',(state_hash,))
            raise
        callback=self.public_url()+'/auth/oidc/'+provider['id']+'/callback'
        params={'client_id':provider['client_id'],'redirect_uri':callback,'response_type':'code','scope':provider['scopes'],
                'state':state,'nonce':nonce,'code_challenge':b64(hashlib.sha256(verifier.encode()).digest()),'code_challenge_method':'S256'}
        if link_session:params.update(prompt='login',max_age='0')
        return meta['authorization_endpoint']+('?' if '?' not in meta['authorization_endpoint'] else '&')+urlencode(params),state_hash,browser

    async def sync_identity(self,identifier):
        identity=self.store.one('SELECT * FROM external_identities WHERE id=?',(identifier,))
        if not identity or not identity['enabled'] or not identity['upstream_tokens']:return 'OIDC_SYNC_UNAVAILABLE'
        provider=self.provider(identity['provider_id'])
        user=iam.user_security(self.store,identity['user_id'])
        expected_tokens=identity['upstream_tokens']
        def still_current():
            current=self.store.one('SELECT * FROM external_identities WHERE id=?',(identifier,))
            configured=self.store.one('SELECT version,enabled FROM oidc_providers WHERE id=?',(provider['id'],))
            account=self.store.one('SELECT epoch,active FROM iam_users WHERE user_id=?',(user['id'],))
            return bool(current and current['enabled'] and current['upstream_tokens']==expected_tokens
                        and configured and configured['enabled'] and configured['version']==provider['version']
                        and account and account['active'] and account['epoch']==user['epoch'])
        try:
            meta,keys=await self.metadata(provider)
            if not meta.get('userinfo_endpoint'):return 'OIDC_USERINFO_UNAVAILABLE'
            secured=json.loads(self.store.decrypt(identity['upstream_tokens']))
            if secured['expires_at']<=time.time()+30:
                if not secured.get('refresh_token'):return 'OIDC_REAUTH_REQUIRED'
                tokens=await self.exchange(provider,meta,{'grant_type':'refresh_token','refresh_token':secured['refresh_token']})
                if tokens.get('token_type','').lower()!='bearer' or not isinstance(tokens.get('access_token'),str):raise DevError('OIDC_TOKEN_INVALID','刷新未返回有效访问令牌',401)
                if tokens.get('id_token'):
                    claims=await self.verify_claims(tokens['id_token'],provider,keys,access_token=tokens['access_token'])
                    if claims['sub']!=identity['subject']:raise DevError('OIDC_SUBJECT_MISMATCH','刷新身份不一致',401)
                life=tokens.get('expires_in',300)
                if type(life) not in (int,float) or not math.isfinite(life) or not 1<=life<=30*86400:raise DevError('OIDC_TOKEN_INVALID','刷新有效期无效',401)
                secured.update(access_token=tokens['access_token'],refresh_token=tokens.get('refresh_token',secured['refresh_token']),expires_at=time.time()+life)
                encrypted=self.store.encrypt(json.dumps(secured))
                with self.store.transaction():
                    if self.provider(provider['id'])['version']!=provider['version'] or iam.user_security(self.store,user['id'])['epoch']!=user['epoch']:return
                    changed=self.store.db.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=? AND enabled=1 AND upstream_tokens=?',(encrypted,identifier,expected_tokens)).rowcount
                    if not changed:return  # A parallel login/unlink owns newer credentials.
                expected_tokens=encrypted
            info=await self.http_json(provider,'GET',meta['userinfo_endpoint'],headers={'Authorization':'Bearer '+secured['access_token']})
            if info.get('sub')!=identity['subject']:raise DevError('OIDC_SUBJECT_MISMATCH','UserInfo 身份不一致',401)
            groups=self.userinfo_groups(provider,info)
            with self.store.transaction():
                current=self.store.one('SELECT * FROM external_identities WHERE id=?',(identifier,))
                if (not current or not current['enabled'] or current['upstream_tokens']!=expected_tokens
                        or self.provider(provider['id'])['version']!=provider['version']
                        or iam.user_security(self.store,user['id'])['epoch']!=user['epoch']):return
                self.reconcile_groups(current,groups,provider)

        except DevError as exc:
            # A response for old credentials/configuration must not disable an
            # identity that was just reauthenticated, relinked or reconfigured.
            if exc.code in {'OIDC_ADMISSION_DENIED','OIDC_SUBJECT_MISMATCH','OIDC_CREDENTIAL_REJECTED'}:
                with self.store.transaction():
                    if still_current():
                        current=self.store.one('SELECT * FROM external_identities WHERE id=?',(identifier,))
                        self.disable_identity(current,exc.code)
            raise

    async def reconcile(self):
        async with self.sync_lock:
            await self.scheduler.run()

    async def start(self):
        self.stop_event.clear()
        async def work():
            while not self.stop_event.is_set():
                try:await self.reconcile()
                except asyncio.CancelledError:raise
                except Exception:
                    # Keep the worker alive without leaking credential-bearing errors.
                    if time.monotonic()>=self.worker_audit_after:
                        self.worker_audit_after=time.monotonic()+3600
                        self.store.audit('oidc-worker','oidc.worker_error',status='error')
                try:await asyncio.wait_for(self.stop_event.wait(),30)
                except asyncio.TimeoutError:pass
        self.worker=asyncio.create_task(work(),name='oidc-entitlements')

    async def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.cancel()
            try:await self.worker
            except asyncio.CancelledError:pass

    def routes(self):
        router,store,auth=self.router,self.store,self.auth
        def cookie(response,secret):
            response.set_cookie('rd_session',secret,httponly=True,samesite='lax',secure=urlsplit(self.public_url()).scheme=='https',max_age=12*3600,path='/')
            response.headers['Cache-Control']='no-store';return response

        @router.get('/api/auth/providers')
        async def providers():
            return {'providers':store.all('SELECT id,label FROM oidc_providers WHERE enabled=1 ORDER BY label')}

        @router.get('/api/iam/oidc/providers')
        async def provider_admin(request:Request):
            auth.instance(request)
            return {'providers':[public_provider(r) for r in store.all('SELECT * FROM oidc_providers ORDER BY created')]}

        async def save_provider(request,body,identifier=None):
            p=auth.instance(request,True)
            with store.lock,store.db:
                store.db.execute('BEGIN IMMEDIATE');p=auth.instance(request,True)
                old=self.provider(identifier,enabled=False) if identifier else None
                if old and body.expected_version!=old['version']:raise DevError('VERSION_CONFLICT','身份提供者配置已修改',409)
                if old and body.issuer!=old['issuer']:raise DevError('ISSUER_IMMUTABLE','Issuer 是身份边界，请建立新提供者而不是改写',409)
                if not old and not body.client_secret:raise DevError('CLIENT_SECRET_REQUIRED','需要客户端密钥',400)
                if not old and store.one('SELECT count(*) AS n FROM oidc_providers')['n']>=16:raise DevError('PROVIDER_LIMIT','最多16个身份提供者',409)
                identifier=identifier or 'idp_'+uuid.uuid4().hex
                secret=store.encrypt(body.client_secret) if body.client_secret else old['client_secret']
                values=(body.label,body.issuer,body.client_id,secret,body.discovery_url,int(body.enabled),body.admission,body.group_claim,body.required_group,body.scopes,json.dumps(body.endpoint_origins),body.freshness_seconds)
                if old:
                    store.db.execute('UPDATE oidc_providers SET label=?,issuer=?,client_id=?,client_secret=?,discovery_url=?,enabled=?,admission=?,group_claim=?,required_group=?,scopes=?,endpoint_origins=?,freshness_seconds=?,version=version+1 WHERE id=?',(*values,identifier))
                    store.db.execute('DELETE FROM oidc_cache WHERE provider_id=?',(identifier,))
                    # A changed provider may reduce admission/group policy. Old
                    # entitlement snapshots are not grandfathered indefinitely.
                    store.db.execute('UPDATE external_identities SET fresh_until=0,checked_at=0 WHERE provider_id=?',(identifier,))
                    for identity in store.all('SELECT * FROM external_identities WHERE provider_id=?',(identifier,)):
                        prefix='oidc:'+identity['id']+':'
                        for table in ('memberships','role_assignments'):
                            store.db.execute(f'UPDATE {table} SET active=0,version=version+1 WHERE substr(source,1,?)=?',(len(prefix),prefix))
                else:
                    store.db.execute('INSERT INTO oidc_providers(id,label,issuer,client_id,client_secret,discovery_url,enabled,admission,group_claim,required_group,scopes,endpoint_origins,freshness_seconds,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,*values,time.time()))
                iam.audit(store,p,'oidc.provider_updated' if old else 'oidc.provider_created',identifier,detail={'enabled':body.enabled,'issuer':body.issuer})
            return public_provider(self.provider(identifier,enabled=False))

        @router.post('/api/iam/oidc/providers',status_code=201)
        async def create_provider(request:Request,body:ProviderInput):return await save_provider(request,body)

        @router.put('/api/iam/oidc/providers/{identifier}')
        async def edit_provider(identifier:str,request:Request,body:ProviderInput):return await save_provider(request,body,identifier)

        @router.post('/api/iam/oidc/providers/{identifier}/check')
        async def check_provider(identifier:str,request:Request):
            auth.instance(request,True);provider=self.provider(identifier,enabled=False)
            meta,keys=await self.metadata(provider,force=True)
            auth.instance(request,True)
            return {'issuer':meta['issuer'],'signing_keys':len(keys['keys']),'callback':self.public_url()+'/auth/oidc/'+identifier+'/callback','backchannel_logout':self.public_url()+'/auth/oidc/'+identifier+'/backchannel-logout'}

        @router.get('/api/iam/oidc/targets')
        async def mapping_targets(request:Request):
            auth.instance(request)
            return {'spaces':store.all("SELECT id,label FROM spaces WHERE active=1 AND kind='team'"),
                    'roles':store.all("SELECT r.id,r.label,r.space_id FROM access_roles r JOIN spaces s ON s.id=r.space_id WHERE s.active=1 AND s.kind='team'")}

        @router.get('/api/iam/oidc/providers/{identifier}/mappings')
        async def mappings(identifier:str,request:Request):
            auth.instance(request)
            return {'mappings':store.all('SELECT * FROM group_mappings WHERE provider_id=?',(identifier,))}

        @router.post('/api/iam/oidc/providers/{identifier}/mappings',status_code=201)
        async def create_mapping(identifier:str,request:Request,body:GroupMappingInput):
            p=auth.instance(request,True);self.provider(identifier,enabled=False)
            with store.lock,store.db:
                store.db.execute('BEGIN IMMEDIATE');auth.instance(request,True)
                if not store.one("SELECT id FROM spaces WHERE id=? AND active=1 AND kind='team'",(body.space_id,)):raise DevError('SPACE_NOT_FOUND','空间不存在',404)
                if body.role_id and not store.one('SELECT id FROM access_roles WHERE id=? AND space_id=?',(body.role_id,body.space_id)):raise DevError('ROLE_NOT_FOUND','角色不属于指定空间',404)
                mid='map_'+uuid.uuid4().hex
                store.db.execute('INSERT INTO group_mappings(id,provider_id,group_name,space_id,level,role_id,may_delegate) VALUES(?,?,?,?,?,?,?)',(mid,identifier,body.group_name,body.space_id,body.level,body.role_id,int(body.may_delegate)))
                store.db.execute('UPDATE external_identities SET checked_at=0 WHERE provider_id=?',(identifier,))
                iam.audit(store,p,'oidc.mapping_created',mid)
            return store.one('SELECT * FROM group_mappings WHERE id=?',(mid,))

        @router.delete('/api/iam/oidc/mappings/{identifier}')
        async def delete_mapping(identifier:str,request:Request):
            with store.lock,store.db:
                store.db.execute('BEGIN IMMEDIATE');p=auth.instance(request,True)
                row=store.one('SELECT * FROM group_mappings WHERE id=?',(identifier,))
                if not row:raise DevError('MAPPING_NOT_FOUND','映射不存在',404)
                for identity in store.all('SELECT id FROM external_identities WHERE provider_id=?',(row['provider_id'],)):
                    source='oidc:'+identity['id']+':'+identifier
                    store.db.execute('UPDATE memberships SET active=0,version=version+1 WHERE source=?',(source,));store.db.execute('UPDATE role_assignments SET active=0,version=version+1 WHERE source=?',(source,))
                store.db.execute('DELETE FROM group_mappings WHERE id=?',(identifier,));iam.audit(store,p,'oidc.mapping_deleted',identifier)
            return {'ok':True}

        @router.post('/api/iam/oidc/reconcile')
        async def synchronize(request:Request):
            auth.instance(request,True);await self.reconcile();auth.instance(request,True)
            return {'ok':True,'note':'Provider outages do not extend entitlement freshness.'}

        @router.get('/auth/oidc/{identifier}/start')
        async def login_start(identifier:str,request:Request,return_to:str='/'):
            self.runtime.oauth.throttle(request)
            async with self.inflight:
                location,state_hash,browser=await self.begin(self.provider(identifier),return_to,
                    client_key=request.client.host if request.client else 'unknown')
            response=RedirectResponse(location,status_code=303)
            response.set_cookie('rd_oidc_'+state_hash[:24],browser,httponly=True,samesite='lax',secure=urlsplit(self.public_url()).scheme=='https',max_age=LOGIN_TTL,path='/auth/oidc/'+identifier+'/callback')
            response.headers['Cache-Control']='no-store';return response

        @router.post('/api/iam/oidc/{identifier}/link')
        async def link_start(identifier:str,request:Request,body:LinkInput):
            session=auth.session_write(request)
            if time.time()-session['authenticated_at']>300:raise DevError('RECENT_LOGIN_REQUIRED','关联身份前请先重新登录当前账号',403)
            async with self.inflight:location,state_hash,browser=await self.begin(self.provider(identifier),body.return_to,link_session=session)
            response=JSONResponse({'redirect':location})
            response.set_cookie('rd_oidc_'+state_hash[:24],browser,httponly=True,samesite='lax',secure=urlsplit(self.public_url()).scheme=='https',max_age=LOGIN_TTL,path='/auth/oidc/'+identifier+'/callback')
            return response

        @router.delete('/api/iam/identities/{identifier}')
        async def unlink(identifier:str,request:Request):
            with store.transaction():
                session=auth.session_write(request)
                if time.time()-session['authenticated_at']>300:raise DevError('RECENT_LOGIN_REQUIRED','解除关联前请重新登录',403)
                identity=store.one('SELECT * FROM external_identities WHERE id=? AND user_id=?',(identifier,session['user_id']))
                if not identity:raise DevError('IDENTITY_NOT_FOUND','身份不存在',404)
                user=iam.user_security(store,session['user_id'])
                other=store.one('SELECT 1 AS ok FROM external_identities i JOIN oidc_providers p ON p.id=i.provider_id WHERE i.user_id=? AND i.id<>? AND i.enabled=1 AND p.enabled=1',(user['id'],identifier))
                if not user['local_login'] and not other:raise DevError('LAST_LOGIN_METHOD','不能移除最后一个登录方式',409)
                self.disable_identity(identity,'unlinked',revoke=True)
                store.audit('panel:'+user['username'],'oidc.unlinked',identifier,commit=False)
            return {'ok':True,'relogin_required':session['identity_id']==identifier}

        @router.get('/auth/oidc/{identifier}/callback')
        async def callback(identifier:str,request:Request):
            params=dict(request.query_params)
            if len(params)!=len(request.query_params.multi_items()):raise DevError('OIDC_CALLBACK_INVALID','回调参数重复',400)
            state=params.get('state','');code=params.get('code','')
            if not isinstance(state,str) or not 20<=len(state)<=200:raise DevError('OIDC_STATE_INVALID','登录状态无效',400)
            state_hash=digest(state);cookie_name='rd_oidc_'+state_hash[:24]
            with store.lock,store.db:
                store.db.execute('BEGIN IMMEDIATE')
                txn=store.one('SELECT * FROM oidc_transactions WHERE state_hash=? AND provider_id=? AND expires>? AND used=0',(state_hash,identifier,time.time()))
                if not txn or not hmac.compare_digest(txn['browser_hash'],digest(request.cookies.get(cookie_name,''))):raise DevError('OIDC_STATE_INVALID','登录状态已过期、已使用或不属于本浏览器',400)
                if txn['link_user_id'] and digest(request.cookies.get('rd_session',''))!=txn['link_session_hash']:raise DevError('LINK_SESSION_EXPIRED','关联期间账号已切换',401)
                store.db.execute('UPDATE oidc_transactions SET used=1 WHERE state_hash=?',(state_hash,))
            if params.get('error') or not 1<=len(code)<=4096:raise DevError('OIDC_LOGIN_DENIED','身份提供者未完成登录',400)
            async with self.inflight:
                provider=self.provider(identifier)
                if provider['version']!=txn['provider_version']:raise DevError('OIDC_CONFIG_CHANGED','登录期间配置变化',409)
                if 'iss' in params and params['iss']!=provider['issuer']:raise DevError('OIDC_ISSUER_MISMATCH','回调身份来源不匹配',401)
                meta,keys=await self.metadata(provider)
                tokens=await self.exchange(provider,meta,{'grant_type':'authorization_code','code':code,'redirect_uri':self.public_url()+'/auth/oidc/'+identifier+'/callback','code_verifier':store.decrypt(txn['verifier'])})
                if tokens.get('token_type','').lower()!='bearer' or not isinstance(tokens.get('access_token'),str):raise DevError('OIDC_TOKEN_INVALID','没有收到有效访问令牌',401)
                claims=await self.verify_claims(tokens.get('id_token'),provider,keys,nonce=txn['nonce'],access_token=tokens['access_token'])
                if txn['link_user_id'] and (type(claims.get('auth_time')) not in (int,float) or claims['auth_time']<txn['expires']-LOGIN_TTL-SKEW):raise DevError('OIDC_RECENT_LOGIN_REQUIRED','关联身份需要身份提供者近期认证',401)
                info=None
                if meta.get('userinfo_endpoint'):
                    info=await self.http_json(provider,'GET',meta['userinfo_endpoint'],headers={'Authorization':'Bearer '+tokens['access_token']})
                    if info.get('sub')!=claims['sub']:raise DevError('OIDC_SUBJECT_MISMATCH','UserInfo 与 ID Token 身份不同',401)
                groups=self.userinfo_groups(provider,info)
                value=self.provision(provider,claims,groups,txn,tokens)
            old_hash=digest(request.cookies.get('rd_session',''))
            store.execute('DELETE FROM sessions WHERE id_hash=?',(old_hash,))
            response=cookie(RedirectResponse(txn['return_to'],status_code=303),value['cookie'])
            response.delete_cookie(cookie_name,path='/auth/oidc/'+identifier+'/callback')
            return response

        @router.post('/auth/oidc/{identifier}/backchannel-logout')
        async def backchannel(identifier:str,request:Request):
            self.runtime.oauth.throttle(request)
            raw=await request.body()
            if len(raw)>65536 or request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':raise DevError('OIDC_LOGOUT_INVALID','无效注销通知',400)
            from urllib.parse import parse_qs
            try:form=parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=2)
            except (ValueError,UnicodeError) as exc:raise DevError('OIDC_LOGOUT_INVALID','无效注销参数',400) from exc
            if set(form)!={'logout_token'} or len(form['logout_token'])!=1:raise DevError('OIDC_LOGOUT_INVALID','无效注销参数',400)
            provider=self.provider(identifier);_,keys=await self.metadata(provider)
            claims=await self.verify_claims(form['logout_token'][0],provider,keys,logout=True)
            with store.lock,store.db:
                store.db.execute('BEGIN IMMEDIATE')
                store.db.execute('DELETE FROM oidc_logout_replays WHERE expires<?',(time.time(),))
                if store.one('SELECT 1 AS ok FROM oidc_logout_replays WHERE provider_id=? AND jti=?',(identifier,claims['jti'])):raise DevError('OIDC_LOGOUT_REPLAY','注销通知已处理',400)
                store.db.execute('INSERT INTO oidc_logout_replays VALUES(?,?,?)',(identifier,claims['jti'],time.time()+LOGIN_TTL))
                where='i.provider_id=?';args=[identifier]
                if claims.get('sub'):where+=' AND i.subject=?';args.append(claims['sub'])
                if claims.get('sid'):where+=' AND ss.provider_sid=?';args.append(claims['sid'])
                ids=store.all('SELECT ss.session_hash FROM session_security ss JOIN external_identities i ON i.id=ss.identity_id WHERE '+where,args)
                for row in ids:store.db.execute('DELETE FROM sessions WHERE id_hash=?',(row['session_hash'],))
                store.audit('oidc:'+identifier,'oidc.backchannel_logout',status='ok',detail={'sessions':len(ids)},commit=False)
            return JSONResponse({'ok':True},headers={'Cache-Control':'no-store'})
