"""One native Goal uses exact member/project scopes and original operations."""
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import digest
from shared.util import DevError
from tests.collaboration_support import collab as collab,key
from tests.legacy_iam_fixture import seed_grant
from tests.test_room_managed_work import connected,current_epoch,save_room_permissions


def worker_for(s,worker,name,scopes=('read','write'),projects=('proj','otherproj')):
    seed_grant(s.store,name,'owner',scopes=scopes,projects=projects)
    return replace(worker,grant_id=name,actor='mcp:'+name+':fixture',scopes=set(scopes),projects=list(projects))



async def renew_consumer(s, identity, actor, old_credentials):
    # Room-wide revocation fences every old turn, even when this host's
    # original work is unaffected. Recover via the real subscription wake.
    with pytest.raises(DevError) as stale:
        s.room_consumers.authorize_member({**identity, **old_credentials}, actor, require_revision=False)
    assert stale.value.code in {'DOT_CONSUMER_SCOPE_CHANGED', 'DOT_CONSUMER_FENCE_LOST'}
    recovered = s.room_consumers.request_recovery({**identity, 'idempotency_key': key()}, actor)
    assert recovered['wakeup_queued']
    await s.events.tick()
    ticket = next(payload['data']['wake_ticket'] for payload in reversed(s.native_test_callbacks)
        if payload['data'].get('member_id') == identity['member_id'])
    session = s.room_consumers.begin({**identity, 'wake_ticket': ticket,
        'consumer_run_id': key(), 'idempotency_key': key()}, actor)
    assert session['consumer_fence'] > old_credentials['consumer_fence']
    return {k: session[k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}

def mutation(s,identity,credentials,goal_id,*,project='proj'):
    return {'project':project,'environment_id':'production','goal_id':goal_id,**credentials,
        'expected_inbox_revision':s.room_consumers.inbox(identity,s.rooms.principal(s.rooms.member(identity['member_id'])))['inbox_revision'],
        'coordinator_epoch':current_epoch(s,goal_id),
        'expected_intent_revision':s.room_runtime.intent_revision(goal_id),'idempotency_key':key()}


def start(s,owner,actor,identity,credentials,**extra):
    message=s.room_runtime.send({'conversation_id':identity['conversation_id'],'member_id':identity['member_id'],
        'body_text':'读取 A 项目的说明，并修改 B 项目的文档；请在原目标内协作。','client_message_id':key(),'idempotency_key':key()},owner)['message']
    request={**identity,**credentials,'source_message_id':message['id'],'source_version':message['version'],
        'source_digest':digest(message['body_text']),'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'target_project_id':'proj','project_ids':['proj','otherproj'],'acceptance':'报告真实读取和修改结果',
        'confirm_task':True,'idempotency_key':key(),**extra}
    return s.room_runtime.start_work(request,actor),request


@pytest.mark.asyncio
async def test_one_goal_applies_capabilities_per_project_and_never_forks_source(collab):
    s,owner,worker,*_=collab
    actor=worker_for(s,worker,'room-wide')
    scopes=[{'project_id':'proj','capabilities':['read']}, {'project_id':'otherproj','capabilities':['read','write']}]
    s,owner,actor,clock,identity,credentials=await connected(collab,
        [{'project':'proj'},{'project':'otherproj'}],scopes,worker_override=actor)
    result,request=start(s,owner,actor,identity,credentials)
    assert set(result['goal']['project_ids'])=={'proj','otherproj'}
    assert result['goal']['capabilities']==['read','write']
    raw={**mutation(s,identity,credentials,result['goal_id']),'objective':'修改文档','acceptance':'真实回执',
        'assignee_grant_id':actor.grant_id,'assignee_member_id':identity['member_id'],'required_capabilities':['read','write']}
    with pytest.raises(DevError,match='能力'):
        s.coordination.work_create({**raw,'target_project_id':'proj'},actor)
    item=s.coordination.work_create({**raw,'target_project_id':'otherproj'},actor)['work_item']
    claimed=s.coordination.work_claim({**mutation(s,identity,credentials,result['goal_id']),
        'work_item_id':item['id'],'expected_version':item['version']},actor)['work_item']
    lease={**mutation(s,identity,credentials,result['goal_id']),'work_item_id':item['id'],
        'attempt':claimed['attempt'],'fencing_token':claimed['fencing_token']}
    with pytest.raises(DevError,match='唯一项目'):
        s.coordination.admit({**lease,'tool':'write','arguments':{'project':'proj','path':'README.md','content':'bad'}},actor)
    admitted=s.coordination.admit({**lease,'tool':'write','arguments':{'path':'README.md','content':'approved B change','expected_sha256':'new'}},actor)
    assert s.store.one('SELECT project_id FROM operations WHERE id=?',(admitted['operation_id'],))['project_id']=='otherproj'
    again=s.room_runtime.start_work({**request,'project_ids':['proj'],'idempotency_key':key(),
        'coordinator_epoch':current_epoch(s,result['goal_id']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']},actor)
    assert again['goal_id']==result['goal_id'] and again['scope_revision_required']
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM delegation_requests'))==1


@pytest.mark.asyncio
async def test_same_goal_work_handoff_uses_real_second_member_consumer(collab):
    s,owner,worker,*_=collab
    first=worker_for(s,worker,'room-first',scopes=('read',),projects=('proj',))
    second=worker_for(s,worker,'room-second',scopes=('read',),projects=('otherproj',))
    projects=[{'project':'proj'},{'project':'otherproj'}]
    rules=[{'project_id':'proj','capabilities':['read']},{'project_id':'otherproj','capabilities':['read']}]
    s,owner,first,clock,lead,lead_credentials=await connected(collab,projects,rules,worker_override=first)
    _,_,second,_,peer,peer_credentials=await connected(collab,worker_override=second,conversation_id=lead['conversation_id'])
    result,_=start(s,owner,first,lead,lead_credentials,participant_member_ids=[peer['member_id']])
    source=s.store.one('SELECT room_id FROM collaboration_messages WHERE id=(SELECT message_id FROM delegation_requests WHERE goal_id=?)',(result['goal_id'],))
    assert source['room_id'] is None
    assert result['goal']['project_ids']==['proj']  # A-only leader receives minimum B state, never B contents.
    task=s.coordination.work_create({**mutation(s,lead,lead_credentials,result['goal_id']),
        'target_project_id':'otherproj','objective':'核对 B 文档','acceptance':'报告事实',
        'assignee_grant_id':second.grant_id,'assignee_member_id':peer['member_id']},first)['work_item']
    assert any(item.get('reference',{}).get('work_item_id')==task['id'] for item in s.room_consumers.inbox(peer,second)['items'])
    with pytest.raises(DevError):
        s.coordination.work_claim({**mutation(s,lead,lead_credentials,result['goal_id']),
            'work_item_id':task['id'],'expected_version':task['version']},first)
    claimed=s.coordination.work_claim({**mutation(s,peer,peer_credentials,result['goal_id'],project='otherproj'),
        'work_item_id':task['id'],'expected_version':task['version']},second)['work_item']
    lease={**mutation(s,peer,peer_credentials,result['goal_id'],project='otherproj'),'work_item_id':task['id'],
        'attempt':claimed['attempt'],'fencing_token':claimed['fencing_token']}
    op=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'}},second)
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],)),{'ok':True,'data':{'content':'B facts'}})
    result=s.coordination.result({**lease,'idempotency_key':key(),'outcome':'succeeded','summary':'B 文档已核对',
        'operation_ids':[op['operation_id']]},second)
    assert result['work_item']['state']=='succeeded'
    message=s.store.one("SELECT body FROM collaboration_messages WHERE kind='delegation_result'")
    assert json.loads(message['body'])['sender_member_id']==peer['member_id']
    lead_view=s.coordination.read({k:v for k,v in mutation(s,lead,lead_credentials,task['goal_id']).items() if k!='idempotency_key'},first)
    assert task['id'] not in {item['id'] for item in lead_view['work_items']}
    assert any(item['id']==task['id'] and item['state']=='succeeded' for item in lead_view['related_work'])
    assert 'B 文档已核对' not in json.dumps(lead_view)
    peer_view=s.coordination.read({k:v for k,v in mutation(s,peer,peer_credentials,task['goal_id'],project='otherproj').items() if k!='idempotency_key'},second)
    assert peer_view['goal']['project_ids']==['otherproj']
    assert all(item['project_id']=='otherproj' for item in peer_view['work_items'])
    with pytest.raises(DevError):
        s.task_liveness.evidence({'project':'proj','environment_id':'production','goal_id':task['goal_id'],'work_item_id':task['id']},first)
    initial=s.store.one('SELECT * FROM coordination_work WHERE goal_id=? AND responsibility=?',(task['goal_id'],'coordinator'))
    initial_lease,initial_op=admit_read(s,lead,lead_credentials,first,task['goal_id'],initial,'proj')
    report(s,lead,lead_credentials,first,task['goal_id'],initial_lease,initial_op)
    source=s.store.one('SELECT * FROM collaboration_messages WHERE goal_id=? AND origin=?',(task['goal_id'],'panel_owner'))
    refs=[{'work_item_id':row['id'],'version':row['version'],'digest':digest(json.loads(row['result']))}
        for row in s.store.all('SELECT * FROM coordination_work WHERE goal_id=?',(task['goal_id'],))]
    with pytest.raises(DevError):
        s.room_runtime.reply(final_request(s,peer,peer_credentials,second,source,refs),second)
    with pytest.raises(DevError) as inaccessible:
        s.room_runtime.reply(final_request(s,lead,lead_credentials,first,source,refs),first)
    assert inaccessible.value.code == 'ROOM_WORK_ARTIFACT_NOT_VISIBLE'
    assert not s.store.all('SELECT * FROM team_publications')
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1


