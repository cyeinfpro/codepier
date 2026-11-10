"""Authorized retention policy, using temporary synthetic databases only."""
import json

import pytest

from hub.collaboration.history_retention import (
    AUDIT_TOMBSTONE, RETIRED_RESPONSE, prune_collaboration_history, replay_response,
)
from shared.util import DevError
from tests.collaboration_support import collab as collab, key

DAY = 86400


def prune(store, now, limit=100):
    return prune_collaboration_history(store, now=now, before=now-30*DAY, limit=limit)


def test_retired_receipt_never_repeats_side_effect_and_keeps_digest(collab):
    service, owner, _, _, room, _, clock, _ = collab
    args = {'idempotency_key': key()}
    calls = []
    def perform():
        calls.append(True)
        return {'id': 'synthetic-result', 'body': 'large old response'}
    with service.store.transaction():
        service.mutation(owner, room, 'retention-test', args, perform)
        saved = service.store.one("SELECT * FROM collaboration_idempotency ORDER BY created DESC LIMIT 1")
        service.store.execute('UPDATE collaboration_idempotency SET created=?', (clock[0]-31*DAY,))
        # An active room remains protected.
        assert prune(service.store, clock[0])['collaboration_idempotency'] == 0
        service.store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (room['id'],))
        assert prune(service.store, clock[0])['collaboration_idempotency'] > 0
        row = service.store.one('SELECT * FROM collaboration_idempotency WHERE id=?', (saved['id'],))
        assert row['digest'] == saved['digest']
        assert row['response'] == RETIRED_RESPONSE
        with pytest.raises(DevError) as exc:
            service.mutation(owner, room, 'retention-test', args, perform)
        assert exc.value.code == 'IDEMPOTENCY_RESPONSE_EXPIRED'
        assert calls == [True]
        with pytest.raises(DevError) as exc:
            service.mutation(owner, room, 'retention-test', {**args, 'changed': True}, perform)
        assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
        assert not any(prune(service.store, clock[0]).values())


def test_inbound_compaction_preserves_uncertainty_references_and_dedupe(collab):
    service, *_, clock, scope = collab
    store, now = service.store, clock[0]
    with store.transaction():
        for n, state, message, age in [
            (1,'committed',None,31), (2,'ignored',None,31),
            (3,'deferred',None,31), (4,'quarantined',None,31),
            (5,'committed','referenced-message',31), (6,'committed',None,29),
        ]:
            store.execute('''INSERT INTO telegram_inbox
                (account_id,update_id,credential_version,generation,source_digest,body,state,message_id,created)
                VALUES ('synthetic-account',?,1,1,'keep-digest','encrypted-synthetic',?,?,?)''',
                (n,state,message,now-age*DAY))
        for state in ('committed','deferred','quarantined'):
            store.execute('INSERT INTO channel_inbound_failures VALUES (?,?,?,?,?,?,?,?,?)',
                (state,'telegram','synthetic-account','{}','encrypted-synthetic',state,'reason',now-31*DAY,now-31*DAY))
        result=prune(store,now)
        assert result['telegram_inbox']==2
        assert result['channel_inbound_failures']==1
        rows=store.all('SELECT * FROM telegram_inbox ORDER BY update_id')
        assert len(rows)==6 and all(r['source_digest']=='keep-digest' for r in rows)
        assert [r['body'] for r in rows]==['','']+['encrypted-synthetic']*4
        assert store.one("SELECT body FROM channel_inbound_failures WHERE id='deferred'")['body']
        assert store.one("SELECT body FROM channel_inbound_failures WHERE id='quarantined'")['body']


def test_retention_batch_is_global_and_audit_keeps_identity(collab):
    service, _, _, _, room, _, clock, _ = collab
    store, now=service.store,clock[0]
    with store.transaction():
        store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (room['id'],))
        for index in range(4):
            store.execute('INSERT INTO collaboration_audit(room_id,actor,action,target,detail,created) VALUES (?,?,?,?,?,?)',
                (room['id'],'synthetic-actor','synthetic-action',str(index),'{"old":"detail"}',now-366*DAY))
        store.execute('INSERT INTO collaboration_audit(room_id,actor,action,target,detail,created) VALUES (?,?,?,?,?,?)',
            (room['id'],'synthetic-actor','synthetic-action','young','{"keep":true}',now-364*DAY))
        assert sum(prune(store,now,2).values())==2
        assert sum(prune(store,now,2).values())==2
        rows=store.all("SELECT * FROM collaboration_audit WHERE action='synthetic-action'")
        assert len(rows)==5
        assert sum(r['detail']==AUDIT_TOMBSTONE for r in rows)==4
        assert all(r['actor']=='synthetic-actor' and r['target'] for r in rows)


