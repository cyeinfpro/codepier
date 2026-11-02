"""Immutable room-member approvals for the existing Goal/work engine.

This is an authorization projection, not an execution queue. It preserves one
source and Goal while checking each member, project, target and native lease at
the original CoordinationService admission points.
"""
from __future__ import annotations

import json

from hub.collaboration.common import canonical, digest
from hub.principal import Principal, refresh_principal
from shared.util import DevError


def migrate(db):
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

    def prepare(self,member,source,principal,args):
        scopes=self.bridge.scopes(member)
        project_ids=sorted(set(args.get('project_ids') or ([args['target_project_id']] if args.get('target_project_id') else [s['project_id'] for s in scopes])))
        if not project_ids or len(project_ids)>16:
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
            rules=[]
            for project_id in project_ids:
                available=[s for s in self.bridge.scopes(current) if s['project_id']==project_id and
                    (not args.get('environment_id') or s['environment_id']==args['environment_id'])]
                if not available:
                    continue
                capabilities,_=self.bridge.effective(available[0],actor)
                if 'read' not in capabilities:
                    continue
                adapter,scope=self.bridge.adapter(current,project_id,args.get('environment_id') or None)
                policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(adapter['policy_id'],))
                if not policy or not self.bridge.authorized(policy):
                    raise DevError('ROOM_WORK_SCOPE_CHANGED','成员的原项目批准已不可用',403)
                policy_spec=json.loads(policy['spec'])
                capabilities=list(policy_spec['capabilities'])
                if args.get('capabilities'):
                    capabilities=sorted(set(capabilities)&set(args['capabilities']))
                targets=list(policy_spec['execution_targets']) if 'execute' in capabilities else []
                if args.get('execution_targets'):
                    targets=sorted(set(targets)&set(args['execution_targets']))
                if 'execute' in capabilities and not targets:
                    capabilities.remove('execute')
                if 'read' not in capabilities:
                    raise DevError('ROOM_WORK_SCOPE_DENIED','参与成员必须能读取本次目标的项目范围',403)
                for capability in capabilities:
                    self.c.coordination.require_capability(actor,capability,project_id)
                rules.append({'project_id':project_id,'environment_id':scope['environment_id'],
                    'scope_version':scope['version'],'scope_digest':self.bridge.fingerprint([scope]),
                    'legacy_dot_id':adapter['legacy_dot_id'],'policy_id':policy['id'],'policy_version':policy['version'],
                    'policy_digest':policy['digest'],'capabilities':capabilities,'execution_targets':targets,
                    'resource_snapshot':json.loads(scope['resource_snapshot']),
                    'expires_at':scope['expires_at'] if scope['lifetime_mode']=='timed' else None})
            if not rules:
                raise DevError('ROOM_WORK_SCOPE_DENIED','参与成员没有本次所选项目的工作批准',403)
            members.append({'member_id':current['id'],'name':current['name'],'grant_id':current['grant_id'],
                'binding_generation':current['binding_generation'],'projects':rules})
        projects=[]
        for project_id in project_ids:
            rules=[r for m in members for r in m['projects'] if r['project_id']==project_id]
            if not rules:
                raise DevError('ROOM_WORK_SCOPE_DENIED','所选项目没有任何当前参与成员的明确批准',403)
            projects.append({'project_id':project_id,'environment_id':rules[0]['environment_id'],
                'capabilities':sorted(set().union(*(set(r['capabilities']) for r in rules))),
                'execution_targets':sorted(set().union(*(set(r['execution_targets']) for r in rules))),
                'project_snapshot':rules[0]['resource_snapshot']['project']})
        all_caps=sorted(set().union(*(set(p['capabilities']) for p in projects)))
        all_targets=sorted(set().union(*(set(p['execution_targets']) for p in projects)))
        if args.get('capabilities') and not set(args['capabilities'])<=set(all_caps):
            raise DevError('ROOM_WORK_SCOPE_DENIED','本次能力不在任何参与成员的项目批准内',403)
        if args.get('execution_targets') and not set(args['execution_targets'])<=set(all_targets):
            raise DevError('ROOM_WORK_SCOPE_DENIED','本次执行位置不在参与成员的项目批准内',403)
        lead=next(m for m in members if m['member_id']==member['id'])
        anchor=args.get('target_project_id') or lead['projects'][0]['project_id']
        if anchor not in {r['project_id'] for r in lead['projects']}:
            raise DevError('ROOM_WORK_SCOPE_DENIED','初始工作必须使用主责成员已批准的项目',403)
        value={'schema_version':1,'conversation_id':member['conversation_id'],'source_message_id':source['id'],
            'source_version':source['version'],'source_digest':digest(json.loads(source['body']).get('body_text','')),
            'coordinator_member_id':member['id'],'coordinator_grant_id':member['grant_id'],
            'anchor_project_id':anchor,
            'project_ids':project_ids,'capabilities':all_caps,'execution_targets':all_targets,
            'participant_grant_ids':sorted({m['grant_id'] for m in members}),'members':members,'projects':projects}
        if json.loads(source['body']).get('external_origin',{}).get('native_room'):
            author=self.c.room_channels.native.author(source)
            auth=self.store.one('SELECT scope FROM room_channel_owner_authorizations WHERE id=?',(author['authorization_id'],))
            approved={r['project_id']:r for r in json.loads(auth['scope'])['projects']}
            if any(r['project_id'] not in approved or not set(r['capabilities'])<=set(approved[r['project_id']]['capabilities'])
                    or not set(r['execution_targets'])<=set(approved[r['project_id']]['execution_targets'])
                    for saved in members for r in saved['projects']):
                raise DevError('CHANNEL_OWNER_SCOPE_DENIED','本次协作超出这个微信入口明确批准的项目或能力',403)
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

    def validate(self,reference,principal,*,active=True):
        spec=self.record(reference)
        room=self.c.conversations.object(principal,spec['conversation_id'])
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
        for capability in rule['capabilities']:
            self.c.coordination.require_capability(actor,capability,rule['project_id'])
        return member,actor

    def source(self,goal,principal):
        spec=self.validate(self.reference(goal),principal,active=False)
        source=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
            (spec['source_message_id'],spec['conversation_id']))
        if (not source or source['version']!=spec['source_version']
                or digest(json.loads(source['body']).get('body_text',''))!=spec['source_digest']):
            raise DevError('ROOM_SOURCE_CHANGED','原目标输入证据已改变',409)
        self.c.chatroom.require_message_visible(principal,source)
        return source

    def owner_author(self,goal,source):
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

    def visible_projects(self,goal,principal,member_id=''):
        spec=self.record(self.reference(goal))
        if not principal.grant_id:
            return set(spec['project_ids'])
        candidates=[m for m in spec['members'] if m['grant_id']==principal.grant_id and (not member_id or m['member_id']==member_id)]
        permitted=[]
        for saved in candidates:
            member=self.c.rooms.member(saved['member_id'],spec['conversation_id'])
            if (member['status'] in {'removed','paused','invited'} or member['grant_id']!=saved['grant_id']
                    or member['binding_generation']!=saved['binding_generation']):
                continue
            current=self.bridge.scopes(member)
            visible=set()
            for rule in saved['projects']:
                scope=next((s for s in current if s['project_id']==rule['project_id'] and s['environment_id']==rule['environment_id']),None)
                if (scope and scope['version']==rule['scope_version'] and self.bridge.fingerprint([scope])==rule['scope_digest']
                        and self.c.rooms.member_scope_authorized(scope)):
                    try:
                        self.c.runtime.project(rule['project_id'],principal)
                        visible.add(rule['project_id'])
                    except DevError:
                        pass
            permitted.append(visible)
        # A shared grant without an explicit validated member credential gets
        # only common visibility; it cannot union two room identities.
        return set.intersection(*permitted) if permitted else set()

    def public_scope(self,goal,principal,member_id=''):
        spec=self.record(self.reference(goal))
        visible=self.visible_projects(goal,principal,member_id)
        return {k:spec[k] for k in ('schema_version','conversation_id','source_message_id','source_version','source_digest',
            'coordinator_member_id','coordinator_grant_id','coordinator_epoch')} | {
            'project_ids':sorted(visible),'projects':[r for r in spec['projects'] if r['project_id'] in visible],
            'members':[{'member_id':m['member_id'],'name':m['name'],'grant_id':m['grant_id'],
                'projects':[r for r in m['projects'] if r['project_id'] in visible]} for m in spec['members']],
            'work_routes':[{'member_id':m['member_id'],'grant_id':m['grant_id'],'project_id':r['project_id'],
                'environment_id':r['environment_id'],'capabilities':r['capabilities']} for m in spec['members'] for r in m['projects']],
            'visibility_limited':visible!=set(spec['project_ids'])}

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
        self.validate_rule(member,rule,spec['conversation_id'])
        return member,rule

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
        self.c.room_consumers.guard_session(raw,principal,conversation,member,require_revision=require_revision)
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
        spec=self.validate(self.reference(goal),principal)
        # The work assignment already narrowed the identity. Every permitted
        # target below belongs to this exact member/project approval.
        if item is None:
            raise DevError('ROOM_WORK_ASSIGNEE_REQUIRED','执行位置必须绑定原工作项',403)
        self.check_work(principal,goal,item)
        assignment=self.store.one('SELECT member_id FROM room_native_work_assignments WHERE work_id=?',(item['id'],))
        rules=[r for m in spec['members'] if m['grant_id']==principal.grant_id and assignment and m['member_id']==assignment['member_id']
               for r in m['projects'] if r['project_id']==project_id]
        if tool!='exec':
            if arguments.get('workspace_id') or arguments.get('target') not in (None,'','agent'):
                raise DevError('GOAL_TARGET_NOT_APPROVED','文件操作仅使用原项目 Agent',403)
            return 'agent'
        allowed=set().union(*(set(r['execution_targets']) for r in rules if 'execute' in r['capabilities']))
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

    def stop_policy(self,policy,reason):
        record=self.store.one('SELECT * FROM room_member_work_bridges WHERE policy_id=?',(policy['id'],))
        if not record:
            return
        rows=self.store.all('''SELECT w.* FROM coordination_work w JOIN room_native_work_assignments a ON a.work_id=w.id
            WHERE a.member_id=? AND w.project_id=?''',(record['member_id'],record['project_id']))
        grouped={}
        for row in rows:
            grouped.setdefault(row['goal_id'],[]).append(row['id'])
        for goal_id,identifiers in grouped.items():
            goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(goal_id,))
            if goal and self.reference(goal):
                self.c.coordination.stop_work(goal,reason,work_item_ids=identifiers)

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
