"""Explicit owner recovery of identities inside an immutable native Goal.

The original Goal, approval, source, project limits and operation journal stay
authoritative.  These immutable revisions replace only a role's live member
binding.  Every revision advances the coordinator epoch, stops original work
through CoordinationService and records a new owner intent on the same Goal.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import uuid

from hub.collaboration.common import canonical, digest
from hub.principal import Principal, refresh_principal
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_goal_recovery_revisions(
        id TEXT PRIMARY KEY,goal_id TEXT NOT NULL REFERENCES coordination_goals(id),
        scope_id TEXT NOT NULL REFERENCES room_native_work_scopes(id),
        coordinator_epoch INTEGER NOT NULL CHECK(coordinator_epoch>=2),
        spec TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,
        UNIQUE(goal_id,coordinator_epoch))''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS room_goal_recovery_immutable
        BEFORE UPDATE ON room_goal_recovery_revisions
        BEGIN SELECT RAISE(ABORT,'room Goal recovery evidence is immutable'); END''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_goal_recovery_current(
        goal_id TEXT PRIMARY KEY REFERENCES coordination_goals(id),
        scope_id TEXT NOT NULL UNIQUE REFERENCES room_native_work_scopes(id),
        revision_id TEXT NOT NULL UNIQUE REFERENCES room_goal_recovery_revisions(id),
        coordinator_epoch INTEGER NOT NULL CHECK(coordinator_epoch>=2))''')


class RoomGoalRecovery:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    @property
    def native(self):
        return self.c.room_work_bridge.native

    def epoch(self, goal_id):
        row = self.store.one('SELECT coordinator_epoch FROM room_goal_recovery_current WHERE goal_id=?', (goal_id,))
        return row['coordinator_epoch'] if row else 1

    def revision(self, reference):
        row = self.store.one('''SELECT r.*,c.coordinator_epoch AS current_epoch FROM room_goal_recovery_current c
            JOIN room_goal_recovery_revisions r ON r.id=c.revision_id WHERE c.scope_id=?''', (reference['id'],))
        if not row:
            return None
        value = json.loads(row['spec'])
        if (digest(value) != row['digest'] or value['scope_id'] != reference['id']
                or value['original_digest'] != reference['digest'] or value['goal_id'] != row['goal_id']
                or value['coordinator_epoch'] != row['coordinator_epoch']
                or row['coordinator_epoch'] != row['current_epoch']):
            raise DevError('ROOM_RECOVERY_EVIDENCE_CHANGED', '原工作恢复证据已改变', 403)
        return value

    def roles(self, reference, original):
        revision = self.revision(reference)
        return deepcopy(revision['roles'] if revision else {m['member_id']:m for m in original['members']})

    @staticmethod
    def merge_roles(roles):
        members = {}
        identity = ('grant_id', 'binding_generation')
        evidence = ('scope_version', 'scope_digest', 'legacy_dot_id', 'policy_id',
                    'policy_version', 'policy_digest')
        for saved in roles.values():
            member = members.setdefault(saved['member_id'], {**saved, 'projects':[]})
            if any(member[k] != saved[k] for k in identity):
                raise DevError('ROOM_RECOVERY_OTHER_ROLE_STALE', '此成员的另一原工作角色需要先明确恢复', 409)
            for rule in saved['projects']:
                current = next((r for r in member['projects'] if (r['project_id'],r['environment_id']) ==
                    (rule['project_id'],rule['environment_id'])), None)
                if current is None:
                    saved_rule=deepcopy(rule)
                    saved_rule['resource_snapshot']['targets']={k:v for k,v in rule['resource_snapshot'].get('targets',{}).items()
                        if k in rule['execution_targets']}
                    member['projects'].append(saved_rule)
                    continue
                targets={k:v for k,v in rule['resource_snapshot'].get('targets',{}).items() if k in rule['execution_targets']}
                if (any(current[k] != rule[k] for k in evidence)
                        or current['resource_snapshot']['project']!=rule['resource_snapshot']['project']
                        or any(current['resource_snapshot']['targets'][k]!=v for k,v in targets.items()
                               if k in current['resource_snapshot']['targets'])):
                    raise DevError('ROOM_RECOVERY_OTHER_ROLE_STALE', '此成员的另一原工作范围需要先明确恢复', 409)
                current['resource_snapshot']['targets'].update(targets)
                current['capabilities'] = sorted(set(current['capabilities']) | set(rule['capabilities']))
                current['execution_targets'] = sorted(set(current['execution_targets']) | set(rule['execution_targets']))
                finite = [v for v in (current.get('expires_at'),rule.get('expires_at')) if v is not None]
                current['expires_at'] = min(finite) if finite else None
        return list(members.values())

    def effective(self, reference, original):
        revision = self.revision(reference)
        if not revision:
            return {**original, 'coordinator_epoch':1}
        members = self.merge_roles(revision['roles'])
        lead = revision['roles'][original['coordinator_member_id']]
        return {**original, 'members':members, 'coordinator_member_id':lead['member_id'],
                'coordinator_grant_id':lead['grant_id'], 'participant_grant_ids':sorted({m['grant_id'] for m in members}),
                'coordinator_epoch':revision['coordinator_epoch']}

    def authority(self, reference):
        revision = self.revision(reference)
        if revision:
            owner = refresh_principal(self.store, Principal(**{**revision['owner_principal'], 'scopes':set()}))
            self.c.rooms.owner(owner, revision['conversation_id'])

    def require_epoch(self, goal, supplied):
        current = self.epoch(goal['id'])
        if type(supplied) is not int or supplied != current:
            raise DevError('ROOM_COORDINATOR_CHANGED', '原工作主责世代已改变，请读取当前工作后继续', 409,
                           coordinator_epoch=current)
        return current

    def candidate(self, member, bounds, conversation_id):
        if self.c.rooms.state(member) != 'ready_work':
            raise DevError('ROOM_RECOVERY_MEMBER_UNAVAILABLE', '接续成员必须完成真实连接及当前工作范围批准', 409)
        actor = self.c.rooms.principal(member)
        self.c.rooms.member_scope(actor,conversation_id,member['id'],member['binding_generation'])
        self.c.room_consumers.binding(self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(conversation_id,)), member, actor)
        rules = []
        for old in bounds['projects']:
            if old.get('expires_at') is not None and old['expires_at'] <= self.c.clock():
                raise DevError('ROOM_WORK_APPROVAL_EXPIRED', '原成员对此工作的批准已到期，恢复不能延长原批准', 403)
            adapter, scope = self.c.room_work_bridge.adapter(member,old['project_id'],old['environment_id'])
            policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?',(adapter['policy_id'],))
            if not policy or not self.c.room_work_bridge.authorized(policy):
                raise DevError('ROOM_WORK_SCOPE_CHANGED', '接续成员的当前项目批准已不可用', 403)
            current = json.loads(policy['spec'])
            resource = json.loads(scope['resource_snapshot'])
            if (resource.get('project') != old['resource_snapshot'].get('project')
                    or any(resource.get('targets',{}).get(k) != old['resource_snapshot'].get('targets',{}).get(k)
                           for k in old['execution_targets'])
                    or not set(old['capabilities']) <= set(current['capabilities'])
                    or not set(old['execution_targets']) <= set(current['execution_targets'])):
                raise DevError('ROOM_RECOVERY_SCOPE_DENIED', '接续成员未被明确批准原角色的项目、能力和执行位置', 403)
            for capability in old['capabilities']:
                self.c.coordination.require_capability(actor,capability,old['project_id'])
            finite = [v for v in (old.get('expires_at'),scope['expires_at'] if scope['lifetime_mode']=='timed' else None) if v is not None]
            rules.append({**old, 'scope_version':scope['version'], 'scope_digest':self.c.room_work_bridge.fingerprint([scope]),
                'legacy_dot_id':adapter['legacy_dot_id'], 'policy_id':policy['id'], 'policy_version':policy['version'],
                'policy_digest':policy['digest'], 'expires_at':min(finite) if finite else None})
        return {'member_id':member['id'], 'name':member['name'], 'grant_id':member['grant_id'],
                'binding_generation':member['binding_generation'], 'projects':rules}

    def eligible_base(self, goal, owner):
        if goal['state'] not in {'active','paused'} or not goal['approval_id'] or (goal['expires_at'] or 0) <= self.c.clock():
            raise DevError('ROOM_RECOVERY_UNAVAILABLE', '原工作未批准、已取消或到期，不能恢复原批准', 409)
        approval = self.store.one('SELECT * FROM coordination_approvals WHERE id=? AND goal_id=?',(goal['approval_id'],goal['id']))
        if not approval or approval['digest'] != goal['digest'] or approval['expires_at'] <= self.c.clock():
            raise DevError('GOAL_APPROVAL_CHANGED', '原工作批准已失效', 403)
        approver = refresh_principal(self.store,Principal(**{**json.loads(approval['principal']),'scopes':set()}))
        self.c.owner(approver)
        self.c.rooms.owner(owner,goal['conversation_id'])
        if self.store.one('SELECT state FROM conversation_rooms WHERE id=?',(goal['conversation_id'],))['state'] != 'active':
            raise DevError('ROOM_WORK_INACTIVE', '先恢复房间，再明确恢复原工作', 409)
        for project in json.loads(goal['spec'])['projects']:
            current = self.c.runtime.project(project['project_id'],approver)
            if self.c.coordination.project_snapshot(current) != project:
                raise DevError('GOAL_PROJECT_CHANGED', '原项目执行位置或设置已改变', 409)
            for capability in self.native.project_capabilities(goal,project['project_id']):
                self.c.coordination.require_capability(approver,capability,project['project_id'])
        return approval

    def plan(self, goal, action, target, owner):
        reference = self.native.reference(goal)
        if not reference:
            raise DevError('ROOM_RECOVERY_UNAVAILABLE', '此入口仅用于原房间成员工作', 409)
        self.eligible_base(goal,owner)
        original = self.native.original(reference)
        roles = self.roles(reference,original)
        member = self.c.rooms.member(target,goal['conversation_id'])
        if action == 'restore_binding':
            selected = [identifier for identifier,saved in roles.items() if saved['member_id']==target]
            if not selected:
                raise DevError('ROOM_RECOVERY_MEMBER_REQUIRED', '恢复只能选择这个原工作中已批准的稳定成员', 403)
            changed = False
            for identifier in selected:
                replacement = self.candidate(member,roles[identifier],goal['conversation_id'])
                changed = changed or replacement != roles[identifier]
                roles[identifier] = replacement
            if not changed:
                raise DevError('ROOM_RECOVERY_NOT_NEEDED', '此成员的原工作身份已是当前批准', 409)
        else:
            coordinator = original['coordinator_member_id']
            if roles[coordinator]['member_id'] == target:
                raise DevError('ROOM_RECOVERY_NOT_NEEDED', '此成员已经是当前工作主责', 409)
            roles[coordinator] = self.candidate(member,roles[coordinator],goal['conversation_id'])
        # A second role may never be silently restored by taking over another.
        merged = self.merge_roles(roles)
        for saved in merged:
            if saved['member_id']==target:
                for rule in saved['projects']:
                    self.native.validate_rule(saved,rule,goal['conversation_id'])
        source = self.native.source(goal,owner)
        if json.loads(source['body']).get('external_origin',{}).get('native_room'):
            available = set()
            for saved in merged:
                for rule in saved['projects']:
                    try:
                        self.native.validate_rule(saved,rule,goal['conversation_id'])
                        available.add(rule['project_id'])
                    except DevError:
                        pass
            authority = self.c.room_channels.native.recovery_author(goal,source,owner,project_ids=available)
        else:
            authority = self.c.channel_owner.author(source)
        if not authority['authenticated']:
            raise DevError('ROOM_OWNER_SOURCE_REQUIRED', '原输入的本人任务批准已失效', 403)
        return reference,original,roles

    @staticmethod
    def public_candidate(member):
        return {'member_id':member['member_id'],'name':member['name'],'state':'ready_work',
                'binding_generation':member['binding_generation'],
                'projects':[{k:r[k] for k in ('project_id','environment_id','capabilities','execution_targets')} for r in member['projects']]}

    def projection(self, goal, owner):
        reference = self.native.reference(goal)
        if not reference or owner.grant_id:
            return None
        spec = self.native.record(reference)
        lead = next(m for m in spec['members'] if m['member_id']==spec['coordinator_member_id'])
        result = {'coordinator_member_id':lead['member_id'],'coordinator_name':lead['name'],
            'coordinator_epoch':self.epoch(goal['id']),'intent_revision':self.c.room_runtime.intent_revision(goal['id']),
            'state':'available','reason_code':'','restore_candidates':[],'takeover_candidates':[]}
        try:
            self.eligible_base(goal,owner)
        except DevError as exc:
            return {**result,'state':'unavailable','reason_code':exc.code}
        if any(op['state'] not in {'succeeded','failed','cancelled'} for op in self.c.coordination.operations(goal)):
            result.update(state='waiting_operations',reason_code='WORK_OPERATION_PENDING')
        current_ids = {m['member_id'] for m in spec['members']}
        for member in self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('removed','paused','legacy','invited') ORDER BY created_at,id",(goal['conversation_id'],)):
            for action,field in (('restore_binding','restore_candidates'),('takeover','takeover_candidates')):
                if action=='restore_binding' and member['id'] not in current_ids:
                    continue
                try:
                    _,_,roles = self.plan(goal,action,member['id'],owner)
                    saved = next(m for m in self.merge_roles(roles) if m['member_id']==member['id'])
                    result[field].append(self.public_candidate(saved))
                except DevError:
                    continue
        return result

    def apply(self, goal, args, owner, conversation):
        if goal['version'] != args['expected_version']:
            raise DevError('STALE_VERSION', '工作已更新，请核对最新进展', 409)
        epoch = self.require_epoch(goal,args['expected_coordinator_epoch'])
        intent = self.c.room_runtime.intent_revision(goal['id'])
        if args['expected_intent_revision'] != intent:
            raise DevError('ROOM_INTENT_CHANGED', '本人意图已更新，请重新核对', 409,intent_revision=intent)
        reference,original,roles = self.plan(goal,args['action'],args['target_member_id'],owner)
        owner_snapshot = {**asdict(owner),'scopes':[],'token_hash':'','session_hash':''}
        value = {'goal_id':goal['id'],'scope_id':reference['id'],'original_digest':reference['digest'],
            'conversation_id':goal['conversation_id'],'coordinator_epoch':epoch+1,'intent_revision':intent+1,
            'action':args['action'],'target_member_id':args['target_member_id'],'roles':roles,'owner_principal':owner_snapshot}
        identifier = uuid.uuid4().hex
        self.store.execute('INSERT INTO room_goal_recovery_revisions VALUES (?,?,?,?,?,?,?)',
            (identifier,goal['id'],reference['id'],epoch+1,canonical(value),digest(value),self.c.clock()))
        self.store.execute('''INSERT INTO room_goal_recovery_current VALUES (?,?,?,?) ON CONFLICT(goal_id)
            DO UPDATE SET revision_id=excluded.revision_id,coordinator_epoch=excluded.coordinator_epoch''',
            (goal['id'],reference['id'],identifier,epoch+1))
        partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        # This is the original admission check, including the original approval,
        # source identity, channel evidence, project snapshots and lifetime.
        self.c.coordination.authorize(owner,partition,{**goal,'state':'active'},active=True)
        lead = roles[original['coordinator_member_id']]
        body = ('房主明确指定 '+lead['name']+' 接替此工作的主责。' if args['action']=='takeover' else
                '房主明确恢复成员 '+self.c.rooms.member(args['target_member_id'])['name']+' 的当前绑定，继续参与此原工作。')
        body += '保持原目标、原范围与原到期时间；先核对并复用全部已有操作回执，未确认结束的操作不能重跑。'
        if args['body_text'].strip():
            body += '\n'+args['body_text'].strip()
        self.c.room_runtime.send({'conversation_id':conversation['id'],'body_text':body,
            'client_message_id':'work-recovery:'+args['idempotency_key'],'idempotency_key':'work-recovery:'+args['idempotency_key'],
            'member_id':lead['member_id'],'reply_to_id':original['source_message_id'],'work_id':goal['id'],
            'attachment_ids':args['attachment_ids']},owner)
        self.store.audit(owner.actor,'room_work.'+args['action'],goal['id'],'ok',
            {'member_id':args['target_member_id'],'coordinator_epoch':epoch+1,'intent_revision':intent+1})
        return {'goal_id':goal['id']}
