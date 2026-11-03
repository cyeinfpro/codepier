"""Stable room-member identities on the original Team handoff journal."""
from __future__ import annotations

import json
import uuid

from hub.collaboration.common import canonical, digest, redact, validate
from shared.room_runtime_contracts import RoomWorkHandoff
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_goal_handoff_members(
        handoff_id TEXT PRIMARY KEY REFERENCES team_handoffs(id),
        author_member_id TEXT NOT NULL REFERENCES room_members(id),
        recipient_member_id TEXT NOT NULL REFERENCES room_members(id),
        author_generation INTEGER NOT NULL,recipient_generation INTEGER NOT NULL,
        provenance_project_ids TEXT NOT NULL)''')


class RoomGoalHandoffs:
    def __init__(self, collaboration):
        self.c,self.store = collaboration,collaboration.store

    def query(self, raw, principal):
        from shared.room_runtime_contracts import RoomWorkHandoffRead
        args=validate(RoomWorkHandoffRead,raw)
        with self.store.transaction(immediate=False):
            actor,_,member=self.c.rooms.member_scope(principal,args['conversation_id'],args['member_id'],
                args['binding_generation'],args['connection_proof'],require_proof=True)
            side=self.store.one('SELECT author_member_id FROM room_goal_handoff_members WHERE handoff_id=?',(args['handoff_id'],))
            return {'handoff':self.read(member,args['handoff_id'],actor,
                author=bool(side and side['author_member_id']==member['id'])),
                'read_only':True,'read_is_consumption':False,'grants_authority':False}

    def saved_member(self, spec, identifier):
        saved = next((m for m in spec['members'] if m['member_id']==identifier),None)
        if not saved:
            raise DevError('ROOM_WORK_MEMBER_REQUIRED','交接成员不在当前原工作范围内',403)
        current = self.c.rooms.member(identifier,spec['conversation_id'])
        actor = self.c.rooms.principal(current)
        self.c.room_consumers.binding(self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(spec['conversation_id'],)),current,actor)
        valid = []
        for rule in saved['projects']:
            try:
                self.c.room_work_bridge.native.validate_rule(saved,rule,spec['conversation_id'])
                valid.append(rule)
            except DevError:
                continue
        if not valid:
            raise DevError('ROOM_WORK_SCOPE_CHANGED','交接成员没有仍有效的原工作项目范围',403)
        return saved,current,valid

    def handoff(self, raw, principal):
        args = validate(RoomWorkHandoff,raw)
        with self.store.transaction():
            member = self.c.room_consumers.authorize_member(args,principal,require_revision=True)
            goal = self.c.room_runtime.goal(args['goal_id'])
            if goal['conversation_id'] != member['conversation_id']:
                raise DevError('ROOM_WORK_NOT_FOUND','当前房间没有这个原工作',404)
            goal = {**goal,'_reader_member_id':member['id']}
            native = self.c.room_work_bridge.native
            if not native.reference(goal):
                raise DevError('ROOM_WORK_MEMBER_REQUIRED','此交接需要房间成员的原工作',403)
            partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
            self.c.coordination.authorize(principal,partition,goal,active=True)
            item = self.c.coordination.work(goal,args['work_item_id']) if args['work_item_id'] else None
            native.guard(args,principal,goal,item,action='handoff')
            spec = native.record(native.reference(goal))
            author,_,author_rules = self.saved_member(spec,member['id'])
            recipient,target,recipient_rules = self.saved_member(spec,args['recipient_member_id'])
            if recipient['member_id']==author['member_id']:
                raise DevError('TEAM_HANDOFF_RECIPIENT','交接需要另一位已批准的工作成员',403)
            refs = self.c.coordination.teams.artifacts(goal,args['artifact_refs'])
            revision = self.c.room_runtime.intent_revision(goal['id'])
            epoch = self.c.room_goal_recovery.require_epoch(goal,args['coordinator_epoch'])
            provenance = sorted({r['project_id'] for r in author_rules})
            if args['causation_id']:
                ancestor = self.store.one('''SELECT h.*,m.author_member_id,m.recipient_member_id FROM team_handoffs h
                    JOIN room_goal_handoff_members m ON m.handoff_id=h.id WHERE h.id=? AND h.goal_id=?
                    AND h.intent_revision=? AND h.coordinator_epoch=?''',
                    (args['causation_id'],goal['id'],revision,epoch))
                if (not ancestor or ancestor['recipient_member_id']!=member['id']
                        or args['hop_count']!=ancestor['hop_count']+1):
                    raise DevError('TEAM_HANDOFF_CAUSATION','交接链必须由当前世代的上一跳收件人继续',409)
                visited = {member['id']}
                while ancestor:
                    visited.add(ancestor['author_member_id'])
                    ancestor = self.store.one('''SELECT h.*,m.author_member_id,m.recipient_member_id FROM team_handoffs h
                        JOIN room_goal_handoff_members m ON m.handoff_id=h.id WHERE h.id=?''',
                        (ancestor['causation_id'],)) if ancestor['causation_id'] else None
                if target['id'] in visited:
                    raise DevError('TEAM_HANDOFF_LOOP','交接形成循环，请让主责作明确决定',409)
            elif args['hop_count']!=0:
                raise DevError('TEAM_HANDOFF_CAUSATION','第一跳必须从零开始',409)

            def save():
                fingerprint = digest([member['id'],target['id'],epoch,args['work_item_id'],
                    args['purpose'],args['decision_needed'],refs,args['causation_id']])
                old = self.store.one('SELECT * FROM team_handoffs WHERE goal_id=? AND intent_revision=? AND business_digest=?',
                    (goal['id'],revision,fingerprint))
                if old:
                    return {'handoff_id':old['id'],'created':False}
                if goal['messages']>=json.loads(goal['spec'])['budget']['max_messages']:
                    raise DevError('GOAL_MESSAGE_BUDGET','原目标消息预算已用完',409)
                identifier,now = uuid.uuid4().hex,self.c.clock()
                self.store.execute('''INSERT INTO team_handoffs
                    (id,goal_id,approval_id,intent_revision,coordinator_epoch,author_dot_id,recipient_dot_id,
                     work_item_id,purpose,decision_needed,artifact_refs,causation_id,hop_count,business_digest,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier,goal['id'],goal['approval_id'],revision,epoch,author_rules[0]['legacy_dot_id'],
                     recipient_rules[0]['legacy_dot_id'],args['work_item_id'],redact(args['purpose']),
                     redact(args['decision_needed']),canonical(refs),args['causation_id'],args['hop_count'],fingerprint,now))
                self.store.execute('INSERT INTO room_goal_handoff_members VALUES (?,?,?,?,?,?)',
                    (identifier,member['id'],target['id'],member['binding_generation'],target['binding_generation'],canonical(provenance)))
                self.store.execute('''INSERT INTO coordination_messages
                    (id,goal_id,approval_id,author_grant_id,body,mention_grant_ids,provenance_project_ids,created)
                    VALUES (?,?,?,?,?,?,?,?)''',(identifier,goal['id'],goal['approval_id'],principal.grant_id,
                        redact(args['purpose']),canonical([target['grant_id']]),canonical(provenance),now))
                self.store.execute('UPDATE coordination_goals SET messages=messages+1 WHERE id=?',(goal['id'],))
                self.c.room_consumers.enqueue_ref(target,'peer_handoff',identifier,1,
                    {'goal_id':goal['id']})
                self.c.rooms.change(goal['conversation_id'],'work',goal['id'],goal['version'])
                return {'handoff_id':identifier,'created':True}

            result = self.c.room_runtime.member_receipt(args,member,principal,'room_work_handoff',save)
            return {'handoff':self.read(member,result['handoff_id'],principal,author=True),
                    'created':result['created'],'scheduled':False}

    def read(self, member, identifier, principal, *, author=False):
        row = self.store.one('''SELECT h.*,m.author_member_id,m.recipient_member_id,m.author_generation,
            m.recipient_generation,m.provenance_project_ids FROM team_handoffs h
            JOIN room_goal_handoff_members m ON m.handoff_id=h.id WHERE h.id=?''',(identifier,))
        expected = 'author_member_id' if author else 'recipient_member_id'
        generation = 'author_generation' if author else 'recipient_generation'
        if not row or row[expected]!=member['id'] or row[generation]!=member['binding_generation']:
            raise DevError('TEAM_HANDOFF_NOT_FOUND','当前成员没有这个交接',404)
        goal = self.c.room_runtime.goal(row['goal_id'])
        if goal['conversation_id']!=member['conversation_id']:
            raise DevError('TEAM_HANDOFF_NOT_FOUND','当前房间没有这个交接',404)
        goal = {**goal,'_reader_member_id':member['id']}
        partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        self.c.coordination.authorize(principal,partition,goal,active=True)
        native = self.c.room_work_bridge.native
        spec = native.record(native.reference(goal))
        self.saved_member(spec,row['author_member_id'])
        self.saved_member(spec,row['recipient_member_id'])
        if (row['approval_id']!=goal['approval_id'] or row['intent_revision']!=self.c.room_runtime.intent_revision(goal['id'])
                or row['coordinator_epoch']!=self.c.room_goal_recovery.epoch(goal['id'])):
            raise DevError('TEAM_HANDOFF_STALE','此交接不再属于当前批准、本人意图或主责世代',409)
        refs = self.c.coordination.teams.artifacts(goal,json.loads(row['artifact_refs']))
        visible = native.visible_projects(goal,principal,member['id'])
        limited = not set(json.loads(row['provenance_project_ids']))<=visible
        original = native.source(goal,principal)
        intent = self.store.one('SELECT * FROM room_work_intent_sources WHERE goal_id=? AND intent_revision=?',
            (goal['id'],row['intent_revision']))
        current = self.store.one('SELECT * FROM collaboration_messages WHERE id=?',
            (intent['source_message_id'],)) if intent else original
        self.c.chatroom.require_message_visible(principal,current)
        recipient = next(m for m in spec['members'] if m['member_id']==member['id'])
        context = {'original_request':{'message_id':original['id'],'version':original['version'],
            'body_text':json.loads(original['body']).get('body_text','')},
            'current_intent':{'message_id':current['id'],'version':current['version'],
                'digest':digest(json.loads(current['body']).get('body_text','')),
                'body_text':json.loads(current['body']).get('body_text','')},
            'member_projects':[{k:r[k] for k in ('project_id','environment_id','capabilities','execution_targets')}
                for r in recipient['projects'] if r['project_id'] in visible],
            'work_item':None}
        if row['work_item_id']:
            item = self.c.coordination.work(goal,row['work_item_id'])
            if item['project_id'] in visible:
                context['work_item'] = {k:item[k] for k in ('id','project_id','objective','acceptance','state')}
                context['work_item']['dependencies'] = json.loads(item['dependencies'])
        # References remain opaque evidence; no result text or operation body
        # crosses a member's current project visibility boundary.
        return {k:row[k] for k in ('id','goal_id','approval_id','intent_revision','coordinator_epoch',
            'author_member_id','recipient_member_id','work_item_id','causation_id','hop_count','created')} | {
            'purpose':'原工作成员请求交接；当前身份仅可读取关联工作状态。' if limited else row['purpose'],
            'decision_needed':'' if limited else row['decision_needed'],'artifact_refs':refs,
            'context_limited':limited,'context':context}
