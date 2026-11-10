"""Room conversation acceptance with real ledgers and synthetic transport."""

from concurrent.futures import ThreadPoolExecutor
from sqlite3 import IntegrityError
from threading import Barrier, Event

import pytest

from hub.collaboration.history_retention import RETIRED_RESPONSE
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_context import migrated_partition_source, send
from tests.test_room_goal_scope_boundaries import worker_for
from tests.test_room_managed_work import connected, save_room_permissions
from tests.test_room_weixin_native import setup_channel


@pytest.fixture(autouse=True)
def decoupled_turns(monkeypatch):
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'decoupled')


def reply_request(service, member, actor, credentials, source, **options):
    page = service.room_consumers.inbox(member, actor)
    item = next(item for item in page['items'] if item['object_id'] == source)
    return {
        **member, **credentials, **item['reply_arguments'],
        'body_text': '已核对原输入。',
        'complete': True,
        'publish_work': False,
        'input_handling': 'answered',
        'idempotency_key': key(),
        **options,
    }


async def weixin_reply(collab, **options):
    (service, owner, actor, clock, member, credentials), flow, *_ = await setup_channel(collab)
    packet_time = int(clock[0] * 1000)
    result = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']),
        packet(2, '原微信输入', create_time_ms=packet_time))
    source = result['message_ids'][0]
    return service, owner, actor, reply_request(service, member, actor, credentials, source, **options)


def reply_rows(store, raw):
    return store.all(
        "SELECT id, body FROM collaboration_messages WHERE reply_to_id=? AND kind='reply'",
        (raw['source_message_id'],))


def receipts(store, raw):
    return store.all(
        '''SELECT fingerprint, result FROM room_runtime_receipts
        WHERE conversation_id=? AND action=? AND request_key=?''',
        (raw['conversation_id'], 'room_reply:' + raw['member_id'], raw['idempotency_key']))


def source_entry(store, raw):
    return store.one(
        '''SELECT acknowledged_at FROM dot_mailbox_entries
        WHERE member_id=? AND object_id=? AND object_version=? AND kind='message' ''',
        (raw['member_id'], raw['source_message_id'], raw['source_version']))


@pytest.mark.asyncio
async def test_concurrent_same_reply_commits_one_logical_weixin_delivery(collab):
    service, _, actor, raw = await weixin_reply(collab)
    ready = Barrier(10, timeout=10)

    def submit(_):
        ready.wait()
        return service.room_runtime.reply(raw, actor)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(submit, range(100)))

    reply_ids = {result['message']['id'] for result in results}
    assert len(reply_ids) == 1
    reply_id = reply_ids.pop()
    assert sum(result['replayed'] is False for result in results) == 1
    assert all(result['historical_result']['message_id'] == reply_id for result in results)
    assert [row['id'] for row in reply_rows(service.store, raw)] == [reply_id]
    saved = receipts(service.store, raw)
    assert len(saved) == 1
    assert service.store.all(
        'SELECT message_id, request_digest FROM room_reply_origins WHERE message_id=?', (reply_id,),
    ) == [{'message_id': reply_id, 'request_digest': saved[0]['fingerprint']}]
    assert service.store.all('SELECT message_id, source_message_id FROM weixin_deliveries') == [
        {'message_id': reply_id, 'source_message_id': raw['source_message_id']}]
    assert source_entry(service.store, raw)['acknowledged_at'] is not None

    for change in ({'body_text': '另一段回复'}, {'source_version': raw['source_version'] + 1},
                   {'publish_work': True}, {'yield_after': True}):
        with pytest.raises(DevError) as conflict:
            service.room_runtime.reply({**raw, **change}, actor)
        assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert receipts(service.store, raw) == saved


@pytest.mark.asyncio
async def test_receipt_insert_failure_rolls_back_reply_ack_origin_outbox_and_yield(collab):
    service, _, actor, raw = await weixin_reply(collab, yield_after=True)
    store = service.store
    with store.transaction():
        store.execute("""CREATE TRIGGER test_abort_reply_receipt BEFORE INSERT ON room_runtime_receipts
            WHEN NEW.action LIKE 'room_reply:%'
            BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END""")

    with pytest.raises(IntegrityError, match='injected receipt failure'):
        service.room_runtime.reply(raw, actor)

    assert not reply_rows(store, raw)
    assert not receipts(store, raw)
    assert not store.all('SELECT message_id FROM room_reply_origins')
    assert not store.all('SELECT id FROM weixin_deliveries')
    assert source_entry(store, raw)['acknowledged_at'] is None
    assert store.one('SELECT state FROM dot_consumer_sessions WHERE id=?',
                     (raw['consumer_session_id'],))['state'] == 'active'

    with store.transaction():
        store.execute('DROP TRIGGER test_abort_reply_receipt')
    reply = service.room_runtime.reply(raw, actor)
    assert reply['historical_result']['yielded_at_commit']
    assert len(reply_rows(store, raw)) == len(receipts(store, raw)) == 1
    assert len(store.all('SELECT id FROM weixin_deliveries')) == 1
    assert source_entry(store, raw)['acknowledged_at'] is not None


