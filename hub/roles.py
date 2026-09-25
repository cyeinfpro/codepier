"""Owner-managed live roles. Rule actions are always evaluated WITH their resources.

Only an explicitly consented role grant follows role edits. A role never makes a
caller a panel administrator; identities, operation ownership and local gates stay
separate. No model-supplied role selector is accepted by the runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, ValidationError, model_validator

from hub.access import AccessModel
from shared.role_contracts import ROLE_SCOPE
from shared.util import DevError
from hub import iam

CAPABILITIES = frozenset({'read', 'write', 'execute', 'computer'})
MAX_ROLES = 200


class ProjectRule(AccessModel):
    actions: list[Literal['read', 'write', 'execute', 'computer']] = Field(min_length=1, max_length=4)
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False
    created_projects: bool = False
    excluded_projects: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def bounded_ids(self):
        if 'read' not in self.actions:
            raise ValueError('每条项目规则必须包含 read；写入与执行不隐含其他项目的权限')
        _ids(self.projects + self.excluded_projects)
        if not self.projects and not self.all_projects and not self.created_projects:
            raise ValueError('请选择项目、全部现有及未来项目，或本角色创建的项目')
        return self


class DeviceRule(AccessModel):
    actions: list[Literal['devices.read', 'projects.create']] = Field(min_length=1, max_length=2)
    devices: list[str] = Field(min_length=1, max_length=100)
    root_prefixes: list[str] = Field(default_factory=list, max_length=100)
    max_project_mode: Literal['read', 'write'] = 'read'
    allow_tasks: bool = False

    @model_validator(mode='after')
    def valid_device_rule(self):
        _ids(self.devices)
        if 'devices.read' not in self.actions:
            raise ValueError('设备规则必须包含 devices.read')
        for value in self.root_prefixes:
            _absolute_path(value)
        if self.allow_tasks and self.max_project_mode != 'write':
            raise ValueError('允许项目任务时，映射模式上限必须为 write')
        return self


class RolePolicy(AccessModel):
    project_rules: list[ProjectRule] = Field(default_factory=list, max_length=32)
    device_rules: list[DeviceRule] = Field(default_factory=list, max_length=32)


class RoleFields(RolePolicy):
    label: str = Field(min_length=1, max_length=80)
    enabled: bool = True


class RoleCreate(RoleFields):
    idempotency_key: str = Field(min_length=8, max_length=128)


class RoleUpdate(RoleFields):
    expected_version: int = Field(ge=1)


def _ids(values):
    if any(not isinstance(x, str) or not x or len(x) > 100 or x == '*' or any(ord(c) < 32 for c in x) for x in values):
        raise ValueError('资源必须使用明确 ID，不能用名称、空值或 *；未来项目使用专用开关')


def _absolute_path(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or '\x00' in value:
        raise ValueError('目录上限必须是有效绝对路径')
    path = PureWindowsPath(value) if re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\') else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('目录上限必须是没有 .. 的绝对路径')
    return path


def _policy(row):
    try:
        return RolePolicy.model_validate(json.loads(row['policy']))
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        raise DevError('INVALID_TOKEN', '角色策略记录无效，未授予访问权限', 401) from exc


def role_binding(store, grant):
    """Validate the immutable grant binding; policy changes never change identity."""
    if not grant or grant.get('authorization_mode', 'fixed') != 'role' or grant.get('revoked'):
        raise DevError('INVALID_TOKEN', '角色授权不存在或已撤销', 401)
    try:
        if json.loads(grant['scopes']) != [ROLE_SCOPE] or not grant.get('role_id') or not grant.get('profile_id'):
            raise ValueError('invalid role grant')
        profile = store.one('SELECT * FROM access_profiles WHERE id=?', (grant['profile_id'],))
        role = store.one('SELECT * FROM access_roles WHERE id=?', (grant['role_id'],))
        if (not profile or not profile['enabled'] or profile['user_id'] != grant['user_id']
                or profile.get('role_id') != grant['role_id'] or not role
                or profile.get('space_id','legacy') != grant.get('space_id','legacy')
                or role.get('space_id','legacy') != grant.get('space_id','legacy')):
            raise ValueError('role/profile binding changed')
        return role, _policy(role), profile
    except (ValueError, TypeError, KeyError) as exc:
        raise DevError('INVALID_TOKEN', '角色与身份绑定已变化、停用或失效；未继承其他角色', 401) from exc


def created_ids(store, role_id):
    return {r['project_id'] for r in store.all('SELECT project_id FROM role_created_projects WHERE role_id=?', (role_id,))}


def project_actions(policy, project_id, created=()):
    result = set()
    for rule in policy.project_rules:
        if project_id in rule.excluded_projects:
            continue
        if rule.all_projects or project_id in rule.projects or rule.created_projects and project_id in created:
            result.update(rule.actions)
    return result


def effective_role(store, grant):
    iam.validate_grant(store, grant)
    role, policy, profile = role_binding(store, grant)
    # A disabled role can still identify itself / refresh credentials, but has no
    # resource privileges. It is a policy stop, not an invitation to re-OAuth.
    scopes, projects = {'read'}, []
    if role['enabled']:
        created = created_ids(store, role['id'])
        for row in store.all('SELECT id FROM projects WHERE space_id=?', (grant.get('space_id','legacy'),)):
            actions = project_actions(policy, row['id'], created)
            if actions:
                projects.append(row['id'])
                scopes.update(actions)
        for rule in policy.device_rules:
            scopes.update(rule.actions)
    return scopes, sorted(projects), profile


def principal_grant(store, principal):
    if principal.admin or not principal.grant_id:
        return None
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    if grant and grant.get('authorization_mode') == 'role':
        if grant['user_id'] != principal.user_id or grant['profile_id'] != principal.profile_id:
            raise DevError('INVALID_TOKEN', '角色凭据身份不匹配', 401)
        return grant
    if getattr(principal, 'authorization_mode', 'fixed') == 'role':
        raise DevError('INVALID_TOKEN', '角色授权已变化', 401)
    return None


def require_role(store, principal, action, *, project_id=None, device_id=None, creation=None):
    """Additional gate, not a substitute for fixed scopes or Agent constraints."""
    if iam.installed(store):
        principal = iam.live_principal(store, principal)
        if project_id is not None:
            iam.require_project(store, principal, action, project_id)
        if device_id is not None:
            row = store.one('SELECT id FROM devices WHERE id=? AND space_id=?', (device_id, principal.space_id))
            if not row:
                raise DevError('ROLE_POLICY_DENIED', '当前角色未允许此设备操作', 403)
            if not principal.grant_id:
                iam.require_device(store, principal, device_id, creation=creation)
                return None
    if principal.admin:
        return None
    grant = principal_grant(store, principal)
    if not grant:
        if action in {'devices.read', 'projects.create'} and principal.grant_id:
            raise DevError('ROLE_REQUIRED', '此管理能力需要明确同意的动态角色授权', 403)
        return None
    role, policy, _ = role_binding(store, grant)
    if action in {'get_profile', 'get_access_context'}:
        return role
    if not role['enabled']:
        raise DevError('ROLE_POLICY_DENIED', '角色已暂停；由主理人恢复政策，不需要重新登录', 403)
    if project_id is not None:
        if action in project_actions(policy, project_id, created_ids(store, role['id'])):
            return role
    elif device_id is not None:
        for rule in policy.device_rules:
            if device_id not in rule.devices or action not in rule.actions:
                continue
            if creation is not None:
                if creation.get('mode') == 'write' and rule.max_project_mode != 'write':
                    continue
                if creation.get('allow_tasks') and not rule.allow_tasks:
                    continue
                try:
                    path = _absolute_path(creation['root'])
                    if rule.root_prefixes and not any(type(path) is type(prefix := _absolute_path(p)) and path.is_relative_to(prefix) for p in rule.root_prefixes):
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
            return role
    elif action == 'read':
        # Discovery only. Concrete resource checks remain mandatory below.
        return role
    raise DevError('ROLE_POLICY_DENIED', '当前角色未允许对该资源执行此操作；请由主理人调整角色政策', 403)


def require_new_mapping(store, principal, device_id, root):
    """Delegation cannot remap an existing restricted project under a new alias.

    Both raw and Agent-canonical paths are checked by the create workflow. Admins
    retain explicit control over intentional nested / overlapping mappings.
    """
    if principal.admin:
        return
    target = _absolute_path(root)
    for row in store.all('SELECT root FROM projects WHERE device_id=?', (device_id,)):
        try:
            existing = _absolute_path(row['root'])
        except ValueError as exc:
            raise DevError('PROJECT_ROOT_UNVERIFIED', '已有映射路径无法核对，请由主理人检查', 409) from exc
        if type(target) is type(existing) and (target.is_relative_to(existing) or existing.is_relative_to(target)):
            raise DevError('PROJECT_ROOT_OVERLAP', '委派创建不能重映射已有项目、父目录或子目录；需要主理人明确处理', 403)


def role_project_scopes(store, principal, project_id):
    if iam.installed(store) and not principal.grant_id:
        return iam.human_project_actions(store, principal, project_id)
    grant = principal_grant(store, principal)
    if not grant:
        return set(principal.scopes)
    role, policy, _ = role_binding(store, grant)
    return project_actions(policy, project_id, created_ids(store, role['id'])) if role['enabled'] else set()


def role_context(store, grant):
    role, policy, _ = role_binding(store, grant)
    created = created_ids(store, role['id'])
    rows = store.all('SELECT id,alias FROM projects WHERE space_id=? ORDER BY alias_key', (grant.get('space_id','legacy'),))
    return {'authorization_mode': 'role', 'role': {'id': role['id'], 'label': role['label'], 'version': role['version'], 'enabled': bool(role['enabled'])},
            'project_permissions': [{'id': r['id'], 'alias': r['alias'], 'actions': sorted(actions)}
                for r in rows if role['enabled'] and (actions := project_actions(policy, r['id'], created))],
            'device_rules': [r.model_dump() for r in policy.device_rules] if role['enabled'] else [],
            'policy_follows_role': True, 'initial_consent_is_resource_ceiling': False}


def validate_role_consent(store, user_id, profile_id, profile_version, role_version, confirmed, *, space_id="legacy"):
    if confirmed is not True:
        raise DevError('DYNAMIC_CONSENT_REQUIRED', '请明确同意角色未来的能力及项目变更，不会自动升级旧授权', 400)
    profile = store.one('SELECT * FROM access_profiles WHERE id=? AND user_id=? AND space_id=?', (profile_id, user_id, space_id))
    if not profile or not profile['enabled'] or not profile.get('role_id'):
        raise DevError('INVALID_PROFILE', '请选择已启用并绑定角色的 Profile', 400)
    role = iam.role_eligible(store, user_id, profile['role_id'], space_id)
    if not role or not role['enabled']:
        raise DevError('ROLE_DISABLED', '角色不存在或已暂停', 409)
    if type(profile_version) is not int or profile_version != profile['version'] or type(role_version) is not int or role_version != role['version']:
        raise DevError('ROLE_CHANGED', '角色或身份在确认期间已变化，请重新读取政策后确认', 409)
    _policy(role)
    return role


def public_role(row, store=None):
    result = {k: row[k] for k in ('id', 'label', 'version', 'created', 'updated')}
    result.update(enabled=bool(row['enabled']), **_policy(row).model_dump())
    if store is not None:
        result['bound_profiles'] = store.one('SELECT count(*) AS n FROM access_profiles WHERE role_id=?', (row['id'],))['n']
        result['active_grants'] = store.one("SELECT count(*) AS n FROM grants WHERE role_id=? AND authorization_mode='role' AND revoked=0", (row['id'],))['n']
    return result


def role_values(store, body, space_id="legacy"):
    label = body.label.strip()
    if not label or any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise DevError('INVALID_ROLE', '名称不能为空或包含控制字符')
    projects = {r['id'] for r in store.all('SELECT id FROM projects WHERE space_id=?', (space_id,))}
    devices = {r['id'] for r in store.all('SELECT id FROM devices WHERE space_id=?', (space_id,))}
    if any(not set(rule.projects + rule.excluded_projects) <= projects for rule in body.project_rules):
        raise DevError('INVALID_PROJECT', '角色中包含已不存在的项目；请重新读取后明确移除旧引用')
    if any(not set(rule.devices) <= devices for rule in body.device_rules):
        raise DevError('INVALID_DEVICE', '角色中包含不存在的设备')
    policy = body.model_dump(include={'project_rules', 'device_rules'})
    return label, unicodedata.normalize('NFKC', label).casefold(), json.dumps(policy, sort_keys=True, ensure_ascii=False)


def make_roles_router(auth, runtime):
    from hub.access_profiles import profile_audit
    router, store = APIRouter(), runtime.store

    @router.get('/api/access-roles')
    async def list_roles(request: Request):
        owner = auth.panel(request)
        rows = store.all('SELECT * FROM access_roles WHERE space_id=? ORDER BY label_key,id LIMIT ?', (owner.space_id, MAX_ROLES)) if owner.admin else iam.assigned_roles(store, owner.user_id, owner.space_id)
        return {'roles': [public_role(row, store if owner.admin else None) for row in rows], 'limit': MAX_ROLES}

    @router.get('/api/access-roles/{identifier}')
    async def get_role(identifier: str, request: Request):
        owner = auth.panel(request)
        row = store.one('SELECT * FROM access_roles WHERE id=? AND space_id=?', (identifier, owner.space_id))
        if row and not owner.admin:
            iam.role_eligible(store, owner.user_id, row['id'], owner.space_id, delegate=False)
        if not row:
            raise DevError('ROLE_NOT_FOUND', '角色不存在', 404)
        return public_role(row, store if owner.admin else None)

    @router.post('/api/access-roles', status_code=201)
    async def create_role(request: Request, body: RoleCreate):
        owner = auth.admin(request, True)
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={'idempotency_key'}), sort_keys=True).encode()).hexdigest()
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            auth.admin(request, True)
            old = store.one('SELECT * FROM access_roles WHERE user_id=? AND space_id=? AND create_key=?', (owner.user_id, owner.space_id, body.idempotency_key))
            if old:
                if old['create_fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '幂等键已用于另一份角色配置', 409)
                return public_role(old, store)
            label, key, policy = role_values(store, body, owner.space_id)
            if store.one('SELECT count(*) AS n FROM access_roles WHERE user_id=?', (owner.user_id,))['n'] >= MAX_ROLES:
                raise DevError('ROLE_LIMIT', '角色数量达到上限', 409)
            identifier, now = 'rol_' + uuid.uuid4().hex, time.time()
            try:
                store.db.execute('INSERT INTO access_roles(id,user_id,label,label_key,policy,enabled,version,created,updated,create_key,create_fingerprint,space_id,owner_user_id) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?)',
                    (identifier, owner.user_id, label, key, policy, int(body.enabled), now, now, body.idempotency_key, fingerprint, owner.space_id, owner.user_id))
            except sqlite3.IntegrityError as exc:
                raise DevError('ROLE_EXISTS', '已有同名角色', 409) from exc
            row = store.one('SELECT * FROM access_roles WHERE id=?', (identifier,))
            profile_audit(store, owner.actor, 'role.created', identifier, public_role(row))
        return public_role(row, store)

    @router.put('/api/access-roles/{identifier}')
    async def update_role(identifier: str, request: Request, body: RoleUpdate):
        owner = auth.admin(request, True)
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            auth.admin(request, True)
            row = store.one('SELECT * FROM access_roles WHERE id=? AND space_id=?', (identifier, owner.space_id))
            if not row:
                raise DevError('ROLE_NOT_FOUND', '角色不存在', 404)
            label, key, policy = role_values(store, body, owner.space_id)
            same = (label, json.loads(policy), body.enabled) == (row['label'], json.loads(row['policy']), bool(row['enabled']))
            if body.expected_version != row['version'] and not same:
                raise DevError('ROLE_CHANGED', '角色已在其他窗口修改，请重新读取后核对', 409)
            if not same:
                before = public_role(row, store)
                try:
                    store.db.execute('UPDATE access_roles SET label=?,label_key=?,policy=?,enabled=?,version=version+1,updated=? WHERE id=?',
                                     (label, key, policy, int(body.enabled), time.time(), identifier))
                except sqlite3.IntegrityError as exc:
                    raise DevError('ROLE_EXISTS', '已有同名角色', 409) from exc
                row = store.one('SELECT * FROM access_roles WHERE id=?', (identifier,))
                profile_audit(store, owner.actor, 'role.updated', identifier, {'before': before, 'after': public_role(row, store), 'applies_to_existing_role_grants': True})
        runtime.wake.set()
        return public_role(row, store)

    return router
