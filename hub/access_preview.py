"""Read-only Hub permission explanations for a connection owned by the viewer.

Never invoke a tool, authenticate as the grant, dispatch work or return credential
material. The representative operations below are policy checks, not a promise
that an Agent, path, command, app, lease or approval will permit an actual call.
"""
from __future__ import annotations

import time

from hub import iam
from hub.principal import Principal
from shared.contracts import MUTATING, PROCESS_TOOLS, TOOLS
from shared.util import DevError

ACTION_TOOLS = {
    'read': 'fs_read',
    'write': 'fs_write',
    'execute': 'shell_exec',
    'computer': 'computer_action',
}


def _reason(code, message):
    return {'code': code, 'message': message}


def _public_binding(row):
    return ({key: row[key] for key in ('id', 'label', 'version')}
            | {'enabled': bool(row['enabled'])}) if row else None


def _credential_state(store, grant, now, expected_resource):
    # Select only metadata. Hashes, token IDs, secrets and OAuth codes never
    # enter the preview or its error messages.
    rows = store.all('SELECT kind,expires FROM tokens WHERE grant_id=?', (grant['id'],))
    access = [r['expires'] for r in rows if r['kind'] in {'access', 'pat'}]
    refresh = [r['expires'] for r in rows if r['kind'] == 'refresh']
    expiry = max(access, default=None)
    renewal = max(refresh, default=None)
    if grant['revoked']:
        return 'revoked', expiry, renewal, _reason('GRANT_REVOKED', '连接已撤销')
    live = [r for r in rows if r['kind'] in {'access', 'pat'} and r['expires'] > now]
    if any(r['kind'] == 'pat' or not expected_resource or grant.get('resource') == expected_resource for r in live):
        return 'active', expiry, renewal, None
    if live:
        return 'blocked', expiry, renewal, _reason('INVALID_TOKEN', '连接的 MCP 资源标识已改变，需要重新连接')
    if renewal and renewal > now:
        return 'refresh_required', expiry, renewal, _reason('TOKEN_REFRESH_REQUIRED', '访问凭据已过期，客户端尚需刷新；未验证刷新结果')
    pending = store.one('SELECT expires FROM oauth_codes WHERE grant_id=? AND expires>? LIMIT 1', (grant['id'], now))
    if pending:
        return 'pending', expiry, renewal, _reason('CONNECTION_PENDING', '等待客户端完成 OAuth 凭据兑换')
    if rows:
        return 'expired', expiry, renewal, _reason('TOKEN_EXPIRED', '连接凭据已过期')
    return 'unavailable', expiry, renewal, _reason('CREDENTIAL_UNAVAILABLE', '没有可用凭据；未确认连接完成')


def _action(store, principal, project, action, blocked):
    from hub.roles import require_role

    tool = ACTION_TOOLS[action]
    reason = blocked
    if reason is None:
        try:
            scope = TOOLS[tool].scope
            if scope not in principal.scopes:
                code = 'ROLE_POLICY_DENIED' if principal.authorization_mode == 'role' else 'INSUFFICIENT_SCOPE'
                raise DevError(code, f'当前连接缺少 {scope} 权限', 403)
            if '*' not in principal.projects and project['id'] not in principal.projects:
                raise DevError('PROJECT_NOT_GRANTED', '项目不在当前连接的有效授权范围内（原始同意、Profile 或账号政策）', 403)
            # Match Runtime.project and its per-operation gate. In particular,
            # the union of dynamic scopes must never become an action/resource
            # cross-product. These predicates do not audit or dispatch.
            require_role(store, principal, 'read', project_id=project['id'])
            require_role(store, principal, scope, project_id=project['id'])
            if tool in MUTATING and project['mode'] != 'write':
                raise DevError('READ_ONLY', 'Hub 中此项目设为只读', 403)
            if tool in PROCESS_TOOLS and not project['allow_tasks']:
                raise DevError('TASKS_DISABLED', 'Hub 中此项目未允许执行任务', 403)
        except DevError as exc:
            reason = _reason(exc.code, exc.message)
    allowed = reason is None
    return {
        'allowed': allowed,
        'status': 'allowed' if allowed else 'denied',
        'reason': reason or _reason('HUB_POLICY_ALLOWED', '当前 Hub 权限允许；节点和具体操作限制仍需在调用时检查'),
        'representative_tool': tool,
    }