@pytest.mark.asyncio
async def test_lost_response_recovers_original_effect_without_yielding_replacement_turn(collab):
    service, owner, actor, raw = await weixin_reply(collab, yield_after=True)
    store = service.store
    reply = service.room_runtime.reply(raw, actor)
    original_id = reply['message']['id']
    assert reply['historical_result']['yielded_at_commit']

    replay = service.room_runtime.reply(raw, actor)
    assert replay['replayed'] and not replay['effects_created']
    assert replay['historical_result'] == reply['historical_result']

    newcomer = send(service, owner, raw, '另一话题的输入。', member_id=raw['member_id'])
    await service.events.tick()
    ticket = next(payload['data']['wake_ticket'] for payload in reversed(service.native_test_callbacks)
                  if payload['data'].get('member_id') == raw['member_id'])
    member = {k: raw[k] for k in ('conversation_id', 'member_id', 'binding_generation', 'connection_proof')}
    turn = service.room_consumers.begin({**member, 'wake_ticket': ticket, 'consumer_run_id': key(),
                                         'idempotency_key': key(), 'include_inbox': True}, actor)
    assert turn['consumer_fence'] > raw['consumer_fence']
    assert newcomer['id'] in {item['object_id'] for item in turn['inbox']['items']}

    retried = service.room_runtime.reply({
        **raw, **{k: turn[k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')},
        'expected_inbox_revision': turn['inbox_revision'],
    }, actor)
    assert retried['message']['id'] == original_id
    assert retried['historical_result'] == reply['historical_result']
    assert retried['replayed'] and not retried['effects_created']
    assert retried['current_consumer']['consumer_session_id'] == turn['consumer_session_id']
    assert retried['current_consumer']['state'] == 'active'
    assert store.one('SELECT state FROM dot_consumer_sessions WHERE id=?',
                     (turn['consumer_session_id'],))['state'] == 'active'
    assert store.all('SELECT consumer_session_id FROM room_reply_origins WHERE message_id=?', (original_id,)) == [
        {'consumer_session_id': raw['consumer_session_id']}]
    assert len(reply_rows(store, raw)) == len(store.all('SELECT id FROM weixin_deliveries')) == 1


@pytest.mark.asyncio
async def test_retired_reply_response_preserves_dedupe_even_after_yield(collab):
    service, _, actor, raw = await weixin_reply(collab, yield_after=True)
    store = service.store
    response = service.room_runtime.reply(raw, actor)
    assert response['historical_result']['yielded_at_commit']
    old = receipts(store, raw)[0]
    with store.transaction():
        store.execute('UPDATE room_runtime_receipts SET result=? WHERE action=? AND request_key=?',
                      (RETIRED_RESPONSE, 'room_reply:' + raw['member_id'], raw['idempotency_key']))

    with pytest.raises(DevError) as retired:
        service.room_runtime.reply(raw, actor)
    assert retired.value.code == 'IDEMPOTENCY_RESPONSE_EXPIRED'
    with pytest.raises(DevError) as conflict:
        service.room_runtime.reply({**raw, 'body_text': '修改已提交的回复'}, actor)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert receipts(store, raw) == [{**old, 'result': RETIRED_RESPONSE}]
    assert len(reply_rows(store, raw)) == len(store.all('SELECT id FROM weixin_deliveries')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reply_first', [True, False])
async def test_permission_revoke_and_reply_linearize_without_blocking_independent_chat(collab, reply_first):
    service, owner, worker, *_ = collab
    actor = worker_for(service, worker, 'room-reply-race', scopes=('read',))
    room, source, _ = migrated_partition_source(service, owner,
        [{'project': 'proj'}, {'project': 'otherproj'}], 'otherproj', 'production', '原 B 项目输入')
    save_room_permissions(service, owner, room['id'], [{'project_id': 'proj'}, {'project_id': 'otherproj'}])
    service, owner, actor, _, member, credentials = await connected(
        collab, worker_override=actor, conversation_id=room['id'])
    scoped = send(service, owner, member, '请回复 B 项目的原输入。',
                  member_id=member['member_id'], reply_to_id=source['id'])
    independent = send(service, owner, member, '独立话题。', member_id=member['member_id'])
    scoped_reply = reply_request(service, member, actor, credentials, scoped['id'], body_text='B 项目详情。')
    independent_reply = reply_request(service, member, actor, credentials, independent['id'])

    def reply():
        return service.room_runtime.reply(scoped_reply, actor)

    def revoke():
        return save_room_permissions(service, owner, room['id'], [{'project_id': 'proj'}])

    first_action, second_action = (reply, revoke) if reply_first else (revoke, reply)
    first_mutated = Event()
    second_started = Event()

    def first():
        with service.store.transaction():
            result = first_action()
            first_mutated.set()
            assert second_started.wait(10)
        return result

    def second():
        assert first_mutated.wait(10)
        second_started.set()
        return second_action()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result, second_result = pool.submit(first), pool.submit(second)
        first_value = first_result.result()
        if reply_first:
            second_result.result()
            message_id = first_value['message']['id']
            recovered = service.room_runtime.reply(scoped_reply, actor)
            assert recovered['historical_result']['committed']
            assert recovered['message'] == {'id': message_id, 'withheld': True}
            assert len(reply_rows(service.store, scoped_reply)) == 1
        else:
            with pytest.raises(DevError):
                second_result.result()
            assert not reply_rows(service.store, scoped_reply)
            assert not receipts(service.store, scoped_reply)

    unrelated = service.room_runtime.reply(independent_reply, actor)
    assert unrelated['message']['reply_to_id'] == independent['id']