def test_pending_goal_protects_receipts_and_audit(collab):
    service, _, _, _, room, _, clock, _=collab
    store,now=service.store,clock[0]
    with store.transaction():
        store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (room['id'],))
        store.execute('UPDATE collaboration_idempotency SET created=?',(now-400*DAY,))
        store.execute('UPDATE collaboration_audit SET created=?',(now-400*DAY,))
        # No approval is needed for a proposed goal; it is still an open dependency.
        store.execute('''INSERT INTO coordination_goals
            (id,room_id,conversation_id,spec,digest,state,created,updated)
            VALUES ('pending-goal',?,?,'{}','synthetic','proposed',?,?)''',
            (room['id'],room['id'],now-400*DAY,now-400*DAY))
        counts=prune(store,now)
        assert counts['collaboration_idempotency']==counts['collaboration_audit']==0


def test_delivery_body_retains_active_cursor_and_pending_siblings(collab):
    service, _, _, _, room, _, clock, _=collab
    store,now=service.store,clock[0]
    with store.transaction():
        for index,(state,ack) in enumerate([('active',10),('revoked',0),('revoked',10)]):
            sub='sub-'+str(index)
            store.execute('''INSERT INTO mcp_event_subscriptions
                (id,room_id,principal,grant_id,name,arguments,secret,state,expires_at,verified_until,key_digest,
                 scan_seq,ack_seq,created,updated) VALUES (?,?, '{}','worker','test','{}','synthetic',?,?,?,'digest',10,?,?,?)''',
                (sub,room['id'],state,now+DAY,now+DAY,ack,now-40*DAY,now-40*DAY))
            store.execute('''INSERT INTO mcp_event_outbox
                (id,room_id,name,object_id,object_version,data,created) VALUES (?,?,'test',?,1,'{}',?)''',
                ('event-'+str(index),room['id'],'object-'+str(index),now-40*DAY))
            store.execute('''INSERT INTO mcp_event_deliveries
                (id,subscription_id,event_id,event_seq,body,state,created,accepted_at)
                VALUES (?,?,?,1,'synthetic-body','accepted',?,?)''',
                ('delivery-'+str(index),sub,'event-'+str(index),now-40*DAY,now-40*DAY))
        assert prune(store,now)['mcp_event_deliveries']==1
        assert store.one("SELECT body FROM mcp_event_deliveries WHERE id='delivery-2'")['body'] is None
        assert store.one("SELECT body FROM mcp_event_deliveries WHERE id='delivery-0'")['body']
        assert store.one("SELECT body FROM mcp_event_deliveries WHERE id='delivery-1'")['body']
        assert store.one('SELECT COUNT(*) n FROM mcp_event_outbox')['n']==3


def test_retention_requires_transaction_and_safe_bounds(collab):
    store,now=collab[0].store,collab[6][0]
    with pytest.raises(RuntimeError):
        prune(store,now)
    with store.transaction():
        with pytest.raises(ValueError):
            prune_collaboration_history(store,now=now,before=now,limit=100)
        with pytest.raises(ValueError):
            prune(store,now,501)


