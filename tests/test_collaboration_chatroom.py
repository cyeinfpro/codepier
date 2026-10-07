"""Isolated chatroom scope, migration, delivery and explicit task conversion."""
import base64
import json
import secrets
from dataclasses import replace

import pytest

from hub.collaboration.common import MESSAGE_EVENT, STATUS_EVENT, canonical
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.schema import migrate
from shared.util import DevError
from tests.collaboration_support import collab, key


def message(c, **extra):
    return {**c[-1], 'room_id': c[4]['id'], 'body_text': '讨论 ordinary 100%_ 字段',
            'client_message_id': key(), 'idempotency_key': key(), **extra}


def grant_speech(c, principal=None, enabled=True):
    s, owner, worker, _, room, _, _, scope = c
    old = s.store.one('SELECT version FROM conversation_writers WHERE room_id=? AND grant_id=?',
                      (room['id'], (principal or worker).grant_id))
    return s.chatroom.access({**scope, 'expected_version': old['version'] if old else 0, 'room_id': room['id'], 'grant_id': (principal or worker).grant_id,
                             'enabled': enabled, 'idempotency_key': key()}, owner)


def join(c, grant=None, label='slot'):
    s, owner, worker, _, _, _, _, scope = c
    slot = s.joining.create({**scope, 'label': label, 'kind': 'work_cloud', 'idempotency_key': key()}, owner)['slot']
    return s.joining.join({'code': slot['join_code'], 'idempotency_key': key()}, grant or worker)['slot']


class Receiver:
    def __init__(self):
        self.requests = []

    async def __call__(self, url, body, headers):
        self.requests.append(json.loads(body))
        payload = json.loads(body)
        return Reply(200, canonical({'challenge': payload['challenge']}).encode()) if payload.get('type') == 'verification' else Reply(204)


def events(c):
    s = c[0]
    s.config = replace(s.config, events_enabled=True)
    receiver = Receiver()
    s.events = EventService(s, receiver)
    return s.events, receiver


def subscription(c, slot, suffix='one', name=MESSAGE_EVENT):
    return {'name': name, 'arguments': {'project_id': c[4]['project_id'], 'environment_id': 'production', 'slot_id': slot['id'],
            **({'conversation_id': c[4]['id']} if name == MESSAGE_EVENT else {})},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/' + suffix,
                         'secret': 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()}}


def test_plain_message_is_atomic_idempotent_and_creates_no_jobs(collab):
    s, owner, *_ = collab
    args = message(collab, body_text='@Work <script>alert(1)</script> password=synthetic-sensitive')
    saved = s.chatroom.create(args, owner)
    assert saved['scheduled'] is False and saved['notifications'] == []
    assert saved['message']['server_sequence'] == 1
    assert saved['message']['body_text'].endswith('password=[REDACTED]')
    assert s.chatroom.create(args, owner) == saved
    assert s.chatroom.create({**args, 'idempotency_key': key()}, owner)['message']['id'] == saved['message']['id']
    assert s.store.all('SELECT * FROM collaboration_jobs') == []
    assert s.store.all('SELECT * FROM collaboration_goals') == []
    assert s.store.all('SELECT * FROM mcp_event_outbox') == []
    for update in ({'body_text': 'changed'}, {'body_text': 'changed', 'idempotency_key': key()}):
        with pytest.raises(DevError):
            s.chatroom.create({**args, **update}, owner)
    for update in ({'author': 'owner'}, {'body_text': '   '}, {'body_text': 'x' * 8001}, {'body_text': '\ud800'}, {'slot_id': 'fake'}):
        with pytest.raises(DevError):
            s.chatroom.create({**message(collab), **update}, owner)