@iam.read_decision
def grant_access_preview(store, viewer, grant_id, *, expected_resource=None):
    from hub.access_profiles import effective_grant

    viewer = iam.live_principal(store, viewer)
    grant = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
                      (grant_id, viewer.user_id, viewer.space_id))
    if not grant:
        raise DevError('NOT_FOUND', '授权不存在', 404)
    now = time.time()
    owner = store.one('SELECT u.id,u.username,s.display_name FROM users u JOIN iam_users s ON s.user_id=u.id WHERE u.id=?', (viewer.user_id,))
    space = store.one('SELECT id,label FROM spaces WHERE id=?', (viewer.space_id,))
    # Even corrupt bindings must not disclose another user's Profile/Space.
    profile = store.one('SELECT id,label,version,enabled FROM access_profiles WHERE id=? AND user_id=? AND space_id=?',
                        (grant.get('profile_id'), viewer.user_id, viewer.space_id))
    role = store.one('SELECT id,label,version,enabled FROM access_roles WHERE id=? AND space_id=?',
                     (grant.get('role_id'), viewer.space_id))
    authorization_mode = grant.get('authorization_mode', 'fixed')
    mode = ('fixedProfile' if grant.get('profile_id') else 'fixed') if authorization_mode == 'fixed' else authorization_mode
    state, expires, renewal, blocked = _credential_state(store, grant, now, expected_resource)
    scopes, project_ids = set(), []
    try:
        scopes, project_ids, _ = effective_grant(store, grant)
    except DevError as exc:
        if blocked is None:
            state, blocked = 'blocked', _reason(exc.code, exc.message)
    if blocked is None and authorization_mode == 'role' and role and not role['enabled']:
        state, blocked = 'paused', _reason('ROLE_POLICY_DENIED', '动态角色已暂停')
    subject = Principal('', grant['user_id'], scopes, project_ids,
                        grant_id=grant['id'], profile_id=grant.get('profile_id'),
                        authorization_mode=authorization_mode, role_id=grant.get('role_id'),
                        space_id=grant['space_id'], identity_id=grant.get('identity_id'),
                        user_epoch=grant.get('user_epoch', 1))
    permissions = iam.project_permissions(store, viewer)
    rows = store.all('SELECT id,alias,mode,allow_tasks FROM projects WHERE space_id=? ORDER BY alias_key,id', (viewer.space_id,))
    # A member may inspect their own connection but may not enumerate projects
    # hidden from their panel session, even as denied entries.
    projects = [{
        'id': row['id'], 'alias': row['alias'],
        'actions': {action: _action(store, subject, row, action, blocked) for action in ACTION_TOOLS},
    } for row in rows if viewer.admin or permissions.get(row['id'])]
    return {
        'grant': {'id': grant['id'], 'label': grant['label'], 'mode': mode,
                  'authorization_mode': authorization_mode, 'revoked': bool(grant['revoked']),
                  'state': state, 'expires_at': expires, 'refresh_expires_at': renewal,
                  'reason': blocked},
        'owner': {'id': owner['id'], 'label': owner['display_name'] or owner['username']},
        'space': space,
        'profile': _public_binding(profile), 'role': _public_binding(role),
        'projects': projects, 'checked_at': now,
        'evaluation': 'current_hub_policy',
        'project_visibility': 'current_panel_visible_projects',
        'runtime_checks': {
            'status': 'not_verified',
            'checks': ['device_availability', 'agent_capabilities', 'node_policy', 'path_or_command',
                       'app_opt_in', 'lease', 'approval', 'operation_specific_policy'],
            'message': '只读预览未联系节点或试跑操作；实际调用仍受节点、本机、具体工具和审批限制。',
        },
    }
