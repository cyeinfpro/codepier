"""Room enrollment, canonical inputs and managed controls use real ledgers.

External QR responses and native notifications are deterministic test transports;
these checks never scan a live account or send a private message.
"""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_migration_boundary import legacy, verify_legacy_member, activate
from tests.test_weixin_qr_enrollment import confirm
from tests.test_dot_weixin_channel import packet
from tests.test_room_managed_work import save_room_permissions


def install(c):
    c.s.config=replace(c.s.config,weixin_enabled=True)
    c.joined=asyncio.run(verify_legacy_member(c,c.member))
    assert activate(c)['activated']
    save_room_permissions(c.s,c.owner,c.conversation_id,
        [{'project_id':c.scope['project'],'environment_id':c.scope['environment_id'],
          'capabilities':['read'],'execution_targets':[]}])
    c.member=c.s.rooms.member(c.member['id'],c.conversation_id)
    enqueued=[]
    original=c.s.room_consumers.enqueue
    def record(message,member,principal):
        result=original(message,member,principal)
        enqueued.append((message['id'],member['id']))
        return result
    c.s.room_consumers.enqueue=record
    return enqueued


@pytest.fixture
def room_channel(collab):
    c=legacy(collab)
    c.enqueued=install(c)
    provider=SimpleNamespace(starts=0,response={'status':'wait'})
    class Provider:
        async def qr_start(self):
            provider.starts+=1
            return {'qrcode':'private-qr-value','qrcode_img_content':'https://weixin.qq.com/synthetic-only'}
        async def qr_status(self,*args):
            return provider.response
    c.s.weixin.client_factory=lambda *_:Provider()
    return c,provider


def flow_start(c,**extra):
    return c.s.room_channels.create({'conversation_id':c.conversation_id,'member_id':c.member['id'],
        'idempotency_key':key(),**extra},c.owner)['flow']


def action(c,flow,verb,**extra):
    return asyncio.run(c.s.room_channels.action({'flow_id':flow['id'],'action':verb,
        'expected_version':flow['version'],'idempotency_key':key(),**extra},c.owner))['flow']


def saved_account(c,provider):
    flow=flow_start(c)
    flow=action(c,flow,'start_qr',confirm_credential_storage=True,confirm_provider_terms=True)
    confirm(provider)
    flow=action(c,flow,'poll_qr')
    assert flow['stage']=='confirm_account'
    flow=action(c,flow,'accept_account',label='Synthetic',confirm_scanner_id=flow['qr']['scanner_id'])
    assert flow['stage']=='confirm_binding'
    return flow


def test_flow_refresh_and_replay_keep_one_qr_without_creating_work(room_channel):
    c,p=room_channel
    flow=flow_start(c)
    assert flow_start(c)['id']==flow['id']
    raw={'flow_id':flow['id'],'action':'start_qr','expected_version':flow['version'],'idempotency_key':key(),
         'confirm_credential_storage':True,'confirm_provider_terms':True}
    started=asyncio.run(c.s.room_channels.action(raw,c.owner))
    replay=asyncio.run(c.s.room_channels.action(raw,c.owner))
    assert started==replay and p.starts==1
    assert started['flow']['stage']=='scan'
    assert 'private-qr-value' not in json.dumps(started)
    assert not c.s.store.all('SELECT * FROM coordination_work')
    assert not c.s.store.all('SELECT * FROM operations')
    with pytest.raises(DevError):
        c.s.room_channels.read(flow['id'],c.actor)


