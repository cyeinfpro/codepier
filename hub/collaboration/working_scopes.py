"""Versioned room work projects, independent of the stable message connection.

Historical conversation partitions and dot enrollment are never rewritten.
Only explicit panel-owner revisions authorize new managed task scopes; ordinary
connector grants, notification routes and external-channel bindings are unchanged.
"""
from __future__ import annotations

import json

from pydantic import Field
from typing import Literal

from hub.collaboration.common import canonical, digest, validate
from hub.principal import Principal, refresh_principal
from hub.room_scope import root_overlap_ids
from shared import collaboration_contracts as contracts
from shared.util import DevError


class Selection(contracts.Model):
    conversation_id: contracts.Identifier
    projects: list[contracts.Scope] = Field(default_factory=list, max_length=16)
    dot_id: str = Field(default='', max_length=128)


class ApplySelection(Selection):
    expected_version: int = Field(ge=1)
    expected_scope_version: int = Field(ge=0)
    selection_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm_scope: Literal[True]
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
        seen, projects = set(), []
        for project in args['projects']:
            identity = (project['project'], project['environment_id'])
            if identity not in seen:
                seen.add(identity)
                projects.append(project)
        return {**args, 'projects': projects}

    def candidate(self, args, principal, conversation):
        previous = self.current(conversation['id'])
        choices = self.candidates(conversation['id'])
        chosen = args['projects']
        ids = [item['project'] for item in chosen]
        if len(set(ids)) != len(ids):
            raise DevError('DUPLICATE_PROJECT', '同一项目选择了不同环境。请刷新项目列表，选择一个环境后再保存；本次未更改。', 422)
        old_rules = {r['project_id']: r for r in previous['spec']['projects']} if previous else {}
        narrowing = not ids or previous is not None and set(ids) <= set(old_rules)
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
        version = (previous['version'] if previous else 0) + 1
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
                rules.append(dict(old_rules[item['project']]))
                continue
            project = self.c.runtime.project(item['project'], principal)
            if project['id'] != item['project']:
                raise DevError('PROJECT_ID_REQUIRED', '请使用明确项目编号', 422)
            for subject in (principal, actor):
                self.c.runtime.project(project['id'], subject)
                for capability in base['capabilities']:
                    self.c.coordination.require_capability(subject, capability, project['id'])
            if 'write' in base['capabilities'] and project['mode'] != 'write':
                raise DevError('READ_ONLY', '所选项目没有写入权限', 403)
            if 'execute' in base['capabilities'] and not project['allow_tasks']:
                raise DevError('TASKS_DISABLED', '所选项目没有执行权限', 403)
            if root_overlap_ids(self.store, project) - set(ids):
                raise DevError('ROOM_PROJECT_ROOT_OVERLAP',
                    '项目“' + project.get('alias', project['id']) + '”包含未选择的项目目录。请取消此上层目录，仅选择需要的独立项目；不会扩大底层凭据。', 409)
            targets = {target: self.c.delegation.target_snapshot(subject, project, target)
                       for target in base['execution_targets'] for subject in (principal, actor)}
            rule = {'project_id': project['id'], 'project_label': project.get('alias', project['id']),
                    'environment_id': room['environment_id'], 'capabilities': sorted(base['capabilities']),
                    'execution_targets': list(base['execution_targets']),
                    'execution_target': base['execution_targets'][0] if len(base['execution_targets']) == 1 else '',
                    'project_snapshot': self.c.coordination.project_snapshot(project),
                    'target_snapshots': targets,
                    'expires_at': 0 if self.c.channel_owner.persistent(policy) else policy['expires_at']}
            old = old_rules.get(project['id'])
            unchanged = old and all(old.get(k) == v for k, v in rule.items())
            rule['authorization_id'] = old['authorization_id'] if unchanged else digest(
                [conversation['id'], dot_id, version, rule])
            rules.append(rule)
        rules.sort(key=lambda r: r['project_id'])
        if not narrowing and 'execute' in base['capabilities']:
            from hub.room_scope import require_tool
            for rule in rules:
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
                'added': sorted(set(ids) - old_ids), 'removed': sorted(old_ids - set(ids)),
                'unchanged': sorted(old_ids & set(ids)), 'spec': spec,
                'changes_underlying_grant': False,
                'message': '保存后沿用原消息连接；移除项目不能开始新步骤，运行操作以取消回执为准。'}

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
                        _, partition = self.c.scope(principal, project, create=True, default_conversation=False)
                        self.store.execute('INSERT INTO conversation_projects VALUES (?,?,?)',
                                           (conversation['id'], partition['id'], now))
                self.store.execute("""INSERT INTO conversation_working_scopes
                    (conversation_id,version,spec,digest,created) VALUES (?,?,?,?,?)""",
                    (conversation['id'], spec['version'], canonical(spec), digest(spec), now))
                for project_id in result['removed']:
                    self.store.execute('INSERT INTO conversation_project_revocations VALUES (?,?,?,?)',
                                       (conversation['id'], project_id, spec['version'], now))
                affected = []
                for goal in self.store.all("SELECT * FROM coordination_goals WHERE conversation_id=?",
                                           (conversation['id'],)):
                    if set(json.loads(goal['spec'])['project_ids']) & set(result['removed']):
                        if goal['state'] in {'active', 'proposed'}:
                            self.c.coordination.stop_work(goal, 'ROOM_PROJECT_REMOVED',
                                                          cancel_project_ids=set(result['removed']))
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
                    'changes_underlying_grant': False, 'preserves_connection': True})
                return {'conversation': self.c.conversations.view(principal,
                        self.c.conversations.object(principal, conversation['id'])),
                        'applied': True, 'working_scope': self.reference(self.current(conversation['id'])),
                        'paused_goal_ids': affected, 'changes_underlying_grant': False}
            return self.c.conversations.mutation(principal, 'conversation_work_projects', args, save)

    def authorize(self, principal, policy, record):
        spec = record['spec']
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
                for target, snapshot in rule['target_snapshots'].items():
                    if self.c.delegation.target_snapshot(subject, project, target) != snapshot:
                        raise DevError('ROOM_PROJECT_TARGET_CHANGED', '原批准执行目标已改变', 409)
            if root_overlap_ids(self.store, project) - {r['project_id'] for r in spec['projects']}:
                raise DevError('ROOM_PROJECT_ROOT_OVERLAP', '批准目录包含当前范围外项目，请重新核对', 409)
        return spec

    def for_new_task(self, principal, policy, capabilities, targets):
        record = self.current(policy['conversation_id'])
        if record is None:
            return None
        spec = self.authorize(principal, policy, record)
        if not spec['projects']:
            raise DevError('ROOM_PROJECTS_EMPTY', '此房间当前仅聊天；请先选择要处理的项目', 409)
        for rule in spec['projects']:
            if not set(capabilities) <= set(rule['capabilities']) or not set(targets) <= set(rule['execution_targets']):
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
        self.authorize(principal, policy, record)
        allowed = {r['project_id']: r for r in spec['projects']}
        if (not project_ids or not set(project_ids) <= set(allowed)
                or set(participant_grant_ids) != {policy['grant_id']}
                or any(not set(capabilities) <= set(allowed[p]['capabilities']) for p in project_ids)):
            raise DevError('ROOM_PROJECT_SCOPE_DENIED', '目标必须在已确认的房间项目和原连接范围内', 403)
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
            live_ids = {r['project_id'] for r in current['spec']['projects']}
            removed = self.store.all("""SELECT project_id FROM conversation_project_revocations
                WHERE conversation_id=? AND created>=?""", (goal['conversation_id'], goal['created']))
            if not set(goal_spec['project_ids']) <= live_ids or set(goal_spec['project_ids']) & {r['project_id'] for r in removed}:
                raise DevError('ROOM_PROJECT_REMOVED', '旧任务包含已移除项目，请按当前范围重新交办', 409)
