"""Original Goal recovery keeps immutable authority and real operation receipts."""
import json

import pytest

from hub.collaboration.common import digest
from shared.public_collaboration import resolve,checked_request
from shared.util import DevError
from tests.collaboration_support import collab as collab,key
from tests.test_room_managed_work import connected,current_epoch
from tests.test_room_members_rebuild import join,subscribe,identity as joined_identity
from tests.test_room_goal_scope_boundaries import (worker_for,start,mutation,two_project_members,
    admit_read,report,final_request)


async def rebind(s,owner,old_identity,actor):
    flow=s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?',(old_identity['member_id'],))
    current=s.rooms.connect_read({'flow_id':flow['id']},owner)
    prepared=s.rooms.connect_action({'flow_id':flow['id'],'action':'rebind',
        'expected_version':current['flow']['version'],'idempotency_key':key()},owner)
    joined,_=join(s,actor,prepared)
    await subscribe(s,actor,joined,'replacement-'+key())
    identity=joined_identity(joined)
    nonce=s.rooms.probe_read({**identity,'wake_ticket':joined['wake_ticket'],'idempotency_key':key()},actor)['probe_nonce']
    s.rooms.probe_reply({**identity,'probe_nonce':nonce,'idempotency_key':key()},actor)
    session=s.room_consumers.begin({**identity,'wake_ticket':joined['wake_ticket'],
        'consumer_run_id':key(),'idempotency_key':key()},actor)
    return identity,{k:session[k] for k in ('consumer_session_id','consumer_fence','consumer_proof')}


def control(s,owner,goal_id,action,target,**extra):
    goal=s.room_runtime.goal(goal_id)
    return s.room_runtime.control({'work_id':goal_id,'action':action,'target_member_id':target,
        'expected_version':goal['version'],'expected_coordinator_epoch':current_epoch(s,goal_id),
        'expected_intent_revision':s.room_runtime.intent_revision(goal_id),'idempotency_key':key(),**extra},owner)


def continue_original(s,actor,identity,credentials,goal_id,**extra):
    intent=s.store.one('SELECT * FROM room_work_intents WHERE goal_id=?',(goal_id,))
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(intent['source_message_id'],))
    raw={**identity,**credentials,'source_message_id':source['id'],'source_version':source['version'],
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'coordinator_epoch':current_epoch(s,goal_id),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'acceptance':'核对并复用原操作回执后，只做必要后续步骤','confirm_task':True,'idempotency_key':key(),**extra}
    return s.room_runtime.start_work(raw,actor)


@pytest.mark.asyncio
async def test_same_member_new_grant_needs_explicit_restore_and_keeps_original_goal(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    created,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    goal_id=created['goal_id']
    original=s.room_runtime.goal(goal_id)
    first=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(goal_id,))
    lease,operation=admit_read(s,identity,credentials,actor,goal_id,first,'proj')
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(operation['operation_id'],)),
        {'ok':True,'data':{'content':'Original immutable result'}})
    new_actor=worker_for(s,actor,'new-current-grant',scopes=('read',),projects=('proj',))
    current,new_credentials=await rebind(s,owner,identity,new_actor)
    with pytest.raises(DevError):
        s.coordination.read({k:v for k,v in mutation(s,current,new_credentials,goal_id).items() if k!='idempotency_key'},new_actor)
    view=s.room_runtime.work_view(s.room_runtime.goal(goal_id),owner)
    assert [m['member_id'] for m in view['recovery']['restore_candidates']]==[identity['member_id']]
    result=control(s,owner,goal_id,'restore_binding',identity['member_id'])['work']
    assert result['coordinator_epoch']==2 and result['intent_revision']==2 and result['version']>original['version']
    current_goal=s.room_runtime.goal(goal_id)
    assert {k:current_goal[k] for k in ('spec','digest','approval_id','expires_at')}=={k:original[k] for k in ('spec','digest','approval_id','expires_at')}
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(first['id'],))['state']=='blocked'
    with pytest.raises(DevError):
        s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'},'idempotency_key':key()},actor)
    with pytest.raises(DevError,match='世代'):
        continue_original(s,new_actor,current,new_credentials,goal_id,coordinator_epoch=1)
    resumed=continue_original(s,new_actor,current,new_credentials,goal_id)
    assert resumed['goal_id']==goal_id and resumed['scheduled']
    for _ in range(3):
        clock[0] += 5
        s.coordination.reconcile()
        assert s.room_runtime.goal(goal_id)['state'] == 'active'
    public_name,claim=resolve(resumed['next_request']['tool'],resumed['next_request']['arguments'])
    assert public_name=='collaboration_work_claim' and claim['coordinator_epoch']==2
    assert s.coordination.work_claim(claim,new_actor)['work_item']['state']=='leased'
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==1
    assert s.store.one('SELECT state FROM operations WHERE id=?',(operation['operation_id'],))['state']=='succeeded'
    assert len(s.store.all('SELECT * FROM room_goal_recovery_revisions'))==1


