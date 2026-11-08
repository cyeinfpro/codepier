"""Synthetic host callback to native inbox, reply and managed work admission."""
import base64
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


def current_epoch(service,goal_id):
    return service.room_goal_recovery.epoch(goal_id)


def permission_rules(scopes, previous=(), lifetime=None):
    """Normalize test intent without resetting unchanged approval deadlines."""
    prior={(r['project_id'],r['environment_id']):r for r in previous}
    rules=[]
    for scope in scopes:
        environment=scope.get('environment_id','production')
        old=prior.get((scope['project_id'],environment),{})
        rules.append({**scope,'environment_id':environment,
            'capabilities':sorted(scope.get('capabilities',['read'])),
            'execution_targets':sorted(scope.get('execution_targets',[])),
            'lifetime':scope.get('lifetime',lifetime or old.get('lifetime',
                {'mode':'until_revoked','expires_at':None}))})
    return sorted(rules,key=lambda r:(r['project_id'],r['environment_id']))


def save_room_permissions(service, owner, conversation_id, scopes, lifetime=None):
    policy=service.room_permissions.read(conversation_id,owner)
    return service.room_permissions.save({'conversation_id':conversation_id,
        'expected_version':policy['version'],
        'scope':{'project_scopes':permission_rules(scopes,policy['scope']['project_scopes'],lifetime)},
        'channels':{name:{'mode':entry['mode'],'scope':entry['scope']}
                    for name,entry in policy['channels'].items()},
        'idempotency_key':key()},owner)


async def connected(collab, projects=None, scopes=None, *, worker_override=None,conversation_id=None):
    s,owner,worker,_,_,_,clock,_=collab
    worker=worker_override or worker
    s.config=replace(s.config,events_enabled=True)
    received=[]
    async def receiver(url,body,headers):
        payload=json.loads(body)
        if payload.get('type')=='verification':
            return Reply(200,canonical({'challenge':payload['challenge']}).encode())
        received.append(payload)
        return Reply(204)
    s.events=EventService(s,receiver)
    s.native_test_callbacks=received
    room={'id':conversation_id} if conversation_id else s.rooms.create({'title':'Native room','idempotency_key':key()},owner)['room']
    if conversation_id:
        policy=s.room_permissions.read(conversation_id,owner)
        assert policy['configured'], 'Configure canonical room permissions before adding a member'
        if scopes is not None:
            assert permission_rules(scopes,policy['scope']['project_scopes'])==policy['scope']['project_scopes'], (
                'Existing-room membership cannot change policy; save canonical permissions explicitly '
                'or express narrower access with the worker grant ceiling')
    else:
        rules=permission_rules(scopes or [])
        if projects is not None:
            attached={(p.get('project_id',p.get('project')),p.get('environment_id','production')) for p in projects}
            assert {(r['project_id'],r['environment_id']) for r in rules} <= attached
        save_room_permissions(s,owner,room['id'],rules)
    added=s.rooms.create_member({'conversation_id':room['id'],'name':'Native dot','idempotency_key':key()},owner)
    flow=s.store.one('SELECT * FROM room_connect_flows WHERE id=?',(added['flow']['id'],))
    joined=s.rooms.join({'invite_code':s.store.decrypt(flow['invite_secret']),'idempotency_key':key()},worker)
    await s.events.subscribe({**joined['subscription_request'],'delivery':{'mode':'webhook',
        'url':'https://callback.example.invalid/native-room/'+joined['member_id'],'secret':'whsec_'+base64.b64encode(b'n'*32).decode()}},worker)
    await s.events.tick()
    ticket=next(payload['data']['wake_ticket'] for payload in reversed(received) if payload['data'].get('member_id')==joined['member_id'])
    identity={k:joined[k] for k in ('conversation_id','member_id','binding_generation','connection_proof')}
    probe=s.rooms.probe_read({**identity,'wake_ticket':ticket,'idempotency_key':key()},worker)
    s.rooms.probe_reply({**identity,'probe_nonce':probe['probe_nonce'],'idempotency_key':key()},worker)
    session=s.room_consumers.begin({**identity,'wake_ticket':ticket,'consumer_run_id':key(),'idempotency_key':key()},worker)
    credentials={k:session[k] for k in ('consumer_session_id','consumer_fence','consumer_proof')}
    return s,owner,worker,clock,identity,credentials