@pytest.mark.asyncio
async def test_owner_supplement_fences_old_attempt_and_continues_same_goal(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    result,raw=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    claim={k:v for k,v in result['next_request']['arguments'].items() if k!='action'}
    item=s.coordination.work_claim(claim,actor)['work_item']
    old_lease={**mutation(s,identity,credentials,result['goal_id']),'work_item_id':item['id'],
        'attempt':item['attempt'],'fencing_token':item['fencing_token']}
    revised=s.room_runtime.control({'work_id':result['goal_id'],'expected_version':result['goal']['version'],
        'action':'supplement','body_text':'补充：只需要检查标题，不需要读取全文。','idempotency_key':key()},owner)
    assert revised['work']['intent_revision']==2
    with pytest.raises(DevError):
        s.coordination.admit({**old_lease,'tool':'read','arguments':{'path':'README.md'}},actor)
    source=s.store.one("SELECT * FROM collaboration_messages WHERE json_extract(body,'$.work_id')=?",(result['goal_id'],))
    request={**identity,**credentials,'source_message_id':source['id'],'source_version':source['version'],
        'coordinator_epoch':current_epoch(s,result['goal_id']),
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'acceptance':'只报告标题','confirm_task':True,'idempotency_key':key()}
    resumed=s.room_runtime.start_work(request,actor)
    assert resumed['goal_id']==result['goal_id'] and resumed['intent_revision']==2 and resumed['scheduled']
    assert resumed['continued_work_item_id']!=item['id']
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(item['id'],))['state']=='blocked'
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_grant_intersection_preserves_chat_join_without_expanding_approved_scope(collab):
    s,owner,worker,*_=collab
    rules=[{'project_id':'proj','capabilities':['read','write']},{'project_id':'otherproj','capabilities':['read']}]
    s,owner,actor,clock,identity,credentials=await connected(collab,
        [{'project':'proj'},{'project':'otherproj'}],rules)
    member=s.rooms.member(identity['member_id'])
    assert s.rooms.state(member)=='ready_work'
    bridges=s.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'",(member['id'],))
    assert [r['project_id'] for r in bridges]==['proj']
    policy=s.store.one('SELECT * FROM delegation_policies WHERE id=?',(bridges[0]['policy_id'],))
    assert json.loads(policy['spec'])['capabilities']==['read']
    original=s.store.one('SELECT capabilities FROM room_member_scopes WHERE member_id=? AND project_id=?',(member['id'],'proj'))
    assert json.loads(original['capabilities'])==['read','write']
    result,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    assert result['goal']['capabilities']==['read']
    with pytest.raises(DevError):
        s.coordination.work_create({**mutation(s,identity,credentials,result['goal_id']),
            'target_project_id':'proj','objective':'不能借批准扩大当前 grant','acceptance':'拒绝越权',
            'required_capabilities':['read','write'],'assignee_grant_id':actor.grant_id,
            'assignee_member_id':identity['member_id']},actor)


