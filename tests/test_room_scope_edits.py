"""Adding one approved project does not rewrite another project's authority."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab,key
from tests.test_room_managed_work import connected, save_room_permissions
from tests.test_room_goal_scope_boundaries import worker_for,start,admit_read,report
from tests.test_room_goal_recovery import rebind


def approve(s,owner,identity,scopes,**extra):
    return save_room_permissions(s,owner,identity['conversation_id'],scopes,**extra)


@pytest.mark.asyncio
async def test_unchanged_scope_keeps_exact_evidence_and_consumer(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    before=s.store.one('SELECT * FROM room_member_scopes WHERE member_id=?',(identity['member_id'],))
    consumer=s.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',(credentials['consumer_session_id'],))
    bridge=s.store.one('SELECT * FROM room_member_work_bridges WHERE member_id=?',(identity['member_id'],))
    clock[0]+=1
    approve(s,owner,identity,[{'project_id':'proj'}])
    assert s.store.one('SELECT * FROM room_member_scopes WHERE member_id=?',(identity['member_id'],))==before
    current_bridge=s.store.one('SELECT * FROM room_member_work_bridges WHERE member_id=?',(identity['member_id'],))
    assert {k:v for k,v in current_bridge.items() if k!='updated'}=={k:v for k,v in bridge.items() if k!='updated'}
    assert s.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',(credentials['consumer_session_id'],))==consumer
    assert s.room_consumers.heartbeat({**identity,**credentials,'idempotency_key':key()},actor)['state']=='active'


@pytest.mark.asyncio
async def test_added_scope_preserves_original_goal_operation_deadline_and_fence(collab):
    s,owner,worker,*_=collab
    actor=worker_for(s,worker,'scope-edit-wide',scopes=('read',),projects=('proj','otherproj'))
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],
        [{'project_id':'proj'}],worker_override=actor)
    deadline=clock[0]+600
    approve(s,owner,identity,[{'project_id':'proj'}],lifetime={'mode':'timed','expires_at':deadline})
    identity,credentials=await rebind(s,owner,identity,actor)
    created,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    goal_id=created['goal_id']
    item=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(goal_id,))
    lease,operation=admit_read(s,identity,credentials,actor,goal_id,item,'proj')
    old_scope=s.store.one("SELECT * FROM room_member_scopes WHERE member_id=? AND project_id='proj'",(identity['member_id'],))
    old_consumer=s.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',(credentials['consumer_session_id'],))
    old_bridge=s.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND project_id='proj' AND state='active'",(identity['member_id'],))
    old_goal=s.room_runtime.goal(goal_id)
    approve(s,owner,identity,[{'project_id':'proj'},{'project_id':'otherproj'}])
    assert s.store.one("SELECT * FROM room_member_scopes WHERE member_id=? AND project_id='proj'",(identity['member_id'],))==old_scope
    assert s.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND project_id='proj' AND state='active'",(identity['member_id'],))==old_bridge
    current=s.store.one('SELECT * FROM dot_consumer_sessions WHERE id=?',(credentials['consumer_session_id'],))
    assert current['state']=='active' and current['fence']==old_consumer['fence'] and current['proof_hash']==old_consumer['proof_hash']
    assert current['scope_digest_at_begin'] == old_consumer['scope_digest_at_begin']
    assert current['observed_scope_digest'] != old_consumer['observed_scope_digest']
    assert s.store.one('SELECT cancel_requested FROM operations WHERE id=?',(operation['operation_id'],))['cancel_requested']==0
    assert s.room_runtime.goal(goal_id)['spec']==old_goal['spec'] and s.room_runtime.goal(goal_id)['expires_at']==old_goal['expires_at']
    assert json.loads(old_goal['spec'])['project_ids']==['proj']
    added=s.store.one("SELECT * FROM room_member_scopes WHERE member_id=? AND project_id='otherproj'",(identity['member_id'],))
    assert added['lifetime_mode']=='until_revoked' and added['expires_at'] is None
    with pytest.raises(DevError) as stale:
        s.coordination.heartbeat({**lease,'idempotency_key':key()},actor)
    assert stale.value.code=='ROOM_INBOX_CHANGED'
    lease['expected_inbox_revision']=s.room_consumers.inbox(identity,actor)['inbox_revision']
    assert s.coordination.heartbeat({**lease,'idempotency_key':key()},actor)['work_item']['fencing_token']==item['fencing_token']+1
    assert report(s,identity,credentials,actor,goal_id,lease,operation)['state']=='succeeded'
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==1


@pytest.mark.asyncio
async def test_explicit_scope_revoke_stops_original_work_and_expires_old_turn(collab):
    s,owner,actor,clock,identity,credentials=await connected(collab,[{'project':'proj'}],[{'project_id':'proj'}])
    created,_=start(s,owner,actor,identity,credentials,project_ids=['proj'])
    item=s.store.one('SELECT * FROM coordination_work WHERE goal_id=?',(created['goal_id'],))
    lease,operation=admit_read(s,identity,credentials,actor,created['goal_id'],item,'proj')
    approve(s,owner,identity,[])
    assert s.store.one('SELECT state FROM dot_consumer_sessions WHERE id=?',(credentials['consumer_session_id'],))['state']=='expired'
    assert s.store.one('SELECT state FROM coordination_work WHERE id=?',(item['id'],))['state']=='blocked'
    assert s.store.one('SELECT state FROM operations WHERE id=?',(operation['operation_id'],))['state']=='cancelled'
    with pytest.raises(DevError):
        s.coordination.heartbeat({**lease,'idempotency_key':key()},actor)
    assert not s.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'",(identity['member_id'],))
