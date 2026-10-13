"""Versioned room work projects, independent of the stable message connection.

Historical conversation partitions and dot enrollment are never rewritten.
Only explicit panel-owner revisions authorize new managed task scopes; ordinary
connector grants, notification routes and external-channel bindings are unchanged.
"""
from __future__ import annotations

import json

from pydantic import Field, model_validator
from typing import Literal

from hub.collaboration.common import canonical, digest, validate
from hub.principal import Principal, refresh_principal
from hub.room_scope import root_overlap_ids
from shared import collaboration_contracts as contracts
from shared.util import DevError


class WorkingProject(contracts.Scope):
    capabilities: list[Literal['read', 'write', 'execute']] | None = Field(default=None, max_length=3)
    execution_targets: list[str] | None = Field(default=None, max_length=16)

    @model_validator(mode='after')
    def exact_permissions(self):
        if self.capabilities is not None:
            if len(set(self.capabilities)) != len(self.capabilities) or (
                    self.capabilities and 'read' not in self.capabilities):
                raise ValueError('项目权限须包含读取，且不能重复；空权限仅保留房间项目')
        if self.execution_targets is not None and (
                len(set(self.execution_targets)) != len(self.execution_targets) or any(
                    target != 'project_agent' and not (target.startswith('vps:') and len(target) == 36
                        and all(c in '0123456789abcdef' for c in target[4:]))
                    for target in self.execution_targets)):
            raise ValueError('执行位置须使用原连接的精确 Agent/VPS 标识')
        return self


class Selection(contracts.Model):
    conversation_id: contracts.Identifier
    projects: list[WorkingProject] = Field(default_factory=list, max_length=16)
    permissions_only: bool = False
    dot_id: str = Field(default='', max_length=128)


class ApplySelection(Selection):
    expected_version: int = Field(ge=1)
    expected_scope_version: int = Field(ge=0)
    selection_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm_scope: Literal[True]
    # Legacy policies already confirm the account-level execution boundary. This
    # optional UI notice receipt is not another authorization gate: confirm_scope
    # and the exact selection_digest approve all project capabilities together.
    confirm_unsandboxed_exec: bool = Field(default=False, description=(
        'Optional execution notice receipt; authorization requires confirm_scope and selection_digest'))
    idempotency_key: contracts.Key


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_working_scopes (
        conversation_id TEXT NOT NULL,version INTEGER NOT NULL,spec TEXT NOT NULL,
        digest TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(conversation_id,version))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_working_scope_immutable
        BEFORE UPDATE ON conversation_working_scopes
        BEGIN SELECT RAISE(ABORT,'room work approval is immutable'); END""")
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_project_revocations (
        conversation_id TEXT NOT NULL,project_id TEXT NOT NULL,version INTEGER NOT NULL,
        created REAL NOT NULL,PRIMARY KEY(conversation_id,project_id,version))""")


