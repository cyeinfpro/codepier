"""Independent conversations, mid-stream project addition and per-project ACL."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab, key


def send(c, room, text, conversation=None, principal=None, **extra):
    return c[0].chatroom.create({'project': room['project_id'], 'environment_id': room['environment_id'],
        'room_id': room['id'], 'conversation_id': conversation or room['id'], 'body_text': text,
        'client_message_id': key(), 'idempotency_key': key(), **extra}, principal or c[1])['message']


def create(c, *projects):
    return c[0].conversations.create({'title': 'Independent discussion', 'projects': [{'project': project} for project in projects],
                                      'idempotency_key': key()}, c[1])['conversation']


def read(c, conversation, *, room=None, actor=None, kind='timeline', **extra):
    room = room or c[4]
    return c[0].read({'project': room['project_id'], 'environment_id': room['environment_id'],
        'room_id': room['id'], 'conversation_id': conversation, 'kind': kind, **extra}, actor or c[1])


def permit(c, conversation, room, actor=None):
    return c[0].chatroom.access({'project': room['project_id'], 'environment_id': room['environment_id'],
        'room_id': room['id'], 'conversation_id': conversation, 'grant_id': (actor or c[2]).grant_id,
        'enabled': True, 'expected_version': 0, 'idempotency_key': key()}, c[1])


def test_new_rooms_do_not_import_history_and_default_id_survives_addition(collab):
    s, owner, _, _, original, _, _, scope = collab
    old = send(collab, original, 'original-history')
    other = s.room_create({'project': 'otherproj', 'idempotency_key': key()}, owner)['room']
    foreign = send(collab, other, 'separate-history')
    fresh = create(collab, 'proj', 'otherproj')
    assert read(collab, fresh['id'])['items'] == []
    assert read(collab, fresh['id'], kind='messages')['items'] == []
    one = send(collab, original, 'project A', fresh['id'])
    two = send(collab, other, 'project B', fresh['id'])
    page = read(collab, fresh['id'])
    assert [item['id'] for item in page['items']] == [one['id'], two['id']]
    assert [item['server_sequence'] for item in page['items']] == [1, 2]
    assert {item['project_id'] for item in page['items']} == {'proj', 'otherproj'}
    default = s.read(scope, owner)['conversation']
    assert default['id'] == original['id']
    args = {'conversation_id': default['id'], 'expected_version': default['version'], 'project': 'otherproj', 'idempotency_key': key()}
    added = s.conversations.add_project(args, owner)
    assert s.conversations.add_project(args, owner) == added
    assert added['conversation']['id'] == original['id'] and added['conversation']['version'] == 2
    assert [item['id'] for item in read(collab, default['id'])['items']] == [old['id']]
    assert foreign['id'] not in {item['id'] for item in read(collab, default['id'])['items']}
    third = send(collab, other, 'added without importing history', default['id'])
    assert [item['id'] for item in read(collab, default['id'])['items']] == [old['id'], third['id']]
    assert [item['id'] for item in s.read({**scope, 'kind': 'timeline'}, owner)['items']] == [old['id']]
    assert [item['id'] for item in s.read({'project': 'otherproj', 'kind': 'timeline'}, owner)['items']] == [foreign['id']]
    repeated = s.conversations.add_project({**args, 'expected_version': 2, 'idempotency_key': key()}, owner)
    assert not repeated['added'] and repeated['conversation']['version'] == 2
    with pytest.raises(DevError) as exc:
        s.conversations.add_project({**args, 'idempotency_key': key()}, owner)
    assert exc.value.code == 'STALE_VERSION'


def test_project_permissions_filter_history_search_changes_and_cursor_after_revocation(collab):
    s, owner, worker, _, original, _, _, scope = collab
    group = create(collab, 'proj', 'otherproj')
    other = s.store.one("SELECT * FROM collaboration_rooms WHERE project_id='otherproj'")
    a = send(collab, original, 'visible-project-secret', group['id'])
    b = send(collab, other, 'restricted-project-secret', group['id'])
    limited = read(collab, group['id'], actor=worker)
    assert [m['id'] for m in limited['items']] == [a['id']]
    assert [p['project_id'] for p in limited['conversation']['projects']] == ['proj']
    assert read(collab, group['id'], actor=worker, kind='search', query='restricted-project-secret')['items'] == []
    assert b['id'] not in {item['object_id'] for item in read(collab, group['id'], actor=worker, kind='changes')['items']}
    with pytest.raises(DevError):
        read(collab, group['id'], actor=worker, room=other)
    s.store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['proj', 'otherproj']), worker.grant_id))
    broad = read(collab, group['id'], actor=worker)
    assert len(broad['items']) == 2
    changed = read(collab, group['id'], actor=worker, kind='changes')
    s.store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['proj']), worker.grant_id))
    with pytest.raises(DevError):
        read(collab, group['id'], actor=worker, after=broad['after_cursor'])
    with pytest.raises(DevError):
        read(collab, group['id'], actor=worker, kind='changes', cursor=changed['next_cursor'])
    assert read(collab, group['id'], actor=worker)['visibility_token'] != broad['visibility_token']
    assert [m['id'] for m in read(collab, group['id'], actor=worker)['items']] == [a['id']]


def test_speech_not_inherited_by_new_conversation_or_added_project_and_no_cross_reply(collab):
    s, owner, worker, _, room, _, _, _ = collab
    s.store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['proj', 'otherproj']), worker.grant_id))
    group = create(collab, 'proj')
    other_group = create(collab, 'proj')
    permit(collab, group['id'], room)
    source = send(collab, room, 'authorized speech', group['id'], principal=worker)
    with pytest.raises(DevError):
        send(collab, room, 'not inherited', other_group['id'], principal=worker)
    s.conversations.add_project({'conversation_id': group['id'], 'expected_version': 1,
                                'project': 'otherproj', 'idempotency_key': key()}, owner)
    other = s.store.one("SELECT * FROM collaboration_rooms WHERE project_id='otherproj'")
    with pytest.raises(DevError):
        send(collab, other, 'not inherited on add', group['id'], principal=worker)
    with pytest.raises(DevError):
        send(collab, room, 'wrong conversation', other_group['id'], reply_to_id=source['id'])
    with pytest.raises(DevError):
        send(collab, other, 'wrong project', group['id'], reply_to_id=source['id'])
    assert s.store.all('SELECT * FROM collaboration_jobs') == []


def test_tasks_and_results_keep_conversation_and_source_project(collab):
    s, owner, worker, _, room, agents, _, scope = collab
    group = create(collab, 'proj', 'otherproj')
    unrelated = create(collab, 'proj')
    source = send(collab, room, 'analyze this project', group['id'])
    args = {**scope, 'room_id': room['id'], 'conversation_id': group['id'], 'message_id': source['id'],
        'expected_message_version': 1, 'assignee_agent_id': agents[0]['id'], 'kind': 'analyze_incident',
        'request': 'Inspect this project', 'acceptance': 'Bounded evidence', 'idempotency_key': key()}
    result = s.chatroom.to_task(args, owner)
    with pytest.raises(DevError):
        s.chatroom.to_task({**args, 'conversation_id': unrelated['id'], 'idempotency_key': key()}, owner)
    assert read(collab, unrelated['id'], kind='overview')['jobs'] == []
    assert read(collab, unrelated['id'], kind='overview')['goals'] == []
    assert read(collab, unrelated['id'], kind='overview')['counts']['open_jobs'] == 0
    with pytest.raises(DevError):
        read(collab, unrelated['id'], kind='job', id=result['job_id'])
    from hub.collaboration.common import canonical, digest
    evidence_body = canonical({'observations': ['synthetic']})
    s.store.execute('''INSERT INTO monitor_evidence (id,room_id,body,digest,created,expires_at)
        VALUES (?,?,?,?,?,?)''', ('fixture_evidence', room['id'], evidence_body, digest(evidence_body), s.clock(), s.clock()+3600))
    context = json.loads(s.store.one('SELECT context FROM collaboration_jobs WHERE id=?', (result['job_id'],))['context'])
    context['evidence_refs'] = ['fixture_evidence']
    s.store.execute('UPDATE collaboration_jobs SET context=? WHERE id=?', (canonical(context), result['job_id']))
    lease = s.claim({**scope, 'job_id': result['job_id'], 'expected_version': 1, 'idempotency_key': key()}, worker)
    submitted = s.submit({**scope, 'job_id': result['job_id'], 'attempt': lease['attempt'], 'fencing_token': lease['fencing_token'],
                          'idempotency_key': key(), 'result': {'outcome': 'explained', 'summary': 'Synthetic explanation', 'observations': [{'claim': 'Fixture', 'evidence_refs': ['fixture_evidence']}]}}, worker)
    assert len(read(collab, group['id'], kind='thread', id=source['id'])['items']) == 3
    assert read(collab, unrelated['id'])['items'] == []
    with pytest.raises(DevError):
        read(collab, unrelated['id'], kind='result', id=submitted['result_id'])
    assert s.read({**scope, 'kind': 'timeline'}, owner)['items'] == []
    assert read(collab, group['id'], kind='evidence', id='fixture_evidence', result_id=submitted['result_id'])['id'] == 'fixture_evidence'
    with pytest.raises(DevError):
        read(collab, unrelated['id'], kind='evidence', id='fixture_evidence', result_id=submitted['result_id'])
    with pytest.raises(DevError):
        read(collab, group['id'], kind='evidence', id='fixture_evidence')


def test_overview_pagination_is_bound_to_the_actual_conversation(collab):
    s, owner, _, _, room, _, _, scope = collab
    for number in range(3):
        send(collab, room, 'default ' + str(number))
    group = create(collab, 'proj')
    first = send(collab, room, 'new first', group['id'])
    second = send(collab, room, 'new second', group['id'])
    overview = read(collab, group['id'], kind='overview', limit=1)
    assert [item['id'] for item in overview['messages']] == [second['id']]
    page = read(collab, group['id'], kind='messages', cursor=overview['messages_next_cursor'], limit=1)
    assert [item['id'] for item in page['items']] == [first['id']]
    assert not overview['jobs_next_cursor'] and not overview['goals_next_cursor']


def test_add_project_replay_reprojects_revoked_existing_project(collab):
    s, owner, _, _, original, _, _, _ = collab
    group = create(collab, 'proj')
    args = {'conversation_id': group['id'], 'expected_version': 1,
            'project': 'otherproj', 'idempotency_key': key()}
    added = s.conversations.add_project(args, owner)
    assert {p['project_id'] for p in added['conversation']['projects']} == {'proj', 'otherproj'}
    # A deleted/unmapped project remains a history tombstone, never read authority.
    s.store.execute("DELETE FROM projects WHERE id='proj'")
    replay = s.conversations.add_project(args, owner)
    assert replay['added'] == added['added'] and replay['conversation']['id'] == group['id']
    assert [p['project_id'] for p in replay['conversation']['projects']] == ['otherproj']
    assert replay['conversation']['visibility_token'] != added['conversation']['visibility_token']
    assert s.store.one('SELECT COUNT(*) AS n FROM conversation_projects WHERE conversation_id=?', (group['id'],))['n'] == 2


def test_create_replay_reprojects_current_version_without_cached_project_list(collab):
    s, owner, _, _, _, _, _, _ = collab
    args = {'title': 'Stable creation receipt', 'projects': [{'project': 'proj'}], 'idempotency_key': key()}
    original = s.conversations.create(args, owner)['conversation']
    s.conversations.add_project({'conversation_id': original['id'], 'expected_version': 1,
                                'project': 'otherproj', 'idempotency_key': key()}, owner)
    current = s.conversations.create(args, owner)['conversation']
    assert current['version'] == 2 and len(current['projects']) == 2
    s.store.execute("DELETE FROM projects WHERE id='otherproj'")
    replay = s.conversations.create(args, owner)['conversation']
    assert replay['id'] == original['id'] and replay['version'] == 2
    assert [p['project_id'] for p in replay['projects']] == ['proj']
    for row in s.store.all('SELECT response FROM conversation_idempotency'):
        assert set(json.loads(row['response'])['conversation']) == {'id'}
