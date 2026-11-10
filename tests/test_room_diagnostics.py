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
    receipt = member['notification']['correlated_receipt']
    assert receipt['state'] == 'claimed' and receipt['consumer_session_id'] == credentials['consumer_session_id']
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
            s.weixin.delivery.finish(claim, result={'provider_state': 'accepted'})
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


@pytest.mark.asyncio
async def test_wake_evidence_follows_original_ticket_and_preserves_coalescing(collab):
    s, owner, worker, clock, identity, credentials = await connected(collab)
    args = {name: identity[name] for name in ('conversation_id', 'member_id')}

    def send(text):
        return s.room_runtime.send({'conversation_id': identity['conversation_id'], 'body_text': text,
            'client_message_id': key(), 'idempotency_key': key()}, owner)['message']

    source = send('原始问题')
    inbox = s.room_consumers.inbox(identity, worker)
    item = next(row for row in inbox['items'] if row['kind'] == 'message')
    request = {**identity, **credentials, 'source_message_id': source['id'], 'source_version': item['source_version'],
        'source_digest': item['source_digest'], 'expected_inbox_revision': inbox['inbox_revision'],
        'body_text': '原回复', 'complete': True, 'idempotency_key': key()}
    reply = s.room_runtime.reply(request, worker)['message']
    assert s.room_runtime.reply(request, worker)['message']['id'] == reply['id']
    origin = next(row for row in s.rooms.diagnostics({'conversation_id': args['conversation_id']}, owner)['traces']
                  if row['kind'] == 'reply' and row['message_id'] == reply['id'])
    assert origin['consumer_session_id'] == credentials['consumer_session_id']
    assert origin['source_message_id'] == source['id']

    send('尚未领取的第二条')
    send('尚未领取的第三条')
    await s.events.tick()
    page = s.rooms.diagnostics(args, owner)
    assert page['read_only'] and page['counts_are_global_totals'] is False
    original = next(row for row in page['items'] if row['state'] == 'redeemed')
    assert original['consumer_session_id'] == credentials['consumer_session_id']
    assert original['created_reply_count'] == 1
    waiting = next(row for row in page['items'] if row['state'] == 'accepted_waiting_claim')
    merged = next(row for row in page['items'] if row['state'] == 'coalesced_before_send')
    assert merged['retained_notification_delivery_id'] == waiting['notification_delivery_id']
    assert not waiting['consumer_session_id']
    s.store.execute('UPDATE dot_wake_tickets SET expires_at=? WHERE delivery_key=?',
                    (clock[0] - 1, waiting['event_id']))
    s.store.execute('UPDATE mcp_event_deliveries SET accepted_at=? WHERE id=?',
                    (clock[0] + 1, original['notification_delivery_id']))
    by_event = {row['event_id']: row for row in s.rooms.diagnostics(args, owner)['items']}
    assert by_event[waiting['event_id']]['state'] == 'accepted_without_observed_claim'
    assert by_event[original['event_id']]['claim_preceded_recorded_acceptance']
    assert by_event[original['event_id']]['created_reply_count'] == 1
    other = s.rooms.create({'title': '其他房间', 'idempotency_key': key()}, owner)['room']
    with pytest.raises(DevError):
        s.rooms.diagnostics({**args, 'conversation_id': other['id']}, owner)


def test_delivery_display_preserves_distinct_owner_and_provider_evidence():
    for state, display_state, title in (
        ('accepted', 'accepted', '接口已受理'),
        ('owner_confirmed', 'owner_confirmed', '由本人核对收到'),
    ):
        result = delivery_display('completed', [{'channel': 'weixin', 'state': 'accepted',
                                                 'parts': [{'state': state}]}])
        assert result['state'] == display_state and title in result['title']
    mixed = delivery_display('completed', [{'channel': 'weixin', 'state': 'needs_attention',
        'parts': [{'state': 'accepted'}, {'state': 'accepted'}, {'state': 'unknown'}]}])
    assert mixed['state'] == 'unknown'
    assert '已受理 2 段，1 段待核对' in mixed['reason']
    stopped = delivery_display('completed', [{'channel': 'weixin', 'state': 'needs_attention',
        'parts': [{'state': 'accepted'}, {'state': 'cancelled'}]}])
    assert stopped['state'] == 'partial'


def test_missing_or_unrecognized_transport_receipt_never_claims_acceptance():
    assert delivery_display('completed', []) is None
    assert delivery_display('completed', [{'channel': 'weixin', 'state': 'unrecognized'}])['state'] == 'unavailable'
    accepted = delivery_display('completed', [{'channel': 'weixin', 'state': 'accepted'}])
    assert accepted['state'] == 'accepted' and '不证明对方已读' in accepted['reason']


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