async def two_project_members(collab):
    s,owner,worker,*_=collab
    a=worker_for(s,worker,'only-a',scopes=('read',),projects=('proj',))
    b=worker_for(s,worker,'only-b',scopes=('read',),projects=('otherproj',))
    s,owner,a,clock,lead,lead_credentials=await connected(collab,[{'project':'proj'},{'project':'otherproj'}],
        [{'project_id':'proj'},{'project_id':'otherproj'}],worker_override=a)
    _,_,b,_,peer,peer_credentials=await connected(collab,
        worker_override=b,conversation_id=lead['conversation_id'])
    goal,request=start(s,owner,a,lead,lead_credentials,participant_member_ids=[peer['member_id']])
    task=s.coordination.work_create({**mutation(s,lead,lead_credentials,goal['goal_id']),
        'target_project_id':'otherproj','objective':'检查 B 文档','acceptance':'报告实际读取',
        'assignee_grant_id':b.grant_id,'assignee_member_id':peer['member_id']},a)['work_item']
    return s,owner,a,b,lead,lead_credentials,peer,peer_credentials,goal,task


def admit_read(s,identity,credentials,actor,goal_id,item,project):
    claimed=s.coordination.work_claim({**mutation(s,identity,credentials,goal_id,project=project),
        'work_item_id':item['id'],'expected_version':item['version']},actor)['work_item']
    lease={**mutation(s,identity,credentials,goal_id,project=project),'work_item_id':item['id'],
        'attempt':claimed['attempt'],'fencing_token':claimed['fencing_token']}
    op=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'}},actor)
    return lease,op


