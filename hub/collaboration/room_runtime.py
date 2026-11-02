"""One canonical room timeline and owner controls over existing managed work.

Messages do not guess execution intent. The selected member interprets an
authenticated input; task admission still requires the saved scope and managed
operation lease. Work controls never manufacture a successful cancellation.
"""
from __future__ import annotations

import json
import uuid

from shared.room_runtime_contracts import RoomMessage, WorkControl, RoomMemberReply, RoomWorkStart
from hub.collaboration.common import canonical, digest, read_cursor, redact, sign_cursor, validate
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_work_intents(
        goal_id TEXT PRIMARY KEY REFERENCES coordination_goals(id),
        intent_revision INTEGER NOT NULL DEFAULT 1,source_message_id TEXT NOT NULL DEFAULT '',
        updated REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_work_sources(
        message_id TEXT NOT NULL REFERENCES collaboration_messages(id),
        member_id TEXT NOT NULL REFERENCES room_members(id),project_id TEXT NOT NULL,
        environment_id TEXT NOT NULL,conversation_id TEXT NOT NULL,partition_id TEXT NOT NULL,
        scope_version INTEGER NOT NULL,scope_digest TEXT NOT NULL,grant_id TEXT NOT NULL,
        binding_generation INTEGER NOT NULL,source_version INTEGER NOT NULL,source_digest TEXT NOT NULL,
        legacy_dot_id TEXT NOT NULL,goal_id TEXT NOT NULL DEFAULT '',created REAL NOT NULL,
        PRIMARY KEY(message_id,member_id,project_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_work_intent_sources(
        goal_id TEXT NOT NULL REFERENCES coordination_goals(id),intent_revision INTEGER NOT NULL,
        source_message_id TEXT NOT NULL UNIQUE,source_version INTEGER NOT NULL,source_digest TEXT NOT NULL,
        member_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(goal_id,intent_revision))''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS room_work_intent_immutable BEFORE UPDATE ON room_work_intent_sources
        BEGIN SELECT RAISE(ABORT,'room work intent source is immutable'); END''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_goal_publications(
        goal_id TEXT NOT NULL REFERENCES coordination_goals(id),intent_revision INTEGER NOT NULL,
        message_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(goal_id,intent_revision))''')


class RoomRuntime:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def owner(self, principal, conversation_id, *, archived=False):
        return self.c.rooms.owner(principal,conversation_id,include_archived=archived)

    def source(self,args,member,principal):
        row=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
            (args['source_message_id'],member['conversation_id']))
        if not row:
            raise DevError('ROOM_SOURCE_NOT_FOUND','此房间没有原始输入',404)
        self.c.chatroom.require_message_visible(principal,row)
        body=json.loads(row['body'])
        if row['version']!=args['source_version'] or digest(body.get('body_text',''))!=args['source_digest']:
            raise DevError('ROOM_SOURCE_CHANGED','原始输入已修订，请重新读取',409)
        addressed=(body.get('room_member_id')==member['id'] or member.get('legacy_dot_id') in body.get('duplex_recipients',[]))
        if not addressed:
            raise DevError('ROOM_SOURCE_NOT_ADDRESSED','原始输入没有发给此成员',403)
        return row

    def member_receipt(self,args,member,principal,action,perform):
        # Consumer proofs rotate; they authorize replay but are not business
        # identity. A retry under the current lease recovers the same result.
        stable={k:v for k,v in args.items() if k not in {'consumer_session_id','consumer_fence','consumer_proof','expected_inbox_revision','connection_proof'}}
        conversation=self.store.one('SELECT * FROM conversation_rooms WHERE id=?',(member['conversation_id'],))
        return self.c.room_channels.receipt(principal,conversation,action+':'+member['id'],stable,perform)

    def reply(self,raw,principal):
        args=validate(RoomMemberReply,raw)
        with self.store.transaction():
            member=self.c.room_consumers.authorize_member(args,principal,require_revision=True)
            source=self.source(args,member,principal)
            source_body=json.loads(source['body'])
            related_id=source_body.get('work_id') or source.get('goal_id')
            related=self.goal(related_id) if related_id else None
            publication=self.publication(args,member,source,related,principal) if related and args['complete'] else None
            def save():
                identifier=uuid.uuid4().hex
                if publication:
                    revision=publication['intent_revision']
                    published=self.store.one('SELECT * FROM room_goal_publications WHERE goal_id=? AND intent_revision=?',(related['id'],revision))
                    if published:
                        existing=self.store.one('SELECT body FROM collaboration_messages WHERE id=?',(published['message_id'],))
                        saved=json.loads(existing['body']) if existing else {}
                        if (saved.get('body_text')!=redact(args['body_text']) or saved.get('outcome')!=publication['outcome']
                                or saved.get('artifact_refs')!=publication['artifact_refs']
                                or saved.get('coordinator_epoch',1)!=publication['coordinator_epoch']):
                            raise DevError('ROOM_PUBLICATION_CONFLICT','原工作本次意图已有不同的最终汇总，不能用新请求覆盖',409)
                        return {'message_id':published['message_id'],'complete':True}
                body={'body_text':redact(args['body_text']),'sender_member_id':member['id'],
                      'sender_name':member['name'],'dot_reply_complete':args['complete'],
                      'source_message_id':source['id'],'provenance_project_ids':source_body.get('provenance_project_ids',[])}
                if related:
                    native=self.c.room_work_bridge.native
                    projects=native.visible_projects(related,principal,member['id']) if native.reference(related) else set(json.loads(related['spec'])['project_ids'])
                    body['provenance_project_ids']=sorted(set(body['provenance_project_ids'])|projects)
                    body['intent_revision']=self.intent_revision(related['id'])
                if publication:
                    body.update(publication,goal_id=related['id'],acceptance_verified_by_owner=False)
                if member.get('legacy_dot_id'):
                    body['sender_dot_id']=member['legacy_dot_id']
                root=source['thread_root_id'] or source['id']
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created,goal_id)
                    VALUES (?,?,?,?,?,?,?,'bound_connector',?,'reply',?,'saved',?,?)''',
                    (identifier,source['room_id'],source['conversation_id'],root,root,source['id'],principal.grant_id,
                     'room-reply:'+digest([member['id'],args['idempotency_key']]),canonical(body),self.c.clock(),related['id'] if related else source.get('goal_id')))
                message=self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(identifier,))
                if publication:
                    self.store.execute('INSERT INTO room_goal_publications VALUES (?,?,?,?)',
                        (related['id'],self.intent_revision(related['id']),identifier,self.c.clock()))
                    self.store.execute('''INSERT INTO team_publications
                        (id,goal_id,intent_revision,result_digest,artifact_refs,outcome,summary,message_id,created)
                        VALUES (?,?,?,?,?,?,?,?,?)''',(identifier,related['id'],publication['intent_revision'],publication['result_digest'],
                            canonical(publication['artifact_refs']),publication['outcome'],redact(args['body_text']),identifier,self.c.clock()))
                self.c.rooms.change(member['conversation_id'],'message',identifier,message['version'])
                if source.get('room_id'):
                    self.c.channels.queue(message)
                else:
                    self.c.weixin.delivery.queue_reply(message)
                if args['complete']:
                    self.c.room_consumers.ack_message(member,source['id'])
                return {'message_id':identifier,'complete':args['complete']}
            receipt=self.member_receipt(args,member,principal,'room_reply',save)
            return {'message':self.message_view(self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(receipt['message_id'],)),principal),
                    'complete':receipt['complete'],'scheduled':False}

    def publication(self,args,member,source,goal,principal):
        native=self.c.room_work_bridge.native
        reference=native.reference(goal)
        if not reference:
            return None
        native.guard(args,principal,goal,action='publish')
        if not native.owner_author(goal,source)['authenticated']:
            raise DevError('ROOM_OWNER_SOURCE_REQUIRED','原输入的本人身份批准已失效',403)
        original=native.record(reference)
        revision=self.intent_revision(goal['id'])
        intent=self.store.one('SELECT * FROM room_work_intent_sources WHERE goal_id=? AND intent_revision=?',(goal['id'],revision))
        current=intent or original
        if (source['id']!=current['source_message_id'] or source['version']!=current['source_version']
                or args['source_digest']!=current['source_digest']):
            raise DevError('ROOM_INTENT_CHANGED','最终汇总必须回复当前意图的原始输入',409)
        if args['outcome'] is None:
            raise DevError('ROOM_WORK_OUTCOME_REQUIRED','工作汇总需要明确全部完成、部分完成或受阻',422)
        works=self.store.all('''SELECT w.* FROM coordination_work w JOIN room_native_work_assignments a ON a.work_id=w.id
            WHERE w.goal_id=? AND w.approval_id=? AND a.intent_revision=?''',(goal['id'],goal['approval_id'],revision))
        if (any(w['state'] in {'queued','leased','running'} for w in works)
                or any(op['state'] not in {'succeeded','failed','cancelled'} for op in self.c.coordination.operations(goal))):
            raise DevError('ROOM_WORK_NOT_COMPLETE','原工作仍有未完成或不确定操作，请发送进展并继续核对',409)
        if args['outcome']=='succeeded' and (goal['state']!='active' or not works or any(w['state']!='succeeded' for w in works)):
            raise DevError('ROOM_WORK_NOT_SUCCESSFUL','仍有未成功的当前工作项，不能声明全部完成',409)
        refs=self.c.coordination.teams.artifacts(goal,args['artifact_refs'])
        if {r['work_item_id'] for r in refs}!={w['id'] for w in works if w['result']}:
            raise DevError('ROOM_WORK_RESULT_SET_CHANGED','汇总需要精确引用当前意图全部已有结果，包括失败与受阻项',409)
        return {'intent_revision':revision,'coordinator_epoch':self.c.room_goal_recovery.epoch(goal['id']),
                'outcome':args['outcome'],'artifact_refs':refs,'result_digest':digest(refs)}

    def start_work(self,raw,principal):
        args=validate(RoomWorkStart,raw)
        with self.store.transaction():
            member=self.c.room_consumers.authorize_member(args,principal,require_revision=True)
            source=self.source(args,member,principal)
            source_body=json.loads(source['body'])
            original_id=source_body.get('work_id') or source.get('goal_id')
            original=self.goal(original_id) if original_id else None
            author=self.c.room_work_bridge.native.owner_author(original,source) if original and self.c.room_work_bridge.native.reference(original) else self.c.channel_owner.author(source)
            if not author['authenticated']:
                raise DevError('ROOM_OWNER_SOURCE_REQUIRED','工作必须来自本人新的明确输入',403)
            if original_id:
                partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(original['room_id'],))
                self.c.coordination.authorize(principal,partition,original)
                if source_body.get('work_id'):
                    return self.continue_work(args,member,source,original,principal)
                result={'goal_id':original['id'],'resumed_existing':True}
                selected=set(args['project_ids'] or ([args['target_project_id']] if args['target_project_id'] else []))
                if selected and selected!=set(json.loads(original['spec'])['project_ids']):
                    result['scope_revision_required']=True
                return self.work_response(result,args,member,principal)
            native_reference,native_scope=self.c.room_work_bridge.native.prepare(member,source,principal,args)
            scopes=self.c.room_work_bridge.scopes(member)
            project_id=native_scope['anchor_project_id']
            bridge,scope=self.c.room_work_bridge.adapter(member,project_id,args['environment_id'] or None)
            owner=self.c.room_work_bridge.owner(member,scope)
            dot=self.c.dots.find(bridge['legacy_dot_id'])
            partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(dot['room_id'],))
            self.c.dots.approved_owner(partition,dot,principal)
            policy=self.c.dots.policy(partition,dot)
            self.c.delegation.authorize_policy(principal,partition,policy)
            capabilities=native_scope['capabilities']
            targets=native_scope['execution_targets']
            def save():
                old=self.store.one('SELECT * FROM room_work_sources WHERE message_id=? AND member_id=? AND project_id=?',
                    (source['id'],member['id'],project_id))
                if old and old['goal_id']:
                    goal=self.goal(old['goal_id'])
                    self.c.coordination.authorize(principal,partition,goal)
                    return {'goal_id':goal['id'],'resumed_existing':True}
                if source.get('goal_id'):
                    original=self.goal(source['goal_id'])
                    if project_id in json.loads(original['spec'])['project_ids']:
                        self.c.coordination.authorize(principal,partition,original)
                        return {'goal_id':original['id'],'resumed_existing':True}
                # The immutable source mapping is admitted only after the
                # current native consumer fence and exact project approval.
                for saved in native_scope['members']:
                    for rule in saved['projects']:
                        adapter_dot=self.c.dots.find(rule['legacy_dot_id'])
                        self.store.execute('''INSERT INTO room_work_sources
                            (message_id,member_id,project_id,environment_id,conversation_id,partition_id,scope_version,scope_digest,
                             grant_id,binding_generation,source_version,source_digest,legacy_dot_id,created)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (source['id'],saved['member_id'],rule['project_id'],rule['environment_id'],member['conversation_id'],adapter_dot['room_id'],
                             rule['scope_version'],rule['scope_digest'],saved['grant_id'],saved['binding_generation'],source['version'],
                             args['source_digest'],rule['legacy_dot_id'],self.c.clock()))
                result=self.c.delegation.send(owner,partition,source,{'delegation':{'policy_id':policy['id'],
                    'policy_version':policy['version'],'capabilities':capabilities,'execution_targets':targets,
                    'acceptance':args['acceptance']},'mentions':[{'slot_id':dot['id']}]},native_scope=native_reference)
                self.store.execute('UPDATE room_work_sources SET goal_id=? WHERE message_id=?',
                    (result['goal_id'],source['id']))
                self.c.room_consumers.enqueue_ref(member,'work_ready',result['work_item_id'],1,
                    {'goal_id':result['goal_id'],'work_item_id':result['work_item_id'],'source_project_id':project_id,
                     'message_id':source['id']})
                self.c.room_consumers.ack_message(member,source['id'])
                self.c.rooms.change(member['conversation_id'],'work',result['goal_id'],1)
                return {'goal_id':result['goal_id'],'resumed_existing':False}
            result=self.member_receipt(args,member,principal,'room_work_start',save)
            return self.work_response(result,args,member,principal)

    def intent_revision(self,goal_id):
        row=self.store.one('SELECT intent_revision FROM room_work_intents WHERE goal_id=?',(goal_id,))
        return row['intent_revision'] if row else 1

    def revise_intent(self,goal,source,member):
        existing=self.store.one('SELECT * FROM room_work_intent_sources WHERE source_message_id=?',(source['id'],))
        if existing:
            return existing['intent_revision']
        if goal['state']=='cancelled':
            raise DevError('WORK_CANCELLED','已取消的工作不能通过补充重新执行',409)
        revision=self.intent_revision(goal['id'])+1
        self.store.execute('INSERT INTO room_work_intent_sources VALUES (?,?,?,?,?,?,?)',
            (goal['id'],revision,source['id'],source['version'],digest(json.loads(source['body']).get('body_text','')),member['id'],self.c.clock()))
        self.store.execute('''INSERT INTO room_work_intents VALUES (?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET
            intent_revision=excluded.intent_revision,source_message_id=excluded.source_message_id,updated=excluded.updated''',
            (goal['id'],revision,source['id'],self.c.clock()))
        self.c.coordination.stop_work(goal,'INTENT_REVISED')
        self.store.execute("UPDATE coordination_goals SET state='paused',version=version+1,updated=? WHERE id=?",(self.c.clock(),goal['id']))
        native=self.c.room_work_bridge.native
        if reference:=native.reference(goal):
            for saved in native.record(reference)['members']:
                current=self.c.rooms.member(saved['member_id'],goal['conversation_id'])
                self.c.room_consumers.enqueue_ref(current,'control_notice',goal['id'],revision,
                    {'goal_id':goal['id'],'message_id':source['id'],'reason_code':'intent_revised'})
        return revision

    def continue_work(self,args,member,source,goal,principal):
        native=self.c.room_work_bridge.native
        reference=native.reference(goal)
        if not reference:
            raise DevError('ROOM_WORK_CONTINUATION_UNAVAILABLE','原工作需沿其原任务继续入口恢复',409)
        spec=native.validate(reference,principal)
        self.c.room_goal_recovery.require_epoch(goal,args.get('coordinator_epoch'))
        if member['id']!=spec['coordinator_member_id'] or goal['conversation_id']!=source['conversation_id']:
            raise DevError('ROOM_WORK_LEAD_REQUIRED','继续原工作需要原主责成员',403)
        if args['project_ids'] and set(args['project_ids'])!=set(spec['project_ids']):
            raise DevError('ROOM_WORK_SCOPE_CHANGED','补充只适用原工作范围，不能自动增加项目',409)
        intent=self.store.one('SELECT * FROM room_work_intents WHERE goal_id=?',(goal['id'],))
        if not intent or intent['source_message_id']!=source['id']:
            raise DevError('ROOM_INTENT_CHANGED','此补充已被更新，请读取最新本人输入',409)
        record=self.store.one('SELECT * FROM room_work_intent_sources WHERE goal_id=? AND intent_revision=?',(goal['id'],intent['intent_revision']))
        if record['source_version']!=source['version'] or record['source_digest']!=args['source_digest']:
            raise DevError('ROOM_SOURCE_CHANGED','原补充的正文或版本已改变',409)
        if any(op['state'] not in {'succeeded','failed','cancelled'} for op in self.c.coordination.operations(goal)):
            return self.work_response({'goal_id':goal['id'],'resumed_existing':True,'reason_code':'WORK_OPERATION_PENDING'},args,member,principal)
        partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        def save():
            self.c.coordination.authorize(principal,partition,{**goal,'state':'active'},active=True)
            self.store.execute("UPDATE coordination_goals SET state='active',version=version+1,updated=? WHERE id=?",(self.c.clock(),goal['id']))
            project_id=args['target_project_id'] or spec['anchor_project_id']
            participant=next(m for m in spec['members'] if m['member_id']==member['id'])
            rule=next((r for r in participant['projects'] if r['project_id']==project_id),None)
            capabilities=args['capabilities'] or (rule['capabilities'] if rule else [])
            if not rule or not set(capabilities)<=set(rule['capabilities']):
                raise DevError('ROOM_WORK_SCOPE_DENIED','补充不能扩大原项目能力',403)
            objective='在原目标中处理本人最新补充；先核对并复用已有操作回执，不能盲目重跑。\n'+json.loads(source['body'])['body_text']
            if len(objective)>4000:
                raise DevError('ROOM_WORK_INPUT_TOO_LONG','补充超过工作规划长度，请将本次要执行的要求缩短',422)
            created=self.c.coordination.work_create({'project':partition['project_id'],'environment_id':partition['environment_id'],
                'goal_id':goal['id'],'target_project_id':project_id,'assignee_grant_id':principal.grant_id,
                'assignee_member_id':member['id'],'objective':objective,'acceptance':args['acceptance'],
                'required_capabilities':capabilities,'expected_intent_revision':intent['intent_revision'],
                'coordinator_epoch':self.c.room_goal_recovery.epoch(goal['id']),
                **{k:args[k] for k in ('consumer_session_id','consumer_fence','consumer_proof')},
                'expected_inbox_revision':self.c.rooms.member(member['id'])['dirty_revision'],
                'idempotency_key':'room-intent-work:'+digest([goal['id'],intent['intent_revision']])},principal)
            self.c.room_consumers.ack_message(member,source['id'])
            self.c.rooms.change(goal['conversation_id'],'work',goal['id'],goal['version']+1)
            return {'goal_id':goal['id'],'resumed_existing':True,'continued_work_item_id':created['work_item']['id']}
        result=self.member_receipt(args,member,principal,'room_work_continue',save)
        return self.work_response(result,args,member,principal)

    def work_response(self,result,args,member,principal):
            goal=self.goal(result['goal_id'])
            if self.c.room_work_bridge.native.reference(goal):
                goal={**goal,'_reader_member_id':member['id']}
            partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
            detail=self.c.coordination.detail(goal,principal)
            native=self.c.room_work_bridge.native
            assignments={row['work_id'] for row in self.store.all('SELECT work_id FROM room_native_work_assignments WHERE member_id=?',(member['id'],))}
            queued=next((item for item in detail['work_items'] if item['state']=='queued'
                and item['assignee_grant_id']==principal.grant_id and (not native.reference(goal) or item['id'] in assignments)),None)
            next_request=None
            eligible=goal['state']=='active' and queued and not result.get('scope_revision_required')
            if eligible:
                try:
                    self.c.coordination.authorize(principal,partition,goal,active=True)
                except DevError as exc:
                    result['reason_code']=exc.code
                    eligible=False
            if eligible:
                from shared.public_collaboration import checked_request
                next_request=checked_request('collaboration_work_claim',{'project':partition['project_id'],
                    'environment_id':partition['environment_id'],'goal_id':goal['id'],
                    'work_item_id':queued['id'],'expected_version':queued['version'],
                    'consumer_session_id':args['consumer_session_id'],'consumer_fence':args['consumer_fence'],
                    'consumer_proof':args['consumer_proof'],
                    'expected_inbox_revision':self.c.rooms.member(member['id'])['dirty_revision'],
                    'expected_intent_revision':self.intent_revision(goal['id']),
                    'coordinator_epoch':self.c.room_goal_recovery.epoch(goal['id']),
                    'idempotency_key':'room-claim:'+digest([queued['id'],queued['version'],args['consumer_session_id']])})
            return {**result,**detail,'intent_revision':self.intent_revision(goal['id']),'scheduled':bool(eligible),'next_request':next_request}

    def message_view(self,row,principal):
        self.c.chatroom.require_message_visible(principal,row)
        view=self.c.chatroom.view(row)
        body=json.loads(row['body'])
        if body.get('sender_member_id'):
            view['display_name']=body.get('sender_name','dot')
        view['created_at']=row['created']
        view['work_id']=row.get('goal_id') or json.loads(row['body']).get('work_id','')
        view['client_message_id']=row['source_id']
        view['routing']=json.loads(row['body']).get('room_routing',{})
        route=view['routing']
        view['display']={'title':('已保存，等待主责 dot' if route.get('reason_code')=='lead_member_required' else
            '已保存，等待恢复连接' if route.get('state')=='waiting' else
            '已发送，等待 dot' if route.get('state')=='queued' else '已保存')}
        return view

    def read(self,conversation_id,principal,*,cursor='',limit=50,client_message_id=''):
        if type(limit) is not int or not 1<=limit<=100:
            raise DevError('INVALID_ARGUMENTS','消息分页数量须为 1–100',422)
        with self.store.transaction(immediate=False):
            principal,conversation=self.owner(principal,conversation_id,archived=True)
            if client_message_id:
                row=self.store.one('''SELECT * FROM collaboration_messages WHERE conversation_id=?
                    AND author=? AND origin='panel_owner' AND source_id=?''',(conversation_id,principal.user_id,client_message_id))
                if not row:
                    return {'messages':[],'not_saved':True,'client_message_id':client_message_id,'revision':conversation['revision']}
                message=self.message_view(row,principal)
                return {'message':message,'messages':[message],'not_saved':False,'routing':message['routing'],
                        'revision':conversation['revision']}
            visible_projects=self.c.conversations.visible(principal,conversation)
            binding=digest(['room-messages-v1',conversation_id,principal.user_id,principal.space_id,
                            sorted(row['id'] for row in visible_projects),conversation['version']])
            before=read_cursor(self.c.secret,binding,cursor) if cursor else 9223372036854775807
            # Page after applying visibility; hidden messages never leak their
            # title, preview, source or number through a projection.
            rows=self.store.all('''SELECT * FROM collaboration_messages WHERE conversation_id=? AND conversation_sequence<?
                ORDER BY conversation_sequence DESC LIMIT ?''',(conversation_id,before,min(1000,limit*10+1)))
            allowed=[row for row in rows if self.c.chatroom.message_visible(principal,row)]
            selected=allowed[:limit]
            selected.reverse()
            has_more=len(allowed)>limit or len(rows)==min(1000,limit*10+1)
            next_sequence=(selected[0]['conversation_sequence'] if len(allowed)>limit and selected else
                rows[-1]['conversation_sequence'] if rows else None)
            return {'messages':[self.message_view(row,principal) for row in selected],
                'next_cursor':sign_cursor(self.c.secret,binding,next_sequence) if has_more and next_sequence is not None else None,
                'works':self.works(principal,conversation_id),'revision':conversation['revision'],
                'visibility_revision':binding,'scope_revision':binding}

    def recipient(self,conversation,identifier):
        if identifier:
            member=self.c.rooms.member(identifier,conversation['id'])
        elif conversation.get('lead_member_id'):
            member=self.c.rooms.member(conversation['lead_member_id'],conversation['id'])
        else:
            rows=self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status!='removed' ORDER BY created_at,id LIMIT 2",(conversation['id'],))
            member=rows[0] if len(rows)==1 else None
        return member

    def send(self,raw,principal):
        args=validate(RoomMessage,raw)
        with self.store.transaction():
            principal,conversation=self.owner(principal,args['conversation_id'])
            def save():
                if conversation['state']!='active':
                    raise DevError('ROOM_PAUSED','房间已暂停，请先继续此房间',409)
                fingerprint=digest({k:v for k,v in args.items() if k!='idempotency_key'})
                old=self.store.one("SELECT * FROM collaboration_messages WHERE conversation_id=? AND origin='panel_owner' AND author=? AND source_id=?",
                    (conversation['id'],principal.user_id,args['client_message_id']))
                if old:
                    if json.loads(old['body']).get('room_request_digest')!=fingerprint:
                        raise DevError('SOURCE_MESSAGE_CONFLICT','同一消息标识不能替换已保存的消息',409)
                    return {'message_id':old['id']}
                count=self.store.one('''SELECT COUNT(*) AS n FROM collaboration_messages
                    WHERE conversation_id=? AND author=? AND created>?''',(conversation['id'],principal.user_id,self.c.clock()-60))['n']
                if count>=60:
                    raise DevError('MESSAGE_RATE_LIMIT','发送过于频繁，请稍后继续',429)
                member=self.recipient(conversation,args['member_id'])
                routing={'state':'queued' if member and member['status'] not in {'paused','removed'} else 'waiting',
                         'member_id':member['id'] if member else '',
                         'reason_code':'member_paused' if member and member['status']=='paused' else
                                       'member_removed' if member and member['status']=='removed' else
                                       'lead_member_required' if not member else ''}
                identifier=uuid.uuid4().hex
                parent=None
                if args['reply_to_id']:
                    parent=self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',(args['reply_to_id'],conversation['id']))
                    if not parent:
                        raise DevError('THREAD_NOT_FOUND','引用消息不在当前房间',404)
                    self.c.chatroom.require_message_visible(principal,parent)
                if args['work_id']:
                    goal=self.goal(args['work_id'])
                    if goal['conversation_id']!=conversation['id']:
                        raise DevError('GOAL_NOT_FOUND','此工作不在当前房间',404)
                    partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
                    self.c.coordination.authorize(principal,partition,goal)
                    if reference:=self.c.room_work_bridge.native.reference(goal):
                        original=self.c.room_work_bridge.native.validate(reference,principal)
                        if args['member_id'] and args['member_id']!=original['coordinator_member_id']:
                            raise DevError('ROOM_WORK_LEAD_REQUIRED','原工作补充须发给原主责成员',403)
                        member=self.c.rooms.member(original['coordinator_member_id'],conversation['id'])
                        routing={'state':'queued','member_id':member['id'],'reason_code':''}
                root=parent['thread_root_id'] or parent['id'] if parent else identifier
                provenance=json.loads(parent['body']).get('provenance_project_ids',[]) if parent else []
                body={'body_text':redact(args['body_text']),'room_request_digest':fingerprint,'room_routing':routing,
                      'room_member_id':member['id'] if member else '', 'work_id':args['work_id'],
                      'attachment_ids':args['attachment_ids']}
                if provenance:
                    body['provenance_project_ids']=provenance
                dot=self.c.dots.find(member['legacy_dot_id']) if member and member['status']=='legacy' and member.get('legacy_dot_id') else None
                partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(dot['room_id'],)) if dot else None
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created)
                    VALUES (?,?,?,?,?,?,?,'panel_owner',?,'text',?,'saved',?)''',
                    (identifier,partition['id'] if partition else None,conversation['id'],root,root,args['reply_to_id'],
                     principal.user_id,args['client_message_id'],canonical(body),self.c.clock()))
                message=self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(identifier,))
                self.c.room_attachments.attach(args['attachment_ids'], message, principal)
                if args['work_id']:
                    self.revise_intent(goal,message,member)
                if member and routing['state']=='queued':
                    if dot:
                        mentions=[{'slot_id':dot['id'],'display_snapshot':member['name']}]
                        try:
                            duplex=self.c.dot_chat.enqueue(partition,message,mentions,principal)
                        except DevError as exc:
                            # Persist the input even if the connection needs
                            # restoration. Never fall back to a broader member.
                            routing.update(state='waiting',reason_code=exc.code)
                            body['room_routing']=routing
                        else:
                            body.update(mentions=mentions,duplex_recipients=duplex)
                            if duplex:
                                message=self.c.owner_task_context.incoming(partition,dot,message,principal)
                                context=json.loads(message['body'])
                                if context.get('task_context_schema')==1:
                                    body['task_context_schema']=1
                                body['provenance_project_ids']=sorted(set(provenance)|set(context.get('provenance_project_ids',[])))
                    self.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?',(canonical(body),identifier))
                    message=self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(identifier,))
                    self.c.room_consumers.enqueue(message,member,principal)
                self.c.rooms.change(conversation['id'],'message',identifier,message['version'])
                if partition:
                    self.c.channels.queue(message)
                return {'message_id':identifier}
            result=self.c.room_channels.receipt(principal,conversation,'room_message',args,save)
            row=self.store.one('SELECT * FROM collaboration_messages WHERE id=?',(result['message_id'],))
            message=self.message_view(row,principal)
            current=self.store.one('SELECT revision FROM conversation_rooms WHERE id=?',(conversation['id'],))
            return {'message':message,'routing':message['routing'],'revision':current['revision']}

    def goal(self,identifier):
        goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(identifier,))
        if not goal:
            work=self.store.one('SELECT goal_id FROM coordination_work WHERE id=?',(identifier,))
            goal=self.store.one('SELECT * FROM coordination_goals WHERE id=?',(work['goal_id'],)) if work else None
        if not goal:
            raise DevError('GOAL_NOT_FOUND','没有此工作',404)
        return goal

    def work_view(self,goal,principal,*,member_id=''):
        native=self.c.room_work_bridge.native
        if member_id and native.reference(goal):
            goal={**goal,'_reader_member_id':member_id}
        partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
        self.c.coordination.authorize(principal,partition,goal)
        items=self.store.all('SELECT * FROM coordination_work WHERE goal_id=? ORDER BY created,id',(goal['id'],))
        if native.reference(goal):
            revision=self.intent_revision(goal['id'])
            current_ids={r['work_id'] for r in self.store.all('SELECT work_id FROM room_native_work_assignments WHERE intent_revision=?',(revision,))}
            items=[item for item in items if item['id'] in current_ids]
        operations=self.c.coordination.operations(goal)
        active=any(op['state'] in {'queued','running','reconnecting','cancelling','unknown'} for op in operations)
        state=goal['state']
        reason=''
        if state in {'paused','cancelled'}:
            state='stopping' if active else state
        elif state=='active':
            if any(op['state']=='unknown' for op in operations):
                state,reason='reconciling','operation_unconfirmed'
            elif items and all(item['state']=='succeeded' for item in items):
                state='completed'
            elif any(item['state'] in {'leased','running'} for item in items):
                state='running'
            elif any(item['state']=='queued' for item in items):
                state='queued'
            else:
                state,reason='blocked',next((item['reason_code'] for item in items if item['reason_code']),'waiting_host')
        intent=self.store.one('SELECT * FROM room_work_intents WHERE goal_id=?',(goal['id'],))
        spec=json.loads(goal['spec'])
        revision=intent['intent_revision'] if intent else max(1,self.c.coordination.teams.current_intent_revision(goal['id']))
        if intent and goal['state']=='paused' and not active:
            state,reason='waiting_member','intent_revised'
        allowed=[] if goal['state']=='cancelled' else ['cancel','supplement']
        if goal['state']=='active' and state!='completed':
            allowed.insert(0,'pause')
        if goal['state']=='paused' and not active and not (intent and reason=='intent_revised'):
            allowed.insert(0,'resume')
        title={'queued':'等待 dot 开始','running':'正在处理','reconciling':'正在核对原操作回执',
            'completed':'工作已完成','paused':'工作已暂停','stopping':'正在停止，等待原操作回执',
            'cancelled':'工作已取消','waiting_member':'已保存补充，等待 dot 核对',
            'blocked':'工作暂时受阻','proposed':'等待批准','expired':'原工作批准已到期'}.get(state,'工作状态已更新')
        reasons={'operation_unconfirmed':'原操作结果尚未确认，核对前不会重复执行。',
            'intent_revised':'将沿原目标和已有操作回执处理最新补充。','waiting_host':'等待当前连接恢复处理。',
            'ROOM_PROJECT_REMOVED':'原工作使用的项目已移出，需要核对范围。',
            'ROOM_MEMBER_SCOPE_REVISED':'原成员工作范围已改变，旧范围不能继续执行。'}
        native_scope=self.c.room_work_bridge.native.record(spec['room_member_scope']) if spec.get('room_member_scope') else None
        visible=native.visible_projects(goal,principal,member_id) if native_scope else set(spec['project_ids'])
        partial=visible!=set(spec['project_ids'])
        recovery=self.c.room_goal_recovery.projection(goal,principal) if native_scope and not principal.grant_id else None
        if recovery and recovery['restore_candidates']:
            allowed.append('restore_binding')
        if recovery and recovery['takeover_candidates']:
            allowed.append('takeover')
        assignments={row['work_id']:row['member_id'] for row in self.store.all('''SELECT a.work_id,a.member_id
            FROM room_native_work_assignments a JOIN coordination_work w ON w.id=a.work_id WHERE w.goal_id=?''',(goal['id'],))} if native_scope else {}
        return {'id':goal['id'],'conversation_id':goal['conversation_id'],'version':goal['version'],
                'intent_revision':revision,
                **({'coordinator_epoch':native_scope['coordinator_epoch']} if native_scope else {}),
                **({'recovery':recovery} if recovery else {}),
                'state':state,'goal_state':goal['state'],'reason_code':reason,
                'display':{'title':title,'reason':reasons.get(reason,''),'allowed_actions':allowed},'allowed_actions':allowed,
                **({'members':[{'id':m['member_id'],'name':m['name'],'grant_id':m['grant_id']} for m in native_scope['members']],
                    'coordinator_member_id':native_scope['coordinator_member_id']} if native_scope else {}),
                'title':'协作工作' if partial else spec['objective'],'acceptance':'' if partial else spec['acceptance'],'project_ids':sorted(visible),
                'visibility_limited':partial,
                'created_at':goal['created'],'updated_at':goal['updated'],
                'items':[{'id':item['id'],'state':item['state'],'reason_code':item['reason_code'],
                    'member_id':assignments.get(item['id'],''),'project_id':item['project_id'],
                    'title':redact(item['objective'])[:160]} for item in items if item['project_id'] in visible],
                'operations':[{k:op[k] for k in ('id','state','cancel_requested')} | {
                    'display':{'title':'原操作 '+op['state'],'reason':op.get('cancel_note','')}}
                    for op in operations if not principal.grant_id or op['project_id'] in visible],
                'related_work':[{'id':item['id'],'state':item['state']} for item in items if item['project_id'] not in visible]}

    def works(self,principal,conversation_id):
        rows=self.store.all('SELECT * FROM coordination_goals WHERE conversation_id=? ORDER BY updated DESC,id LIMIT 100',(conversation_id,))
        result=[]
        for goal in rows:
            try:
                result.append(self.work_view(goal,principal))
            except DevError:
                continue
        return result

    def snapshot(self,principal,conversation_id):
        works=self.works(principal,conversation_id)
        state='running' if any(w['state'] in {'running','queued','reconciling'} for w in works) else 'waiting_user' if any(w['state']=='blocked' for w in works) else 'idle'
        return {'state':state,'works':works}

    def control(self,raw,principal):
        args=validate(WorkControl,raw)
        with self.store.transaction():
            goal=self.goal(args['work_id'])
            principal,conversation=self.owner(principal,goal['conversation_id'])
            partition=self.store.one('SELECT * FROM collaboration_rooms WHERE id=?',(goal['room_id'],))
            self.c.coordination.authorize(principal,partition,goal)
            def save():
                if goal['version']!=args['expected_version']:
                    raise DevError('STALE_VERSION','工作已更新，请核对最新进展',409)
                if args['action'] in {'takeover','restore_binding'}:
                    return self.c.room_goal_recovery.apply(goal,args,principal,conversation)
                elif args['action'] in {'pause','cancel'}:
                    self.c.coordination.control({'project':partition['project_id'],'environment_id':partition['environment_id'],
                        'goal_id':goal['id'],'expected_version':goal['version'],'action':args['action'],
                        'idempotency_key':'room-work:'+args['idempotency_key']},principal)
                elif args['action']=='resume':
                    if goal['state']!='paused':
                        raise DevError('WORK_NOT_PAUSED','只有已暂停的原工作可以继续',409)
                    if any(op['state'] not in {'succeeded','failed','cancelled'} for op in self.c.coordination.operations(goal)):
                        raise DevError('WORK_OPERATION_PENDING','原操作尚未确认结束，请先核对原操作回执',409)
                    # Check the original approval and every live boundary before
                    # changing state. Expired or revoked work is not reapproved.
                    self.c.coordination.authorize(principal,partition,{**goal,'state':'active'},active=True)
                    native=self.c.room_work_bridge.native
                    if reference:=native.reference(goal):
                        spec=native.record(reference)
                        # Continuing is a new owner intent on the same Goal.
                        # No old item or admitted operation is blindly requeued.
                        self.send({'conversation_id':conversation['id'],
                            'body_text':'继续原工作。先核对并复用已完成、取消或失败的原操作回执，再决定必要的后续步骤。',
                            'client_message_id':'work-input:'+args['idempotency_key'],
                            'idempotency_key':'work-input:'+args['idempotency_key'],
                            'member_id':spec['coordinator_member_id'],'reply_to_id':spec['source_message_id'],
                            'work_id':goal['id']},principal)
                    else:
                        if self.c.coordination.operations(goal):
                            raise DevError('WORK_RESUME_CONTEXT_REQUIRED','原工作已有操作，请沿原任务继续入口核对回执',409)
                        self.store.execute("UPDATE coordination_goals SET state='active',version=version+1,updated=? WHERE id=?",(self.c.clock(),goal['id']))
                        current=self.goal(goal['id'])
                        for item in self.store.all("SELECT * FROM coordination_work WHERE goal_id=? AND state='blocked' AND reason_code='GOAL_PAUSE'",(goal['id'],)):
                            self.store.execute("UPDATE coordination_work SET state='queued',reason_code='',version=version+1,fencing_token=fencing_token+1,updated=? WHERE id=?",(self.c.clock(),item['id']))
                            self.c.coordination.emit_work(partition,current,self.c.coordination.work(current,item['id']))
                else:
                    if not args['body_text'].strip() and not args['attachment_ids']:
                        raise DevError('WORK_INPUT_REQUIRED','补充内容不能为空',422)
                    if goal['state']=='cancelled':
                        raise DevError('WORK_CANCELLED','已取消的工作不能通过补充重新执行',409)
                    link=self.c.delegation.goal_link(goal)
                    parent=link['message_id'] if link else ''
                    native=self.c.room_work_bridge.native
                    if reference:=native.reference(goal):
                        member={'id':native.record(reference)['coordinator_member_id']}
                    else:
                        member=self.store.one('SELECT member_id AS id FROM room_work_sources WHERE goal_id=? ORDER BY created LIMIT 1',(goal['id'],))
                    sent=self.send({'conversation_id':conversation['id'],'body_text':args['body_text'],
                        'client_message_id':'work-input:'+args['idempotency_key'],'idempotency_key':'work-input:'+args['idempotency_key'],
                        'member_id':member['id'] if member else '', 'reply_to_id':parent,'work_id':goal['id'],
                        'attachment_ids':args['attachment_ids']},principal)
                self.c.rooms.change(conversation['id'],'work',goal['id'],goal['version']+1)
                return {'goal_id':goal['id']}
            result=self.c.room_channels.receipt(principal,conversation,'room_work_control',args,save)
            current=self.goal(result['goal_id'])
            return {'work':self.work_view(current,principal),'operations':self.c.coordination.operations(current),
                    'revision':self.store.one('SELECT revision FROM conversation_rooms WHERE id=?',(conversation['id'],))['revision']}
