"""Room-title provenance is read-only presentation metadata, never authorization."""
import pytest

from tests.collaboration_support import collab as collab, key
from tests.test_conversation_project_scopes import fixture


def test_generated_title_metadata_is_exact_and_read_only(collab):
    c = fixture(collab)
    room = c.s.conversations.object(c.owner, c.room['id'])
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    assert c.s.conversations.title_is_generated(room) is True
    assert c.s.conversations.view(c.owner, room)['title_is_generated'] is True
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert c.s.conversations.title_is_generated({**room, 'title': room['title'] + ' · 自定义'}) is False
    assert c.s.conversations.title_is_generated({**room, 'id': 'missing-original-room'}) is False


@pytest.mark.parametrize('title', ['ProjectAlpha 团队讨论', 'proj · production', 'proj · production · 计划'])
def test_explicit_title_is_never_marked_generated(collab, title):
    c = fixture(collab)
    created = c.s.conversations.create({'title': title, 'projects': [
        {'project': 'proj', 'environment_id': 'production'}], 'idempotency_key': key()}, c.owner)['conversation']
    assert created['title'] == title
    assert created['title_is_generated'] is False


def test_original_named_room_is_not_generated_even_when_pattern_matches(collab):
    c = fixture(collab)
    room = c.s.conversations.object(c.owner, c.room['id'])
    c.s.store.execute('UPDATE collaboration_rooms SET title=? WHERE id=?', (room['title'], c.room['id']))
    assert c.s.conversations.view(c.owner, room)['title_is_generated'] is False
