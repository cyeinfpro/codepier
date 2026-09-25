"""Owner-managed, stable access identities. Profiles only cap a grant's consent.

The grant, not the profile, continues to own operations, workflows and leases.
A ChatGPT conversation/project name is never an authorization input.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import replace

from fastapi import APIRouter, Request
from pydantic import Field

from hub.access import AccessModel, project_selection
from shared.util import DevError
from shared.role_contracts import ROLE_SCOPE
from hub import iam

SCOPES = frozenset({'read', 'write', 'execute', 'computer'})
MAX_PROFILES = 200


def intersect_projects(consented, ceiling):
    if '*' in consented:
        return sorted(set(ceiling))
    if '*' in ceiling:
        return sorted(set(consented))
    return sorted(set(consented) & set(ceiling))


def stored_permissions(record):
    """Reject malformed database policy before doing any set intersections."""
    raw_scopes, projects = json.loads(record['scopes']), json.loads(record['projects'])
    if (not isinstance(raw_scopes, list) or not raw_scopes
            or any(not isinstance(item, str) for item in raw_scopes)
            or not set(raw_scopes) <= SCOPES or 'read' not in raw_scopes
            or not isinstance(projects, list)
            or any(not isinstance(item, str) or not item for item in projects)
            or '*' in projects and projects != ['*']):
        raise ValueError('Invalid stored permission')
    return set(raw_scopes), projects


def effective_grant(store, grant):
    """Read current policy every time; never copy a wider profile into a token."""
    if not grant or grant['revoked']:
        raise DevError('INVALID_TOKEN', '授权已撤销', 401)
    grant = dict(grant)
    iam.validate_grant(store, grant)
    if grant.get('authorization_mode', 'fixed') == 'role':
        from hub.roles import effective_role
        return effective_role(store, grant)
    if grant.get('authorization_mode', 'fixed') != 'fixed':
        raise DevError('INVALID_TOKEN', '未知授权模式，未授予访问权限', 401)
    try:
        scopes, projects = stored_permissions(grant)
        profile = None
        if grant.get('profile_id'):
            profile = store.one('SELECT * FROM access_profiles WHERE id=?', (grant['profile_id'],))
            if not profile or profile['user_id'] != grant['user_id'] or not profile['enabled'] or profile.get('space_id','legacy') != grant.get('space_id','legacy'):
                raise DevError('INVALID_TOKEN', '访问 Profile 已停用或不再属于此授权', 401)
            cap_scopes, cap_projects = stored_permissions(profile)
            scopes &= cap_scopes
            projects = intersect_projects(projects, cap_projects)
        if 'read' not in scopes or not scopes <= SCOPES or not isinstance(projects, list):
            raise ValueError('Invalid stored permission')
        if any(not isinstance(item, str) or not item for item in projects):
            raise ValueError('Invalid stored projects')
        if iam.installed(store):
            allowed = {row['id'] for row in store.all('SELECT id FROM projects WHERE space_id=?', (grant.get('space_id','legacy'),))}
            projects = sorted(allowed if '*' in projects else allowed & set(projects))
            from hub.runtime import Principal
            owner = Principal('', grant['user_id'], set(), [], space_id=grant.get('space_id','legacy'))
            projects = [pid for pid in projects if 'read' in iam.human_project_actions(store, owner, pid)]
        return scopes, projects, profile
    except (ValueError, TypeError, KeyError) as exc:
        raise DevError('INVALID_TOKEN', '授权策略记录无效；未授予访问权限', 401) from exc


def refresh_profile_principal(store, principal):
    """Recheck managed identities after waits without changing legacy callers."""
    principal = iam.live_principal(store, principal)
    if principal.admin or not principal.grant_id:
        return principal
    row = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    if not row or row['user_id'] != principal.user_id or row['profile_id'] != principal.profile_id or row.get('space_id','legacy') != principal.space_id:
        raise DevError('INVALID_TOKEN', '访问 Profile 的授权绑定已变化', 401)
    scopes, projects, _ = effective_grant(store, row)
    return replace(principal, scopes=scopes, projects=projects,
                   authorization_mode=row.get('authorization_mode', 'fixed'), role_id=row.get('role_id'),
                   space_id=row.get('space_id','legacy'), user_epoch=row.get('user_epoch',1), identity_id=row.get('identity_id'))


def validate_profile_consent(store, user_id, profile_id, scopes, projects, version, *, space_id="legacy"):
    """Called inside the same transaction that creates the grant."""
    if profile_id is None:
        if version is not None:
            raise DevError('INVALID_PROFILE', 'profile_version 需要 profile_id')
        return None
    if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 100:
        raise DevError('INVALID_PROFILE', '请选择有效的访问 Profile')
    row = store.one('SELECT * FROM access_profiles WHERE id=? AND user_id=? AND space_id=?', (profile_id, user_id, space_id))
    if not row:
        raise DevError('PROFILE_NOT_FOUND', '访问 Profile 不存在', 404)
    if not row['enabled']:
        raise DevError('PROFILE_DISABLED', '访问 Profile 已停用', 409)
    if type(version) is not int or version != row['version']:
        raise DevError('PROFILE_CHANGED', 'Profile 已变化，请重新读取并确认授权范围', 409)
    try:
        cap_scopes, cap_projects = stored_permissions(row)
    except (ValueError, TypeError, KeyError) as exc:
        raise DevError('INVALID_PROFILE', 'Profile 策略记录无效，未建立授权', 409) from exc
    if not set(scopes) <= cap_scopes or intersect_projects(projects, cap_projects) != sorted(set(projects)):
        raise DevError('PROFILE_SCOPE_EXCEEDED', '所选权限或项目超出 Profile 上限', 403)
    return row


def current_profile(store, principal):
    """One identity from authenticated credentials; accepts no model selector."""
    if principal.admin or not principal.grant_id:
        raise DevError('TOKEN_REQUIRED', '请使用需要识别的 MCP 连接凭据', 401)
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    if not grant or grant['user_id'] != principal.user_id:
        raise DevError('INVALID_TOKEN', '授权不存在', 401)
    scopes, projects, profile = effective_grant(store, grant)
    if profile:
        identity = {'id': profile['id'], 'name': 'CodePier', 'nickname': profile['label']}
    else:
        # Legacy grants represent the existing owner account, not new reusable
        # access profiles. Its persisted opaque user ID survives reconnection.
        # Do not pretend that different legacy tokens are distinct profiles.
        identity = {'id': principal.user_id, 'name': 'CodePier', 'nickname': '传统账号连接 · 按 grant 限权'}
    return identity, scopes, projects, profile


def access_context(store, principal):
    identity, scopes, projects, profile = current_profile(store, principal)
    rows = store.all('SELECT id,alias FROM projects WHERE space_id=? ORDER BY alias_key', (principal.space_id,))
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    extra = {'authorization_mode': 'fixed'}
    if grant.get('authorization_mode') == 'role':
        from hub.roles import role_context
        extra = role_context(store, grant)
    return {**extra, 'space_id': principal.space_id, 'profile': identity, 'managed': profile is not None, 'scopes': sorted(scopes),
            'projects': [row for row in rows if '*' in projects or row['id'] in projects],
            'all_projects': '*' in projects,
            'isolation': 'credential', 'chat_project_is_security_boundary': False,
            'note': '实际权限还受项目和 Agent 本机限制；操作与租约继续按原 grant 隔离，不按聊天名称授权。'}


def public_profile(row):
    return {'role_id': row.get('role_id')} | {key: row[key] for key in ('id', 'label', 'version', 'created', 'updated')} | {
        'scopes': json.loads(row['scopes']), 'projects': json.loads(row['projects']),
        'enabled': bool(row['enabled']), 'all_projects': json.loads(row['projects']) == ['*']}


class ProfileFields(AccessModel):
    label: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: ['read'], min_length=1, max_length=4)
    role_id: str | None = Field(default=None, min_length=1, max_length=100)
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False
    enabled: bool = True


class ProfileCreate(ProfileFields):
    idempotency_key: str = Field(min_length=8, max_length=128)


class ProfileUpdate(ProfileFields):
    expected_version: int = Field(ge=1)


def profile_values(store, body, user_id, space_id="legacy"):
    label = body.label.strip()
    if not label or any(ord(char) < 32 or ord(char) == 127 for char in label):
        raise DevError('INVALID_PROFILE', '名称不能为空或包含控制字符')
    scopes = set(body.scopes)
    if 'read' not in scopes or not scopes <= SCOPES:
        raise DevError('INVALID_SCOPE', 'Profile 必须包含 read，且仅支持 read/write/execute/computer')
    if body.role_id:
        iam.role_eligible(store, user_id, body.role_id, space_id)
    projects = [] if body.role_id and not body.projects and not body.all_projects else project_selection(store, body.projects, body.all_projects, space_id=space_id)
    if not body.role_id and not iam.is_space_admin(store, user_id, space_id):
        from hub.runtime import Principal
        candidate = Principal('', user_id, set(), [], space_id=space_id)
        if '*' in projects or any(not scopes <= iam.human_project_actions(store, candidate, pid) for pid in projects):
            raise DevError('INSUFFICIENT_SCOPE', 'Profile 不能超出账号现有权限', 403)
    return label, unicodedata.normalize('NFKC', label).casefold(), sorted(scopes), projects


def profile_audit(store, actor, action, profile_id, detail):
    store.audit(actor, action, profile_id, detail=detail, commit=False)


def make_profiles_router(auth, runtime):
    router, store = APIRouter(), runtime.store

    @router.get('/api/access-profiles')
    async def list_profiles(request: Request):
        principal = auth.panel(request)
        rows = store.all('SELECT * FROM access_profiles WHERE user_id=? AND space_id=? ORDER BY label_key,id LIMIT ?',
                         (principal.user_id, principal.space_id, MAX_PROFILES))
        items = []
        for row in rows:
            item = public_profile(row)
            if row.get('role_id'):
                from hub.roles import public_role
                role = store.one('SELECT * FROM access_roles WHERE id=? AND space_id=?', (row['role_id'], principal.space_id))
                item['role'] = public_role(role, store if principal.admin else None) if role else None
            items.append(item)
        return {'profiles': items, 'limit': MAX_PROFILES}

    @router.get('/api/access-profiles/{profile_id}')
    async def read_profile(profile_id: str, request: Request):
        principal = auth.panel(request)
        row = store.one('SELECT * FROM access_profiles WHERE id=? AND user_id=? AND space_id=?', (profile_id, principal.user_id, principal.space_id))
        if not row:
            raise DevError('PROFILE_NOT_FOUND', '访问 Profile 不存在', 404)
        return public_profile(row)

    @router.post('/api/access-profiles', status_code=201)
    async def create_profile(request: Request, body: ProfileCreate):
        principal = auth.panel(request, True)
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={'idempotency_key'}),
                                                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            auth.panel(request, True)
            old = store.one('SELECT * FROM access_profiles WHERE user_id=? AND space_id=? AND create_key=?',
                            (principal.user_id, principal.space_id, body.idempotency_key))
            if old:
                if old['create_fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '此幂等键已用于其他 Profile 参数，请先核查原记录', 409)
                return public_profile(old)
            label, label_key, scopes, projects = profile_values(store, body, principal.user_id, principal.space_id)
            if store.one('SELECT count(*) AS n FROM access_profiles WHERE user_id=?', (principal.user_id,))['n'] >= MAX_PROFILES:
                raise DevError('PROFILE_LIMIT', f'每个账号最多保存 {MAX_PROFILES} 个 Profile', 409)
            identifier, now = 'prf_' + uuid.uuid4().hex, time.time()
            try:
                store.db.execute('''INSERT INTO access_profiles
                    (id,user_id,label,label_key,scopes,projects,enabled,version,created,updated,create_key,create_fingerprint,role_id,space_id,owner_user_id)
                    VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)''',
                    (identifier, principal.user_id, label, label_key, json.dumps(scopes), json.dumps(projects),
                     int(body.enabled), now, now, body.idempotency_key, fingerprint, body.role_id, principal.space_id, principal.user_id))
            except sqlite3.IntegrityError as exc:
                raise DevError('PROFILE_EXISTS', '此账号已有同名 Profile，请使用不同名称', 409) from exc
            profile_audit(store, principal.actor, 'profile.created', identifier,
                          {'label': label, 'scopes': scopes, 'projects': projects, 'enabled': body.enabled, 'role_id': body.role_id})
            row = store.one('SELECT * FROM access_profiles WHERE id=?', (identifier,))
        return public_profile(row)

    @router.put('/api/access-profiles/{profile_id}')
    async def update_profile(profile_id: str, request: Request, body: ProfileUpdate):
        principal = auth.panel(request, True)
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            auth.panel(request, True)
            row = store.one('SELECT * FROM access_profiles WHERE id=? AND user_id=? AND space_id=?', (profile_id, principal.user_id, principal.space_id))
            if not row:
                raise DevError('PROFILE_NOT_FOUND', '访问 Profile 不存在', 404)
            label, label_key, scopes, projects = profile_values(store, body, principal.user_id, principal.space_id)
            selected = (label, scopes, projects, body.enabled, body.role_id)
            before = (row['label'], json.loads(row['scopes']), json.loads(row['projects']), bool(row['enabled']), row.get('role_id'))
            if row['version'] != body.expected_version and selected != before:
                raise DevError('PROFILE_CHANGED', 'Profile 已在其他窗口修改，请重新读取后核对', 409)
            if selected != before:
                try:
                    store.db.execute('''UPDATE access_profiles SET label=?,label_key=?,scopes=?,projects=?,enabled=?,
                        role_id=?,version=version+1,updated=? WHERE id=?''',
                        (label, label_key, json.dumps(scopes), json.dumps(projects), int(body.enabled), body.role_id, time.time(), profile_id))
                except sqlite3.IntegrityError as exc:
                    raise DevError('PROFILE_EXISTS', '此账号已有同名 Profile', 409) from exc
                profile_audit(store, principal.actor, 'profile.updated', profile_id,
                              {'before': public_profile(row), 'label': label, 'scopes': scopes, 'projects': projects,
                                'enabled': body.enabled, 'role_id': body.role_id, 'existing_grant_consent_unchanged': True})
            row = store.one('SELECT * FROM access_profiles WHERE id=?', (profile_id,))
        runtime.wake.set()
        return public_profile(row)

    return router