@pytest.mark.asyncio
async def test_owner_can_replace_lead_with_ready_member_without_expanding_role(collab):
    s,owner,actor,clock,lead,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    created,_=start(s,owner,actor,lead,credentials,project_ids=['proj'])
    goal_id=created['goal_id']
    other=worker_for(s,actor,'replacement-lead',scopes=('read','write'),projects=('proj',))
    _,_,other,_,replacement,replacement_credentials=await connected(collab,scopes=[{'project_id':'proj','capabilities':['read','write']}],
        worker_override=other,conversation_id=lead['conversation_id'])
    old=s.room_runtime.goal(goal_id)
    view=s.room_runtime.work_view(old,owner)
    assert any(m['member_id']==replacement['member_id'] for m in view['recovery']['takeover_candidates'])
    changed=control(s,owner,goal_id,'takeover',replacement['member_id'])['work']
    assert changed['coordinator_member_id']==replacement['member_id'] and changed['coordinator_epoch']==2
    with pytest.raises(DevError):
        s.coordination.read({k:v for k,v in mutation(s,lead,credentials,goal_id).items() if k!='idempotency_key'},actor)
    effective=s.room_work_bridge.native.record(s.room_work_bridge.native.reference(s.room_runtime.goal(goal_id)))
    assert effective['members'][0]['projects'][0]['capabilities']==['read']
    with pytest.raises(DevError):
        continue_original(s,other,replacement,replacement_credentials,goal_id,capabilities=['read','write'])
    continued=continue_original(s,other,replacement,replacement_credentials,goal_id)
    assert continued['goal_id']==goal_id and continued['scheduled']
    for _ in range(3):
        clock[0] += 5
        s.coordination.reconcile()
        assert s.room_runtime.goal(goal_id)['state'] == 'active'
    with pytest.raises(DevError,match='更新|世代'):
        control(s,owner,goal_id,'takeover',lead['member_id'],expected_coordinator_epoch=1)
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_worker_rebind_restores_same_participant_without_replacing_lead(collab):
    s,owner,a,b,lead,acred,peer,bcred,created,task=await two_project_members(collab)
    goal_id=created['goal_id']
    new_b=worker_for(s,b,'replacement-b',scopes=('read',),projects=('otherproj',))
    current,new_bcred=await rebind(s,owner,peer,new_b)
    changed=control(s,owner,goal_id,'restore_binding',peer['member_id'])['work']
    assert changed['coordinator_member_id']==lead['member_id'] and changed['coordinator_epoch']==2
    continued=continue_original(s,a,lead,acred,goal_id)
    assert continued['goal_id']==goal_id
    work=s.coordination.work_create({**mutation(s,lead,acred,goal_id),'target_project_id':'otherproj',
        'objective':'核对 B 原回执后继续原项目范围','acceptance':'不重复旧操作','required_capabilities':['read'],
        'assignee_grant_id':new_b.grant_id,'assignee_member_id':peer['member_id']},a)['work_item']
    claimed=s.coordination.work_claim({**mutation(s,current,new_bcred,goal_id,project='otherproj'),
        'work_item_id':work['id'],'expected_version':work['version']},new_b)['work_item']
    assert claimed['state']=='leased'
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(task['id'],))['state']=='blocked'