def test_same_flow_completes_qr_identity_pair_and_until_revoked_tasks(room_channel):
    c,p=room_channel
    flow=saved_account(c,p)
    flow=action(c,flow,'bind',confirm_external_data_ingress=True,allow_replies=True,confirm_until_revoked=True,
        allow_owner_tasks=True,expected_scope_digest=flow['task_scope']['scope_digest'],
        confirm_owner_identity=True,confirm_future_tasks=True,confirm_result_delivery=True,confirm_pairing_notices=True)
    assert flow['stage']=='pairing' and not flow['work_authorized']
    claim=c.s.weixin.claim_poll(flow['account_id'])
    c.s.weixin.commit_updates(claim,packet(1,flow['binding']['pairing_challenge'],create_time_ms=int(c.clock[0]*1000)+1))
    current=c.s.room_channels.read(flow['id'],c.owner)['flow']
    assert current['stage']=='confirm_owner' and not current['work_authorized']
    current=action(c,current,'confirm_owner',confirm_peer_id=current['binding']['pending_peer_id'])
    assert current['stage']=='ready' and current['peer_verified'] and current['work_authorized']
    assert len(c.s.store.all('SELECT * FROM weixin_accounts'))==1
    assert len(c.s.store.all('SELECT * FROM weixin_bindings'))==1
    assert not c.s.store.all('SELECT * FROM coordination_work')
    assert c.s.store.one('SELECT state FROM room_channel_owner_authorizations WHERE binding_id=?',
        (current['binding']['id'],))['state']=='active'


def test_login_and_chat_pairing_never_imply_owner_execution(room_channel):
    c,p=room_channel
    flow=saved_account(c,p)
    assert not flow['peer_verified'] and not flow['work_authorized']
    flow=action(c,flow,'bind',confirm_external_data_ingress=True,allow_replies=True,confirm_until_revoked=True)
    claim=c.s.weixin.claim_poll(flow['account_id'])
    c.s.weixin.commit_updates(claim,packet(1,flow['binding']['pairing_challenge'],create_time_ms=int(c.clock[0]*1000)+1))
    flow=c.s.room_channels.read(flow['id'],c.owner)['flow']
    assert flow['stage']=='confirm_owner' and not flow['work_authorized']
    with pytest.raises(DevError):
        action(c,flow,'confirm_owner',confirm_peer_id='another-person')
    flow=action(c,flow,'confirm_owner',confirm_peer_id=flow['binding']['pending_peer_id'])
    assert flow['stage']=='ready' and not flow['work_authorized']


def test_cancel_rejects_late_flow_commit_and_does_not_revoke_saved_account(room_channel):
    c,p=room_channel
    flow=saved_account(c,p)
    account=flow['account_id']
    cancelled=action(c,flow,'cancel')
    assert cancelled['stage']=='cancelled'
    with pytest.raises(DevError):
        action(c,flow,'bind',confirm_external_data_ingress=True,allow_replies=True,confirm_until_revoked=True)
    assert c.s.weixin.account(account,c.owner)['state']=='active'
    assert not c.s.store.all('SELECT * FROM weixin_bindings')


def test_canonical_message_is_exactly_queryable_and_retry_safe(room_channel):
    c,p=room_channel
    raw={'conversation_id':c.conversation_id,'member_id':c.member['id'],'body_text':'检查 README，再解释方案',
         'client_message_id':key(),'idempotency_key':key()}
    first=c.s.room_runtime.send(raw,c.owner)
    replay=c.s.room_runtime.send(raw,c.owner)
    assert first==replay and len(c.enqueued)==1
    exact=c.s.room_runtime.read(c.conversation_id,c.owner,client_message_id=raw['client_message_id'])
    assert not exact['not_saved'] and exact['message']['id']==first['message']['id']
    assert c.s.room_runtime.read(c.conversation_id,c.owner,client_message_id=key())['not_saved']
    with pytest.raises(DevError):
        c.s.room_runtime.send({**raw,'idempotency_key':key(),'body_text':'替换原意图'},c.owner)
    rows=c.s.room_runtime.read(c.conversation_id,c.owner)
    assert rows['messages'][0]['client_message_id']==raw['client_message_id']
    assert rows['scope_revision']==rows['visibility_revision']
    assert not c.s.store.all('SELECT * FROM coordination_work')


def test_explicit_unknown_member_never_falls_back_to_lead(room_channel):
    c,p=room_channel
    with pytest.raises(DevError):
        c.s.room_runtime.send({'conversation_id':c.conversation_id,'member_id':'missing',
            'body_text':'检查并修复','client_message_id':key(),'idempotency_key':key()},c.owner)
    assert not c.enqueued