def test_room_receipts_backfill_only_verified_archived_scope_and_dry_run(collab):
    from hub.collaboration.common import canonical
    service,owner,_,_,room,_,clock,_=collab
    store,now=service.store,clock[0]
    args={'idempotency_key':key(),'conversation_id':room['id']}
    with store.transaction():
        service.rooms.mutation(owner,'retention-test',args,lambda:{'conversation_id':room['id']})
        assert store.one('SELECT conversation_id FROM room_mutation_scopes')['conversation_id']==room['id']
        store.execute('UPDATE room_mutations SET created_at=?',(now-31*DAY,))
        store.execute('INSERT INTO room_mutations VALUES (?,?,?,?)',
            ('legacy-room','digest',canonical({'conversation_id':room['id']}),now-31*DAY))
        store.execute('INSERT INTO room_mutations VALUES (?,?,?,?)',
            ('unknown-room','digest','{"unrecognized":"protected"}',now-31*DAY))
        assert prune(store,now)['room_mutations']==0
        store.execute("UPDATE conversation_rooms SET state='archived',archived_at=? WHERE id=?",(now-31*DAY,room['id']))
        changes=store.db.total_changes
        preview=prune_collaboration_history(store,now=now,before=now-30*DAY,dry_run=True)
        assert preview['room_mutations']==2 and store.db.total_changes==changes
        actual=prune(store,now)
        assert actual==preview
        assert store.one("SELECT conversation_id FROM room_mutation_scopes WHERE mutation_id='legacy-room'")['conversation_id']==room['id']
        assert store.one("SELECT result FROM room_mutations WHERE id='unknown-room'")['result']!=''
        with pytest.raises(DevError) as exc:
            service.rooms.mutation(owner,'retention-test',args,lambda:pytest.fail('must not repeat'))
        assert exc.value.code=='IDEMPOTENCY_RESPONSE_EXPIRED'


def test_room_reply_retention_keeps_original_channel_disposition_and_fingerprint(collab):
    service, owner, _, _, room, _, clock, _ = collab
    store, now = service.store, clock[0]
    identity = (owner.user_id, room['id'], 'room_reply:member', key())
    disposition = {'channel': 'weixin', 'binding_id': 'binding', 'disposition': 'blocked',
                   'reason_code': 'authorization_revoked', 'delivery_id': None}
    with store.transaction():
        service.room_channels.save_receipt(identity, 'original-fingerprint', {
            'message_id': 'reply-id', 'source_message_id': 'source-id', 'binding_generation': 1,
            'complete': True, 'channel_dispositions': [disposition], 'transient_detail': 'discard'})
        store.execute('UPDATE room_runtime_receipts SET created=?', (now - 31 * DAY,))
        assert prune(store, now)['room_runtime_receipts'] == 0
        store.execute("UPDATE conversation_rooms SET state='archived',archived_at=? WHERE id=?",
                      (now - 31 * DAY, room['id']))
        assert prune(store, now)['room_runtime_receipts'] == 1
        assert prune(store, now)['room_runtime_receipts'] == 0
        row = service.room_channels.receipt_record(identity)
        assert row['fingerprint'] == 'original-fingerprint'
        tombstone = json.loads(row['result'])
        assert tombstone['channel_dispositions'] == [disposition]
        assert 'transient_detail' not in tombstone
        with pytest.raises(DevError) as expired:
            replay_response(row['result'])
        assert expired.value.code == 'IDEMPOTENCY_RESPONSE_EXPIRED'
        assert expired.value.details == {'message_id': 'reply-id'}


def test_expired_legacy_update_receipt_never_advises_resend(collab,monkeypatch):
    from hub.collaboration.common import digest
    service,owner,_,_,room,_,clock,_=collab
    store,now=service.store,clock[0]
    raw={'kind':'update','request_key':'synthetic-old-key'}
    dot={'id':'synthetic-dot','conversation_id':room['id']}
    slot={'grant_id':'worker'}
    monkeypatch.setattr(service.dot_chat,'scope',lambda *a:(raw,owner,room,slot,dot))
    identifier=digest([room['space_id'],room['owner_user_id'],slot['grant_id'],
        'dot_reply_update:'+dot['id'],room['id'],raw['request_key']])
    with store.transaction():
        store.execute('INSERT INTO collaboration_idempotency VALUES (?,?,?,?,?)',
            (identifier,room['id'],'digest','{"message_id":"old-message"}',now-31*DAY))
        store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?",(room['id'],))
        assert prune(store,now)['collaboration_idempotency']==1
    receipt=service.task_liveness.receipt(raw,owner)
    assert receipt['committed'] is True
    assert receipt['receipt_state']=='expired'
    assert receipt['safe_to_resend_same_request'] is False


