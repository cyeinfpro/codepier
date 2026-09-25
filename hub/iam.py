"""Live application identity, Space membership and resource authorization.

No caller can select its role, user, administrator flag or Space through tool
arguments. HTTP selects a Space for a *human* after membership verification;
MCP credentials carry a fixed Space binding. All predicates query current state.
"""
from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar

audit_context = ContextVar("codepier_audit_context", default=None)
from dataclasses import replace

from shared.util import DevError

CAPABILITIES = {'read', 'write', 'execute', 'computer'}
LEVELS = {'guest': 0, 'member': 1, 'admin': 2, 'owner': 3}


def installed(store):
    return getattr(store, 'iam_enabled', False)


def user_security(store, user_id):
    row = store.one('SELECT u.*,s.active,s.instance_admin,s.local_login,s.epoch,s.version,s.display_name FROM users u JOIN iam_users s ON s.user_id=u.id WHERE u.id=?', (user_id,))
    if not row or not row['active']:
        raise DevError('ACCOUNT_DISABLED', '账号不存在或已停用', 401)
    return row


def check_identity(store, identity_id, *, require_fresh=True):
    if not identity_id:
        return None
    row = store.one('SELECT i.*,p.enabled AS provider_enabled FROM external_identities i JOIN oidc_providers p ON p.id=i.provider_id WHERE i.id=?', (identity_id,))
    if not row or not row['enabled'] or not row['provider_enabled']:
        raise DevError('IDENTITY_DISABLED', '外部身份或身份提供者已停用', 401)
    if require_fresh and row['fresh_until'] <= time.time():
        raise DevError('ENTITLEMENTS_STALE', '身份权限校验已过期；请在面板重新登录身份提供者', 403)
    return row


def membership(store, user_id, space_id):
    space = store.one('SELECT * FROM spaces WHERE id=? AND active=1', (space_id,))
    if not space:
        raise DevError('SPACE_NOT_FOUND', '没有可用的空间', 404)
    if store.one('SELECT 1 AS blocked FROM membership_blocks WHERE user_id=? AND space_id=? AND blocked=1',(user_id,space_id)):
        raise DevError('SPACE_FORBIDDEN', '空间成员资格已暂停', 403)
    rows = store.all('SELECT * FROM memberships WHERE user_id=? AND space_id=? AND active=1 AND (expires IS NULL OR expires>?)', (user_id, space_id, time.time()))
    if not rows:
        raise DevError('SPACE_FORBIDDEN', '你已不属于此空间', 403)
    return max(rows, key=lambda row: LEVELS[row['level']])


def is_space_admin(store, user_id, space_id):
    try:
        user_security(store, user_id)
        return LEVELS[membership(store, user_id, space_id)['level']] >= LEVELS['admin']
    except DevError:
        return False


def role_eligible(store, user_id, role_id, space_id, *, delegate=True):
    role = store.one('SELECT * FROM access_roles WHERE id=? AND space_id=?', (role_id, space_id))
    if not role:
        raise DevError('ROLE_NOT_FOUND', '角色不存在于当前空间', 404)
    membership(store, user_id, space_id)
    if not is_space_admin(store, user_id, space_id):
        row = store.one('''SELECT 1 AS ok FROM role_assignments WHERE user_id=? AND role_id=? AND space_id=?
            AND active=1 AND (expires IS NULL OR expires>?) AND (?=0 OR may_delegate=1) LIMIT 1''',
            (user_id, role_id, space_id, time.time(), int(delegate)))
        if not row:
            raise DevError('ROLE_ASSIGNMENT_REQUIRED', '当前账号没有使用/委派此角色的权限', 403)
    return role


def validate_grant(store, grant):
    if not installed(store):
        return
    if not grant or grant.get('revoked'):
        raise DevError('INVALID_TOKEN', '授权已撤销', 401)
    user = user_security(store, grant['user_id'])
    if user['epoch'] != grant.get('user_epoch', 1):
        raise DevError('INVALID_TOKEN', '用户授权版本已改变', 401)
    membership(store, grant['user_id'], grant.get('space_id', 'legacy'))
    identity = check_identity(store, grant.get('identity_id'))
    if identity and identity['user_id'] != grant['user_id']:
        raise DevError('INVALID_TOKEN', '身份绑定不匹配', 401)
    if grant.get('authorization_mode') == 'role':
        role_eligible(store, grant['user_id'], grant['role_id'], grant.get('space_id', 'legacy'))