@pytest.mark.asyncio
async def test_handoff_uses_original_team_journal_and_current_epoch_inbox(collab):
    s,owner,actor,clock,lead,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    other=worker_for(s,actor,'handoff-peer',scopes=('read',),projects=('proj',))
    _,_,other,_,peer,peer_credentials=await connected(collab,scopes=[{'project_id':'proj'}],worker_override=other,conversation_id=lead['conversation_id'])
    created,_=start(s,owner,actor,lead,credentials,project_ids=['proj'],participant_member_ids=[peer['member_id']])
    goal_id=created['goal_id']
    request={**lead,**credentials,'goal_id':goal_id,'recipient_member_id':peer['member_id'],
        'expected_intent_revision':1,'coordinator_epoch':1,'expected_inbox_revision':s.room_consumers.inbox(lead,actor)['inbox_revision'],
        'purpose':'请核对同一原目标的检查顺序','decision_needed':'确认最小读取范围','idempotency_key':key()}
    sent=s.room_goal_handoffs.handoff(request,actor)
    assert len(s.store.all('SELECT * FROM team_handoffs'))==1
    assert len(s.store.all('SELECT * FROM coordination_messages'))==1
    assert s.room_goal_handoffs.handoff(request,actor)['handoff']['id']==sent['handoff']['id']
    inbox=s.room_consumers.inbox(peer,other)
    entry=next(i for i in inbox['items'] if i['kind']=='peer_handoff')
    assert entry['handoff']['purpose']==request['purpose'] and not entry['handoff']['context_limited']
    assert entry['handoff']['context']['original_request']['message_id']
    assert entry['handoff']['context']['member_projects']==[{
        'project_id':'proj','environment_id':'production','capabilities':['read'],'execution_targets':[]}]
    with pytest.raises(DevError) as loop:
        s.room_goal_handoffs.handoff({**peer,**peer_credentials,'goal_id':goal_id,'recipient_member_id':lead['member_id'],
            'expected_intent_revision':1,'coordinator_epoch':1,'expected_inbox_revision':inbox['inbox_revision'],
            'purpose':'不能绕圈','causation_id':sent['handoff']['id'],'hop_count':1,'idempotency_key':key()},other)
    assert loop.value.code=='TEAM_HANDOFF_LOOP'
    changed=control(s,owner,goal_id,'takeover',peer['member_id'])['work']
    assert changed['coordinator_epoch']==2
    assert not [i for i in s.room_consumers.inbox(peer,other)['items'] if i['kind']=='peer_handoff']
    assert len(s.store.all('SELECT * FROM team_handoffs'))==1
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_handoff_cross_project_never_returns_sender_result_context(collab):
    s,owner,a,b,lead,acred,peer,bcred,created,task=await two_project_members(collab)
    raw={**peer,**bcred,'goal_id':created['goal_id'],'recipient_member_id':lead['member_id'],
        'expected_intent_revision':1,'coordinator_epoch':1,'expected_inbox_revision':s.room_consumers.inbox(peer,b)['inbox_revision'],
        'purpose':'B 项目私有实现细节','decision_needed':'B 私有路径','idempotency_key':key()}
    s.room_goal_handoffs.handoff(raw,b)
    entry=next(i for i in s.room_consumers.inbox(lead,a)['items'] if i['kind']=='peer_handoff')
    assert entry['handoff']['context_limited'] and entry['handoff']['decision_needed']==''
    assert '私有' not in json.dumps(entry,ensure_ascii=False)


