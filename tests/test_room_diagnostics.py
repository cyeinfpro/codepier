"""Original receipt projections: no host, model or provider success is invented."""
import json

import pytest

from hub.collaboration.common import digest
from hub.collaboration.room_diagnostics import delivery_display
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_managed_work import connected
from tests.test_room_goal_scope_boundaries import admit_read, report, final_request
from tests.test_room_weixin_native import setup_channel
from tests.test_dot_weixin_channel import packet


@pytest.mark.asyncio
async def test_room_diagnostics_has_real_consumer_and_queue_evidence_without_mutation_or_secrets(collab, monkeypatch):
    s, owner, worker, clock, identity, credentials = await connected(collab)
    source = s.room_runtime.send({'conversation_id': identity['conversation_id'],
        'body_text': 'private diagnostic source must never be exported',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    clock[0] += 12
    changes = s.store.one('SELECT total_changes() AS n')['n']
    monkeypatch.setattr(s.store, 'decrypt', lambda *_: pytest.fail('A diagnostic read decrypted a credential'))
    view = s.rooms.diagnostics({'conversation_id': identity['conversation_id']}, owner)
    assert s.store.one('SELECT total_changes() AS n')['n'] == changes
    assert view['read_only'] and view['redacted'] and not view['grants_authority']
    member = view['members'][0]
    assert member['consumer']['id'] == credentials['consumer_session_id']
    assert member['consumer']['state'] == 'active'
    assert member['notification']['last_accepted_at'] is not None
    assert member['notification']['read_verified'] is False
    queue = next(item for item in view['queues'] if item['kind'] == 'waiting_consumer')
    assert queue['pending_count'] == 1 and queue['oldest_age_seconds'] == 12
    trace = next(item for item in view['traces'] if item['kind'] == 'message')
    assert trace['message_id'] == source['id'] and trace['member_id'] == identity['member_id']
    exposed = json.dumps(view)
    for private in (source['body_text'], identity['connection_proof'], credentials['consumer_proof'], 'callback.example.invalid'):
        assert private not in exposed
    assert view['limits']['dispatch_concurrency'] == 4
    rates = {item['kind']: item['rate_per_second'] for item in view['budgets'] if item.get('kind')}
    assert rates == {'callback_global': 8, 'callback_owner': 4}


@pytest.mark.asyncio
async def test_completed_goal_keeps_unknown_wechat_delivery_separate_and_correlates_original_operation(collab):
    (s, owner, actor, clock, identity, credentials), flow, _, _ = await setup_channel(collab, tasks=True)
    message_id = s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(2, '读取 README，报告原操作结果。', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message_id,))
    started = s.room_runtime.start_work({**identity, **credentials, 'source_message_id': source['id'],
        'source_version': source['version'], 'source_digest': digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision': s.room_consumers.inbox(identity, actor)['inbox_revision'],
        'confirm_task': True, 'acceptance': '保留真实原操作证据', 'idempotency_key': key()}, actor)
    goal_id = started['goal_id']
    item = s.store.one('SELECT * FROM coordination_work WHERE id=?', (started['next_request']['arguments']['work_item_id'],))
    lease, operation = admit_read(s, identity, credentials, actor, goal_id, item, 'proj')
    done = report(s, identity, credentials, actor, goal_id, lease, operation)
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (message_id,))
    clock[0] += 1
    final = s.room_runtime.reply(final_request(s, identity, credentials, actor, source,
        [{'work_item_id': done['id'], 'version': done['version'], 'digest': digest(done['result'])}]), actor)['message']
    final_delivery = s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (final['id'],))
    # Exercise the real prepare/send/finish journal with a synthetic transport
    # loss. Older progress parts must finish first, respecting original order.
    while candidates := s.weixin.delivery.candidates(clock[0]):
        part = s.store.one('SELECT * FROM weixin_parts WHERE id=?', (candidates[0]['id'],))
        claim = s.weixin.delivery.claim(part['id'])
        assert claim
        s.weixin.delivery.mark_sending(claim, [{'type': 1, 'text_item': {'text': 'synthetic protocol fixture'}}])
        if part['delivery_id'] == final_delivery['id']:
            s.weixin.delivery.finish(claim, error=TimeoutError('synthetic response lost'))
        else:
            s.weixin.delivery.finish(claim, result={'ret': 0, 'synthetic': True})
    before = s.store.one('SELECT total_changes() AS n')['n']
    view = s.room_runtime.work_view(s.room_runtime.goal(goal_id), owner)
    diagnostic = s.rooms.diagnostics({'conversation_id': identity['conversation_id']}, owner)
    assert s.store.one('SELECT total_changes() AS n')['n'] == before
    assert view['state'] == 'completed'
    assert view['delivery_display']['state'] == 'unknown'
    assert view['delivery_display']['title'] == '已完成，微信回复发送待核对'
    assert {row['message_id'] for row in view['channel_deliveries']} == {final['id']}
    assert view['channel_deliveries'][0]['parts'][0]['state'] == 'unknown'
    operation_trace = next(row for row in diagnostic['traces'] if row.get('operation_id') == operation['operation_id'])
    assert operation_trace['goal_id'] == goal_id and operation_trace['work_item_id'] == item['id']
    delivery_trace = next(row for row in diagnostic['traces'] if row.get('delivery_id') == final_delivery['id'])
    assert delivery_trace['goal_id'] == goal_id and delivery_trace['source_message_id'] == message_id
    assert delivery_trace['parts'][0]['state'] == 'unknown'
    assert len(s.store.all('SELECT * FROM operations')) == 1
    assert len(s.store.all('SELECT * FROM coordination_goals')) == 1
    assert s.weixin.delivery.claim(view['channel_deliveries'][0]['parts'][0]['id']) is None