@pytest.mark.asyncio
async def test_consumer_receipts_only_retire_settled_sessions(collab):
    from tests.test_dot_consumer_sessions import connect,request,lease
    from hub.collaboration.history_retention import replay_response
    c=await connect(collab)
    session=c.s.dot_consumers.begin(request(c),c.actor)
    c.s.dot_consumers.heartbeat(lease(c,session,lease_seconds=60),c.actor)
    store,now=c.s.store,c.clock[0]
    with store.transaction():
        store.execute('UPDATE dot_consumer_mutations SET created=?',(now-31*DAY,))
        assert prune(store,now)['dot_consumer_mutations']==0
        store.execute("UPDATE dot_consumer_sessions SET state='expired',updated=?,lease_until=?",
            (now-31*DAY,now-31*DAY))
        # Even a terminal session protects its receipt while its subscription is active.
        assert prune(store,now)['dot_consumer_mutations']==0
        store.execute("UPDATE mcp_event_subscriptions SET state='revoked' WHERE id=?",(c.sub['id'],))
        assert prune(store,now)['dot_consumer_mutations']==1
        row=store.one('SELECT * FROM dot_consumer_mutations')
        assert row['session_id']==session['consumer_session_id']
        assert row['request_digest']
        with pytest.raises(DevError) as exc:
            replay_response(row['result'])
        assert exc.value.code=='IDEMPOTENCY_RESPONSE_EXPIRED'


def test_audit_longer_window_is_honored(collab):
    service,_,_,_,room,_,clock,_=collab
    store,now=service.store,clock[0]
    with store.transaction():
        store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?",(room['id'],))
        store.execute('UPDATE collaboration_audit SET created=?',(now-400*DAY,))
        preview=prune_collaboration_history(store,now=now,before=now-30*DAY,audit_before=now-730*DAY,dry_run=True)
        assert preview['collaboration_audit']==0
        with pytest.raises(ValueError):
            prune_collaboration_history(store,now=now,before=now-30*DAY,audit_before=now-300*DAY)


def test_terminal_goal_still_protects_blocked_work_and_uncertain_operations(collab):
    from hub.collaboration.common import canonical
    from tests.test_collaboration_coordination import setup_goal,make_work
    coordinator,service,owner,worker,broad,goal,scope,_=setup_goal(collab)
    work=make_work(coordinator,scope,goal,worker)
    store,now=service.store,collab[6][0]
    room=collab[4]
    with store.transaction():
        store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?",(room['id'],))
        store.execute("UPDATE conversation_rooms SET state='archived',archived_at=? WHERE id=?",(now-31*DAY,room['id']))
        store.execute('UPDATE collaboration_idempotency SET created=?',(now-31*DAY,))
        store.execute("UPDATE coordination_goals SET state='cancelled' WHERE id=?",(goal['id'],))
        # Approval creates an initial coordinator work item as well as our explicit
        # item. Settle that sibling so this test isolates the target's guard.
        store.execute("UPDATE coordination_work SET state='cancelled' WHERE goal_id=?",(goal['id'],))
        store.execute("UPDATE coordination_work SET state='blocked' WHERE id=?",(work['id'],))
        store.execute('INSERT INTO room_mutations VALUES (?,?,?,?)',
            ('guard-room','digest',canonical({'conversation_id':room['id']}),now-31*DAY))
        store.execute('INSERT INTO collaboration_idempotency VALUES (?,?,?,?,?)',
            ('guard-partition',room['id'],'digest','{"synthetic":"response"}',now-31*DAY))
        assert prune(store,now)['collaboration_idempotency']==0
        assert prune(store,now)['room_mutations']==0
        store.execute("UPDATE coordination_work SET state='cancelled' WHERE id=?",(work['id'],))
        stored=store.one('SELECT * FROM coordination_work WHERE id=?',(work['id'],))
        store.execute('''INSERT INTO operations
            (id,project_id,actor,tool,args_summary,fingerprint,state,created,updated)
            VALUES ('pending-operation','proj','synthetic','exec','{}','digest','cancelling',?,?)''',
            (now-31*DAY,now-31*DAY))
        store.execute('''INSERT INTO coordination_operations
            (operation_id,goal_id,work_item_id,approval_id,attempt,fencing_token,project_id,grant_id,request_digest,created)
            VALUES ('pending-operation',?,?,?,0,0,'proj','broad','digest',?)''',
            (goal['id'],work['id'],stored['approval_id'],now-31*DAY))
        for state in ('cancelling','unknown','needs_review'):
            store.execute("UPDATE operations SET state=? WHERE id='pending-operation'",(state,))
            result=prune(store,now)
            assert result['collaboration_idempotency']==result['room_mutations']==0
        store.execute("UPDATE operations SET state='cancelled' WHERE id='pending-operation'")
        result=prune(store,now)
        assert result['collaboration_idempotency']>0 and result['room_mutations']==1