@pytest.mark.asyncio
async def test_unique_final_publication_rejects_changed_body_under_a_new_key(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    created,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    item=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(created['goal_id'],))
    lease,operation=admit_read(s,identity,credentials,actor,created['goal_id'],item,'proj')
    finished=report(s,identity,credentials,actor,created['goal_id'],lease,operation)
    source=s.store.one("SELECT * FROM collaboration_messages WHERE goal_id=? AND origin='panel_owner'",(created['goal_id'],))
    refs=[{'work_item_id':finished['id'],'version':finished['version'],'digest':digest(finished['result'])}]
    raw=final_request(s,identity,credentials,actor,source,refs)
    published=s.room_runtime.reply(raw,actor)
    assert s.room_runtime.reply({**raw,'idempotency_key':key()},actor)['message']['id']==published['message']['id']
    with pytest.raises(DevError) as conflict:
        s.room_runtime.reply({**raw,'body_text':'另一段结论','idempotency_key':key()},actor)
    assert conflict.value.code=='ROOM_PUBLICATION_CONFLICT'
    assert len(s.store.all('SELECT * FROM room_goal_publications'))==1
    assert len(s.store.all('SELECT * FROM team_publications'))==1


@pytest.mark.asyncio
async def test_takeover_tracks_inflight_operation_until_its_real_completion(collab):
    s,owner,actor,clock,lead,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    created,_=start(s,owner,actor,lead,credentials,project_ids=['proj'])
    goal_id=created['goal_id']
    old_item=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(goal_id,))
    lease,operation=admit_read(s,lead,credentials,actor,goal_id,old_item,'proj')
    # Model only transport delivery; all cancellation and terminal receipts
    # below run through the real original engine. No successful work is forged.
    s.store.execute("UPDATE operations SET state='running',attempts=1 WHERE id=?",(operation['operation_id'],))
    other=worker_for(s,actor,'inflight-replacement',scopes=('read',),projects=('proj',))
    _,_,other,_,replacement,new_credentials=await connected(collab,scopes=[{'project_id':'proj'}],
        worker_override=other,conversation_id=lead['conversation_id'])
    changed=control(s,owner,goal_id,'takeover',replacement['member_id'])['work']
    assert changed['state']=='stopping' and changed['recovery']['state']=='waiting_operations'
    assert changed['operations']==[{'id':operation['operation_id'],'state':'cancelling','cancel_requested':1,
        'display':{'title':'原操作 cancelling','reason':'cancel_requested_in_flight_not_proven_stopped'}}]
    waiting=continue_original(s,other,replacement,new_credentials,goal_id)
    assert waiting['reason_code']=='WORK_OPERATION_PENDING' and not waiting['scheduled']
    assert len(s.store.all('SELECT * FROM coordination_work'))==1
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(operation['operation_id'],)),
        {'ok':False,'error':{'code':'CANCELLED','message':'合成 Agent 返回真实取消回执'}})
    continued=continue_original(s,other,replacement,new_credentials,goal_id)
    assert continued['scheduled'] and continued['continued_work_item_id']!=old_item['id']
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==1
    assert s.store.one('SELECT state FROM operations WHERE id=?',(operation['operation_id'],))['state']=='cancelled'


