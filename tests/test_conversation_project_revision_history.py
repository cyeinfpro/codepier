"""Cross-partition history keeps live source authorization and provenance."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import inbox, owner_message, send_dot
from tests.test_collaboration_dots import call
from tests.test_conversation_project_revision import fixture, select, initial_work, claim, admit


SECRET = 'synthetic-sensitive-other-project-history'


def context(collab, *, revised=True):
    c = fixture(collab)
    c.s.conversations.add_project({
        'project': 'otherproj', 'environment_id': 'production', 'conversation_id': c.room['id'],
        'expected_version': 1, 'idempotency_key': key(),
    }, c.owner)
    other = c.s.store.one("SELECT * FROM collaboration_rooms WHERE project_id='otherproj'")
    original = c.s.chatroom.create({
        'project': 'otherproj', 'environment_id': 'production', 'room_id': other['id'],
        'conversation_id': c.room['id'], 'body_text': SECRET, 'mentions': [],
        'client_message_id': key(), 'idempotency_key': key(),
    }, c.owner)['message']
    if revised:
        select(c, ['proj'])
    return c, other, original


def thread(c, identifier, *, project='proj', conversation=None):
    return c.s.read({
        'project': project, 'environment_id': 'production',
        'conversation_id': conversation or c.room['id'], 'kind': 'thread', 'id': identifier,
    }, c.actor)


def raw_message(c, identifier):
    return c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (identifier,))


def provenance(c, identifier):
    return set(json.loads(raw_message(c, identifier)['body']).get('provenance_project_ids', []))


def cross_reply(c, source):
    return owner_message(c, 'Please discuss the historical source safely.',
                         reply_to_id=source['id'])[0]['message']


def revoke_source(c):
    c.s.store.execute("UPDATE grants SET projects='[\"proj\"]' WHERE id='broad'")


def delegated(c, reply):
    item = next(row for row in inbox(c)['items'] if row['message_id'] == reply['id'])
    value = call(c, item['task_request'])
    record = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (value['delegation_id'],))
    return {**value, 'goal_id': record['goal_id']}


def test_anchor_thread_reads_historical_partition_and_reply_chain_preserves_provenance(collab):
    c, _, original = context(collab)
    assert thread(c, original['id'])['items'][0]['id'] == original['id']
    first = cross_reply(c, original)
    assert raw_message(c, first['id'])['room_id'] == c.room['id']
    assert first['thread_root_id'] == original['thread_root_id']
    assert provenance(c, first['id']) == {'proj', 'otherproj'}
    item = next(row for row in inbox(c)['items'] if row['message_id'] == first['id'])
    assert call(c, item['thread_request'])['items'][0]['id'] == original['id']
    answer = send_dot(c, 'A derived synthetic answer.', first['id'])['message']
    assert provenance(c, answer['id']) == {'proj', 'otherproj'}
    followup = owner_message(c, 'Continue in this original thread.', mentions=[],
                             reply_to_id=answer['id'])[0]['message']
    assert followup['mentions'][0]['slot_id'] == c.slot['id']
    assert provenance(c, followup['id']) == {'proj', 'otherproj'}
    assert next(row for row in inbox(c)['items'] if row['message_id'] == followup['id'])
    second_answer = send_dot(c, 'The source identity remains attached.', followup['id'])['message']
    assert provenance(c, second_answer['id']) == {'proj', 'otherproj'}
    assert second_answer['thread_root_id'] == original['thread_root_id']


@pytest.mark.parametrize('action', ['inbox', 'thread_source', 'thread_derived', 'reply_source', 'reply_derived'])
def test_lost_source_grant_blocks_history_inbox_and_reply_without_leaking_source(collab, action):
    c, _, original = context(collab)
    reply = cross_reply(c, original)
    count = c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n']
    revoke_source(c)
    with pytest.raises(DevError) as error:
        if action == 'inbox':
            inbox(c)
        elif action == 'thread_source':
            thread(c, original['id'])
        elif action == 'thread_derived':
            thread(c, reply['id'])
        else:
            send_dot(c, 'Must reject derived reply.', original['id'] if action == 'reply_source' else reply['id'])
    assert SECRET not in str(error.value)
    assert c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == count
    listing = c.s.read({**c.scope, 'conversation_id': c.room['id'], 'kind': 'messages'}, c.actor)
    assert SECRET not in json.dumps(listing)
    assert not any(row['id'] in {original['id'], reply['id']} for row in listing['items'])


def test_cached_dot_reply_is_not_an_authority_bypass_after_source_revocation(collab):
    c, _, original = context(collab)
    source = cross_reply(c, original)
    raw = {'action': 'dot_message', **c.scope, 'dot_id': c.slot['id'],
           'reply_to_id': source['id'], 'body_text': SECRET + ' derived reply', 'idempotency_key': key()}
    c.s.invoke('collaboration', raw, c.actor)
    count = c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n']
    revoke_source(c)
    with pytest.raises(DevError) as error:
        c.s.invoke('collaboration', raw, c.actor)
    assert SECRET not in str(error.value)
    assert c.s.store.one('SELECT COUNT(*) AS n FROM collaboration_messages')['n'] == count


def test_other_conversation_message_id_is_never_resolved_through_anchor(collab):
    c, other, _ = context(collab)
    foreign = c.s.chatroom.create({
        'project': 'otherproj', 'environment_id': 'production', 'room_id': other['id'],
        'conversation_id': other['id'], 'body_text': 'Separate synthetic conversation.',
        'client_message_id': key(), 'idempotency_key': key(),
    }, c.owner)['message']
    with pytest.raises(DevError):
        thread(c, foreign['id'])
    with pytest.raises(DevError):
        send_dot(c, 'Must not cross conversations.', foreign['id'])


def test_legacy_room_without_revision_keeps_original_partition_restriction(collab):
    c, _, original = context(collab, revised=False)
    assert thread(c, original['id'], project='otherproj')['items'][0]['id'] == original['id']
    with pytest.raises(DevError):
        thread(c, original['id'])
    with pytest.raises(DevError):
        send_dot(c, 'Legacy connection cannot gain cross-partition reply rights.', original['id'])


def test_task_conversion_progress_result_and_followup_keep_source_provenance(collab):
    c, _, original = context(collab)
    source = cross_reply(c, original)
    task = delegated(c, source)
    assert provenance(c, source['id']) == {'proj', 'otherproj'}
    lease = claim(c, task, initial_work(c, task))
    raw_lease = {**c.scope, 'goal_id': task['goal_id'], 'work_item_id': lease['id'],
                 'attempt': lease['attempt'], 'fencing_token': lease['fencing_token']}
    progress = c.s.invoke('collaboration_work', {
        'action': 'progress', **raw_lease, 'summary': 'Synthetic progress without execution.',
        'idempotency_key': key(),
    }, c.actor)
    assert provenance(c, progress['message_id']) == {'proj', 'otherproj'}
    c.s.invoke('collaboration_work', {
        'action': 'result', **raw_lease, 'outcome': 'blocked',
        'summary': 'Synthetic fixture is waiting; no execution took place.',
        'operation_ids': [], 'idempotency_key': key(),
    }, c.actor)
    result = c.s.store.one("SELECT * FROM collaboration_messages WHERE goal_id=? AND kind='delegation_result'",
                           (task['goal_id'],))
    assert result and provenance(c, result['id']) == {'proj', 'otherproj'}
    followup = owner_message(c, 'Discuss that result.', mentions=[], reply_to_id=result['id'])[0]['message']
    assert provenance(c, followup['id']) == {'proj', 'otherproj'}
    revoke_source(c)
    assert not c.s.chatroom.message_visible(c.actor, result)
    with pytest.raises(DevError):
        send_dot(c, 'Must not launder derived result.', followup['id'])


@pytest.mark.parametrize('action', ['delegation_read', 'goal_detail', 'claim', 'execute'])
def test_lost_history_source_authority_blocks_existing_task_reads_and_new_steps(collab, action):
    c, _, original = context(collab)
    source = cross_reply(c, original)
    task = delegated(c, source)
    item = initial_work(c, task)
    lease = claim(c, task, item) if action == 'execute' else None
    revoke_source(c)
    with pytest.raises(DevError) as error:
        if action == 'delegation_read':
            call(c, task['read_request'])
        elif action == 'goal_detail':
            c.s.read({**c.scope, 'conversation_id': c.room['id'], 'kind': 'goal', 'id': task['goal_id']}, c.actor)
        elif action == 'claim':
            claim(c, task, item)
        else:
            admit(c, task, lease)
    assert SECRET not in str(error.value)
    assert not c.s.store.all('SELECT * FROM operations')


def test_narrowed_grant_room_metadata_only_returns_visible_working_projects(collab):
    from hub.principal import refresh_principal

    c = fixture(collab)
    select(c, ['proj', 'otherproj'])
    saved = c.s.conversations.working_scopes.current(c.room['id'])
    revoke_source(c)
    actor = refresh_principal(c.s.store, c.actor)
    views = [
        next(row for row in c.s.conversations.listing(actor)['items'] if row['id'] == c.room['id']),
        c.s.read({**c.scope, 'conversation_id': c.room['id'], 'kind': 'overview'}, c.actor)['conversation'],
    ]
    for view in views:
        assert {row['project_id'] for row in view['projects']} == {'proj'}
        assert view['working_scope']['project_ids'] == ['proj']
        assert view['working_scope']['version'] == saved['version']
        assert view['working_scope']['digest'] == saved['digest']
        assert view['working_scope']['dot_id'] == c.slot['id']
        assert view['connection']['project_id'] == 'proj'
    assert c.s.conversations.working_scopes.current(c.room['id']) == saved


def test_child_only_grant_room_metadata_hides_anchor_connection_and_dot(collab):
    from hub.principal import refresh_principal

    c = fixture(collab)
    select(c, ['proj', 'otherproj'])
    saved = c.s.conversations.working_scopes.current(c.room['id'])
    c.s.store.execute("UPDATE grants SET projects='[\"otherproj\"]' WHERE id='broad'")
    actor = refresh_principal(c.s.store, c.actor)
    views = [
        next(row for row in c.s.conversations.listing(actor)['items'] if row['id'] == c.room['id']),
        c.s.read({'project': 'otherproj', 'environment_id': 'production',
                  'conversation_id': c.room['id'], 'kind': 'overview'}, c.actor)['conversation'],
    ]
    for view in views:
        assert {row['project_id'] for row in view['projects']} == {'otherproj'}
        assert view['working_scope']['project_ids'] == ['otherproj']
        assert view['working_scope']['version'] == saved['version']
        assert view['working_scope']['digest'] == saved['digest']
        assert 'dot_id' not in view['working_scope']
        assert 'connection' not in view
    assert c.s.conversations.working_scopes.current(c.room['id']) == saved
