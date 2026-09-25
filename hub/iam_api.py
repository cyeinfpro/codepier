"""Human administration of Spaces, membership and delegated roles.

Every mutation is CSRF-protected and serialized with its current authorization,
optimistic version and last-owner check. Tokens/IdP credentials are never listed.
"""
from __future__ import annotations
import json
import time
import uuid
from fastapi import APIRouter, Request
from pydantic import Field
from typing import Literal
from hub.access import AccessModel
from hub import iam
from shared.crypto import token, digest
from shared.util import DevError

class SpaceCreate(AccessModel):
    label: str = Field(min_length=1,max_length=100)
    idempotency_key: str = Field(min_length=8,max_length=128)

class SpaceEdit(AccessModel):
    label: str = Field(min_length=1,max_length=100)
    active: bool = True
    expected_version: int = Field(ge=1)

class MemberEdit(AccessModel):
    level: Literal['owner','admin','member','guest'] = 'member'
    active: bool = True
    expected_version: int = Field(default=0,ge=0)

class AssignmentEdit(AccessModel):
    active: bool = True
    may_delegate: bool = False
    expected_version: int = Field(default=0,ge=0)

class UserEdit(AccessModel):
    active: bool = True
    instance_admin: bool = False
    expected_version: int = Field(ge=1)

class InviteCreate(AccessModel):
    level: Literal['admin','member','guest'] = 'member'
    days: int = Field(default=7,ge=1,le=30)

class InviteAccept(AccessModel):
    invitation: str = Field(min_length=20,max_length=200,repr=False)

class BlockEdit(AccessModel):
    blocked: bool

class ShareEdit(AccessModel):
    visibility: Literal['private','space']


def spaces_for(store,user_id):
    rows=store.all('SELECT DISTINCT s.* FROM spaces s JOIN memberships m ON m.space_id=s.id WHERE m.user_id=? AND m.active=1 AND s.active=1 AND (m.expires IS NULL OR m.expires>?) ORDER BY s.created,s.id',(user_id,time.time()))
    result=[]
    for row in rows:
        try: member=iam.membership(store,user_id,row['id'])
        except DevError: continue
        result.append({**row,'level':member['level']})
    return result