def report(s,identity,credentials,actor,goal_id,lease,operation,*,outcome='succeeded'):
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(operation['operation_id'],)),
        {'ok':True,'data':{'content':'# Real fixture receipt'}} if outcome=='succeeded' else
        {'ok':False,'error':{'code':'FIXTURE_READ_FAILED','message':'合成读取失败'}})
    return s.coordination.result({**lease,'idempotency_key':key(),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'outcome':outcome,'summary':'已核对真实原操作回执','operation_ids':[operation['operation_id']]},actor)['work_item']


@pytest.mark.asyncio
@pytest.mark.parametrize('removed',['proj','otherproj'])
async def test_removing_one_project_blocks_only_its_work_even_when_it_was_the_anchor(collab,removed):
    s,owner,a,b,lead,acred,peer,bcred,goal,task=await two_project_members(collab)
    first=s.store.one('SELECT * FROM coordination_work WHERE id=?',(goal['next_request']['arguments']['work_item_id'],))
    alease,aop=admit_read(s,lead,acred,a,goal['goal_id'],first,'proj')
    blease,bop=admit_read(s,peer,bcred,b,goal['goal_id'],task,'otherproj')
    remaining='otherproj' if removed=='proj' else 'proj'
    save_room_permissions(s,owner,lead['conversation_id'],[{'project_id':remaining}])
    blocked=first if removed=='proj' else task
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(blocked['id'],))['state']=='blocked'
    assert s.room_runtime.goal(goal['goal_id'])['state']=='active'
    identity,credentials,actor,lease,op=(peer,bcred,b,blease,bop) if remaining=='otherproj' else (lead,acred,a,alease,aop)
    assert s.store.one('SELECT cancel_requested FROM operations WHERE id=?',(op['operation_id'],))['cancel_requested']==0
    collab[6][0] += 91  # Original 90s consumer turn expires before the 300s work lease.
    credentials=await renew_consumer(s,identity,actor,credentials)
    lease={**lease,**credentials}
    done=report(s,identity,credentials,actor,goal['goal_id'],lease,op)
    assert done['state']=='succeeded'
    old_identity,old_credentials,old_actor,old_lease=(lead,acred,a,alease) if removed=='proj' else (peer,bcred,b,blease)
    with pytest.raises(DevError):
        s.coordination.admit({**old_lease,'idempotency_key':key(),'tool':'read','arguments':{'path':'README.md'}},old_actor)
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==2