class WorkingScopes:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def current(self, conversation_id):
        row = self.store.one("""SELECT * FROM conversation_working_scopes
            WHERE conversation_id=? ORDER BY version DESC LIMIT 1""", (conversation_id,))
        if row:
            spec = json.loads(row['spec'])
            if digest(spec) != row['digest'] or spec['version'] != row['version']:
                raise DevError('ROOM_PROJECT_SCOPE_INVALID', '房间项目范围证据无效', 409)
            return {**row, 'spec': spec}
        return None

    @staticmethod
    def reference(row):
        return {'version': row['version'], 'digest': row['digest']}

    def candidates(self, conversation_id):
        return self.store.all("""SELECT d.id,s.label FROM collaboration_dots d
            JOIN collaboration_join_slots s ON s.id=d.id
            WHERE d.conversation_id=? AND s.state='registered' ORDER BY s.created,s.id""",
            (conversation_id,))

    @staticmethod
    def normalized_selection(args):
        # UI repeated selections are one intent, never a capability union. Keep
        # the first occurrence to preserve valid pre-existing retry fingerprints.
        seen, projects = {}, []
        for project in args['projects']:
            identity = (project['project'], project['environment_id'])
            value = {k: v for k, v in project.items() if v is not None}
            for field in ('capabilities', 'execution_targets'):
                if field in value:
                    value[field] = sorted(value[field])
            if identity in seen and seen[identity] != value:
                raise DevError('ROOM_PROJECT_PERMISSION_CONFLICT',
                    '同一项目选择了不同权限或执行位置，请核对后只保留一项；本次未更改。', 422)
            if identity not in seen:
                seen[identity] = value
                projects.append(value)
        return {**{k: v for k, v in args.items() if not (k in {'permissions_only', 'confirm_unsandboxed_exec'} and not v)}, 'projects': projects}

    def candidate(self, args, principal, conversation):
        previous = self.current(conversation['id'])
        choices = self.candidates(conversation['id'])
        chosen = args['projects']
        ids = [item['project'] for item in chosen]
        if len(set(ids)) != len(ids):
            raise DevError('DUPLICATE_PROJECT', '同一项目选择了不同环境。请刷新项目列表，选择一个环境后再保存；本次未更改。', 422)
        old_rules = {r['project_id']: r for r in previous['spec']['projects']} if previous else {}
        if args.get('permissions_only') and set(ids) != (set(old_rules) if previous else {
                r['project_id'] for r in self.c.conversations.visible(principal, conversation)}):
            raise DevError('ROOM_PROJECT_MEMBERSHIP_CHANGED',
                '房间项目已改变，请刷新权限列表；权限入口不能增删房间项目。', 409)
        narrowing = not ids or previous is not None and all(
            item['project'] in old_rules
            and set(item.get('capabilities', old_rules[item['project']]['capabilities']))
                <= set(old_rules[item['project']]['capabilities'])
            and set(item.get('execution_targets', old_rules[item['project']]['execution_targets']))
                <= set(old_rules[item['project']]['execution_targets'])
            for item in chosen)
        dot_id = args['dot_id'] or (previous['spec']['dot_id'] if previous else
                                  choices[0]['id'] if len(choices) == 1 else '')
        if not dot_id:
            raise DevError('ROOM_PROJECT_DOT_REQUIRED', '请选择此房间已有的消息连接', 409)
        dot = self.c.dots.find(dot_id)
        if not dot or dot['conversation_id'] != conversation['id'] or (not narrowing and dot_id not in {c['id'] for c in choices}):
            raise DevError('ROOM_PROJECT_DOT_REQUIRED', '选择的连接不属于此房间', 409)
        # An established room never silently changes which connection executes its work.
        if previous and previous['spec']['dot_id'] != dot_id:
            raise DevError('ROOM_PROJECT_CONNECTION_CHANGED', '请保留此房间原消息连接；切换连接需另行确认', 409)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
        slot = self.c.joining.slot(room, dot_id)
        policy = (self.store.one('SELECT * FROM delegation_policies WHERE id=?', (dot['policy_id'],))
                  if narrowing else self.c.dots.policy(room, dot))
        if not policy:
            raise DevError('ROOM_PROJECT_CONNECTION_CHANGED', '原消息连接没有可核对的任务基线', 409)
        actor = None
        if not narrowing:
            self.c.live_room(room)
            self.c.joining.active(room, slot, registered=True)
            self.c.delegation.authorize_policy(principal, room, policy)
            actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(policy['grant_snapshot']))
        base = json.loads(policy['spec'])
        inherited = self.legacy_defaults(principal, conversation, dot, policy) if not previous else {}
        version = (previous['version'] if previous else 0) + 1
        options = self.permission_options(args, principal, conversation)
        by_project = {item['project_id']: item for item in options}
        rules = []
        for item in chosen:
            if item['environment_id'] != room['environment_id']:
                raise DevError('ROOM_PROJECT_ENVIRONMENT_MISMATCH', '房间项目须使用原连接的同一环境', 409)
            if narrowing:
                retained = self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?',
                                          (item['project'], principal.space_id))
                if retained and root_overlap_ids(self.store, retained) - set(ids):
                    raise DevError('ROOM_PROJECT_ROOT_OVERLAP',
                        '保留的上层目录仍包含未选择项目；请同时移除上层目录，不能把项目移除显示为目录隔离。', 409)
                rule = dict(old_rules[item['project']])
                for field in ('capabilities', 'execution_targets'):
                    if field in item:
                        rule[field] = sorted(item[field])
                if 'execute' in rule['capabilities'] and not rule['execution_targets']:
                    raise DevError('ROOM_PROJECT_TARGET_REQUIRED', '执行权限必须选择原批准的执行位置', 422)
                if any(rule[k] != old_rules[item['project']][k] for k in ('capabilities', 'execution_targets')):
                    rule['authorization_id'] = digest([conversation['id'], dot_id, version,
                        {k: v for k, v in rule.items() if k != 'authorization_id'}])
                rule['execution_target'] = rule['execution_targets'][0] if len(rule['execution_targets']) == 1 else ''
                rule['target_snapshots'] = ({k: v for k, v in rule['target_snapshots'].items()
                    if k in rule['execution_targets']} if 'execute' in rule['capabilities'] else {})
                rules.append(rule)
                continue
            project = self.c.runtime.project(item['project'], principal)
            if project['id'] != item['project']:
                raise DevError('PROJECT_ID_REQUIRED', '请使用明确项目编号', 422)
            old = old_rules.get(project['id']) or inherited.get(project['id'])
            available = by_project.get(project['id'], {})
            capabilities = item.get('capabilities', old['capabilities'] if old else
                                    available.get('allowed_capabilities') or ['read'])
            execution_targets = item.get('execution_targets', old['execution_targets'] if old else
                                         available.get('allowed_execution_targets') or base['execution_targets'])
            if not set(capabilities) <= set(base['capabilities']):
                raise DevError('ROOM_PROJECT_POLICY_LIMITED',
                    '原房间任务授权未包含所选能力，请由房主先核对原房间任务设置；本次未保存。', 403,
                    allowed_capabilities=sorted(base['capabilities']), changes_applied=False)
            if not set(execution_targets) <= set(base['execution_targets']):
                raise DevError('ROOM_PROJECT_TARGET_DENIED', '执行位置超过原房间任务授权；本次未保存。', 403)
            if 'execute' in capabilities and not execution_targets:
                raise DevError('ROOM_PROJECT_TARGET_REQUIRED', '执行权限必须选择原批准的执行位置', 422)
            for subject in (principal, actor):
                self.c.runtime.project(project['id'], subject)
                for capability in capabilities:
                    self.c.coordination.require_capability(subject, capability, project['id'])
            if 'write' in capabilities and project['mode'] != 'write':
                raise DevError('READ_ONLY', '所选项目没有写入权限', 403)
            if 'execute' in capabilities and not project['allow_tasks']:
                raise DevError('TASKS_DISABLED', '所选项目没有执行权限', 403)
            if root_overlap_ids(self.store, project) - set(ids):
                raise DevError('ROOM_PROJECT_ROOT_OVERLAP',
                    '项目“' + project.get('alias', project['id']) + '”包含未选择的项目目录。请取消此上层目录，仅选择需要的独立项目；不会扩大底层凭据。', 409)
            targets = ({target: self.c.delegation.target_snapshot(subject, project, target)
                        for target in execution_targets for subject in (principal, actor)}
                       if 'execute' in capabilities else {})
            rule = {'project_id': project['id'], 'project_label': project.get('alias', project['id']),
                    'environment_id': room['environment_id'], 'capabilities': sorted(capabilities),
                    'execution_targets': sorted(execution_targets),
                    'execution_target': execution_targets[0] if len(execution_targets) == 1 else '',
                    'project_snapshot': self.c.coordination.project_snapshot(project),
                    'target_snapshots': targets,
                    'expires_at': old['expires_at'] if old and 'expires_at' in old else (
                        0 if self.c.channel_owner.persistent(policy) else policy['expires_at'])}
            old = old_rules.get(project['id'])
            unchanged = old and all(old.get(k) == v for k, v in rule.items())
            rule['authorization_id'] = old['authorization_id'] if unchanged else digest(
                [conversation['id'], dot_id, version, rule])
            rules.append(rule)
        rules.sort(key=lambda r: r['project_id'])
        if not narrowing:
            from hub.room_scope import require_tool
            for rule in rules:
                if 'execute' not in rule['capabilities']:
                    continue
                for target in rule['execution_targets']:
                    require_tool(self.store, actor, 'exec', {'project': rule['project_id'],
                        'target': 'agent' if target == 'project_agent' else target})
        connection = {'dot_id': dot_id, 'dot_label': slot['label'], 'grant_id': slot['grant_id'],
                      'room_id': room['id'], 'project_id': room['project_id'],
                      'environment_id': room['environment_id']}
        old_ids = set(old_rules) if previous else {
            r['project_id'] for r in self.c.conversations.visible(principal, conversation)}
        spec = {'version': version, 'conversation_id': conversation['id'],
                'conversation_version': conversation['version'], 'dot_id': dot_id,
                'dot_setup_digest': dot['setup_digest'], 'connection': connection,
                'policy_id': previous['spec']['policy_id'] if previous and narrowing else policy['id'],
                'policy_version': previous['spec']['policy_version'] if previous and narrowing else policy['version'],
                'policy_digest': previous['spec']['policy_digest'] if previous and narrowing else policy['digest'], 'projects': rules,
                'approved_by': principal.actor, 'owner_user_id': principal.user_id,
                'space_id': principal.space_id}
        return {'ready': True, 'conversation_version': conversation['version'],
                'scope_version': version - 1, 'selection_digest': digest(spec),
                'connection': connection, 'projects': rules, 'choices': choices,
                'permission_options': options, 'permission_source': 'room_working_scope',
                'execution_confirmation_field': 'confirm_scope',
                'execution_confirmation_digest_field': 'selection_digest',
                'requires_execution_confirmation': any('execute' in r['capabilities']
                    and 'execute' not in old_rules.get(r['project_id'], {}).get('capabilities', []) for r in rules),
                'added': sorted(set(ids) - old_ids), 'removed': sorted(old_ids - set(ids)),
                'unchanged': sorted(old_ids & set(ids)), 'spec': spec,
                'changes_underlying_grant': False,
                'message': '一次保存房间项目及其任务权限；原连接授权上限不变。执行使用账号权限，不是目录沙箱。移除项目或权限后旧任务不能开始新步骤。'}

    def legacy_defaults(self, principal, conversation, dot, policy):
        """Preserve legacy task approvals; membership alone is never authority."""
        base = json.loads(policy['spec'])
        defaults = {}
        for item in self.c.conversations.visible(principal, conversation):
            project_id = item['project_id']
            if project_id == base['project_snapshot']['project_id']:
                defaults[project_id] = {'capabilities': base['capabilities'],
                    'execution_targets': base['execution_targets'],
                    'expires_at': 0 if self.c.channel_owner.persistent(policy) else policy['expires_at']}
                continue
            approvals = []
            for row in self.store.all("""SELECT * FROM conversation_project_scopes
                    WHERE conversation_id=? AND dot_id=? AND project_id=? AND environment_id=? AND state='active'""",
                    (conversation['id'], dot['id'], project_id, item['environment_id'])):
                try:
                    approvals.append(self.c.conversations.project_scopes.authorize(
                        principal, policy, self.c.conversations.project_scopes.reference(row)))
                except DevError:
                    continue
            defaults[project_id] = {
                'capabilities': sorted(set.intersection(*(set(a['capabilities']) for a in approvals))) if approvals else [],
                'execution_targets': sorted({a['execution_target'] for a in approvals}) if approvals else [],
                **({'expires_at': min(a['expires_at'] for a in approvals)} if approvals else {})}
        return defaults

    def permission_options(self, args, principal, conversation):
        """Read-only effective choices; never substitutes a less restricted actor."""
        selected = args['projects']
        if not selected:
            return []
        previous, choices = self.current(conversation['id']), self.candidates(conversation['id'])
        dot_id = args['dot_id'] or (previous['spec']['dot_id'] if previous else
                                  choices[0]['id'] if len(choices) == 1 else '')
        try:
            dot = self.c.dots.find(dot_id)
            if not dot or dot['conversation_id'] != conversation['id']:
                raise DevError('ROOM_PROJECT_DOT_REQUIRED', '请选择当前房间已有的消息连接', 409)
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
            policy = self.c.dots.policy(room, dot)
            self.c.delegation.authorize_policy(principal, room, policy)
            actor = self.c.grant_reader(room, policy['grant_id'], snapshot=json.loads(policy['grant_snapshot']))
            base = json.loads(policy['spec'])
        except DevError as exc:
            return [{'project_id': item['project'], 'allowed_capabilities': [], 'allowed_execution_targets': [],
                     'capability_denials': [{'capability': capability, 'reason_code': exc.code,
                                            'message': exc.message} for capability in ('read', 'write', 'execute')]}
                    for item in selected]
        options = []
        from hub.room_scope import require_tool
        for item in selected:
            allowed, denied, targets = [], [], []
            try:
                if item['environment_id'] != room['environment_id']:
                    raise DevError('ROOM_PROJECT_ENVIRONMENT_MISMATCH', '项目须使用原连接的同一环境', 409)
                project = self.c.runtime.project(item['project'], principal)
                if project['id'] != item['project']:
                    raise DevError('PROJECT_ID_REQUIRED', '请使用明确项目编号', 422)
                self.c.runtime.project(project['id'], actor)
                for target in base['execution_targets']:
                    try:
                        for subject in (principal, actor):
                            self.c.delegation.target_snapshot(subject, project, target)
                        require_tool(self.store, actor, 'exec', {'project': project['id'],
                            'target': 'agent' if target == 'project_agent' else target})
                        targets.append(target)
                    except DevError:
                        pass
                for capability in ('read', 'write', 'execute'):
                    try:
                        if capability not in base['capabilities']:
                            raise DevError('ROOM_PROJECT_POLICY_LIMITED', '原房间任务授权未包含此能力', 403)
                        for subject in (principal, actor):
                            self.c.coordination.require_capability(subject, capability, project['id'])
                        if capability == 'write' and project['mode'] != 'write':
                            raise DevError('READ_ONLY', '项目当前只允许读取', 403)
                        if capability == 'execute':
                            if not project['allow_tasks']:
                                raise DevError('TASKS_DISABLED', '项目当前未允许执行', 403)
                            if not targets:
                                raise DevError('ROOM_PROJECT_TARGET_DENIED', '没有可核对的原批准执行位置', 403)
                            for target in targets:
                                require_tool(self.store, actor, 'exec', {'project': project['id'],
                                    'target': 'agent' if target == 'project_agent' else target})
                        allowed.append(capability)
                    except DevError as exc:
                        denied.append({'capability': capability, 'reason_code': exc.code, 'message': exc.message})
            except DevError as exc:
                denied = [{'capability': capability, 'reason_code': exc.code, 'message': exc.message}
                          for capability in ('read', 'write', 'execute')]
            options.append({'project_id': item['project'], 'allowed_capabilities': allowed,
                            'allowed_execution_targets': sorted(targets) if 'execute' in allowed else [],
                            'capability_denials': denied})
        return options

    def checked_candidate(self, args, principal, conversation):
        try:
            return self.candidate(args, principal, conversation)
        except DevError as exc:
            # Explain a proven ceiling denial only after the actual guards failed.
            # This diagnostic never creates a usable, less-restricted principal.
            replacement = self.connection_limit_error(args, principal, conversation, exc)
            raise replacement or exc

    def connection_limit_error(self, args, principal, conversation, original):
        if original.code not in {'PROJECT_NOT_FOUND', 'INSUFFICIENT_SCOPE',
                                 'ROOM_SCOPE_DENIED', 'ROOM_SCOPE_ISOLATION_REQUIRED',
                                 'ROOM_SCOPE_TARGET_DENIED'} or not args['projects']:
            return None
        self.c.owner(principal)
        previous, choices = self.current(conversation['id']), self.candidates(conversation['id'])
        dot_id = args['dot_id'] or (previous['spec']['dot_id'] if previous else
                                  choices[0]['id'] if len(choices) == 1 else '')
        dot = self.c.dots.find(dot_id)
        if not dot or dot['conversation_id'] != conversation['id']:
            return None
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
        policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (dot['policy_id'],))
        if not room or not policy:
            return None
        from hub import iam
        from hub.access_profiles import intersect_projects, stored_permissions
        from hub.room_scope import current
        try:
            actor = self.c.grant_reader(room, policy['grant_id'],
                                        snapshot=json.loads(policy['grant_snapshot']))
            ceiling = current(self.store, policy['grant_id'])
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (policy['grant_id'],))
            # Dynamic roles have per-project policy; do not mistake their denials
            # for a request to widen the connection-wide ceiling.
            if not ceiling or grant.get('authorization_mode', 'fixed') != 'fixed':
                return None
            scopes, project_ids = stored_permissions(grant)
            if grant.get('profile_id'):
                profile = self.store.one('SELECT * FROM access_profiles WHERE id=?', (grant['profile_id'],))
                caps, ids = stored_permissions(profile)
                scopes &= caps
                project_ids = intersect_projects(project_ids, ids)
            base = json.loads(policy['spec'])
            rules = {rule['project_id']: rule for rule in ceiling['manifest']['project_rules']}
            restricted = []
            for project_id in dict.fromkeys([room['project_id']] + [p['project'] for p in args['projects']]):
                project = self.c.runtime.project(project_id, principal)
                if (project['id'] != project_id or '*' not in project_ids and project_id not in project_ids
                        or not set(base['capabilities']) <= scopes):
                    return None
                for capability in base['capabilities']:
                    self.c.coordination.require_capability(principal, capability, project_id)
                    if iam.installed(self.store):
                        iam.require_project(self.store, actor, capability, project_id)
                for target in base['execution_targets']:
                    self.c.delegation.target_snapshot(principal, project, target)
                rule = rules.get(project_id, {})
                caps = sorted(set(base['capabilities']) - set(rule.get('capabilities', [])))
                targets = sorted(target for target in base['execution_targets']
                                 if ('agent' if target == 'project_agent' else target)
                                 not in rule.get('execution_targets', []))
                if ('execute' in base['capabilities']
                        and ceiling['manifest']['execution_mode'] != 'account_permissions_acknowledged'):
                    caps = sorted(set(caps) | {'execute'})
                if caps or targets:
                    restricted.append({'project_id': project_id, 'capabilities': caps,
                                       'execution_targets': targets})
            if restricted:
                return DevError('ROOM_PROJECT_CONNECTION_LIMITED',
                    '这个项目超出此连接已确认的权限，需先确认连接权限；本次选择已保留，尚未保存。',
                    403, remedy='review_connection_scope', grant_id=grant['id'],
                    conversation_id=conversation['id'], restricted_projects=restricted,
                    changes_applied=False)
        except (DevError, ValueError, KeyError, TypeError):
            pass  # Keep the original authorization error if attribution is uncertain.
        return None

    def preview(self, raw, principal):
        args = self.normalized_selection(validate(Selection, raw))
        with self.store.transaction(immediate=False):
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.owner(principal)
            conversation = self.c.conversations.object(principal, args['conversation_id'])
            try:
                result = self.checked_candidate(args, principal, conversation)
            except DevError as exc:
                return {'ready': False, 'reason_code': exc.code, 'message': exc.message,
                        'choices': self.candidates(conversation['id']),
                        'conversation_version': conversation['version'],
                        'scope_version': (self.current(conversation['id']) or {}).get('version', 0),
                        'changes_underlying_grant': False,
                        'permission_source': 'room_working_scope',
                        'permission_options': self.permission_options(args, principal, conversation),
                        **(exc.details if exc.code == 'ROOM_PROJECT_CONNECTION_LIMITED' else {})}
            return {k: v for k, v in result.items() if k != 'spec'}

    def apply(self, raw, principal):
        args = self.normalized_selection(validate(ApplySelection, raw))
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.owner(principal)
            conversation = self.c.conversations.object(principal, args['conversation_id'])
            # mutation handles retries before re-running a stale preview; a saved receipt
            # only returns the current room view and never replays a permission change.
            def save():
                previous = self.current(conversation['id'])
                if (conversation['version'] != args['expected_version']
                        or (previous['version'] if previous else 0) != args['expected_scope_version']):
                    raise DevError('STALE_VERSION', '房间项目已改变，请重新核对后确认', 409)
                result = self.checked_candidate(args, principal, conversation)
                if result['selection_digest'] != args['selection_digest']:
                    raise DevError('ROOM_PROJECT_PREVIEW_CHANGED', '项目、目录或连接权限已变化，请重新核对后确认', 409)
                spec, now = result['spec'], self.c.clock()
                # Keep the original transport/historical partitions. Add only missing
                # selected partitions; changing this work list never rewrites messages.
                for project in args['projects']:
                    known = self.store.one('''SELECT 1 FROM conversation_projects cp
                        JOIN collaboration_rooms r ON r.id=cp.room_id
                        WHERE cp.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                        (conversation['id'], project['project'], project['environment_id']))
                    if not known:
                        _, partition = self.c.scope(principal, {k: project[k] for k in ('project', 'environment_id')},
                                                    create=True, default_conversation=False)
                        self.store.execute('INSERT INTO conversation_projects VALUES (?,?,?)',
                                           (conversation['id'], partition['id'], now))
                self.store.execute("""INSERT INTO conversation_working_scopes
                    (conversation_id,version,spec,digest,created) VALUES (?,?,?,?,?)""",
                    (conversation['id'], spec['version'], canonical(spec), digest(spec), now))
                for project_id in result['removed']:
                    self.store.execute('INSERT INTO conversation_project_revocations VALUES (?,?,?,?)',
                                       (conversation['id'], project_id, spec['version'], now))
                old_rules = {p['project_id']: p for p in previous['spec']['projects']} if previous else {}
                baseline_policy = self.store.one('SELECT spec FROM delegation_policies WHERE id=?', (spec['policy_id'],))
                baseline = json.loads(baseline_policy['spec'])
                changed_permissions = [r['project_id'] for r in spec['projects'] if
                    (r['project_id'] in old_rules and r['authorization_id'] != old_rules[r['project_id']]['authorization_id'])
                    or (not previous and r['project_id'] in result['unchanged']
                        and any(set(r[field]) != set(baseline[field]) for field in ('capabilities', 'execution_targets')))]
                for project_id in changed_permissions:
                    self.store.execute('INSERT INTO conversation_project_revocations VALUES (?,?,?,?)',
                                       (conversation['id'], project_id, spec['version'], now))
                fenced = set(result['removed']) | set(changed_permissions)
                affected = []
                for goal in self.store.all("SELECT * FROM coordination_goals WHERE conversation_id=?",
                                           (conversation['id'],)):
                    if set(json.loads(goal['spec'])['project_ids']) & fenced:
                        if goal['state'] in {'active', 'proposed'}:
                            self.c.coordination.stop_work(goal, 'ROOM_PROJECT_REMOVED',
                                                          cancel_project_ids=fenced)
                            self.store.execute("""UPDATE coordination_goals SET state='paused',
                                version=version+1,updated=? WHERE id=?""", (now, goal['id']))
                            affected.append(goal['id'])
                self.store.execute('UPDATE conversation_rooms SET version=version+1 WHERE id=?',
                                   (conversation['id'],))
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',
                                      (spec['connection']['room_id'],))
                self.c.audit(room, principal.actor, 'conversation.work_projects_revised',
                    conversation['id'], {'scope_version': spec['version'], 'scope_digest': digest(spec),
                    'project_ids': [r['project_id'] for r in spec['projects']],
                    'removed': result['removed'], 'paused_goal_ids': affected,
                    'permission_changes': changed_permissions,
                    'project_permissions': [{k: rule[k] for k in
                        ('project_id', 'capabilities', 'execution_targets', 'authorization_id')} for rule in spec['projects']],
                    'confirm_scope': True, 'confirm_unsandboxed_exec': args.get('confirm_unsandboxed_exec', False),
                    'execution_authorization_source': 'confirm_scope_and_selection_digest',
                    'changes_underlying_grant': False, 'preserves_connection': True})
                return {'conversation': self.c.conversations.view(principal,
                        self.c.conversations.object(principal, conversation['id'])),
                        'applied': True, 'working_scope': self.reference(self.current(conversation['id'])),
                        'paused_goal_ids': affected, 'changes_underlying_grant': False}
            return self.c.conversations.mutation(principal, 'conversation_work_projects', args, save)

    def authorize(self, principal, policy, record):
        spec = record['spec']
        self.c.conversations.object(principal, spec['conversation_id'])
        dot = self.c.dots.find(spec['dot_id'])
        if (not dot or dot['setup_digest'] != spec['dot_setup_digest']
                or policy['slot_id'] != spec['dot_id'] or policy['id'] != spec['policy_id']
                or policy['version'] != spec['policy_version'] or policy['digest'] != spec['policy_digest']
                or policy['grant_id'] != spec['connection']['grant_id']):
            raise DevError('ROOM_PROJECT_CONNECTION_CHANGED', '原房间连接或批准基线已变化', 409)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
        self.c.delegation.authorize_policy(principal, room, policy)
        actor = self.c.grant_reader(room, policy['grant_id'], snapshot=json.loads(policy['grant_snapshot']))
        owner = refresh_principal(self.store, Principal(**{**json.loads(policy['principal']), 'scopes': set()}))
        self.c.owner(owner)
        for rule in spec['projects']:
            if rule['expires_at'] and rule['expires_at'] <= self.c.clock():
                raise DevError('ROOM_PROJECT_SCOPE_EXPIRED', '房间项目授权已到期', 409)
            for subject in (owner, principal, actor):
                project = self.c.runtime.project(rule['project_id'], subject)
                if self.c.coordination.project_snapshot(project) != rule['project_snapshot']:
                    raise DevError('ROOM_PROJECT_MAPPING_CHANGED', '房间项目目录或执行设置已变化', 409)
                for capability in rule['capabilities']:
                    self.c.coordination.require_capability(subject, capability, project['id'])
                # File-only permission does not depend on an unused VPS mapping.
                # Re-enabling execute still rebuilds and validates every target.
                for target, snapshot in (rule['target_snapshots'].items() if 'execute' in rule['capabilities'] else []):
                    if self.c.delegation.target_snapshot(subject, project, target) != snapshot:
                        raise DevError('ROOM_PROJECT_TARGET_CHANGED', '原批准执行目标已改变', 409)
            if root_overlap_ids(self.store, project) - {r['project_id'] for r in spec['projects']}:
                raise DevError('ROOM_PROJECT_ROOT_OVERLAP', '批准目录包含当前范围外项目，请重新核对', 409)
        return spec

    def task_defaults(self, principal, policy):
        """Default tasks use the common live room capabilities, never the old union."""
        base = json.loads(policy['spec'])
        record = self.current(policy['conversation_id'])
        if record is None:
            return {field: list(base[field]) for field in ('capabilities', 'execution_targets')}
        active = [rule for rule in record['spec']['projects'] if rule['capabilities']]
        if not active:
            raise DevError('ROOM_PROJECTS_EMPTY', '此房间当前仅聊天；请先选择项目及任务权限', 409)
        self.authorize(principal, policy, {**record, 'spec': {**record['spec'], 'projects': active}})
        capabilities = set.intersection(*(set(rule['capabilities']) for rule in active))
        capabilities &= set(base['capabilities'])
        targets = (set.intersection(*(set(rule['execution_targets']) for rule in active))
                   & set(base['execution_targets'])) if 'execute' in capabilities else (
                       {'project_agent'} & set(base['execution_targets']))
        if not targets and 'execute' in capabilities:
            capabilities.discard('execute')
            targets = {'project_agent'} & set(base['execution_targets'])
        if not capabilities or not targets:
            raise DevError('ROOM_PROJECT_SCOPE_DENIED', '当前房间没有共同可用的任务能力或执行位置，请明确选择项目目标', 403)
        return {'capabilities': sorted(capabilities), 'execution_targets': sorted(targets),
                'project_ids': [rule['project_id'] for rule in active]}

    def for_new_task(self, principal, policy, capabilities, targets):
        record = self.current(policy['conversation_id'])
        if record is None:
            return None
        active = [rule for rule in record['spec']['projects'] if rule['capabilities']]
        spec = self.authorize(principal, policy, {**record, 'spec': {**record['spec'], 'projects': active}})
        if not spec['projects']:
            raise DevError('ROOM_PROJECTS_EMPTY', '此房间当前仅聊天；请先选择要处理的项目', 409)
        for rule in spec['projects']:
            if not set(capabilities) <= set(rule['capabilities']) or (
                    'execute' in capabilities and not set(targets) <= set(rule['execution_targets'])):
                raise DevError('ROOM_PROJECT_SCOPE_DENIED', '任务能力或目标超过当前房间项目范围', 403)
        return {'reference': self.reference(record), 'project_ids': [r['project_id'] for r in spec['projects']]}

    def goal_scope(self, principal, conversation_id, project_ids, participant_grant_ids, capabilities):
        record = self.current(conversation_id)
        if not record:
            return None
        spec = record['spec']
        policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (spec['policy_id'],))
        if policy is None:
            raise DevError('ROOM_PROJECT_CONNECTION_CHANGED', '原连接批准已不可用', 409)
        allowed = {r['project_id']: r for r in spec['projects']}
        if (not project_ids or not set(project_ids) <= set(allowed)
                or set(participant_grant_ids) != {policy['grant_id']}
                or any(not set(capabilities) <= set(allowed[p]['capabilities']) for p in project_ids)):
            raise DevError('ROOM_PROJECT_SCOPE_DENIED', '目标必须在已确认的房间项目和原连接范围内', 403)
        self.authorize(principal, policy, {**record, 'spec': {**spec,
            'projects': [allowed[project_id] for project_id in project_ids]}})
        return self.reference(record)

    def validate_goal(self, principal, goal):
        current = self.current(goal['conversation_id'])
        if current is None:
            return
        goal_spec = json.loads(goal['spec'])
        reference = goal_spec.get('room_project_scope')
        if reference:
            saved = self.store.one("""SELECT * FROM conversation_working_scopes
                WHERE conversation_id=? AND version=?""", (goal['conversation_id'], reference['version']))
            if not saved or saved['digest'] != reference['digest']:
                raise DevError('ROOM_PROJECT_APPROVAL_CHANGED', '任务原房间项目批准已改变', 409)
            original = json.loads(saved['spec'])
            if digest(original) != saved['digest']:
                raise DevError('ROOM_PROJECT_APPROVAL_CHANGED', '任务原项目批准证据无效', 409)
            old = {r['project_id']: r for r in original['projects']}
            live = {r['project_id']: r for r in current['spec']['projects']}
            if any(p not in old or p not in live or old[p]['authorization_id'] != live[p]['authorization_id']
                   for p in goal_spec['project_ids']):
                raise DevError('ROOM_PROJECT_REMOVED', '任务项目已移除或重新授权；旧任务不能恢复新步骤', 409)
            policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (original['policy_id'],))
            if not policy:
                raise DevError('ROOM_PROJECT_CONNECTION_CHANGED', '任务原连接已不可用', 409)
            # Validate only the task's original subset, not later unrelated additions.
            self.authorize(principal, policy, {**saved, 'spec': {**original,
                'projects': [old[p] for p in goal_spec['project_ids']]}})
        else:
            live = {r['project_id']: r for r in current['spec']['projects']}
            live_ids = set(live)
            removed = self.store.all("""SELECT project_id FROM conversation_project_revocations
                WHERE conversation_id=? AND created>=?""", (goal['conversation_id'], goal['created']))
            if not set(goal_spec['project_ids']) <= live_ids or set(goal_spec['project_ids']) & {r['project_id'] for r in removed}:
                raise DevError('ROOM_PROJECT_REMOVED', '旧任务包含已移除项目，请按当前范围重新交办', 409)
            if any(not set(goal_spec['capabilities']) <= set(live[p]['capabilities'])
                   or not set(goal_spec.get('execution_targets', [])) <= set(live[p]['execution_targets'])
                   for p in goal_spec['project_ids']):
                raise DevError('ROOM_PROJECT_SCOPE_DENIED', '旧任务能力或执行位置超过当前房间权限，请重新交办', 403)
