"""Event notification consent reuses current read access, never worker authority."""
import json
from dataclasses import replace

import pytest
from hub.collaboration.common import EVENTS, TASK_EVENT
from shared.util import DevError
from tests.test_collaboration_service import collab, key, command
from tests.test_collaboration_events import setup_events


def broad(c):
    return replace(c[2],grant_id='broad',actor='mcp:broad:fixture',scopes={'read','write','execute'})


@pytest.mark.asyncio
async def test_existing_broad_grant_subscribes_without_worker_binding(collab):
    s=collab[0];events,receiver,args=setup_events(collab)
    before=s.store.one("SELECT * FROM grants WHERE id='broad'")
    assert {event['name'] for event in events.catalog({},broad(collab))['events']} == set(EVENTS)
    sub=await events.subscribe(args,broad(collab))
    assert (await events.subscribe(args,broad(collab)))['id']==sub['id']
    assert s.store.one("SELECT * FROM grants WHERE id='broad'")==before
    assert not s.store.one("SELECT id FROM collaboration_agents WHERE grant_id='broad'")
    row=s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',(sub['id'],))
    with s.store.transaction():events.queue_test(collab[4],row)
    await events.tick()
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state']=='accepted'
    assert len(receiver.requests)==2
    assert not s.store.all('SELECT * FROM collaboration_jobs')
    created=command(collab)[1]
    with pytest.raises(DevError) as error:
        s.claim({**collab[-1],'job_id':created['job_id'],'expected_version':1,'idempotency_key':key()},broad(collab))
    assert error.value.code=='WORKER_SCOPE_TOO_BROAD'
    with pytest.raises(DevError):
        s.register_agent({**collab[-1],'label':'not a worker','kind':'work_cloud','grant_id':'broad','idempotency_key':key()},collab[1])


@pytest.mark.asyncio
async def test_first_subscription_creates_only_passive_scoped_room(collab):
    s=collab[0];events,_,args=setup_events(collab)
    args={**args,'arguments':{**args['arguments'],'environment_id':'notifications'}}
    counts={table:len(s.store.all('SELECT * FROM '+table)) for table in ['collaboration_agents','collaboration_jobs','monitor_plans','monitor_probes']}
    sub=await events.subscribe(args,broad(collab))
    room=s.store.one("SELECT * FROM collaboration_rooms WHERE environment_id='notifications'")
    assert room['space_id']=='legacy' and room['owner_user_id']=='owner' and room['project_id']=='proj'
    assert s.store.one('SELECT room_id FROM mcp_event_subscriptions WHERE id=?',(sub['id'],))['room_id']==room['id']
    assert counts=={table:len(s.store.all('SELECT * FROM '+table)) for table in counts}
    assert (await events.subscribe(args,broad(collab)))['id']==sub['id']
    assert len(s.store.all("SELECT * FROM collaboration_rooms WHERE environment_id='notifications'"))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('change',["UPDATE grants SET revoked=1 WHERE id='broad'", "UPDATE grants SET scopes='[\"write\",\"execute\"]' WHERE id='broad'", "UPDATE grants SET projects='[]' WHERE id='broad'"])
async def test_subscription_rechecks_grant_before_delivery(collab,change):
    s=collab[0];events,receiver,args=setup_events(collab)
    sub=await events.subscribe(args,broad(collab))
    row=s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',(sub['id'],))
    with s.store.transaction():events.queue_test(collab[4],row)
    item=events.reserve()[0]
    s.store.execute(change)
    await events.deliver(item)
    assert len(receiver.requests)==1
    assert s.store.one('SELECT state FROM mcp_event_deliveries')['state']=='abandoned'
    with pytest.raises(DevError):await events.subscribe(args,broad(collab))


@pytest.mark.asyncio
async def test_subscription_cannot_create_room_outside_project_or_without_read(collab):
    s=collab[0];events,receiver,args=setup_events(collab)
    outside={**args,'arguments':{**args['arguments'],'project_id':'otherproj','environment_id':'unapproved'}}
    with pytest.raises(DevError):await events.subscribe(outside,broad(collab))
    assert not s.store.one("SELECT id FROM collaboration_rooms WHERE project_id='otherproj'")
    s.store.execute("UPDATE grants SET scopes='[\"write\",\"execute\"]' WHERE id='broad'")
    with pytest.raises(DevError):events.catalog({},broad(collab))
    with pytest.raises(DevError):await events.subscribe(args,broad(collab))
    assert receiver.requests==[]


@pytest.mark.asyncio
async def test_notifications_preserve_exact_target_grant_and_queue(collab):
    s=collab[0];events,receiver,args=setup_events(collab)
    sub=await events.subscribe(args,broad(collab))
    command(collab)  # directed to a different grant, never leaked to broad reader.
    await events.tick()
    assert len(receiver.requests)==1
    row=s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',(sub['id'],))
    event={'name':TASK_EVENT,'target_grant_id':'broad','queue':'dot-coordination','data':json.dumps({'test':False})}
    assert not events.matches(row,args['arguments'],event)
    event['queue']='work-analysis'
    assert events.matches(row,args['arguments'],event)


@pytest.mark.asyncio
async def test_read_revocation_during_challenge_prevents_subscription_commit(collab):
    s=collab[0];events,receiver,args=setup_events(collab)
    async def revoke(url,body,headers):
        answer=await receiver(url,body,headers)
        s.store.execute("UPDATE grants SET scopes='[\"write\"]' WHERE id='broad'")
        return answer
    events.sender=revoke
    with pytest.raises(DevError):await events.subscribe(args,broad(collab))
    assert not s.store.all('SELECT * FROM mcp_event_subscriptions')