def test_speech_requires_separate_owner_permission_and_rechecks_replays(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    slot = join(collab)
    args = message(collab)
    with pytest.raises(DevError, match='发言权限'):
        s.chatroom.create(args, worker)
    with pytest.raises(DevError):
        s.chatroom.access({**scope, 'room_id': room['id'], 'grant_id': worker.grant_id, 'enabled': True, 'expected_version': 0, 'idempotency_key': key()}, worker)
    grant_speech(collab)
    saved = s.chatroom.create(args, worker)['message']
    assert saved['author'] == 'worker' and saved['origin'] == 'bound_connector'
    assert saved['chat_identity_verified'] is False and saved['author'] != slot['id']
    grant_speech(collab, enabled=False)
    with pytest.raises(DevError):
        s.chatroom.create(args, worker)
    grant_speech(collab)
    clock[0] += 8 * 86400
    with pytest.raises(DevError):
        s.chatroom.create(message(collab), worker)


def test_replies_pagination_search_and_monotone_read_cursor(collab):
    s, owner, worker, _, room, _, _, scope = collab
    first = s.chatroom.create(message(collab), owner)['message']
    second = s.chatroom.create(message(collab, reply_to_id=first['id'], body_text='second'), owner)['message']
    third = s.chatroom.create(message(collab, reply_to_id=second['id'], body_text='third'), owner)['message']
    assert third['thread_root_id'] == first['id'] and third['reply_to_id'] == second['id']
    page = s.read({**scope, 'kind': 'timeline', 'limit': 2}, owner)
    assert [m['id'] for m in page['items']] == [second['id'], third['id']]
    older = s.read({**scope, 'kind': 'timeline', 'cursor': page['next_cursor'], 'limit': 2}, owner)
    assert [m['id'] for m in older['items']] == [first['id']]
    fourth = s.chatroom.create(message(collab, body_text='fourth'), owner)['message']
    added = s.read({**scope, 'kind': 'timeline', 'after': page['after_cursor']}, owner)
    assert [m['id'] for m in added['items']] == [fourth['id']]
    assert len(s.read({**scope, 'kind': 'thread', 'id': second['id']}, owner)['items']) == 3
    assert len(s.read({**scope, 'kind': 'search', 'query': '100%_'}, owner)['items']) == 1
    assert s.read({**scope, 'kind': 'search', 'query': "' OR 1=1 --"}, owner)['items'] == []
    for seq in (4, 2):
        assert s.chatroom.set_read_cursor({**scope, 'room_id': room['id'], 'last_seen_sequence': seq, 'idempotency_key': key()}, owner)['read_sequence'] == 4
    with pytest.raises(DevError):
        s.chatroom.set_read_cursor({**scope, 'room_id': room['id'], 'last_seen_sequence': 99, 'idempotency_key': key()}, owner)
    with pytest.raises(DevError):
        s.read({**scope, 'kind': 'timeline', 'after': page['after_cursor']}, worker)
    another = s.room_create({**scope, 'environment_id': 'staging', 'idempotency_key': key()}, owner)['room']
    with pytest.raises(DevError):
        s.chatroom.create(message(collab, environment_id='staging', room_id=another['id'], reply_to_id=first['id']), owner)
    with pytest.raises(DevError):
        s.read({**scope, 'kind': 'thread', 'room_id': another['id'], 'id': first['id']}, owner)
    assert len(s.read({**scope, 'environment_id': 'missing', 'kind': 'rooms'}, owner)['items']) == 2


def test_conversion_requires_owner_version_and_eligible_worker_and_keeps_snapshot(collab):
    s, owner, worker, _, room, agents, _, scope = collab
    source = s.chatroom.create(message(collab), owner)['message']
    slot = join(collab)
    args = {**scope, 'room_id': room['id'], 'message_id': source['id'], 'expected_message_version': 1,
            'assignee_agent_id': agents[0]['id'], 'kind': 'analyze_incident', 'request': 'Investigate synthetic fixture',
            'acceptance': 'Evidence-backed explanation', 'idempotency_key': key()}
    for principal, update in [(worker, {}), (owner, {'expected_message_version': 2}), (owner, {'assignee_agent_id': slot['id']})]:
        with pytest.raises(DevError):
            s.chatroom.to_task({**args, **update}, principal)
        assert s.store.all('SELECT * FROM collaboration_jobs') == []
    result = s.chatroom.to_task(args, owner)
    assert s.chatroom.to_task(args, owner) == result
    assert s.chatroom.to_task({**args, 'idempotency_key': key()}, owner)['job_id'] == result['job_id']
    assert len(s.store.all('SELECT * FROM collaboration_jobs')) == 1
    job = s.read({**scope, 'kind': 'job', 'id': result['job_id']}, worker)
    assert job['context']['source_snapshot']['source_body_text'] == source['body_text']
    lease = s.claim({**scope, 'job_id': result['job_id'], 'expected_version': 1, 'idempotency_key': key()}, worker)
    s.submit({**scope, 'job_id': result['job_id'], 'attempt': lease['attempt'], 'fencing_token': lease['fencing_token'],
              'idempotency_key': key(), 'result': {'outcome': 'inconclusive', 'summary': 'Need evidence'}}, worker)
    thread = s.read({**scope, 'kind': 'thread', 'id': source['id']}, owner)['items']
    assert len(thread) == 3 and thread[-1]['kind'] == 'agent_result'
    change = s.read({**scope, 'kind': 'changes'}, owner)
    assert {'job', 'message', 'goal'} <= {item['kind'] for item in change['items']}
    assert s.read({**scope, 'kind': 'changes', 'cursor': change['next_cursor']}, owner)['items'] == []


@pytest.mark.asyncio
async def test_mentions_require_new_opt_in_and_route_only_exact_slot_without_peer_loops(collab):
    s, owner, worker, _, room, _, _, scope = collab
    event_service, receiver = events(collab)
    one, two = join(collab, label='one'), join(collab, label='two')
    await event_service.subscribe(subscription(collab, one, name=STATUS_EVENT), worker)
    before = s.chatroom.create(message(collab, mentions=[{'slot_id': one['id']}]), owner)
    assert before['notifications'][0]['state'] == 'not_subscribed'
    for slot, suffix in ((one, 'one'), (two, 'two')):
        await event_service.subscribe(subscription(collab, slot, suffix), worker)
    msg = s.chatroom.create(message(collab, mentions=[{'slot_id': one['id']}]), owner)
    assert msg['notifications'][0]['state'] == 'queued'
    await event_service.tick()
    delivered = [r for r in receiver.requests if r.get('data', {}).get('message_id') == msg['message']['id']]
    assert len(delivered) == 1 and delivered[0]['data']['recipient_slot_id'] == one['id']
    status = s.read({**scope, 'kind': 'message_status', 'id': msg['message']['id']}, owner)
    assert status['notifications'][0]['state'] == 'accepted' and not status['notifications'][0]['read_verified']
    assert any(item['kind'] == 'message_delivery' for item in s.read({**scope, 'kind': 'changes'}, owner)['items'])
    assert 'body_text' not in delivered[0]['data'] and 'request' not in delivered[0]['data']
    grant_speech(collab)
    reply = s.chatroom.create(message(collab, reply_to_id=msg['message']['id'], mentions=[{'slot_id': two['id']}]), worker)
    assert reply['notifications'][0]['state'] == 'suppressed_agent_reply'
    assert len(s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (MESSAGE_EVENT,))) == 1
    no_slot = subscription(collab, one)
    del no_slot['arguments']['slot_id']
    with pytest.raises(DevError):
        await event_service.subscribe(no_slot, worker)
    no_conversation = subscription(collab, one)
    del no_conversation['arguments']['conversation_id']
    with pytest.raises(DevError):
        await event_service.subscribe(no_conversation, worker)
    assert all(r['name'] != MESSAGE_EVENT for r in s.joining.subscription_requests(room, s.joining.slot(room, one['id'])))
    members = s.read({**scope, 'kind': 'members'}, owner)['items']
    assert all(m['can_speak'] and m['message_notification_state'] == 'active' for m in members)