@pytest.mark.asyncio
async def test_resume_keeps_original_operation_and_waits_for_new_fenced_intent(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    goal,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    item=s.store.one('SELECT * FROM coordination_work WHERE id=?',(goal['next_request']['arguments']['work_item_id'],))
    lease,op=admit_read(s,identity,credentials,actor,goal['goal_id'],item,'proj')
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],)),{'ok':True,'data':{'content':'已读内容'}})
    paused=s.room_runtime.control({'work_id':goal['goal_id'],'expected_version':s.room_runtime.goal(goal['goal_id'])['version'],
        'action':'pause','idempotency_key':key()},owner)['work']
    resumed=s.room_runtime.control({'work_id':goal['goal_id'],'expected_version':paused['version'],
        'action':'resume','idempotency_key':key()},owner)['work']
    assert resumed['state']=='waiting_member' and resumed['intent_revision']==2
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(item['id'],))['state']=='blocked'
    source=s.store.one("SELECT * FROM collaboration_messages WHERE json_extract(body,'$.work_id')=?",(goal['goal_id'],))
    continued=s.room_runtime.start_work({**identity,**credentials,'source_message_id':source['id'],'source_version':source['version'],
        'coordinator_epoch':current_epoch(s,goal['goal_id']),
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'acceptance':'先复用原操作回执','confirm_task':True,'idempotency_key':key()},actor)
    assert continued['goal_id']==goal['goal_id'] and continued['continued_work_item_id']!=item['id']
    assert len(s.store.all('SELECT * FROM operations'))==1


def final_request(s,identity,credentials,actor,source,refs,**extra):
    return {**identity,**credentials,'source_message_id':source['id'],'source_version':source['version'],
        'coordinator_epoch':current_epoch(s,source.get('goal_id') or json.loads(source['body'])['work_id']),
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'expected_intent_revision':1,'outcome':'succeeded','artifact_refs':refs,
        'body_text':'按所有当前工作结果汇总。','complete':True,'idempotency_key':key(),**extra}


@pytest.mark.asyncio
async def test_final_publication_reuses_original_artifact_evidence_and_is_unique(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    goal,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    item=s.store.one('SELECT * FROM coordination_work WHERE id=?',(goal['next_request']['arguments']['work_item_id'],))
    source=s.store.one('SELECT * FROM collaboration_messages WHERE goal_id=? AND origin=?',(goal['goal_id'],'panel_owner'))
    lease,op=admit_read(s,identity,credentials,actor,goal['goal_id'],item,'proj')
    with pytest.raises(DevError,match='未完成或不确定'):
        s.room_runtime.reply(final_request(s,identity,credentials,actor,source,[]),actor)
    done=report(s,identity,credentials,actor,goal['goal_id'],lease,op)
    ref={'work_item_id':done['id'],'version':done['version'],'digest':digest(done['result'])}
    with pytest.raises(DevError,match='全部已有结果'):
        s.room_runtime.reply(final_request(s,identity,credentials,actor,source,[]),actor)
    with pytest.raises(DevError,match='最新结果版本'):
        s.room_runtime.reply(final_request(s,identity,credentials,actor,source,[{**ref,'version':ref['version']-1}]),actor)
    request=final_request(s,identity,credentials,actor,source,[ref])
    final=s.room_runtime.reply(request,actor)
    again=s.room_runtime.reply({**request,'idempotency_key':key()},actor)
    assert again['message']['id']==final['message']['id']
    assert len(s.store.all('SELECT * FROM team_publications WHERE goal_id=?',(goal['goal_id'],)))==1
    assert len(s.store.all('SELECT * FROM room_goal_publications WHERE goal_id=?',(goal['goal_id'],)))==1
    s.room_runtime.control({'work_id':goal['goal_id'],'expected_version':s.room_runtime.goal(goal['goal_id'])['version'],
        'action':'supplement','body_text':'改为仅检查标题','idempotency_key':key()},owner)
    with pytest.raises(DevError,match='意图'):
        s.room_runtime.reply({**request,'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']},actor)
    with pytest.raises(DevError,match='当前意图'):
        s.room_runtime.reply({**request,'expected_intent_revision':2,'idempotency_key':key(),
            'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']},actor)