def live_principal(store, principal):
    if not installed(store):
        return principal
    user = user_security(store, principal.user_id)
    member = membership(store, principal.user_id, principal.space_id)
    if principal.user_epoch is not None and user['epoch'] != principal.user_epoch:
        raise DevError('INVALID_TOKEN', '用户会话/授权已失效', 401)
    identity = check_identity(store, principal.identity_id)
    if identity and identity['user_id'] != principal.user_id:
        raise DevError('INVALID_TOKEN', '身份绑定不匹配', 401)
    if principal.grant_id:
        grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
        validate_grant(store, grant)
        if grant['user_id'] != principal.user_id or grant['space_id'] != principal.space_id:
            raise DevError('INVALID_TOKEN', '授权范围不匹配', 401)
        return replace(principal, admin=False, instance_admin=False, user_epoch=user['epoch'])
    # Only server-originated panel principals reach this branch. Member roles are
    # recomputed after awaits; an old in-memory admin flag is never authoritative.
    elevated = LEVELS[member['level']] >= LEVELS['admin']
    projects = store.all('SELECT id FROM projects WHERE space_id=?', (principal.space_id,))
    provisional = replace(principal, admin=elevated, instance_admin=bool(user['instance_admin']), user_epoch=user['epoch'])
    scopes = {'read'}
    visible = []
    for project in projects:
        actions = human_project_actions(store, provisional, project['id'])
        if actions:
            visible.append(project['id']); scopes.update(actions)
    if elevated:
        scopes |= CAPABILITIES
    return replace(provisional, scopes=scopes, projects=visible)


def assigned_roles(store, user_id, space_id):
    now = time.time()
    return store.all('''SELECT DISTINCT r.* FROM access_roles r JOIN role_assignments a ON a.role_id=r.id
        WHERE r.space_id=? AND a.space_id=r.space_id AND a.user_id=? AND a.active=1
          AND r.enabled=1 AND (a.expires IS NULL OR a.expires>?)''', (space_id, user_id, now))


def human_project_actions(store, principal, project_id):
    project = store.one('SELECT space_id FROM projects WHERE id=?', (project_id,))
    if not project or project['space_id'] != principal.space_id:
        return set()
    if is_space_admin(store, principal.user_id, principal.space_id):
        return set(CAPABILITIES)
    from hub.roles import _policy, project_actions, created_ids
    result = set()
    for role in assigned_roles(store, principal.user_id, principal.space_id):
        result |= project_actions(_policy(role), project_id, created_ids(store, role['id']))
    return result


def project_in_space(store, principal, project_id):
    row = store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (project_id, principal.space_id))
    if not row:
        raise DevError('PROJECT_NOT_FOUND', '未找到当前空间中的已授权项目', 404)
    return row


def require_project(store, principal, action, project_id):
    if not installed(store):
        return
    user_security(store, principal.user_id)
    membership(store, principal.user_id, principal.space_id)
    project_in_space(store, principal, project_id)
    if (not principal.grant_id or principal.authorization_mode == 'fixed') and action not in human_project_actions(store, principal, project_id):
        raise DevError('ROLE_POLICY_DENIED', '当前账号未获授予此项目的操作权限', 403)



def device_identity_active(store, device):
    """Machine authentication is distinct from a browser login or an IdP token."""
    if not device or not device.get('enabled'):
        return False
    owner=device.get('owner_user_id')
    if owner is None:
        # Compatibility for old device records: never extend this to new Spaces.
        return device.get('space_id')=='legacy' and bool(store.one("SELECT 1 AS ok FROM spaces WHERE id='legacy' AND active=1"))
    try:
        user_security(store,owner)
        membership(store,owner,device['space_id'])
        return True
    except DevError:
        return False


def require_device(store, principal, device_id, *, manage=False, creation=None):
    principal = live_principal(store, principal)
    row = store.one('SELECT * FROM devices WHERE id=? AND space_id=?', (device_id, principal.space_id))
    if not row:
        raise DevError('DEVICE_NOT_FOUND', '找不到当前空间的设备', 404)
    if principal.admin or (not principal.grant_id and row['owner_user_id'] == principal.user_id and not creation):
        return row
    if principal.grant_id:
        if manage:
            raise DevError('OWNER_REQUIRED', 'MCP 不具有设备管理权', 403)
        from hub.roles import require_role
        require_role(store, principal, 'projects.create' if creation else 'devices.read', device_id=device_id, creation=creation)
        return row
    if manage:
        raise DevError('DEVICE_FORBIDDEN', '只能管理自己的设备', 403)
    from hub.roles import _policy, _absolute_path
    for role in assigned_roles(store, principal.user_id, principal.space_id):
        for rule in _policy(role).device_rules:
            if device_id not in rule.devices:
                continue
            if not creation:
                return row
            if 'projects.create' not in rule.actions or creation.get('mode') == 'write' and rule.max_project_mode != 'write' or creation.get('allow_tasks') and not rule.allow_tasks:
                continue
            target = _absolute_path(creation['root'])
            if not rule.root_prefixes or any(type(target) is type(prefix := _absolute_path(root)) and target.is_relative_to(prefix) for root in rule.root_prefixes):
                return row
    # A device owner can map their own approved paths. Local Agent validation
    # still determines whether those roots may be written or execute commands.
    if row['owner_user_id'] == principal.user_id:
        return row
    raise DevError('DEVICE_FORBIDDEN', '设备操作未获授权', 403)


