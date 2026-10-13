"""Credential-bound room ceilings; no request-supplied opt-in or authority.

These policies only intersect existing grants, Profiles/Roles and live project
rights. A room selector is never a sandbox. Strict OS execution fails closed
until a separately approved isolation driver can prove the requested boundary.
"""
from __future__ import annotations

import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import time
import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from hub.access import AccessModel
from hub.collaboration.common import canonical, digest, validate
from shared.util import DevError

PROTOCOL = 1
TABLE = 'room_scope_manifests'
SNAPSHOT_FIELDS = ('id', 'space_id', 'device_id', 'root', 'mode', 'allow_tasks')
METADATA_TOOLS = frozenset({'get_profile', 'get_access_context', 'projects_list', 'projects_resolve',
    'workspace_list', 'workspaces_list', 'operations_list', 'artifacts_list'})
FORBIDDEN_DOMAINS = ('computer', 'browser', 'gateway', 'device_', 'devices_', 'project_create',
                     'projects_create', 'integration_', 'integrations_', 'native_', 'skills_install')


class ProjectRule(AccessModel):
    project_id: str = Field(min_length=1, max_length=100)
    capabilities: list[Literal['read', 'write', 'execute']] = Field(min_length=1, max_length=3)
    execution_targets: list[str] = Field(default_factory=lambda: ['agent'], min_length=1, max_length=16)

    @model_validator(mode='after')
    def exact(self):
        if 'read' not in self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('每个项目必须包含只读权限，不能使用跨项目权限并集')
        if len(set(self.execution_targets)) != len(self.execution_targets) or any(
                target != 'agent' and not (target.startswith('vps:') and len(target) == 36
                    and all(c in '0123456789abcdef' for c in target[4:])) for target in self.execution_targets):
            raise ValueError('执行目标必须使用精确 Agent/VPS 标识')
        return self


class ScopeSet(AccessModel):
    project: str = Field(min_length=1, max_length=100)
    environment_id: str = Field(default='production', min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    grant_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    project_rules: list[ProjectRule] = Field(default_factory=list, max_length=32)
    execution_mode: Literal['file_tools_only', 'account_permissions_acknowledged', 'require_os_isolation'] = 'file_tools_only'
    expires_seconds: int = Field(default=86400, ge=0, le=30 * 86400, strict=True)
    confirm_connection_wide_ceiling: Literal[True]
    acknowledge_unsandboxed_exec: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode='after')
    def exact_lifetime(self):
        if 0 < self.expires_seconds < 300:
            raise ValueError('凭据限制期限须为 0（持续至撤销），或 300 至 2592000 秒')
        return self


class ScopeControl(AccessModel):
    project: str = Field(min_length=1, max_length=100)
    environment_id: str = Field(default='production', min_length=1, max_length=100)
    grant_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    action: Literal['revoke']
    idempotency_key: str = Field(min_length=8, max_length=128)


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_scope_manifests(
        grant_id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,
        room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,version INTEGER NOT NULL,
        state TEXT NOT NULL,manifest TEXT NOT NULL,manifest_digest TEXT NOT NULL,
        expires_at REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS room_scope_room ON room_scope_manifests(room_id,grant_id)')


def installed(store):
    if getattr(store, '_room_scope_schema_ready', False):
        return True
    present = bool(store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)))
    if present:
        store._room_scope_schema_ready = True
    return present


def snapshot(project):
    return {key: project[key] for key in SNAPSHOT_FIELDS}


def root_overlap_ids(store, project):
    """Inspect mapping metadata only; never open another project's files."""
    root = PureWindowsPath(project['root']) if PureWindowsPath(project['root']).drive else PurePosixPath(project['root'])
    return {other['id'] for other in store.all(
        'SELECT id,root FROM projects WHERE device_id=?', (project['device_id'],))
        if other['id'] != project['id'] and type(root)(other['root']).is_relative_to(root)}


def fingerprint(row, manifest):
    return digest([row['grant_id'], row['space_id'], row['owner_user_id'], row['room_id'],
                   row['conversation_id'], row['version'], row['expires_at'], manifest])