@pytest.mark.asyncio
async def test_recovery_cannot_extend_timed_original_member_approval(collab):
    s,owner,actor,clock,lead,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    # Approve a shorter original lifetime through the owner API, then establish
    # a fresh current consumer using the real staged binding flow.
    room=s.rooms.read({'conversation_id':lead['conversation_id']},owner)['room']
    deadline=clock[0]+600
    s.rooms.projects({'conversation_id':room['id'],'expected_version':room['version'],
        'projects':[{'project':'proj'}],'member_approvals':[{'member_id':lead['member_id'],
            'project_scopes':[{'project_id':'proj'}],'lifetime':{'mode':'timed','expires_at':deadline}}],
        'idempotency_key':key()},owner)
    lead,credentials=await rebind(s,owner,lead,actor)
    created,_=start(s,owner,actor,lead,credentials,project_ids=['proj'])
    goal_id=created['goal_id']
    original=s.room_runtime.goal(goal_id)
    room=s.rooms.read({'conversation_id':lead['conversation_id']},owner)['room']
    s.rooms.projects({'conversation_id':room['id'],'expected_version':room['version'],
        'projects':[{'project':'proj'}],'member_approvals':[{'member_id':lead['member_id'],
            'project_scopes':[{'project_id':'proj'}],'lifetime':{'mode':'until_revoked'}}],
        'idempotency_key':key()},owner)
    # A stronger/newer current approval still cannot renew this existing Goal.
    clock[0]+=61  # Respect the production verification challenge window.
    current,new_credentials=await rebind(s,owner,lead,actor)
    control(s,owner,goal_id,'restore_binding',lead['member_id'])
    spec=s.room_work_bridge.native.record(s.room_work_bridge.native.reference(s.room_runtime.goal(goal_id)))
    assert spec['members'][0]['projects'][0]['expires_at']==deadline
    assert s.room_runtime.goal(goal_id)['expires_at']==original['expires_at']
    clock[0]=deadline+1
    with pytest.raises(DevError):
        continue_original(s,actor,current,new_credentials,goal_id)
    assert not s.room_runtime.work_view(s.room_runtime.goal(goal_id),owner)['recovery']['restore_candidates']
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_large_handoff_has_bounded_inbox_and_authenticated_passive_read(collab):
    from hub.collaboration.common import canonical
    from hub.collaboration.dot_mailbox import PAGE_BYTE_BUDGET
    s,owner,actor,clock,lead,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    other=worker_for(s,actor,'large-handoff-peer',scopes=('read',),projects=('proj',))
    _,_,other,_,peer,peer_credentials=await connected(collab,scopes=[{'project_id':'proj'}],worker_override=other,conversation_id=lead['conversation_id'])
    source=s.room_runtime.send({'conversation_id':lead['conversation_id'],'member_id':lead['member_id'],
        'body_text':'原始要求'*950,'client_message_id':key(),'idempotency_key':key()},owner)['message']
    created=s.room_runtime.start_work({**lead,**credentials,'source_message_id':source['id'],'source_version':source['version'],
        'source_digest':digest(source['body_text']),'expected_inbox_revision':s.room_consumers.inbox(lead,actor)['inbox_revision'],
        'target_project_id':'proj','participant_member_ids':[peer['member_id']],'acceptance':'验收条件'*475,
        'confirm_task':True,'idempotency_key':key()},actor)
    item=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(created['goal_id'],))
    sent=s.room_goal_handoffs.handoff({**lead,**credentials,'goal_id':created['goal_id'],
        'recipient_member_id':peer['member_id'],'work_item_id':item['id'],'expected_intent_revision':1,'coordinator_epoch':1,
        'expected_inbox_revision':s.room_consumers.inbox(lead,actor)['inbox_revision'],
        'purpose':'具体目的'*250,'decision_needed':'需要决定'*250,'idempotency_key':key()},actor)
    inbox=s.room_consumers.inbox(peer,other)
    entry=next(i for i in inbox['items'] if i['kind']=='peer_handoff')
    assert len(canonical(inbox).encode())<PAGE_BYTE_BUDGET
    assert entry['read_action']=='room_work_handoff_read' and entry['content_requires_read']
    assert '"connection_proof":' not in canonical(inbox)
    assert peer['connection_proof'] not in canonical(inbox) and lead['connection_proof'] not in canonical(inbox)
    request=checked_request('collaboration_room_work_handoff_read',{**peer,'handoff_id':sent['handoff']['id']})
    full=s.invoke(request['tool'],request['arguments'],other)
    assert full['read_only'] and not full['read_is_consumption']
    assert full['handoff']['context']['original_request']['body_text']==source['body_text']
    assert full['handoff']['context']['work_item']['acceptance']=='验收条件'*475
    with pytest.raises(DevError):
        s.room_goal_handoffs.query({**peer,'connection_proof':lead['connection_proof'],'handoff_id':sent['handoff']['id']},other)
    paused=s.room_runtime.control({'work_id':created['goal_id'],'action':'pause',
        'expected_version':s.room_runtime.goal(created['goal_id'])['version'],'idempotency_key':key()},owner)
    assert paused['work']['goal_state']=='paused'
    with pytest.raises(DevError):
        s.invoke(request['tool'],request['arguments'],other)
