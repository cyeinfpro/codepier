"""Native room WeChat uses synthetic provider/host protocols and real ledgers."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration import weixin_delivery_order as order
from hub.collaboration.common import digest
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import connected,current_epoch,save_room_permissions
from tests.test_weixin_qr_enrollment import confirm
from tests.test_dot_weixin_channel import packet
from tests.legacy_iam_fixture import seed_grant


async def setup_channel(collab, *, tasks=False,projects=None,scopes=None,worker_override=None):
    ctx=await connected(collab,
        projects if projects is not None else ([{'project':'proj','environment_id':'production'}] if tasks else []),
        scopes if scopes is not None else ([{'project_id':'proj','capabilities':['read'],'execution_targets':[]}] if tasks else []),
        worker_override=worker_override)
    s,owner,worker,clock,identity,credentials=ctx
    s.config=replace(s.config,weixin_enabled=True)
    provider=SimpleNamespace(starts=0,response={'status':'wait'})
    class Provider:
        async def qr_start(self):
            provider.starts+=1
            return {'qrcode':'private-native-qr','qrcode_img_content':'https://weixin.qq.com/synthetic-room-only'}
        async def qr_status(self,*args):
            return provider.response
    s.weixin.client_factory=lambda *_:Provider()
    async def action(flow,verb,**extra):
        return (await s.room_channels.action({'flow_id':flow['id'],'action':verb,
            'expected_version':flow['version'],'idempotency_key':key(),**extra},owner))['flow']
    flow=s.room_channels.create({'conversation_id':identity['conversation_id'],'member_id':identity['member_id'],'idempotency_key':key()},owner)['flow']
    flow=await action(flow,'start_qr',confirm_credential_storage=True,confirm_provider_terms=True)
    assert flow['stage']=='scan'
    confirm(provider)
    flow=await action(flow,'poll_qr')
    flow=await action(flow,'accept_account',confirm_scanner_id=flow['qr']['scanner_id'])
    assert flow['stage']=='confirm_binding'
    scope={'allow_owner_tasks':True,'expected_scope_digest':flow['task_scope']['scope_digest'],
        'confirm_owner_identity':True,'confirm_future_tasks':True,'confirm_result_delivery':True,
        'confirm_pairing_notices':True} if tasks else {}
    flow=await action(flow,'bind',confirm_external_data_ingress=True,allow_replies=True,confirm_until_revoked=True,**scope)
    claim=s.weixin.claim_poll(flow['account_id'])
    assert claim
    s.weixin.commit_updates(claim,packet(1,flow['binding']['pairing_challenge'],create_time_ms=int(clock[0]*1000)+1))
    flow=s.room_channels.read(flow['id'],owner)['flow']
    assert flow['stage']=='confirm_owner'
    flow=await action(flow,'confirm_owner',confirm_peer_id='synthetic-peer')
    assert flow['stage']=='ready' and flow['work_authorized']==tasks
    clock[0]+=3
    return ctx,flow,provider,action


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_update_stamp', [False, True, 120, 1000])
async def test_empty_room_wechat_pair_inbox_reply_reuses_original_outbox(collab, initial_update_stamp):
    (s,owner,worker,clock,identity,credentials),flow,provider,action=await setup_channel(collab)
    created = int(clock[0] * 1000)
    message = packet(2, '请聊聊这个想法', create_time_ms=created,
                     **({'update_time_ms': created + (0 if initial_update_stamp is True else initial_update_stamp)} if initial_update_stamp else {}))
    received=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),message)['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(received,))
    assert source['room_id'] is None
    assert not s.store.all('SELECT * FROM room_member_work_bridges')
    assert not s.channel_owner.author(source)['authenticated']
    inbox=s.room_consumers.inbox(identity,worker)
    reference=next(item for item in inbox['items'] if item['kind']=='message')
    assert 'room_reply' in inbox['handling_instructions']
    assert reference['message']['owner_task_blocker']=='CHANNEL_OWNER_NOT_APPROVED'
    assert reference['message']['requires_owner_execution_request']
    assert 'work_start_arguments' not in reference
    reply=s.room_runtime.reply({**identity,**credentials,**reference['reply_arguments'],
        'body_text':'可以，先聊聊你的目标。','idempotency_key':key()},worker)
    deliveries=s.store.all('SELECT * FROM weixin_deliveries')
    assert len(deliveries)==1 and deliveries[0]['message_id']==reply['message']['id']
    assert deliveries[0]['source_message_id']==received
    assert not any(item['object_id']==received for item in s.room_consumers.inbox(identity,worker)['items'])
    assert s.weixin.delivery.validate(deliveries[0])[3]['id']==reply['message']['id']
    clock[0]+=3
    duplicate=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),message)
    assert duplicate['message_ids']==[received]
    assert len(s.store.all('SELECT * FROM weixin_deliveries'))==1
    assert not s.store.all('SELECT * FROM coordination_work')
    assert not s.store.all('SELECT * FROM operations')
    exposed=json.dumps(s.room_runtime.read(identity['conversation_id'],owner))
    assert 'private-context-token' not in exposed and 'synthetic-qr-bot-secret' not in exposed
    flow=await action(flow,'disconnect')
    with pytest.raises(DevError):
        s.weixin.delivery.validate(deliveries[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('field, offset', [('update_time_ms', -1), ('delete_time_ms', 0)])
async def test_first_seen_changed_provider_message_stays_out_of_inbox(collab, field, offset):
    (s, _, worker, clock, identity, _), flow, _, _ = await setup_channel(collab, tasks=True)
    created = int(clock[0] * 1000)
    message = packet(2, '不能开始的任务', create_time_ms=created, **{field: created + offset})
    received = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), message)['message_ids'][0]
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (received,))
    assert json.loads(source['body'])['owner_task_blocker'] == 'CHANNEL_OWNER_SOURCE_CHANGED'
    assert not s.channel_owner.author(source)['authenticated']
    assert received not in {entry['object_id'] for entry in s.room_consumers.inbox(identity, worker)['items']}


@pytest.mark.asyncio
async def test_revised_provider_source_is_fenced_even_at_current_inbox_revision(collab):
    (s, owner, worker, clock, identity, credentials), flow, _, action = await setup_channel(collab)
    created = int(clock[0] * 1000)
    original = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '原先的话题', create_time_ms=created))['message_ids'][0]
    page = s.room_consumers.inbox(identity, worker)
    original_entry = next(item for item in page['items'] if item['object_id'] == original)
    clock[0] += 3
    edited = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '现在的话题', create_time_ms=created, update_time_ms=created + 1000))['message_ids'][0]
    page = s.room_consumers.inbox(identity, worker)
    assert original not in {item['object_id'] for item in page['items']}
    current = next(item for item in page['items'] if item['object_id'] == edited)

    with pytest.raises(DevError) as changed:
        s.room_runtime.reply({**identity, **credentials, **original_entry['reply_arguments'],
            'expected_inbox_revision': page['inbox_revision'], 'body_text': '已看到原话题。',
            'idempotency_key': key()}, worker)
    assert changed.value.code == 'ROOM_SOURCE_CHANGED'

    # Losing the channel after an input was saved stops its transport reply,
    # while the current source can still have a room-only status response.
    await action(flow, 'disconnect')
    reply = s.room_runtime.reply({**identity, **credentials, **current['reply_arguments'],
        'expected_inbox_revision': s.room_consumers.inbox(identity, worker)['inbox_revision'],
        'body_text': '已保存，原微信连接已解除。', 'idempotency_key': key()}, worker)
    assert reply['message']['reply_to_id'] == edited
    assert not s.store.all('SELECT id FROM weixin_deliveries')
    page = s.room_consumers.inbox(identity, worker)
    assert not page['items']
    yielded = s.room_consumers.yield_session({**identity, **credentials,
        'expected_inbox_revision': page['inbox_revision'], 'idempotency_key': key()}, worker)
    assert yielded['state'] == 'yielded'


@pytest.mark.asyncio
@pytest.mark.parametrize('timestamp,reason', [
    (0, 'WEIXIN_SOURCE_TIME_MISSING'),
    (1, 'WEIXIN_SOURCE_BEFORE_APPROVAL'),
])
async def test_wechat_unverified_task_has_reason_and_can_receive_reply(collab, timestamp, reason):
    (s,_,worker,_,identity,credentials),flow,_,_=await setup_channel(collab,tasks=True)
    source=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2,'请检查项目',create_time_ms=timestamp))['message_ids'][0]
    inbox=s.room_consumers.inbox(identity,worker)
    entry=next(item for item in inbox['items'] if item['object_id']==source)
    assert entry['message']['owner_task_blocker']==reason
    assert entry['message']['owner_task_reason']
    assert 'work_start_arguments' not in entry
    reply=s.room_runtime.reply({**identity,**credentials,**entry['reply_arguments'],
        'body_text':entry['message']['owner_task_reason'],'idempotency_key':key()},worker)
    delivery=s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',(reply['message']['id'],))
    assert delivery['source_message_id']==source and delivery['state']=='queued'


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_original_member',[False,True])
async def test_wechat_goal_takeover_preserves_channel_origin_and_current_lead_delivery(collab,pause_original_member):
    from tests.test_room_goal_recovery import control,continue_original
    from tests.test_room_goal_scope_boundaries import worker_for,admit_read,report,final_request

    (s,owner,actor,clock,lead,credentials),flow,_,action=await setup_channel(collab,tasks=True)
    source_id=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2,'请读取项目说明，并沿原工作报告结果。',create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(source_id,))
    inbox=s.room_consumers.inbox(lead,actor)
    entry=next(item for item in inbox['items'] if item['kind']=='message')
    assert entry['message']['trusted_author']['kind']=='verified_external_owner'
    assert not entry['message']['requires_owner_execution_request']
    assert 'owner_task_blocker' not in entry['message']
    request={**lead,**credentials,**entry['work_start_arguments'],
        'target_project_id':'proj','acceptance':'按原回执报告结果','idempotency_key':key()}
    started=s.room_runtime.start_work(request,actor)
    goal_id=started['goal_id'];original=s.room_runtime.goal(goal_id)
    progress=s.room_runtime.reply({**lead,**credentials,'source_message_id':source_id,
        'source_version':entry['source_version'],'source_digest':entry['source_digest'],
        'expected_inbox_revision':s.room_consumers.inbox(lead,actor)['inbox_revision'],
        'body_text':'原主责已收到，正在核对。','complete':False,'idempotency_key':key()},actor)
    stale_delivery=s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',(progress['message']['id'],))
    assert stale_delivery and s.weixin.delivery.validate(stale_delivery)
    replacement_actor=worker_for(s,actor,'wechat-replacement',scopes=('read','write'),projects=('proj',))
    _,_,replacement_actor,_,replacement,replacement_credentials=await connected(collab,
        worker_override=replacement_actor,
        conversation_id=lead['conversation_id'])
    if pause_original_member:
        connect=s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?',(lead['member_id'],))
        current=s.rooms.connect_read({'flow_id':connect['id']},owner)['flow']
        s.rooms.connect_action({'flow_id':connect['id'],'action':'pause','expected_version':current['version'],
            'idempotency_key':key()},owner)
        with pytest.raises(DevError,match='原主责'):
            s.room_channels.native.author(source)
    binding_before=s.store.one('SELECT * FROM weixin_bindings WHERE id=?',(flow['binding']['id'],))
    changed=control(s,owner,goal_id,'takeover',replacement['member_id'])['work']
    assert changed['coordinator_epoch']==2 and changed['coordinator_member_id']==replacement['member_id']
    assert s.store.one('SELECT * FROM weixin_bindings WHERE id=?',(binding_before['id'],))==binding_before
    assert {k:s.room_runtime.goal(goal_id)[k] for k in ('spec','digest','approval_id','expires_at')}=={
        k:original[k] for k in ('spec','digest','approval_id','expires_at')}
    with pytest.raises(DevError):
        s.weixin.delivery.validate(stale_delivery)
    continued=continue_original(s,replacement_actor,replacement,replacement_credentials,goal_id)
    assert continued['goal_id']==goal_id and continued['scheduled']
    effective=s.room_work_bridge.native.record(s.room_work_bridge.native.reference(s.room_runtime.goal(goal_id)))
    assert effective['members'][0]['projects'][0]['capabilities']==['read']
    item=s.store.one('SELECT * FROM coordination_work WHERE id=?',(continued['continued_work_item_id'],))
    lease,operation=admit_read(s,replacement,replacement_credentials,replacement_actor,goal_id,item,'proj')
    result=report(s,replacement,replacement_credentials,replacement_actor,goal_id,lease,operation)
    intent=s.store.one('SELECT * FROM room_work_intents WHERE goal_id=?',(goal_id,))
    current_source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(intent['source_message_id'],))
    refs=[{'work_item_id':result['id'],'version':result['version'],'digest':digest(result['result'])}]
    final=s.room_runtime.reply(final_request(s,replacement,replacement_credentials,replacement_actor,
        current_source,refs,expected_intent_revision=intent['intent_revision']),replacement_actor)
    delivery=s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',(final['message']['id'],))
    assert delivery and delivery['source_message_id']==source_id and delivery['binding_id']==binding_before['id']
    assert delivery['peer_id']==binding_before['peer_id'] and delivery['generation']==binding_before['generation']
    assert s.weixin.delivery.validate(delivery)[3]['id']==final['message']['id']
    if pause_original_member:
        with pytest.raises(DevError,match='原主责'):
            s.room_channels.native.valid(binding_before)
    else:
        assert s.room_channels.native.valid(binding_before)[4]['id']==lead['member_id']
    flow=s.room_channels.read(flow['id'],owner)['flow']
    await action(flow,'revoke_tasks')
    with pytest.raises(DevError) as revoked:
        s.weixin.delivery.validate(delivery)
    assert revoked.value.code=='CHANNEL_OWNER_NOT_APPROVED'
    assert s.store.one('SELECT state FROM weixin_bindings WHERE id=?',(binding_before['id'],))['state']=='active'
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_update_stamp', [False, True, 120, 1000])
async def test_native_owner_task_to_managed_result_same_wechat_source(collab, initial_update_stamp):
    (s,owner,worker,clock,identity,credentials),flow,provider,action=await setup_channel(collab,tasks=True)
    created = int(clock[0] * 1000)
    message = packet(2, '请检查项目 README', create_time_ms=created,
                     **({'update_time_ms': created + (0 if initial_update_stamp is True else initial_update_stamp)} if initial_update_stamp else {}))
    received = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), message)['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(received,))
    assert s.channel_owner.author(source)['authenticated']
    inbox=s.room_consumers.inbox(identity,worker)
    entry = next(item for item in inbox['items'] if item['object_id'] == received)
    assert 'work_start_arguments' in entry
    result=s.room_runtime.start_work({**identity,**credentials,'source_message_id':received,'source_version':source['version'],
        'source_digest':digest(json.loads(source['body'])['body_text']),'expected_inbox_revision':inbox['inbox_revision'],
        'acceptance':'阅读并报告内容','confirm_task':True,'idempotency_key':key()},worker)
    claim={k:v for k,v in result['next_request']['arguments'].items() if k!='action'}
    item=s.coordination.work_claim(claim,worker)['work_item']
    lease={'project':'proj','environment_id':'production','goal_id':result['goal_id'],'work_item_id':item['id'],
        'attempt':item['attempt'],'fencing_token':item['fencing_token'],**credentials,
        'expected_intent_revision':result['intent_revision'],
        'coordinator_epoch':current_epoch(s,result['goal_id']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,worker)['inbox_revision']}
    op=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'},'idempotency_key':key()},worker)
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],)),{'ok':True,'data':{'content':'# Synthetic'}})
    s.coordination.result({**lease,'expected_inbox_revision':s.room_consumers.inbox(identity,worker)['inbox_revision'],
        'outcome':'succeeded','summary':'已确认 README 标题。','operation_ids':[op['operation_id']],'idempotency_key':key()},worker)
    deliveries=s.store.all('SELECT * FROM weixin_deliveries')
    assert len(deliveries)>=2  # Actual claim progress and final result are distinct canonical facts.
    for delivery in deliveries:
        assert delivery['source_message_id']==received and delivery['binding_id']==flow['binding']['id']
        s.weixin.delivery.validate(delivery)
    final=s.store.one("SELECT * FROM collaboration_messages WHERE goal_id=? AND kind='delegation_result'",(result['goal_id'],))
    assert any(row['message_id']==final['id'] for row in deliveries)
    assert len(s.store.all('SELECT * FROM coordination_work'))==1


@pytest.mark.asyncio
async def test_wechat_input_cannot_enlarge_its_consent_with_another_member_project(collab):
    (s,owner,worker,clock,identity,credentials),flow,provider,action=await setup_channel(collab,tasks=True)
    room=s.rooms.read({'conversation_id':identity['conversation_id']},owner)['room']
    policy=save_room_permissions(s,owner,room['id'],[{'project_id':'proj'},{'project_id':'otherproj'}])
    s.room_permissions.save({'conversation_id':room['id'],'expected_version':policy['version'],
        'scope':policy['scope'],'channels':{'weixin':{'mode':'custom','scope':{'project_scopes':[
            {'project_id':'proj','environment_id':'production','capabilities':['read'],'execution_targets':[]}]}},
            'telegram':{'mode':'follow_room'}},'idempotency_key':key()},owner)
    seed_grant(s.store,'wx-peer','owner',scopes=('read',),projects=('otherproj',))
    actor=replace(worker,grant_id='wx-peer',actor='mcp:wx-peer:fixture',projects=['otherproj'])
    _,_,actor,_,peer,peer_credentials=await connected(collab,
        worker_override=actor,conversation_id=room['id'])
    clock[0]+=91
    s.room_consumers.request_recovery({**identity,'idempotency_key':key()},worker)
    await s.events.tick()
    ticket=next(p['data']['wake_ticket'] for p in reversed(s.native_test_callbacks) if p['data'].get('member_id')==identity['member_id'])
    session=s.room_consumers.begin({**identity,'wake_ticket':ticket,'consumer_run_id':key(),'idempotency_key':key()},worker)
    credentials={k:session[k] for k in ('consumer_session_id','consumer_fence','consumer_proof')}
    received=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),packet(2,'请检查项目文档',
        create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(received,))
    assert s.channel_owner.author(source)['authenticated']
    with pytest.raises(DevError,match='微信入口明确批准'):
        s.room_runtime.start_work({**identity,**credentials,'source_message_id':received,'source_version':source['version'],
            'source_digest':digest(json.loads(source['body'])['body_text']),
            'expected_inbox_revision':s.room_consumers.inbox(identity,worker)['inbox_revision'],
            'target_project_id':'proj','project_ids':['proj','otherproj'],'participant_member_ids':[peer['member_id']],
            'acceptance':'不能借其他成员扩大这个微信入口','confirm_task':True,'idempotency_key':key()},worker)
    assert not s.store.all('SELECT * FROM coordination_goals')


@pytest.mark.asyncio
async def test_existing_wechat_goal_keeps_unchanged_project_after_other_project_removal(collab):
    s,owner,worker,*_=collab
    seed_grant(s.store,'wx-both','owner',scopes=('read',),projects=('proj','otherproj'))
    actor=replace(worker,grant_id='wx-both',actor='mcp:wx-both:fixture',projects=['proj','otherproj'])
    (s,owner,actor,clock,identity,credentials),flow,provider,action=await setup_channel(collab,tasks=True,
        projects=[{'project':'proj'},{'project':'otherproj'}],scopes=[{'project_id':'proj'},{'project_id':'otherproj'}],worker_override=actor)
    received=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),packet(2,'检查原项目说明',
        create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(received,))
    result=s.room_runtime.start_work({**identity,**credentials,'source_message_id':received,'source_version':source['version'],
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'target_project_id':'proj','project_ids':['proj','otherproj'],'acceptance':'报告真实原项目检查结果',
        'confirm_task':True,'idempotency_key':key()},actor)
    room=s.rooms.read({'conversation_id':identity['conversation_id']},owner)['room']
    save_room_permissions(s,owner,room['id'],[{'project_id':'proj'}])
    assert s.channel_owner.author(source)['authenticated']  # The unchanged original project stays authorized.
    assert s.room_work_bridge.native.owner_author(s.room_runtime.goal(result['goal_id']),source)['authenticated']
    clock[0]+=91
    assert s.room_consumers.request_recovery({**identity,'idempotency_key':key()},actor)['wakeup_queued']
    await s.events.tick()
    ticket=next(p['data']['wake_ticket'] for p in reversed(s.native_test_callbacks) if p['data'].get('member_id')==identity['member_id'])
    session=s.room_consumers.begin({**identity,'wake_ticket':ticket,'consumer_run_id':key(),'idempotency_key':key()},actor)
    credentials={k:session[k] for k in ('consumer_session_id','consumer_fence','consumer_proof')}
    claim={**{k:v for k,v in result['next_request']['arguments'].items() if k!='action'},**credentials,
        'idempotency_key':key(),'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']}
    item=s.coordination.work_claim(claim,actor)['work_item']
    lease={'project':'proj','environment_id':'production','goal_id':result['goal_id'],'work_item_id':item['id'],
        'attempt':item['attempt'],'fencing_token':item['fencing_token'],**credentials,'expected_intent_revision':1,
        'coordinator_epoch':current_epoch(s,result['goal_id']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']}
    op=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'},'idempotency_key':key()},actor)
    s.runtime.complete(s.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],)),{'ok':True,'data':{'content':'# Original A'}})
    done=s.coordination.result({**lease,'outcome':'succeeded','summary':'原 A 项目已经读取',
        'operation_ids':[op['operation_id']],'idempotency_key':key()},actor)['work_item']
    assert done['state']=='succeeded'
    delivery=s.store.one('''SELECT d.* FROM weixin_deliveries d JOIN collaboration_messages m ON m.id=d.message_id
        WHERE m.kind='delegation_result' AND m.goal_id=?''',(result['goal_id'],))
    assert delivery and s.weixin.delivery.validate(delivery)[3]['goal_id']==result['goal_id']
    newer=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),packet(3,'新的请求',
        create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    assert s.channel_owner.author(s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(newer,)))['authenticated']
    current_input=s.store.one('SELECT scope FROM room_channel_owner_inputs WHERE message_id=?',(newer,))
    assert [rule['project_id'] for rule in json.loads(current_input['scope'])['projects']]==['proj']
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1


@pytest.mark.asyncio
async def test_revoke_only_wechat_task_permission_retains_chat_and_original_operation(collab):
    (s,owner,actor,clock,identity,credentials),flow,provider,action=await setup_channel(collab,tasks=True)
    assert 'revoke_tasks' in flow['allowed_actions']
    source_id=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),packet(2,'读取 README',
        create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    source=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(source_id,))
    started=s.room_runtime.start_work({**identity,**credentials,'source_message_id':source_id,'source_version':source['version'],
        'source_digest':digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'acceptance':'读取并报告','confirm_task':True,'idempotency_key':key()},actor)
    item=s.coordination.work_claim({k:v for k,v in started['next_request']['arguments'].items() if k!='action'},actor)['work_item']
    lease={'project':'proj','environment_id':'production','goal_id':started['goal_id'],'work_item_id':item['id'],
        'attempt':item['attempt'],'fencing_token':item['fencing_token'],**credentials,'expected_intent_revision':1,
        'coordinator_epoch':current_epoch(s,started['goal_id']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision']}
    op=s.coordination.admit({**lease,'tool':'read','arguments':{'path':'README.md'},'idempotency_key':key()},actor)
    request={'flow_id':flow['id'],'expected_version':flow['version'],'action':'revoke_tasks','idempotency_key':key()}
    revoked=(await s.room_channels.action(request,owner))['flow']
    again=(await s.room_channels.action(request,owner))['flow']
    assert revoked['version']==again['version'] and revoked['stage']=='ready'
    assert revoked['primary_action']=='approve_tasks' and not revoked['work_authorized']
    assert 'revoke_tasks' not in revoked['allowed_actions'] and revoked['binding']['state']=='active'
    assert s.store.one('SELECT state FROM weixin_accounts WHERE id=?',(flow['account_id'],))['state']=='active'
    retained=s.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],))
    assert retained and retained['cancel_requested']==1 and retained['state']=='cancelled'
    assert s.room_runtime.goal(started['goal_id'])['state']=='paused'
    with pytest.raises(DevError):
        s.coordination.admit({**lease,'idempotency_key':key(),'tool':'read','arguments':{'path':'README.md'}},actor)
    clock[0]+=3
    chat_id=s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),packet(3,'继续聊聊这个想法',
        create_time_ms=int(clock[0]*1000)))['message_ids'][0]
    chat=s.store.one('SELECT * FROM collaboration_messages WHERE id=?',(chat_id,))
    assert not s.channel_owner.author(chat)['authenticated']
    reply=s.room_runtime.reply({**identity,**credentials,'source_message_id':chat_id,'source_version':chat['version'],
        'source_digest':digest(json.loads(chat['body'])['body_text']),
        'expected_inbox_revision':s.room_consumers.inbox(identity,actor)['inbox_revision'],
        'body_text':'可以，我们继续聊天。','idempotency_key':key()},actor)
    delivery=s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',(reply['message']['id'],))
    assert delivery and s.weixin.delivery.validate(delivery)[0]['id']==flow['binding']['id']
    assert len(s.store.all('SELECT * FROM coordination_goals'))==1
    assert len(s.store.all('SELECT * FROM operations'))==1


@pytest.mark.asyncio
async def test_native_room_qr_renewal_remains_visible_and_preserves_original_identity(collab):
    (s,owner,actor,clock,identity,credentials),flow,provider,action=await setup_channel(collab)
    original_account,original_binding=flow['account_id'],flow['binding']['id']
    original_generation=flow['binding']['generation']
    s.weixin.poll_failed(s.weixin.claim_poll(original_account),DevError('WEIXIN_SESSION_EXPIRED','合成账号过期',403))
    flow=s.room_channels.read(flow['id'],owner)['flow']
    assert flow['stage']=='needs_reconnect' and flow['primary_action']=='start_qr'
    flow=await action(flow,'start_qr',confirm_credential_storage=True,confirm_provider_terms=True)
    assert flow['stage']=='scan' and flow['primary_action']=='poll_qr' and 'cancel' in flow['allowed_actions']
    # Cancelling a renewal discards its QR, not the retained account/binding.
    flow=await action(flow,'cancel')
    assert flow['stage']=='needs_reconnect'
    assert s.store.one('SELECT state FROM weixin_bindings WHERE id=?',(original_binding,))['state']=='active'
    flow=await action(flow,'start_qr',confirm_credential_storage=True,confirm_provider_terms=True)
    confirm(provider)
    flow=await action(flow,'poll_qr')
    assert flow['stage']=='confirm_account' and flow['qr']['re_pair_required']
    flow=await action(flow,'accept_account',confirm_scanner_id=flow['qr']['scanner_id'],confirm_replace_existing=True)
    assert flow['stage']=='pairing' and flow['binding']['id']==original_binding and flow['account_id']==original_account
    s.weixin.commit_updates(s.weixin.claim_poll(original_account),packet(1,flow['binding']['pairing_challenge'],
        create_time_ms=int(clock[0]*1000)+1))
    flow=s.room_channels.read(flow['id'],owner)['flow']
    flow=await action(flow,'confirm_owner',confirm_peer_id='synthetic-peer')
    assert flow['stage']=='ready' and flow['binding']['generation']>original_generation
    assert flow['member_id']==identity['member_id'] and not s.store.all('SELECT * FROM coordination_work')


@pytest.mark.asyncio
async def test_reply_origin_receipt_and_outbox_are_atomic(collab, monkeypatch):
    import sqlite3

    (s, owner, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab)
    incoming = packet(2, '原问题', create_time_ms=int(clock[0] * 1000))
    received = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), incoming)['message_ids'][0]
    entry = next(item for item in s.room_consumers.inbox(identity, worker)['items']
                 if item['object_id'] == received)
    request = {**identity, **credentials, **entry['reply_arguments'], 'body_text': '原回复', 'idempotency_key': key()}
    execute = s.store.execute

    def fail_receipt(sql, parameters=()):
        if sql.startswith('INSERT INTO room_runtime_receipts') and parameters[2].startswith('room_reply:'):
            raise sqlite3.OperationalError('injected reply receipt write failure')
        return execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(s.store, 'execute', fail_receipt)
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            s.room_runtime.reply(request, worker)
    assert not s.store.one('SELECT 1 FROM collaboration_messages WHERE reply_to_id=?', (received,))
    assert not s.store.one('SELECT 1 FROM weixin_deliveries WHERE source_message_id=?', (received,))
    assert not s.store.one('SELECT 1 FROM room_reply_origins')
    assert not s.store.one("SELECT 1 FROM meta WHERE key='collaboration_min_writer'")
    assert next(item for item in s.room_consumers.inbox(identity, worker)['items'] if item['object_id'] == received)

    result = s.room_runtime.reply(request, worker)
    reply_id = result['message']['id']
    assert s.room_runtime.reply(request, worker)['message']['id'] == reply_id
    receipts = s.store.all("SELECT fingerprint,result FROM room_runtime_receipts WHERE action LIKE 'room_reply:%'")
    origins = s.store.all('SELECT * FROM room_reply_origins')
    assert len(receipts) == len(origins) == 1
    origin = origins[0]
    assert origin['message_id'] == reply_id
    assert origin['consumer_session_id'] == credentials['consumer_session_id']
    assert origin['binding_generation'] == identity['binding_generation']
    assert origin['request_digest'] == receipts[0]['fingerprint']
    assert s.store.one("SELECT value FROM meta WHERE key='collaboration_min_writer'")['value'] == '1'
    disposition = json.loads(receipts[0]['result'])['channel_dispositions'][0]
    assert disposition['disposition'] == 'queued'
    assert disposition['delivery_id'] == s.store.one(
        'SELECT id FROM weixin_deliveries WHERE message_id=?', (reply_id,))['id']


@pytest.mark.parametrize('blocked', [False, True])
@pytest.mark.asyncio
async def test_reply_disposition_keeps_authority_and_transport_separate(collab, blocked):
    (s, owner, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab)
    incoming = packet(2, '原问题', create_time_ms=int(clock[0] * 1000))
    received = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), incoming)['message_ids'][0]
    entry = next(item for item in s.room_consumers.inbox(identity, worker)['items']
                 if item['object_id'] == received)
    with s.store.transaction():
        if blocked:
            s.store.execute('UPDATE weixin_bindings SET allow_replies=0 WHERE id=?', (flow['binding']['id'],))
        else:
            s.store.execute('DELETE FROM weixin_contexts WHERE account_id=?', (flow['account_id'],))
    request = {**identity, **credentials, **entry['reply_arguments'], 'body_text': '原回复', 'idempotency_key': key()}
    reply_id = s.room_runtime.reply(request, worker)['message']['id']
    message = s.room_runtime.read(identity['conversation_id'], owner, message_id=reply_id)['message']
    channel, = message['channel_deliveries']
    if blocked:
        assert channel['state'] == 'blocked' and channel['reason_code'] == 'reply_permission_revoked'
        assert not channel.get('id')
        assert not s.store.one('SELECT 1 FROM weixin_deliveries WHERE message_id=?', (reply_id,))
    else:
        assert channel['state'] == 'waiting_context'
        assert s.store.one('SELECT 1 FROM weixin_deliveries WHERE id=? AND message_id=?', (channel['id'], reply_id))
    plan = s.store.all("""EXPLAIN QUERY PLAN SELECT result FROM room_runtime_receipts
        WHERE owner_user_id=? AND conversation_id=? AND json_valid(result)
          AND action LIKE 'room_reply:%' AND json_extract(result,'$.message_id')=?""",
        (owner.user_id, identity['conversation_id'], reply_id))
    assert any('room_reply_receipt_message' in row['detail'] for row in plan)


@pytest.mark.parametrize('ordering_v2', [True, False])
@pytest.mark.asyncio
async def test_room_reply_can_declare_a_visible_delivery_dependency(collab, ordering_v2):
    (s, _, worker, clock, identity, credentials), flow, _, _ = await setup_channel(collab)
    s.config = replace(s.config, weixin_delivery_order_v2=ordering_v2)

    def request_for(sequence):
        incoming = packet(sequence, f'原问题 {sequence}', create_time_ms=int(clock[0] * 1000))
        received = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']), incoming)['message_ids'][0]
        entry = next(item for item in s.room_consumers.inbox(identity, worker)['items']
                     if item['object_id'] == received)
        return {**identity, **credentials, **entry['reply_arguments'],
                'body_text': f'回复 {sequence}', 'idempotency_key': key()}

    first = s.room_runtime.reply(request_for(2), worker)
    prior = s.store.one('SELECT id FROM weixin_deliveries WHERE message_id=?', (first['message']['id'],))
    clock[0] += 3
    request = request_for(3)
    with pytest.raises(DevError) as invalid:
        s.room_runtime.reply({**request, 'depends_on_delivery_id': 'missing', 'dependency_mode': 'requires_success'}, worker)
    assert invalid.value.code == 'WEIXIN_DELIVERY_RELATION_INVALID'
    assert not s.store.one('SELECT 1 FROM collaboration_messages WHERE reply_to_id=?', (request['source_message_id'],))
    args = {**request, 'depends_on_delivery_id': prior['id'], 'dependency_mode': 'requires_success'}
    second = s.room_runtime.reply(args, worker)
    row = s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (second['message']['id'],))
    assert row['depends_on_delivery_id'] == prior['id']
    assert row['dependency_mode'] == 'requires_success'
    if ordering_v2:
        assert json.loads(row['ordering_evidence'])['dependency_mode'] == 'requires_success'
    else:
        assert row['ordering_schema_version'] == 1 and row['ordering_evidence'] == ''
    assert order.dependency_state(s.store, row['id']) == 'waiting'
    s.store.execute("UPDATE weixin_parts SET state='rejected' WHERE delivery_id=?", (prior['id'],))
    assert order.dependency_state(s.store, row['id']) == 'failed'
    with pytest.raises(DevError) as conflict:
        s.room_runtime.reply({**args, 'dependency_mode': 'after_settled'}, worker)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