def current(store, grant_id, *, grant=None):
    if not grant_id or not installed(store):
        return None
    row = store.one('SELECT * FROM room_scope_manifests WHERE grant_id=?', (grant_id,))
    if row is None:
        return None  # Legacy grants are deliberately unchanged.
    if row['state'] != 'active':
        raise DevError('ROOM_SCOPE_REVOKED', '此房间连接上限已撤销；不会退回更宽的原授权', 403)
    if not math.isfinite(row['expires_at']) or (row['expires_at'] != 0 and row['expires_at'] <= time.time()):
        raise DevError('ROOM_SCOPE_EXPIRED', '此房间连接已过期；请由房主明确重新确认', 403)
    grant = grant or store.one('SELECT * FROM grants WHERE id=?', (grant_id,))
    if not grant or grant['user_id'] != row['owner_user_id'] or grant.get('space_id', 'legacy') != row['space_id'] or grant['revoked']:
        raise DevError('ROOM_SCOPE_CHANGED', '房间连接原身份已变化', 403)
    try:
        manifest = json.loads(row['manifest'])
        if (manifest['schema_version'] != PROTOCOL or fingerprint(row, manifest) != row['manifest_digest']
                or not isinstance(manifest['project_rules'], list) or not manifest['project_rules']):
            raise ValueError()
        # Zero is long-term only with an explicit, digest-bound owner selection.
        # Legacy missing modes remain bounded; no migration extends old approvals.
        lifetime = manifest.get('lifetime_mode', 'bounded')
        if lifetime not in {'bounded', 'until_revoked'} or (lifetime == 'until_revoked') != (row['expires_at'] == 0):
            raise ValueError()
        membership_mode = manifest.get('project_membership_mode', 'conversation')
        if membership_mode not in {'conversation', 'grant_ceiling'}:
            raise ValueError()
        for rule in manifest['project_rules']:
            ProjectRule.model_validate({k: rule[k] for k in ('project_id', 'capabilities', 'execution_targets')})
            project = store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (rule['project_id'], row['space_id']))
            if project is None or snapshot(project) != rule['project_snapshot']:
                raise DevError('ROOM_SCOPE_MAPPING_CHANGED', '房间批准的项目、目录、节点或执行模式已改变，请重新确认', 403)
            root = PureWindowsPath(project['root']) if PureWindowsPath(project['root']).drive else PurePosixPath(project['root'])
            allowed_ids = {r['project_id'] for r in manifest['project_rules']}
            for other in store.all('SELECT id,root FROM projects WHERE device_id=?', (project['device_id'],)):
                if other['id'] not in allowed_ids and type(root)(other['root']).is_relative_to(root):
                    raise DevError('ROOM_SCOPE_ROOT_OVERLAP', '批准目录内新增了范围外项目映射，请重新核对房间范围', 403)
            for target in rule['execution_targets']:
                if target == 'agent':
                    continue
                actual = store.one('''SELECT v.id,v.connection_revision,p.binding_id FROM vps_connections v
                    JOIN vps_projects p ON p.vps_id=v.id WHERE v.id=? AND p.project_id=? AND v.space_id=? AND v.enabled=1''',
                    (target[4:], project['id'], row['space_id']))
                if actual is None or actual != rule.get('target_snapshots', {}).get(target):
                    raise DevError('ROOM_SCOPE_TARGET_CHANGED', '原批准的 VPS 连接、凭据或项目分配已改变，不能继承到新目标', 403)
            # Old approvals retain their original membership dependency. New
            # explicit owner approvals cap the credential independently of work
            # membership, so a not-yet-joined project can be selected afterward.
            if membership_mode == 'conversation' and not store.one('''SELECT 1 FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                (row['conversation_id'], rule['project_id'], manifest['environment_id'])):
                raise DevError('ROOM_SCOPE_CHANGED', '原范围项目已不再属于批准的话题或环境', 403)
        room = store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['room_id'],))
        conversation = store.one('SELECT * FROM conversation_rooms WHERE id=?', (row['conversation_id'],))
        if (not room or room['state'] != 'active' or room['owner_user_id'] != row['owner_user_id']
                or room['environment_id'] != manifest['environment_id']
                or room['space_id'] != row['space_id'] or not conversation
                or conversation['owner_user_id'] != row['owner_user_id'] or conversation['space_id'] != row['space_id']
                or not store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                                 (row['conversation_id'], row['room_id']))):
            raise DevError('ROOM_SCOPE_CHANGED', '原房间、话题或主理人状态已变化', 403)
    except (ValueError, KeyError, TypeError) as exc:
        raise DevError('ROOM_SCOPE_INVALID', '房间范围记录无效，未开放原始宽权限', 403) from exc
    return {**row, 'manifest': manifest}


def narrow_effective(store, grant, scopes, projects, profile):
    # Auth.bearer joins tokens + grants: its `id` is the TOKEN row, while
    # direct refreshes pass a grants row. The ceiling always binds the grant.
    grant_id = grant.get('grant_id') or grant['id']
    record = current(store, grant_id, grant=grant)
    if record is None:
        return scopes, projects, profile
    rules = record['manifest']['project_rules']
    allowed = {rule['project_id'] for rule in rules}
    projects = sorted(allowed if '*' in projects else allowed & set(projects))
    union = {cap for rule in rules if rule['project_id'] in projects for cap in rule['capabilities']}
    return set(scopes) & union, projects, profile


def require_action(store, principal, action, *, project_id=None, device_id=None):
    record = current(store, principal.grant_id)
    if record is None:
        return None
    if record['space_id'] != principal.space_id or record['owner_user_id'] != principal.user_id:
        raise DevError('ROOM_SCOPE_CHANGED', '房间范围不属于当前认证身份', 403)
    if device_id is not None:
        raise DevError('ROOM_SCOPE_DEVICE_DENIED', '房间连接不包含整台设备管理或新建映射能力', 403)
    if project_id is None:
        if action not in {'read', 'get_profile', 'get_access_context'}:
            raise DevError('ROOM_SCOPE_RESOURCE_REQUIRED', '房间连接操作必须指向已批准的确切项目', 403)
        return record
    rule = next((r for r in record['manifest']['project_rules'] if r['project_id'] == project_id), None)
    if rule is None or action not in rule['capabilities']:
        raise DevError('ROOM_SCOPE_DENIED', '此连接未获当前房间对该项目和能力的授权', 403)
    if action == 'execute' and record['manifest']['execution_mode'] != 'account_permissions_acknowledged':
        raise DevError('ROOM_SCOPE_ISOLATION_REQUIRED', '此房间只允许文件工具；未证明 OS 隔离，不允许 shell/任务执行', 403)
    return record


def require_room(store, principal, room, conversation_id=None):
    record = current(store, principal.grant_id)
    if record is None:
        return
    if (room is None or room['environment_id'] != record['manifest']['environment_id']
            or room['project_id'] not in {r['project_id'] for r in record['manifest']['project_rules']}
            or not store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                             (record['conversation_id'], room['id']))
            or conversation_id and record['conversation_id'] != conversation_id):
        raise DevError('ROOM_SCOPE_CONVERSATION_DENIED', '此连接不能切换到另一个房间或话题', 403)


def require_tool(store, principal, name, args):
    """Extra fail-closed dispatch gate; normal typed handlers still reauthorize."""
    record = current(store, principal.grant_id)
    if record is None:
        return
    if name.startswith(FORBIDDEN_DOMAINS) or name in {'vps_list', 'vps'} and not args.get('project'):
        raise DevError('ROOM_SCOPE_DOMAIN_DENIED', '此房间连接未包含该设备、桌面、浏览器、网关或全局管理域', 403)
    if args.get('workspace_id') or name.startswith('worktree') or name in {'download_artifact', 'inspect_file_source'}:
        raise DevError('ROOM_SCOPE_WORKSPACE_DENIED', '此房间限制仅批准原项目根目录；工作树和文件导入需另行核验，不自动扩大路径或来源', 403)
    if name in {'ssh_exec', 'git_remote', 'git_push'}:
        raise DevError('ROOM_SCOPE_TARGET_REQUIRED', '房间连接只使用可核验的项目目标，不能直接填写远端地址或凭据', 403)
    if name in METADATA_TOOLS:
        return  # Their established queries use the already-intersected projects.
    project = None
    if args.get('project'):
        value = args['project']
        if not isinstance(value, str):
            raise DevError('ROOM_SCOPE_PROJECT_REQUIRED', '必须指定确切项目', 403)
        project = store.one('SELECT * FROM projects WHERE space_id=? AND (id=? OR alias_key=?)',
            (principal.space_id, value, unicodedata.normalize('NFKC', value).casefold()))
    elif args.get('operation_id'):
        operation = store.one('SELECT project_id FROM operations WHERE id=?', (args['operation_id'],))
        project = store.one('SELECT * FROM projects WHERE id=?', (operation['project_id'],)) if operation else None
    elif args.get('artifact_id'):
        artifact = store.one('SELECT project_id FROM artifacts WHERE id=?', (args['artifact_id'],))
        project = store.one('SELECT * FROM projects WHERE id=?', (artifact['project_id'],)) if artifact else None
    if project is None:
        # Local collaboration handlers bind a room/goal/slot and recheck below.
        if name.startswith(('collaboration_', 'monitor_')) or name in {'collaboration', 'collaboration_work', 'collaboration_query'}:
            return
        raise DevError('ROOM_SCOPE_PROJECT_REQUIRED', '房间连接不能省略或绕过项目身份', 403)
    from shared.contracts import TOOLS, required_scope
    capability = required_scope(name, args) if name in TOOLS else 'read'
    require_action(store, principal, capability, project_id=project['id'])
    if name in {'exec', 'shell_exec', 'ssh_exec', 'tasks_run'}:
        require_action(store, principal, 'execute', project_id=project['id'])
        rule = next(r for r in record['manifest']['project_rules'] if r['project_id'] == project['id'])
        if args.get('target', 'agent') not in rule['execution_targets']:
            raise DevError('ROOM_SCOPE_TARGET_DENIED', '执行目标不属于原房间批准清单', 403)


def descriptor(store, principal):
    record = current(store, principal.grant_id)
    if record is None:
        return {'protocol': PROTOCOL, 'credential_bound': False, 'ordinary_tools_room_scoped': False,
                'os_sandbox_verified': False, 'host_other_connectors_controlled': False}
    return {'protocol': PROTOCOL, 'credential_bound': True, 'ordinary_tools_room_scoped': True,
            'grant_id': record['grant_id'], 'room_id': record['room_id'], 'conversation_id': record['conversation_id'],
            'version': record['version'], 'expires_at': record['expires_at'], 'manifest_digest': record['manifest_digest'],
            'lifetime_mode': record['manifest'].get('lifetime_mode', 'bounded'),
            'project_rules': [{k: r[k] for k in ('project_id', 'capabilities', 'execution_targets')}
                              for r in record['manifest']['project_rules']],
            'execution_mode': record['manifest']['execution_mode'], 'os_sandbox_verified': False,
            'host_other_connectors_controlled': False, 'workspace_policy': 'project_root_only',
            'enforcement': 'server_credential_resource_intersection',
            'warning': '约束当前 CodePier 凭据的资源调用；不控制宿主的其他连接器，工作目录不是 OS 沙箱。'}


class RoomScopeService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def set(self, raw, principal):
        args = validate(ScopeSet, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.c.live_room(room)
            conversation = self.c.conversations.resolve(principal, room, args)
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (args['grant_id'],))
            if (not grant or grant['revoked'] or grant['user_id'] != principal.user_id
                    or grant.get('space_id', 'legacy') != principal.space_id):
                raise DevError('ROOM_SCOPE_GRANT_INVALID', '请选择当前房主已有的有效连接，不创建或冒用其他身份', 403)
            # Harmless duplicate selections share one receipt and one rule. Keep
            # first-occurrence order so existing valid request fingerprints survive.
            rules, seen = [], {}
            for rule in args['project_rules']:
                identity = {**rule, 'capabilities': sorted(rule['capabilities']),
                            'execution_targets': sorted(rule['execution_targets'])}
                if rule['project_id'] in seen:
                    if seen[rule['project_id']] != identity:
                        raise DevError('ROOM_SCOPE_PROJECT_RULE_CONFLICT',
                            '同一项目选择了不同权限或执行位置。请核对后只保留一项；本次未更改连接权限。', 422,
                            project_id=rule['project_id'], remedy='review_connection_rules',
                            changes_applied=False)
                    continue
                seen[rule['project_id']] = identity
                rules.append(rule)
            args = {**args, 'project_rules': rules}
            if not rules:
                raise DevError('ROOM_SCOPE_EMPTY',
                    '要让房间只聊天，请打开“管理房间项目”并取消全部项目。这里调整的是整条连接权限，本次未更改。', 422,
                    remedy='edit_room_projects', conversation_id=conversation['id'], changes_applied=False)
            if room['project_id'] not in seen:
                raise DevError('ROOM_SCOPE_ANCHOR_REQUIRED',
                    '要保留原连接并只处理所选项目，请打开“管理房间项目”保存选择。这里调整的是整条连接权限，本次未更改。', 422,
                    remedy='edit_room_projects', conversation_id=conversation['id'], changes_applied=False)
            wants_exec = any('execute' in r['capabilities'] for r in rules)
            if args['execution_mode'] == 'require_os_isolation':
                raise DevError('ROOM_SCOPE_ISOLATION_UNAVAILABLE', '尚无经批准并验证的 OS 隔离驱动；严格执行范围不能降级为 cwd 限制', 409)
            if wants_exec and (args['execution_mode'] != 'account_permissions_acknowledged' or not args['acknowledge_unsandboxed_exec']):
                raise DevError('ROOM_SCOPE_EXECUTION_CONSENT', '执行使用 Agent 账号权限；需要房主单独明确确认，文件模式不包含执行', 422)
            from hub import iam
            iam.validate_grant(self.store, grant)
            from hub.access_profiles import stored_permissions
            from hub.roles import effective_role, project_actions, role_binding, created_ids
            # Validate against the underlying consent, not a wider model-proposed
            # manifest nor a stale old room ceiling. Updating is an explicit owner action.
            if grant.get('authorization_mode') == 'role':
                _, base_projects, _ = effective_role(self.store, grant)
                role, policy, _ = role_binding(self.store, grant)
                base_caps = lambda pid: project_actions(policy, pid, created_ids(self.store, role['id']))
            else:
                scopes, base_projects = stored_permissions(grant)
                if grant.get('profile_id'):
                    profile = self.store.one('SELECT * FROM access_profiles WHERE id=?', (grant['profile_id'],))
                    if not profile or not profile['enabled'] or profile['user_id'] != principal.user_id or profile.get('space_id', 'legacy') != principal.space_id:
                        raise DevError('ROOM_SCOPE_PROFILE_INVALID', '当前访问 Profile 不可用', 403)
                    p_scopes, p_projects = stored_permissions(profile)
                    scopes &= p_scopes
                    from hub.access_profiles import intersect_projects
                    base_projects = intersect_projects(base_projects, p_projects)
                base_caps = lambda pid: scopes
            snapshots = []
            for rule in rules:
                project = self.c.runtime.project(rule['project_id'], principal)
                # Credential approval and task membership are independent.
                # This saves no conversation partition or working-scope revision.
                if ('*' not in base_projects and project['id'] not in base_projects
                        or not set(rule['capabilities']) <= set(base_caps(project['id']))):
                    raise DevError('ROOM_SCOPE_CEILING_EXCEEDED', '房间限制只能取原连接授权交集，不能扩大项目或能力', 403)
                for capability in rule['capabilities']:
                    self.c.coordination.require_capability(principal, capability, project['id'])
                # A broad parent mapping can expose an excluded child through a
                # different alias. Reject overlaps rather than implying hard isolation.
                if root_overlap_ids(self.store, project) - {r['project_id'] for r in rules}:
                    label = project.get('alias') or project['id']
                    raise DevError('ROOM_SCOPE_ROOT_OVERLAP',
                        f'项目“{label}”的批准目录包含范围外的项目映射。请在项目管理核对该项目根目录，'
                        '使用独立目录的项目建立话题后再设置凭据范围；本次未更改连接权限。', 409,
                        project_id=project['id'], project_label=label, remedy='use_independent_project_root')
                targets = {target: self.c.runtime.vps.reference({'vps': target[4:]}, project)
                           for target in rule['execution_targets'] if target != 'agent'}
                snapshots.append({**rule, 'project_snapshot': snapshot(project),
                                  **({'target_snapshots': targets} if targets else {})})

            def save():
                actual = self.store.one('SELECT * FROM room_scope_manifests WHERE grant_id=?', (grant['id'],))
                if (actual['version'] if actual else 0) != args['expected_version']:
                    raise DevError('STALE_VERSION', '连接范围已变化，请重新读取后确认', 409)
                now = self.c.clock()
                lifetime = 'until_revoked' if args['expires_seconds'] == 0 else 'bounded'
                manifest = {'schema_version': PROTOCOL, 'project_rules': snapshots, 'execution_mode': args['execution_mode'],
                            'environment_id': room['environment_id'], 'lifetime_mode': lifetime,
                            'project_membership_mode': 'grant_ceiling'}
                record = {'grant_id': grant['id'], 'space_id': principal.space_id, 'owner_user_id': principal.user_id,
                    'room_id': room['id'], 'conversation_id': conversation['id'], 'version': args['expected_version'] + 1,
                    'expires_at': 0.0 if lifetime == 'until_revoked' else now + args['expires_seconds']}
                self.store.execute('''INSERT INTO room_scope_manifests
                    (grant_id,space_id,owner_user_id,room_id,conversation_id,version,state,manifest,manifest_digest,expires_at,created,updated)
                    VALUES (?,?,?,?,?,?,'active',?,?,?,?,?) ON CONFLICT(grant_id) DO UPDATE SET
                    room_id=excluded.room_id,conversation_id=excluded.conversation_id,version=excluded.version,state='active',
                    manifest=excluded.manifest,manifest_digest=excluded.manifest_digest,expires_at=excluded.expires_at,updated=excluded.updated''',
                    (*[record[k] for k in ('grant_id', 'space_id', 'owner_user_id', 'room_id', 'conversation_id', 'version')],
                     canonical(manifest), fingerprint(record, manifest), record['expires_at'], actual['created'] if actual else now, now))
                self.c.audit(room, principal.actor, 'room_scope.owner_approved', grant['id'],
                    {'version': record['version'], 'project_ids': [r['project_id'] for r in snapshots],
                     'execution_mode': args['execution_mode'], 'changes_underlying_grant': False,
                     'project_membership_mode': manifest['project_membership_mode'],
                     'lifetime_mode': lifetime, 'expires_seconds': args['expires_seconds'], 'expires_at': record['expires_at']})
                return {'grant_id': grant['id'], 'version': record['version'], 'manifest_digest': fingerprint(record, manifest),
                        'credential_bound': True, 'affects_all_chats_using_this_grant': True, 'os_sandbox_verified': False,
                        'lifetime_mode': lifetime, 'expires_at': record['expires_at']}
            return self.c.mutation(principal, room, 'room_scope_set', args, save)

    def control(self, raw, principal):
        args = validate(ScopeControl, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            row = self.store.one('SELECT * FROM room_scope_manifests WHERE grant_id=? AND room_id=?', (args['grant_id'], room['id']))
            if not row:
                raise DevError('ROOM_SCOPE_NOT_FOUND', '当前房间没有这个连接限制', 404)

            def save():
                cursor = self.store.execute("UPDATE room_scope_manifests SET state='revoked',version=version+1,updated=? WHERE grant_id=? AND version=?",
                    (self.c.clock(), row['grant_id'], args['expected_version']))
                if cursor.rowcount != 1:
                    raise DevError('STALE_VERSION', '连接范围已变化，请重新读取', 409)
                self.c.audit(room, principal.actor, 'room_scope.revoked', row['grant_id'], {'returns_to_broad_grant': False})
                return {'grant_id': row['grant_id'], 'state': 'revoked', 'version': args['expected_version'] + 1}
            return self.c.mutation(principal, room, 'room_scope_control', args, save)

    def listing(self, raw, principal):
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, raw)
            self.c.owner(principal)
            conversation = self.c.conversations.resolve(principal, room, raw)
            projects = [self.c.runtime.project(item['project_id'], principal)
                for item in self.c.conversations.visible(principal, conversation)
                if item['environment_id'] == room['environment_id']]
            visible_ids = {project['id'] for project in projects}
            root_checks = []
            for project in projects:
                overlaps = root_overlap_ids(self.store, project)
                root_checks.append({'project_id': project['id'],
                    'overlap_with_project_ids': sorted(overlaps & visible_ids),
                    'has_outside_projects': bool(overlaps - visible_ids)})
            rows = self.store.all('SELECT * FROM room_scope_manifests WHERE room_id=? ORDER BY grant_id', (room['id'],))
            grants = self.store.all('''SELECT g.id,MIN(s.label) AS label,m.version AS scope_version,
                m.conversation_id AS scoped_conversation_id,m.state AS scope_state
                FROM grants g JOIN collaboration_join_slots s ON s.grant_id=g.id
                LEFT JOIN room_scope_manifests m ON m.grant_id=g.id
                WHERE s.room_id=? AND g.user_id=? AND g.space_id=? AND g.revoked=0 GROUP BY g.id ORDER BY label,g.id''',
                (room['id'], principal.user_id, principal.space_id))
            return {'items': [{k: r[k] for k in ('grant_id', 'room_id', 'conversation_id', 'version', 'state', 'manifest_digest', 'expires_at')}
                              | {'manifest': json.loads(r['manifest'])} for r in rows],
                    'eligible_connections': grants, 'project_root_checks': root_checks,
                    'read_only': True, 'strict_os_isolation_available': False,
                    'warning': '启用后约束同一凭据在所有会话中的 CodePier 调用；不会改变原 grant 或创建新凭据。'}