@pytest.mark.asyncio
async def test_diagnostics_never_exposes_another_room_to_a_member_connection(collab):
    s, owner, actor, clock, identity, credentials = await connected(collab)
    with pytest.raises(DevError):
        s.rooms.diagnostics({'conversation_id': identity['conversation_id']}, actor)


def test_missing_or_unrecognized_transport_receipt_never_claims_acceptance():
    assert delivery_display('completed', []) is None
    assert delivery_display('completed', [{'channel': 'weixin', 'state': 'unrecognized'}])['state'] == 'unavailable'
    accepted = delivery_display('completed', [{'channel': 'weixin', 'state': 'accepted'}])
    assert accepted['state'] == 'accepted' and '不证明对方已读' in accepted['reason']
    assert delivery_display('completed', [{'channel': 'weixin', 'state': 'waiting_context'}])['state'] == 'pending'
    confirmed = delivery_display('completed', [{'channel': 'weixin', 'state': 'accepted',
                                                'parts': [{'state': 'owner_confirmed'}]}])
    assert confirmed['state'] == 'owner_confirmed' and '房主核对' in confirmed['title']


def test_legacy_team_delivery_projection_uses_current_intent_publication(collab, monkeypatch):
    from tests.test_multidot_team_coordination import setup_team, goal, complete, source, work

    c = setup_team(collab)
    goal(c)
    kickoff = c.q.work_view(c.s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (c.goal['id'],)))
    first_ref = complete(c, kickoff, c.actors[0])['artifact_ref']
    first = c.t.publish({**c.ctx, 'summary': '第一次汇总', 'outcome': 'succeeded',
        'artifact_refs': [first_ref], 'idempotency_key': key()}, c.actors[0])['publication']
    original = c.s.channels.for_messages
    selected = []

    def record(identifiers):
        selected.extend(identifiers)
        return original(identifiers)

    monkeypatch.setattr(c.s.channels, 'for_messages', record)
    c.s.room_runtime.work_read(c.goal['id'], c.owner)
    assert selected == [first['message_id']]
    revised = source(c, text='请在原工作内核对第二个问题')
    c.t.revise_intent({**c.ctx, **{k: revised[k] for k in ('message_id', 'source_version', 'source_digest')},
        'idempotency_key': key()}, c.owner)
    selected.clear()
    view = c.s.room_runtime.work_read(c.goal['id'], c.owner)['work']
    assert view['intent_revision'] == 2 and first['message_id'] not in selected
    c.ctx['expected_intent_revision'] = 2
    second_ref = complete(c, work(c, index=0), c.actors[0])['artifact_ref']
    second = c.t.publish({**c.ctx, 'summary': '第二次汇总', 'outcome': 'succeeded',
        'artifact_refs': [second_ref], 'idempotency_key': key()}, c.actors[0])['publication']
    selected.clear()
    c.s.room_runtime.work_read(c.goal['id'], c.owner)
    assert selected == [second['message_id']]