def test_migration_is_additive_idempotent_and_preserves_paused_room_and_joins(collab):
    s, owner, _, _, room, _, _, scope = collab
    first = s.chatroom.create(message(collab), owner)['message']
    joined = join(collab)
    s.store.execute("UPDATE collaboration_rooms SET state='paused' WHERE id=?", (room['id'],))
    before = s.store.all('SELECT * FROM collaboration_messages')
    with s.store.transaction():
        migrate(s.store.db)
        migrate(s.store.db)
    assert s.store.all('SELECT * FROM collaboration_messages') == before
    assert s.store.one('SELECT state FROM collaboration_rooms')['state'] == 'paused'
    assert s.store.one('SELECT id FROM collaboration_join_slots')['id'] == joined['id']
    with pytest.raises(DevError):
        s.chatroom.create(message(collab), owner)
    assert s.read({**scope, 'kind': 'timeline'}, owner)['items'][0]['id'] == first['id']


@pytest.mark.asyncio
async def test_discussion_notifications_require_independent_conversation_consent(collab):
    s, owner, worker, _, room, _, _, scope = collab
    event_service, receiver = events(collab)
    slot = join(collab)
    old = subscription(collab, slot)
    await event_service.subscribe(old, worker)
    conversation = s.conversations.create({'title': 'New independent room', 'projects': [scope], 'idempotency_key': key()}, owner)['conversation']
    args = message(collab, conversation_id=conversation['id'], mentions=[{'slot_id': slot['id']}])
    assert s.chatroom.create(args, owner)['notifications'][0]['state'] == 'not_subscribed'
    optin = subscription(collab, slot, 'independent')
    optin['arguments']['conversation_id'] = conversation['id']
    await event_service.subscribe(optin, worker)
    created = s.chatroom.create(message(collab, conversation_id=conversation['id'], mentions=[{'slot_id': slot['id']}]), owner)
    await event_service.tick()
    delivered = [r for r in receiver.requests if r.get('data', {}).get('message_id') == created['message']['id']]
    assert len(delivered) == 1 and delivered[0]['data']['conversation_id'] == conversation['id']
    assert all(r.get('data', {}).get('message_id') != args['client_message_id'] for r in receiver.requests)
    assert s.store.all('SELECT * FROM collaboration_jobs') == []


@pytest.mark.asyncio
async def test_later_route_without_replay_does_not_claim_queued_old_delivery(collab):
    s, owner, worker, _, room, _, _, scope = collab
    event_service, _ = events(collab)
    slot = join(collab)
    old = await event_service.subscribe(subscription(collab, slot, 'before'), worker)
    msg = s.chatroom.create(message(collab, mentions=[{'slot_id': slot['id']}]), owner)['message']
    s.store.execute("UPDATE mcp_event_subscriptions SET state='paused' WHERE id=?", (old['id'],))
    await event_service.subscribe(subscription(collab, slot, 'after'), worker)
    status = s.read({**scope, 'kind': 'message_status', 'id': msg['id']}, owner)
    assert status['notifications'][0]['state'] == 'recipient_unavailable'
