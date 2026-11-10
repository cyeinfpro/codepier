"""Immutable room-member approvals for the existing Goal/work engine.

This is an authorization projection, not an execution queue. It preserves one
source and Goal while checking each member, project, target and native lease at
the original CoordinationService admission points.
"""
from __future__ import annotations

import json

from hub.collaboration.room_input_authority import RoomInputAuthority, intersect_grants, migrate as migrate_input_authority
from hub.collaboration.common import canonical, digest
from hub.principal import Principal, refresh_principal
from shared.util import DevError


def migrate(db):
    migrate_input_authority(db)
    db.execute('''CREATE TABLE IF NOT EXISTS room_native_work_scopes(
        id TEXT PRIMARY KEY,source_message_id TEXT NOT NULL UNIQUE,conversation_id TEXT NOT NULL,
        spec TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS room_native_scope_immutable
        BEFORE UPDATE ON room_native_work_scopes BEGIN SELECT RAISE(ABORT,'room work scope is immutable'); END''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_native_work_assignments(
        work_id TEXT PRIMARY KEY REFERENCES coordination_work(id),member_id TEXT NOT NULL REFERENCES room_members(id),
        intent_revision INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL)''')


class RoomGoalScopes:
    def __init__(self,bridge):
        self.bridge,self.c,self.store=bridge,bridge.c,bridge.store
        self.input_authority = RoomInputAuthority(bridge)

    @staticmethod
    def reference(goal):
        return json.loads(goal.get('spec') or '{}').get('room_member_scope')

    def record(self,reference):
        original=self.original(reference)
        return self.c.room_goal_recovery.effective(reference,original)

    def original(self,reference):
        row=self.store.one('SELECT * FROM room_native_work_scopes WHERE id=?',(reference.get('id',''),))
        if not row or row['digest']!=reference.get('digest'):
            raise DevError('ROOM_WORK_APPROVAL_CHANGED','原房间工作范围证据已改变',403)
        spec=json.loads(row['spec'])
        if digest(spec)!=row['digest']:
            raise DevError('ROOM_WORK_APPROVAL_CHANGED','原房间工作范围证据不完整',403)
        return spec

    def work_rule(self,goal,item):
        """Resolve the assignment's saved rule without requiring it to be live.

        Revocation and original receipts must still identify a stopped work's
        environment. A later takeover must not reinterpret an earlier work
        using the new coordinator's role or the Goal's anchor partition.
        """
        reference=self.reference(goal)
        if not reference:
            return None
        assignment=self.store.one('SELECT * FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
        if not assignment or item['goal_id']!=goal['id']:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','原工作缺少稳定成员分配证据',403)
        original=self.original(reference)
        revision=self.store.one('''SELECT * FROM room_goal_recovery_revisions
            WHERE goal_id=? AND json_extract(spec,'$.intent_revision')<=?
            ORDER BY coordinator_epoch DESC LIMIT 1''',(goal['id'],assignment['intent_revision']))
        members=original['members']
        if revision:
            value=json.loads(revision['spec'])
            if (digest(value)!=revision['digest'] or value['scope_id']!=reference['id']
                    or value['original_digest']!=reference['digest'] or value['goal_id']!=goal['id']
                    or value['coordinator_epoch']!=revision['coordinator_epoch']):
                raise DevError('ROOM_RECOVERY_EVIDENCE_CHANGED','原工作恢复证据已改变',403)
            members=self.c.room_goal_recovery.merge_roles(value['roles'])
        rules=[rule for member in members if member['member_id']==assignment['member_id']
               for rule in member['projects'] if rule['project_id']==item['project_id']]
        if len(rules)!=1:
            raise DevError('ROOM_WORK_ENVIRONMENT_REQUIRED','原工作须有唯一的成员、项目与环境证据',409)
        return rules[0]

    def work_environment(self,goal,item):
        rule=self.work_rule(goal,item)
        if rule is not None:
            return rule['environment_id']
        partition=self.store.one('SELECT environment_id FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        if not partition:
            raise DevError('ROOM_WORK_PARTITION_CHANGED','原工作项目分区证据不存在',403)
        return partition['environment_id']

    def work_partition(self,goal,item):
        rule=self.work_rule(goal,item)
        if rule is None:
            return self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        # The saved private adapter retains its original partition after a
        # project is detached. This preserves receipt provenance, not access.
        partition=self.store.one('''SELECT r.* FROM collaboration_rooms r
            JOIN collaboration_dots d ON d.room_id=r.id WHERE d.id=?
            AND r.project_id=? AND r.environment_id=?''',
            (rule['legacy_dot_id'],item['project_id'],rule['environment_id']))
        if not partition:
            raise DevError('ROOM_WORK_PARTITION_CHANGED','原工作项目分区证据不存在',403)
        return partition

    def prepare(self,member,source,principal,args):
        proof = self.input_authority.proof(source, member)
        coordinator_rules = self.input_authority.rules(source, member, proof=proof)
        external = json.loads(source['body']).get('external_origin', {}).get('native_room')
        denied_code = 'CHANNEL_OWNER_SCOPE_DENIED' if external else 'ROOM_WORK_SCOPE_DENIED'
        available_defaults = {(scope['project_id'], scope['environment_id'])
                              for scope in self.bridge.setup_scopes(member)}
        defaults = [rule['project_id'] for key, rule in coordinator_rules.items() if key in available_defaults]
        project_ids = sorted(set(args.get('project_ids') or ([args['target_project_id']] if args.get('target_project_id') else defaults)))
        if external:
            source_rules = self.input_authority.rules(source, proof=proof)
            source_projects = {pid for pid, env in source_rules if not args.get('environment_id') or env == args['environment_id']}
            if not set(project_ids) <= source_projects:
                raise DevError(denied_code, '本次协作超出这个微信入口明确批准的项目或环境', 403)
        if not project_ids:
            raise DevError('ROOM_WORK_PROJECT_REQUIRED','请选择此成员已批准的工作项目',409)
        if args.get('target_project_id') and args['target_project_id'] not in project_ids:
            raise DevError('ROOM_WORK_PROJECT_REQUIRED','初始工作项目必须在本次项目范围内',409)
        member_ids=list(dict.fromkeys([member['id'],*args.get('participant_member_ids',[])]))
        members=[]
        for identifier in member_ids:
            current=self.c.rooms.member(identifier,member['conversation_id'])
            if current['status'] in {'removed','paused','invited'} or not current['grant_id']:
                raise DevError('ROOM_WORK_MEMBER_UNAVAILABLE','工作参与成员尚未完成当前连接',409)
            actor=self.c.rooms.principal(current)
            self.c.chatroom.require_message_visible(actor,source)
            if not current['probe_replied_at']:
                raise DevError('ROOM_PROBE_REQUIRED','参与成员需要完成真实连接检查',409)
            self.c.room_consumers.binding(self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(member['conversation_id'],)),current,actor)
            approved = (coordinator_rules if current['id'] == member['id'] else
                        self.input_authority.rules(source, current, proof=proof))
            rules=[]
            scopes = self.bridge.setup_scopes(current)
            for project_id in project_ids:
                available=[s for s in scopes if s['project_id']==project_id and
                    (not args.get('environment_id') or s['environment_id']==args['environment_id']) and
                    (s['project_id'],s['environment_id']) in approved]
                if not available:
                    continue
                if len(available)!=1:
                    raise DevError('ROOM_WORK_ENVIRONMENT_REQUIRED','同一项目有多个已批准环境，请明确选择本次环境',409)
                capabilities,_=self.bridge.effective(available[0],actor)
                if 'read' not in capabilities:
                    continue
                adapter,scope=self.bridge.adapter(current,project_id,available[0]['environment_id'])
                policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(adapter['policy_id'],))
                if not policy or not self.bridge.authorized(policy):
                    raise DevError('ROOM_WORK_SCOPE_CHANGED','成员的原项目批准已不可用',403)
                policy_spec=json.loads(policy['spec'])
                input_rule = approved[(project_id, scope['environment_id'])]
                requested = {**input_rule,
                    'capabilities': args.get('capabilities') or input_rule['capabilities'],
                    'execution_targets': args.get('execution_targets') or input_rule['execution_targets']}
                effective = intersect_grants(self.c.clock(), input_rule, requested,
                    {**input_rule, 'capabilities': policy_spec['capabilities'],
                     'execution_targets': policy_spec['execution_targets']})
                if not effective:
                    continue
                capabilities, targets = effective['capabilities'], effective['execution_targets']
                for capability in capabilities:
                    self.c.coordination.require_capability(actor,capability,project_id)
                rules.append({'project_id':project_id,'environment_id':scope['environment_id'],
                    'scope_version':scope['version'],'scope_digest':self.bridge.fingerprint([scope]),
                    'legacy_dot_id':adapter['legacy_dot_id'],'policy_id':policy['id'],'policy_version':policy['version'],
                    'policy_digest':policy['digest'],'capabilities':capabilities,'execution_targets':targets,
                    'resource_snapshot':json.loads(scope['resource_snapshot']),
                    'expires_at':effective['expires_at']})
            if not rules:
                raise DevError('ROOM_WORK_SCOPE_DENIED','参与成员没有本次所选项目的工作批准',403)
            members.append({'member_id':current['id'],'name':current['name'],'grant_id':current['grant_id'],
                'binding_generation':current['binding_generation'],'projects':rules})
        projects=[]
        for project_id in project_ids:
            rules=[r for m in members for r in m['projects'] if r['project_id']==project_id]
            if not rules:
                raise DevError('ROOM_WORK_SCOPE_DENIED','所选项目没有任何当前参与成员的明确批准',403)
            if len({rule['environment_id'] for rule in rules})!=1:
                raise DevError('ROOM_WORK_ENVIRONMENT_REQUIRED','参与成员对同一项目选择了不同环境，请明确本次环境与参与范围',409)
            projects.append({'project_id':project_id,'environment_id':rules[0]['environment_id'],
                'capabilities':sorted(set().union(*(set(r['capabilities']) for r in rules))),
                'execution_targets':sorted(set().union(*(set(r['execution_targets']) for r in rules))),
                'project_snapshot':rules[0]['resource_snapshot']['project']})
        all_caps=sorted(set().union(*(set(p['capabilities']) for p in projects)))
        all_targets=sorted(set().union(*(set(p['execution_targets']) for p in projects)))
        if args.get('capabilities') or args.get('execution_targets'):
            requested_caps = set(args.get('capabilities', []))
            requested_targets = set(args.get('execution_targets', []))
            requested_project = args.get('target_project_id')
            if not any((not requested_project or rule['project_id'] == requested_project)
                       and requested_caps <= set(rule['capabilities'])
                       and requested_targets <= set(rule['execution_targets'])
                       for saved in members for rule in saved['projects']):
                raise DevError(denied_code,'本次能力和执行位置必须属于同一个已批准的成员、项目及环境',403)
        lead=next((m for m in members if m['member_id']==member['id']),None)
        if lead is None:
            raise DevError('ROOM_MEMBER_NOT_IN_APPROVAL','主责成员不在原工作批准名单中',403)
        anchor=args.get('target_project_id') or lead['projects'][0]['project_id']
        if anchor not in {r['project_id'] for r in lead['projects']}:
            raise DevError('ROOM_WORK_SCOPE_DENIED','初始工作必须使用主责成员已批准的项目',403)
        value={'schema_version':1,'conversation_id':member['conversation_id'],'source_message_id':source['id'],
            'source_version':source['version'],'source_digest':digest(json.loads(source['body']).get('body_text','')),
            'coordinator_member_id':member['id'],'coordinator_grant_id':member['grant_id'],
            'anchor_project_id':anchor,
            'project_ids':project_ids,'capabilities':all_caps,'execution_targets':all_targets,
            'participant_grant_ids':sorted({m['grant_id'] for m in members}),'members':members,'projects':projects}
        identifier='roomscope_'+digest(value)[:32]
        prior=self.store.one('SELECT * FROM room_native_work_scopes WHERE source_message_id=?',(source['id'],))
        if prior and prior['digest']!=digest(value):
            raise DevError('ROOM_SOURCE_ALREADY_USED','原输入已形成一个工作范围，新增范围需本人新的明确输入',409)
        self.store.execute('INSERT OR IGNORE INTO room_native_work_scopes VALUES (?,?,?,?,?,?)',
            (identifier,source['id'],member['conversation_id'],canonical(value),digest(value),self.c.clock()))
        reference={'id':identifier,'digest':digest(value)}
        self.validate(reference,principal)
        for saved in value['members']:
            for rule in saved['projects']:
                self.validate_rule(saved,rule,value['conversation_id'])
        return reference,value

    def validate(self,reference,principal,*,active=True,include_archived=False):
        if include_archived and (active or principal.grant_id):
            raise DevError('ROOM_HISTORY_OWNER_ONLY','归档工作历史只允许本人只读查看',403)
        spec=self.record(reference)
        room=self.c.conversations.object(principal,spec['conversation_id'],include_archived=include_archived)
        if active:
            self.c.rooms.require_native(room)
        if principal.grant_id and principal.grant_id not in spec['participant_grant_ids']:
            raise DevError('ROOM_WORK_MEMBER_REQUIRED','当前连接不在原工作成员范围内',403)
        if not active:
            return spec
        if room['state']!='active':
            raise DevError('ROOM_WORK_INACTIVE','原房间已暂停，不能开始新工作',409)
        self.c.room_goal_recovery.authority(reference)
        return spec

    def validate_rule(self,saved,rule,conversation_id):
        """Only the step's immutable member/project rule is live authority."""
        if rule.get('expires_at') is not None and rule['expires_at']<=self.c.clock():
            raise DevError('ROOM_WORK_APPROVAL_EXPIRED','原成员工作批准已到期，恢复不能自动延长',403)
        member=self.c.rooms.member(saved['member_id'],conversation_id)
        if (member['status'] in {'removed','paused','invited','legacy'} or member['grant_id']!=saved['grant_id']
                or member['binding_generation']!=saved['binding_generation']):
            raise DevError('ROOM_WORK_MEMBER_CHANGED','原工作参与成员的绑定已改变',403)
        actor=self.c.rooms.principal(member)
        self.c.rooms.member_scope(actor,conversation_id,member['id'],member['binding_generation'])
        adapter,scope=self.bridge.adapter(member,rule['project_id'],rule['environment_id'])
        policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(rule['policy_id'],))
        if (scope['version']!=rule['scope_version'] or self.bridge.fingerprint([scope])!=rule['scope_digest']
                or adapter['legacy_dot_id']!=rule['legacy_dot_id'] or not policy
                or policy['version']!=rule['policy_version'] or policy['digest']!=rule['policy_digest']
                or not self.bridge.authorized(policy)):
            raise DevError('ROOM_WORK_SCOPE_CHANGED','原工作项目范围已移出、撤销或重新批准',403)
        self.require_work_partition(policy)
        for capability in rule['capabilities']:
            self.c.coordination.require_capability(actor,capability,rule['project_id'])
        return member,actor

    def source(self,goal,principal,*,include_archived=False):
        spec=self.validate(self.reference(goal),principal,active=False,include_archived=include_archived)
        source=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
            (spec['source_message_id'],spec['conversation_id']))
        if (not source or source['version']!=spec['source_version']
                or digest(json.loads(source['body']).get('body_text',''))!=spec['source_digest']):
            raise DevError('ROOM_SOURCE_CHANGED','原目标输入证据已改变',409)
        self.c.chatroom.require_message_visible(principal,source)
        return source

    def owner_author(self,goal,source):
        spec = self.record(self.reference(goal))
        if source['id'] == spec['source_message_id']:
            proof = self.input_authority.proof(source, goal=goal)
            if proof:
                rules = self.input_authority.rules(source, goal=goal, proof=proof)
                if not any((r['project_id'], r['environment_id']) in rules for r in spec['projects']):
                    code = 'CHANNEL_OWNER_SCOPE_CHANGED' if proof['channel'] else 'ROOM_WORK_SCOPE_CHANGED'
                    raise DevError(code, '原工作没有仍连续有效的项目与环境授权', 403)
                return self.input_authority.verified(source, proof)
        if not json.loads(source['body']).get('external_origin',{}).get('native_room'):
            return self.c.channel_owner.author(source)
        spec=self.record(self.reference(goal))
        active=set()
        for saved in spec['members']:
            for rule in saved['projects']:
                try:
                    self.validate_rule(saved,rule,spec['conversation_id'])
                    active.add(rule['project_id'])
                except DevError:
                    continue
        return self.c.room_channels.native.author(source,project_ids=active,goal=goal)

    def participant(self,spec,grant_id,member_id=''):
        saved=next((m for m in spec['members'] if m['grant_id']==grant_id and (not member_id or m['member_id']==member_id)),None)
        if not saved:
            raise DevError('ROOM_WORK_MEMBER_REQUIRED','当前连接不在原工作成员范围内',403)
        member=self.c.rooms.member(saved['member_id'],spec['conversation_id'])
        if member['grant_id']!=grant_id or member['binding_generation']!=saved['binding_generation']:
            raise DevError('ROOM_WORK_MEMBER_CHANGED','原成员绑定已改变',403)
        return self.c.rooms.principal(member)

    @staticmethod
    def scope_key(rule):
        return rule['project_id'], rule['environment_id']

    def work_scope_key(self, goal, item):
        return item['project_id'], self.work_environment(goal, item)

    def artifact_scope_key(self, goal, work_item_id):
        return self.work_scope_key(goal, self.c.coordination.work(goal, work_item_id))

    def work_is_visible(self, goal, item, scope_keys):
        try:
            return self.work_scope_key(goal, item) in scope_keys
        except DevError:
            # Unverifiable historical assignments cannot expose work payloads.
            return False

    def visible_scope_keys(self,goal,principal,member_id=''):
        spec=self.record(self.reference(goal))
        if not principal.grant_id:
            return {self.scope_key(rule) for rule in spec['projects']}
        candidates=[m for m in spec['members'] if m['grant_id']==principal.grant_id and (not member_id or m['member_id']==member_id)]
        source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (spec['source_message_id'],))
        proof = self.input_authority.proof(source, goal=goal) if source else None
        permitted=[]
        for saved in candidates:
            member=self.c.rooms.member(saved['member_id'],spec['conversation_id'])
            if (member['status'] in {'removed','paused','invited','legacy'} or member['grant_id']!=saved['grant_id']
                    or member['binding_generation']!=saved['binding_generation']):
                continue
            current={self.scope_key(row): row for row in self.bridge.scopes(member)}
            readable=self.c.rooms.read_scope_keys(member,principal)
            visible=set()
            for rule in saved['projects']:
                key=self.scope_key(rule)
                scope=current.get(key)
                if key not in readable or not scope:
                    continue
                # Legacy work retains its original approval boundary; new work
                # uses live room read access independently of execution consent.
                original = scope['version'] == rule['scope_version'] and self.bridge.fingerprint([scope]) == rule['scope_digest']
                if proof or original:
                    visible.add(key)
            permitted.append(visible)
        # Shared grants without a selected member get the intersection.
        return set.intersection(*permitted) if permitted else set()

    def visible_projects(self,goal,principal,member_id=''):
        return {project for project,_ in self.visible_scope_keys(goal,principal,member_id)}

    def public_scope(self,goal,principal,member_id=''):
        spec=self.record(self.reference(goal))
        visible=self.visible_scope_keys(goal,principal,member_id)
        return {k:spec[k] for k in ('schema_version','conversation_id','source_message_id','source_version','source_digest',
            'coordinator_member_id','coordinator_grant_id','coordinator_epoch')} | {
            'project_ids':sorted({project for project,_ in visible}),
            'projects':[r for r in spec['projects'] if self.scope_key(r) in visible],
            'members':[{'member_id':m['member_id'],'name':m['name'],'grant_id':m['grant_id'],
                'projects':[r for r in m['projects'] if self.scope_key(r) in visible]} for m in spec['members']],
            'work_routes':[{'member_id':m['member_id'],'grant_id':m['grant_id'],'project_id':r['project_id'],
                'environment_id':r['environment_id'],'capabilities':r['capabilities']}
                for m in spec['members'] for r in m['projects'] if self.scope_key(r) in visible],
            'visibility_limited':visible!={self.scope_key(rule) for rule in spec['projects']}}

    def project_capabilities(self,goal,project_id):
        reference=self.reference(goal)
        if not reference:
            return json.loads(goal['spec'])['capabilities']
        rules=self.record(reference)['projects']
        return next((r['capabilities'] for r in rules if r['project_id']==project_id),[])

    def check_work(self,principal,goal,item,member_id=''):
        reference=self.reference(goal)
        if not reference:
            return None
        spec=self.validate(reference,principal)
        if not member_id and item.get('id'):
            assignment=self.store.one('SELECT member_id FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
            member_id=assignment['member_id'] if assignment else ''
        candidates=[m for m in spec['members'] if m['grant_id']==principal.grant_id and (not member_id or m['member_id']==member_id)]
        if len(candidates)!=1:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','共享连接需要明确此工作的稳定成员',403)
        member=candidates[0]
        rule=next((r for r in member['projects'] if r['project_id']==item['project_id']),None)
        capabilities=json.loads(item['required_capabilities'])
        if not rule or not set(capabilities)<=set(rule['capabilities']):
            raise DevError('ROOM_WORK_SCOPE_DENIED','工作项能力超过此成员对此项目的原批准',403)
        current = self.c.rooms.member(member['member_id'], spec['conversation_id'])
        source = self.source(goal, principal)
        proof = self.input_authority.proof(source, current, goal)
        if not proof:
            # Existing Goals predate the ingress snapshot. Preserve their
            # original immutable proof and strict authorization semantics.
            self.validate_rule(member, rule, spec['conversation_id'])
            if json.loads(source['body']).get('external_origin', {}).get('native_room'):
                self.c.room_channels.native.author(source, project_ids={item['project_id']}, goal=goal)
            return member, rule
        if current['binding_generation'] != member['binding_generation']:
            raise DevError('ROOM_WORK_MEMBER_CHANGED', '原工作参与成员的绑定已改变', 403)
        effective = self.continuous_rule(goal, source, current, rule, proof)
        if not effective or not set(capabilities) <= set(effective['capabilities']):
            raise DevError('ROOM_WORK_SCOPE_CHANGED', '原工作步骤的项目、能力或执行位置已失去连续授权', 403)
        actor = self.c.rooms.principal(current)
        self.c.rooms.member_scope(actor, spec['conversation_id'], current['id'], current['binding_generation'])
        adapter, _ = self.bridge.adapter(current, rule['project_id'], rule['environment_id'])
        policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (adapter['policy_id'],))
        if not policy or not self.bridge.authorized(policy):
            raise DevError('ROOM_WORK_SCOPE_CHANGED', '原工作步骤的当前项目授权已不可用', 403)
        self.require_work_partition(policy)
        return member, effective

    def require_work_partition(self, policy):
        partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(policy['room_id'],))
        if not partition:
            raise DevError('ROOM_WORK_PARTITION_CHANGED','原工作项目分区证据不存在',403)
        self.c.live_room(partition)

    def continuous_rules(self, goal, source, current, proof, *, keys):
        if current['status'] in {'removed','paused','invited','legacy'}:
            return {}
        revision = self.c.room_goal_recovery.revision(self.reference(goal))
        recovered = bool(revision and revision.get('action') in {'restore_binding', 'takeover'})
        return self.input_authority.rules(source, current, goal=goal, proof=proof, recovered=recovered, keys=keys)

    def continuous_rule(self, goal, source, current, rule, proof):
        key = rule['project_id'], rule['environment_id']
        rules = self.continuous_rules(goal, source, current, proof, keys={key})
        return intersect_grants(self.c.clock(), rule, rules.get(key))

    def require_delivery_projects(self, goal, source, project_ids):
        proof = self.input_authority.proof(source, goal=goal)
        if not proof:
            return self.c.room_channels.native.author(source, project_ids=project_ids, goal=goal)
        author = self.owner_author(goal, source)
        reference = self.reference(goal)
        spec = self.record(reference)
        for saved in spec['members']:
            relevant = [rule for rule in saved['projects'] if rule['project_id'] in project_ids]
            if not relevant:
                continue
            current = self.c.rooms.member(saved['member_id'], spec['conversation_id'])
            keys = {(rule['project_id'], rule['environment_id']) for rule in relevant}
            live = self.continuous_rules(goal, source, current, proof, keys=keys)
            for rule in relevant:
                effective = intersect_grants(self.c.clock(), rule, live.get((rule['project_id'], rule['environment_id'])))
                if (not effective or not set(rule['capabilities']) <= set(effective['capabilities'])
                        or not set(rule['execution_targets']) <= set(effective['execution_targets'])):
                    raise DevError('ROOM_WORK_SCOPE_CHANGED', '原工作回复的项目权限已经改变', 403)
        return author

    def assign(self,goal,item,member_id=''):
        reference=self.reference(goal)
        if not reference:
            return
        spec=self.record(reference)
        candidates=[m for m in spec['members'] if m['grant_id']==item['assignee_grant_id'] and (not member_id or m['member_id']==member_id)]
        if len(candidates)!=1:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','请明确选择此工作项的成员',403)
        member=self.c.rooms.member(candidates[0]['member_id'],spec['conversation_id'])
        self.check_work(self.c.rooms.principal(member),goal,item,member['id'])
        intent=self.c.room_runtime.intent_revision(goal['id'])
        self.store.execute('''INSERT INTO room_native_work_assignments VALUES (?,?,?,?)
            ON CONFLICT(work_id) DO UPDATE SET member_id=excluded.member_id,intent_revision=excluded.intent_revision''',
            (item['id'],member['id'],intent,self.c.clock()))

    def guard(self,raw,principal,goal,item=None,*,action='',require_revision=True):
        reference=self.reference(goal)
        if not reference:
            return None
        spec=self.validate(reference,principal)
        self.c.room_goal_recovery.require_epoch(goal,raw.get('coordinator_epoch'))
        revision=self.c.room_runtime.intent_revision(goal['id'])
        if raw.get('expected_intent_revision')!=revision:
            raise DevError('ROOM_INTENT_CHANGED','用户意图已改变，请读取原工作的当前修订',409,intent_revision=revision)
        if not principal.grant_id:
            self.c.owner(principal)
            return None
        session=self.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',(raw.get('consumer_session_id',''),))
        member=self.c.rooms.member(session['member_id'],goal['conversation_id']) if session and session.get('member_id') else None
        if not member or member['id'] not in {m['member_id'] for m in spec['members']}:
            raise DevError('ROOM_WORK_MEMBER_REQUIRED','工作需要原参与成员的真实消费回合',403)
        conversation=self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(goal['conversation_id'],))
        self.c.room_consumers.guard_session(raw,principal,conversation,member,require_revision=require_revision,action=action)
        if action in {'create','assign','claim','execute','takeover'} and self.store.one(
                'SELECT 1 FROM room_goal_publications WHERE goal_id=? AND intent_revision=?',(goal['id'],revision)):
            raise DevError('ROOM_WORK_ALREADY_PUBLISHED','本次原工作已发表最终汇总，新步骤需要本人新的明确输入',409)
        if action in {'create','assign','publish','takeover'} and member['id']!=spec['coordinator_member_id']:
            raise DevError('ROOM_WORK_LEAD_REQUIRED','只有本次工作的原主责成员可调整工作计划',403)
        if item:
            assignment=self.store.one('SELECT * FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
            if not assignment or assignment['member_id']!=member['id']:
                raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','原工作项分配给另一位成员',403)
            if assignment['intent_revision']!=revision:
                raise DevError('ROOM_INTENT_CHANGED','原工作项属于旧意图，请主责重新核对',409)
            self.check_work(principal,goal,item,member['id'])
        return session

    def target(self,principal,goal,tool,arguments,project_id,*,item=None):
        # The work assignment already narrowed the identity. Every permitted
        # target below belongs to this exact member/project approval.
        if item is None:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','执行位置必须绑定原工作项',403)
        _, rule = self.check_work(principal, goal, item)
        if rule['project_id'] != project_id:
            raise DevError('ROOM_WORK_SCOPE_DENIED', '执行位置必须对应当前工作项项目', 403)
        if tool!='exec':
            if arguments.get('workspace_id') or arguments.get('target') not in (None,'','agent'):
                raise DevError('GOAL_TARGET_NOT_APPROVED','文件操作仅使用原项目 Agent',403)
            return 'agent'
        allowed = set(rule['execution_targets']) if 'execute' in rule['capabilities'] else set()
        targets={'agent' if t=='project_agent' else t for t in allowed}
        if len(targets)!=1 and not arguments.get('target'):
            raise DevError('DELEGATION_TARGET_REQUIRED','此项目有多个已批准执行位置，请明确选择',422)
        target=arguments.get('target') or next(iter(targets),'')
        if arguments.get('workspace_id') or target not in targets:
            raise DevError('GOAL_TARGET_NOT_APPROVED','执行位置超出此成员对此项目的原批准',403)
        return target

    def emit(self,goal,item):
        reference=self.reference(goal)
        if not reference:
            return False
        assignment=self.store.one('SELECT * FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
        if not assignment:
            return True  # The initial immutable approval is still being bound.
        member=self.c.rooms.member(assignment['member_id'],goal['conversation_id'])
        self.c.room_consumers.enqueue_ref(member,'work_ready',item['id'],item['version'],
            {'goal_id':goal['id'],'approval_id':item['approval_id'],'work_item_id':item['id']})
        self.c.rooms.change(goal['conversation_id'],'work',goal['id'],goal['version'])
        return True

    def annotate(self,goal,item,body):
        if not self.reference(goal):
            return body
        assignment=self.store.one('SELECT * FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
        if not assignment:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','原工作没有稳定成员分配',403)
        member=self.c.rooms.member(assignment['member_id'],goal['conversation_id'])
        body.update(sender_member_id=member['id'],sender_name=member['name'],intent_revision=assignment['intent_revision'])
        return body

    def operation_guard(self,goal,item):
        if not self.reference(goal):
            return False
        assignment=self.store.one('SELECT * FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
        if not assignment or assignment['intent_revision']!=self.c.room_runtime.intent_revision(goal['id']):
            raise DevError('ROOM_INTENT_CHANGED','旧用户意图不能投递新的操作',409)
        member=self.c.rooms.member(assignment['member_id'],goal['conversation_id'])
        self.check_work(self.c.rooms.principal(member),goal,item,member['id'])
        return True

    def check_dispatch_target(self, goal, item, operation, principal):
        if not self.reference(goal) or operation['tool'] != 'exec':
            return
        request = json.loads(self.store.decrypt(operation['payload']))
        self.target(principal, goal, 'exec', request['args'], item['project_id'], item=item)

    def work_rule_continues(self, goal, item, current, rule):
        spec = self.record(self.reference(goal))
        source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (spec['source_message_id'],))
        proof = self.input_authority.proof(source, current, goal) if source else None
        if not proof:
            return False
        try:
            live = self.continuous_rule(goal, source, current, rule, proof)
            if not live or not set(json.loads(item['required_capabilities'])) <= set(live['capabilities']):
                return False
            allowed_targets = {'agent' if t == 'project_agent' else t for t in live['execution_targets']}
            pending_execs = self.store.all('''SELECT o.payload FROM operations o
                JOIN coordination_operations c ON c.operation_id=o.id WHERE c.work_item_id=?
                AND o.tool='exec' AND o.state IN ('queued','running','reconnecting','cancelling','unknown')''', (item['id'],))
            return all(json.loads(self.store.decrypt(op['payload']))['args']['target'] in allowed_targets for op in pending_execs)
        except DevError:
            return False

    def stop_revoked_inputs(self, conversation_id):
        after = ''
        while True:
            rows = self.store.all('''SELECT w.*, a.member_id FROM coordination_work w
                JOIN coordination_goals g ON g.id=w.goal_id
                JOIN room_native_work_assignments a ON a.work_id=w.id
                WHERE g.conversation_id=? AND g.state='active' AND w.state IN ('queued','leased','running')
                AND w.id>? ORDER BY w.id LIMIT 64''', (conversation_id, after))
            if not rows:
                return
            for row in rows:
                goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (row['goal_id'],))
                if not goal or not self.reference(goal):
                    continue
                rule = self.work_rule(goal, row)
                member = self.c.rooms.member(row['member_id'], conversation_id)
                spec = self.record(self.reference(goal))
                source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (spec['source_message_id'],))
                if source and self.input_authority.proof(source, member, goal) and not self.work_rule_continues(goal, row, member, rule):
                    self.c.coordination.stop_work(goal, 'ROOM_INPUT_APPROVAL_REVOKED', work_item_ids=[row['id']])
                    self.c.rooms.change(conversation_id, 'work', goal['id'], goal['version'])
            after = rows[-1]['id']

    def stop_policy(self,policy,reason):
        record=self.store.one('SELECT * FROM room_member_work_bridges WHERE policy_id=?',(policy['id'],))
        if not record:
            return
        rows=self.store.all('''SELECT w.* FROM coordination_work w JOIN room_native_work_assignments a ON a.work_id=w.id
            WHERE a.member_id=? AND w.project_id=?''',(record['member_id'],record['project_id']))
        grouped={}
        goals={}
        for row in rows:
            if row['goal_id'] not in goals:
                goals[row['goal_id']]=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(row['goal_id'],))
            goal=goals[row['goal_id']]
            if not goal or not self.reference(goal):
                continue
            # A member may use the same project in several environments.
            # Stop only work whose immutable assignment names this adapter,
            # never another environment's original approval for that member.
            rule=self.work_rule(goal,row)
            if rule['environment_id'] != record['environment_id']:
                continue
            member = self.c.rooms.member(record['member_id'], goal['conversation_id'])
            if rule['legacy_dot_id'] != record['legacy_dot_id']:
                # Snapshot-governed work can survive a compatible adapter
                # rebuild. Future lifecycle actions still stop that same
                # member/project/environment without touching another role.
                if record['state'] != 'active' or record['binding_generation'] != member['binding_generation']:
                    continue
                spec = self.record(self.reference(goal))
                source = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (spec['source_message_id'],))
                if not source or not self.input_authority.proof(source, member, goal):
                    continue
            if reason == 'ROOM_MEMBER_SCOPE_REVISED' and self.work_rule_continues(goal, row, member, rule):
                continue
            grouped.setdefault(row['goal_id'],[]).append(row['id'])
        for goal_id,identifiers in grouped.items():
            self.c.coordination.stop_work(goals[goal_id],reason,work_item_ids=identifiers)

    def result_ready(self,goal,item):
        reference=self.reference(goal)
        if not reference:
            return False
        spec=self.record(reference)
        member=self.c.rooms.member(spec['coordinator_member_id'],goal['conversation_id'])
        self.c.room_consumers.enqueue_ref(member,'result_ready',item['id'],item['version'],
            {'goal_id':goal['id'],'work_item_id':item['id']})
        self.c.rooms.change(goal['conversation_id'],'work',goal['id'],goal['version'])
        return True
