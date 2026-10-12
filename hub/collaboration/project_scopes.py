"""Explicit, append-only project additions for an existing task dot.

The panel's add-and-authorize action creates evidence for NEW tasks only.
It does not edit enrollment baselines, grants, subscriptions or old approvals.
"""
from __future__ import annotations

import json
import uuid

from hub.collaboration.common import canonical, digest, validate
from hub.principal import Principal, refresh_principal
from shared import collaboration_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_project_scopes (
        id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,dot_id TEXT NOT NULL,
        project_id TEXT NOT NULL,environment_id TEXT NOT NULL,version INTEGER NOT NULL,
        spec TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',
        created REAL NOT NULL,revoked_at REAL,
        UNIQUE(conversation_id,dot_id,version))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_project_scope_immutable
        BEFORE UPDATE ON conversation_project_scopes
        WHEN NEW.id!=OLD.id OR NEW.conversation_id!=OLD.conversation_id OR NEW.dot_id!=OLD.dot_id
          OR NEW.project_id!=OLD.project_id OR NEW.environment_id!=OLD.environment_id
          OR NEW.version!=OLD.version OR NEW.spec!=OLD.spec OR NEW.digest!=OLD.digest
          OR NEW.created!=OLD.created OR (OLD.state='revoked' AND NEW.state!='revoked')
        BEGIN SELECT RAISE(ABORT,'project scope approval is immutable'); END""")


class ProjectScopes:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def revision(self, conversation_id, dot_id):
        return self.store.one("""SELECT COALESCE(MAX(version),0) AS n
            FROM conversation_project_scopes WHERE conversation_id=? AND dot_id=?""",
            (conversation_id, dot_id))['n']

    def candidate(self, principal, conversation, project, environment, dot_id):
        dot = self.c.dots.find(dot_id)
        if not dot or dot['conversation_id'] != conversation['id']:
            raise DevError('PROJECT_SCOPE_DOT_REQUIRED', '请选择这个房间已接入的助手', 409)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
        if environment != room['environment_id']:
            raise DevError('PROJECT_SCOPE_ENVIRONMENT_MISMATCH', '新增项目必须使用该助手已确认的同一环境', 409)
        if project['id'] == room['project_id']:
            raise DevError('PROJECT_SCOPE_ALREADY_ANCHOR', '这是助手原已批准的项目，无需重复添加', 409)
        policy = self.c.dots.policy(room, dot)
        self.c.delegation.authorize_policy(principal, room, policy)
        base = json.loads(policy['spec'])
        if base['execution_targets'] != ['project_agent']:
            raise DevError('PROJECT_SCOPE_TARGET_REQUIRED', '原助手执行目标不是项目 Agent；不能推断新增项目的 VPS 授权', 409)
        if 'execute' not in base['capabilities'] or not base['acknowledge_unsandboxed_exec']:
            raise DevError('PROJECT_SCOPE_EXECUTE_REQUIRED', '原助手没有已确认的执行能力，不能声称添加后可执行', 409)
        slot = self.c.joining.slot(room, dot_id)
        actor = self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(policy['grant_snapshot']))
        for subject in (principal, actor):
            self.c.runtime.project(project['id'], subject)
            for capability in base['capabilities']:
                self.c.coordination.require_capability(subject, capability, project['id'])
        if project['mode'] != 'write' or not project['allow_tasks']:
            raise DevError('PROJECT_SCOPE_EXECUTION_DISABLED', '新增项目未允许写入和任务执行', 409)
        # Never copy an until-revoked channel authorization into this operation.
        expires = min(policy['expires_at'], slot['expires_at'], policy['created'] + 604800)
        if expires - self.c.clock() < 60:
            raise DevError('PROJECT_SCOPE_APPROVAL_EXPIRED', '原助手执行范围已到期或即将到期，请先核对原范围', 409)
        for row in self.store.all("""SELECT * FROM conversation_project_scopes
                WHERE conversation_id=? AND dot_id=? AND project_id=? AND environment_id=? AND state='active'""",
                (conversation['id'], dot_id, project['id'], environment)):
            if json.loads(row['spec'])['expires_at'] > self.c.clock():
                raise DevError('PROJECT_SCOPE_ALREADY_APPROVED', '这个项目已有独立授权记录，请先核对或撤销该记录', 409)
        return {
            'conversation_id': conversation['id'], 'conversation_version': conversation['version'],
            'approved_by': principal.actor, 'owner_user_id': principal.user_id, 'space_id': principal.space_id,
            'dot_id': dot_id, 'dot_label': slot['label'], 'grant_id': slot['grant_id'],
            'dot_setup_digest': dot['setup_digest'], 'anchor_room_id': room['id'],
            'anchor_project_id': room['project_id'], 'project_id': project['id'],
            'project_label': project.get('alias', project['id']), 'environment_id': environment,
            'policy_id': policy['id'], 'policy_version': policy['version'], 'policy_digest': policy['digest'],
            'scope_version': self.revision(conversation['id'], dot_id) + 1,
            'capabilities': sorted(base['capabilities']), 'execution_target': 'project_agent',
            'project_snapshot': self.c.coordination.project_snapshot(project),
            'target_snapshot': self.c.delegation.target_snapshot(principal, project, 'project_agent'),
            'expires_at': expires, 'reply_conversation_id': conversation['id'],
            'reply_location': '当前房间内该助手的原任务话题',
            'execution_boundary': 'execution_account_not_project_sandbox',
            'applies_to': 'new_explicit_owner_tasks_only',
        }

    def preview(self, raw, principal):
        args = validate(contracts.ConversationProjectPreview, raw)
        with self.store.transaction(immediate=False):
            principal = refresh_principal(self.store, principal)
            self.c.guard()
            self.c.owner(principal)
            conversation = self.c.conversations.object(principal, args['conversation_id'])
            project = self.c.runtime.project(args['project'], principal)
            choices = self.store.all("""SELECT d.id,s.label FROM collaboration_dots d
                JOIN collaboration_join_slots s ON s.id=d.id
                WHERE d.conversation_id=? AND s.state='registered' ORDER BY s.created,s.id""",
                (conversation['id'],))
            dot_id = args['dot_id'] or (choices[0]['id'] if len(choices) == 1 else '')
            result = {'choices': choices, 'changes_authority': False}
            if not dot_id:
                return {**result, 'ready': False, 'reason_code': 'PROJECT_SCOPE_DOT_REQUIRED',
                        'message': '请选择授权执行此项目的助手' if choices else '此房间尚无已接入的任务助手'}
            try:
                spec = self.candidate(principal, conversation, project, args['environment_id'], dot_id)
            except DevError as exc:
                return {**result, 'ready': False, 'reason_code': exc.code, 'message': exc.message}
            return {**result, 'ready': True, 'scope': spec, 'scope_digest': digest(spec)}

    def approve(self, principal, conversation, project, environment, approval):
        spec = self.candidate(principal, conversation, project, environment, approval['dot_id'])
        if not approval['confirm_scope'] or digest(spec) != approval['scope_digest']:
            raise DevError('PROJECT_SCOPE_PREVIEW_CHANGED', '项目、助手或执行范围已改变，请重新审阅后添加', 409)
        identifier = uuid.uuid4().hex
        self.store.execute("""INSERT INTO conversation_project_scopes
            (id,conversation_id,dot_id,project_id,environment_id,version,spec,digest,created)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (identifier, conversation['id'], spec['dot_id'], project['id'], environment,
             spec['scope_version'], canonical(spec), digest(spec), self.c.clock()))
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (spec['anchor_room_id'],))
        self.c.audit(room, principal.actor, 'conversation.project_scope_approved', identifier,
                     {'scope_version': spec['scope_version'], 'scope_digest': digest(spec),
                      'project_id': project['id'], 'dot_id': spec['dot_id'], 'creates_grant': False})
        return self.reference(self.store.one('SELECT * FROM conversation_project_scopes WHERE id=?', (identifier,)))

    @staticmethod
    def reference(row):
        return {'id': row['id'], 'version': row['version'], 'digest': row['digest']}

    def authorize(self, principal, policy, reference):
        row = self.store.one('SELECT * FROM conversation_project_scopes WHERE id=?', (reference['id'],))
        if not row or self.reference(row) != reference:
            raise DevError('PROJECT_SCOPE_APPROVAL_CHANGED', '项目授权证据不存在或已改变', 409)
        spec = json.loads(row['spec'])
        if digest(spec) != row['digest'] or row['state'] != 'active' or spec['expires_at'] <= self.c.clock():
            raise DevError('PROJECT_SCOPE_INACTIVE', '项目执行授权已撤销、到期或证据无效', 409)
        dot = self.c.dots.find(spec['dot_id'])
        if (not dot or dot['setup_digest'] != spec['dot_setup_digest']
                or policy['id'] != spec['policy_id'] or policy['version'] != spec['policy_version']
                or policy['digest'] != spec['policy_digest'] or policy['slot_id'] != spec['dot_id']
                or policy['grant_id'] != spec['grant_id'] or policy['conversation_id'] != spec['conversation_id']):
            raise DevError('PROJECT_SCOPE_BASE_CHANGED', '原助手或原执行范围已改变，新增项目授权失效', 409)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (spec['anchor_room_id'],))
        self.c.delegation.authorize_policy(principal, room, policy)
        if not self.store.one("""SELECT 1 FROM conversation_projects cp
                JOIN collaboration_rooms r ON r.id=cp.room_id
                WHERE cp.conversation_id=? AND r.project_id=? AND r.environment_id=?""",
                (spec['conversation_id'], spec['project_id'], spec['environment_id'])):
            raise DevError('PROJECT_SCOPE_MEMBERSHIP_CHANGED', '项目已不属于原确认的房间环境', 409)
        owner = refresh_principal(self.store, Principal(**{**json.loads(policy['principal']), 'scopes': set()}))
        actor = self.c.grant_reader(room, spec['grant_id'], snapshot=json.loads(policy['grant_snapshot']))
        for subject in (owner, actor, principal):
            project = self.c.runtime.project(spec['project_id'], subject)
            if self.c.coordination.project_snapshot(project) != spec['project_snapshot']:
                raise DevError('PROJECT_SCOPE_PROJECT_CHANGED', '项目目录、设备或执行设置已改变', 409)
            for capability in spec['capabilities']:
                self.c.coordination.require_capability(subject, capability, project['id'])
        if self.c.delegation.target_snapshot(owner, project, 'project_agent') != spec['target_snapshot']:
            raise DevError('PROJECT_SCOPE_TARGET_CHANGED', '项目执行目标已改变', 409)
        return spec

    def for_new_task(self, principal, policy, capabilities, targets):
        # Saved policies and existing requests retain their original immutable scope.
        if targets != ['project_agent']:
            return [], []
        refs, projects = [], []
        for row in self.store.all("""SELECT * FROM conversation_project_scopes
                WHERE conversation_id=? AND dot_id=? AND state='active' ORDER BY version""",
                (policy['conversation_id'], policy['slot_id'])):
            spec = json.loads(row['spec'])
            if spec['expires_at'] <= self.c.clock():
                continue
            ref = self.reference(row)
            spec = self.authorize(principal, policy, ref)
            if not set(capabilities) <= set(spec['capabilities']):
                raise DevError('PROJECT_SCOPE_CAPABILITY_DENIED', '本次任务能力超过新增项目的明确批准', 403)
            refs.append(ref)
            projects.append(spec['project_id'])
        return refs, projects

    def validate_task(self, principal, room, policy, snapshot, goal):
        refs = snapshot.get('project_scope_approvals', [])
        approved = {room['project_id']}
        for ref in refs:
            spec = self.authorize(principal, policy, ref)
            approved.add(spec['project_id'])
        if set(json.loads(goal['spec'])['project_ids']) != approved:
            raise DevError('PROJECT_SCOPE_TASK_CHANGED', '任务项目与其不可变范围证据不一致', 409)

    def listing(self, principal, conversation, project_ids):
        result = []
        for row in self.store.all("""SELECT * FROM conversation_project_scopes
                WHERE conversation_id=? AND state='active' ORDER BY created,version""", (conversation['id'],)):
            spec = json.loads(row['spec'])
            if spec['project_id'] not in project_ids or spec['anchor_project_id'] not in project_ids:
                continue
            if principal.grant_id and principal.grant_id != spec['grant_id']:
                continue
            policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (spec['policy_id'],))
            reason = ''
            try:
                if not policy:
                    raise DevError('PROJECT_SCOPE_BASE_CHANGED', '原执行范围已不可用', 409)
                self.authorize(principal, policy, self.reference(row))
            except DevError as exc:
                reason = exc.code
            result.append({**self.reference(row), **{key: spec[key] for key in
                ('project_id', 'project_label', 'environment_id', 'dot_id', 'dot_label',
                 'capabilities', 'execution_target', 'reply_location', 'expires_at')},
                'effective_active': not bool(reason), 'blocked_reason': reason})
        return result

    def control(self, raw, principal):
        args = validate(contracts.ConversationProjectScopeControl, raw)
        with self.store.transaction():
            principal = refresh_principal(self.store, principal)
            self.c.guard()
            self.c.owner(principal)
            conversation = self.c.conversations.object(principal, args['conversation_id'])
            row = self.store.one('SELECT * FROM conversation_project_scopes WHERE id=? AND conversation_id=?',
                                 (args['scope_id'], conversation['id']))
            if not row:
                raise DevError('PROJECT_SCOPE_NOT_FOUND', '这个房间没有该项目授权记录', 404)
            def save():
                if row['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '项目授权版本已改变', 409)
                self.store.execute("UPDATE conversation_project_scopes SET state='revoked',revoked_at=? WHERE id=?",
                                   (self.c.clock(), row['id']))
                for link in self.store.all('SELECT * FROM delegation_requests WHERE conversation_id=?', (conversation['id'],)):
                    if self.reference(row) not in json.loads(link['policy_snapshot']).get('project_scope_approvals', []):
                        continue
                    room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (link['room_id'],))
                    goal = self.c.coordination.object(room, link['goal_id'])
                    self.c.coordination.stop_work(goal, 'PROJECT_SCOPE_REVOKED')
                    self.store.execute("UPDATE coordination_goals SET state='paused',version=version+1,updated=? WHERE id=?",
                                       (self.c.clock(), goal['id']))
                anchor = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',
                                        (json.loads(row['spec'])['anchor_room_id'],))
                self.c.audit(anchor, principal.actor, 'conversation.project_scope_revoked', row['id'], self.reference(row))
                return {'conversation': self.c.conversations.view(principal, conversation), 'revoked': True}
            return self.c.conversations.mutation(principal, 'conversation_project_scope_revoke', args, save)