@pytest.mark.asyncio
async def test_failed_work_can_only_publish_explicit_partial_or_blocked_result(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    goal,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    item=s.store.one('SELECT * FROM coordination_work WHERE id=?',(goal['next_request']['arguments']['work_item_id'],))
    lease,op=admit_read(s,identity,credentials,actor,goal['goal_id'],item,'proj')
    done=report(s,identity,credentials,actor,goal['goal_id'],lease,op,outcome='failed')
    source=s.store.one('SELECT * FROM collaboration_messages WHERE goal_id=? AND origin=?',(goal['goal_id'],'panel_owner'))
    ref={'work_item_id':done['id'],'version':done['version'],'digest':digest(done['result'])}
    request=final_request(s,identity,credentials,actor,source,[ref])
    with pytest.raises(DevError,match='不能声明全部完成'):
        s.room_runtime.reply(request,actor)
    saved=s.room_runtime.reply({**request,'outcome':'blocked','idempotency_key':key()},actor)
    body=json.loads(s.store.one('SELECT body FROM collaboration_messages WHERE id=?',(saved['message']['id'],))['body'])
    assert body['outcome']=='blocked'
    assert s.room_runtime.work_view(s.room_runtime.goal(goal['goal_id']),owner)['state']=='blocked'


@pytest.mark.asyncio
async def test_explicit_persistent_scope_has_no_seven_day_fallback_after_pause(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    member=s.rooms.member(identity['member_id'])
    bridge,_=s.room_work_bridge.adapter(member,'proj')
    policy=s.store.one('SELECT * FROM delegation_policies WHERE id=?',(bridge['policy_id'],))
    assert policy['expires_at']>clock[0] and s.room_work_bridge.persistent(policy)
    flow=s.store.one('SELECT * FROM room_connect_flows WHERE member_id=?',(member['id'],))
    paused=s.rooms.connect_action({'flow_id':flow['id'],'expected_version':flow['version'],
        'action':'pause','idempotency_key':key()},owner)
    assert s.channel_owner.expired(policy)  # Even a stale row cannot fall back to its old deadline.
    resumed=s.rooms.connect_action({'flow_id':flow['id'],'expected_version':paused['flow']['version'],
        'action':'resume','idempotency_key':key()},owner)
    current=s.store.one('SELECT * FROM delegation_policies WHERE id=?',(policy['id'],))
    assert s.room_work_bridge.persistent(current)
    clock[0]+=8*86400
    assert not s.channel_owner.expired(current)


@pytest.mark.asyncio
async def test_native_peer_mention_is_readable_notice_not_fake_handoff(collab):
    s,owner,a,b,lead,acred,peer,bcred,created,task=await two_project_members(collab)
    goal_id=created['goal_id']
    sent=s.coordination.message({**mutation(s,lead,acred,goal_id),
        'body_text':'Please inspect your portion', 'mention_grant_ids':[b.grant_id]},a)
    items=s.room_consumers.inbox(peer,b)['items']
    notice=next(row for row in items if row['object_id']==sent['message_id'])
    assert notice['kind']=='control_notice'
    assert notice['reference']['goal_id']==goal_id
    assert s.room_runtime.work_view(s.room_runtime.goal(goal_id),a,member_id=lead['member_id'])['allowed_actions']==[]
    assert 'pause' in s.room_runtime.work_view(s.room_runtime.goal(goal_id),owner)['allowed_actions']


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [28, 128])
async def test_large_room_work_scope_starts_and_keeps_exact_project_boundary(collab, count):
    s, owner, worker, *_ = collab
    ids = ['proj', 'otherproj'] + [f'project-{i:03}' for i in range(count - 2)]
    for pid in ids[2:]:
        s.store.execute("""INSERT INTO projects
            (id,alias,alias_key,device_id,root,mode,allow_tasks,created)
            VALUES (?,?,?,'dev','/tmp/fixture','write',1,?)""",
            (pid, pid, pid, collab[6][0]))
    actor = worker_for(s, worker, 'large-room', scopes=('read',), projects=ids)
    scopes = [{'project_id': pid, 'capabilities': ['read']} for pid in ids]
    s, owner, actor, _, identity, credentials = await connected(collab,
        [{'project': pid} for pid in ids], scopes, worker_override=actor)
    result, request = start(s, owner, actor, identity, credentials, project_ids=ids)
    assert set(result['goal']['project_ids']) == set(ids)
    with pytest.raises(DevError) as stale:
        s.room_runtime.start_work(request, actor)
    assert stale.value.code == 'ROOM_INBOX_CHANGED'
    replay = s.room_runtime.start_work({**request, 'idempotency_key': key(),
        'coordinator_epoch': current_epoch(s, result['goal_id']),
        'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision']}, actor)
    assert replay['goal_id'] == result['goal_id']
    assert len(s.store.all('SELECT * FROM coordination_goals')) == 1
    raw = {**mutation(s, identity, credentials, result['goal_id']),
        'objective': 'Read the final selected project', 'acceptance': 'Report its facts',
        'assignee_grant_id': actor.grant_id, 'assignee_member_id': identity['member_id'],
        'required_capabilities': ['read']}
    work = s.coordination.work_create({**raw, 'target_project_id': ids[-1]}, actor)['work_item']
    assert work['project_id'] == ids[-1]
    with pytest.raises(DevError):
        s.coordination.work_create({**raw, 'idempotency_key': key(),
            'target_project_id': 'not-approved'}, actor)
    assert len(s.store.all('SELECT * FROM coordination_work WHERE goal_id=?',
        (result['goal_id'],))) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False,True])
