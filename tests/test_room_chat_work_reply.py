"""Unified discussion, original input handling and optional publication."""
import pytest

from hub.collaboration.common import digest, validate
from shared.room_runtime_contracts import RoomMemberReply
from shared.util import DevError
from tests.collaboration_support import collab, key
from tests.test_room_consumer_sessions import begin, connect, credentials, identity, rooms, send, verify
from tests.test_room_goal_scope_boundaries import admit_read, final_request, report, start
from tests.test_room_managed_work import connected


@pytest.mark.asyncio
async def test_complete_explanation_preserves_input_until_explicitly_handled(rooms):
    c = await connect(rooms)
    verify(c)
    source = send(c, '先解释原因，能修就修。')
    _, turn = begin(c)
    inbox = c.s.room_consumers.inbox(identity(c.joined), c.actor)
    item = next(item for item in inbox['items'] if item['object_id'] == source['message']['id'])
    raw = {**credentials(c, turn), **item['reply_arguments'], 'body_text': '原因已定位，修复尚未准入。',
           'idempotency_key': key(), 'publish_work': False, 'input_handling': 'pending', 'yield_after': True}
    result = c.s.room_runtime.reply(raw, c.actor)
    assert result['complete'] and not result['yielded_at_commit']
    entry = c.s.store.one('SELECT acknowledged_at FROM dot_mailbox_entries WHERE id=?', (item['id'],))
    assert entry['acknowledged_at'] is None
    with pytest.raises(DevError) as conflict:
        c.s.room_runtime.reply({**raw, 'input_handling': 'answered'}, c.actor)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert c.s.room_runtime.reply(raw, c.actor)['message']['id'] == result['message']['id']


@pytest.mark.asyncio
async def test_related_discussion_and_formal_publication_have_separate_effects(collab):
    s, owner, actor, _, identity, credentials = await connected(collab, [{'project': 'proj'}], [{'project_id': 'proj'}])
    work, _ = start(s, owner, actor, identity, credentials, project_ids=['proj'])
    goal_id = work['goal_id']
    followup = s.room_runtime.send({'conversation_id': identity['conversation_id'], 'member_id': identity['member_id'],
        'work_id': goal_id, 'revise_work_intent': False, 'body_text': '目前为什么选这个办法？',
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    source = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (followup['id'],))
    raw = final_request(s, identity, credentials, actor, source, [], body_text='因为证据支持这个办法。', outcome=None)
    assert 'publish_work' not in validate(RoomMemberReply, {**raw, 'yield_after': False})
    assert validate(RoomMemberReply, {**raw, 'publish_work': False})['publish_work'] is False
    result = s.room_runtime.reply({**raw, 'publish_work': False, 'input_handling': 'answered'}, actor)
    assert result['message']['reply_to_id'] == followup['id']
    assert not s.store.one('SELECT 1 FROM room_goal_publications WHERE goal_id=?', (goal_id,))
    with pytest.raises(DevError) as changed:
        s.room_runtime.reply(raw, actor)
    assert changed.value.code == 'IDEMPOTENCY_CONFLICT'

    item = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal_id,))
    lease, operation = admit_read(s, identity, credentials, actor, goal_id, item, 'proj')
    finished = report(s, identity, credentials, actor, goal_id, lease, operation)
    refs = [{'work_item_id': finished['id'], 'version': finished['version'], 'digest': digest(finished['result'])}]
    raw = final_request(s, identity, credentials, actor, source, refs, publish_work=True)
    published = s.room_runtime.reply(raw, actor)
    assert published['message']['reply_to_id'] == followup['id']
    assert published['message']['body']['artifact_refs'] == refs
    assert s.room_runtime.intent_revision(goal_id) == 1
    assert len(s.store.all('SELECT * FROM room_goal_publications WHERE goal_id=?', (goal_id,))) == 1