def make_iam_router(auth,runtime):
    router,store=APIRouter(prefix='/api/iam'),runtime.store
    def admin(request,space_id,write=False):
        p=auth.admin(request,write)
        if p.space_id!=space_id: raise DevError('SPACE_FORBIDDEN','请求空间与当前空间不一致',403)
        return p
    def owner_guard(space_id,user_id,level,active):
        old=store.one("SELECT * FROM memberships WHERE space_id=? AND user_id=? AND source='manual'",(space_id,user_id))
        if old and old['active'] and old['level']=='owner' and (level!='owner' or not active):
            others=store.one("SELECT count(DISTINCT m.user_id) AS n FROM memberships m JOIN iam_users u ON u.user_id=m.user_id WHERE m.space_id=? AND m.user_id<>? AND m.active=1 AND m.level='owner' AND u.active=1 AND (m.expires IS NULL OR m.expires>?) AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.space_id=m.space_id AND b.user_id=m.user_id AND b.blocked=1)",(space_id,user_id,time.time()))
            if not others['n']: raise DevError('LAST_OWNER','不能移除或暂停最后一位空间主理人；请先转移所有权',409)
    def wake(p,action,target='',detail=None):
        iam.audit(store,p,action,target,detail=detail)
        runtime.publish('iam',{'space_id':p.space_id,'user_id':target or p.user_id})
        runtime.wake.set()

    @router.get('/me')
    async def me(request:Request):
        session=auth.session(request);u=iam.user_security(store,session['user_id'])
        ids=store.all('SELECT i.id,i.provider_id,p.label,i.enabled,i.fresh_until FROM external_identities i JOIN oidc_providers p ON p.id=i.provider_id WHERE i.user_id=?',(u['id'],))
        return {'id':u['id'],'username':u['username'],'display_name':u['display_name'] or u['username'],
                'instance_admin':bool(u['instance_admin']),'local_login':bool(u['local_login']),
                'spaces':spaces_for(store,u['id']),'identities':ids,'session_expires':session['expires'],
                'disabled_spaces':store.all("SELECT DISTINCT s.* FROM spaces s JOIN memberships m ON m.space_id=s.id WHERE m.user_id=? AND m.level='owner' AND m.active=1 AND s.active=0 AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.space_id=m.space_id AND b.user_id=m.user_id AND b.blocked=1)",(u['id'],))}

    @router.get('/spaces')
    async def spaces(request:Request):
        s=auth.session(request)
        return {'spaces':spaces_for(store,s['user_id'])}

    @router.post('/spaces',status_code=201)
    async def create_space(request:Request,body:SpaceCreate):
        p=auth.panel(request,True)
        fingerprint=digest(json.dumps(body.model_dump(exclude={'idempotency_key'}),sort_keys=True))
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=auth.panel(request,True)
            old=store.one("SELECT * FROM iam_replays WHERE user_id=? AND space_id='' AND action='spaces.create' AND idem=?",(p.user_id,body.idempotency_key))
            if old:
                if old['fingerprint']!=fingerprint:raise DevError('IDEMPOTENCY_CONFLICT','幂等键已使用',409)
                return json.loads(old['receipt'])
            if len(spaces_for(store,p.user_id))>=30:raise DevError('SPACE_LIMIT','账号最多加入30个空间',409)
            sid='sp_'+uuid.uuid4().hex
            store.db.execute('INSERT INTO spaces(id,label,kind,created) VALUES(?,?,?,?)',(sid,body.label.strip(),'team',time.time()))
            store.db.execute("INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,'owner')",(sid,p.user_id))
            result=store.one('SELECT * FROM spaces WHERE id=?',(sid,))
            store.db.execute("INSERT INTO iam_replays VALUES(?,'','spaces.create',?,?,?,?)",(p.user_id,body.idempotency_key,fingerprint,json.dumps(result),time.time()))
            wake(p,'space.created',sid)
        return result

    @router.put('/spaces/{space_id}')
    async def edit_space(space_id:str,request:Request,body:SpaceEdit):
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=admin(request,space_id,True)
            if iam.membership(store,p.user_id,space_id)['level']!='owner':raise DevError('OWNER_REQUIRED','只有主理人可修改空间',403)
            row=store.one('SELECT * FROM spaces WHERE id=?',(space_id,))
            if row['version']!=body.expected_version:raise DevError('VERSION_CONFLICT','空间已变化',409)
            if row['kind']!='team' and not body.active:raise DevError('PERSONAL_SPACE_REQUIRED','个人/恢复空间不能停用',409)
            store.db.execute('UPDATE spaces SET label=?,active=?,version=version+1 WHERE id=?',(body.label.strip(),int(body.active),space_id));wake(p,'space.updated',space_id)
        return store.one('SELECT * FROM spaces WHERE id=?',(space_id,))

    @router.post('/spaces/{space_id}/restore')
    async def restore_space(space_id:str,request:Request,body:SpaceEdit):
        with store.transaction():
            session=auth.session_write(request)
            iam.check_identity(store,session['identity_id'])
            owner=store.one("SELECT 1 AS ok FROM memberships m WHERE m.space_id=? AND m.user_id=? AND m.level='owner' AND m.active=1 AND (m.expires IS NULL OR m.expires>?) AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.space_id=m.space_id AND b.user_id=m.user_id AND b.blocked=1)",(space_id,session['user_id'],time.time()))
            row=store.one('SELECT * FROM spaces WHERE id=?',(space_id,))
            if not owner or not row:raise DevError('SPACE_NOT_FOUND','空间不存在或没有恢复权限',404)
            if row['version']!=body.expected_version:raise DevError('VERSION_CONFLICT','空间已变化',409)
            store.db.execute('UPDATE spaces SET active=1,version=version+1 WHERE id=?',(space_id,))
            store.audit('panel:'+session['username'],'space.restored',space_id,commit=False)
            runtime.wake.set();runtime.publish('iam',{'user_id':session['user_id'],'space_id':space_id})
        return store.one('SELECT * FROM spaces WHERE id=?',(space_id,))

    @router.get('/spaces/{space_id}/members')
    async def members(space_id:str,request:Request):
        admin(request,space_id)
        rows=store.all('SELECT m.*,u.username,s.display_name,s.active AS user_active,COALESCE(b.blocked,0) AS blocked FROM memberships m JOIN users u ON u.id=m.user_id JOIN iam_users s ON s.user_id=u.id LEFT JOIN membership_blocks b ON b.space_id=m.space_id AND b.user_id=m.user_id WHERE m.space_id=? ORDER BY u.username,m.source',(space_id,))
        return {'members':rows}

    @router.put('/spaces/{space_id}/members/{user_id}')
    async def set_member(space_id:str,user_id:str,request:Request,body:MemberEdit):
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=admin(request,space_id,True)
            iam.user_security(store,user_id)
            if store.one("SELECT kind FROM spaces WHERE id=?",(space_id,))['kind']=='personal' and user_id!=p.user_id:
                raise DevError('PERSONAL_SPACE_PRIVATE','共享成员请使用团队空间',403)
            old=store.one("SELECT * FROM memberships WHERE space_id=? AND user_id=? AND source='manual'",(space_id,user_id))
            if (old['version'] if old else 0)!=body.expected_version:raise DevError('VERSION_CONFLICT','成员已变化',409)
            if (body.level=='owner' or old and old['level']=='owner') and iam.membership(store,p.user_id,space_id)['level']!='owner':raise DevError('OWNER_REQUIRED','只有主理人可转移所有权',403)
            owner_guard(space_id,user_id,body.level,body.active)
            store.db.execute("INSERT INTO memberships(space_id,user_id,source,level,active) VALUES(?,?,'manual',?,?) ON CONFLICT(space_id,user_id,source) DO UPDATE SET level=excluded.level,active=excluded.active,version=memberships.version+1",(space_id,user_id,body.level,int(body.active)))
            wake(p,'membership.updated',user_id,body.model_dump())
        return store.one("SELECT * FROM memberships WHERE space_id=? AND user_id=? AND source='manual'",(space_id,user_id))

    @router.put('/spaces/{space_id}/members/{user_id}/suspension')
    async def block_member(space_id:str,user_id:str,request:Request,body:BlockEdit):
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=admin(request,space_id,True)
            if body.blocked:
                iam.membership(store,user_id,space_id)
            # Restoration is a privilege increase too. A suspended owner's
            # membership cannot be resolved by membership(), which rejects the
            # block; inspect the stored owner assignment without bypassing the
            # acting administrator's live checks.
            target_owner=store.one("SELECT 1 AS ok FROM memberships WHERE space_id=? AND user_id=? AND level='owner'",(space_id,user_id))
            if target_owner and iam.membership(store,p.user_id,space_id)['level']!='owner':
                raise DevError('OWNER_REQUIRED','只有主理人可以暂停或恢复另一主理人',403)
            if body.blocked:owner_guard(space_id,user_id,'guest',False)
            store.db.execute('INSERT INTO membership_blocks VALUES(?,?,?) ON CONFLICT(space_id,user_id) DO UPDATE SET blocked=excluded.blocked',(space_id,user_id,int(body.blocked)))
            wake(p,'membership.suspension',user_id,body.model_dump())
        return {'blocked':body.blocked}

    @router.get('/spaces/{space_id}/assignments')
    async def assignments(space_id:str,request:Request):
        p=auth.panel(request)
        if p.space_id!=space_id:raise DevError('SPACE_FORBIDDEN','空间不匹配',403)
        where='space_id=?';args=[space_id]
        if not p.admin:where+=' AND user_id=?';args.append(p.user_id)
        return {'assignments':store.all('SELECT * FROM role_assignments WHERE '+where,args)}

    @router.put('/spaces/{space_id}/assignments/{role_id}/{user_id}')
    async def set_assignment(space_id:str,role_id:str,user_id:str,request:Request,body:AssignmentEdit):
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=admin(request,space_id,True)
            iam.membership(store,user_id,space_id)
            if not store.one('SELECT 1 AS ok FROM access_roles WHERE id=? AND space_id=?',(role_id,space_id)):raise DevError('ROLE_NOT_FOUND','角色不存在',404)
            old=store.one("SELECT * FROM role_assignments WHERE role_id=? AND user_id=? AND source='manual'",(role_id,user_id))
            if (old['version'] if old else 0)!=body.expected_version:raise DevError('VERSION_CONFLICT','角色分配已改变',409)
            store.db.execute("INSERT INTO role_assignments(role_id,user_id,space_id,may_delegate,active) VALUES(?,?,?,?,?) ON CONFLICT(role_id,user_id,source) DO UPDATE SET may_delegate=excluded.may_delegate,active=excluded.active,version=role_assignments.version+1",(role_id,user_id,space_id,int(body.may_delegate),int(body.active)))
            wake(p,'role.assignment',user_id,{'role_id':role_id,**body.model_dump()})
        return store.one("SELECT * FROM role_assignments WHERE role_id=? AND user_id=? AND source='manual'",(role_id,user_id))

    @router.get('/spaces/{space_id}/invites')
    async def invitations(space_id:str,request:Request):
        admin(request,space_id)
        return {'invitations':store.all('SELECT hash AS id,level,created_by,expires,used_by,created FROM space_invites WHERE space_id=? ORDER BY created DESC LIMIT 200',(space_id,))}

    @router.delete('/spaces/{space_id}/invites/{identifier}')
    async def revoke_invite(space_id:str,identifier:str,request:Request):
        with store.transaction():
            p=admin(request,space_id,True)
            store.db.execute('DELETE FROM space_invites WHERE hash=? AND space_id=?',(identifier,space_id));wake(p,'invitation.revoked',space_id)
        return {'ok':True}

    @router.post('/spaces/{space_id}/invites',status_code=201)
    async def create_invite(space_id:str,request:Request,body:InviteCreate):
        p=admin(request,space_id,True)
        if store.one('SELECT kind FROM spaces WHERE id=?',(space_id,))['kind']=='personal':raise DevError('PERSONAL_SPACE_PRIVATE','个人空间不发送团队邀请',403)
        secret='cpi_'+token();now=time.time()
        with store.lock,store.db:
            admin(request,space_id,True)
            store.db.execute('INSERT INTO space_invites VALUES(?,?,?,?,?,NULL,?)',(digest(secret),space_id,body.level,p.user_id,now+body.days*86400,now))
            wake(p,'invitation.created',space_id,{'level':body.level})
        return {'invitation':secret,'expires':now+body.days*86400,'note':'Only displayed once; accept while signed in to the intended account.'}

    @router.post('/invites/accept')
    async def accept_invite(request:Request,body:InviteAccept):
        p=auth.panel(request,True)
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=auth.panel(request,True)
            row=store.one('SELECT * FROM space_invites WHERE hash=? AND expires>? AND used_by IS NULL',(digest(body.invitation),time.time()))
            if not row:raise DevError('INVITATION_INVALID','邀请不存在、已过期或已使用',400)
            if not iam.is_space_admin(store,row['created_by'],row['space_id']):raise DevError('INVITATION_INVALID','邀请人已不具备管理权',400)
            source='invite:'+digest(body.invitation)[:24]
            store.db.execute('INSERT INTO memberships(space_id,user_id,source,level) VALUES(?,?,?,?)',(row['space_id'],p.user_id,source,row['level']))
            store.db.execute('UPDATE space_invites SET used_by=? WHERE hash=?',(p.user_id,row['hash']))
            wake(p,'invitation.accepted',row['space_id'])
        return {'space_id':row['space_id']}

    @router.get('/users')
    async def users(request:Request):
        auth.instance(request)
        return {'users':store.all('SELECT u.id,u.username,u.created,s.active,s.instance_admin,s.local_login,s.version,s.display_name FROM users u JOIN iam_users s ON s.user_id=u.id ORDER BY u.created')}

    @router.put('/users/{user_id}')
    async def edit_user(user_id:str,request:Request,body:UserEdit):
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=auth.instance(request,True)
            row=store.one('SELECT * FROM iam_users WHERE user_id=?',(user_id,))
            if not row:raise DevError('USER_NOT_FOUND','账号不存在',404)
            if row['version']!=body.expected_version:raise DevError('VERSION_CONFLICT','账号已变化',409)
            if row['active'] and row['instance_admin'] and (not body.active or not body.instance_admin):
                if not store.one('SELECT 1 AS ok FROM iam_users WHERE user_id<>? AND instance_admin=1 AND active=1 LIMIT 1',(user_id,)):raise DevError('LAST_ADMIN','不能移除最后一位实例管理员',409)
            if row['active'] and row['instance_admin'] and row['local_login'] and (not body.active or not body.instance_admin):
                if not store.one('SELECT 1 AS ok FROM iam_users WHERE user_id<>? AND instance_admin=1 AND active=1 AND local_login=1 LIMIT 1',(user_id,)):
                    raise DevError('LAST_RECOVERY_ADMIN','不能移除最后一位可本地登录的恢复管理员',409)
            if not body.active:
                for m in store.all("SELECT m.space_id FROM memberships m JOIN spaces s ON s.id=m.space_id WHERE m.user_id=? AND m.active=1 AND m.level='owner' AND s.kind<>'personal'",(user_id,)):
                    owner_guard(m['space_id'],user_id,'guest',False)
            store.db.execute('UPDATE iam_users SET active=?,instance_admin=?,version=version+1,epoch=epoch+1 WHERE user_id=?',(int(body.active),int(body.instance_admin),user_id))
            store.db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
            store.db.execute('UPDATE grants SET revoked=1 WHERE user_id=?',(user_id,))
            wake(p,'user.updated',user_id,body.model_dump())
        if not body.active:
            for device in store.all('SELECT id FROM devices WHERE owner_user_id=?',(user_id,)):
                await runtime.disconnect_device(device['id'],'Owner account suspended')
        return {'id':user_id,**body.model_dump(exclude={'expected_version'}),'version':body.expected_version+1}

    @router.get('/sessions')
    async def sessions(request:Request):
        session=auth.session(request)
        return {'sessions':store.all('SELECT s.id_hash AS id,s.expires,ss.authenticated_at,ss.identity_id FROM sessions s LEFT JOIN session_security ss ON ss.session_hash=s.id_hash WHERE s.user_id=? AND s.expires>?',(session['user_id'],time.time())),'current':session['id_hash']}

    @router.delete('/sessions/{identifier}')
    async def delete_session(identifier:str,request:Request):
        with store.transaction():
            session=auth.session_write(request)
            store.db.execute('DELETE FROM sessions WHERE id_hash=? AND user_id=?',(identifier,session['user_id']))
            store.audit('panel:'+session['username'],'session.revoked',commit=False)
        return {'ok':True}

    @router.put('/share/{kind}/{identifier}')
    async def share(kind:str,identifier:str,request:Request,body:ShareEdit):
        table={'workflow':'workflows','artifact':'artifacts'}.get(kind)
        if not table:raise DevError('INVALID_RESOURCE','只支持共享工作流或产物，不共享交互会话',400)
        with store.lock,store.db:
            store.db.execute('BEGIN IMMEDIATE');p=auth.panel(request,True)
            row=store.one(f'SELECT * FROM {table} WHERE id=? AND space_id=?',(identifier,p.space_id))
            if not row or row['owner_user_id']!=p.user_id:raise DevError('RESOURCE_NOT_FOUND','只能共享自己的记录',404)
            runtime.project(row['project_id'],p)
            store.db.execute(f'UPDATE {table} SET visibility=? WHERE id=?',(body.visibility,identifier));wake(p,'record.shared',identifier,{'visibility':body.visibility,'kind':kind})
        return {'id':identifier,'visibility':body.visibility}
    return router