async def test_owner_can_discuss_the_original_work_without_revising_it(collab, explicit):
    s,owner,actor,_,identity,credentials=await connected(
        collab,[{'project':'proj'}],[{'project_id':'proj'}])
    result,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    goal_id=result['goal_id']
    source=s.store.one('SELECT id FROM collaboration_messages WHERE goal_id=? AND origin=?',
        (goal_id,'panel_owner'))
    assert s.room_runtime.work_read(goal_id,owner)['work']['original_source_message_id']==source['id']
    original=s.room_runtime.goal(goal_id)
    if explicit:
        linked={'work_id':goal_id,'revise_work_intent':False}
    else:
        linked={'reply_to_id':source['id']}
    discussion=s.room_runtime.send({'conversation_id':identity['conversation_id'],
        'body_text':'为什么这样安排？','idempotency_key':key(),'client_message_id':key(),**linked},owner)
    assert discussion['message']['work_id']==goal_id
    assert s.room_runtime.intent_revision(goal_id)==1
    assert s.room_runtime.goal(goal_id)['version']==original['version']
    assert s.room_runtime.goal(goal_id)['state']==original['state']
    inbox=s.room_consumers.inbox(identity,actor)
    assert any(item['object_id']==discussion['message']['id'] for item in inbox['items'])
    assert len(s.store.all('SELECT id FROM coordination_goals'))==1


@pytest.mark.asyncio
async def test_same_project_alternate_environment_cannot_read_original_work(collab):
    s,owner,actor,_,identity,credentials=await connected(collab,
        [{'project':'proj','environment_id':'production'},{'project':'proj','environment_id':'staging'}],
        [{'project_id':'proj','environment_id':environment} for environment in ('production','staging')])
    result,_=start(s,owner,actor,identity,credentials,project_ids=['proj'],environment_id='staging')
    assert result['next_request']['arguments']['environment_id']=='staging'
    request={'goal_id':result['goal_id'],'project':'proj','environment_id':'production'}
    with pytest.raises(DevError) as denied:
        s.coordination.read(request,actor)
    assert denied.value.code=='GOAL_PROJECT_DENIED'
    detail=s.coordination.read({**request,'environment_id':'staging'},actor)
    assert detail['goal']['environment_id']=='staging'
    assert {rule['environment_id'] for rule in detail['room_work_context']['scope']['work_routes']}=={'staging'}