def record_visible(store, principal, row):
    """Private execution history is not shared merely by a shared Role/project."""
    if not installed(store):
        return principal.admin or row.get('grant_id') == principal.grant_id
    if not row or row.get('space_id') != principal.space_id:
        return False
    if principal.grant_id:
        return row.get('grant_id') == principal.grant_id or row.get('visibility') == 'space'
    # The instance operator is a trusted, audited recovery actor. Space admins
    # alone do NOT receive other humans' private sessions/outputs.
    return (principal.instance_admin or row.get('owner_user_id') == principal.user_id
            or row.get('visibility') == 'space')


def require_record(store, principal, row, *, kind='OPERATION'):
    principal = live_principal(store, principal)
    if not record_visible(store, principal, row):
        raise DevError(kind + '_NOT_FOUND', '找不到此身份授权范围内的记录', 404)
    if row.get('project_id'):
        if '*' not in principal.projects and row['project_id'] not in principal.projects:
            raise DevError(kind + '_NOT_FOUND', '找不到此身份授权范围内的记录', 404)
        require_project(store, principal, 'read', row['project_id'])
    return principal


def scope_sql(principal, alias=''):
    return f'{alias}space_id=?', [principal.space_id]


def private_sql(principal, alias=''):
    clause, args = scope_sql(principal, alias)
    if principal.grant_id:
        clause += f" AND ({alias}grant_id=? OR {alias}visibility='space')"; args.append(principal.grant_id)
    elif not principal.instance_admin:
        clause += f" AND ({alias}owner_user_id=? OR {alias}visibility='space')"; args.append(principal.user_id)
    return clause, args


def event_visible(runtime, principal, item):
    """Event contents are not emitted until current authorization is checked."""
    kind, data = item.get('type'), item.get('data') or {}
    if kind == 'iam':
        return data.get('user_id') == principal.user_id or data.get('space_id') == principal.space_id
    if kind == 'device':
        try: require_device(runtime.store, principal, data.get('id')); return True
        except DevError: return False
    if kind == 'project':
        try: runtime.project(data.get('id'), principal); return True
        except DevError: return False
    if kind in {'operation', 'operation_event', 'output', 'trace'}:
        try: runtime.operation_row(data.get('id') or data.get('operation_id'), principal); return True
        except DevError: return False
    if kind == 'workflow':
        try: runtime.workflows.load(data.get('id'), principal); return True
        except DevError: return False
    if kind == 'vps':
        try:
            if data.get('id'):runtime.vps.get(data['id'],principal)
            elif data.get('project_id'):runtime.project(data['project_id'],principal)
            else:return False
            return True
        except DevError:return False
    if kind == 'computer_approval':
        # An invalidation has no native content. Requiring a currently nonempty
        # inbox would suppress the last item's removal and leave stale controls.
        audience = item.get('_audience') or {}
        return (audience.get('space_id') == principal.space_id
                and (audience.get('user_id') == principal.user_id or principal.instance_admin))
    # Unknown/global notifications are not broadcast to arbitrary members.
    return False


def audit(store, principal, action, target='', status='ok', detail=None):
    """Participates in the caller's transaction; does not commit it."""
    store.db.execute('INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?,?)',
        (time.time(), principal.actor, action, target, status, json.dumps(detail or {}, ensure_ascii=False), principal.space_id, principal.user_id))


def create_personal_space(store, user_id, label):
    space_id = 'sp_' + uuid.uuid4().hex
    store.db.execute('INSERT INTO spaces(id,label,kind,created) VALUES(?,?,?,?)', (space_id, label[:100], 'personal', time.time()))
    store.db.execute('INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,?)', (space_id, user_id, 'owner'))
    return space_id


class AuditContextMiddleware:
    def __init__(self, app):self.app=app
    async def __call__(self, scope, receive, send):
        key=audit_context.set(None)
        try:await self.app(scope,receive,send)
        finally:audit_context.reset(key)