@pytest.mark.asyncio
async def test_native_empty_room_reply_is_fenced_and_canonical(collab):
    s,owner,worker,clock,identity,credentials=await connected(collab)
    sent=s.room_runtime.send({'conversation_id':identity['conversation_id'],'body_text':'你好，帮我整理思路',
        'client_message_id':key(),'idempotency_key':key()},owner)['message']
    inbox=s.room_consumers.inbox(identity,worker)
    source=next(row for row in inbox['items'] if row['kind']=='message')
    raw={**identity,**credentials,'source_message_id':sent['id'],'source_version':source['source_version'],
         'source_digest':source['source_digest'],'expected_inbox_revision':inbox['inbox_revision'],
         'body_text':'可以，先从你的目标开始。','complete':True,'idempotency_key':key()}
    reply=s.room_runtime.reply(raw,worker)
    assert reply['message']['reply_to_id']==sent['id']
    assert s.room_runtime.reply(raw,worker)['message']['id']==reply['message']['id']
    assert not s.room_consumers.inbox(identity,worker)['items']
    assert not s.store.all('SELECT * FROM coordination_work')
    clock[0]+=301
    with pytest.raises(DevError):
        s.room_runtime.reply({**raw,'idempotency_key':key()},worker)


@pytest.mark.asyncio
async def test_native_project_request_starts_one_original_work_and_claims_it(collab):
    s,owner,worker,clock,identity,credentials=await connected(collab,
        [{'project':'proj','environment_id':'production'}],
        [{'project_id':'proj','capabilities':['read'],'execution_targets':[]}])
    sent=s.room_runtime.send({'conversation_id':identity['conversation_id'],'body_text':'请检查 README 并给出检查结果',
        'client_message_id':key(),'idempotency_key':key()},owner)['message']
    inbox=s.room_consumers.inbox(identity,worker)
    source=next(row for row in inbox['items'] if row['kind']=='message')
    raw={**identity,**credentials,'source_message_id':sent['id'],'source_version':source['source_version'],
         'source_digest':source['source_digest'],'expected_inbox_revision':inbox['inbox_revision'],
         'target_project_id':'proj','acceptance':'读取 README 并说明实际检查结论','confirm_task':True,'idempotency_key':key()}
    result=s.room_runtime.start_work(raw,worker)
    assert result['scheduled']
    claim=result['next_request']['arguments']
    claim.pop('action',None)
    claimed=s.coordination.work_claim(claim,worker)
    assert claimed['work_item']['state']=='leased'
    item=claimed['work_item']
    lease={'project':'proj','environment_id':'production','goal_id':result['goal_id'],'work_item_id':item['id'],
        'attempt':item['attempt'],'fencing_token':item['fencing_token'],**credentials,
        'expected_intent_revision':result['intent_revision'],
        'coordinator_epoch':current_epoch(s,result['goal_id']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,worker)['inbox_revision']}
    operation=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'},'idempotency_key':key()},worker)
    # Deterministic Agent completion uses Runtime's real receipt path. The
    # fixture never inserts a successful work or operation state directly.
    saved=s.store.one('SELECT * FROM operations WHERE id=?',(operation['operation_id'],))
    s.runtime.complete(saved,{'ok':True,'data':{'content':'# Synthetic README'}})
    lease['expected_inbox_revision']=s.room_consumers.inbox(identity,worker)['inbox_revision']
    finished=s.coordination.result({**lease,'outcome':'succeeded','summary':'已读取 README，包含项目标题。',
        'operation_ids':[operation['operation_id']],'idempotency_key':key()},worker)
    assert finished['work_item']['state']=='succeeded'
    assert finished['work_item']['result']['execution_verified'] is True
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM coordination_work'))==1
    refreshed=s.room_consumers.inbox(identity,worker)
    replay=s.room_runtime.start_work({**raw,'expected_inbox_revision':refreshed['inbox_revision'],
        'coordinator_epoch':current_epoch(s,result['goal_id'])},worker)
    assert replay['goal_id']==result['goal_id'] and len(s.store.all('SELECT * FROM coordination_work'))==1
