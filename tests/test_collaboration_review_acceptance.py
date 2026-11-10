"""Review regressions against an isolated collaboration database."""
import json

import pytest

from hub.collaboration.common import canonical
from hub.collaboration.room_diagnostics import LIMITS
from hub.collaboration.weixin_delivery_order import WAITING_PREDECESSOR, dependency_state
from hub.principal import Principal
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_weixin_native import setup_channel


def queued_replies(ctx, flow, *messages):
    service, _, worker, clock, identity, credentials = ctx
    source_id = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']),
        packet(2, '原问题', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    inbox = service.room_consumers.inbox(identity, worker)
    source = next(item for item in inbox['items'] if item['object_id'] == source_id)
    deliveries = []
    for body in messages:
        result = service.room_runtime.reply({**identity, **credentials, **source['reply_arguments'],
            'expected_inbox_revision': inbox['inbox_revision'], 'body_text': body, 'idempotency_key': key()}, worker)
        deliveries.append(service.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',
                                            (result['message']['id'],)))
        clock[0] += 1
    return deliveries


@pytest.mark.asyncio
async def test_delivery_diagnostics_hide_restricted_edges_and_cross_account_rows(collab):
    ctx, flow, _, _ = await setup_channel(collab)
    service, owner, worker, _, identity, _ = ctx
    hidden, public = queued_replies(ctx, flow, '不应出现在诊断里的正文', '可见的后续回复')
    row = service.store.one('SELECT body FROM collaboration_messages WHERE id=?', (hidden['message_id'],))
    body = json.loads(row['body'])
    body['provenance_scopes'] = [{'project_id': 'otherproj', 'environment_id': 'restricted'}]
    service.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?',
                          (canonical(body), hidden['message_id']))

    args = {'conversation_id': identity['conversation_id'], 'delivery_id': public['id']}
    view = service.rooms.diagnostics(args, owner)
    assert view['direct_blocker'] == {'restricted': True}
    assert view['roots'] == [] and view['affected_waiting_count'] == view['direct_waiting_count'] == 0
    serialized = json.dumps(view, ensure_ascii=False)
    for private in (hidden['id'], hidden['message_id'], body['body_text'], 'otherproj'):
        assert private not in serialized
    with pytest.raises(DevError) as denied:
        service.rooms.diagnostics({**args, 'delivery_id': hidden['id']}, owner)
    assert denied.value.code == 'NOT_FOUND'

    other = Principal('panel:other', 'other', {'read', 'write'}, ['*'], admin=True)
    other_room = service.rooms.create({'title': '另一个房间', 'idempotency_key': key()}, owner)['room']
    for caller, room_id in ((worker, identity['conversation_id']), (owner, other_room['id']),
                            (other, identity['conversation_id'])):
        with pytest.raises(DevError):
            service.rooms.diagnostics({**args, 'conversation_id': room_id}, caller)


@pytest.mark.asyncio
async def test_large_delivery_history_uses_point_lookup_and_caps_relationship_summary(collab):
    ctx, flow, _, _ = await setup_channel(collab)
    service, owner, _, clock, identity, _ = ctx
    original, = queued_replies(ctx, flow, '原回复')
    limit = LIMITS['relationship_page']
    store = service.store
    with store.transaction():
        store.execute("""WITH RECURSIVE seq(n) AS (
            VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<2048)
            INSERT INTO weixin_deliveries
                (id,binding_id,generation,message_id,message_version,message_digest,source_message_id,
                 peer_id,state,created,updated,ordering_key,projection_message_id)
            SELECT 'history-'||n,binding_id,generation,'message-'||n,message_version,message_digest,
                source_message_id,CASE WHEN n<=? THEN peer_id ELSE 'other-'||n END,
                'waiting_media',?+n,?+n,ordering_key,message_id
            FROM weixin_deliveries CROSS JOIN seq WHERE id=?""",
            (limit + 1, clock[0], clock[0], original['id']))
    target = f'history-{limit + 1}'
    assert dependency_state(store, target) == 'waiting'
    plan = store.all(f"""EXPLAIN QUERY PLAN SELECT {WAITING_PREDECESSOR}
        FROM weixin_deliveries d WHERE d.id=?""", (target,))
    details = [row['detail'] for row in plan]
    assert any('SEARCH d USING INDEX' in row for row in details), details
    assert any('weixin_delivery_order' in row for row in details), details
    assert not any('SCAN prior' in row or 'SCAN earlier' in row for row in details), details

    result = service.rooms.diagnostics({'conversation_id': identity['conversation_id'],
        'delivery_id': original['id']}, owner)
    assert result['truncated'] and result['counts_are_global_totals'] is False
    assert result['direct_waiting_count'] == result['affected_waiting_count'] == limit
    summary = service.rooms.diagnostics({'conversation_id': identity['conversation_id']}, owner)
    assert summary['read_only'] and len(summary['traces']) <= LIMITS['traces']
